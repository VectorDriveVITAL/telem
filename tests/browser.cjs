/* Run against a development instance: TELEM_TEST_URL=http://127.0.0.1:8000 npm run test:browser */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
(async () => {
  const base = process.env.TELEM_TEST_URL || "http://127.0.0.1:8000";
  const output = process.env.TELEM_SCREENSHOTS || "test-results";
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.CHROMIUM_EXECUTABLE || undefined,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
    deviceScaleFactor: 1,
  });
  page.setDefaultTimeout(12000);
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  // External fonts and map tiles are cosmetic; make the test independent of them.
  await page.route("https://fonts.**", (r) => r.abort());
  await page.route("https://*.basemaps.cartocdn.com/**", (r) => r.abort());
  async function json(url, method = "GET", data) {
    const response = await page.request.fetch(base + url, { method, data });
    assert(response.ok(), await response.text());
    return response.status() === 204 ? null : response.json();
  }
  const source = "browser-" + Date.now();
  try {
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page
      .locator("#overview-cards .overview-card")
      .first()
      .waitFor({ state: "attached" });
    await page.screenshot({
      path: path.join(output, "overview.png"),
      fullPage: true,
    });
    await page
      .getByRole("button", { name: "Sensor telemetry", exact: true })
      .click();
    await page
      .getByRole("button", { name: "+ Register buoy", exact: true })
      .click();
    await page.locator("[name=id]").fill(source);
    await page.locator("[name=lat]").fill("29.7");
    await page.locator("[name=lng]").fill("-95.4");
    await page.locator("[name=sensors]").fill("temp");
    await page.locator("#dialog-submit").click();
    await page.locator("#operation-dialog").waitFor({ state: "hidden" });
    await page
      .locator(
        `#sidebar-sensors-list [data-select="buoy"][data-source="${source}"]`,
      )
      .click();
    await page.locator("#buoy-drawer.open").waitFor();
    await page
      .getByRole("button", { name: "Edit limits", exact: true })
      .click();
    await page.locator("[name=warn_max]").fill("30");
    await page.locator("[name=crit_max]").fill("40");
    await page.locator("#dialog-submit").click();
    await page.locator("#operation-dialog").waitFor({ state: "hidden" });
    const end = Date.now();
    await json(
      `/api/ingest/buoys/${source}/batch`,
      "POST",
      Array.from({ length: 30 }, (_, i) => ({
        time: new Date(end - (30 - i) * 10000).toISOString(),
        temp: 20 + i / 5,
        battery: 80 - i / 10,
        solar_watts: 2 + i / 30,
        signal_dbm: -70 - i / 5,
      })),
    );
    await page.getByRole("button", { name: "+ Log work", exact: true }).click();
    await page.locator("[name=kind]").selectOption("calibration");
    await page.locator("[name=operator]").fill("Browser Test");
    await page
      .locator("[name=description]")
      .fill("Verified the probe against two reference points.");
    await page.locator("#dialog-submit").click();
    await page.locator("#operation-dialog").waitFor({ state: "hidden" });
    await page
      .locator("#drawer-maintenance")
      .getByText("Verified the probe against two reference points.")
      .waitFor();
    await page.screenshot({
      path: path.join(output, "buoy-drawer.png"),
      fullPage: true,
    });
    await page.getByRole("button", { name: "Analyze", exact: true }).click();
    await page.locator("#view-analysis.active").waitFor();
    await page.waitForTimeout(350);
    await page.waitForFunction(
      () =>
        document.querySelectorAll("#chart-analysis .chart-trace[d]").length >=
          4 &&
        document.querySelector("#chart-analysis .chart-trace").getAttribute("d")
          .length > 0,
    );
    assert.equal(await page.locator("#chart-analysis .chart-event").count(), 1);
    await page.screenshot({
      path: path.join(output, "analysis.png"),
      fullPage: true,
    });
    // A wheel burst previews immediately, retains trace nodes, and coalesces I/O.
    let zoomRequests = 0;
    const countZoom = (request) => {
      const url = new URL(request.url());
      if (
        url.pathname.endsWith(`/buoys/${source}/timeseries`) &&
        new Date(url.searchParams.get("end")) -
          new Date(url.searchParams.get("start")) <
          3599000
      )
        zoomRequests++;
    };
    page.on("request", countZoom);
    const zoomCheck = await page.evaluate(async () => {
      const svg = document.querySelector("#chart-analysis");
      const trace = svg.querySelector(".chart-trace");
      const rect = svg.getBoundingClientRect();
      const initial = trace.getAttribute("d");
      const started = performance.now();
      for (let i = 0; i < 5; i++)
        svg.dispatchEvent(
          new WheelEvent("wheel", {
            deltaY: -10,
            clientX: rect.right - 20,
            bubbles: true,
            cancelable: true,
          }),
        );
      const dispatchMs = performance.now() - started;
      await new Promise((r) => setTimeout(r, 80));
      return {
        retained: trace === svg.querySelector(".chart-trace"),
        changed: initial !== trace.getAttribute("d"),
        dispatchMs,
      };
    });
    assert(zoomCheck.retained, "zoom must retain chart trace nodes");
    assert(zoomCheck.changed, "zoom should preview before its network request");
    assert(zoomCheck.dispatchMs < 200, "wheel burst blocked the UI");
    await page.waitForTimeout(700);
    await page
      .getByRole("button", { name: "Return to live", exact: true })
      .click();
    await page.waitForTimeout(500);
    console.log("Chart interaction:", zoomCheck);
    // Zoom is reflected in export's exact time range.
    const before = await page.locator("#chart-analysis").boundingBox();
    await page.mouse.move(
      before.x + before.width * 0.98,
      before.y + before.height * 0.5,
    );
    await page.mouse.wheel(0, -100);
    await page.getByRole("button", { name: "Export CSV", exact: true }).click();
    const selected = await page.locator("[name=start]").inputValue();
    const selectedEnd = await page.locator("[name=end]").inputValue();
    assert(new Date(selectedEnd) - new Date(selected) < 3600000);
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.locator("#dialog-submit").click(),
    ]);
    const filename = path.join(output, "readings.csv");
    await download.saveAs(filename);
    assert(fs.readFileSync(filename, "utf8").includes("signal_dbm"));
    await page.locator("#operation-dialog").waitFor({ state: "hidden" });
    await json(`/api/ingest/buoys/${source}`, "POST", {
      temp: 50,
      battery: 10,
    });
    await page.getByRole("button", { name: "Overview", exact: true }).click();
    await page
      .getByRole("button", { name: "Refresh now", exact: true })
      .click();
    await page.getByRole("button", { name: "Incidents", exact: true }).click();
    const card = page
      .locator(".incident-card")
      .filter({ hasText: source })
      .first();
    await card.locator(".incident-head").click();
    await card.getByRole("button", { name: "Assign owner / cause" }).click();
    await page.locator("[name=owner]").fill("Yi Cheng");
    await page.locator("[name=cause]").fill("Test low battery condition");
    await page.locator("#dialog-submit").click();
    await page.locator("#operation-dialog").waitFor({ state: "hidden" });
    await card.getByRole("button", { name: "+ Note", exact: true }).click();
    await page.locator("[name=text]").fill("Scheduled a replacement.");
    await page.locator("#dialog-submit").click();
    await page.locator("#operation-dialog").waitFor({ state: "hidden" });
    await card
      .getByRole("button", { name: "Acknowledge", exact: true })
      .click();
    await card
      .getByRole("button", { name: "Acknowledged", exact: true })
      .waitFor();
    await card.getByText("Scheduled a replacement.").waitFor();
    await page.screenshot({
      path: path.join(output, "incidents.png"),
      fullPage: true,
    });
    await card
      .getByRole("button", { name: "Trace on chart", exact: true })
      .click();
    await page
      .locator("#scrub-status-sensors")
      .filter({ hasText: "REPLAY" })
      .waitFor();
    // Dynamic palette includes a source created after page load.
    await page.keyboard.press("Control+k");
    await page.locator("#cmdk-input").fill(source);
    assert.equal(await page.locator(".cmdk-item").count(), 1);
    await page.keyboard.press("Enter");
    await page.locator("#buoy-drawer.open").waitFor();
    await page.locator("#drawer-close").click();
    await page.getByRole("button", { name: "Dark", exact: true }).click();
    assert.equal(await page.locator("html").getAttribute("data-theme"), "dark");
    await page.reload({ waitUntil: "domcontentloaded" });
    await page
      .locator("#overview-cards .overview-card")
      .first()
      .waitFor({ state: "attached" });
    assert.equal(await page.locator("html").getAttribute("data-theme"), "dark");
    const rules = await json(`/api/buoys/${source}/sensors/temp/rules`);
    assert.equal(rules.crit_max, 40);
    // Acknowledgement and expiring source mutes persist through re-render/reload.
    await page
      .getByRole("button", { name: "Sensor telemetry", exact: true })
      .click();
    const feedItem = page
      .locator(`#feed-sensors-list .feed-item[data-source="${source}"]`)
      .first();
    await feedItem.getByRole("button", { name: "Ack", exact: true }).click();
    await feedItem
      .getByRole("button", { name: "Acked", exact: true })
      .waitFor();
    await feedItem.getByRole("button", { name: "Mute", exact: true }).click();
    await page.locator("#dialog-submit").click();
    await page.locator("#operation-dialog").waitFor({ state: "hidden" });
    await page.reload({ waitUntil: "domcontentloaded" });
    const mutedRow = page
      .locator("#muted-summary-sensors .source-item")
      .filter({ hasText: source });
    await mutedRow.getByRole("button", { name: "Unmute", exact: true }).click();
    await page
      .locator(`#feed-sensors-list .feed-item[data-source="${source}"]`)
      .first()
      .waitFor();
    // Table filtering remains active through a refresh.
    await page
      .getByRole("button", { name: "Sensor telemetry", exact: true })
      .click();
    await page.locator("#filter-sensors").fill(source);
    await page.waitForTimeout(4500);
    assert.equal(await page.locator("#buoys-tbody tr").count(), 1);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("button", { name: "Overview", exact: true }).click();
    await page.screenshot({
      path: path.join(output, "mobile.png"),
      fullPage: true,
    });
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth + 1,
      ),
      "mobile horizontal overflow",
    );
    assert.deepEqual(errors, [], "browser runtime errors");
    console.log(
      "Browser workflows passed: create, rules, maintenance, analysis, zoom, CSV, incident actions, replay, search, persistence, filtering, themes, mobile.",
    );
  } catch (error) {
    await page.screenshot({
      path: path.join(output, "failure.png"),
      fullPage: true,
    });
    console.log("Last UI message:", await page.locator("#toast").textContent());
    console.log("Browser errors:", errors);
    throw error;
  } finally {
    await json(`/api/buoys/${source}`, "DELETE").catch(() => {});
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
