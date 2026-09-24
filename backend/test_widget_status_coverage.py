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
    seg = {h["s"]: h["days"] for h in hist}
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
    check({h["s"]: h["days"] for h in hist2}.get("QA Hold") == 1.0,
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
    t2seg = {h["s"]: h["days"] for h in t2.get("history") or []}
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


def main() -> int:
    test_read_side()
    test_write_side()
    print(json.dumps({"ok": True, "result": "PASS", "checks": CHECKS}, ensure_ascii=False))
    print("PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
