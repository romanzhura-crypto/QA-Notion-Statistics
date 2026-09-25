#!/usr/bin/env python3
"""Apply one Notion status webhook event (board #171, architecture B).

Pipeline: Notion Webhooks → Cloudflare Worker (HMAC + repository_dispatch) →
this script in GitHub Actions. It closes/open status intervals in the LOG
STATISTICS `Segments` property with the EXACT event timestamp.

Design rules (keep in sync with docs/release-data-contract.md):
- Idempotent: duplicate/retried events are no-ops.
- Order-tolerant: Notion does not guarantee delivery order; the handler reads
  the CURRENT page status and ignores stale events (timestamp older than the
  collector's Status since). `log-statistics-sync.py` stays the reconciliation
  fallback and never loses dwell time.
- Minute precision, dedupe by (s, from, to) — same as the collector.
- Never prints tokens.

Input: EVENT_JSON env (client_payload from repository_dispatch) or --file <json>.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _ljs():
    spec = importlib.util.spec_from_file_location("log_statistics_sync", HERE / "log-statistics-sync.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LJS = _ljs()


def plan_event(log_props: dict, new_status: str, event_ts, done_memory: str | None = None) -> dict:
    """LOG-row property patch for one observed status event. Empty patch = no-op.

    new_status = CURRENT status of the task page (fetched fresh from Notion;
    the webhook payload carries no property values). event_ts = event.timestamp.
    """
    ts = LJS.parse_ts(event_ts)
    if not ts:
        raise ValueError("bad event timestamp")
    out: dict = {}
    have_collected = LJS.collected_from_props(log_props)
    have_since = LJS.since_from_props(log_props)
    new_status = str(new_status or "").strip() or "Unknown"
    prev_done = LJS.log_done_at(log_props) or done_memory

    if not have_collected:
        # First see: seed only. Collector owns column zeroing semantics.
        out["Collected Status"] = LJS.rich_text_prop(new_status)
        out["Status since"] = LJS.date_prop(ts)
        return out

    if have_collected == new_status:
        return out  # duplicate/aggregated repeat — no-op

    if have_since and ts <= have_since:
        return out  # stale/out-of-order event; next event or the collector reconciles

    segs = LJS.segments_from_props(log_props)
    seg_from = LJS.iso_z(have_since or ts)
    seg_to = LJS.iso_z(ts)
    is_old_done = have_collected.lower() == "done"
    if not is_old_done and not any(
        x["s"] == have_collected and x["from"] == seg_from and x["to"] == seg_to for x in segs
    ):
        segs.append({"s": have_collected, "from": seg_from, "to": seg_to})
        out[LJS.SEGMENTS_PROP] = LJS.segments_prop(segs)

    if not is_old_done and have_since and LJS.status_col_allowed(have_collected):
        added = LJS.elapsed_days(have_since, ts)
        if added > 0:
            prev = LJS.number_from_props(log_props, have_collected)
            out[have_collected] = {"number": LJS.round_days(prev + added)}

    out["Collected Status"] = LJS.rich_text_prop(new_status)
    out["Status since"] = LJS.date_prop(ts)

    # Done at: immutable; event timestamp is more precise than last_edited_time.
    done_now = new_status.lower() == "done"
    if done_now and not prev_done:
        out["Done at"] = {"date": {"start": LJS.iso_z(ts)}}
    elif not done_now and is_old_done and prev_done:
        out["Done at"] = {"date": None}  # reopen clears the marker (collector parity)
    return out


def main() -> int:
    raw = os.environ.get("EVENT_JSON") or ""
    if "--file" in sys.argv:
        raw = Path(sys.argv[sys.argv.index("--file") + 1]).read_text(encoding="utf-8")
    if not raw:
        print(json.dumps({"ok": False, "error": "no EVENT_JSON"}))
        return 2
    ev = json.loads(raw)
    entity_id = str(ev.get("entity_id") or "")
    entity_type = str(ev.get("entity_type") or "page")
    if not entity_id or entity_type != "page":
        print(json.dumps({"ok": True, "skipped": "not a page event"}))
        return 0

    LJS.log_number_cols()  # schema check: never PATCH a missing number column
    rows = LJS.query_all(LJS.LOG_DSID)
    mapping, _empties, _dups = LJS.index_log_rows(rows)
    log_row = mapping.get(entity_id)
    if not log_row:
        print(json.dumps({"ok": True, "skipped": "no LOG row for page"}))
        return 0

    code, page = LJS._req("GET", f"/v1/pages/{entity_id}")
    if code != 200:
        print(json.dumps({"ok": False, "error": f"page fetch {code}"}))
        return 1
    props = page.get("properties") or {}
    status = (
        LJS._prop_status_name(props.get("Status"))
        or LJS._prop_status_name(props.get("Current Status"))
        or "Unknown"
    )
    patch = plan_event(log_row.get("properties") or {}, str(status), ev.get("timestamp"))
    if not patch:
        print(json.dumps({"ok": True, "noop": True, "status": status}))
        return 0
    code, obj = LJS._req("PATCH", f"/v1/pages/{log_row['id']}", {"properties": patch})
    ok = code == 200
    print(json.dumps({
        "ok": ok,
        "status": status,
        "patched": sorted(patch.keys()),
        "error": None if ok else str(obj.get("message") or obj),
    }))
    return 0 if ok else 1


def selftest() -> None:
    ts0 = "2026-09-25T09:00:00.000Z"
    ts1 = "2026-09-25T10:00:00.000Z"
    ts2 = "2026-09-25T10:05:00.000Z"
    base = {
        "Collected Status": {"rich_text": [{"plain_text": "Development"}]},
        "Status since": {"date": {"start": ts0}},
        "Development": {"type": "number", "number": 1.5},
        "Done at": {"date": None},
    }
    # 1) transition closes the interval with exact boundaries, opens the new one
    p = plan_event(base, "Ready For QA", ts1)
    segs = LJS.segments_from_props(p)
    assert segs == [{"s": "Development", "from": ts0, "to": ts1}], segs
    assert LJS.rich_text_plain(p["Collected Status"]) == "Ready For QA"
    assert p["Status since"]["date"]["start"].startswith("2026-09-25T10:00")
    assert p["Development"]["number"] == 1.5 + 1 / 24, p["Development"]
    # 2) duplicate/retried event (same status) is a no-op
    assert plan_event({**base, "Collected Status": p["Collected Status"], "Status since": p["Status since"],
                       "Segments": p[LJS.SEGMENTS_PROP]}, "Ready For QA", ts1) == {}
    # 3) stale out-of-order event (older than Status since) is a no-op
    assert plan_event({**base, "Collected Status": p["Collected Status"], "Status since": p["Status since"]},
                      "Testing", "2026-09-25T09:30:00.000Z") == {}
    # 4) 5-minute interval survives minute rounding
    p2 = plan_event({**base, "Collected Status": p["Collected Status"], "Status since": p["Status since"],
                     "Segments": p[LJS.SEGMENTS_PROP]}, "Testing", ts2)
    segs2 = LJS.segments_from_props(p2)
    assert len(segs2) == 2 and segs2[1]["from"].startswith("2026-09-25T10:00") and segs2[1]["to"].startswith("2026-09-25T10:05"), segs2
    assert 0 < LJS.round_days((LJS.parse_ts(ts2) - LJS.parse_ts(ts1)).total_seconds() / 86400) < 0.01
    # 5) repeats never merged: transition back appends another Development segment
    p3 = plan_event({**base, "Collected Status": p2["Collected Status"], "Status since": p2["Status since"],
                     "Segments": p2[LJS.SEGMENTS_PROP]}, "Development", "2026-09-25T11:00:00.000Z")
    names = [x["s"] for x in LJS.segments_from_props(p3)]
    assert names == ["Development", "Ready For QA", "Testing"], names
    # 6) Done: diamond set once (immutable), reopen clears it
    pd = plan_event({**base, "Collected Status": p3["Collected Status"], "Status since": p3["Status since"]},
                    "Done", "2026-09-25T12:00:00.000Z")
    assert pd["Done at"]["date"]["start"].startswith("2026-09-25T12:00"), pd["Done at"]
    frozen = plan_event({**base, "Done at": {"date": {"start": "2026-09-24T08:00:00.000Z"}},
                         "Collected Status": {"rich_text": [{"plain_text": "Testing"}]}},
                        "Done", "2026-09-25T12:00:00.000Z")
    assert frozen.get("Done at") is None  # not rewritten (immutable)
    reopen = plan_event({**base, "Collected Status": {"rich_text": [{"plain_text": "Done"}]},
                         "Done at": {"date": {"start": "2026-09-24T08:00:00.000Z"}}},
                        "Development", "2026-09-25T13:00:00.000Z")
    assert reopen["Done at"] == {"date": None}, reopen["Done at"]
    # 7) first see = seed only
    seed = plan_event({"Done at": {"date": None}}, "New", ts0)
    assert LJS.rich_text_plain(seed["Collected Status"]) == "New" and LJS.SEGMENTS_PROP not in seed
    print(json.dumps({"ok": True, "mode": "selftest"}))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("selftest", "test"):
        selftest()
    else:
        sys.exit(main())
