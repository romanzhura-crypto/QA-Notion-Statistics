#!/usr/bin/env python3
"""Multi-base widget profiles tests (board #263 chunk A).

Covers the «база = профиль» parameterization end to end, offline:
  * widget_profile.py       — resolution order, per-profile state files
  * release-widget-sync.py  — DS id / artifact dir (1c/) per profile,
                              payload data_source_id = profile id
  * log-statistics-sync.py  — LOG DS + run-state/journal per profile
  * status-webhook-event.py — webhook run-state per profile + target guard
  * quality-gate.py         — PASS on a JSON inside the 1c/ subdirectory

Backward compat: the default profile (no WIDGET_PROFILE) resolves to the exact
legacy values/paths — the prod sprint contour is untouched.

Run: python3 tests/test_widget_profiles.py  → PASS (exit 0)
Offline: no Notion calls, no network, no writes outside tmp dirs.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = next(p for p in (ROOT / "scripts", ROOT / "backend") if p.is_dir())

SPRINT_DSID = "2a8e17b6-8482-80b2-87ad-000b68f9d74e"
SPRINT_LOG_DSID = "3dee17b6-8482-80a3-9fc4-000bafe19b46"
ONE_C_DSID = "f2be17b6-8482-82e2-9892-07777ffeaa90"
ONE_C_LOG_DSID = "c98e17b6-8482-820e-9f34-072fdef5db94"

CHECKS = 0


def check(cond, msg: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        raise AssertionError(f"check #{CHECKS} failed: {msg}")
    print(f"  ok #{CHECKS} {msg}")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class env_patch:
    """Temporarily set/unset env vars (restores on exit)."""

    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.kw}
        for k, v in self.kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def fresh(name: str, filename: str):
    """Load a script module fresh (module-level profile globals follow env)."""
    return _load(name, SCRIPTS / filename)


WP_ENV_KEYS = (
    "WIDGET_PROFILE", "NOTION_DATA_SOURCE_ID", "LOG_STATISTICS_DSID",
    "WIDGETS_JSON", "QA_WWW_JSON", "WIDGETS_JOURNAL", "LOG_STATS_RUN_STATE",
    "LOG_STATS_JOURNAL", "WEBHOOK_RUN_STATE", "WIDGET_PROFILES_CONFIG",
)


def clean_env(**extra):
    """Env patch that CLEARS every profile-related var, then applies extras."""
    kw = {k: None for k in WP_ENV_KEYS}
    kw.update(extra)
    return env_patch(**kw)


def test_profile_resolution() -> None:
    print("widget_profile: resolution order + per-profile state files")
    with clean_env():
        wp = fresh("wp_a", "widget_profile.py")
        ctx = wp.context()
        check(ctx["profile"] == "sprint", "default profile is sprint")
        check(ctx["data_source_id"] == SPRINT_DSID, f"sprint DS id legacy: {ctx['data_source_id']}")
        check(ctx["log_dsid"] == SPRINT_LOG_DSID, f"sprint LOG DS id legacy: {ctx['log_dsid']}")
        check(ctx["artifact_dir"] == ".", "sprint artifact_dir is root")
        check(ctx["target"] == "sprint", "sprint target tag")
        check(ctx["ws_json"] == ROOT / "widgets" / "release-data.json",
              f"sprint ws_json legacy path: {ctx['ws_json']}")
        check(ctx["log_run_state"] == ROOT / "config" / "log-statistics-run.json",
              f"sprint run-state legacy path: {ctx['log_run_state']}")
        check(ctx["webhook_run_state"] == ROOT / "config" / "status-webhook-run.json",
              f"sprint webhook run-state legacy path: {ctx['webhook_run_state']}")
        check(ctx["widgets_journal"] == ROOT / "config" / "release-widget-journal.jsonl",
              "sprint widgets journal legacy path")

    with clean_env(WIDGET_PROFILE="1c"):
        wp = fresh("wp_b", "widget_profile.py")
        ctx = wp.context()
        check(ctx["profile"] == "1c" and ctx["target"] == "1c", "1c profile + target tag")
        check(ctx["data_source_id"] == ONE_C_DSID, f"1c DS id: {ctx['data_source_id']}")
        check(ctx["log_dsid"] == ONE_C_LOG_DSID, f"1c LOG STATISTICS_APS_1C DS id: {ctx['log_dsid']}")
        check(ctx["artifact_dir"] == "1c", "1c artifact_dir = 1c")
        check(ctx["ws_json"] == ROOT / "widgets" / "1c" / "release-data.json",
              f"1c artifact in subdirectory: {ctx['ws_json']}")
        check(str(ctx["out_json"]).endswith("/widgets/1c/release-data.json"),
              f"1c out_json in subdirectory: {ctx['out_json']}")
        for key in ("log_run_state", "log_journal", "widgets_journal", "webhook_run_state"):
            check(ctx[key].name.endswith("-1c" + ctx[key].suffix),
                  f"1c {key} has profile suffix: {ctx[key].name}")

    # the two profiles never share a run-state/journal file
    with clean_env():
        sprint_ctx = fresh("wp_c", "widget_profile.py").context()
    with clean_env(WIDGET_PROFILE="1c"):
        one_c_ctx = fresh("wp_d", "widget_profile.py").context()
    sprint_files = {str(sprint_ctx[k]) for k in ("log_run_state", "log_journal", "widgets_journal", "webhook_run_state", "ws_json")}
    one_c_files = {str(one_c_ctx[k]) for k in ("log_run_state", "log_journal", "widgets_journal", "webhook_run_state", "ws_json")}
    check(sprint_files.isdisjoint(one_c_files), "profiles never share state/artifact files")

    # env overrides beat the profile entry
    with clean_env(WIDGET_PROFILE="1c", NOTION_DATA_SOURCE_ID="env-dsid-1", LOG_STATISTICS_DSID="env-log-1"):
        ctx = fresh("wp_e", "widget_profile.py").context()
        check(ctx["data_source_id"] == "env-dsid-1", "NOTION_DATA_SOURCE_ID override wins")
        check(ctx["log_dsid"] == "env-log-1", "LOG_STATISTICS_DSID override wins")
    with clean_env(NOTION_DATA_SOURCE_ID="env-dsid-2"):
        ctx = fresh("wp_f", "widget_profile.py").context()
        check(ctx["data_source_id"] == "env-dsid-2", "override wins on the default profile too")


def fake_task_row(p: str) -> dict:
    return dict(
        id="11111111-2222-3333-4444-555555555555",
        url="https://www.notion.so/x",
        created_time="2026-10-01T08:00:00.000Z",
        last_edited_time="2026-10-01T09:00:00.000Z",
        properties={
            "Documentation": {"type": "title", "title": [{"plain_text": "Task X"}]},
            "Status": {"type": "status", "status": {"name": "Development"}},
            "ID": {"type": "unique_id", "unique_id": {"prefix": p, "number": 7}},
            "Release": {"type": "multi_select", "multi_select": [{"name": "R1"}]},
            "Estimate": {"type": "rich_text", "rich_text": [{"plain_text": "4"}]},
            "Time spent (h)": {"type": "formula", "formula": {"number": 1.5}},
        },
    )


def fake_log_row(task_id: str) -> dict:
    """Matching LOG row so the task gets a dwell history (gate input)."""
    return dict(
        id="22222222-3333-4444-5555-666666666666",
        properties={
            "Task": {"type": "relation", "relation": [{"id": task_id}]},
            "Collected Status": {"type": "rich_text", "rich_text": [{"plain_text": "Development"}]},
            "Status since": {"type": "date", "date": {"start": "2026-10-01T09:00:00.000Z"}},
            "Segments": {"type": "rich_text", "rich_text": [{"plain_text": ""}]},
            "Done at": {"type": "date", "date": None},
        },
    )


def stub_pages(rel_mod, task_rows: list, log_rows: list) -> None:
    """Offline query_pages stub: task DS -> task rows, LOG DS -> log rows."""
    def fake_query_pages(dsid, fetch=None):
        return (list(task_rows), 0) if dsid == rel_mod.DSID else (list(log_rows), 0)
    rel_mod.query_pages = fake_query_pages
    rel_mod._req = lambda method, path, body=None: {"properties": {}}


def test_release_sync_per_profile() -> None:
    print("release-widget-sync: DS id + artifact dir per profile")
    with clean_env():
        rel = fresh("rel_sprint", "release-widget-sync.py")
        check(rel.DSID == SPRINT_DSID, "default profile DS id = legacy sprint")
        check(rel.WS_JSON == ROOT / "widgets" / "release-data.json", "default profile artifact path unchanged")
        check(rel.LOG_DSID == SPRINT_LOG_DSID, "default profile LOG DS id unchanged")

    with clean_env(WIDGET_PROFILE="1c"):
        rel = fresh("rel_1c", "release-widget-sync.py")
        check(rel.DSID == ONE_C_DSID, f"1c profile DS id: {rel.DSID}")
        check(rel.LOG_DSID == ONE_C_LOG_DSID, f"1c profile LOG DS id: {rel.LOG_DSID}")
        check(rel.WS_JSON == ROOT / "widgets" / "1c" / "release-data.json", "1c artifact path has 1c/ subdir")
        check(rel.WIDGETS_JOURNAL.name.endswith("-1c.jsonl"), "1c widgets journal is per profile")

        # snapshot build writes data_source_id = PROFILE id into the 1c/ JSON
        with tempfile.TemporaryDirectory() as td:
            troot = Path(td)
            with clean_env(WIDGET_PROFILE="1c", ROOT=str(troot)):
                rel2 = fresh("rel_1c_build", "release-widget-sync.py")
                row = fake_task_row("APS-1C")
                stub_pages(rel2, [row], [fake_log_row(row["id"])])
                payload = rel2.snapshot()
                check(payload["data_source_id"] == ONE_C_DSID,
                      f"payload data_source_id = profile id: {payload['data_source_id']}")
                check(payload["log_data_source_id"] == ONE_C_LOG_DSID, "payload log_data_source_id = profile LOG id")
                out = troot / "widgets" / "1c" / "release-data.json"
                check(out.is_file(), f"artifact written under 1c/ subdirectory: {out}")
                data = json.loads(out.read_text(encoding="utf-8"))
                check(data["data_source_id"] == ONE_C_DSID, "JSON data_source_id = profile id")
                check(isinstance(data.get("tasks"), list) and len(data["tasks"]) == 1, "task carried through")
                # quality-gate accepts the JSON inside the subdirectory
                gate = subprocess.run(
                    [sys.executable, str(SCRIPTS / "quality-gate.py"), str(out)],
                    capture_output=True, text=True, timeout=60,
                )
                check(gate.returncode == 0 and '"gate": "PASS"' in gate.stdout,
                      f"quality-gate PASS on 1c/ JSON: {gate.stdout.strip()}")
                # state files created for the 1c profile never touch sprint paths
                check(not (troot / "config" / "log-statistics-run.json").exists(),
                      "sprint run-state untouched by a 1c run")

    # default profile: backward-compat build writes the ROOT artifact (no subdir)
    with tempfile.TemporaryDirectory() as td:
        troot = Path(td)
        with clean_env(ROOT=str(troot)):
            rel3 = fresh("rel_sprint_build", "release-widget-sync.py")
            row = fake_task_row("TASK")
            stub_pages(rel3, [row], [fake_log_row(row["id"])])
            payload = rel3.snapshot()
            out = troot / "widgets" / "release-data.json"
            check(payload["data_source_id"] == SPRINT_DSID, "default build keeps legacy DS id")
            check(out.is_file() and not (troot / "widgets" / "1c").exists(),
                  "default build artifact stays in the artifact root (contract unchanged)")


def test_log_collector_per_profile() -> None:
    print("log-statistics-sync: LOG DS + run-state per profile")
    with clean_env():
        log = fresh("log_sprint", "log-statistics-sync.py")
        check(log.LOG_DSID == SPRINT_LOG_DSID, "default LOG DS id unchanged")
        check(log.RUN_STATE == ROOT / "config" / "log-statistics-run.json", "default run-state path unchanged")
        check(log.JOURNAL == ROOT / "config" / "log-statistics-journal.jsonl", "default journal path unchanged")
        check(log.SNAPSHOT == ROOT / "widgets" / "release-data.json", "default snapshot path unchanged")
    with clean_env(WIDGET_PROFILE="1c"):
        log = fresh("log_1c", "log-statistics-sync.py")
        check(log.LOG_DSID == ONE_C_LOG_DSID, f"1c LOG DS id: {log.LOG_DSID}")
        check(log.RUN_STATE.name == "log-statistics-run-1c.json", f"1c run-state: {log.RUN_STATE.name}")
        check(log.JOURNAL.name == "log-statistics-journal-1c.jsonl", f"1c journal: {log.JOURNAL.name}")
        check(log.SNAPSHOT == ROOT / "widgets" / "1c" / "release-data.json", "1c reads the 1c/ snapshot")


def test_webhook_per_profile() -> None:
    print("status-webhook-event: run-state per profile + target guard")
    with clean_env():
        wh = fresh("wh_sprint", "status-webhook-event.py")
        check(wh._state_path() == ROOT / "config" / "status-webhook-run.json", "default webhook run-state unchanged")
        check(wh.LJS.LOG_DSID == SPRINT_LOG_DSID, "default webhook LOG DS unchanged")
    with clean_env(WIDGET_PROFILE="1c"):
        wh = fresh("wh_1c", "status-webhook-event.py")
        check(wh._state_path().name == "status-webhook-run-1c.json", f"1c webhook run-state: {wh._state_path().name}")
        check(wh.LJS.LOG_DSID == ONE_C_LOG_DSID, "1c webhook writes LOG STATISTICS_APS_1C")

    # target guard: a mismatched target tag is skipped BEFORE any Notion work,
    # counters still recorded, monotonic run-state untouched otherwise.
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "run.json"
        ev = json.dumps({
            "type": "page", "entity_id": "x", "entity_type": "page",
            "target": "1c", "timestamp": "2026-10-09T10:00:00.000Z",
        })
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "status-webhook-event.py")],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "EVENT_JSON": ev, "WEBHOOK_RUN_STATE": str(state), "WIDGET_PROFILE": "sprint"},
        )
        out = json.loads(r.stdout.strip().splitlines()[-1])
        check(r.returncode == 0 and out.get("skipped") == "target mismatch",
              f"sprint handler skips a 1c-target event: {out}")
        st = json.loads(state.read_text(encoding="utf-8"))
        check(st.get("events_skipped") == 1 and "last_event_at" not in st,
              "skip is counted, business clock never moves")

        # matching target passes the guard (in-process, offline: Notion seams
        # stubbed — the handler only observes "no LOG row", never writes)
        ev2 = json.dumps({
            "type": "page", "entity_id": "no-such-page", "entity_type": "page",
            "target": "1c", "timestamp": "2026-10-09T10:00:00.000Z",
        })
        state2 = Path(td) / "run-1c.json"
        import contextlib
        import io

        with clean_env(WIDGET_PROFILE="1c", EVENT_JSON=ev2, WEBHOOK_RUN_STATE=str(state2)):
            wh2 = fresh("wh_1c_main", "status-webhook-event.py")
            wh2.LJS.query_all = lambda dsid: []
            wh2.LJS.log_number_cols = lambda: set()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc2 = wh2.main()
        out2 = json.loads(buf.getvalue().strip().splitlines()[-1])
        check(rc2 == 0 and out2.get("skipped") == "no LOG row for page",
              f"1c handler accepts a 1c-target event past the guard: {out2}")


def test_quality_gate_profiles() -> None:
    print("quality-gate: identical contract for both profile artifacts")
    with tempfile.TemporaryDirectory() as td:
        th = {"task_count_delta_pct": 10.0, "gap_hours": 6.0, "segments_negative": 0,
              "segments_absurd": 0, "drift_days": 1.0, "staleness_hours": 12.0, "recon_drift_hours": 6.0}
        q = dict(thresholds=th, segments_negative=0, segments_absurd=0, tasks_without_history=0,
                 task_count_prev=10, task_count_delta_pct=0.0, webhook=None)
        for rel_dir in ("", "1c"):
            base = Path(td) / rel_dir if rel_dir else Path(td)
            base.mkdir(parents=True, exist_ok=True)
            f = base / "release-data.json"
            f.write_text(json.dumps({"ok": True, "task_count": 10, "quality": q}), encoding="utf-8")
            r = subprocess.run([sys.executable, str(SCRIPTS / "quality-gate.py"), str(f)],
                               capture_output=True, text=True, timeout=60)
            check(r.returncode == 0 and '"gate": "PASS"' in r.stdout,
                  f"quality-gate PASS for artifact at ./{rel_dir + '/' if rel_dir else ''}release-data.json")
            bad = dict(q, segments_negative=1)
            f.write_text(json.dumps({"ok": True, "task_count": 10, "quality": bad}), encoding="utf-8")
            r2 = subprocess.run([sys.executable, str(SCRIPTS / "quality-gate.py"), str(f)],
                                capture_output=True, text=True, timeout=60)
            check(r2.returncode == 1 and '"gate": "FAIL"' in r2.stdout,
                  f"quality-gate FAIL is enforced at ./{rel_dir + '/' if rel_dir else ''}release-data.json")


def main() -> int:
    test_profile_resolution()
    test_release_sync_per_profile()
    test_log_collector_per_profile()
    test_webhook_per_profile()
    test_quality_gate_profiles()
    print(json.dumps({"ok": True, "result": "PASS", "checks": CHECKS}, ensure_ascii=False))
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
