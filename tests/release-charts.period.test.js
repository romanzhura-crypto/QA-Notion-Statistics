// jsdom tests for release-charts.html board #239 (период):
// - requirement 1: with a period selected the «Релиз» (sprint) filter is DISABLED
//   and IGNORED — the selection spans releases; the release value is preserved
//   and honoured again after the period is cleared
// - requirement 3: KPI (Задач/Estimate/Time tracking/DEV с данными/Медиана dwell)
//   are recalculated for the selected period
// - hours accounting: the even split across releases (board #214) applies ONLY
//   to per-release accounting; in period mode hours count IN FULL
// Run: NODE_PATH=/tmp/sdtest/node_modules node tests/release-charts.period.test.js
"use strict";
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

// layout-agnostic: workspace tree keeps HTML in widgets/, repo tree in frontend/
const HTML_PATH = ["widgets", "frontend"]
  .map(d => path.join(__dirname, "..", d, "release-charts.html"))
  .find(p => fs.existsSync(p));
const HTML = fs.readFileSync(HTML_PATH, "utf8");

const SEG = (s, days, from, to) => ({ s, days, from, to });
const fixture = {
  ok: true,
  generated_at: "2026-10-01T08:00:00.000Z",
  generated_at_local: "2026-10-01T11:00:00+03:00",
  enriched_at: "2026-10-01T08:10:00.000Z",
  timezone: "Europe/Minsk",
  source: "test",
  data_source_id: "3dee17b6-8482-80a3-9fc4-000bafe19b46",
  task_count: 3,
  releases: ["10.2026", "11.2026"],
  tasks: [
    {
      // September task in TWO releases (split 10/2=5, 4/2=2 per release), dwell 7
      id: "3c8e17b6-8482-8166-0000-0000000000a1",
      tid: "TASK-1",
      u: "https://app.notion.com/p/alpha",
      r: ["10.2026", "11.2026"],
      p: "P1",
      d: "QA",
      s: "Done",
      n: "Alpha",
      e: 10,
      t: 4,
      created: "2026-09-01T07:00:00.000Z",
      history: [SEG("Development", 7, "2026-09-01T07:00:00.000Z", "2026-09-08T07:00:00.000Z")]
    },
    {
      // August task in ONE release (e=6, t=2), dwell 5
      id: "3c8e17b6-8482-8166-0000-0000000000b2",
      tid: "TASK-2",
      u: "https://app.notion.com/p/beta",
      r: ["10.2026"],
      p: "P2",
      d: "DEV",
      s: "Done",
      n: "Beta",
      e: 6,
      t: 2,
      created: "2026-08-05T07:00:00.000Z",
      history: [SEG("Development", 5, "2026-08-05T07:00:00.000Z", "2026-08-10T07:00:00.000Z")]
    },
    {
      // October task, OPEN interval (дни 2), e=4, t=0
      id: "3c8e17b6-8482-8166-0000-0000000000c3",
      tid: "TASK-3",
      u: "https://app.notion.com/p/gamma",
      r: ["11.2026"],
      p: "P1",
      d: "QA",
      s: "Testing",
      n: "Gamma",
      e: 4,
      t: 0,
      created: "2026-10-05T07:00:00.000Z",
      history: [SEG("Testing", 2, "2026-10-05T07:00:00.000Z", null)]
    }
  ]
};

async function main() {
  const dom = new JSDOM(HTML, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: "https://romanzhura-crypto.github.io/QA-Notion-Statistics/release-charts.html",
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
  const kpi = (id) => doc.getElementById(id).textContent;

  const relInput = doc.getElementById("release");
  const clearBtn = doc.getElementById("period-clear");

  // ---- baseline (release mode): hours SPLIT across releases (board #214) ----
  ok(relInput.value === "10.2026", "baseline: first release auto-picked: " + JSON.stringify(relInput.value));
  ok(!relInput.disabled, "baseline: release input enabled");
  ok(clearBtn.hidden, "baseline: period reset hidden");
  ok(kpi("k-n") === "2", "baseline: release 10.2026 = 2 tasks, got " + kpi("k-n"));
  ok(kpi("k-e") === "11", "baseline: Estimate split (10/2 + 6/1 = 11 ч), got " + kpi("k-e"));
  ok(kpi("k-t") === "4", "baseline: Time tracking split (4/2 + 2/1 = 4 ч), got " + kpi("k-t"));
  ok(kpi("k-md") === "6 дн", "baseline: median dwell = (5+7)/2 = 6 дн, got " + JSON.stringify(kpi("k-md")));
  ok(doc.getElementById("k-n-label").textContent === "Задач в релизе", "baseline: KPI label = release wording");
  ok(doc.getElementById("share-note").textContent.indexOf("разделены поровну") !== -1, "baseline: split methodology note");

  // ---- sanity: release 11.2026 alone (Alpha split + Gamma) ------------------
  setCombo(relInput, "11.2026");
  ok(kpi("k-n") === "2", "sanity: release 11.2026 = 2 tasks (Alpha+Gamma)");
  ok(kpi("k-e") === "9", "sanity: Estimate split (10/2 + 4/1 = 9 ч), got " + kpi("k-e"));

  // ---- requirement 1: sprint/release NOT taken into account -----------------
  setPeriod("2026-09-01", "2026-09-30");
  ok(kpi("k-n") === "1", "req1: period Sep ignores the release filter (Alpha only), got " + kpi("k-n"));
  ok(relInput.disabled === true, "req1: release input DISABLED in period mode");
  ok(clearBtn.hidden === false, "req1: period reset (×) visible while period set");

  // ---- hours count IN FULL in period mode (no release split) ----------------
  ok(kpi("k-e") === "10", "period hours: Estimate FULL (10 ч, not 10/2), got " + kpi("k-e"));
  ok(kpi("k-t") === "4", "period hours: Time tracking FULL (4 ч), got " + kpi("k-t"));
  ok(doc.getElementById("share-note").textContent.indexOf("часы показаны полностью") !== -1,
    "period hours: note says hours are full");
  ok(doc.getElementById("k-n-label").textContent === "Задач в периоде", "period hours: KPI label = period wording");

  // ---- requirement 3: KPI recalculated per period ---------------------------
  ok(kpi("k-md") === "7 дн", "req3: Sep median dwell = 7 дн, got " + JSON.stringify(kpi("k-md")));
  ok(kpi("k-d") === "1", "req3: Sep DEV с данными = 1 (QA), got " + kpi("k-d"));

  setPeriod("2026-08-01", "2026-08-31");
  ok(kpi("k-n") === "1", "req3: Aug = 1 task (Beta)");
  ok(kpi("k-e") === "6" && kpi("k-t") === "2", "req3: Aug hours recalculated (6/2), got " + kpi("k-e") + "/" + kpi("k-t"));
  ok(kpi("k-md") === "5 дн", "req3: Aug median dwell = 5 дн, got " + JSON.stringify(kpi("k-md")));

  setPeriod("2026-08-01", "2026-09-30");
  ok(kpi("k-n") === "2", "req3: Aug+Sep = 2 tasks");
  ok(kpi("k-e") === "16" && kpi("k-t") === "6", "req3: Aug+Sep hours = 16/6 (full), got " + kpi("k-e") + "/" + kpi("k-t"));
  ok(kpi("k-md") === "6 дн", "req3: Aug+Sep median dwell = 6 дн, got " + JSON.stringify(kpi("k-md")));

  setPeriod("2026-10-01", "2026-10-31");
  ok(kpi("k-n") === "1", "req3: Oct = 1 task (Gamma, open interval)");
  ok(kpi("k-md") === "≈ 2 дн", "req3: Oct median keeps the open-interval marker, got " + JSON.stringify(kpi("k-md")));

  setPeriod("2026-01-01", "2026-01-31");
  ok(kpi("k-n") === "0", "req3: Jan = 0 tasks");
  ok(kpi("k-e") === "0" && kpi("k-t") === "0", "req3: Jan hours = 0/0, got " + kpi("k-e") + "/" + kpi("k-t"));
  ok(kpi("k-md") === "—", "req3: Jan median = «—» (no data), got " + JSON.stringify(kpi("k-md")));
  ok(doc.getElementById("dev-empty").hidden === false, "req3: dev empty state visible");

  // ---- clear: sprint filter returns, split accounting restored --------------
  click(clearBtn);
  ok(doc.getElementById("period-from").value === "" && doc.getElementById("period-to").value === "",
    "clear: both period bounds reset");
  ok(clearBtn.hidden === true, "clear: period reset hidden again");
  ok(relInput.disabled === false, "clear: release input enabled again");
  ok(relInput.value === "11.2026", "clear: release value preserved: " + JSON.stringify(relInput.value));
  setCombo(relInput, "10.2026");
  ok(kpi("k-n") === "2", "clear: release scope honoured again (2 tasks)");
  ok(kpi("k-e") === "11", "clear: split accounting restored (11 ч), got " + kpi("k-e"));
  ok(doc.getElementById("share-note").textContent.indexOf("разделены поровну") !== -1, "clear: split methodology note restored");
  ok(doc.getElementById("k-n-label").textContent === "Задач в релизе", "clear: KPI label back to release wording");

  console.log("# pass " + pass + ", fail " + fail);
  if (fail) process.exit(1);
}
main().catch(e => { console.error(e); process.exit(1); });
