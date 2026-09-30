// jsdom tests for release-charts.html board #214 (A11, variant B): a task in
// several releases contributes hours/N to EACH release — per-release totals
// summed across releases equal the true total (no double counting).
// Also checks the methodology note («часы / число релизов») is visible.
// Run: NODE_PATH=/tmp/sdtest/node_modules node tests/release-charts.multirelease.test.js
"use strict";
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

// layout-agnostic: workspace tree keeps HTML in widgets/, repo tree in frontend/
const HTML_PATH = ["widgets", "frontend"]
  .map(d => path.join(__dirname, "..", d, "release-charts.html"))
  .find(p => fs.existsSync(p));
const HTML = fs.readFileSync(HTML_PATH, "utf8");

// T1 and T3 are multi-release (N=2 -> half hours per release), T2 is single.
const fixture = {
  ok: true,
  generated_at: "2026-09-30T08:00:00.000Z",
  generated_at_local: "2026-09-30T11:00:00+03:00",
  enriched_at: "2026-09-30T08:10:00.000Z",
  timezone: "Europe/Minsk",
  source: "test",
  data_source_id: "3dee17b6-8482-80a3-9fc4-000bafe19b46",
  task_count: 3,
  releases: ["R1", "R2"],
  tasks: [
    { id: "m1", r: ["R1", "R2"], d: "Dev A", s: "Done", n: "Multi 1", e: 8, t: 6 },
    { id: "m2", r: ["R1"], d: "Dev A", s: "Done", n: "Single", e: 4, t: 2 },
    { id: "m3", r: ["R1", "R2"], d: "Dev B", s: "Done", n: "Multi 2", e: 2, t: 2 }
  ]
};
const FULL_EST = fixture.tasks.reduce((s, t) => s + t.e, 0); // 14
const FULL_SPENT = fixture.tasks.reduce((s, t) => s + t.t, 0); // 10

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
      // jsdom does not implement scrollIntoView (combo highlight uses it)
      window.Element.prototype.scrollIntoView = function () {};
    }
  });
  await new Promise(r => setTimeout(r, 300));
  try {
    await fn(dom.window);
  } finally {
    dom.window.close();
  }
}

function pick(window, name) {
  const el = window.document.getElementById("release");
  el.value = name;
  el.dispatchEvent(new window.Event("input"));
}

function kpi(window, id) {
  return window.document.getElementById(id).textContent;
}

async function main() {
  let pass = 0, fail = 0;
  const ok = (cond, msg) => {
    if (cond) { pass++; console.log("ok - " + msg); }
    else { fail++; console.log("FAIL - " + msg); }
  };

  await withDom(fixture, (window) => {
    const doc = window.document;

    // --- R1: T1(8/6 split -> 4/3) + T2(4/2 full) + T3(2/2 split -> 1/1)
    pick(window, "R1");
    ok(kpi(window, "k-n") === "3", "R1: 3 tasks in release");
    ok(kpi(window, "k-e") === "9", "R1: Estimate = 4 + 4 + 1 (split) = 9, got " + kpi(window, "k-e"));
    ok(kpi(window, "k-t") === "6", "R1: Time tracking = 3 + 2 + 1 (split) = 6, got " + kpi(window, "k-t"));
    const devR1 = doc.getElementById("dev-table").innerHTML;
    ok(/Dev A<\/td><td>8<\/td><td>5<\/td>/.test(devR1),
      "R1 per-DEV: Dev A gets split hours (8 est / 5 spent): " + devR1);
    ok(/Dev B<\/td><td>1<\/td><td>1<\/td>/.test(devR1),
      "R1 per-DEV: Dev B gets split hours (1 est / 1 spent): " + devR1);

    // --- R2: only the two multi-release tasks at half hours
    pick(window, "R2");
    ok(kpi(window, "k-n") === "2", "R2: 2 tasks in release");
    ok(kpi(window, "k-e") === "5", "R2: Estimate = 4 + 1 (split) = 5, got " + kpi(window, "k-e"));
    ok(kpi(window, "k-t") === "4", "R2: Time tracking = 3 + 1 (split) = 4, got " + kpi(window, "k-t"));

    // --- sums converge: per-release totals across releases == full hours once
    const estR2 = parseFloat(kpi(window, "k-e")), spentR2 = parseFloat(kpi(window, "k-t"));
    pick(window, "R1");
    const estR1 = parseFloat(kpi(window, "k-e")), spentR1 = parseFloat(kpi(window, "k-t"));
    ok(estR1 + estR2 === FULL_EST,
      "Σ Estimate over R1+R2 (" + (estR1 + estR2) + ") == full task hours once (" + FULL_EST + ") — no double count");
    ok(spentR1 + spentR2 === FULL_SPENT,
      "Σ Time tracking over R1+R2 (" + (spentR1 + spentR2) + ") == full task hours once (" + FULL_SPENT + ") — no double count");

    // --- methodology note (подпись методики) is visible
    const text = doc.body.textContent;
    ok(text.indexOf("разделены поровну") !== -1, "methodology note «разделены поровну» shown");
    ok(text.indexOf("часы / число релизов") !== -1, "methodology note «часы / число релизов» shown");
  });

  // --- single-release task keeps full hours (N=1)
  await withDom(fixture, (window) => {
    pick(window, "R1");
    const devR1 = window.document.getElementById("dev-table").innerHTML;
    ok(/Dev A<\/td><td>8<\/td><td>5<\/td>/.test(devR1),
      "N=1 guard: full hours survive next to split ones (Dev A 4+4 / 3+2)");
  });

  console.log(JSON.stringify({ pass, fail }));
  process.exit(fail ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
