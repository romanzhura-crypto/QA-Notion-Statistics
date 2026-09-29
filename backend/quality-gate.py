#!/usr/bin/env python3
"""A27 quality gate for release-data.json (board #183, additive).

Reads the `quality` block produced by release-widget-sync.py snapshot and
FAILs (exit 1) when a metric breaches its threshold. Wired into GH Actions
job "Sync Notion" between snapshot and Pages deploy — a bad snapshot never
reaches the widgets.

Usage:
  python3 quality-gate.py <release-data.json>   # gate one snapshot
  python3 quality-gate.py selftest              # synthetic PASS+FAIL probes

Hard rules (no silent quality debt):
  - task_count_delta_pct        > thresholds.task_count_delta_pct  -> FAIL
  - segments_negative           > 0                                -> FAIL
  - segments_absurd             > 0                                -> FAIL
  - tasks_without_history       > 0                                -> FAIL
  - drift_days (when present)   > thresholds.drift_days             -> FAIL (N2)
  - a19_conflicts (when present) > 0                               -> FAIL (A19)
  - quality.webhook.gap_hours   > thresholds.gap_hours              -> FAIL (N1)
  - quality.webhook.events_error > 0                               -> FAIL
  - quality.webhook == null (or gap_hours == null) -> N1 check SKIPPED (webhook not observed yet)
N1 gap_hours is DELIVERY-based (board #229): hours since the last delivered
event incl. synthetic worker pings (last_delivery_at; fallback last_event_at for
old run-state). A silent Notion with a live delivery chain never FAILs; a dead
chain (no deliveries > gap limit) does.
WARN-only: truncated_titles (cosmetic), fixtures_excluded (informational),
unknown_statuses (listed, dwell kept — see contract).
Empty placeholder payload (task_count == 0) is SKIP (exit 0): nothing to gate.
"""
import json
import sys

WARN_ONLY = ("truncated_titles", "fixtures_excluded", "unknown_statuses")


def evaluate(payload: dict) -> tuple[str, list]:
    """Return (verdict, problems). verdict: PASS | FAIL | SKIP."""
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return "FAIL", ["payload missing or ok != true"]
    if int(payload.get("task_count") or 0) == 0:
        return "SKIP", []  # empty placeholder (wiped demo board) — nothing to gate
    q = payload.get("quality")
    if not isinstance(q, dict):
        return "FAIL", ["quality block missing (A27) — snapshot too old?"]
    th = q.get("thresholds") or {}
    problems = []

    def check_max(metric, limit, name=None):
        val = q.get(metric)
        if val is None or limit is None:
            return  # metric not produced by this snapshot generation
        if float(val) > float(limit):
            problems.append(f"{name or metric}: {val} > {limit}")

    delta_lim = th.get("task_count_delta_pct")
    if q.get("task_count_prev") is not None and delta_lim is not None:
        check_max("task_count_delta_pct", delta_lim)
    check_max("segments_negative", 0)
    check_max("segments_absurd", 0)
    check_max("tasks_without_history", 0)
    check_max("drift_days", th.get("drift_days"))  # N2: Segments vs columns
    check_max("a19_conflicts", 0)  # A19: concurrent close collisions
    # N1: gap_hours lives in quality.webhook (board #184.3/#229). webhook == null
    # → the webhook pipeline is not deployed/observed yet → skip, never
    # false-fail. gap_hours is delivery-based (fresh pings keep it green while
    # Notion is silent); FAIL when the delivery chain is dead.
    wh = q.get("webhook")
    if isinstance(wh, dict):
        gap = wh.get("gap_hours")
        gap_lim = th.get("gap_hours")
        if gap is not None and gap_lim is not None and float(gap) > float(gap_lim):
            problems.append(f"webhook.gap_hours: {gap} > {gap_lim}")
        if int(wh.get("events_error") or 0) > 0:
            problems.append(f"webhook.events_error: {wh.get('events_error')} > 0")

    for m in WARN_ONLY:
        v = q.get(m)
        if v and (isinstance(v, (int, float)) and v > 0 or isinstance(v, list) and v):
            print(f"warning (non-blocking): {m} = {v if not isinstance(v, list) else v[:10]}", file=sys.stderr)
    return ("FAIL" if problems else "PASS"), problems


def main(argv: list) -> int:
    if len(argv) == 2 and argv[1] == "selftest":
        return selftest()
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        payload = json.loads(open(argv[1], encoding="utf-8").read())
    except (OSError, ValueError) as e:
        print(f"FAIL cannot read snapshot: {e}", file=sys.stderr)
        return 1
    verdict, problems = evaluate(payload)
    for p in problems:
        print(f"FAIL {p}", file=sys.stderr)
    print(json.dumps({"ok": verdict != "FAIL", "gate": verdict, "problems": problems}))
    return 1 if verdict == "FAIL" else 0


def selftest() -> int:
    th = {"task_count_delta_pct": 10.0, "gap_hours": 6.0, "drift_days": 1.0}

    def q(**kw):
        base = {"thresholds": th, "segments_negative": 0, "segments_absurd": 0,
                "tasks_without_history": 0, "task_count_prev": 100,
                "task_count_delta_pct": 0.0, "truncated_titles": 0,
                "fixtures_excluded": 0, "unknown_statuses": [],
                "drift_days": 0.0, "drift_items": 0, "webhook": None}
        base.update(kw)
        return {"ok": True, "task_count": 100, "quality": base}

    def wh(**kw):
        base = {"events_total": 5, "events_processed": 4, "events_noop": 1,
                "events_skipped": 0, "events_error": 0, "events_ping": 0,
                "last_event_at": "2026-09-27T06:00:00.000Z",
                "last_delivery_at": "2026-09-27T08:00:00.000Z", "gap_hours": 1.0}
        base.update(kw)
        return base

    cases = [
        ("PASS", q()),
        ("PASS", q(truncated_titles=8)),  # WARN-only
        ("PASS", q(unknown_statuses=["Code Freeze"])),  # WARN-only (listed)
        ("PASS", q(webhook=wh())),  # N1 green
        ("PASS", q(webhook=wh(gap_hours=None, last_event_at=None))),  # unknown gap → skip
        ("PASS", q(webhook=wh(  # N1 board #229: quiet Notion, fresh pings → PASS
            last_event_at="2026-09-20T00:00:00.000Z",
            last_delivery_at="2026-09-27T08:00:00.000Z",
            events_ping=12, gap_hours=1.0))),
        ("PASS", q(webhook=wh(  # old run-state (no last_delivery_at) → fallback, same verdict
            last_delivery_at=None, events_ping=0, gap_hours=1.0))),
        ("FAIL", q(task_count_delta_pct=15.0)),
        ("FAIL", q(segments_negative=1)),
        ("FAIL", q(segments_absurd=1)),
        ("FAIL", q(tasks_without_history=2)),
        ("FAIL", q(webhook=wh(gap_hours=7.5))),  # N1: delivery chain dead
        ("FAIL", q(webhook=wh(events_error=1))),  # handler failures
        ("FAIL", q(drift_days=2.0)),  # N2
        ("FAIL", q(a19_conflicts=1)),  # A19
        ("FAIL", {"ok": True, "task_count": 100}),  # quality missing
        ("FAIL", {"ok": False, "task_count": 100, "quality": q()["quality"]}),
        ("SKIP", {"ok": True, "task_count": 0, "quality": q()["quality"]}),
    ]
    for want, payload in cases:
        got, problems = evaluate(payload)
        assert got == want, (want, got, problems, payload)
    print(json.dumps({"ok": True, "mode": "selftest", "cases": len(cases)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
