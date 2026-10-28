/* Turns bridge output lines into styled rows. Pure data in, data out, so it can be
   tested without a DOM: each row is {cls, parts: [{t, c?, title?}]}. */

// Thresholds from the VITAL LoRa receiver guide: the radio edge is about -120 dBm and the
// guide advises moving the boards closer below -115; LoRa still decodes a little below 0 dB SNR.
export const rssiQuality = (dbm) => (dbm >= -100 ? "good" : dbm >= -115 ? "ok" : "bad");
export const snrQuality = (db) => (db >= 5 ? "good" : db >= 0 ? "ok" : "bad");
const WORDS = { good: "strong", ok: "fair", bad: "weak" };
// The buoy transmits every 5 s; this much silence between packets is worth calling out.
export const SILENCE_S = 12;

// Every line is `HH:MM:SS  source  action  detail` (the server normalises them).
const ROW = /^(\d\d:\d\d:\d\d)\s+(telem|bridge|board|flash)\s+(\S+)\s*(.*)$/;
export const parseRow = (line) => {
  const m = ROW.exec(line);
  return m && { time: m[1], src: m[2], act: m[3], detail: m[4] };
};

const BY_ACTION = {
  rx: "rx", tx: "rx", post: "sent", dry: "dry", connect: "conn", idle: "hb",
  warn: "warn", skip: "warn", spool: "warn", fail: "err", out: "flash",
};
export function rowClass(line) {
  const r = parseRow(line);
  if (!r) return "chatter";
  if (/\b(FAILED?|ERROR|Traceback|timed out)\b/i.test(r.detail)) return "err";
  if (r.act === "serial") return /\blost\b/.test(r.detail) ? "err" : "warn";
  if (/^\.\.\. listening, 0 |^listening, 0 /.test(r.detail)) return "warn";
  if (r.src === "telem") return "telem";
  return BY_ACTION[r.act] || "chatter";
}

const TOKEN =
  /(RSSI -?\d+ dBm)|(SNR -?\d+ dB)|(HTTP \d{3})|(\[DRY RUN\])|(\/dev\/\S+|\bCOM\d+)|(#\d+)|(\d+(?:\.\d+)? ppm)/g;

function tokenPart(m) {
  const t = m[0];
  if (m[1]) {
    const q = rssiQuality(parseInt(t.slice(5)));
    return { t, c: "q-" + q, title: `Signal strength: ${WORDS[q]}` };
  }
  if (m[2]) {
    const q = snrQuality(parseInt(t.slice(4)));
    return { t, c: "q-" + q, title: `Signal quality: ${WORDS[q]}` };
  }
  if (m[3]) return { t, c: t.endsWith("200") ? "q-good" : "q-bad" };
  if (m[4]) return { t, c: "tag-dry", title: "Dry run: readings are not posted to telem" };
  if (m[5]) return { t, c: "port" };
  if (m[6]) return { t, c: "seq" };
  return { t, c: "val" };
}

export function highlight(line) {
  const parts = [];
  let rest = line;
  const head = /^(\d\d:\d\d:\d\d)(\s+)(telem|bridge|board|flash)(\s+)(\S+)/.exec(line);
  if (head) {
    parts.push(
      { t: head[1], c: "time" }, { t: head[2] },
      { t: head[3], c: "src" }, { t: head[4] },
      { t: head[5], c: "act" },
    );
    rest = line.slice(head[0].length);
  }
  let at = 0;
  for (const m of rest.matchAll(TOKEN)) {
    if (m.index > at) parts.push({ t: rest.slice(at, m.index) });
    parts.push(tokenPart(m));
    at = m.index + m[0].length;
  }
  if (at < rest.length) parts.push({ t: rest.slice(at) });
  return parts;
}

const marker = (cls, text) => ({ cls: "mark-" + cls, parts: [{ t: text }] });

// Same allowance as the server: the first packets after connecting may skip ahead.
const FRESH_PACKETS = 3;

export function describeLog(lines, showChatter = true) {
  const rows = [];
  let prevSeq = null,
    prevSec = null,
    fresh = 0;
  for (const line of lines) {
    if (/^\S+\s+telem\s+(start|stop|flash)\b/.test(line)) prevSeq = prevSec = null;
    if (/^\S+\s+(telem\s+(start|stop|flash)|bridge\s+connect)\b/.test(line)) fresh = FRESH_PACKETS;
    const m = /^(\d\d):(\d\d):(\d\d)\s+board\s+rx\s+#(\d+)/.exec(line);
    if (m) {
      const sec = +m[1] * 3600 + +m[2] * 60 + +m[3],
        seq = +m[4];
      if (prevSeq !== null) {
        const lost = seq - prevSeq - 1;
        const range = `#${prevSeq + 1}${lost > 1 ? "–#" + (seq - 1) : ""}`,
          plural = lost > 1 ? "s" : "";
        if (lost > 0 && fresh > 0) rows.push(marker("info", `↷ ${lost} packet${plural} missed while connecting (${range})`));
        else if (lost > 0) rows.push(marker("err", `⚠ ${lost} packet${plural} lost (${range})`));
        else if (seq <= prevSeq) rows.push(marker("warn", "↺ buoy restarted (sequence went back)"));
        if (sec - prevSec > SILENCE_S) rows.push(marker("warn", `⏸ ${sec - prevSec} s without a packet`));
      }
      prevSeq = seq;
      prevSec = sec;
      fresh = Math.max(0, fresh - 1);
    }
    const cls = rowClass(line);
    if ((cls === "chatter" || cls === "hb") && !showChatter) continue;
    rows.push({ cls, parts: highlight(line) });
  }
  return rows;
}
