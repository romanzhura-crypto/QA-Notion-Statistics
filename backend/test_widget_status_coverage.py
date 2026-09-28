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


def main() -> int:
    test_read_side()
    test_write_side()
    test_timestamps()
    print(json.dumps({"ok": True, "result": "PASS", "checks": CHECKS}, ensure_ascii=False))
    print("PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
