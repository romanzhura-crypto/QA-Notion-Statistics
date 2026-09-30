// jsdom tests for release-charts.html board #213 (A9: медиана dwell в агрегатах,
// единое округление) + board #208 (A7: открытые интервалы помечаются «≈»):
// - KPI «Медиана dwell» = standard median over tasks WITH history (tasks without
//   history are excluded, not counted as zero; mean of two central values at
//   even n)
// - display rounding policy: minutes whole, hours/days 0.1 (fmtDur)
// - an open interval (from + to:null) in the selection flags the KPI «≈ …»
// Run: NODE_PATH=/tmp/sdtest/node_modules node tests/release-charts.median.test.js
"use strict";
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

// layout-agnostic: workspace tree keeps HTML in widgets/, repo tree in frontend/
const HTML_PATH = ["widgets", "frontend"]
  .map(d => path.join(__dirname, "..", d, "release-charts.html"))
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
  releases: ["40.2026"]
};

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
  e: 4,
  t: 3.5,
  created: "2026-09-01T07:00:00.000Z",
  history: [
    SEG("Development", days, "2026-09-01T07:00:00.000Z", "2026-09-01T07:00:00.000Z")
  ]
});
const noHistTask = {
  id: "3c8e17b6-8482-8166-0000-0000000000c9",
  tid: "TASK-9",
  u: "https://app.notion.com/p/TASK-9",
  r: ["40.2026"],
  p: "parts",
  d: "QA",
  s: "Development",
  n: "No history task",
  e: 2,
  t: 1,
  created: "2026-09-01T07:00:00.000Z"
};

// totals [1, 2, 3, 10] + one task without history (excluded from the median)
const medianFixture = Object.assign({}, BASE, {
  task_count: 5,
  tasks: [
    histTask("3c8e17b6-8482-8166-0000-0000000000c1", "TASK-1", "One day", 1),
    histTask("3c8e17b6-8482-8166-0000-0000000000c2", "TASK-2", "Two days", 2),
    histTask("3c8e17b6-8482-8166-0000-0000000000c3", "TASK-3", "Three days", 3),
    histTask("3c8e17b6-8482-8166-0000-0000000000c4", "TASK-4", "Ten days", 10),
    noHistTask
  ]
});

// one task with an OPEN interval (from + to:null) → KPI flagged «≈ … (по сейчас)»
const openFixture = Object.assign({}, BASE, {
  task_count: 1,
  tasks: [{
    id: "3c8e17b6-8482-8166-0000-0000000000d1",
    tid: "TASK-1",
    u: "https://app.notion.com/p/TASK-1",
    r: ["40.2026"],
    p: "parts",
    d: "QA",
    s: "Testing",
    n: "Open interval task",
    e: 1,
    t: 1,
    created: "2026-09-25T07:00:00.000Z",
    history: [
      SEG("Testing", 5 / 1440, "2026-09-30T07:00:00.000Z", null)
    ]
  }]
});

// single half-day dwell → unified rounding renders «12 ч»
const hoursFixture = Object.assign({}, BASE, {
  task_count: 1,
  tasks: [histTask("3c8e17b6-8482-8166-0000-0000000000e1", "TASK-1", "Half day", 0.5)]
});

async function withDom(payload, fn) {
  const dom = new JSDOM(HTML, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: "https://romanzhura-crypto.github.io/QA-Notion-Statistics/release-charts.html",
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

  await withDom(medianFixture, (window) => {
    const doc = window.document;
    const kmd = doc.getElementById("k-md");
    ok(!!kmd, "KPI «Медиана dwell» element #k-md exists");
    // standard median over tasks WITH history: (2 + 3) / 2 = 2.5; the task
    // without history is excluded (counting it as 0 would yield «2 дн»)
    ok(kmd.textContent === "2.5 дн",
      "median dwell = mean of two central values, no-history task excluded: " + kmd.textContent);
    ok(kmd.textContent.indexOf("≈") === -1, "no «≈» when all intervals are closed");
    ok(kmd.title.indexOf("Медиана") !== -1 && kmd.title.indexOf("истори") !== -1,
      "KPI title names the methodology");
  });

  await withDom(openFixture, (window) => {
    const kmd = window.document.getElementById("k-md");
    ok(kmd.textContent.indexOf("≈ ") === 0, "open interval flags the KPI «≈»: " + kmd.textContent);
    ok(kmd.textContent.indexOf("5 мин") !== -1, "micro dwell rendered as minutes: " + kmd.textContent);
    ok(kmd.title.indexOf("по сейчас") !== -1, "KPI title explains «по сейчас»");
  });

  await withDom(hoursFixture, (window) => {
    const kmd = window.document.getElementById("k-md");
    ok(kmd.textContent === "12 ч", "unified rounding: 0.5 day renders as «12 ч»: " + kmd.textContent);
  });

  console.log(JSON.stringify({ pass, fail }));
  process.exit(fail ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
