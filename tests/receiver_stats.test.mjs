// Run with: node --test tests/receiver_stats.test.mjs
import assert from "node:assert/strict";
import test from "node:test";
import { analyse, availability, change, derived, dewPoint, outlierZ, smooth, stuckRun, summarize } from "../frontend/receiver-stats.js";

const T0 = Date.parse("2026-10-28T12:00:00Z");
const series = (values, stepS = 5) =>
  values.map((value, i) => ({ time: new Date(T0 + i * stepS * 1000).toISOString(), value }));

test("summarize gives last, range, mean and standard deviation", () => {
  const s = summarize(series([1, 2, 3, null, 4]));
  assert.deepEqual([s.n, s.last, s.min, s.max, s.avg], [4, 4, 1, 4, 2.5]);
  assert.ok(Math.abs(s.std - Math.sqrt(1.25)) < 1e-9);
  assert.equal(summarize(series([null])), null);
});

test("change uses the requested window, or everything when the series is shorter", () => {
  const v = series(Array.from({ length: 121 }, (_, i) => i), 30); // 1 h of 30 s points, value = index
  assert.deepEqual(change(v, 10), { delta: 20, spanMin: 10 });
  assert.deepEqual(change(series([5, 7, 9], 60), 60), { delta: 4, spanMin: 2 });
  assert.equal(change(series([1]), 10), null);
});

test("a run of identical values is detected", () => {
  assert.equal(stuckRun(series([1, 2, 0, 0, 0, 0])), 4);
  assert.equal(stuckRun(series([3, 3, 3, 4])), 1);
  assert.equal(stuckRun([]), 0);
});

test("availability is the share of expected readings that arrived", () => {
  assert.equal(availability(series([1, 2, 3, 4, 5])), 100);
  assert.equal(availability(series([1, 2, 3, 4, 5]).filter((_, i) => i !== 2)), (100 * 4) / 5);
  assert.equal(availability(series([1])), null);
});

test("outlier z-score needs history and a spread", () => {
  const calm = Array.from({ length: 40 }, (_, i) => 10 + (i % 3) * 0.1);
  assert.ok(Math.abs(outlierZ(series([...calm, 10.1]))) < 3);
  assert.ok(outlierZ(series([...calm, 25])) > 6);
  assert.equal(outlierZ(series([...Array(40).fill(7), 9])), null); // no spread, no score
  assert.equal(outlierZ(series([1, 2, 3])), null);
});

test("dew point matches a known value and rejects bad input", () => {
  assert.ok(Math.abs(dewPoint(25, 60) - 16.7) < 0.3);
  assert.ok(Math.abs(dewPoint(20, 100) - 20) < 0.01);
  assert.equal(dewPoint(20, 0), null);
  assert.equal(dewPoint(NaN, 50), null);
});

test("derived values and their warning levels", () => {
  const rising = Array.from({ length: 121 }, (_, i) => 60 + (i / 120) * 20); // +20 %RH over the hour
  const d = Object.fromEntries(
    derived({
      air_temp: series([22]), humidity: series([60]),
      core_temp: series(Array(121).fill(22), 30), core_humidity: series(rising, 30),
      temp: series([21]), pressure: series([1000, 1000.2], 1800),
    }).map((x) => [x.key, x]),
  );
  assert.equal(d.hull_rh_rate.level, "crit");
  assert.ok(Math.abs(d.hull_rh_rate.value - 20) < 0.01);
  assert.equal(d.condensation.level, "warn"); // water 21 vs hull dew point of about 18
  assert.ok(Math.abs(d.air_water.value - 1) < 1e-9);
  assert.equal(d.pressure_tendency.note, "steady");
  assert.equal(derived({}).length, 0);
});

test("analyse flags a stuck probe and a dropout", () => {
  const flat = analyse(series(Array(30).fill(0)));
  assert.deepEqual(flat.flags.map((f) => [f.kind, f.text]), [["stuck", "flat for 3 min"]]);
  // averaged points: the same number of identical buckets means a much longer flat stretch
  assert.equal(analyse(series(Array(30).fill(0), 300), 300).flags[0].text, "flat for 150 min");
  assert.equal(analyse(series(Array(8).fill(0)))?.flags.length, 0); // too few readings to call it flat
  const gappy = analyse(series(Array.from({ length: 40 }, (_, i) => i)).filter((_, i) => i % 2 === 0));
  assert.ok(gappy.flags.some((f) => f.kind === "gaps"));
  assert.equal(analyse(series([null])), null);
});

test("smoothing removes a single-reading spike but follows a real step", () => {
  const spiky = smooth(series([10, 10, 10, 10, 50, 10, 10, 10, 10]), 5);
  assert.ok(spiky.every((p) => p.value === 10));
  const step = smooth(series([1, 1, 1, 1, 1, 9, 9, 9, 9, 9]), 5);
  assert.equal(step.at(-1).value, 9);
  assert.equal(step.length, 10);
  assert.deepEqual(smooth(series([null, 4])).map((p) => p.value), [4]);
});

test("pressure tendency uses the 3 h series when it is long enough", () => {
  const p3 = series(Array.from({ length: 181 }, (_, i) => 1000 + (i / 180) * 3), 60); // +3 hPa over 3 h
  const long = derived({ pressure: series([1000, 1000.1], 1800), pressure_3h: p3 }).find((x) => x.key === "pressure_tendency");
  assert.equal(long.label, "Pressure tendency (3 h)");
  assert.ok(Math.abs(long.value - 1) < 0.01);
  assert.equal(long.note, "rising");
  const short = derived({ pressure: series([1000, 1000.1], 1800), pressure_3h: series([1000, 1000.05], 600) })
    .find((x) => x.key === "pressure_tendency");
  assert.match(short.label, /30 min/); // fell back to the hour series
});
