// jsdom tests for status-dwell.html board #165/#167:
// - each interval rendered as its own segment (repeats never merged)
// - sub-hour intervals stay visible (minute precision)
// - hover tooltip: total + per-interval info (ДД.MM.ГГГГ ЧЧ:MM), all same-status
//   segments highlighted together
// - caption "Источник — таблица задач…" removed
// Run: NODE_PATH=/tmp/sdtest/node_modules node tests/status-dwell.segments.test.js
"use strict";
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const HTML = fs.readFileSync(path.join(__dirname, "..", "widgets", "status-dwell.html"), "utf8");

const SEG = (s, days, from, to) => ({ s, days, from, to });
const fixture = {
  ok: true,
  generated_at: "2026-09-25T09:00:00.000Z",
  source: "test",
  data_source_id: "3dee17b6-8482-80a3-9fc4-000bafe19b46",
  task_count: 1,
  releases: ["40.2026"],
  status_meta: [
    { name: "Ready For Dev", color: "gray" },
    { name: "Development", color: "blue" },
    { name: "Ready For QA", color: "yellow" },
    { name: "Testing", color: "green" },
    { name: "Done", color: "purple" }
  ],
  tasks: [
    {
      id: "3c8e17b6-8482-8166-0000-0000000000aa",
      tid: "TASK-1",
      u: "https://app.notion.com/p/x",
      r: ["40.2026"],
      p: "parts",
      d: "QA",
      s: "Testing",
      n: "Repeat segments test task",
      created: "2026-09-25T07:00:00.000Z",
      edited: "2026-09-25T12:00:00.000Z",
      history: [
        SEG("Ready For Dev", 1 / 24, "2026-09-25T07:00:00.000Z", "2026-09-25T08:00:00.000Z"),
        SEG("Development", 5 / 1440, "2026-09-25T08:00:00.000Z", "2026-09-25T08:05:00.000Z"),
        SEG("Ready For QA", 1 / 24, "2026-09-25T08:05:00.000Z", "2026-09-25T09:05:00.000Z"),
        SEG("Development", 1 / 24, "2026-09-25T09:05:00.000Z", "2026-09-25T10:05:00.000Z"),
        { s: "Development", days: 0.5 }, // legacy remainder, no timestamps
        SEG("Testing", 2 / 24, "2026-09-25T10:05:00.000Z", null), // open interval
        { s: "Done", at: "2026-09-25T12:00:00.000Z" }
      ]
    }
  ]
};


async function main() {
  const dom = new JSDOM(HTML, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: "https://romanzhura-crypto.github.io/QA-Notion-Statistics/status-dwell.html",
    beforeParse(window) {
      window.fetch = async () => ({
        ok: true,
        status: 200,
        json: async () => JSON.parse(JSON.stringify(fixture))
      });
      window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {} }));
    }
  });
  const { window } = dom;
  await new Promise(r => setTimeout(r, 300));
  const doc = window.document;
  let pass = 0, fail = 0;
  const ok = (cond, msg) => {
    if (cond) { pass++; console.log("ok - " + msg); }
    else { fail++; console.log("FAIL - " + msg); }
  };

  const svg = doc.getElementById("chart");
  const rects = Array.from(svg.querySelectorAll("rect"));
  const polys = Array.from(svg.querySelectorAll("polygon"));

  ok(rects.length === 6, "6 dwell rects (5 intervals + 1 legacy remainder), got " + rects.length);
  ok(polys.length === 1, "1 Done diamond, got " + polys.length);

  const widths = rects.map(r => parseFloat(r.getAttribute("width")));
  ok(widths.every(w => w >= 4), "every segment visible (width >= 4px): " + JSON.stringify(widths));
  const fiveMinRect = rects[1];
  ok(parseFloat(fiveMinRect.getAttribute("width")) >= 4, "5-minute interval keeps a visible bar");

  const groups = rects.map(r => r.getAttribute("data-group"));
  const devGroups = [groups[1], groups[3], groups[4]];
  ok(!!devGroups[0] && devGroups[0] === devGroups[1] && devGroups[1] === devGroups[2],
    "3 Development segments share one highlight group: " + JSON.stringify(devGroups));
  ok(!groups[0], "single-status segment has no group (nothing extra to highlight)");

  const tip2 = rects[1].getAttribute("data-tip") || "";
  ok(tip2.indexOf("Development · участков: 3") !== -1, "tooltip has aggregate info: " + tip2.split("\n")[0]);
  ok(tip2.indexOf("всего") !== -1, "tooltip has total duration");
  ok(tip2.indexOf("1)") !== -1 && tip2.indexOf("2)") !== -1 && tip2.indexOf("3)") !== -1,
    "tooltip has per-interval lines 1..3");
  ok(/получен \d{2}\.\d{2}\.\d{4} \d{2}:\d{2}/.test(tip2), "tooltip has obtained-at ДД.MM.ГГГГ ЧЧ:MM");
  ok(tip2.indexOf("неизвестно") !== -1, "tooltip marks legacy interval without timestamps");
  ok(/5 мин/.test(tip2), "tooltip shows minute duration for the 5-minute interval");

  const tipOpen = rects[5].getAttribute("data-tip") || "";
  ok(tipOpen.indexOf("сейчас") !== -1, "open interval tooltip says 'сейчас': " + tipOpen.split("\n")[1]);

  const tipDev1 = rects[3].getAttribute("data-tip") || "";
  ok(tipDev1.indexOf("→ ушёл") !== -1, "closed interval shows both boundaries");

  ok(HTML.indexOf("Источник — таблица задач") === -1, "caption «Источник — таблица задач…» removed");
  ok(HTML.indexOf("Полоса = текущий Status") === -1, "caption «Полоса = текущий Status…» removed");

  // hover: all same-status segments highlight together
  const ev = new window.MouseEvent("mouseenter", { bubbles: false });
  rects[1].dispatchEvent(ev);
  const hl = Array.from(svg.querySelectorAll("rect.hl"));
  ok(hl.length === 3, "mouseenter highlights all 3 Development segments, got " + hl.length);
  rects[1].dispatchEvent(new window.MouseEvent("mouseleave", { bubbles: false }));
  ok(svg.querySelectorAll("rect.hl").length === 0, "mouseleave clears highlight");

  console.log(JSON.stringify({ pass, fail }));
  window.close();
  process.exit(fail ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
