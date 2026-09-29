#!/usr/bin/env python3
"""Board #164 phase 2 — widget status coverage: Notion statuses outside the
hardcoded status lists must NOT be silently lost by the widget statistics.

Covers both collectors:
  * release-widget-sync.py  — read side: dwell history + `unknown_statuses` payload field
  * log-statistics-sync.py  — write side: elapsed/dwell accounted for unknown
    statuses (no silent zero), names surfaced in `unknown_statuses`

Run: python3 scripts/test_widget_status_coverage.py   → PASS (exit 0)
Offline: no Notion calls, no network.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import time
from datetime import timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rel = _load("release_widget_sync", HERE / "release-widget-sync.py")
log = _load("log_statistics_sync", HERE / "log-statistics-sync.py")

CHECKS = 0


def seg_sum(items) -> dict:
    """Total days per status across history items. Since board #165 every
    interval is its own item, so totals must be summed (not last-wins)."""
    out = {}
    for h in items or []:
        if not isinstance(h, dict):
            continue
        out[h.get("s")] = round(out.get(h.get("s"), 0.0) + (h.get("days") or 0.0), 6)
    return out


def check(cond, msg: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        raise AssertionError(f"check #{CHECKS} failed: {msg}")
    print(f"  ok #{CHECKS} {msg}")


def test_read_side() -> None:
    print("release-widget-sync: dynamic status collection + unknown_statuses")
    now = rel.parse_ts("2026-09-22T00:00:00Z")

    # 1. Unknown number column AND open interval for unknown collected status:
    #    no time is lost (2.5 closed + 2.0 open), names are surfaced.
    props = {
        "Collected Status": {"rich_text": [{"plain_text": "Blocked"}]},
        "Status since": {"date": {"start": "2026-09-20T00:00:00Z"}},
        "New": {"type": "number", "number": 1.0},
        "Blocked": {"type": "number", "number": 2.5},
        "Done at": {"date": None},
    }
    unknown: set = set()
    hist = rel.history_from_log_row(props, now=now, unknown=unknown)
    seg = seg_sum(hist)
    check(seg.get("New") == 1.0, "known status dwell kept")
    check(seg.get("Blocked") == 4.5, f"unknown status time kept in history (2.5 closed + 2.0 open): {seg}")
    check("Blocked" in unknown, f"unknown collected status surfaced: {sorted(unknown)}")

    # 2. Open interval only (no number column yet) — time still not lost.
    open_only = {
        "Collected Status": {"rich_text": [{"plain_text": "QA Hold"}]},
        "Status since": {"date": {"start": "2026-09-21T00:00:00Z"}},
        "Done at": {"date": None},
    }
    u2: set = set()
    hist2 = rel.history_from_log_row(open_only, now=now, unknown=u2)
    check(seg_sum(hist2).get("QA Hold") == 1.0,
          "open interval of unknown status kept (1.0d)")
    check(u2 == {"QA Hold"}, "unknown name surfaced without number column")

    # 3. Payload: unknown_statuses present (additive), includes LOG-col unknowns
    #    and unknown task current statuses; stderr warning fired.
    payload = {
        "tasks": [
            {"id": "3c8e17b6-8482-8166-0000-000000000000", "s": "Code Freeze"},
            {"id": "3c8e17b6-8482-8166-0000-000000000001", "s": "Done"},
            {"id": "3c8e17b6-8482-8166-0000-000000000002", "s": "Blocked"},
        ],
    }
    log_rows = [{
        "id": "3c8e17b6-8482-8166-0000-0000000000f0",
        "properties": {
            "Task": {"relation": [{"id": "3c8e17b6-8482-8166-0000-000000000002"}]},
            "Collected Status": {"rich_text": [{"plain_text": "Blocked"}]},
            "Status since": {"date": {"start": "2026-09-20T00:00:00Z"}},
            "Blocked": {"type": "number", "number": 2.5},
            "Done at": {"date": None},
        },
    }]
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rel.enrich_with_log_statistics(payload, log_rows=log_rows)
    check("unknown_statuses" in payload, "payload field unknown_statuses present (non-empty case)")
    check(payload["unknown_statuses"] == ["Blocked", "Code Freeze"],
          f"unknown statuses listed (LOG cols + current statuses): {payload['unknown_statuses']}")
    t2 = next(t for t in payload["tasks"] if t["id"].endswith("002"))
    t2seg = seg_sum(t2.get("history"))
    check(t2seg.get("Blocked", 0.0) > 2.5,
          f"task history keeps unknown status time (closed 2.5 + open interval): {t2seg}")
    check("unknown_statuses" in err.getvalue().lower() or "LOG_STATUS_COLS" in err.getvalue(),
          "stderr warning emitted on detection")

    clean = {"tasks": [{"id": "3c8e17b6-8482-8166-0000-000000000003", "s": "Done"}]}
    with contextlib.redirect_stderr(io.StringIO()):
        rel.enrich_with_log_statistics(clean, log_rows=[])
    check("unknown_statuses" in clean and clean["unknown_statuses"] == [],
          "payload field unknown_statuses present (empty case)")


def test_write_side() -> None:
    print("log-statistics-sync: elapsed/dwell for unknown statuses + unknown_statuses")
    log.UNKNOWN_STATUSES.clear()
    log._NOTED_SEGMENTS.clear()
    now = log.parse_ts("2026-09-19T08:00:00Z")
    task = {
        "id": "bbb",
        "s": "Testing",
        "start": "2026-09-10T00:00:00Z",
        "created": "2026-09-10T00:00:00Z",
        "edited": "2026-09-19T08:00:00Z",
    }
    row = {
        "properties": {
            "Collected Status": log.rich_text_prop("Blocked"),
            "Status since": log.date_prop(log.parse_ts("2026-09-17T08:00:00Z")),
            "Blocked": {"number": 1.5},
            "Done at": {"date": None},
        }
    }

    # 1. Close of unknown status: elapsed is added to its dwell (1.5 + 2.0), not zeroed.
    closed = log.collector_transition(task, row, now=now)
    check(closed.get("Blocked", {}).get("number") == 3.5,
          f"unknown status dwell written on close: {closed.get('Blocked')}")
    check(log.UNKNOWN_STATUSES.get("Blocked") == 2.0,
          f"unknown status time accounted in unknown_statuses: {log.UNKNOWN_STATUSES}")

    # 2. Idempotent per segment: re-computation (needs_update + upsert) does not double-count.
    log.collector_transition(task, row, now=now)
    check(log.UNKNOWN_STATUSES.get("Blocked") == 2.0, "no double counting per segment")

    # 3. LOG has no number column for the status: no PATCH on missing column,
    #    but the time is still accounted (never silently zeroed).
    log.WRITABLE_NUMBER_COLS = set(log.STATUS_COLS)
    try:
        gated = log.collector_transition(task, row, now=now)
        check("Blocked" not in gated, "no write to a missing LOG column")
    finally:
        log.WRITABLE_NUMBER_COLS = None
    check(log.UNKNOWN_STATUSES.get("Blocked") == 2.0,
          "time accounted even without a LOG column")

    # 4. Unknown current status is surfaced at seed; dynamic columns zeroed.
    seed_task = {"id": "ccc", "s": "Code Freeze", "start": "2026-09-18T00:00:00Z",
                 "created": "2026-09-18T00:00:00Z", "edited": "2026-09-18T00:00:00Z"}
    seed = log.collector_transition(
        seed_task,
        {"properties": {"Blocked": {"number": 7.0}, "Done at": {"date": None}}},
        now=now,
    )
    check("Code Freeze" in log.UNKNOWN_STATUSES, "unknown seed/current status surfaced")
    check(seed.get("Blocked") == {"number": 0}, "unknown column zeroed at seed like known ones")

    # 5. needs_update sees drift on unknown columns (no silent skip).
    check(log.needs_update(
        log_days_task := {"id": "ddd", "s": "Development",
                          "start": "2026-09-10T00:00:00Z", "created": "2026-09-10T00:00:00Z",
                          "edited": "2026-09-17T00:00:00Z"},
        {"properties": {"Blocked": {"number": 7.0}, "Done at": {"date": None}}},
        now=log.parse_ts("2026-09-18T08:00:00Z"),
    ) is True, "unknown-column drift triggers update")

    # 6. Done is never a dwell column (reopen from Done writes no Done number).
    reopen = log.collector_transition(
        task,
        {"properties": {
            "Collected Status": log.rich_text_prop("Done"),
            "Status since": log.date_prop(log.parse_ts("2026-09-18T08:00:00Z")),
            "Done at": {"date": {"start": "2026-09-18T08:00:00.000Z"}},
        }},
        now=now,
    )
    check("Done" not in {k for k, v in reopen.items() if log.is_number_prop(v)},
          "Done stays a date marker, not a dwell column")


def test_timestamps() -> None:
    print("A31/A6 (board #196): split timestamps + timezone normalization")
    # A6: date-only = midnight Europe/Minsk in BOTH collectors (+3h vs UTC midnight)
    for mod in (rel, log):
        d = mod.parse_ts("2026-09-20")
        check(d is not None and d.strftime("%Y-%m-%dT%H:%M:%S%z") == "2026-09-20T00:00:00+0300",
              f"{mod.__name__}: date-only parsed as midnight Europe/Minsk (+03)")
        check(d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-09-19T21:00:00Z",
              f"{mod.__name__}: explicit +3h shift vs UTC midnight (21:00Z previous day)")

    # A6: day aggregate with a date-only boundary follows the Minsk midnight:
    # open interval 2026-09-20 (Minsk) -> 2026-09-22T00:00Z = 2.125 days, not 2.0.
    hist = rel.history_from_log_row({
        "Collected Status": {"rich_text": [{"plain_text": "Blocked"}]},
        "Status since": {"date": {"start": "2026-09-20"}},
        "Done at": {"date": None},
    }, now=rel.parse_ts("2026-09-22T00:00:00Z"), unknown=set())
    op = [h for h in hist if h.get("to") is None][0]
    check(op["from"] == "2026-09-19T21:00:00.000Z",
          "A6: date-only Status since -> 2026-09-19T21:00Z (Minsk midnight)")
    check(abs(op["days"] - 2.125) < 1e-9,
          "A6: open-interval day aggregate uses the Minsk midnight boundary (2.125d)")

    # A31: enrich must not move generated_at (Sprint data time); enriched_at is
    # the LOG-merge stamp and refreshes on re-enrich. A6: timezone +
    # generated_at_local travel with the payload.
    import tempfile
    old_ws, old_out = rel.WS_JSON, rel.OUT_JSON
    with tempfile.TemporaryDirectory() as td:
        rel.WS_JSON = Path(td) / "release-data.json"
        rel.OUT_JSON = Path(td) / "out.json"
        try:
            rel.WS_JSON.write_text(json.dumps({
                "generated_at": "2026-09-28T06:00:00Z",
                "task_count": 1,
                "tasks": [{"id": "t1", "s": "Done"}],
            }), encoding="utf-8")
            p1 = rel.enrich_existing(now=rel.parse_ts("2026-09-28T07:00:00Z"), log_rows=[])
            check(p1["generated_at"] == "2026-09-28T06:00:00Z",
                  "A31: enrich keeps generated_at (Sprint data time)")
            check(p1["enriched_at"] == "2026-09-28T07:00:00Z",
                  "A31: enriched_at stamped at LOG-merge time")
            check(p1["timezone"] == "Europe/Minsk", "A6: payload.timezone present")
            check(p1["generated_at_local"] == "2026-09-28T09:00:00+03:00",
                  "A6: generated_at_local is the same instant in Minsk (+3h)")
            p2 = rel.enrich_existing(now=rel.parse_ts("2026-09-28T08:00:00Z"), log_rows=[])
            check(p2["generated_at"] == "2026-09-28T06:00:00Z",
                  "A31: re-enrich still keeps generated_at")
            check(p2["enriched_at"] == "2026-09-28T08:00:00Z",
                  "A31: re-enrich refreshes enriched_at")
            check(p2["generated_at_local"] == "2026-09-28T09:00:00+03:00",
                  "A6: generated_at_local follows generated_at, not the enrich moment")
        finally:
            rel.WS_JSON, rel.OUT_JSON = old_ws, old_out


def test_a10_titles_and_diff() -> None:
    print("A10 (board #197): full title in n_full + diffAdded keyed by id only")
    import shutil
    import subprocess
    import tempfile

    # case 1: title > TITLE_MAX lands in n_full IN FULL; n keeps the trimmed form
    long_title = "Release widget title edge case " * 9  # 288 chars > 140
    assert len(long_title) > rel.TITLE_MAX
    row = {
        "id": "3c8e17b6-8482-8166-0000-0000000000aa",
        "url": "https://notion.so/x",
        "created_time": "2026-09-20T10:00:00.000Z",
        "last_edited_time": "2026-09-21T10:00:00.000Z",
        "properties": {
            "Documentation": {"type": "title", "title": [{"plain_text": long_title}]},
            "Status": {"type": "status", "status": {"name": "New"}},
            "DEV": {"type": "select", "select": {"name": "Ivan"}},
            "Release": {"type": "multi_select", "multi_select": [{"name": "1.0"}]},
        },
    }
    old = (rel.query_pages, rel._req, rel.write_payload, rel.WS_JSON, rel.OUT_JSON)
    with tempfile.TemporaryDirectory() as td:
        try:
            rel.query_pages = lambda dsid, fetch=None: (([row], 0) if dsid == rel.DSID else ([], 0))
            rel._req = lambda method, path, body=None: {
                "properties": {"Status": {"status": {"options": [{"name": "New", "color": "default"}]}}}
            }
            rel.write_payload = lambda payload: payload
            rel.WS_JSON = Path(td) / "release-data.json"
            rel.OUT_JSON = Path(td) / "out.json"
            payload = rel.snapshot()
        finally:
            (rel.query_pages, rel._req, rel.write_payload, rel.WS_JSON, rel.OUT_JSON) = old
    t = payload["tasks"][0]
    check(t.get("n_full") == long_title and t.get("n") == long_title[:rel.TITLE_MAX]
          and len(t["n_full"]) > rel.TITLE_MAX,
          f"A10: title > {rel.TITLE_MAX} chars lands in n_full in full, n stays trimmed")

    # case 2: diffAdded (frontend/release-charts.html) keys tasks by id ONLY —
    # two different tasks sharing the same truncated n|d|r must never false-match.
    # Executed for real (the shipped JS) via node; static source fallback when
    # node is unavailable. Offline either way.
    html_path = next(
        (p for p in (HERE.parent / "frontend" / "release-charts.html",
                     HERE.parent / "widgets" / "release-charts.html") if p.is_file()),
        None,
    )
    ok = False
    if html_path is not None:
        html = html_path.read_text(encoding="utf-8")
        src = html[html.index("function taskKey"):html.index("function applyPayload")]
        node = shutil.which("node")
        if node:
            harness = """
const X = { n: "Одинаково обрезанное длинное название задачи…", d: "Ivan", r: ["1.0"] };
const A = Object.assign({ id: "aaa" }, X);
const B = Object.assign({ id: "bbb" }, X);
const pair = diffAdded({ releases: ["1.0"], tasks: [A] }, { releases: ["1.0"], tasks: [A, B] });
const swap = diffAdded({ releases: ["1.0"], tasks: [A] }, { releases: ["1.0"], tasks: [B] });
const legacy = diffAdded({ releases: ["1.0"], tasks: [Object.assign({}, X)] },
                         { releases: ["1.0"], tasks: [A, B] });
console.log(JSON.stringify({ pair: pair.addedTasks, swap: swap.addedTasks, legacy: legacy.addedTasks }));
"""
            with tempfile.TemporaryDirectory() as td:
                js = Path(td) / "diffadded-case.js"
                js.write_text(src + harness, encoding="utf-8")
                out = subprocess.run([node, str(js)], capture_output=True, text=True, timeout=30)
            try:
                got = json.loads(out.stdout.strip().splitlines()[-1])
            except Exception:
                got = {"error": (out.stderr or out.stdout)[-200:]}
            ok = got == {"pair": 1, "swap": 1, "legacy": 2}
            msg = f"A10: diffAdded by id only — same truncated n|d|r, different id is NOT a match (got {got})"
        else:
            ok = "useId" not in src and ".n ||" not in src and "t.id" in src
            msg = "A10: diffAdded source is id-only (static check, node unavailable)"
    else:
        msg = "A10: release-charts.html not found next to the tree root"
    check(ok, msg)


def test_journal() -> None:
    """A17 (board #202/#224) — checkpoint journal: resume / stale / end."""
    import shutil
    import tempfile
    import time as _time

    jdir = Path(tempfile.mkdtemp(prefix="journal-cov-"))
    try:
        # 1) crash without end -> resume restores done ids
        jp = jdir / "j.jsonl"
        j1 = log.journal_begin(jp)
        log.journal_done(j1, "aaa", "created")
        log.journal_done(j1, "bbb", {"page": "p1"})
        j2 = log.journal_begin(jp)
        check(
            j2["resumed"] is True and j2["run_id"] == j1["run_id"] and j2["done"] == {"aaa", "bbb"},
            "A17 journal: begin→done→(crash, no end)→begin = resume with done ids restored",
        )
        # 2) abandoned stale run (started_at 25h ago) is not resumed
        old = _time.time() - 25 * 3600
        jp2 = jdir / "old.jsonl"
        log._journal_append(jp2, {"type": "run", "t": old, "run_id": "oldrun000001", "started_at": old})
        log._journal_append(jp2, {"type": "done", "t": old, "run_id": "oldrun000001", "id": "zzz", "result": None})
        j3 = log.journal_begin(jp2)
        check(
            j3["resumed"] is False and j3["run_id"] != "oldrun000001" and j3["done"] == set(),
            "A17 journal: stale abandoned run (started_at 25h ago) NOT resumed — fresh run_id",
        )
        # 3) after end the journal is zeroed -> next begin is a fresh run
        log.journal_end(j2, "ok", counts={"done": 2})
        j4 = log.journal_begin(jp)
        check(
            jp.read_text(encoding="utf-8").startswith("{\"type\": \"run\"")
            and j4["resumed"] is False
            and j4["run_id"] != j2["run_id"],
            "A17 journal: journal_end zeroes the journal, next begin = fresh run",
        )
    finally:
        shutil.rmtree(jdir, ignore_errors=True)


def test_resume_main() -> None:
    print("log-statistics-sync main(): checkpoint resume (board #225)")
    d = Path("/tmp") / f"a172-resume-{id(object())}"
    d.mkdir(parents=True, exist_ok=True)
    tasks = [{"id": "T1", "n": "a"}, {"id": "T2", "n": "b"}, {"id": "T3", "n": "c"}]
    saved = {k: getattr(log, k) for k in ("RUN_STATE", "JOURNAL", "WRITE_SLEEP_S")}
    saved_env = {k: log.os.environ.get(k) for k in ("LOG_STATS_RUN", "LOG_STATS_JOB")}
    orig = {k: getattr(log, k) for k in (
        "load_tasks", "query_all", "log_number_cols", "index_log_rows",
        "upsert", "needs_update", "archive_page",
    )}
    calls = []
    try:
        log.RUN_STATE = d / "run-state.json"
        log.JOURNAL = d / "journal.jsonl"
        log.WRITE_SLEEP_S = 0.0
        log.os.environ["LOG_STATS_RUN"] = "1"
        log.os.environ["LOG_STATS_JOB"] = "selftest"
        log.load_tasks = lambda: (list(tasks), "2026-09-28T00:00:00Z", 3)
        log.query_all = lambda dsid: []
        log.log_number_cols = lambda: set()
        log.index_log_rows = lambda rows: ({}, [], [])
        log.needs_update = lambda *a, **k: True
        log.archive_page = lambda pid: (200, "archived")

        def upsert_crash(task, title, page_id, now=None, log_row=None):
            calls.append(task["id"])
            if len(calls) >= 3:
                raise RuntimeError("simulated crash mid-batch")
            return "created", 200, "P-" + task["id"]

        log.upsert = upsert_crash
        crashed = False
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                log.main()
            except RuntimeError:
                crashed = True
        check(crashed, "A17.2: crash mid-batch propagates (no journal end -> next run resumes)")
        jlines = [json.loads(x) for x in (d / "journal.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        dones = [r for r in jlines if r.get("type") == "done"]
        check(
            len(dones) == 2 and {r["id"] for r in dones} == {"T1", "T2"},
            f"A17.2: journal holds 2 done after crash at 3rd of 3: {[(r.get('id'), r.get('result')) for r in dones]}",
        )

        def upsert_count(task, title, page_id, now=None, log_row=None):
            calls.append(task["id"])
            return "created", 200, "P-" + task["id"]

        log.upsert = upsert_count
        calls.clear()
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            log.main()
        check(calls == ["T3"], f"A17.2: resume upserts exactly the remaining task once: {calls}")
        out = json.loads(buf2.getvalue().strip().splitlines()[-1])
        check(
            out.get("resumed") is True and out.get("resume_skipped") == 2 and out.get("created") == 1,
            f"A17.2: output carries resumed/resume_skipped: resumed={out.get('resumed')} skip={out.get('resume_skipped')} created={out.get('created')}",
        )
        rs = json.loads((d / "run-state.json").read_text(encoding="utf-8"))
        check(
            rs.get("resumed") is True and rs.get("resume_skipped") == 2,
            "A17.2: run-state carries resumed/resume_skipped",
        )

        # b) rerun after a COMPLETED run is idempotent: journal zeroed -> fresh run,
        #    needs_update False -> all skipped, zero repeated writes.
        log.index_log_rows = lambda rows: (
            {t["id"]: {"id": "P-" + t["id"], "properties": {}} for t in tasks}, [], [],
        )
        log.needs_update = lambda *a, **k: False
        calls.clear()
        buf3 = io.StringIO()
        with contextlib.redirect_stdout(buf3):
            log.main()
        out3 = json.loads(buf3.getvalue().strip().splitlines()[-1])
        check(
            calls == [] and out3.get("skipped") == 3 and out3.get("resumed") is False and out3.get("resume_skipped") == 0,
            f"A17.2: rerun after completed run is idempotent (0 writes): calls={calls} "
            f"skipped={out3.get('skipped')} resumed={out3.get('resumed')} resume_skipped={out3.get('resume_skipped')}",
        )
    finally:
        for k, v in orig.items():
            setattr(log, k, v)
        for k, v in saved.items():
            setattr(log, k, v)
        for k, v in saved_env.items():
            if v is None:
                log.os.environ.pop(k, None)
            else:
                log.os.environ[k] = v
        for p in d.iterdir():
            p.unlink()
        d.rmdir()


def test_widget_fetch_cache() -> None:
    print("release-widget-sync snapshot(): fetch checkpoint cache (board #226)")
    td = Path(tempfile.mkdtemp(prefix="a173-cache-"))
    jp = td / "journal.jsonl"
    # ids must be UUID-shaped: non-UUID ids are fixtures (is_fixture_row) and
    # would be excluded from the payload
    u1 = "11111111-1111-4111-8111-111111111111"
    u2 = "22222222-2222-4222-8222-222222222222"
    u3 = "33333333-3333-4333-8333-333333333333"
    pages = [
        {"results": [{"id": u1, "properties": {}}, {"id": u2, "properties": {}}],
         "has_more": True, "next_cursor": "c1"},
        # seam duplicate u2 across pages — cache must hold deduped rows (A24)
        {"results": [{"id": u2, "properties": {}}, {"id": u3, "properties": {}}],
         "has_more": False},
    ]
    meta = {"properties": {"Status": {"status": {"options": [{"name": "New", "color": "blue"}]}}}}

    def make_req(state):
        def _req(method, path, body=None):
            if method == "GET":
                state["meta"] += 1
                if state.get("crash_at_meta") and state["meta"] >= state["crash_at_meta"]:
                    raise RuntimeError("simulated crash after cache (board #226)")
                return meta
            key = "tasks" if "ds-test" in path else "log"
            state[key] += 1
            if state.get("crash_at_query") and state[key] >= state["crash_at_query"]:
                raise RuntimeError("simulated crash before cache (board #226)")
            if key == "log":
                return {"results": [], "has_more": False}
            return pages[0] if "start_cursor" not in (body or {}) else pages[1]
        return _req

    def fresh_state(**kw):
        s = {"tasks": 0, "log": 0, "meta": 0}
        s.update(kw)
        return s

    def snapshot_ok():
        with contextlib.redirect_stdout(io.StringIO()):
            payload = rel.snapshot()
        return json.loads(json.dumps(payload))

    missing = object()
    saved = {k: getattr(rel, k, missing) for k in
             ("WIDGETS_JOURNAL", "WS_JSON", "OUT_JSON", "DSID", "LOG_DSID",
              "SNAP_CACHE_MAX_AGE_S", "_req", "_FETCH_JRUN")}
    try:
        rel.WIDGETS_JOURNAL = jp
        rel.WS_JSON = td / "release-data.json"
        rel.OUT_JSON = td / "out.json"
        rel.DSID = "ds-test"
        rel.LOG_DSID = "log-test"
        rel.SNAP_CACHE_MAX_AGE_S = 3600
        rel._FETCH_JRUN = None

        # --- A: crash after the tasks cache line -> resume must NOT re-fetch
        state = fresh_state(crash_at_meta=1)
        rel._req = make_req(state)
        crashed = False
        try:
            rel.snapshot()
        except RuntimeError:
            crashed = True
        jlines = [json.loads(x) for x in jp.read_text(encoding="utf-8").splitlines() if x.strip()]
        kinds = [r.get("type") for r in jlines]
        cache_keys = [r.get("key") for r in jlines if r.get("type") == "cache"]
        check(
            crashed and kinds.count("run") == 1 and "end" not in kinds and cache_keys == ["ds-test"],
            f"A17.3: crash after tasks cache -> journal open with 1 cache line ds-test: {kinds} {cache_keys}",
        )
        check(
            state["tasks"] == 2,
            f"A17.3: run-1 tasks fetch = 2 pages: {state['tasks']}",
        )
        # resume run: cache fresh -> tasks NOT re-fetched (page counter stays 2)
        state["crash_at_meta"] = None
        out = snapshot_ok()
        check(
            state["tasks"] == 2 and state["log"] == 1,
            f"A17.3: resume reuses tasks cache (no re-fetch): tasks_pages={state['tasks']} log_pages={state['log']}",
        )
        q = out.get("quality") or {}
        uniq = {t.get("id") for t in out.get("tasks") or []}
        check(
            out.get("task_count") == 3 and uniq == {u1, u2, u3}
            and q.get("task_count_mismatch") == 0 and q.get("duplicate_rows") == 1,
            f"A17.3: payload from cache: task_count={out.get('task_count')} unique={sorted(uniq)} "
            f"mismatch={q.get('task_count_mismatch')} dups={q.get('duplicate_rows')}",
        )
        check(
            jp.read_text(encoding="utf-8") == "",
            "A17.3: journal_end zeroes the journal after a successful run",
        )

        # --- B: cache older than SNAP_CACHE_MAX_AGE_S -> fetch again
        state2 = fresh_state(crash_at_meta=1)
        rel._req = make_req(state2)
        try:
            rel.snapshot()
        except RuntimeError:
            pass
        old = time.time() - 7200  # twice the 3600s limit
        aged = []
        for raw in jp.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            r = json.loads(raw)
            if r.get("type") == "cache":
                r["fetched_at"] = old
            aged.append(json.dumps(r, ensure_ascii=False))
        jp.write_text("\n".join(aged) + "\n", encoding="utf-8")
        state2["crash_at_meta"] = None
        snapshot_ok()
        check(
            state2["tasks"] == 4,
            f"A17.3: stale cache (fetched_at 2h ago > 3600s) -> fetch repeats: tasks_pages={state2['tasks']}",
        )

        # --- C: crash BEFORE any cache line -> fetch repeats, nothing lost
        jp.write_text("", encoding="utf-8")
        rel.WS_JSON.write_text('{"task_count": 0, "tasks": []}', encoding="utf-8")
        state3 = fresh_state(crash_at_query=1)
        rel._req = make_req(state3)
        try:
            rel.snapshot()
        except RuntimeError:
            pass
        cache_lines = [x for x in jp.read_text(encoding="utf-8").splitlines() if '"cache"' in x]
        check(
            cache_lines == [],
            "A17.3: crash before cache -> no cache line in the journal",
        )
        state3["crash_at_query"] = None
        out3 = snapshot_ok()
        uniq3 = {t.get("id") for t in out3.get("tasks") or []}
        check(
            state3["tasks"] == 3 and out3.get("task_count") == 3 and uniq3 == {u1, u2, u3},
            f"A17.3: no cache -> refetch, no losses: pages={state3['tasks']} "
            f"task_count={out3.get('task_count')} unique={sorted(uniq3)}",
        )
    finally:
        for k, v in saved.items():
            if v is missing:
                if hasattr(rel, k):
                    delattr(rel, k)
            else:
                setattr(rel, k, v)
        for p in td.iterdir():
            p.unlink()
        td.rmdir()


def main() -> int:
    test_read_side()
    test_write_side()
    test_timestamps()
    test_a10_titles_and_diff()
    test_journal()
    test_resume_main()
    test_widget_fetch_cache()
    print(json.dumps({"ok": True, "result": "PASS", "checks": CHECKS}, ensure_ascii=False))
    print("PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
