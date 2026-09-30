// jsdom tests for status-dwell.html board #165/#167/#218/#208/#213:
// - each interval rendered as its own segment (repeats never merged)
// - sub-hour intervals stay visible WITHOUT min-width inflation (A9 dot marker)
// - open interval (to=null) is explicitly marked: data-open + hatch + «открыт»
// - hover tooltip: total + per-interval info (ДД.MM.ГГГГ ЧЧ:MM), all same-status
//   segments highlighted together
// - caption "Источник — таблица задач…" removed
// - board #218 freshness hint: «Данные от …» (generated_at, Europe/Minsk) and
//   «История от …» (enriched_at), ДД.MM.ГГГГ ЧЧ:MM; legacy payload without
//   enriched_at shows «История от —»
// Run: NODE_PATH=/tmp/sdtest/node_modules node tests/status-dwell.segments.test.js
"use strict";
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

// layout-agnostic: workspace tree keeps HTML in widgets/, repo tree in frontend/
const HTML_PATH = ["widgets", "frontend"]
  .map(d => path.join(__dirname, "..", d, "status-dwell.html"))
  .find(p => fs.existsSync(p));
const HTML = fs.readFileSync(HTML_PATH, "utf8");

const SEG = (s, days, from, to) => ({ s, days, from, to });
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
  // SVG invariant: one rect.seg per dwell interval + rect.open-hatch overlays
  // (decorative, pointer-events none) + polygon per Done diamond.
  const rects = Array.from(svg.querySelectorAll("rect.seg"));
  const hatches = Array.from(svg.querySelectorAll("rect.open-hatch"));
  const dots = Array.from(svg.querySelectorAll("circle.micro-dot"));
  const polys = Array.from(svg.querySelectorAll("polygon"));

  ok(rects.length === 6, "6 dwell rects (5 intervals + 1 legacy remainder), got " + rects.length);
  ok(polys.length === 1, "1 Done diamond, got " + polys.length);

  // A9 (board #213): no min-width inflation — widths stay strictly proportional;
  // the 5-minute interval keeps a visible dot marker instead of a fake 4px bar.
  const widths = rects.map(r => parseFloat(r.getAttribute("width")));
  ok(widths.every(w => w >= 0), "segment widths present: " + JSON.stringify(widths));
  const fiveMinRect = rects[1];
  const fiveW = parseFloat(fiveMinRect.getAttribute("width"));
  ok(fiveW > 0 && fiveW < 2, "5-minute interval NOT inflated (proportional width < 2px): " + fiveW);
  ok(widths[4] > 100 && widths[4] > widths[0], "0.5-day remainder dominates proportionally: " + JSON.stringify(widths));
  ok(dots.length === 1, "micro segment gets a dot marker, got " + dots.length);
  ok((fiveMinRect.getAttribute("data-tip") || "").indexOf("(< 1 ч)") !== -1,
    "5-minute interval tooltip flags «< 1 ч»");
  ok((dots[0].getAttribute("data-tip") || "").indexOf("(< 1 ч)") !== -1,
    "micro dot tooltip flags «< 1 ч»");

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
  // A7 (board #208): open interval is explicitly marked, end=«now» never silent
  ok(rects[5].getAttribute("data-open") === "1", "open interval rect marked data-open=\"1\"");
  ok(hatches.length === 1, "open interval gets a hatch overlay, got " + hatches.length);
  ok(tipOpen.indexOf("(открыт, по сейчас)") !== -1, "open tooltip duration marked «открыт, по сейчас»");
  ok(/→ сейчас \(открыт\)/.test(tipOpen), "open interval range says «→ сейчас (открыт)»");
  const legendHtml = doc.getElementById("legend").innerHTML;
  ok(legendHtml.indexOf("открытый интервал (по сейчас)") !== -1, "legend explains the hatch marker");

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

  // board #218: freshness hint «Данные от …» / «История от …»
  const syncLine = doc.getElementById("sync-time").textContent;
  ok(/Данные от \d{2}\.\d{2}\.\d{4} \d{2}:\d{2}/.test(syncLine),
    "hint «Данные от ДД.MM.ГГГГ ЧЧ:MM»: " + JSON.stringify(syncLine));
  ok(/История от \d{2}\.\d{2}\.\d{4} \d{2}:\d{2}/.test(syncLine),
    "hint «История от ДД.MM.ГГГГ ЧЧ:MM»: " + JSON.stringify(syncLine));
  ok(syncLine.indexOf("Данные от 25.09.2026 12:00") !== -1,
    "generated_at rendered in Europe/Minsk (09:00Z → 12:00): " + JSON.stringify(syncLine));
  ok(syncLine.indexOf("История от 25.09.2026 12:30") !== -1,
    "enriched_at rendered in Europe/Minsk (09:30Z → 12:30): " + JSON.stringify(syncLine));
  ok(HTML.indexOf("Последняя синхронизация") === -1, "old label «Последняя синхронизация» removed");

  // board #218: legacy payload without enriched_at/generated_at_local → «История от —»
  {
    const legacy = Object.assign({}, fixture);
    delete legacy.enriched_at;
    delete legacy.generated_at_local;
    const dom2 = new JSDOM(HTML, {
      runScripts: "dangerously",
      pretendToBeVisual: true,
      url: "https://romanzhura-crypto.github.io/QA-Notion-Statistics/status-dwell.html",
      beforeParse(w2) {
        w2.fetch = async () => ({
          ok: true,
          status: 200,
          json: async () => JSON.parse(JSON.stringify(legacy))
        });
        w2.matchMedia = w2.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {} }));
      }
    });
    await new Promise(r => setTimeout(r, 300));
    const line2 = dom2.window.document.getElementById("sync-time").textContent;
    ok(line2.indexOf("Данные от 25.09.2026 12:00") !== -1,
      "legacy payload: hint still shows generated_at: " + JSON.stringify(line2));
    ok(line2.indexOf("История от —") !== -1,
      "legacy payload without enriched_at shows «История от —»: " + JSON.stringify(line2));
    dom2.window.close();
  }

  console.log(JSON.stringify({ pass, fail }));
  window.close();
  process.exit(fail ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
