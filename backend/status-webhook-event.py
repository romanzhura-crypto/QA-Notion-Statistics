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
from datetime import datetime, timezone
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

# Widget profile (board #263 chunk A): the webhook handler writes the LOG
# STATISTICS base of the SELECTED profile (WIDGET_PROFILE, default "sprint" =
# exact legacy behavior). LOG DS comes from LJS.LOG_DSID (profile-resolved);
# the run-state file is per profile (suffix -<profile> for non-default).
import sys as _sys  # noqa: E402

_sys.path.insert(0, str(Path(__file__).resolve().parent))
import widget_profile as wp  # noqa: E402

PROFILE = wp.profile_name()
PROFILE_CTX = wp.context()
# Webhook run-state (board #184.2): durable counters + last event timestamp.
# GH Actions FS is ephemeral — CI restores/saves this file via actions/cache
# (board #184.4); on QA VM it just lives on disk. Additive observability for
# quality.webhook (N1 gap detection); never contains tokens.
WEBHOOK_RUN_STATE_DEFAULT = PROFILE_CTX["webhook_run_state"]


def _state_path() -> Path:
    # env resolved per call: tests may redirect; CI restores/saves via actions/cache
    return Path(os.environ.get("WEBHOOK_RUN_STATE") or WEBHOOK_RUN_STATE_DEFAULT)


def run_state_read() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def run_state_note(outcome: str, event_ts=None, ping: bool = False, now=None) -> dict:
    """Merge one handled event into the run-state. outcome: processed|noop|skipped|error.

    event_ts = the Notion event timestamp (exact), not handler wall-clock.
    ping = synthetic delivery heartbeat (worker cron, board #229): outcome is
    "noop", plus additive events_ping; it is NOT a business event.
    last_delivery_at = handler wall-clock of ANY delivered event
    (processed/noop/ping/skipped — errors excluded), monotonic max: N1 gap
    measures the DELIVERY chain, a silent Notion must not fail the gate.
    last_event_at = real business events only (processed/noop, non-ping),
    monotonic — unchanged semantics. now = ISO override for deterministic tests.
    Always returns the fresh state (for logging); never raises."""
    state = run_state_read()
    state["events_total"] = int(state.get("events_total") or 0) + 1
    state[f"events_{outcome}"] = int(state.get(f"events_{outcome}") or 0) + 1
    if ping:
        state["events_ping"] = int(state.get("events_ping") or 0) + 1
    ts = LJS.iso_z(LJS.parse_ts(event_ts)) if event_ts else None
    if ts and outcome in ("processed", "noop") and not ping:
        # noop still proves delivery alive — only errors do not refresh the gap
        prev = state.get("last_event_at")
        if not prev or ts > str(prev):
            state["last_event_at"] = ts
    if outcome in ("processed", "noop", "skipped"):
        # any DELIVERED event (incl. skipped + synthetic ping) proves the chain
        # alive — only handler errors do not refresh the delivery clock
        dl = LJS.iso_z(LJS.parse_ts(now)) if now else LJS.iso_z(datetime.now(timezone.utc))
        prev_dl = state.get("last_delivery_at")
        if not prev_dl or dl > str(prev_dl):
            state["last_delivery_at"] = dl
    try:
        target = _state_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)  # atomic: readers see full JSON only
    except OSError:
        pass  # degraded: metrics lost, event handling itself must not fail
    return state


def _status_name(prop) -> str | None:
    """Status/select name from a Notion property (keep in sync with
    release-widget-sync._prop_status_name — that module is snapshot-side)."""
    if not prop:
        return None
    t = prop.get("type")
    if t == "status":
        return (prop.get("status") or {}).get("name")
    if t == "select":
        return (prop.get("select") or {}).get("name")
    return None


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
    # Delivery order is not guaranteed and aggregated events may repeat with
    # different timestamps (docs: Event ordering / retries). Keep boundaries
    # strictly monotonic: never overlap the previous segment.
    last_to = None
    if segs:
        last_to = LJS.parse_ts(segs[-1].get("to"))
    base_from = LJS.parse_ts(have_since or ts)
    if last_to and base_from and base_from < last_to:
        base_from = last_to
    seg_from = LJS.iso_z(base_from)
    seg_to = LJS.iso_z(ts)
    if last_to and LJS.parse_ts(seg_to) and LJS.parse_ts(seg_to) < last_to:
        seg_to = LJS.iso_z(last_to)
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
    since_ts = LJS.parse_ts(ts)
    if last_to and since_ts and since_ts < last_to:
        since_ts = last_to
    out["Status since"] = LJS.date_prop(since_ts)

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
    if str(ev.get("type") or "").lower() == "ping" or ev.get("kind") == "ping":
        # Synthetic heartbeat from the worker cron (board #229): proves the
        # delivery chain alive. No Notion/Segments/A19 work — ever.
        st = run_state_note("noop", ev.get("timestamp"), ping=True)
        print(json.dumps({"ok": True, "ping": True, "state": st}))
        return 0
    entity_id = str(ev.get("entity_id") or "")
    entity_type = str(ev.get("entity_type") or "page")
    # Optional target tag (board #263): the payload may declare its profile
    # ("sprint" | "1c"). A mismatch is routed to the wrong handler instance —
    # count as skipped, never touch Segments (idempotence preserved). Untagged
    # events are legacy single-base traffic: only the DEFAULT profile processes
    # them, non-default instances skip (never double-apply across bases).
    ev_target = str(ev.get("target") or "").strip()
    if ev_target and ev_target != str(PROFILE_CTX["target"]):
        run_state_note("skipped", ev.get("timestamp"))
        print(json.dumps({"ok": True, "skipped": "target mismatch", "target": ev_target}))
        return 0
    if not ev_target and str(PROFILE) != wp.DEFAULT_PROFILE:
        run_state_note("skipped", ev.get("timestamp"))
        print(json.dumps({"ok": True, "skipped": "untagged event, non-default profile"}))
        return 0
    if not entity_id or entity_type != "page":
        run_state_note("skipped", ev.get("timestamp"))
        print(json.dumps({"ok": True, "skipped": "not a page event"}))
        return 0

    LJS.log_number_cols()  # schema check: never PATCH a missing number column
    rows = LJS.query_all(LJS.LOG_DSID)
    mapping, _empties, _dups = LJS.index_log_rows(rows)
    log_row = mapping.get(entity_id)
    if not log_row:
        run_state_note("skipped", ev.get("timestamp"))
        print(json.dumps({"ok": True, "skipped": "no LOG row for page"}))
        return 0

    code, page = LJS._req("GET", f"/v1/pages/{entity_id}")
    if code != 200:
        run_state_note("error", ev.get("timestamp"))
        print(json.dumps({"ok": False, "error": f"page fetch {code}"}))
        return 1
    props = page.get("properties") or {}
    status = (
        _status_name(props.get("Status"))
        or _status_name(props.get("Current Status"))
        or "Unknown"
    )
    patch = plan_event(log_row.get("properties") or {}, str(status), ev.get("timestamp"))
    if not patch:
        run_state_note("noop", ev.get("timestamp"))
        print(json.dumps({"ok": True, "noop": True, "status": status}))
        return 0
    # A19 (board #185): optimistic concurrent write — recompute the patch from
    # the VERIFIED fresh row on every attempt (a concurrent collector close is
    # merged, never clobbered); bounded retry, then fail-visible 409.
    def build(row: dict) -> dict:
        return plan_event((row or {}).get("properties") or {}, str(status), ev.get("timestamp"))

    with LJS.a19_lock():
        code, obj = LJS.patch_with_a19(log_row["id"], build, log_row)
    ok = code == 200
    run_state_note("processed" if ok else "error", ev.get("timestamp"))
    conflict = code == 409 and str(obj).startswith("a19:")
    print(json.dumps({
        "ok": ok,
        "status": status,
        "patched": sorted(patch.keys()),
        "a19_conflict": conflict,
        "error": None if ok else (obj if conflict else str((obj or {}).get("message") or obj)),
    }, default=str))
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
    # 8) monotonic boundaries: an out-of-order/duplicate event never overlaps
    # the previous segment (docs: delivery order is not guaranteed)
    mono = plan_event({
        "Collected Status": {"rich_text": [{"plain_text": "Testing"}]},
        "Status since": {"date": {"start": "2026-09-25T11:58:00.000Z"}},
        "Segments": LJS.segments_prop([{"s": "Development", "from": "2026-09-25T11:55:00.000Z", "to": "2026-09-25T11:58:57.000Z"}]),
        "Done at": {"date": None},
    }, "Ready For Dev", "2026-09-25T12:00:38.000Z")
    msegs = LJS.segments_from_props(mono)
    assert msegs[-1]["from"] == "2026-09-25T11:58:57.000Z", msegs  # clamped to previous to
    assert msegs[-1]["to"].startswith("2026-09-25T12:00"), msegs

    # 9) webhook run-state (board #184.2): counters + last_event_at monotonic,
    # atomic write, never raises; errors do not refresh the gap clock.
    import os, tempfile
    old_state = os.environ.get("WEBHOOK_RUN_STATE")
    with tempfile.TemporaryDirectory() as td:
        os.environ["WEBHOOK_RUN_STATE"] = str(Path(td) / "run.json")
        try:
            s = run_state_note("processed", ts1)
            assert s["events_total"] == 1 and s["events_processed"] == 1, s
            assert s["last_event_at"] == ts1, s
            # duplicate/repeat with older ts must NOT move last_event_at back
            s = run_state_note("noop", "2026-09-25T09:30:00.000Z")
            assert s["events_noop"] == 1 and s["last_event_at"] == ts1, s
            # newer event moves the clock forward
            s = run_state_note("processed", ts2)
            assert s["last_event_at"] == ts2 and s["events_total"] == 3, s
            # error counts but does NOT refresh the gap clock (N1 semantics)
            s = run_state_note("error", "2026-09-26T10:00:00.000Z")
            assert s["events_error"] == 1 and s["last_event_at"] == ts2, s
            s2 = run_state_read()  # durable across reads
            assert s2 == s, (s2, s)
            # skipped events count but never move the clock (nothing was applied)
            s = run_state_note("skipped", "2026-09-26T11:00:00.000Z")
            assert s["events_skipped"] == 1 and s["last_event_at"] == ts2, s
            # board #229 (N1 variant 1): delivery clock + synthetic ping.
            # Frozen future "now" values: the delivery clock is monotonic vs the
            # real wall clock written by the calls above.
            dl1 = "2030-01-01T00:00:00.000Z"
            s = run_state_note("noop", None, ping=True, now=dl1)
            assert s["events_ping"] == 1 and s["events_noop"] == 2, s
            assert s["last_delivery_at"] == dl1, s  # ping refreshes the gap clock
            assert s["last_event_at"] == ts2, s  # ping is NOT a business event
            s = run_state_note("error", None, now="2030-01-01T01:00:00.000Z")
            assert s["last_delivery_at"] == dl1, s  # errors do not prove delivery
            s = run_state_note("skipped", "2026-09-26T11:30:00.000Z", now="2029-12-31T23:00:00.000Z")
            assert s["last_delivery_at"] == dl1, s  # monotonic max: never moves back
            assert s["last_event_at"] == ts2, s  # skipped is NOT a business event
            s = run_state_note("processed", "2026-09-26T12:00:00.000Z", now="2030-01-01T02:00:00.000Z")
            assert s["last_delivery_at"] == "2030-01-01T02:00:00.000Z", s
            assert s["last_event_at"] == "2026-09-26T12:00:00.000Z", s
            # corrupt/unreadable file degrades to fresh state, never raises
            Path(os.environ["WEBHOOK_RUN_STATE"]).write_text("not json", encoding="utf-8")
            s = run_state_note("processed", ts1)
            assert s["events_total"] == 1, s  # restart from zero after corruption
        finally:
            if old_state is None:
                os.environ.pop("WEBHOOK_RUN_STATE", None)
            else:
                os.environ["WEBHOOK_RUN_STATE"] = old_state
    print(json.dumps({"ok": True, "mode": "selftest"}))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("selftest", "test"):
        selftest()
    else:
        sys.exit(main())
