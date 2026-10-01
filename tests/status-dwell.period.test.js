// jsdom tests for status-dwell.html board #239 (период):
// - requirement 1: with a period selected the «Релиз» (sprint) filter is DISABLED
//   and IGNORED — the selection spans releases; the release value is preserved
//   and honoured again after the period is cleared
// - requirement 2: «Проект» and «Задача» keep working on top of the period
//   (combo items come from the period scope; invalid picks cascade-cleared)
// - requirement 3: KPI «Задач/Статусов/Медиана/Макс» are recalculated for the
//   selected period
// - period semantics: lifecycle overlap (history intervals incl. open→now,
//   Done moments, created/Start points, no-history fallback interval);
//   one bound only = open-ended range; inverted bounds are swapped silently;
//   open intervals keep the «≈» marker
// Run: NODE_PATH=/tmp/sdtest/node_modules node tests/status-dwell.period.test.js
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
  generated_at: "2026-10-01T09:00:00.000Z",
  generated_at_local: "2026-10-01T12:00:00+03:00",
  enriched_at: "2026-10-01T09:30:00.000Z",
  timezone: "Europe/Minsk",
  source: "test",
  data_source_id: "3dee17b6-8482-80a3-9fc4-000bafe19b46",
  task_count: 3,
  releases: ["10.2026", "11.2026"],
  status_meta: [
    { name: "Development", color: "blue" },
    { name: "Testing", color: "green" },
    { name: "Done", color: "purple" }
  ],
  tasks: [
    {
      // September task, release 10.2026, project P1, closed (dwell 5+2 = 7 дн)
      id: "3c8e17b6-8482-8166-0000-0000000000a1",
      tid: "TASK-1",
      u: "https://app.notion.com/p/alpha",
      r: ["10.2026"],
      p: "P1",
      d: "QA",
      s: "Done",
      n: "Alpha",
      created: "2026-09-01T07:00:00.000Z",
      history: [
        SEG("Development", 5, "2026-09-01T07:00:00.000Z", "2026-09-06T07:00:00.000Z"),
        SEG("Testing", 2, "2026-09-06T07:00:00.000Z", "2026-09-08T07:00:00.000Z"),
        { s: "Done", at: "2026-09-08T09:00:00.000Z" }
      ]
    },
    {
      // August task, release 11.2026, project P2, closed (dwell 3+2 = 5 дн)
      id: "3c8e17b6-8482-8166-0000-0000000000b2",
      tid: "TASK-2",
      u: "https://app.notion.com/p/beta",
      r: ["11.2026"],
      p: "P2",
      d: "DEV",
      s: "Done",
      n: "Beta",
      created: "2026-08-05T07:00:00.000Z",
      history: [
        SEG("Development", 3, "2026-08-05T07:00:00.000Z", "2026-08-08T07:00:00.000Z"),
        SEG("Testing", 2, "2026-08-08T07:00:00.000Z", "2026-08-10T07:00:00.000Z"),
        { s: "Done", at: "2026-08-10T09:00:00.000Z" }
      ]
    },
    {
      // October task, release 10.2026, project P1, OPEN interval (дни 2)
      id: "3c8e17b6-8482-8166-0000-0000000000c3",
      tid: "TASK-3",
      u: "https://app.notion.com/p/gamma",
      r: ["10.2026"],
      p: "P1",
      d: "QA",
      s: "Testing",
      n: "Gamma",
      created: "2026-10-05T07:00:00.000Z",
      history: [
        SEG("Testing", 2, "2026-10-05T07:00:00.000Z", null) // open
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
  const fire = (el) => el.dispatchEvent(new window.Event("input", { bubbles: true }));
  const setCombo = (el, v) => { el.value = v; fire(el); };
  const setPeriod = (from, to) => {
    const f = doc.getElementById("period-from");
    const t = doc.getElementById("period-to");
    f.value = from;
    t.value = to;
    fire(f);
  };
  const rowIds = () => Array.from(doc.querySelectorAll("#tbl tr[data-task]")).map(tr => tr.getAttribute("data-task"));
  const kpi = (id) => doc.getElementById(id).textContent;
  const projOpts = () => Array.from(doc.querySelectorAll("#project-menu .combo-option")).map(b => b.getAttribute("data-value"));
  const taskOpts = () => Array.from(doc.querySelectorAll("#task-menu .combo-option")).map(b => b.getAttribute("data-value"));

  const relInput = doc.getElementById("release");
  const relToggle = doc.getElementById("release-toggle");
  const clearBtn = doc.getElementById("period-clear");

  // ---- baseline (no period): release scope as before -----------------------
  ok(relInput.value === "10.2026", "baseline: first release auto-picked: " + JSON.stringify(relInput.value));
  ok(!relInput.disabled, "baseline: release input enabled");
  ok(clearBtn.hidden, "baseline: period reset hidden");
  ok(rowIds().length === 2, "baseline: 2 rows in release 10.2026 (Alpha+Gamma)");
  ok(kpi("k-n") === "2", "baseline: k-n = 2, got " + kpi("k-n"));
  ok(doc.getElementById("empty").textContent === "В этом релизе нет задач.", "baseline: release empty text");

  // ---- requirement 1: sprint/release NOT taken into account -----------------
  setCombo(relInput, "11.2026");
  ok(rowIds().length === 1 && rowIds()[0].indexOf("b2") !== -1, "sanity: release 11.2026 alone = Beta only");
  setPeriod("2026-09-01", "2026-09-30");
  ok(rowIds().length === 1 && rowIds()[0].indexOf("a1") !== -1,
    "req1: period Sep ignores the release filter (11.2026 still set → Alpha shown): " + JSON.stringify(rowIds()));
  ok(kpi("k-n") === "1", "req1: k-n = 1, got " + kpi("k-n"));
  ok(relInput.disabled === true, "req1: release input DISABLED in period mode");
  ok(relToggle.disabled === true, "req1: release toggle DISABLED in period mode");
  ok(doc.getElementById("release-err").hidden, "req1: no «unknown release» error in period mode");
  ok(clearBtn.hidden === false, "req1: period reset (×) visible while period set");

  // ---- requirement 3: KPI recalculated per period ---------------------------
  ok(kpi("k-m") === "7 дн", "req3: Sep median = 7 дн (Alpha 5+2), got " + JSON.stringify(kpi("k-m")));
  ok(kpi("k-x") === "7 дн", "req3: Sep max = 7 дн, got " + JSON.stringify(kpi("k-x")));
  ok(kpi("k-s") === "3", "req3: Sep statuses = 3 (Development/Testing/Done), got " + kpi("k-s"));

  setPeriod("2026-08-01", "2026-08-31");
  ok(rowIds().length === 1 && rowIds()[0].indexOf("b2") !== -1, "req3: Aug period = Beta only");
  ok(kpi("k-m") === "5 дн", "req3: Aug median = 5 дн, got " + JSON.stringify(kpi("k-m")));

  setPeriod("2026-08-01", "2026-09-30");
  ok(rowIds().length === 2, "req3: Aug+Sep period = 2 tasks");
  ok(kpi("k-m") === "6 дн", "req3: Aug+Sep median = 6 дн ((5+7)/2), got " + JSON.stringify(kpi("k-m")));
  ok(kpi("k-x") === "7 дн", "req3: Aug+Sep max = 7 дн, got " + JSON.stringify(kpi("k-x")));

  setPeriod("2026-10-01", "2026-10-31");
  ok(rowIds().length === 1 && rowIds()[0].indexOf("c3") !== -1, "req3: Oct period = Gamma (open interval)");
  ok(kpi("k-m") === "≈ 2 дн", "req3: Oct median keeps the open-interval marker, got " + JSON.stringify(kpi("k-m")));

  setPeriod("2026-01-01", "2026-01-31");
  ok(rowIds().length === 0, "req3: Jan period = 0 rows");
  ok(kpi("k-n") === "0", "req3: Jan k-n = 0, got " + kpi("k-n"));
  ok(doc.getElementById("empty").hidden === false, "req3: empty state visible");
  ok(doc.getElementById("empty").textContent === "В этом периоде нет задач.", "req3: period empty text: " + JSON.stringify(doc.getElementById("empty").textContent));

  // ---- requirement 2: Проект/Задача work on top of the period ---------------
  setPeriod("2026-09-01", "2026-09-30");
  click(doc.getElementById("project-toggle"));
  ok(JSON.stringify(projOpts()) === '["P1"]',
    "req2: project combo items come from the period scope (P1 only), got " + JSON.stringify(projOpts()));
  setCombo(doc.getElementById("project"), "P1");
  ok(rowIds().length === 1 && rowIds()[0].indexOf("a1") !== -1, "req2: period Sep + project P1 = Alpha");
  setCombo(doc.getElementById("project"), "P2");
  ok(rowIds().length === 0, "req2: period Sep + project P2 (out of scope) = 0 rows");
  ok(doc.getElementById("project-err").hidden === false, "req2: project error shown for out-of-scope pick");
  setCombo(doc.getElementById("project"), "");

  click(doc.getElementById("task-toggle"));
  ok(taskOpts().length === 1 && taskOpts()[0].indexOf("a1") !== -1,
    "req2: task combo items come from the period scope (TASK-1 only), got " + JSON.stringify(taskOpts()));
  setCombo(doc.getElementById("task-filter"), "3c8e17b6-8482-8166-0000-0000000000a1");
  ok(rowIds().length === 1 && rowIds()[0].indexOf("a1") !== -1, "req2: period Sep + task filter = Alpha only");
  ok(doc.getElementById("task-clear").hidden === false, "req2: task reset (×) visible in detail mode");
  click(doc.getElementById("task-clear"));
  ok(rowIds().length === 1, "req2: task reset returns to the period scope");

  // project survives a period change when still valid (cascade keeps it)
  setCombo(doc.getElementById("project"), "P1");
  setPeriod("2026-10-01", "2026-10-31");
  ok(doc.getElementById("project").value === "P1", "req2: valid project kept across period change");
  ok(rowIds().length === 1 && rowIds()[0].indexOf("c3") !== -1, "req2: Oct + project P1 = Gamma");

  // ---- bounds: one-sided range and inverted (swapped) bounds ----------------
  setCombo(doc.getElementById("project"), "");
  setPeriod("2026-09-01", "");
  ok(rowIds().length === 2, "bounds: from-only = open-ended (Alpha+Gamma), got " + JSON.stringify(rowIds()));
  setPeriod("", "2026-08-31");
  ok(rowIds().length === 1 && rowIds()[0].indexOf("b2") !== -1, "bounds: to-only = up to bound (Beta)");
  setPeriod("2026-09-30", "2026-09-01");
  ok(rowIds().length === 1 && rowIds()[0].indexOf("a1") !== -1, "bounds: inverted bounds swapped silently (Alpha)");

  // ---- clear: sprint filter returns, value preserved ------------------------
  click(clearBtn);
  ok(doc.getElementById("period-from").value === "" && doc.getElementById("period-to").value === "",
    "clear: both period bounds reset");
  ok(clearBtn.hidden === true, "clear: period reset hidden again");
  ok(relInput.disabled === false, "clear: release input enabled again");
  ok(relInput.value === "11.2026", "clear: release value preserved: " + JSON.stringify(relInput.value));
  ok(rowIds().length === 1 && rowIds()[0].indexOf("b2") !== -1, "clear: release scope honoured again (Beta)");
  ok(kpi("k-m") === "5 дн", "clear: KPI back to release scope, got " + JSON.stringify(kpi("k-m")));
  ok(doc.getElementById("empty").textContent === "В этом релизе нет задач.", "clear: release empty text restored");

  console.log("# pass " + pass + ", fail " + fail);
  if (fail) process.exit(1);
}
main().catch(e => { console.error(e); process.exit(1); });
