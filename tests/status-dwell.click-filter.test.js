// jsdom tests for status-dwell.html board #233 (row click → task filter + detail mode):
// - click on a table row sets the «Задача» filter (taskCombo) to that task and
//   re-renders ONE task: chart redrawn in detail mode (full-width timeline,
//   thick bar 40–64px, status name + duration labels on segments, time axis),
//   micro segments stay truthful (dot only), Done diamond preserved
// - keyboard: Enter/Space on the focused row does the same (tabindex="0")
// - the task title is an explicit Notion link (target="_blank" rel="noopener");
//   clicking it does NOT trigger the filter (stopPropagation)
// - exit detail mode: «×» (#task-clear) and Esc clear the filter and return to
//   the ordinary multi-row chart; a repeat click on the same row is idempotent
// - filter state stays synced with taskCombo (visible label, changeable via combo)
// - a11y invariants preserved: aria-sort on sortable th, role=combobox on inputs
// Run: NODE_PATH=/tmp/sdtest/node_modules node tests/status-dwell.click-filter.test.js
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
  generated_at: "2026-09-30T09:00:00.000Z",
  generated_at_local: "2026-09-30T12:00:00+03:00",
  enriched_at: "2026-09-30T09:30:00.000Z",
  timezone: "Europe/Minsk",
  source: "test",
  data_source_id: "3dee17b6-8482-80a3-9fc4-000bafe19b46",
  task_count: 2,
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
      id: "3c8e17b6-8482-8166-0000-0000000000a1",
      tid: "TASK-1",
      u: "https://app.notion.com/p/alpha",
      r: ["40.2026"],
      p: "parts",
      d: "QA",
      s: "Done",
      n: "Alpha detail",
      created: "2026-09-25T07:00:00.000Z",
      edited: "2026-09-28T09:00:00.000Z",
      history: [
        SEG("Ready For Dev", 1 / 24, "2026-09-25T07:00:00.000Z", "2026-09-25T08:00:00.000Z"),
        SEG("Development", 2, "2026-09-25T08:00:00.000Z", "2026-09-27T08:00:00.000Z"),
        SEG("Ready For QA", 5 / 1440, "2026-09-27T08:00:00.000Z", "2026-09-27T08:05:00.000Z"), // micro
        SEG("Testing", 1, "2026-09-27T08:05:00.000Z", "2026-09-28T08:05:00.000Z"),
        { s: "Done", at: "2026-09-28T09:00:00.000Z" }
      ]
    },
    {
      id: "3c8e17b6-8482-8166-0000-0000000000b2",
      tid: "TASK-2",
      u: "https://app.notion.com/p/beta",
      r: ["40.2026"],
      p: "parts",
      d: "DEV",
      s: "Testing",
      n: "Beta open",
      created: "2026-09-26T07:00:00.000Z",
      history: [
        SEG("Development", 1, "2026-09-26T07:00:00.000Z", "2026-09-27T07:00:00.000Z"),
        SEG("Testing", 1, "2026-09-27T07:00:00.000Z", null) // open interval
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
      // jsdom gap: combo highlight() scrolls options into view
      window.HTMLElement.prototype.scrollIntoView = function () {};
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
  const click = (el) => el.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true }));
  const key = (el, k) => el.dispatchEvent(new window.KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true }));

  const svg = doc.getElementById("chart");
  const taskInput = doc.getElementById("task-filter");
  const clearBtn = doc.getElementById("task-clear");

  // ---- initial state: ordinary multi-row mode -------------------------------
  let rowsEls = Array.from(doc.querySelectorAll("#tbl tr[data-task]"));
  ok(rowsEls.length === 2, "initial: 2 clickable rows, got " + rowsEls.length);
  ok(taskInput.value === "", "initial: task filter empty");
  ok(clearBtn.hidden, "initial: reset button hidden");
  ok(svg.querySelectorAll("rect.seg").length === 6, "initial: 6 dwell segments (4 + 2, Done has none), got " + svg.querySelectorAll("rect.seg").length);
  const heights0 = Array.from(svg.querySelectorAll("rect.seg")).map(r => parseFloat(r.getAttribute("height")));
  ok(heights0.every(hh => hh === 18), "initial: ordinary thin bars (18px): " + JSON.stringify(heights0));

  // ---- a11y invariants preserved -------------------------------------------
  const thSort = doc.querySelector('.tbl-tasks thead th[data-key="n"]');
  ok(thSort && thSort.getAttribute("aria-sort") === "none", "th aria-sort intact");
  ok(rowsEls[0].getAttribute("tabindex") === "0", "row keyboard-focusable (tabindex=0)");
  ok(rowsEls[0].getAttribute("role") === "button", "row has button role");
  ok(!!rowsEls[0].getAttribute("aria-label"), "row has aria-label");
  const comboInput = doc.getElementById("task-filter");
  ok(comboInput.getAttribute("role") === "combobox", "task combo role=combobox intact");

  // ---- Notion link: explicit anchor, click does NOT filter ------------------
  const a = rowsEls[0].querySelector("td .task-open");
  ok(!!a, "title rendered as link .task-open");
  ok(a.getAttribute("href") === "https://app.notion.com/p/alpha", "link href = task URL");
  ok(a.getAttribute("target") === "_blank" && /noopener/.test(a.getAttribute("rel") || ""), "link target=_blank rel=noopener");
  click(a);
  ok(taskInput.value === "", "link click does NOT set the task filter (stopPropagation)");
  ok(doc.querySelectorAll("#tbl tr[data-task]").length === 2, "link click: still 2 rows");

  // ---- row click → task filter + detail mode -------------------------------
  click(rowsEls[0].querySelectorAll("td")[1]); // click on a cell (bubbles to tr)
  ok(taskInput.value === "TASK-1 · Alpha detail", "row click sets «Задача» filter to row label: " + JSON.stringify(taskInput.value));
  ok(!clearBtn.hidden, "reset button appears when filter set");
  ok(doc.querySelectorAll("#tbl tr[data-task]").length === 1, "re-render = one task, got " + doc.querySelectorAll("#tbl tr[data-task]").length);

  let segs = Array.from(svg.querySelectorAll("rect.seg"));
  ok(segs.length === 4, "detail: all 4 segments of the task on the chart, got " + segs.length);
  const heights = segs.map(r => parseFloat(r.getAttribute("height")));
  ok(heights.every(hh => hh >= 40 && hh <= 64), "detail: thick timeline bar (40–64px): " + JSON.stringify(heights));
  const widths = segs.map(r => parseFloat(r.getAttribute("width")));
  const widthSum = widths.reduce((s2, ww) => s2 + ww, 0);
  ok(widthSum > 500, "detail: bar stretched across the full chart width (sum=" + widthSum.toFixed(1) + ")");
  // segment order + colors (labels on segments)
  const texts = Array.from(svg.querySelectorAll("text")).map(t => t.textContent);
  ok(texts.indexOf("Development") !== -1, "detail: status name label on wide segment");
  ok(texts.some(t => /^2 дн$/.test(t)), "detail: duration label on wide segment: " + JSON.stringify(texts));
  ok(texts.some(t => /^1 дн$/.test(t)), "detail: duration label on Testing segment");
  // micro segment stays truthful: dot only, no inflated bar / no label
  const dots = svg.querySelectorAll("circle.micro-dot");
  ok(dots.length === 1, "detail: micro segment (<2px) keeps the dot marker, got " + dots.length);
  ok(parseFloat(svg.querySelectorAll("rect.seg")[2].getAttribute("width")) < 2,
    "detail: micro segment NOT inflated: " + svg.querySelectorAll("rect.seg")[2].getAttribute("width"));
  // Done diamond preserved
  const polys = svg.querySelectorAll("polygon");
  ok(polys.length === 1, "detail: Done diamond preserved, got " + polys.length);
  ok((polys[0].getAttribute("data-tip") || "").indexOf("Done ·") !== -1, "Done diamond tooltip intact");
  // time axis with tick labels
  const tickTexts = Array.from(svg.querySelectorAll("text")).map(t => t.textContent);
  ok(tickTexts.indexOf("0 мин") !== -1, "detail: time axis has tick labels (0 мин): " + JSON.stringify(tickTexts.slice(-6)));
  // tooltips still bound (bindTips) on segments
  ok(segs.every(s2 => (s2.getAttribute("data-tip") || "").length > 0), "detail: tooltips kept on every segment");

  // ---- repeat click on the same row is idempotent --------------------------
  click(doc.querySelector("#tbl tr[data-task]"));
  ok(taskInput.value === "TASK-1 · Alpha detail", "repeat click: filter unchanged");
  ok(doc.querySelectorAll("#tbl tr[data-task]").length === 1, "repeat click: still one task");
  ok(svg.querySelectorAll("rect.seg").length === 4, "repeat click: chart intact");

  // ---- exit via «×» ---------------------------------------------------------
  click(clearBtn);
  ok(taskInput.value === "", "«×» clears the task filter");
  ok(clearBtn.hidden, "«×» hidden again after reset");
  ok(doc.querySelectorAll("#tbl tr[data-task]").length === 2, "«×»: back to the ordinary list (2 rows)");
  const heightsBack = Array.from(svg.querySelectorAll("rect.seg")).map(r => parseFloat(r.getAttribute("height")));
  ok(heightsBack.every(hh => hh === 18), "«×»: ordinary thin bars restored: " + JSON.stringify(heightsBack));

  // ---- keyboard: Enter on focused row ---------------------------------------
  const row2 = doc.querySelectorAll("#tbl tr[data-task]")[1];
  key(row2, "Enter");
  ok(taskInput.value === "TASK-2 · Beta open", "Enter on row sets the filter: " + JSON.stringify(taskInput.value));
  ok(doc.querySelectorAll("#tbl tr[data-task]").length === 1, "Enter: one task rendered");

  // ---- exit via Esc ---------------------------------------------------------
  key(doc, "Escape");
  ok(taskInput.value === "", "Esc clears the task filter (exit detail mode)");
  ok(doc.querySelectorAll("#tbl tr[data-task]").length === 2, "Esc: back to the ordinary list");

  // ---- keyboard: Space on focused row, then Esc through the combo -----------
  const row1 = doc.querySelectorAll("#tbl tr[data-task]")[0];
  key(row1, " ");
  ok(taskInput.value === "TASK-1 · Alpha detail", "Space on row sets the filter");
  key(doc, "Escape");
  ok(taskInput.value === "", "Esc after Space resets again");

  // ---- combo change path stays synced (pick from the menu) -------------------
  click(doc.getElementById("task-toggle"));
  const opt = doc.querySelector('#task-menu .combo-option[data-value="3c8e17b6-8482-8166-0000-0000000000b2"]');
  ok(!!opt, "task menu offers TASK-2 option");
  opt.dispatchEvent(new window.MouseEvent("mousedown", { bubbles: true, cancelable: true }));
  ok(taskInput.value === "TASK-2 · Beta open", "combo pick sets the same filter state: " + JSON.stringify(taskInput.value));
  ok(!clearBtn.hidden, "reset button visible after combo pick too");
  ok(doc.querySelectorAll("#tbl tr[data-task]").length === 1, "combo pick: one task rendered");

  // Esc with an open task menu must close the menu, NOT clear the filter
  click(doc.getElementById("task-toggle"));
  ok(doc.getElementById("task-menu").classList.contains("open"), "task menu open for Esc-menu test");
  key(taskInput, "Escape");
  ok(!doc.getElementById("task-menu").classList.contains("open"), "Esc closes the open menu");
  ok(taskInput.value === "TASK-2 · Beta open", "Esc on open menu does NOT clear the filter");
  key(doc, "Escape");
  ok(taskInput.value === "", "Esc with closed menu clears the filter");

  console.log(JSON.stringify({ pass, fail }));
  window.close();
  process.exit(fail ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
