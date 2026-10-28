// Run with: node --test tests/receiver_log.test.mjs
import assert from "node:assert/strict";
import test from "node:test";
import { describeLog, highlight, parseRow, rowClass } from "../frontend/receiver-log.js";

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

test("chatter and heartbeat lines can be hidden but errors and markers stay", () => {
  const lines = [
    "10:00:00  board   log      rak> OK",
    "10:00:01  board   idle     listening, 3 packets so far",
    "10:00:02  bridge  fail     HTTP 500 - spooled",
  ];
  assert.equal(describeLog(lines, true).length, 3);
  assert.deepEqual(describeLog(lines, false).map((r) => r.cls), ["err"]);
});
