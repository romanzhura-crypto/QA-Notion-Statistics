// jsdom tests for status-dwell.html board #208 (A7, честный fallback открытого
// сегмента) + board #213 (A9, медиана):
// - no-history fallback (task not Done) = OPEN interval from Start date/created
//   to now, marked explicitly (data-open + hatch + «открыт» + «fallback») —
//   the end «сейчас» is never substituted silently
// - aggregates including open intervals are flagged «≈ … (по сейчас)»:
//   «Сумма до Done», row totals, KPI Медиана/Максимум
// - median KPI uses the standard formula (mean of two central values at even n)
// Run: NODE_PATH=/tmp/sdtest/node_modules node tests/status-dwell.open-fallback.test.js
"use strict";
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

// layout-agnostic: workspace tree keeps HTML in widgets/, repo tree in frontend/
const HTML_PATH = ["widgets", "frontend"]
  .map(d => path.join(__dirname, "..", d, "status-dwell.html"))
  .find(p => fs.existsSync(p));
const HTML = fs.readFileSync(HTML_PATH, "utf8");

const BASE = {
  ok: true,
  generated_at: "2026-09-30T08:00:00.000Z",
  generated_at_local: "2026-09-30T11:00:00+03:00",
  enriched_at: "2026-09-30T08:10:00.000Z",
  timezone: "Europe/Minsk",
  source: "test",
  data_source_id: "3dee17b6-8482-80a3-9fc4-000bafe19b46",
  releases: ["40.2026"],
  status_meta: [
    { name: "Testing", color: "green" },
    { name: "Development", color: "blue" },
    { name: "Done", color: "purple" }
  ]
};

// Block A: two not-Done tasks WITHOUT history → fallback open intervals.
const fallbackFixture = Object.assign({}, BASE, {
  task_count: 2,
  tasks: [
    {
      id: "3c8e17b6-8482-8166-0000-0000000000a1",
      tid: "TASK-1",
      u: "https://app.notion.com/p/x1",
      r: ["40.2026"],
      p: "parts",
      d: "QA",
      s: "Testing",
      n: "Fallback from created",
      created: "2026-09-20T07:00:00.000Z",
      start: null
    },
    {
      id: "3c8e17b6-8482-8166-0000-0000000000a2",
      tid: "TASK-2",
      u: "https://app.notion.com/p/x2",
      r: ["40.2026"],
      p: "parts",
      d: "QA",
      s: "Testing",
      n: "Fallback from Start date",
      created: "2026-09-25T08:00:00.000Z",
      start: "2026-09-25T09:00:00.000Z"
    }
  ]
});

// Block B: four tasks with closed history, dwell totals 1, 2, 3, 10 days.
const SEG = (s, days, from, to) => ({ s, days, from, to });
const histTask = (id, tid, n, days) => ({
  id: id,
  tid: tid,
  u: "https://app.notion.com/p/" + tid,
  r: ["40.2026"],
  p: "parts",
  d: "QA",
  s: "Development",
  n: n,
  created: "2026-09-01T07:00:00.000Z",
  history: [
    SEG("Development", days, "2026-09-01T07:00:00.000Z", "2026-09-01T07:00:00.000Z")
  ]
});
const medianFixture = Object.assign({}, BASE, {
  task_count: 4,
  tasks: [
    histTask("3c8e17b6-8482-8166-0000-0000000000b1", "TASK-1", "One day", 1),
    histTask("3c8e17b6-8482-8166-0000-0000000000b2", "TASK-2", "Two days", 2),
    histTask("3c8e17b6-8482-8166-0000-0000000000b3", "TASK-3", "Three days", 3),
    histTask("3c8e17b6-8482-8166-0000-0000000000b4", "TASK-4", "Ten days", 10)
  ]
});

async function withDom(payload, fn) {
  const dom = new JSDOM(HTML, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: "https://romanzhura-crypto.github.io/QA-Notion-Statistics/status-dwell.html",
    beforeParse(window) {
      window.fetch = async () => ({
        ok: true,
        status: 200,
        json: async () => JSON.parse(JSON.stringify(payload))
      });
      window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {} }));
    }
  });
  await new Promise(r => setTimeout(r, 300));
  try {
    await fn(dom.window);
  } finally {
    dom.window.close();
  }
}

async function main() {
  let pass = 0, fail = 0;
  const ok = (cond, msg) => {
    if (cond) { pass++; console.log("ok - " + msg); }
    else { fail++; console.log("FAIL - " + msg); }
  };

  await withDom(fallbackFixture, (window) => {
    const doc = window.document;
    const svg = doc.getElementById("chart");
    const rects = Array.from(svg.querySelectorAll("rect.seg"));
    const hatches = Array.from(svg.querySelectorAll("rect.open-hatch"));

    ok(rects.length === 2, "2 fallback dwell rects, got " + rects.length);
    ok(hatches.length === 2, "both fallback segments get a hatch overlay, got " + hatches.length);
    ok(rects.every(r => r.getAttribute("data-open") === "1"),
      "every fallback segment marked data-open=\\\"1\\\"");

    const tips = rects.map(r => r.getAttribute("data-tip") || "");
    const tipCreated = tips.find(t => t.indexOf("создания") !== -1) || "";
    const tipStart = tips.find(t => t.indexOf("Start date") !== -1) || "";
    ok(tipCreated.indexOf("≈ от создания") !== -1,
      "fallback without start: «≈ от создания …» in tooltip: " + tipCreated.split("\n")[1]);
    ok(tipStart.indexOf("≈ от Start date") !== -1,
      "fallback with start: «≈ от Start date …» in tooltip: " + tipStart.split("\n")[1]);
    ok(tipCreated.indexOf("fallback") !== -1, "fallback basis named in tooltip");
    ok(tipCreated.indexOf("(открыт, по сейчас)") !== -1, "open marker «(открыт, по сейчас)» in tooltip");
    ok(/→ сейчас \(открыт: fallback/.test(tipCreated), "range line ends at «сейчас» and says открыт");

    // honest aggregates: row totals + «Сумма до Done» + KPIs
    const labels = Array.from(svg.querySelectorAll("text")).map(t => t.textContent);
    const totalLabels = labels.filter(t => t.indexOf("(по сейчас)") !== -1);
    ok(totalLabels.length === 2, "row totals marked «(по сейчас)», got " + totalLabels.length + ": " + JSON.stringify(totalLabels));
    ok(totalLabels.every(t => t.indexOf("≈") === 0), "row totals prefixed with «≈»");

    const sumCells = Array.from(doc.querySelectorAll("#tbl tr td:nth-child(5)")).map(td => td.textContent);
    ok(sumCells.length === 2 && sumCells.every(s => s.indexOf("≈ ") === 0 && s.indexOf("(по сейчас)") !== -1),
      "«Сумма до Done» of open tasks = «≈ … (по сейчас)»: " + JSON.stringify(sumCells));

    const km = doc.getElementById("k-m").textContent;
    const kx = doc.getElementById("k-x").textContent;
    ok(km.indexOf("≈ ") === 0, "KPI Медиана flagged «≈» when open intervals present: " + km);
    ok(kx.indexOf("≈ ") === 0, "KPI Максимум flagged «≈» when open intervals present: " + kx);
    ok(doc.getElementById("k-m").title.indexOf("по сейчас") !== -1,
      "KPI title explains the «≈» (по сейчас)");
  });

  await withDom(medianFixture, (window) => {
    const doc = window.document;
    // standard median: totals [1, 2, 3, 10] → (2 + 3) / 2 = 2.5 (old lower-median gave 2)
    ok(doc.getElementById("k-m").textContent === "2.5 дн",
      "median = mean of two central values at even n: " + doc.getElementById("k-m").textContent);
    ok(doc.getElementById("k-x").textContent === "10 дн",
      "max rendered with unified rounding: " + doc.getElementById("k-x").textContent);
    ok(doc.getElementById("k-m").textContent.indexOf("≈") === -1,
      "no «≈» flag when all intervals are closed");
  });

  console.log(JSON.stringify({ pass, fail }));
  process.exit(fail ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
