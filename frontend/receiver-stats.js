/* Sensor analytics over a time series: [{time, value}], oldest first. Pure functions. */

const nums = (points) => points.map((p) => p.value).filter((v) => Number.isFinite(v));
const median = (xs) => {
  const s = [...xs].sort((a, b) => a - b),
    m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};

export function summarize(points) {
  const v = nums(points);
  if (!v.length) return null;
  const avg = v.reduce((a, b) => a + b, 0) / v.length;
  return {
    n: v.length,
    last: v.at(-1),
    min: Math.min(...v),
    max: Math.max(...v),
    avg,
    std: Math.sqrt(v.reduce((a, b) => a + (b - avg) ** 2, 0) / v.length),
  };
}

/* Change over the last `minutes`, or over everything we have when the series is shorter
   (`spanMin` says which). */
export function change(points, minutes) {
  const valid = points.filter((p) => Number.isFinite(p.value));
  if (valid.length < 2) return null;
  const last = valid.at(-1),
    t = +new Date(last.time) - minutes * 60000,
    from = valid.findLast((p) => +new Date(p.time) <= t) || valid[0];
  const spanMin = (+new Date(last.time) - +new Date(from.time)) / 60000;
  return spanMin > 0 ? { delta: last.value - from.value, spanMin } : null;
}

/* How many of the most recent readings are exactly the same value. A flat probe is a finding. */
export function stuckRun(points) {
  const v = nums(points);
  let n = 0;
  while (n < v.length && v[v.length - 1 - n] === v.at(-1)) n++;
  return v.length ? n : 0;
}

/* Share of expected readings that arrived between the first and last point. */
export function availability(points, periodS = 5) {
  const valid = points.filter((p) => Number.isFinite(p.value));
  if (valid.length < 2) return null;
  const span = (+new Date(valid.at(-1).time) - +new Date(valid[0].time)) / 1000;
  return Math.min(100, (100 * valid.length) / (span / periodS + 1));
}

/* Robust z-score of the latest reading against the ones before it (median / MAD). */
export function outlierZ(points) {
  const v = nums(points);
  if (v.length < 20) return null;
  const rest = v.slice(0, -1),
    med = median(rest),
    mad = median(rest.map((x) => Math.abs(x - med)));
  return mad > 0 ? (0.6745 * (v.at(-1) - med)) / mad : null;
}

/* Magnus approximation, good to about 0.4 C between 0 and 60 C. */
export function dewPoint(tempC, rh) {
  if (!Number.isFinite(tempC) || !Number.isFinite(rh) || rh <= 0) return null;
  const a = 17.62,
    b = 243.12,
    g = Math.log(rh / 100) + (a * tempC) / (b + tempC);
  return (b * g) / (a - g);
}

/* Values read off the latest point of each series, plus how they moved over the last hour.
   `levels` are heuristics for a sealed hull, not calibrated limits. */
export function derived(series) {
  const last = (k) => nums(series[k] || []).at(-1),
    hour = (k) => change(series[k] || [], 60),
    out = [];
  const dpAir = dewPoint(last("air_temp"), last("humidity"));
  if (dpAir !== null) out.push({ key: "dew_air", label: "Air dew point", value: dpAir, unit: "°C" });
  const dpHull = dewPoint(last("core_temp"), last("core_humidity"));
  if (dpHull !== null) {
    out.push({ key: "dew_hull", label: "Hull dew point", value: dpHull, unit: "°C" });
    const water = last("temp");
    if (Number.isFinite(water)) {
      const margin = water - dpHull;
      out.push({
        key: "condensation", label: "Condensation margin", value: margin, unit: "°C",
        level: margin < 0 ? "crit" : margin < 3 ? "warn" : "ok",
        hint: "Water temperature minus the hull's dew point. Near or below 0 means moisture can condense inside.",
      });
    }
  }
  const h = hour("core_humidity");
  if (h) {
    const rate = (h.delta / h.spanMin) * 60;
    out.push({
      key: "hull_rh_rate", label: "Hull humidity trend", value: rate, unit: "%RH/h",
      level: rate > 15 ? "crit" : rate > 5 ? "warn" : "ok",
      hint: "A steady rise inside a sealed hull can mean a leak.",
    });
  }
  // Weather trends need hours: use the 3 h series when it has at least an hour in it.
  const long = series.pressure_3h ? change(series.pressure_3h, 180) : null,
    p = long && long.spanMin >= 60 ? long : hour("pressure");
  if (p) {
    const rate = (p.delta / p.spanMin) * 60;
    out.push({
      key: "pressure_tendency",
      label: `Pressure tendency (${p.spanMin >= 150 ? "3 h" : Math.round(p.spanMin) + " min"})`,
      value: rate, unit: "hPa/h",
      note: Math.abs(rate) < 0.5 ? "steady" : rate > 0 ? "rising" : "falling",
    });
  }
  const air = last("air_temp"),
    water = last("temp"),
    hull = last("core_temp");
  if (Number.isFinite(air) && Number.isFinite(water))
    out.push({ key: "air_water", label: "Air minus water", value: air - water, unit: "°C" });
  if (Number.isFinite(hull) && Number.isFinite(air))
    out.push({ key: "hull_air", label: "Hull minus air", value: hull - air, unit: "°C" });
  return out;
}

/* Rolling median of the last `window` readings: keeps real movement, drops single-reading spikes. */
export function smooth(points, window = 9) {
  const valid = points.filter((p) => Number.isFinite(p.value));
  return valid.map((p, i) => ({
    time: p.time,
    value: median(valid.slice(Math.max(0, i - window + 1), i + 1).map((q) => q.value)),
  }));
}

/* Everything the analytics table shows for one sensor. `periodS` is the spacing of the points
   (5 s for raw readings, the bucket size for averaged ones). */
export function analyse(points, periodS = 5) {
  const s = summarize(points);
  if (!s) return null;
  const run = stuckRun(points),
    z = outlierZ(points),
    avail = availability(points, periodS),
    flags = [];
  if (run >= 3 && run * periodS >= 60 && s.n >= 12) {
    const sec = run * periodS;
    flags.push({ kind: "stuck", text: `flat for ${sec >= 120 ? Math.round(sec / 60) + " min" : sec + " s"}` });
  }
  if (z !== null && Math.abs(z) > 6) flags.push({ kind: "outlier", text: `outlier (z ${z.toFixed(1)})` });
  if (avail !== null && avail < 90) flags.push({ kind: "gaps", text: `${avail.toFixed(0)}% of readings` });
  return { ...s, change10: change(points, 10), change60: change(points, 60), availability: avail, flags };
}
