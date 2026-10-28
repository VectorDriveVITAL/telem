// Run with: node --test tests/receiver_log.test.mjs
import assert from "node:assert/strict";
import test from "node:test";
import { createDescriber, describeLog, highlight, parseRow, rowCategory, rowClass } from "../frontend/receiver-log.js";

const text = (row) => row.parts.map((p) => p.t).join("");
const part = (parts, t) => parts.find((p) => p.t === t);
const RX = (t, n, rssi = -18, snr = 12) => `${t}  board   rx       #${n}  RSSI ${rssi} dBm  SNR ${snr} dB`;

test("a line splits into time, source, action and detail", () => {
  assert.deepEqual(parseRow("10:24:10  bridge  post     10.51 ppm  (HTTP 200)"), {
    time: "10:24:10", src: "bridge", act: "post", detail: "10.51 ppm  (HTTP 200)",
  });
  assert.equal(parseRow("not a log line"), null);
});

test("rows are classified by source and action", () => {
  const cls = (l) => rowClass(l);
  assert.equal(cls(RX("10:24:10", 11)), "rx");
  assert.equal(cls("10:24:10  board   tx       #5  V1,5,,"), "rx");
  assert.equal(cls("10:24:10  bridge  post     10.51 ppm  (HTTP 200)"), "sent");
  assert.equal(cls('10:25:40  bridge  dry      POST {"a": 1}'), "dry");
  assert.equal(cls("10:24:09  bridge  connect  /dev/cu.usbmodem1102 @ 115200"), "conn");
  assert.equal(cls("10:24:22  board   idle     listening, 3 packets so far"), "hb");
  assert.equal(cls("10:24:22  board   idle     listening, 0 packets so far"), "warn");
  assert.equal(cls("19:46:11  board   log      rak> OK"), "chatter");
  assert.equal(cls("19:41:23  board   warn     waiting for probe (no device at 0x44 or 0x45)"), "warn");
  assert.equal(cls("10:25:40  telem   stop     bridge"), "telem");
  assert.equal(cls("10:25:40  telem   flash    FAILED"), "err");
  assert.equal(cls("10:25:41  bridge  fail     HTTP 500: boom - spooled"), "err");
  assert.equal(cls("10:25:41  bridge  serial   serial lost (x) - reconnecting in 3s"), "err");
  assert.equal(cls("10:25:41  bridge  serial   no ST-LINK serial port found, waiting..."), "warn");
  assert.equal(cls("10:25:41  flash   out      Download verified successfully"), "flash");
});

test("source and action are highlighted as columns, text is preserved", () => {
  const line = RX("10:24:10", 11);
  const parts = highlight(line);
  assert.equal(parts.map((p) => p.t).join(""), line);
  assert.equal(part(parts, "board").c, "src");
  assert.equal(part(parts, "rx").c, "act");
  assert.equal(part(parts, "RSSI -18 dBm").c, "q-good");
  assert.equal(part(parts, "SNR 12 dB").c, "q-good");
  assert.equal(part(parts, "#11").c, "seq");
});

test("signal and http values are coloured by quality", () => {
  const q = (l, t) => part(highlight(l), t).c;
  assert.equal(q(RX("10:00:00", 1, -118, -3), "RSSI -118 dBm"), "q-bad");
  assert.equal(q(RX("10:00:00", 1, -118, -3), "SNR -3 dB"), "q-bad");
  assert.equal(q(RX("10:00:00", 1, -105, 3), "RSSI -105 dBm"), "q-ok");
  assert.equal(q("10:00:00  bridge  post     1 ppm  (HTTP 200)", "HTTP 200"), "q-good");
  assert.equal(q("10:00:00  bridge  fail     HTTP 500: x", "HTTP 500"), "q-bad");
});

test("lost packets, buoy restarts and silences get marker rows", () => {
  const rows = describeLog([
    RX("10:00:00", 10), RX("10:00:05", 11), RX("10:00:20", 14), RX("10:00:25", 1),
  ]);
  assert.deepEqual(rows.filter((r) => r.cls.startsWith("mark-")).map(text), [
    "⚠ 2 packets lost (#12–#13)",
    "⏸ 15 s without a packet",
    "↺ buoy restarted (sequence went back)",
  ]);
});

test("seq jumps right after connecting are informational, later ones are lost packets", () => {
  const rows = describeLog([
    "10:37:07  bridge  connect  /dev/cu.usbmodem1102 @ 115200",
    RX("10:37:07", 159), RX("10:37:08", 160), RX("10:37:11", 167),
    RX("10:37:16", 168), RX("10:37:21", 169), RX("10:37:26", 172),
  ]);
  assert.deepEqual(rows.filter((r) => r.cls.startsWith("mark-")).map((r) => [r.cls, text(r)]), [
    ["mark-info", "↷ 6 packets missed while connecting (#161–#166)"],
    ["mark-err", "⚠ 2 packets lost (#170–#171)"],
  ]);
});

test("a bridge restart does not count as lost packets", () => {
  const rows = describeLog([
    RX("10:25:35", 28),
    "10:25:40  telem   stop     bridge",
    "10:25:40  telem   start    bridge -> http://127.0.0.1:8000",
    RX("10:27:55", 56),
  ]);
  assert.equal(rows.filter((r) => r.cls.startsWith("mark-")).length, 0);
});

test("rows fall into the console's filter groups, problems always as issues", () => {
  const cat = rowCategory;
  assert.equal(cat(RX("10:00:00", 1)), "packets");
  assert.equal(cat("10:00:00  board   tx       #5  V1"), "packets");
  assert.equal(cat("10:00:00  bridge  post     1 ppm  (HTTP 200)"), "posts");
  assert.equal(cat("10:00:00  bridge  dry      POST {}"), "posts");
  assert.equal(cat("10:00:00  bridge  fail     HTTP 500"), "issues");
  assert.equal(cat("10:00:00  board   warn     waiting for probe"), "issues");
  assert.equal(cat("10:00:00  bridge  serial   serial lost (x)"), "issues");
  assert.equal(cat("10:00:00  telem   stop     bridge"), "system");
  assert.equal(cat("10:00:00  bridge  connect  /dev/x @ 115200"), "system");
  assert.equal(cat("10:00:00  flash   out      done"), "system");
  assert.equal(cat("10:00:00  board   log      rak> OK"), "board");
  assert.equal(cat("10:00:00  board   idle     listening, 3 packets so far"), "board");
});

test("a bridge start becomes a session divider and markers carry a category", () => {
  const d = createDescriber();
  const [row] = d.push("10:24:09  telem   start    bridge -> http://127.0.0.1:8000");
  assert.equal(row.cls, "session");
  assert.equal(row.session, true);
  assert.equal(text(row), "bridge session 10:24:09   bridge -> http://127.0.0.1:8000");
  d.push(RX("10:24:10", 5));
  const [early] = d.push(RX("10:24:30", 9)); // still connecting: informational
  assert.equal(early.cat, "packets");
  d.push(RX("10:24:35", 10));
  d.push(RX("10:24:40", 11));
  const [lost] = d.push(RX("10:24:50", 14));
  assert.equal(lost.cat, "issues");
});

test("pushing line by line gives the same rows as describing them all at once", () => {
  const lines = [RX("10:00:00", 10), RX("10:00:05", 11), RX("10:00:20", 14)];
  const d = createDescriber();
  assert.deepEqual(lines.flatMap((l) => d.push(l)), describeLog(lines));
});

test("a dry-run payload collapses to a summary and keeps its JSON", () => {
  const d = createDescriber();
  const [row] = d.push('10:25:40  bridge  dry      POST {"time": "2026-10-28T15:25:40Z", "temp": 21.7, "ph": 7.5}');
  assert.deepEqual(row.json, { time: "2026-10-28T15:25:40Z", temp: 21.7, ph: 7.5 });
  assert.equal(text(row), "10:25:40  bridge  dry      POST 3 fields  ▸ show");
  assert.equal(row.parts.at(-1).c, "toggle");
  const [broken] = d.push("10:25:41  bridge  dry      POST {not json");
  assert.equal(broken.json, undefined); // a cut-off line is shown as it is
});

test("receive rows carry the packet they describe", () => {
  const d = createDescriber();
  const rows = d.push(RX("10:24:10", 11));
  assert.deepEqual(rows.at(-1).packet, { seq: 11, time: "10:24:10" });
  assert.equal(d.push("10:24:10  bridge  post     1 ppm  (HTTP 200)").at(-1).packet, undefined);
});
