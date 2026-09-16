/* Fleet operations. No client-generated readings; every chart uses API timestamps. */
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const enc = encodeURIComponent;
const colors = [
  "var(--amber)",
  "var(--blue)",
  "var(--teal)",
  "var(--compare3)",
  "var(--green)",
  "var(--red)",
];
const pretty = (name) =>
  name.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
const number = (n) =>
  Number(n).toLocaleString(undefined, { maximumFractionDigits: 3 });
const timestamp = (t) => (t ? new Date(t).toLocaleString() : "No reading yet");
const shortTime = (t) =>
  new Date(t).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
const ago = (t) => {
  if (!t) return "No reading yet";
  const s = Math.max(0, Math.floor((Date.now() - new Date(t)) / 1000));
  return s < 60
    ? `${s}s ago`
    : s < 3600
      ? `${Math.floor(s / 60)}m ago`
      : `${Math.floor(s / 3600)}h ago`;
};
const dot = (s) =>
  s === "critical" ? "crit" : s === "warning" ? "warn" : "ok";
const badges = (s) =>
  `<span class="badge ${s.simulated ? "" : "real"}">${s.simulated ? "SIM" : "REAL"}</span>${s.stale ? ' <span class="badge stale">STALE</span>' : ""}`;
const S = {
  tab: "overview",
  services: [],
  buoys: [],
  alerts: [],
  incidents: [],
  mutes: [],
  overview: null,
  selected: { services: null, sensors: null, analysis: null },
  metric: "temp",
  minutes: { services: 60, sensors: 60, analysis: 60 },
  windows: { services: null, sensors: null, analysis: null },
  compare: { services: new Set(), sensors: new Set() },
  comparing: { services: false, sensors: false },
  analysisMetrics: new Set(["battery", "solar_watts", "signal_dbm", "temp"]),
  expanded: new Set(),
  incidentFilter: "all",
  overviewFilter: null,
  drawer: null,
  sort: { services: { key: "name", dir: 1 }, sensors: { key: "id", dir: 1 } },
  lastSync: 0,
  refreshing: false,
  historySeq: { services: 0, sensors: 0, analysis: 0 },
  maintenance: {},
  lastError: null,
};
let toastTimer,
  formSubmit,
  map,
  tiles,
  markers = {},
  paletteIndex = 0,
  paletteResults = [];

async function api(path, method = "GET", body) {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(25000),
  });
  if (!response.ok) {
    let detail;
    try {
      detail = (await response.json()).detail;
    } catch {
      detail = `Request failed (${response.status})`;
    }
    if (Array.isArray(detail))
      detail = detail
        .map((e) => `${e.loc.slice(1).join(".")}: ${e.msg}`)
        .join("; ");
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return response.status === 204 ? null : response.json();
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").style.display = "block";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").style.display = "none"), 5000);
}
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
const numberFrames = new WeakMap();
function setText(id, value) {
  const el = $(id);
  if (!el || el.textContent === String(value)) return;
  const previous = Number(el.textContent.replace(/[, %]/g, ""));
  const next = Number(String(value).replace(/[, %]/g, ""));
  cancelAnimationFrame(numberFrames.get(el));
  if (
    !["rps-num", "batt-num"].includes(id) ||
    reducedMotion.matches ||
    !Number.isFinite(previous) ||
    !Number.isFinite(next)
  ) {
    el.textContent = value;
    return;
  }
  const start = performance.now();
  const frame = (now) => {
    const t = Math.min(1, (now - start) / 650);
    el.textContent =
      t === 1
        ? value
        : number(previous + (next - previous) * (1 - (1 - t) ** 3)) +
          (String(value).endsWith("%") ? "%" : "");
    if (t < 1) numberFrames.set(el, requestAnimationFrame(frame));
  };
  numberFrames.set(el, requestAnimationFrame(frame));
}
function sourceButton(kind, source, label = "Inspect") {
  return `<button class="action" data-action="inspect" data-kind="${kind}" data-source="${esc(source)}">${label}</button>`;
}
function getBuoy(id = S.selected.sensors) {
  return S.buoys.find((b) => b.id === id);
}
function getService(id = S.selected.services) {
  return S.services.find((s) => s.name === id);
}
function activeWindow(scope) {
  return (
    S.windows[scope] || {
      start: Date.now() - S.minutes[scope] * 60000,
      end: Date.now(),
    }
  );
}
const windowLoads = {};
function setWindow(scope, range) {
  S.windows[scope] = range;
  const pending = (windowLoads[scope] ||= {
    busy: false,
    timer: null,
    dirty: false,
  });
  pending.dirty = true;
  charts[scope].bounds = activeWindow(scope);
  cancelAnimationFrame(pending.frame);
  pending.frame = requestAnimationFrame(() => charts[scope].render());
  scheduleWindowLoad(scope);
}
function scheduleWindowLoad(scope) {
  const pending = windowLoads[scope];
  if (pending.busy || pending.timer || !pending.dirty) return;
  // Throttle, rather than debounce: sustained gestures receive data throughout.
  pending.timer = setTimeout(async () => {
    pending.timer = null;
    pending.busy = true;
    pending.dirty = false;
    try {
      await loadChart(scope);
    } catch (error) {
      toast(error.message);
    } finally {
      pending.busy = false;
      scheduleWindowLoad(scope);
    }
  }, 100);
}

class TelemetryChart {
  constructor(scope) {
    this.scope = scope;
    this.svg = $("chart-" + scope);
    this.tooltip = $("tooltip-" + scope);
    this.data = [];
    this.events = [];
    this.limits = [];
    this.bounds = { start: Date.now() - 3600000, end: Date.now() };
    this.normalized = false;
    this.interval = 60;
    this.drag = null;
    this.svg.setAttribute("role", "img");
    this.svg.setAttribute("aria-label", "Telemetry time series");
    this.svg.addEventListener("mousemove", (e) => this.hover(e));
    this.svg.addEventListener("mouseleave", () => {
      this.tooltip.style.opacity = 0;
      this.svg.querySelector(".chart-crosshair")?.setAttribute("opacity", "0");
    });
    this.svg.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        this.zoom(e.deltaY > 0 ? 1.5 : 1 / 1.5, this.fraction(e));
      },
      { passive: false },
    );
    this.svg.addEventListener("pointerdown", (e) => {
      if (e.button === 0) {
        this.drag = this.fraction(e);
        this.svg.setPointerCapture(e.pointerId);
      }
    });
    this.svg.addEventListener("pointerup", (e) => {
      if (this.drag === null) return;
      const a = this.drag,
        b = this.fraction(e);
      this.drag = null;
      if (Math.abs(a - b) > 0.025) {
        const span = this.bounds.end - this.bounds.start;
        setWindow(scope, {
          start: this.bounds.start + Math.min(a, b) * span,
          end: this.bounds.start + Math.max(a, b) * span,
        });
      }
    });
    this.svg.addEventListener("pointercancel", () => {
      this.drag = null;
    });
    new ResizeObserver(([entry]) => {
      const width = entry.contentRect.width;
      if (width > 0 && Math.abs(width - (this.width || 0)) > 1) {
        cancelAnimationFrame(this.resizeFrame);
        this.resizeFrame = requestAnimationFrame(() => this.render());
      }
    }).observe(this.svg);
  }
  fraction(e) {
    const r = this.svg.getBoundingClientRect();
    return Math.max(
      0,
      Math.min(
        1,
        (e.clientX - r.left - this.left) /
          (this.width - this.left - this.right),
      ),
    );
  }
  zoom(factor, anchor = 1) {
    const span = this.bounds.end - this.bounds.start,
      next = Math.max(60000, Math.min(31 * 86400000, span * factor)),
      t = this.bounds.start + span * anchor;
    setWindow(this.scope, {
      start: t - next * anchor,
      end: t + next * (1 - anchor),
    });
  }
  set(data, bounds, options = {}) {
    this.data = data.map((series) => ({
      ...series,
      points: series.points.map((p) => ({ ...p, time: +new Date(p.time) })),
    }));
    this.bounds = bounds;
    this.events = options.events || [];
    this.limits = options.limits || [];
    this.normalized = !!options.normalized;
    this.interval = options.interval || 0;
    this.render();
  }
  render() {
    if (!this.svg.getBoundingClientRect().width) return;
    this.width = Math.max(280, this.svg.getBoundingClientRect().width || 600);
    this.height = this.scope === "analysis" ? 300 : 230;
    this.left = 52;
    this.right = 14;
    this.top = 22;
    this.bottom = this.height - 32;
    const W = this.width,
      H = this.height,
      span = Math.max(1, this.bounds.end - this.bounds.start);
    this.x = (t) =>
      this.left +
      ((t - this.bounds.start) / span) * (W - this.left - this.right);
    const all = this.data.flatMap((s) =>
      s.points
        .filter(
          (p) =>
            p.value !== null &&
            Number.isFinite(p.value) &&
            new Date(p.time) >= this.bounds.start &&
            new Date(p.time) <= this.bounds.end,
        )
        .map((p) => p.value),
    );
    let lo = Math.min(...all, ...this.limits.map((l) => l.value)),
      hi = Math.max(...all, ...this.limits.map((l) => l.value));
    if (!Number.isFinite(lo)) {
      lo = 0;
      hi = 1;
    }
    if (lo === hi) {
      lo -= 1;
      hi += 1;
    }
    const pad = (hi - lo) * 0.12;
    lo -= pad;
    hi += pad;
    this.domains = this.data.map((s) => {
      const values = s.points
        .filter((p) => p.value !== null && Number.isFinite(p.value))
        .map((p) => p.value);
      let min = Math.min(...values),
        max = Math.max(...values);
      if (!Number.isFinite(min)) {
        min = 0;
        max = 1;
      }
      return { min, max };
    });
    this.y = (value, i) => {
      if (this.normalized) {
        const d = this.domains[i];
        value =
          d.max === d.min ? 50 : ((value - d.min) / (d.max - d.min)) * 100;
        return this.bottom - (value / 100) * (this.bottom - this.top);
      }
      return (
        this.bottom - ((value - lo) / (hi - lo)) * (this.bottom - this.top)
      );
    };
    let svg = `<defs><clipPath id="clip-${this.scope}"><rect x="${this.left}" y="${this.top}" width="${W - this.left - this.right}" height="${this.bottom - this.top}"/></clipPath></defs>`;
    for (let i = 0; i < 4; i++) {
      const y = this.top + ((this.bottom - this.top) * i) / 3,
        value = this.normalized
          ? 100 - (i * 100) / 3
          : hi - ((hi - lo) * i) / 3;
      svg += `<line class="svg-grid" x1="${this.left}" x2="${W - this.right}" y1="${y}" y2="${y}"/><text class="svg-label" x="${this.left - 7}" y="${y + 3}" text-anchor="end">${esc(Number(value.toPrecision(3)))}${this.normalized ? "%" : ""}</text>`;
    }
    for (let i = 0; i < 4; i++) {
      const t = this.bounds.start + (span * i) / 3;
      svg += `<text class="svg-label" x="${this.x(t)}" y="${H - 10}" text-anchor="${i === 0 ? "start" : i === 3 ? "end" : "middle"}">${esc(new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}</text>`;
    }
    svg += `<g clip-path="url(#clip-${this.scope})">`;
    for (const limit of this.limits) {
      const y = this.y(limit.value, 0);
      svg += `<line class="chart-limit" x1="${this.left}" x2="${W - this.right}" y1="${y}" y2="${y}"/><text class="svg-label" x="${W - this.right - 2}" y="${y - 3}" text-anchor="end">${esc(limit.label)} ${esc(number(limit.value))}</text>`;
    }
    for (const event of this.events) {
      const t = +new Date(event.time);
      if (t < this.bounds.start || t > this.bounds.end) continue;
      svg += `<line class="chart-event" x1="${this.x(t)}" x2="${this.x(t)}" y1="${this.top}" y2="${this.bottom}"><title>${esc(event.label)} · ${esc(timestamp(event.time))}</title></line><circle cx="${this.x(t)}" cy="${this.top + 4}" r="3" fill="var(--text-mid)"/>`;
    }
    this.data.forEach((series, index) => {
      let d = "",
        connected = false,
        previous = null,
        last = null,
        segmentLength = 0;
      const isolated = [];
      const finishSegment = () => {
        if (segmentLength === 1 && last) isolated.push(last);
        segmentLength = 0;
        connected = false;
      };
      const valid = series.points.filter((p) => Number.isFinite(p.value));
      const deltas = valid
        .slice(1)
        .map((p, i) => new Date(p.time) - new Date(valid[i].time))
        .filter((d) => d > 0)
        .sort((a, b) => a - b);
      // A requested bucket can be shorter than the source's actual cadence.
      const maxGap = Math.max(
        this.interval * 2500,
        deltas.length ? deltas[Math.floor(deltas.length / 2)] * 3 : Infinity,
      );
      // Nulls represent unaligned fields, not explicit outage markers.
      for (const [pointIndex, point] of valid.entries()) {
        const t = +new Date(point.time);
        if (t < this.bounds.start || t > this.bounds.end) continue;
        // Seed and live samples can have different cadences in the same window.
        // Use the surrounding intervals without allowing one long outage to
        // inflate its own threshold.
        const before =
          pointIndex > 1
            ? valid[pointIndex - 1].time - valid[pointIndex - 2].time
            : maxGap / 3;
        const after =
          pointIndex + 1 < valid.length
            ? valid[pointIndex + 1].time - t
            : maxGap / 3;
        const localGap = Math.max(maxGap, Math.min(before, after) * 3);
        if (previous !== null && t - previous > localGap) finishSegment();
        const x = this.x(t),
          y = this.y(point.value, index);
        d += `${connected ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)} `;
        connected = true;
        previous = t;
        last = { x, y };
        segmentLength++;
      }
      finishSegment();
      for (const point of isolated)
        svg += `<circle class="chart-sample" cx="${point.x}" cy="${point.y}" r="2.2" fill="${series.color || colors[index % colors.length]}"/>`;
      svg += `<path class="chart-trace" data-series="${esc(series.name)}" stroke="${series.color || colors[index % colors.length]}" d="${d}"/>`;
      if (last)
        svg += `<circle cx="${last.x}" cy="${last.y}" r="2.6" fill="${series.color || colors[index % colors.length]}"/>`;
    });
    svg += `</g><line class="chart-crosshair" opacity="0" stroke="var(--text-low)" stroke-dasharray="3 3" y1="${this.top}" y2="${this.bottom}" pointer-events="none"/>`;
    if (!all.length)
      svg += `<text class="chart-empty" x="${W / 2}" y="${H / 2}" text-anchor="middle">No recorded readings in this window</text>`;
    this.svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    this.svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    // Retain trace nodes: morph compatible paths, crossfade topology changes.
    const oldPaths = new Map(
      [...this.svg.querySelectorAll(".chart-trace")].map((p) => [
        p.dataset.series,
        p,
      ]),
    );
    for (const path of oldPaths.values()) cancelAnimationFrame(path._frame);
    this.svg.innerHTML = svg;
    for (const fresh of this.svg.querySelectorAll(".chart-trace")) {
      const old = oldPaths.get(fresh.dataset.series);
      const target = fresh.getAttribute("d");
      if (!old || reducedMotion.matches) {
        if (!reducedMotion.matches && target)
          fresh.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 400 });
        continue;
      }
      cancelAnimationFrame(old._frame);
      const from = old.getAttribute("d") || "";
      const topology = (d) => d.replace(/-?\d+(?:\.\d+)?/g, "#");
      old.setAttribute("stroke", fresh.getAttribute("stroke"));
      fresh.replaceWith(old);
      if (from === target) continue;
      if (from && topology(from) === topology(target)) {
        const a = from.match(/-?\d+(?:\.\d+)?/g).map(Number);
        const b = target.match(/-?\d+(?:\.\d+)?/g).map(Number);
        const start = performance.now();
        const frame = (now) => {
          const t = Math.min(1, (now - start) / 420),
            ease = 1 - (1 - t) ** 3;
          let i = 0;
          old.setAttribute(
            "d",
            t === 1
              ? target
              : target.replace(/-?\d+(?:\.\d+)?/g, () => {
                  const n = a[i] + (b[i] - a[i]) * ease;
                  i++;
                  return n.toFixed(2);
                }),
          );
          if (t < 1) old._frame = requestAnimationFrame(frame);
        };
        old._frame = requestAnimationFrame(frame);
      } else {
        const ghost = old.cloneNode();
        ghost.removeAttribute("data-series");
        ghost.classList.remove("chart-trace");
        ghost.style.fill = "none";
        ghost.style.strokeWidth = "1.8";
        old.before(ghost);
        const fade = ghost.animate([{ opacity: 1 }, { opacity: 0 }], {
          duration: 250,
        });
        fade.onfinish = () => ghost.remove();
        old.setAttribute("d", target);
        old.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 300 });
      }
    }
  }
  hover(e) {
    if (!this.data.length) return;
    const target =
      this.bounds.start +
      (this.bounds.end - this.bounds.start) * this.fraction(e);
    const candidates = this.data
      .map((series) => {
        const points = series.points;
        let lo = 0,
          hi = points.length;
        while (lo < hi) {
          const mid = (lo + hi) >>> 1;
          if (points[mid].time < target) lo = mid + 1;
          else hi = mid;
        }
        let left = lo - 1,
          right = lo;
        while (left >= 0 && !Number.isFinite(points[left].value)) left--;
        while (right < points.length && !Number.isFinite(points[right].value))
          right++;
        return [points[left], points[right]].filter(Boolean);
      })
      .flat();
    if (!candidates.length) return;
    const nearest = candidates.reduce((a, b) =>
      Math.abs(a.time - target) < Math.abs(b.time - target) ? a : b,
    );
    const t = +new Date(nearest.time);
    const rows = this.data
      .map((s) => {
        const point = s.points.find(
          (p) => +new Date(p.time) === t && p.value !== null,
        );
        return `<div>${esc(s.name)}: ${point ? esc(number(point.value) + " " + s.unit) : "—"}</div>`;
      })
      .join("");
    const nearby = this.events
      .filter(
        (event) =>
          Math.abs(new Date(event.time) - target) <
          (this.bounds.end - this.bounds.start) * 0.015,
      )
      .map((event) => `<div>${esc(event.label)}</div>`)
      .join("");
    const crosshair = this.svg.querySelector(".chart-crosshair");
    crosshair?.setAttribute("x1", this.x(t));
    crosshair?.setAttribute("x2", this.x(t));
    crosshair?.setAttribute("opacity", "0.7");
    this.tooltip.innerHTML = `<div class="t-time">${esc(timestamp(nearest.time))}</div>${rows}${nearby}`;
    this.tooltip.style.opacity = 1;
    this.tooltip.style.top = "26px";
    this.tooltip.style.left =
      Math.max(
        5,
        Math.min(this.x(t), this.width - this.tooltip.offsetWidth - 10),
      ) + "px";
  }
}
const charts = {
  services: new TelemetryChart("services"),
  sensors: new TelemetryChart("sensors"),
  analysis: new TelemetryChart("analysis"),
};

const historyCache = new Map();
async function fetchHistory(kind, source, metrics, range) {
  const query = new URLSearchParams({
    metrics: metrics.join(","),
    start: new Date(range.start).toISOString(),
    end: new Date(range.end).toISOString(),
    aggregation: "mean",
    interval_seconds: String(
      Math.max(1, Math.ceil((range.end - range.start) / 240000)),
    ),
  });
  const key = `/api/${kind === "service" ? "services" : "buoys"}/${enc(source)}/timeseries?${query}`;
  const cached = historyCache.get(key);
  if (cached && (cached.pending || Date.now() - cached.time < 4000))
    return cached.promise;
  const entry = { time: Date.now(), pending: true };
  entry.promise = api(key).then(
    (result) => {
      entry.pending = false;
      entry.time = Date.now();
      return result;
    },
    (error) => {
      historyCache.delete(key);
      throw error;
    },
  );
  historyCache.set(key, entry);
  if (historyCache.size > 48)
    historyCache.delete(historyCache.keys().next().value);
  return entry.promise;
}
async function maintenanceFor(id, force = false) {
  if (force || !S.maintenance[id])
    S.maintenance[id] = await api(`/api/buoys/${enc(id)}/maintenance`);
  return S.maintenance[id];
}
function readingSeries(result, metric, name, unit, color) {
  return {
    name,
    metric,
    unit,
    color,
    points: result.points.map((p) => ({
      time: p.time,
      value: p.values[metric],
    })),
  };
}
function drawSpark(id, points) {
  const el = $(id),
    valid = points.filter((p) => p.value !== null && Number.isFinite(p.value));
  if (!el) return;
  if (!valid.length) {
    el.setAttribute("d", "");
    return;
  }
  const lo = Math.min(...valid.map((p) => p.value)),
    hi = Math.max(...valid.map((p) => p.value)),
    first = +new Date(valid[0].time),
    last = +new Date(valid.at(-1).time);
  el.setAttribute(
    "d",
    valid
      .map(
        (p, i) =>
          `${i ? "L" : "M"}${(((new Date(p.time) - first) / Math.max(1, last - first)) * 260).toFixed(2)},${(32 - ((p.value - lo) / Math.max(1e-9, hi - lo)) * 28).toFixed(2)}`,
      )
      .join(" "),
  );
}
function sensorLimits(sensor) {
  if (!sensor?.rules?.enabled) return [];
  return ["warn_min", "warn_max", "crit_min", "crit_max"]
    .filter((k) => sensor.rules[k] !== null)
    .map((k) => ({ value: sensor.rules[k], label: k.replace("_", " ") }));
}
async function loadChart(scope) {
  const seq = ++S.historySeq[scope],
    range = activeWindow(scope);
  const source = S.selected[scope];
  if (!source) {
    charts[scope].set([], range);
    return;
  }
  let series = [],
    events = [],
    limits = [],
    interval = 0;
  if (scope === "analysis") {
    const buoy = getBuoy(source);
    if (!buoy) return;
    const metrics = [...S.analysisMetrics].filter(
      (m) => m in buoy.sensors || m in buoy.field_metadata,
    );
    if (!metrics.length) {
      charts[scope].set([], range);
      setText("analysis-summary", "Select one or more metrics above.");
      return;
    }
    const [result, maintenance] = await Promise.all([
      fetchHistory("buoy", source, metrics, range),
      maintenanceFor(source),
    ]);
    interval = result.interval_seconds;
    series = metrics.map((m, i) =>
      readingSeries(
        result,
        m,
        pretty(m),
        buoy.sensors[m]?.unit || buoy.field_metadata[m]?.unit || "",
        colors[i % colors.length],
      ),
    );
    events = maintenance.map((m) => ({
      time: m.time,
      label: pretty(m.kind) + ": " + m.description,
    }));
    if (seq !== S.historySeq[scope]) return;
    charts[scope].set(series, activeWindow(scope), {
      events,
      interval,
      normalized: $("analysis-scale").value === "normalized",
    });
    $("analysis-summary").innerHTML = series
      .map((s) => {
        const values = s.points
          .filter((p) => p.value !== null)
          .map((p) => p.value);
        return `<div class="series-summary" style="border-color:${s.color}"><strong>${esc(s.name)}</strong><br>${values.length ? `${esc(number(Math.min(...values)))} → ${esc(number(Math.max(...values)))} ${esc(s.unit)}` : "No samples"}<br>${values.length} recorded buckets</div>`;
      })
      .join("");
    $("analysis-events").innerHTML = maintenanceHTML(
      maintenance.filter(
        (m) => new Date(m.time) >= range.start && new Date(m.time) <= range.end,
      ),
    );
    return;
  }
  const kind = scope === "services" ? "service" : "buoy",
    metric = scope === "services" ? "latency_ms" : S.metric;
  const main = scope === "services" ? getService(source) : getBuoy(source);
  const selectedUnits =
    scope === "services"
      ? "ms"
      : main?.sensors[metric]?.unit || main?.field_metadata[metric]?.unit || "";
  const ids = [source, ...S.compare[scope]].filter(
    (id, i, arr) => arr.indexOf(id) === i,
  );
  const maintenanceRequest =
    scope === "sensors" ? maintenanceFor(source) : Promise.resolve([]);
  const [maintenance, results] = await Promise.all([
    maintenanceRequest,
    Promise.all(
      ids.map(async (id, index) => {
        const b = scope === "sensors" ? getBuoy(id) : null;
        if (
          scope === "sensors" &&
          (!b || (!(metric in b.sensors) && !(metric in b.field_metadata)))
        )
          return null;
        const unit =
          scope === "services"
            ? "ms"
            : b.sensors[metric]?.unit || b.field_metadata[metric]?.unit || "";
        if (unit !== selectedUnits) return null;
        const metrics =
          index === 0
            ? [...new Set([metric, scope === "services" ? "rps" : "battery"])]
            : [metric];
        const result = await fetchHistory(kind, id, metrics, range);
        interval = result.interval_seconds;
        return {
          ...readingSeries(result, metric, id, unit, colors[index]),
          related: result,
        };
      }),
    ),
  ]);
  series = results.filter(Boolean);
  if (scope === "sensors") {
    events = maintenance
      .filter((m) => !m.sensor || m.sensor === metric)
      .map((m) => ({
        time: m.time,
        label: pretty(m.kind) + ": " + m.description,
      }));
    limits = sensorLimits(main?.sensors[metric]);
  } else if (main) {
    limits = [
      { value: main.warn_ms, label: "warning" },
      { value: main.crit_ms, label: "critical" },
    ];
  }
  events.push(
    ...S.alerts
      .filter((a) => a.source_type === kind && a.source === source)
      .map((a) => ({ time: a.time, label: a.message })),
  );
  if (seq !== S.historySeq[scope]) return;
  charts[scope].set(series, activeWindow(scope), { events, limits, interval });
  const sparkMetric = scope === "services" ? "rps" : "battery";
  const sparkPoints =
    series[0]?.related?.points.map((p) => ({
      time: p.time,
      value: p.values[sparkMetric],
    })) || [];
  drawSpark(
    scope === "services" ? "spark-path-services" : "spark-path-batt",
    sparkPoints,
  );
  if (scope === "sensors" && S.drawer === source)
    drawSpark("dw-batt-spark", sparkPoints);
  const last = series[0]?.points.filter((p) => p.value !== null).at(-1);
  const live = !S.windows[scope];
  setText(
    "chart-sub-" + scope,
    `${selectedUnits || "Value"} · ${new Date(range.start).toLocaleString()} – ${new Date(range.end).toLocaleTimeString()} · ${last ? "latest bucket " + shortTime(last.time) : "no samples"}`,
  );
  setText(
    "scrub-status-" + scope,
    live ? "● LIVE" : "REPLAY · " + shortTime(range.end),
  );
  $("scrub-status-" + scope).classList.toggle("live", live);
  $("replay-banner-" + scope).classList.toggle("show", !live);
  $("compare-legend-" + scope).classList.toggle(
    "show",
    S.comparing[scope] || S.compare[scope].size > 0,
  );
  $("compare-legend-" + scope).innerHTML =
    series
      .map(
        (s) =>
          `<span class="chart-legend-item" style="color:${s.color}">${esc(s.name)}${s.name !== source ? `<button class="action" data-action="uncompare" data-scope="${scope}" data-source="${esc(s.name)}" aria-label="Remove ${esc(s.name)}">×</button>` : ""}</span>`,
      )
      .join("") +
    (S.comparing[scope]
      ? '<span class="chart-legend-item muted">Select up to 3 other sources in the sidebar.</span>'
      : "");
}

function setTheme(theme) {
  if (!["amber", "dark", "light"].includes(theme)) theme = "amber";
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("telem-theme", theme);
  document.querySelectorAll("[data-theme]").forEach((b) => {
    if (b.tagName === "BUTTON")
      b.classList.toggle("active", b.dataset.theme === theme);
  });
  if (tiles) tiles.setUrl(tileURL());
}
function switchTab(tab, updateHash = true, load = true) {
  if (
    !["overview", "services", "sensors", "incidents", "analysis"].includes(tab)
  )
    return;
  const changed = S.tab !== tab;
  S.tab = tab;
  document
    .querySelectorAll(".view")
    .forEach((v) => v.classList.toggle("active", v.id === "view-" + tab));
  document
    .querySelectorAll("[data-view]")
    .forEach((b) => b.classList.toggle("active", b.dataset.view === tab));
  if (tab === "sensors") {
    requestAnimationFrame(() => {
      initMap();
      map?.invalidateSize();
    });
  }
  if (changed && !reducedMotion.matches) {
    document
      .querySelectorAll(`#view-${tab} .panel, #view-${tab} .svc-item`)
      .forEach((el, i) => {
        el.animate(
          [
            { opacity: 0, transform: "translateY(10px)" },
            { opacity: 1, transform: "translateY(0)" },
          ],
          {
            duration: 350,
            delay: Math.min(i, 6) * 30,
            easing: "cubic-bezier(.2,.8,.2,1)",
            fill: "backwards",
          },
        );
      });
  }
  if (load && charts[tab]) loadChart(tab).catch((e) => toast(e.message));
  if (updateHash) writeHash();
}
function writeHash() {
  const source = S.selected[S.tab];
  const value = "#" + S.tab + (source ? "/" + enc(source) : "");
  historyReplace(value);
}
function historyReplace(hash) {
  window.history.replaceState(null, "", hash);
}
function applyHash() {
  const [tab, raw] = location.hash.slice(1).split("/");
  let id;
  try {
    id = raw ? decodeURIComponent(raw) : null;
  } catch {
    return;
  }
  if (
    tab &&
    ["overview", "services", "sensors", "incidents", "analysis"].includes(tab)
  ) {
    if (
      id &&
      ((tab === "services" && getService(id)) ||
        (["sensors", "analysis"].includes(tab) && getBuoy(id)))
    )
      S.selected[tab] = id;
    switchTab(tab, false);
  }
}
async function selectSource(kind, id, open = true) {
  const scope = kind === "service" ? "services" : "sensors";
  if (!(scope === "services" ? getService(id) : getBuoy(id))) {
    toast("This source has been removed. Its incident history is retained.");
    return;
  }
  if (S.comparing[scope] && id !== S.selected[scope]) {
    if (scope === "sensors") {
      const main = getBuoy(),
        other = getBuoy(id),
        a = main.sensors[S.metric] || main.field_metadata[S.metric],
        b = other.sensors[S.metric] || other.field_metadata[S.metric];
      if (!b || a?.unit !== b.unit) {
        toast("Comparison requires the same metric and units on both buoys.");
        return;
      }
    }
    if (S.compare[scope].has(id)) S.compare[scope].delete(id);
    else if (S.compare[scope].size < 3) S.compare[scope].add(id);
    else {
      toast("Compare up to four sources including the selected source.");
      return;
    }
    renderSources();
    await loadChart(scope);
    return;
  }
  S.selected[scope] = id;
  S.windows[scope] = null;
  $("scrub-range-" + scope).value = "1000";
  if (scope === "sensors") {
    const b = getBuoy(id);
    if (!(S.metric in b.sensors) && !(S.metric in b.field_metadata))
      S.metric = Object.keys(b.sensors)[0] || "battery";
  }
  switchTab(scope, true, false);
  renderSources();
  renderReadouts();
  writeHash();
  await Promise.all([
    loadChart(scope),
    scope === "sensors" && open ? openDrawer(id) : Promise.resolve(),
  ]);
}
function filtered(scope) {
  const term = $("filter-" + scope).value.toLowerCase(),
    filter = $("status-" + scope).value;
  const items = (scope === "services" ? S.services : S.buoys).filter((item) => {
    const name = scope === "services" ? item.name : item.id;
    if (!name.toLowerCase().includes(term)) return false;
    if (filter === "all") return true;
    if (filter === "rising") return S.overview?.rising_services.includes(name);
    if (filter === "offline") return item.offline;
    if (filter === "battery") return item.battery <= 15;
    if (filter === "stale")
      return scope === "services" ? item.stale : item.stale_sensor_count > 0;
    return item.status === filter;
  });
  const { key, dir } = S.sort[scope];
  return [...items].sort((a, b) =>
    typeof a[key] === "number"
      ? (a[key] - b[key]) * dir
      : String(a[key]).localeCompare(String(b[key])) * dir,
  );
}
function renderSources() {
  const services = filtered("services"),
    buoys = filtered("sensors");
  $("sidebar-services-list").innerHTML =
    services
      .map(
        (s) =>
          `<div class="svc-item ${s.name === S.selected.services ? "active" : ""}" tabindex="0" role="button" data-select="service" data-source="${esc(s.name)}"><span class="dot ${dot(s.status)}"></span><span class="svc-name">${esc(s.name)}</span><span class="svc-rps">${S.compare.services.has(s.name) ? "✓" : number(s.rps)}</span></div>`,
      )
      .join("") || '<div class="empty">No matching services.</div>';
  $("sidebar-sensors-list").innerHTML =
    buoys
      .map(
        (b) =>
          `<div class="svc-item ${b.id === S.selected.sensors ? "active" : ""}" tabindex="0" role="button" data-select="buoy" data-source="${esc(b.id)}"><span class="dot ${dot(b.status)}"></span><span class="svc-name">${esc(b.id)}</span><span class="svc-rps">${S.compare.sensors.has(b.id) ? "✓" : Math.round(b.battery) + "%"}</span></div>`,
      )
      .join("") || '<div class="empty">No matching buoys.</div>';
  setText("services-count", `SERVICES · ${S.services.length}`);
  setText("buoys-count", `BUOYS · ${S.buoys.length}`);
  $("services-tbody").innerHTML = services
    .map(
      (s) =>
        `<tr><td>${esc(s.name)}</td><td><span class="dot ${dot(s.status)}"></span> ${esc(s.status)} ${badges(s)}</td><td>${number(s.rps)}</td><td>${number(s.latency_ms)}ms</td><td>${number(s.error_rate)}%</td>${`<td>${sourceButton("service", s.name, "View")}</td>`}</tr>`,
    )
    .join("");
  $("buoys-tbody").innerHTML = buoys
    .map(
      (b) =>
        `<tr><td>${esc(b.id)}</td><td><span class="dot ${dot(b.status)}"></span> ${esc(b.status_text)}</td><td>${number(b.battery)}%</td><td>${ago(b.last_contact_at)} ${b.contact_simulated ? '<span class="badge">SIM</span>' : ""}</td><td>${esc(b.gnss_fix)}</td><td>${number(b.signal_dbm)}dBm</td><td>${sourceButton("buoy", b.id, "View")}</td></tr>`,
    )
    .join("");
  setText(
    "services-table-summary",
    `${services.length} shown · ${S.services.length} total`,
  );
  setText(
    "buoys-table-summary",
    `${buoys.length} shown · ${S.buoys.length} total`,
  );
}
function readingsHTML(sensors) {
  return (
    Object.entries(sensors)
      .map(
        ([name, s]) =>
          `<div class="readout ${s.stale ? "stale" : ""}"><div class="readout-label">${esc(pretty(name))}</div><div class="readout-value">${esc(number(s.value))} <small>${esc(s.unit)}</small></div><span class="readout-meta">${badges(s)} · ${esc(ago(s.last_reading_at))}</span></div>`,
      )
      .join("") ||
    '<div class="empty">No probes registered. Power and position remain available.</div>'
  );
}
function drift(b) {
  const north = (b.lat - b.mooring_lat) * 111320,
    east =
      (b.lng - b.mooring_lng) *
      111320 *
      Math.cos((b.mooring_lat * Math.PI) / 180),
    meters = Math.hypot(north, east),
    bearing = ((Math.atan2(east, north) * 180) / Math.PI + 360) % 360;
  return {
    north,
    east,
    meters,
    bearing,
    text: `${number(meters)} m · ${Math.round(bearing)}°`,
  };
}
function coords(b) {
  return `${Math.abs(b.lat).toFixed(5)}° ${b.lat < 0 ? "S" : "N"}, ${Math.abs(b.lng).toFixed(5)}° ${b.lng < 0 ? "W" : "E"}`;
}
function renderReadouts() {
  const svc = getService(),
    b = getBuoy();
  if (svc) {
    $("chart-panel-services").querySelector(".panel-title").textContent =
      "Request latency · " + svc.name;
    setText("rps-num", number(svc.rps));
    document.querySelector("#view-services .stat-delta").textContent =
      "req/s · " +
      (svc.simulated ? "simulated" : "reported") +
      " · " +
      ago(svc.last_reading_at);
  } else {
    $("chart-panel-services").querySelector(".panel-title").textContent =
      "Request latency";
    setText("rps-num", "—");
  }
  if (b) {
    const metrics = [
      ...new Set([...Object.keys(b.sensors), ...Object.keys(b.field_metadata)]),
    ];
    if (!metrics.includes(S.metric) && S.metric !== "battery")
      S.metric = metrics[0] || "battery";
    $("metric-select").innerHTML = (metrics.length ? metrics : ["battery"])
      .map(
        (m) =>
          `<button class="range-btn ${S.metric === m ? "active" : ""}" data-metric="${esc(m)}">${esc(pretty(m))}</button>`,
      )
      .join("");
    $("chart-panel-sensors").querySelector(".panel-title").innerHTML =
      `${esc(pretty(S.metric))} · <button class="source-link" data-action="inspect" data-kind="buoy" data-source="${esc(b.id)}">${esc(b.id)} ↗</button>`;
    $("current-readings-grid").innerHTML = readingsHTML(b.sensors);
    setText("batt-num", number(b.battery) + "%");
    setText(
      "batt-status",
      `Solar ${number(b.solar_watts)} W · ${b.field_metadata.solar_watts.simulated ? "simulated" : "reported"}`,
    );
    const panel = $("view-sensors").querySelector(".gnss-coords");
    panel.textContent = coords(b);
    const rows = panel.parentElement.querySelectorAll(
      ".gnss-row span:last-child",
    );
    rows[0].textContent = b.gnss_fix;
    rows[1].textContent = drift(b).text;
    $("bearing-pointer").setAttribute(
      "transform",
      `rotate(${drift(b).bearing} 30 30)`,
    );
    $("current-readings-grid")
      .closest(".panel")
      .querySelector(".panel-title").textContent = "Current readings · " + b.id;
  } else {
    $("metric-select").innerHTML = "";
    $("current-readings-grid").innerHTML =
      '<div class="empty">Register a buoy to get started.</div>';
    setText("batt-num", "—");
  }
  renderDrift();
  updateMap();
  if (S.drawer) renderDrawer();
}
function renderOverview() {
  if (!S.overview) return;
  const c = S.overview.counts;
  const cards = [
    [
      "offline_buoys",
      "Offline buoys",
      "Beyond the contact deadline",
      "offline",
    ],
    ["low_batteries", "Low batteries", "15% or less remaining", "battery"],
    ["stale_sensors", "Stale probes", "Past their reading deadline", "stale"],
    [
      "active_incidents",
      "Active incidents",
      "Open operator workflows",
      "incidents",
    ],
    [
      "rising_services",
      "Rising latency",
      "Latest change >5ms and >5%",
      "rising",
    ],
  ];
  $("overview-cards").innerHTML = cards
    .map(
      ([key, label, help, filter]) =>
        `<button class="overview-card" data-action="overview-filter" data-filter="${filter}"><span class="label">${label}</span><strong>${c[key]}</strong><small>${help}</small></button>`,
    )
    .join("");
  const filtered = S.overview.urgent_sources.filter(
    (s) =>
      !S.overviewFilter ||
      (S.overviewFilter === "offline" && s.offline) ||
      (S.overviewFilter === "battery" && s.low_battery) ||
      (S.overviewFilter === "stale" && s.stale_sensors) ||
      (S.overviewFilter === "rising" && s.rising),
  );
  setText(
    "overview-summary",
    `${c.buoys} buoys · ${c.services} services · ${filtered.length} sources need attention`,
  );
  $("urgent-sources").innerHTML =
    filtered
      .map(
        (s) =>
          `<div class="source-item"><div><div class="source-name"><span class="dot ${dot(s.severity)}"></span>${esc(s.source)}</div><div class="source-reason">${esc(s.reason)}</div></div>${sourceButton(s.source_type, s.source)}</div>`,
      )
      .join("") ||
    '<div class="empty">No sources need attention in this view.</div>';
  $("overview-activity").innerHTML =
    S.alerts
      .slice(0, 6)
      .map(
        (a) =>
          `<div class="source-item"><div><div class="source-name">${esc(a.source)}</div><div class="source-reason">${esc(a.message)}</div><small class="muted">${esc(ago(a.time))}</small></div><button class="action" data-action="jump" data-kind="${a.source_type}" data-source="${esc(a.source)}" data-time="${esc(a.time)}">Trace</button></div>`,
      )
      .join("") ||
    '<div class="empty">No alerts yet. Activity will appear as conditions change.</div>';
}
function renderFeeds() {
  for (const scope of ["services", "sensors"]) {
    const kind = scope === "services" ? "service" : "buoy";
    const mutes = S.mutes.filter((m) => m.source_type === kind);
    $("muted-summary-" + scope).innerHTML = mutes
      .map(
        (m) =>
          `<div class="source-item"><small>${esc(m.source)} muted until ${esc(shortTime(m.expires_at))}</small><button class="action" data-action="unmute" data-id="${esc(m.id)}">Unmute</button></div>`,
      )
      .join("");
    $("feed-" + scope + "-list").innerHTML =
      S.alerts
        .filter((a) => a.source_type === kind && !a.muted)
        .slice(0, 20)
        .map(
          (a) =>
            `<div class="feed-item ${a.acknowledged_at ? "acked" : ""}" data-action="jump" data-kind="${kind}" data-source="${esc(a.source)}" data-time="${esc(a.time)}"><div class="feed-top"><span class="sev ${dot(a.severity)}"></span><span class="feed-sev-text">${esc(a.severity.toUpperCase())}</span><span class="feed-time">${esc(shortTime(a.time))}</span><button class="ack-btn" data-action="ack-alert" data-id="${a.id}" data-acked="${!!a.acknowledged_at}">${a.acknowledged_at ? "Acked" : "Ack"}</button><button class="mute-btn" data-action="mute" data-kind="${kind}" data-source="${esc(a.source)}">Mute</button></div><div class="feed-msg">${esc(a.message)}</div></div>`,
        )
        .join("") || '<div class="empty">No unmuted events.</div>';
  }
}
function renderIncidents() {
  const list = S.incidents.filter(
    (i) => S.incidentFilter === "all" || i.status === S.incidentFilter,
  );
  setText(
    "incident-summary",
    `${S.incidents.length} incidents · ${S.incidents.filter((i) => i.status === "ongoing").length} ongoing`,
  );
  $("incident-cards").innerHTML =
    list
      .map(
        (i) =>
          `<div class="panel incident-card ${S.expanded.has(i.id) ? "expanded" : ""}" data-id="${i.id}"><div class="incident-head" role="button" tabindex="0" data-action="expand-incident" data-id="${i.id}" aria-expanded="${S.expanded.has(i.id)}"><span class="incident-id mono">${esc(i.id)}</span><span class="incident-title">${esc(i.title)}</span><span class="badge ${i.severity}">${esc(i.severity)}</span><span class="incident-status-tag">${i.status.toUpperCase()}</span><span class="incident-chevron">⌄</span></div><div class="incident-body"><div class="incident-tools"><button class="action" data-action="ack-incident" data-id="${i.id}">${i.acknowledged_at ? "Acknowledged" : "Acknowledge"}</button><button class="action" data-action="edit-incident" data-id="${i.id}">${i.owner ? "Owner: " + esc(i.owner) : "Assign owner / cause"}</button><button class="action" data-action="note" data-id="${i.id}">+ Note</button><button class="action" data-action="resolve" data-id="${i.id}">${i.status === "ongoing" ? "Resolve" : "Reopen"}</button><button class="action" data-action="jump" data-kind="${i.source_type}" data-source="${esc(i.source)}" data-time="${esc(i.started_at)}">Trace on chart</button></div><div class="incident-notes"><div class="muted">Started ${esc(timestamp(i.started_at))}${i.resolved_at ? " · Resolved " + esc(timestamp(i.resolved_at)) : ""}</div><p>${esc(i.cause)}</p>${i.events.map((e) => `<button class="event-jump" data-action="jump" data-kind="${e.source_type}" data-source="${esc(e.source)}" data-time="${esc(e.time)}"><span class="mono">${esc(shortTime(e.time))}</span> · ${esc(e.message)}</button>`).join("")}${i.notes.map((n) => `<div class="note"><small>${esc(n.author)} · ${esc(timestamp(n.time))}</small>${esc(n.text)}</div>`).join("")}</div></div></div>`,
      )
      .join("") ||
    '<div class="panel empty">No incidents match this filter.</div>';
}
function maintenanceHTML(items) {
  return (
    items
      .map(
        (m) =>
          `<div class="maintenance-item"><strong>${esc(pretty(m.kind))}${m.sensor ? " · " + esc(m.sensor) : ""}</strong><p>${esc(m.description)}</p><small>${esc(timestamp(m.time))} · ${esc(m.operator)}</small></div>`,
      )
      .join("") ||
    '<div class="empty">No maintenance recorded in this view.</div>'
  );
}
async function openDrawer(id) {
  S.drawer = id;
  await maintenanceFor(id, true);
  renderDrawer();
  $("drawer-backdrop").classList.add("open");
  $("buoy-drawer").inert = false;
  $("buoy-drawer").classList.add("open");
  $("drawer-close").focus();
}
function closeDrawer() {
  S.drawer = null;
  $("drawer-backdrop").classList.remove("open");
  $("buoy-drawer").classList.remove("open");
  $("buoy-drawer").inert = true;
}
function renderDrawer() {
  const b = getBuoy(S.drawer);
  if (!b) {
    closeDrawer();
    return;
  }
  setText("drawer-name", b.id);
  setText("drawer-status", b.status_text);
  $("drawer-status").className = "drawer-status " + dot(b.status);
  $("dw-readings").innerHTML = readingsHTML(b.sensors);
  if (S.selected.sensors === b.id)
    $("dw-batt-spark").setAttribute(
      "d",
      $("spark-path-batt").getAttribute("d") || "",
    );
  setText("dw-batt", number(b.battery) + "%");
  setText("dw-solar", number(b.solar_watts) + " W");
  setText("dw-coords", coords(b));
  setText("dw-fix", b.gnss_fix);
  setText("dw-drift", drift(b).text);
  $("dw-bearing-pointer").setAttribute(
    "transform",
    `rotate(${drift(b).bearing} 30 30)`,
  );
  setText(
    "dw-contact",
    ago(b.last_contact_at) + (b.contact_simulated ? " · simulated" : ""),
  );
  setText("dw-signal", number(b.signal_dbm) + " dBm");
  $("dw-events").innerHTML =
    S.alerts
      .filter((a) => a.source_type === "buoy" && a.source === b.id)
      .slice(0, 5)
      .map(
        (a) =>
          `<div class="drawer-event">${esc(shortTime(a.time))} · ${esc(a.message)}</div>`,
      )
      .join("") || "No recent events.";
  $("drawer-rules").innerHTML =
    Object.entries(b.sensors)
      .map(
        ([name, s]) =>
          `<div class="rule-row"><div><strong>${esc(pretty(name))}</strong><small>${badges(s)} · ${esc(ago(s.last_reading_at))}<br>${s.rules.enabled ? "Freshness deadline: " + s.rules.stale_after_seconds + "s" : "Alerts disabled"}<br>${esc(
            sensorLimits(s)
              .map((l) => l.label + " " + number(l.value))
              .join(" · ") || "No value limits set",
          )}</small></div><button class="action" data-action="rules" data-sensor="${esc(name)}">Edit limits</button></div>`,
      )
      .join("") || '<div class="empty">Add a sensor to configure limits.</div>';
  $("drawer-maintenance").innerHTML = maintenanceHTML(
    S.maintenance[b.id] || [],
  );
}
function renderAnalysisControls() {
  const select = $("analysis-buoy");
  select.innerHTML = S.buoys
    .map((b) => `<option value="${esc(b.id)}">${esc(b.id)}</option>`)
    .join("");
  select.value = S.selected.analysis || "";
  const b = getBuoy(S.selected.analysis);
  const fields = b
    ? [...Object.keys(b.sensors), ...Object.keys(b.field_metadata)]
    : [];
  $("analysis-metrics").innerHTML = fields
    .map(
      (m) =>
        `<label><input type="checkbox" value="${esc(m)}" ${S.analysisMetrics.has(m) ? "checked" : ""}>${esc(pretty(m))}</label>`,
    )
    .join("");
}
function tileURL() {
  const light = document.documentElement.dataset.theme === "light";
  return `https://{s}.basemaps.cartocdn.com/${light ? "light_all" : "dark_all"}/{z}/{x}/{y}{r}.png`;
}
function initMap() {
  if (map) return;
  if (!window.L) {
    $("leaflet-map").innerHTML =
      '<div class="empty">The map library could not load. Coordinates and the mooring plot are still available.</div>';
    return;
  }
  map = L.map("leaflet-map", { scrollWheelZoom: false }).setView(
    [S.buoys[0]?.lat || 0, S.buoys[0]?.lng || 0],
    12,
  );
  tiles = L.tileLayer(tileURL(), {
    attribution: "© OpenStreetMap © CARTO",
    subdomains: "abcd",
    maxZoom: 18,
  }).addTo(map);
  updateMap();
  if (S.buoys.length > 1)
    map.fitBounds(
      S.buoys.map((b) => [b.lat, b.lng]),
      { padding: [30, 30], maxZoom: 14 },
    );
}
function updateMap() {
  if (!map) return;
  for (const id of Object.keys(markers)) {
    if (!getBuoy(id)) {
      map.removeLayer(markers[id]);
      delete markers[id];
    }
  }
  for (const b of S.buoys) {
    const icon = L.divIcon({
      className: "",
      html: `<div class="buoy-marker ${dot(b.status)}"></div>`,
      iconSize: [12, 12],
      iconAnchor: [6, 6],
    });
    if (!markers[b.id])
      markers[b.id] = L.marker([b.lat, b.lng], { icon })
        .addTo(map)
        .on("click", () =>
          selectSource("buoy", b.id).catch((e) => toast(e.message)),
        );
    markers[b.id]
      .setLatLng([b.lat, b.lng])
      .setIcon(icon)
      .bindTooltip(`${esc(b.id)} · ${esc(b.status_text)}`);
  }
}
function renderDrift() {
  const svg = document.querySelector(".posmap");
  const all = S.buoys.map((b) => ({ b, ...drift(b) })),
    max = Math.max(
      10,
      ...all.map((d) => Math.max(Math.abs(d.east), Math.abs(d.north))),
    );
  svg.innerHTML =
    `<line class="pos-grid" x1="300" y1="15" x2="300" y2="195"/><line class="pos-grid" x1="30" y1="105" x2="570" y2="105"/><text class="pos-label" x="305" y="18">N · ${esc(number(max))}m</text><text class="pos-label" x="305" y="120">each buoy’s mooring</text>` +
    all
      .filter((d) => d.meters > 0.1)
      .map(({ b, east, north }) => {
        const x = 300 + (east / max) * 230,
          y = 105 - (north / max) * 80;
        return `<line class="pos-link" x1="300" y1="105" x2="${x}" y2="${y}"/><g class="svg-marker" data-select="buoy" data-source="${esc(b.id)}"><circle class="pos-node ${dot(b.status)}" cx="${x}" cy="${y}" r="5"/><text class="pos-label" x="${x + 8}" y="${y - 5}">${esc(b.id)}</text><title>${esc(b.id)}: ${esc(number(Math.hypot(east, north)))} m</title></g>`;
      })
      .join("");
  let legend = document.getElementById("drift-legend");
  if (!legend) {
    legend = document.createElement("div");
    legend.id = "drift-legend";
    legend.className = "drift-legend";
    svg.after(legend);
  }
  const atMooring = all.filter((d) => d.meters <= 0.1).length;
  if (atMooring)
    svg.innerHTML += `<circle class="pos-anchor" cx="300" cy="105" r="5"/><text class="pos-label" x="308" y="98">${atMooring} at mooring</text>`;
  legend.innerHTML = all
    .map(
      (d) =>
        `<button class="action" data-select="buoy" data-source="${esc(d.b.id)}">${esc(d.b.id)} · ${esc(number(d.meters))} m</button>`,
    )
    .join("");
}

function field(
  label,
  name,
  value = "",
  type = "text",
  extra = "",
  wide = false,
) {
  return `<label class="${wide ? "full" : ""}">${esc(label)}<input name="${name}" type="${type}" value="${esc(value)}" ${extra}></label>`;
}
function textfield(label, name, value = "", wide = true) {
  return `<label class="${wide ? "full" : ""}">${esc(label)}<textarea name="${name}" required maxlength="2000">${esc(value)}</textarea></label>`;
}
function dialog(title, fields, submit, label = "Save") {
  setText("dialog-title", title);
  $("dialog-fields").innerHTML = fields;
  setText("form-error", "");
  $("dialog-submit").textContent = label;
  formSubmit = submit;
  $("operation-dialog").showModal();
}
const localDate = (value) => {
  const d = new Date(value);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 19);
};
function formRules(sensor) {
  const id = S.drawer,
    b = getBuoy(id),
    rules = b.sensors[sensor].rules;
  dialog(
    `${sensor} · sensor rules`,
    ["warn_min", "warn_max", "crit_min", "crit_max"]
      .map((k) => field(pretty(k), k, rules[k] ?? "", "number", 'step="any"'))
      .join("") +
      field(
        "Stale after (seconds)",
        "stale_after_seconds",
        rules.stale_after_seconds,
        "number",
        'required min="5" max="604800"',
      ) +
      `<label><input name="enabled" type="checkbox" ${rules.enabled ? "checked" : ""}> Enable alerts</label><p class="help full">Leave a value limit blank to disable it. Value alerts trigger outside the limits. Freshness uses the probe’s measurement time, independently of buoy heartbeats.</p>`,
    async (data) => {
      const payload = {
        enabled: data.has("enabled"),
        stale_after_seconds: Number(data.get("stale_after_seconds")),
      };
      for (const k of ["warn_min", "warn_max", "crit_min", "crit_max"])
        payload[k] = data.get(k) === "" ? null : Number(data.get(k));
      await api(
        `/api/buoys/${enc(id)}/sensors/${enc(sensor)}/rules`,
        "PUT",
        payload,
      );
    },
  );
}
function formMaintenance(id) {
  const b = getBuoy(id);
  if (!b) return;
  dialog(
    `Log work · ${id}`,
    `<label>Work performed<select name="kind">${["deployment", "calibration", "probe_swap", "battery_replacement", "site_visit", "other"].map((k) => `<option value="${k}">${pretty(k)}</option>`).join("")}</select></label><label>Sensor (optional)<select name="sensor"><option value="">Whole buoy</option>${Object.keys(
      b.sensors,
    )
      .map((s) => `<option value="${esc(s)}">${esc(s)}</option>`)
      .join("")}</select></label>` +
      field(
        "Performed at",
        "time",
        localDate(Date.now()),
        "datetime-local",
        'required step="1"',
      ) +
      field(
        "Operator",
        "operator",
        "Operator",
        "text",
        'required maxlength="120"',
      ) +
      textfield("What changed?", "description"),
    async (data) => {
      await api(`/api/buoys/${enc(id)}/maintenance`, "POST", {
        kind: data.get("kind"),
        sensor: data.get("sensor") || null,
        operator: data.get("operator"),
        description: data.get("description"),
        time: new Date(data.get("time")).toISOString(),
      });
      await maintenanceFor(id, true);
    },
  );
}
function formBuoy() {
  dialog(
    "Register a buoy",
    field(
      "Buoy ID",
      "id",
      "",
      "text",
      'required pattern="[A-Za-z0-9][A-Za-z0-9_.-]*" maxlength="80"',
    ) +
      field(
        "Latitude",
        "lat",
        0,
        "number",
        'step="any" min="-90" max="90" required',
      ) +
      field(
        "Longitude",
        "lng",
        0,
        "number",
        'step="any" min="-180" max="180" required',
      ) +
      field(
        "Sensors, comma separated",
        "sensors",
        "temp,ph",
        "text",
        "",
        true,
      ) +
      `<p class="help full">New sources start in simulation. Posting a real reading takes over that field. Units can be supplied when adding individual probes.</p>`,
    async (data) => {
      const id = data.get("id");
      await api("/api/buoys", "POST", {
        id,
        lat: Number(data.get("lat")),
        lng: Number(data.get("lng")),
        sensors: data
          .get("sensors")
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
      });
      S.selected.sensors = id;
      S.selected.analysis = id;
    },
  );
}
function formService() {
  dialog(
    "Register a service",
    field(
      "Service name",
      "name",
      "",
      "text",
      'required pattern="[A-Za-z0-9][A-Za-z0-9_.-]*" maxlength="80"',
      true,
    ) +
      field(
        "Warning latency (ms)",
        "warn_ms",
        400,
        "number",
        'min="1" required',
      ) +
      field(
        "Critical latency (ms)",
        "crit_ms",
        800,
        "number",
        'min="1" required',
      ),
    async (data) => {
      await api("/api/services", "POST", {
        name: data.get("name"),
        warn_ms: Number(data.get("warn_ms")),
        crit_ms: Number(data.get("crit_ms")),
      });
      S.selected.services = data.get("name");
    },
  );
}
function formSettings() {
  const b = getBuoy(S.drawer),
    id = b.id;
  dialog(
    `${id} · settings`,
    field(
      "Offline after (seconds)",
      "offline_after_seconds",
      b.offline_after_seconds,
      "number",
      'min="5" max="604800" required',
      true,
    ) +
      field(
        "Mooring latitude",
        "mooring_lat",
        b.mooring_lat,
        "number",
        'step="any" min="-90" max="90" required',
      ) +
      field(
        "Mooring longitude",
        "mooring_lng",
        b.mooring_lng,
        "number",
        'step="any" min="-180" max="180" required',
      ) +
      `<p class="help full">The contact deadline watches actual device check-ins once real data arrives. The mooring coordinates are the reference for this buoy’s drift.</p><button type="button" class="action full" data-action="simulate-buoy" data-source="${esc(id)}">Resume simulation for this buoy</button>`,
    async (data) => {
      await api(
        `/api/buoys/${enc(id)}/settings`,
        "PUT",
        Object.fromEntries([...data].map(([k, v]) => [k, Number(v)])),
      );
    },
  );
}
async function downloadExport(query) {
  const response = await fetch("/api/export?" + new URLSearchParams(query));
  if (!response.ok) {
    const result = await response.json();
    throw new Error(
      typeof result.detail === "string"
        ? result.detail
        : "Invalid export selection",
    );
  }
  const blob = await response.blob(),
    url = URL.createObjectURL(blob),
    a = document.createElement("a");
  a.href = url;
  a.download =
    query.kind === "incidents" ? "telem-incidents.csv" : "telem-readings.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function formExport(scope) {
  const incident = scope === "incidents",
    range = incident
      ? { start: Date.now() - 86400000, end: Date.now() }
      : activeWindow(scope),
    source = S.selected[scope];
  if (!incident && !source) {
    toast("Select a source first.");
    return;
  }
  const metrics =
    scope === "analysis"
      ? [...S.analysisMetrics]
      : [scope === "services" ? "latency_ms" : S.metric];
  if (!incident && !metrics.length) {
    toast("Select at least one metric.");
    return;
  }
  dialog(
    "Export " + (incident ? "incident history" : source),
    field(
      "From",
      "start",
      localDate(range.start),
      "datetime-local",
      'required step="1"',
    ) +
      field(
        "To",
        "end",
        localDate(range.end),
        "datetime-local",
        'required step="1"',
      ) +
      (incident
        ? ""
        : `<label>Readings<select name="aggregation"><option value="raw">Raw readings</option><option value="mean">Mean per interval</option><option value="min">Minimum per interval</option><option value="max">Maximum per interval</option><option value="last">Last per interval</option></select></label>` +
          field(
            "Interval (seconds)",
            "interval_seconds",
            60,
            "number",
            'min="1" max="86400" required',
          )) +
      `<p class="help full">${incident ? "Exports incidents that started in this window, including operator notes." : "Metrics: " + esc(metrics.join(", ")) + ". Uses this exact time window, including chart zoom or replay. Raw exports are limited to 20,000 metric readings; narrow the window or choose aggregation for larger requests."}</p>`,
    async (data) => {
      const query = {
        kind: incident
          ? "incidents"
          : scope === "services"
            ? "service"
            : "buoy",
        start: new Date(data.get("start")).toISOString(),
        end: new Date(data.get("end")).toISOString(),
      };
      if (!incident)
        Object.assign(query, {
          source,
          metrics: metrics.join(","),
          aggregation: data.get("aggregation"),
          interval_seconds: data.get("interval_seconds"),
        });
      await downloadExport(query);
    },
    "Download CSV",
  );
}
async function jump(kind, source, time) {
  const scope = kind === "service" ? "services" : "sensors";
  if (!(kind === "service" ? getService(source) : getBuoy(source))) {
    toast(
      "The source was deregistered; this incident is retained for reference.",
    );
    return;
  }
  closeDrawer();
  S.comparing[scope] = false;
  await selectSource(kind, source, false);
  const t = +new Date(time);
  S.windows[scope] = { start: t - 15 * 60000, end: t + 15 * 60000 };
  await loadChart(scope);
  toast("Showing the 30-minute window around " + shortTime(time));
}
async function handleAction(button) {
  const a = button.dataset.action,
    id = button.dataset.id;
  if (a === "inspect") {
    await selectSource(button.dataset.kind, button.dataset.source);
    return;
  }
  if (a === "refresh") {
    await refresh();
    return;
  }
  if (a === "clear-filter") {
    S.overviewFilter = null;
    renderOverview();
    return;
  }
  if (a === "overview-filter") {
    const filter = button.dataset.filter;
    if (filter === "incidents") {
      S.incidentFilter = "ongoing";
      document
        .querySelectorAll("#incident-filter button")
        .forEach((b) =>
          b.classList.toggle("active", b.dataset.filter === "ongoing"),
        );
      renderIncidents();
      switchTab("incidents");
      return;
    }
    S.overviewFilter = filter;
    renderOverview();
    const scope = filter === "rising" ? "services" : "sensors";
    $("status-" + scope).value = filter;
    renderSources();
    switchTab(scope);
    return;
  }
  if (a === "expand-incident") {
    S.expanded.has(id) ? S.expanded.delete(id) : S.expanded.add(id);
    renderIncidents();
    return;
  }
  if (a === "ack-incident") {
    const inc = S.incidents.find((i) => i.id === id);
    await api(`/api/incidents/${enc(id)}`, "PATCH", {
      acknowledged: !inc.acknowledged_at,
    });
    await refresh();
    return;
  }
  if (a === "resolve") {
    const inc = S.incidents.find((i) => i.id === id);
    await api(`/api/incidents/${enc(id)}`, "PATCH", {
      status: inc.status === "ongoing" ? "resolved" : "ongoing",
    });
    await refresh();
    return;
  }
  if (a === "edit-incident") {
    const inc = S.incidents.find((i) => i.id === id);
    dialog(
      "Incident owner and cause",
      field("Owner", "owner", inc.owner, "text", 'maxlength="120"', true) +
        textfield("Root cause / investigation", "cause", inc.cause),
      async (data) => {
        await api(
          `/api/incidents/${enc(id)}`,
          "PATCH",
          Object.fromEntries(data),
        );
      },
    );
    return;
  }
  if (a === "note") {
    dialog(
      "Add an incident note",
      field(
        "Author",
        "author",
        "Operator",
        "text",
        'required maxlength="120"',
        true,
      ) + textfield("Note", "text"),
      async (data) => {
        await api(
          `/api/incidents/${enc(id)}/notes`,
          "POST",
          Object.fromEntries(data),
        );
      },
    );
    return;
  }
  if (a === "ack-alert") {
    await api(`/api/alerts/${enc(id)}`, "PATCH", {
      acknowledged: button.dataset.acked !== "true",
    });
    await refresh();
    return;
  }
  if (a === "mute") {
    const source = button.dataset.source,
      kind = button.dataset.kind;
    dialog(
      `Mute ${source}`,
      field(
        "Mute for (minutes)",
        "duration_minutes",
        60,
        "number",
        'min="1" max="10080" required',
        true,
      ) +
        '<p class="help full">Hides this source from the event feeds for everyone using this dashboard. Incidents continue to be recorded.</p>',
      async (data) => {
        await api("/api/mutes", "POST", {
          source_type: kind,
          source,
          duration_minutes: Number(data.get("duration_minutes")),
        });
      },
    );
    return;
  }
  if (a === "unmute") {
    await api(`/api/mutes/${enc(id)}`, "DELETE");
    await refresh();
    return;
  }
  if (a === "jump") {
    await jump(button.dataset.kind, button.dataset.source, button.dataset.time);
    return;
  }
  if (a === "uncompare") {
    S.compare[button.dataset.scope].delete(button.dataset.source);
    await loadChart(button.dataset.scope);
    renderSources();
    return;
  }
  if (a === "add-buoy") {
    formBuoy();
    return;
  }
  if (a === "add-service") {
    formService();
    return;
  }
  if (a === "rules") {
    formRules(button.dataset.sensor);
    return;
  }
  if (a === "add-sensor") {
    const source = S.drawer;
    dialog(
      "Add a probe",
      field(
        "Sensor name",
        "sensor",
        "",
        "text",
        'required pattern="[A-Za-z0-9][A-Za-z0-9_.-]*" maxlength="80"',
      ) + field("Unit", "unit", "", "text", 'maxlength="30"'),
      async (data) => {
        await api(
          `/api/buoys/${enc(source)}/sensors`,
          "POST",
          Object.fromEntries(data),
        );
      },
    );
    return;
  }
  if (a === "buoy-settings") {
    formSettings();
    return;
  }
  if (a === "simulate-buoy") {
    await api(`/api/buoys/${enc(button.dataset.source)}/simulate`, "POST", {});
    $("operation-dialog").close();
    await refresh();
    toast("Simulation resumed. New samples will arrive on the next tick.");
    return;
  }
  if (a === "add-maintenance") {
    formMaintenance(S.drawer);
    return;
  }
  if (a === "analysis-maintenance") {
    formMaintenance(S.selected.analysis);
    return;
  }
  if (a === "open-analysis") {
    S.selected.analysis = S.drawer;
    closeDrawer();
    renderAnalysisControls();
    switchTab("analysis");
    return;
  }
  if (a === "analysis-live") {
    S.windows.analysis = null;
    await loadChart("analysis");
    return;
  }
  if (a === "export") {
    formExport(button.closest(".view").id.replace("view-", ""));
    return;
  }
  if (a === "analysis-export") {
    formExport("analysis");
    return;
  }
  if (a === "incident-export") {
    formExport("incidents");
    return;
  }
  if (a === "close-dialog") {
    $("operation-dialog").close();
    return;
  }
}

function renderPalette() {
  const q = $("cmdk-input").value.toLowerCase();
  const items = [
    ...S.services.map((s) => ({
      kind: "service",
      name: s.name,
      status: s.status,
    })),
    ...S.buoys.map((b) => ({ kind: "buoy", name: b.id, status: b.status })),
  ];
  const score = (name) => {
    name = name.toLowerCase();
    if (name.includes(q)) return name.indexOf(q);
    let at = 0;
    for (const c of name) if (c === q[at]) at++;
    return at === q.length ? 100 : Infinity;
  };
  paletteResults = items
    .filter((i) => score(i.name) < Infinity)
    .sort((a, b) => score(a.name) - score(b.name));
  paletteIndex = Math.max(0, Math.min(paletteIndex, paletteResults.length - 1));
  $("cmdk-list").innerHTML =
    paletteResults
      .map(
        (i, index) =>
          `<button class="cmdk-item ${index === paletteIndex ? "selected" : ""}" data-palette="${index}"><span class="dot ${dot(i.status)}"></span><span class="cmdk-name">${esc(i.name)}</span><span class="muted">${i.kind}</span></button>`,
      )
      .join("") || '<div class="empty">No matching sources.</div>';
}
function openPalette() {
  $("cmdk-backdrop").classList.add("open");
  $("cmdk-input").value = "";
  paletteIndex = 0;
  renderPalette();
  $("cmdk-input").focus();
}
function closePalette() {
  $("cmdk-backdrop").classList.remove("open");
}
async function pickPalette(index) {
  const item = paletteResults[index];
  if (!item) return;
  closePalette();
  const scope = item.kind === "service" ? "services" : "sensors";
  S.comparing[scope] = false;
  await selectSource(item.kind, item.name);
}

async function refresh() {
  if (S.refreshing) return S.refreshing;
  S.refreshing = (async () => {
    const [services, buoys, alerts, incidents, mutes, overview] =
      await Promise.all(
        [
          "/api/services",
          "/api/buoys",
          "/api/alerts?limit=100",
          "/api/incidents",
          "/api/mutes",
          "/api/overview",
        ].map((p) => api(p)),
      );
    const oldAnalysis = S.selected.analysis,
      oldMetricNames = Object.keys(getBuoy(oldAnalysis)?.sensors || {}).join(
        ",",
      );
    Object.assign(S, { services, buoys, alerts, incidents, mutes, overview });
    if (!getService()) S.selected.services = services[0]?.name || null;
    if (!getBuoy()) S.selected.sensors = buoys[0]?.id || null;
    if (!getBuoy(S.selected.analysis))
      S.selected.analysis = buoys[0]?.id || null;
    for (const scope of ["services", "sensors"])
      for (const id of S.compare[scope])
        if (!(scope === "services" ? getService(id) : getBuoy(id)))
          S.compare[scope].delete(id);
    renderOverview();
    renderSources();
    renderReadouts();
    renderFeeds();
    renderIncidents();
    if (
      oldAnalysis !== S.selected.analysis ||
      oldMetricNames !==
        Object.keys(getBuoy(S.selected.analysis)?.sensors || {}).join(",") ||
      $("analysis-buoy").options.length !== buoys.length
    )
      renderAnalysisControls();
    else if ($("analysis-buoy").options.length === 0) renderAnalysisControls();
    if ($("cmdk-backdrop").classList.contains("open")) renderPalette();
    if (charts[S.tab]) await loadChart(S.tab);
    S.lastSync = Date.now();
    S.lastError = null;
    $("sync-banner").classList.remove("show");
    const real =
      buoys.some((b) => !b.contact_simulated) ||
      services.some((s) => !s.simulated);
    $("env-pill").innerHTML =
      '<span class="dot"></span>' +
      (real ? "Live + simulated" : "Demo simulation");
  })();
  try {
    await S.refreshing;
  } finally {
    S.refreshing = false;
  }
}
async function poll() {
  try {
    await refresh();
  } catch (e) {
    S.lastError = e.message;
    $("sync-banner").classList.add("show");
    setText("sync-banner-text", e.message + " · retrying automatically");
  } finally {
    setTimeout(poll, 4000);
  }
}

$("operation-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  setText("form-error", "");
  $("dialog-submit").disabled = true;
  try {
    await formSubmit(new FormData(e.currentTarget));
    $("operation-dialog").close();
    await refresh();
    toast("Saved.");
  } catch (err) {
    setText("form-error", err.message);
  } finally {
    $("dialog-submit").disabled = false;
  }
});
document.addEventListener("click", async (e) => {
  try {
    const action = e.target.closest("[data-action]");
    if (action) {
      await handleAction(action);
      return;
    }
    const selected = e.target.closest("[data-select]");
    if (selected) {
      await selectSource(selected.dataset.select, selected.dataset.source);
      return;
    }
    const tab = e.target.closest("[data-view]");
    if (tab) {
      switchTab(tab.dataset.view);
      return;
    }
    const theme = e.target.closest("button[data-theme]");
    if (theme) {
      setTheme(theme.dataset.theme);
      return;
    }
    const metric = e.target.closest("[data-metric]");
    if (metric) {
      S.metric = metric.dataset.metric;
      renderReadouts();
      await loadChart("sensors");
      return;
    }
    const range = e.target.closest("[data-minutes]");
    if (range) {
      const scope = range.closest(".view").id.replace("view-", "");
      S.minutes[scope] = Number(range.dataset.minutes);
      S.windows[scope] = null;
      $("scrub-range-" + scope).value = "1000";
      range.parentElement
        .querySelectorAll("button")
        .forEach((b) => b.classList.toggle("active", b === range));
      await loadChart(scope);
      return;
    }
    const zoom = e.target.closest("[data-zoom]");
    if (zoom) {
      const scope = zoom.closest(".view").id.replace("view-", "");
      if (zoom.dataset.zoom === "reset") {
        S.windows[scope] = null;
        $("scrub-range-" + scope).value = "1000";
        await loadChart(scope);
      } else charts[scope].zoom(zoom.dataset.zoom === "in" ? 0.5 : 2);
      return;
    }
    const incidentFilter = e.target.closest("[data-filter]");
    if (incidentFilter && incidentFilter.closest("#incident-filter")) {
      S.incidentFilter = incidentFilter.dataset.filter;
      incidentFilter.parentElement
        .querySelectorAll("button")
        .forEach((b) => b.classList.toggle("active", b === incidentFilter));
      renderIncidents();
      return;
    }
    const pick = e.target.closest("[data-palette]");
    if (pick) {
      await pickPalette(Number(pick.dataset.palette));
      return;
    }
  } catch (err) {
    toast(err.message);
  }
});
for (const scope of ["services", "sensors"]) {
  $("filter-" + scope).addEventListener("input", renderSources);
  $("status-" + scope).addEventListener("change", renderSources);
  const slider = $("scrub-range-" + scope);
  slider.min = 0;
  slider.max = 1000;
  slider.value = 1000;
  slider.setAttribute("aria-label", "Replay through recent history");
  slider.addEventListener("input", () => {
    const end =
      Date.now() -
      (1 - Number(slider.value) / 1000) * S.minutes[scope] * 60000 * 3;
    setWindow(
      scope,
      slider.value === "1000"
        ? null
        : { start: end - S.minutes[scope] * 60000, end },
    );
  });
  $("compare-toggle-" + scope).addEventListener("click", () => {
    S.comparing[scope] = !S.comparing[scope];
    $("compare-toggle-" + scope).classList.toggle("active", S.comparing[scope]);
    loadChart(scope).catch((e) => toast(e.message));
  });
  const table = $(
    scope === "services" ? "services-tbody" : "buoys-tbody",
  ).closest("table");
  const keys =
    scope === "services"
      ? ["name", "status", "rps", "latency_ms", "error_rate"]
      : [
          "id",
          "status",
          "battery",
          "last_contact_seconds",
          "satellites",
          "signal_dbm",
        ];
  table.querySelectorAll("th[data-sort]").forEach((th, index) => {
    th.tabIndex = 0;
    th.addEventListener("click", () => {
      const key = keys[index],
        dir = S.sort[scope].key === key ? -S.sort[scope].dir : 1;
      S.sort[scope] = { key, dir };
      table
        .querySelectorAll("th")
        .forEach((t) => t.removeAttribute("aria-sort"));
      th.setAttribute("aria-sort", dir === 1 ? "ascending" : "descending");
      renderSources();
    });
  });
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  table.parentNode.insertBefore(wrap, table);
  wrap.appendChild(table);
}
$("analysis-buoy").addEventListener("change", () => {
  S.selected.analysis = $("analysis-buoy").value;
  S.windows.analysis = null;
  renderAnalysisControls();
  writeHash();
  loadChart("analysis").catch((e) => toast(e.message));
});
$("analysis-range").addEventListener("change", () => {
  S.minutes.analysis = Number($("analysis-range").value);
  S.windows.analysis = null;
  loadChart("analysis").catch((e) => toast(e.message));
});
$("analysis-scale").addEventListener("change", () => {
  charts.analysis.normalized = $("analysis-scale").value === "normalized";
  charts.analysis.render();
});
$("analysis-metrics").addEventListener("change", (e) => {
  if (e.target.checked) S.analysisMetrics.add(e.target.value);
  else S.analysisMetrics.delete(e.target.value);
  loadChart("analysis").catch((e) => toast(e.message));
});
$("drawer-close").addEventListener("click", closeDrawer);
$("drawer-backdrop").addEventListener("click", closeDrawer);
$("cmdk-open").addEventListener("click", openPalette);
$("cmdk-input").addEventListener("input", () => {
  paletteIndex = 0;
  renderPalette();
});
$("cmdk-backdrop").addEventListener("click", (e) => {
  if (e.target === $("cmdk-backdrop")) closePalette();
});
document.addEventListener("keydown", async (e) => {
  if (
    e.key === "Tab" &&
    S.drawer &&
    !$("operation-dialog").open &&
    !$("cmdk-backdrop").classList.contains("open")
  ) {
    const focusable = [
      ...$("buoy-drawer").querySelectorAll(
        'button,input,select,textarea,[tabindex="0"]',
      ),
    ].filter((el) => !el.disabled);
    const first = focusable[0],
      last = focusable.at(-1);
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last?.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first?.focus();
    }
  }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    openPalette();
  }
  if (e.key === "Escape") {
    closePalette();
    closeDrawer();
  }
  if ($("cmdk-backdrop").classList.contains("open")) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      paletteIndex += e.key === "ArrowDown" ? 1 : -1;
      renderPalette();
    }
    if (e.key === "Enter") {
      e.preventDefault();
      try {
        await pickPalette(paletteIndex);
      } catch (err) {
        toast(err.message);
      }
    }
  } else if (
    (e.key === "Enter" || e.key === " ") &&
    e.target.matches('[role="button"],th[data-sort]')
  ) {
    e.preventDefault();
    e.target.click();
  }
});
window.addEventListener("hashchange", () => {
  applyHash();
  renderSources();
  renderReadouts();
  renderAnalysisControls();
});
let hoverAnchor = null;
document.addEventListener("mouseover", (e) => {
  const item = e.target.closest(".svc-item[data-select]");
  if (!item || item === hoverAnchor) return;
  hoverAnchor = item;
  const kind = item.dataset.select,
    source = item.dataset.source,
    s = kind === "service" ? getService(source) : getBuoy(source);
  if (!s) return;
  const box = $("hover-preview");
  box.innerHTML = `<div class="hover-preview-name">${esc(source)}</div><div class="hover-preview-row">${esc(s.status)}</div><div class="hover-preview-row">${kind === "service" ? number(s.latency_ms) + " ms · " + number(s.rps) + " req/s" : number(s.battery) + "% battery · " + ago(s.last_contact_at)}</div><div class="hover-preview-hint">Click to inspect${S.comparing[kind === "service" ? "services" : "sensors"] ? " or compare" : ""}</div>`;
  const rect = item.getBoundingClientRect();
  box.style.left = Math.min(rect.right + 8, window.innerWidth - 230) + "px";
  box.style.top = Math.min(rect.top, window.innerHeight - 120) + "px";
  box.style.opacity = 1;
});
document.addEventListener("mouseout", (e) => {
  if (hoverAnchor && !hoverAnchor.contains(e.relatedTarget)) {
    hoverAnchor = null;
    $("hover-preview").style.opacity = 0;
  }
});

// Preserve the existing service cards while replacing static mock figures with
// honest availability labels; the ingestion contract does not report these yet.
const statPanels = document.querySelectorAll(
  "#view-services .stats-row>.panel",
);
statPanels[1].querySelector(".stat-body").innerHTML =
  '<div class="stat-num">—</div><div class="stat-delta">Not reported</div><p class="muted">An uptime target and observation history are needed to calculate an error budget.</p>';
statPanels[2].querySelector(".stat-body").innerHTML =
  '<div class="stat-num">—</div><div class="stat-delta">Not reported</div><p class="muted">Current service ingestion reports overall error rate, shown in the services table.</p>';
setTheme(localStorage.getItem("telem-theme") || "amber");
setInterval(() => {
  setText("clock", new Date().toLocaleTimeString());
  setText(
    "last-sync",
    S.lastSync
      ? "last sync " + Math.floor((Date.now() - S.lastSync) / 1000) + "s ago"
      : "waiting for API",
  );
}, 1000);
(async () => {
  try {
    await refresh();
    applyHash();
    renderSources();
    renderReadouts();
    renderAnalysisControls();
  } catch (e) {
    S.lastError = e.message;
    setText("sync-banner-text", e.message + " · retrying automatically");
    $("sync-banner").classList.add("show");
  }
  setTimeout(poll, 4000);
})();
