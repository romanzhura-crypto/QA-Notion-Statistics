// jsdom tests for release-charts.html board #218 (UI hint of data freshness):
// - «Данные от …» rendered from generated_at in Europe/Minsk (== generated_at_local)
// - «История от …» rendered from enriched_at (LOG-history merge moment)
// - format ДД.MM.ГГГГ ЧЧ:MM, как в остальном UI; legacy payload → «История от —»
// Run: NODE_PATH=/tmp/sdtest/node_modules node tests/release-charts.freshness.test.js
"use strict";
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

// layout-agnostic: workspace tree keeps HTML in widgets/, repo tree in frontend/
const HTML_PATH = ["widgets", "frontend"]
  .map(d => path.join(__dirname, "..", d, "release-charts.html"))
  .find(p => fs.existsSync(p));
const HTML = fs.readFileSync(HTML_PATH, "utf8");

const fixture = {
  ok: true,
  generated_at: "2026-09-25T09:00:00.000Z",
  generated_at_local: "2026-09-25T12:00:00+03:00",
  enriched_at: "2026-09-25T09:30:00.000Z",
  timezone: "Europe/Minsk",
  source: "test",
  data_source_id: "3dee17b6-8482-80a3-9fc4-000bafe19b46",
  task_count: 1,
  releases: ["40.2026"],
  tasks: [
    {
      id: "3c8e17b6-8482-8166-0000-0000000000bb",
      tid: "TASK-1",
      u: "https://app.notion.com/p/x",
      r: ["40.2026"],
      p: "parts",
      d: "QA",
      s: "Done",
      n: "Freshness test task",
      e: 4,
      t: 3.5
    }
  ]
};

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

  await withDom(fixture, (window) => {
    const doc = window.document;
    const el = doc.getElementById("sync-time");
    ok(!!el, "freshness hint element #sync-time exists");
    const line = el ? el.textContent : "";
    ok(/Данные от \d{2}\.\d{2}\.\d{4} \d{2}:\d{2}/.test(line),
      "hint «Данные от ДД.MM.ГГГГ ЧЧ:MM»: " + JSON.stringify(line));
    ok(/История от \d{2}\.\d{2}\.\d{4} \d{2}:\d{2}/.test(line),
      "hint «История от ДД.MM.ГГГГ ЧЧ:MM»: " + JSON.stringify(line));
    ok(line.indexOf("Данные от 25.09.2026 12:00") !== -1,
      "generated_at rendered in Europe/Minsk (09:00Z → 12:00): " + JSON.stringify(line));
    ok(line.indexOf("История от 25.09.2026 12:30") !== -1,
      "enriched_at rendered in Europe/Minsk (09:30Z → 12:30): " + JSON.stringify(line));
    ok(doc.getElementById("k-n").textContent === "1", "widget rendered with fixture (k-n = 1)");
  });

  await withDom(Object.assign({}, fixture, { enriched_at: undefined, generated_at_local: undefined }), (window) => {
    const line = window.document.getElementById("sync-time").textContent;
    ok(line.indexOf("Данные от 25.09.2026 12:00") !== -1,
      "legacy payload: hint still shows generated_at: " + JSON.stringify(line));
    ok(line.indexOf("История от —") !== -1,
      "legacy payload without enriched_at shows «История от —»: " + JSON.stringify(line));
  });

  console.log(JSON.stringify({ pass, fail }));
  process.exit(fail ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
