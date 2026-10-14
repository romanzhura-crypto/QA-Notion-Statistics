// jsdom regression test for board #280: «клик по строке TASK-6893 не открывает
// детальный режим». Root cause: Notion task names may carry trailing/leading
// whitespace (TASK-6893 n = «…для отгрузки »). taskCombo.pick() wrote the raw
// label into the input, match() compared it trimmed-vs-untrimmed → no match →
// cascadeReset() wiped the just-set filter → the row click silently did nothing
// (no exception, no detail mode). Same for Enter/Space on the row.
// Also covers the data-drift half of the case: overlapping history segments
// (Ready For QA from=08:41:00 < Ready For Dev to=08:41:58, 58 sec), float t,
// an open segment — these must never break the render or produce negative
// segment widths (clamp/наложение допустимы, exception — нет).
// Run: NODE_PATH=/tmp/sdtest/node_modules node tests/status-dwell.whitespace-overlap.test.js
"use strict";
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

// layout-agnostic: workspace tree keeps HTML in widgets/, repo tree in frontend/
const HTML_PATH = ["widgets", "frontend"]
  .map(d => path.join(__dirname, "..", d, "status-dwell.html"))
  .find(p => fs.existsSync(p));
const HTML = fs.readFileSync(HTML_PATH, "utf8");

const fixture = {
  ok: true,
  generated_at: "2026-10-14T18:47:33Z",
  generated_at_local: "2026-10-14T21:47:33+03:00",
  enriched_at: "2026-10-14T18:48:06Z",
  timezone: "Europe/Minsk",
  source: "test",
  data_source_id: "3dee17b6-8482-80a3-9fc4-000bafe19b46",
  task_count: 3,
  releases: ["41.2026"],
  status_meta: [
    { name: "New", color: "gray" },
    { name: "Ready For Dev", color: "gray" },
    { name: "Ready For QA", color: "yellow" },
    { name: "Testing", color: "green" },
    { name: "Done", color: "purple" }
  ],
  tasks: [
    // Real-shape TASK-6893 from release-data.json (generated_at 2026-10-14):
    // trailing-space name, overlapping segments, float days, open segment.
    {
      id: "3ede17b6-8482-813c-b4b9-d1be4688bd68",
      tid: "TASK-6893",
      u: "https://app.notion.com/p/3ede17b68482813cb4b9d1be4688bd68",
      r: ["41.2026"],
      p: "storages",
      d: "Шабунин Игорь",
      s: "Ready For QA",
      e: null,
      t: 1.0,
      n: "Запчасти из кузовной таблицы не блокируется для отгрузки ",
      n_full: "Запчасти из кузовной таблицы не блокируется для отгрузки ",
      start: "2026-10-02",
      created: "2026-10-02T08:57:00.000Z",
      edited: "2026-10-05T08:41:00.000Z",
      history: [
        { s: "New", days: 2.9479166666666665, from: "2026-10-02T09:02:00.000Z", to: "2026-10-05T07:46:45.000Z" },
        // overlap drift: Ready For QA starts 58 sec BEFORE Ready For Dev ends
        { s: "Ready For Dev", days: 0.03819444444444445, from: "2026-10-05T07:46:45.000Z", to: "2026-10-05T08:41:58.000Z" },
        { s: "Ready For QA", days: 9.421527777777778, from: "2026-10-05T08:41:00.000Z", to: null }
      ]
    },
    // leading-space name — the same bug class from the other side
    {
      id: "4afe17b6-8482-813c-b4b9-d1be4688bd68",
      tid: "TASK-6894",
      u: "https://app.notion.com/p/4afe17b68482813cb4b9d1be4688bd68",
      r: ["41.2026"],
      p: "storages",
      d: "QA",
      s: "Testing",
      t: 2.5,
      n: " Задача с пробелом в начале",
      created: "2026-10-01T08:00:00.000Z",
      history: [
        { s: "Testing", days: 1.5, from: "2026-10-01T08:00:00.000Z", to: "2026-10-02T20:00:00.000Z" },
        { s: "Ready For QA", days: 3, from: "2026-10-02T20:00:00.000Z", to: null }
      ]
    },
    // negative/NaN duration must clamp to 0 (never a negative rect width)
    {
      id: "5bfe17b6-8482-813c-b4b9-d1be4688bd68",
      tid: "TASK-6895",
      u: "https://app.notion.com/p/5bfe17b68482813cb4b9d1be4688bd68",
      r: ["41.2026"],
      p: "storages",
      d: "QA",
      s: "Testing",
      n: "Отрицательная длительность",
      created: "2026-10-01T08:00:00.000Z",
      history: [
        { s: "Testing", days: -3, from: "2026-10-01T08:00:00.000Z", to: "2026-10-02T20:00:00.000Z" },
        { s: "Ready For QA", days: 1, from: "2026-10-02T20:00:00.000Z", to: null }
      ]
    }
  ]
};

async function main() {
  const errors = [];
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
      window.HTMLElement.prototype.scrollIntoView = function () {};
      window.addEventListener("error", (e) => errors.push("window.error: " + ((e.error && e.error.stack) || e.message)));
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
  const input = (el, v) => {
    el.value = v;
    el.dispatchEvent(new window.Event("input", { bubbles: true, cancelable: true }));
  };

  const svg = doc.getElementById("chart");
  const taskInput = doc.getElementById("task-filter");
  const clearBtn = doc.getElementById("task-clear");

  // ---- initial: 3 rows, trailing-space name trimmed in display -------------
  let rows = Array.from(doc.querySelectorAll("#tbl tr[data-task]"));
  ok(rows.length === 3, "initial: 3 clickable rows, got " + rows.length);
  const name6893 = rows.map(r => r.querySelectorAll("td")[1].textContent).find(s => s.indexOf("кузовной") !== -1);
  ok(name6893 === "Запчасти из кузовной таблицы не блокируется для отгрузки",
    "table name cell trimmed (no trailing space): " + JSON.stringify(name6893));
  const aria6893 = rows.map(r => r.getAttribute("aria-label") || "").find(s => s.indexOf("TASK-6893") !== -1);
  ok(aria6893 === "Показать на графике только задачу TASK-6893 Запчасти из кузовной таблицы не блокируется для отгрузки",
    "aria-label trimmed: " + JSON.stringify(aria6893));

  // ---- negative-duration segment clamped, never a negative width ----------
  ok(Array.from(svg.querySelectorAll("rect.seg")).every(r => parseFloat(r.getAttribute("width")) >= 0),
    "initial: no negative segment widths (negative days clamped)");

  // ---- THE bug: click on the TASK-6893 row opens detail mode --------------
  const row6893 = rows.find(r => r.getAttribute("data-task") === "3ede17b6-8482-813c-b4b9-d1be4688bd68");
  ok(!!row6893, "TASK-6893 row present");
  click(row6893.querySelectorAll("td")[1]); // click on a cell (bubbles to tr)
  ok(taskInput.value === "TASK-6893 · Запчасти из кузовной таблицы не блокируется для отгрузки",
    "row click sets «Задача» filter despite trailing-space name: " + JSON.stringify(taskInput.value));
  ok(!clearBtn.hidden, "reset button appears when filter set");
  rows = Array.from(doc.querySelectorAll("#tbl tr[data-task]"));
  ok(rows.length === 1, "re-render = one task, got " + rows.length);

  let segs = Array.from(svg.querySelectorAll("rect.seg"));
  ok(segs.length === 3, "detail: all 3 segments rendered (overlap tolerated), got " + segs.length);
  const heights = segs.map(r => parseFloat(r.getAttribute("height")));
  ok(heights.every(hh => hh >= 40 && hh <= 64), "detail: thick timeline bar (40–64px): " + JSON.stringify(heights));
  const widths = segs.map(r => parseFloat(r.getAttribute("width")));
  ok(widths.every(ww => ww >= 0), "detail: no negative widths despite 58-sec overlap: " + JSON.stringify(widths));
  const widthSum = widths.reduce((s2, ww) => s2 + ww, 0);
  ok(widthSum > 500, "detail: bar stretched across the full chart width (sum=" + widthSum.toFixed(1) + ")");
  // open interval keeps its hatch marker in detail mode
  ok(svg.querySelectorAll("rect.seg.open-seg").length === 1 && svg.querySelectorAll("rect.open-hatch").length === 1,
    "detail: open interval keeps hatch overlay (наложение допустимо, exception-ов нет)");
  const texts = Array.from(svg.querySelectorAll("text")).map(t2 => t2.textContent);
  ok(texts.some(t2 => t2.indexOf("по сейчас") !== -1), "detail: open-interval label («по сейчас») present");
  // tooltips survive overlapping segments (bindTips)
  ok(segs.every(s2 => (s2.getAttribute("data-tip") || "").length > 0), "detail: tooltips kept on every segment");

  // ---- repeat click on the same row is idempotent -------------------------
  click(doc.querySelector("#tbl tr[data-task]"));
  ok(taskInput.value.indexOf("TASK-6893") === 0, "repeat click: filter unchanged");
  ok(doc.querySelectorAll("#tbl tr[data-task]").length === 1, "repeat click: still one task");

  // ---- typed label WITHOUT the trailing space must match (combo) ----------
  input(taskInput, "TASK-6893 · Запчасти из кузовной таблицы не блокируется для отгрузки");
  ok(taskInput.value === "TASK-6893 · Запчасти из кузовной таблицы не блокируется для отгрузки",
    "typed trimmed label kept");
  ok(doc.querySelectorAll("#tbl tr[data-task]").length === 1,
    "typed trimmed label matches the trailing-space item (match() normalizes)");
  ok(doc.getElementById("task-err").hidden, "no «unknown task» error for the trimmed typed label");

  // ---- exit via «×» -------------------------------------------------------
  click(clearBtn);
  ok(taskInput.value === "", "«×» clears the task filter");
  rows = Array.from(doc.querySelectorAll("#tbl tr[data-task]"));
  ok(rows.length === 3, "«×»: back to the ordinary list (3 rows)");

  // ---- keyboard: Enter / Space also work with whitespace names ------------
  const row6894 = rows.find(r => r.getAttribute("data-task") === "4afe17b6-8482-813c-b4b9-d1be4688bd68");
  key(row6894, "Enter");
  ok(taskInput.value === "TASK-6894 · Задача с пробелом в начале",
    "Enter works with leading-space name: " + JSON.stringify(taskInput.value));
  ok(doc.querySelectorAll("#tbl tr[data-task]").length === 1, "Enter: one task rendered");
  click(clearBtn);
  rows = Array.from(doc.querySelectorAll("#tbl tr[data-task]"));
  const row6895 = rows.find(r => r.getAttribute("data-task") === "5bfe17b6-8482-813c-b4b9-d1be4688bd68");
  key(row6895, " ");
  ok(taskInput.value === "TASK-6895 · Отрицательная длительность",
    "Space works too: " + JSON.stringify(taskInput.value));
  const negWidths = Array.from(svg.querySelectorAll("rect.seg")).map(r => parseFloat(r.getAttribute("width")));
  ok(negWidths.length > 0 && negWidths.every(ww => ww >= 0),
    "detail of the negative-days task: widths clamped to >= 0: " + JSON.stringify(negWidths));

  // ---- nothing threw along the way ---------------------------------------
  ok(errors.length === 0, "no window errors during the whole flow" + (errors.length ? ": " + errors.join(" | ") : ""));

  console.log("\n" + pass + " pass / " + fail + " fail");
  if (fail) process.exitCode = 1;
}
main();
