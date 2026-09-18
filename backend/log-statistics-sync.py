#!/usr/bin/env python3
"""Sync Sprint snapshot → LOG STATISTICS (wide by-task table).

Title format on CREATE (and empty-row reuse): Sprit №{run}({job}) {DD.MM.YYYY HH:MM:SS}
  timezone Europe/Minsk. Orthography "Sprit" is required by owner.
  Incremental PATCH does not rewrite Name every run.

Default mode: incremental
  - CREATE if Sprint id is missing in LOG
  - PATCH only if Status / dwell days / Done at differ
  - Archive LOG rows with no Task, duplicate Task, or Task id not in Sprint
  - Count change is not a full-rewrite switch

Dwell: current Status only (calendar days from Start date / created_time
until now, or last_edited_time if Done). Does not invent Version-history
intervals. Other status number columns are set to 0.

Token: config/notion.json. Never printed.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("ROOT") or "/home/chuck/.openclaw/workspace")
CONFIG = Path(os.environ.get("NOTION_CONFIG") or (ROOT / "config" / "notion.json"))
SNAPSHOT = Path(os.environ.get("WIDGETS_JSON") or (ROOT / "widgets" / "release-data.json"))
RUN_STATE = Path(os.environ.get("LOG_STATS_RUN_STATE") or (ROOT / "config" / "log-statistics-run.json"))
LOG_DSID = os.environ.get("LOG_STATISTICS_DSID") or "3dee17b6-8482-80a3-9fc4-000bafe19b46"
MINSK = ZoneInfo("Europe/Minsk")
WRITE_SLEEP_S = float(os.environ.get("LOG_STATS_WRITE_SLEEP_S") or "0.35")
DAYS_EPS = float(os.environ.get("LOG_STATS_DAYS_EPS") or "1.0")

STATUS_COLS = [
    "New",
    "Investigate",
    "Analysis/Design",
    "Tech Solution",
    "Product Review",
    "Ready For Dev",
    "ETA",
    "Development",
    "Ready For QA",
    "Testing",
    "Failed",
    "Code Review",
    "Merged",
    "Ready For Release",
]


def _cfg():
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    if not isinstance(cfg, dict):
        cfg = {}
    token = (os.environ.get("NOTION_TOKEN") or cfg.get("token") or "").strip()
    if not token:
        raise RuntimeError("notion token missing")
    version = os.environ.get("NOTION_VERSION") or cfg.get("notion_version") or "2026-03-11"
    return token, version


def _req(method: str, path: str, body=None, timeout: int = 60):
    token, version = _cfg()
    data = None if body is None else json.dumps(body).encode("utf-8")
    url = "https://api.notion.com" + path
    last = (0, {"error": "no_attempt"})
    for attempt in range(6):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Notion-Version", version)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                obj = {"raw": raw[:800]}
            last = (e.code, obj)
            if e.code == 429 and attempt < 5:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = float(retry_after) if retry_after else (2 ** attempt)
                except (TypeError, ValueError):
                    wait = 2 ** attempt
                time.sleep(min(max(wait, 0.5), 30.0))
                continue
            return e.code, obj
        except TimeoutError as e:
            last = (0, {"error": type(e).__name__})
            if attempt < 5:
                time.sleep(min(2 ** attempt, 8.0))
                continue
            return 0, last[1]
    return last


def load_run_state() -> dict:
    if RUN_STATE.exists():
        try:
            data = json.loads(RUN_STATE.read_text())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {"last_run": 0}


def save_run_state(data: dict) -> None:
    RUN_STATE.parent.mkdir(parents=True, exist_ok=True)
    RUN_STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def next_run_number(github_run_number: int | None) -> int:
    if github_run_number and github_run_number > 0:
        return github_run_number
    st = load_run_state()
    n = int(st.get("last_run") or 0) + 1
    return n


def format_title(run_no: int, job: str, when: datetime) -> str:
    local = when.astimezone(MINSK)
    stamp = local.strftime("%d.%m.%Y %H:%M:%S")
    return f"Sprit №{run_no}({job}) {stamp}"


def parse_ts(v):
    if not v:
        return None
    s = str(v).strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        s = s + "T00:00:00+00:00"
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def days_in_status(task: dict, now: datetime | None = None) -> float:
    start = parse_ts(task.get("start")) or parse_ts(task.get("created"))
    if not start:
        return 0.0
    done = str(task.get("s") or "").lower() == "done"
    end = parse_ts(task.get("edited")) if done else (now or datetime.now(timezone.utc))
    if end is None:
        end = now or datetime.now(timezone.utc)
    ms = max(0.0, (end - start).total_seconds() * 1000.0)
    return round((ms / 86400000.0) * 10) / 10.0


def query_all(dsid: str) -> list:
    rows = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        code, q = _req("POST", f"/v1/data_sources/{dsid}/query", body)
        if code != 200:
            raise RuntimeError(f"query {dsid} HTTP {code} {q.get('code')} {q.get('message')}")
        rows.extend(q.get("results") or [])
        if not q.get("has_more"):
            break
        cursor = q.get("next_cursor")
        time.sleep(0.2)
    return rows


def relation_ids(prop: dict) -> list[str]:
    return [x.get("id") for x in (prop.get("relation") or []) if x.get("id")]


def index_log_rows(rows: list) -> tuple[dict, list[str], list[str]]:
    """task_id → row; empty pages; duplicate extra page ids."""
    mapping: dict[str, dict] = {}
    empties: list[str] = []
    dups: list[str] = []
    for row in rows:
        if row.get("archived") or row.get("in_trash"):
            continue
        props = row.get("properties") or {}
        ids = relation_ids(props.get("Task") or {})
        pid = row.get("id")
        if not pid:
            continue
        if not ids:
            empties.append(pid)
            continue
        tid = ids[0]
        if tid in mapping:
            dups.append(pid)
        else:
            mapping[tid] = row
    return mapping, empties, dups


def existing_by_task(rows: list):
    """Back-compat: task_id → page_id, empty page ids."""
    mapping, empties, _dups = index_log_rows(rows)
    return {tid: row.get("id") for tid, row in mapping.items()}, empties


def number_props_for(task: dict, now: datetime | None = None) -> dict:
    status = task.get("s") or ""
    days = days_in_status(task, now=now)
    out = {}
    for col in STATUS_COLS:
        out[col] = {"number": days if col == status else 0}
    return out


def done_at_prop(task: dict) -> dict:
    if str(task.get("s") or "").lower() != "done":
        return {"date": None}
    edited = parse_ts(task.get("edited")) or parse_ts(task.get("start")) or parse_ts(task.get("created"))
    if not edited:
        return {"date": None}
    return {"date": {"start": edited.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}}


def title_prop(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text[:2000]}}]}


def page_properties(task: dict, title: str | None, now: datetime | None = None) -> dict:
    props = {
        "Task": {"relation": [{"id": task["id"]}]},
    }
    if title:
        props["Name"] = title_prop(title)
    props.update(number_props_for(task, now=now))
    props["Done at"] = done_at_prop(task)
    return props


def log_status_days(props: dict) -> tuple[str | None, float | None]:
    found = None
    days = None
    for col in STATUS_COLS:
        raw = (props.get(col) or {}).get("number")
        if raw is None:
            continue
        try:
            n = float(raw)
        except (TypeError, ValueError):
            continue
        if n > 0:
            found = col
            days = n
    return found, days


def log_done_at(props: dict) -> str | None:
    d = (props.get("Done at") or {}).get("date") or {}
    return d.get("start") if d else None


def norm_dt_key(v) -> str | None:
    dt = parse_ts(v)
    if not dt:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def needs_update(task: dict, log_row: dict, now: datetime | None = None) -> bool:
    props = log_row.get("properties") or {}
    want_status = task.get("s") or ""
    want_done = want_status.lower() == "done"
    want_days = days_in_status(task, now=now)
    have_status, have_days = log_status_days(props)
    have_done = log_done_at(props)
    want_done_start = None
    done_payload = done_at_prop(task).get("date")
    if isinstance(done_payload, dict):
        want_done_start = done_payload.get("start")

    if want_done:
        if have_status is not None:
            return True
        return norm_dt_key(have_done) != norm_dt_key(want_done_start)

    if have_done:
        return True
    # All-zero LOG row cannot encode current Status; skip if dwell is also ~0.
    if have_status is None:
        return want_days >= DAYS_EPS
    if have_status != want_status:
        return True
    if abs((have_days or 0.0) - want_days) >= DAYS_EPS:
        return True
    return False


def upsert(task: dict, title: str | None, page_id: str | None, now: datetime | None = None) -> tuple[str, int, str | None]:
    props = page_properties(task, title, now=now)
    if page_id:
        code, obj = _req("PATCH", f"/v1/pages/{page_id}", {"properties": props})
        return "updated", code, obj.get("id") if code == 200 else f"{obj.get('code')}: {obj.get('message')}"
    body = {
        "parent": {"type": "data_source_id", "data_source_id": LOG_DSID},
        "properties": props,
    }
    if "Name" not in props:
        props["Name"] = title_prop(title or "Sprit")
    code, obj = _req("POST", "/v1/pages", body)
    return "created", code, obj.get("id") if code == 200 else f"{obj.get('code')}: {obj.get('message')}"


def archive_page(page_id: str) -> tuple[int, str]:
    code, obj = _req("PATCH", f"/v1/pages/{page_id}", {"archived": True})
    if code == 200:
        return code, "archived"
    return code, f"{obj.get('code')}: {obj.get('message')}"


def load_tasks() -> tuple[list, object, object]:
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    tasks = []
    for t in data.get("tasks") or []:
        tid = str(t.get("id") or "")
        if not tid or tid.startswith("table-test"):
            continue
        tasks.append(t)
    return tasks, data.get("generated_at"), data.get("task_count")


def env_flag(name: str, default: str = "0") -> bool:
    return (os.environ.get(name) or default).strip().lower() not in ("0", "false", "no", "")


def main():
    run_no = int(os.environ.get("LOG_STATS_RUN") or "0")
    job = (os.environ.get("LOG_STATS_JOB") or "").strip()
    gh_run_number = os.environ.get("GITHUB_RUN_NUMBER")
    gh_run_number_i = int(gh_run_number) if (gh_run_number or "").isdigit() else None
    if run_no <= 0:
        run_no = next_run_number(gh_run_number_i)
    if not job:
        job = (os.environ.get("GITHUB_RUN_ID") or os.environ.get("LOG_STATS_JOB_FALLBACK") or "local").strip()
    when = datetime.now(MINSK)
    now_utc = datetime.now(timezone.utc)
    title = (os.environ.get("LOG_STATS_TITLE") or "").strip() or format_title(run_no, job, when)
    skip_existing = env_flag("LOG_STATS_SKIP_EXISTING", "0")
    full = env_flag("LOG_STATS_FULL", "0")
    rewrite_title = env_flag("LOG_STATS_REWRITE_TITLE", "0")
    # Legacy: SKIP_EXISTING=0 used to mean "patch every row". That is now FULL.
    if (os.environ.get("LOG_STATS_SKIP_EXISTING") or "").strip() == "0" and not env_flag("LOG_STATS_INCREMENTAL", "0"):
        # explicit 0 no longer forces full rewrite; incremental is default
        skip_existing = False
    tasks, generated_at, snap_count = load_tasks()
    rows = query_all(LOG_DSID)
    mapping, empties, dups = index_log_rows(rows)
    created = updated = failed = skipped = 0
    archived_empty = archived_dup = archived_unlinked = 0
    errors = []
    reuse_empty = list(empties)
    sprint_ids = {t["id"] for t in tasks}

    def record_err(item: dict, code: int, info: str):
        nonlocal failed
        failed += 1
        if len(errors) < 15:
            errors.append({**item, "http": code, "err": info})

    def write_sleep():
        if WRITE_SLEEP_S > 0:
            time.sleep(WRITE_SLEEP_S)

    for pid in dups:
        code, msg = archive_page(pid)
        if code == 200:
            archived_dup += 1
        else:
            record_err({"id": pid, "kind": "dup"}, code, msg)
        write_sleep()

    for tid, row in list(mapping.items()):
        if tid in sprint_ids:
            continue
        pid = row.get("id")
        code, msg = archive_page(pid)
        if code == 200:
            archived_unlinked += 1
            mapping.pop(tid, None)
        else:
            record_err({"id": pid, "kind": "unlinked", "task": tid}, code, msg)
        write_sleep()

    for i, task in enumerate(tasks):
        log_row = mapping.get(task["id"])
        page_id = log_row.get("id") if log_row else None
        was_empty = False
        if page_id and skip_existing and not full:
            skipped += 1
            if (i + 1) % 50 == 0:
                print(json.dumps({
                    "progress": i + 1, "created": created, "updated": updated,
                    "skipped": skipped, "failed": failed,
                }, ensure_ascii=False), flush=True)
            continue
        if page_id and not full and not needs_update(task, log_row, now=now_utc):
            skipped += 1
            if (i + 1) % 50 == 0:
                print(json.dumps({
                    "progress": i + 1, "created": created, "updated": updated,
                    "skipped": skipped, "failed": failed,
                }, ensure_ascii=False), flush=True)
            continue
        if not page_id and reuse_empty:
            page_id = reuse_empty.pop(0)
            was_empty = True
        use_title = title if (not log_row or was_empty or rewrite_title or not page_id) else None
        action, code, info = upsert(task, use_title, page_id, now=now_utc)
        if code == 200:
            if action == "created":
                created += 1
            else:
                updated += 1
            mapping[task["id"]] = {"id": info, "properties": {}}
        else:
            record_err({"id": task.get("id"), "n": task.get("n")}, code, info)
        write_sleep()
        if (i + 1) % 50 == 0:
            print(json.dumps({
                "progress": i + 1, "created": created, "updated": updated,
                "skipped": skipped, "failed": failed,
            }, ensure_ascii=False), flush=True)

    for pid in reuse_empty:
        code, msg = archive_page(pid)
        if code == 200:
            archived_empty += 1
        else:
            record_err({"id": pid, "kind": "empty"}, code, msg)
        write_sleep()

    st = load_run_state()
    st.update({
        "last_run": run_no,
        "last_job": job,
        "last_title": title,
        "last_at": when.isoformat(),
        "last_created": created,
        "last_updated": updated,
        "last_failed": failed,
        "last_skipped": skipped,
        "last_archived_empty": archived_empty,
        "last_archived_dup": archived_dup,
        "last_archived_unlinked": archived_unlinked,
        "mode": "full" if full else ("skip_existing" if skip_existing else "incremental"),
        "snapshot_generated_at": generated_at,
    })
    save_run_state(st)
    out = {
        "ok": failed == 0,
        "mode": st["mode"],
        "title": title,
        "run": run_no,
        "job": job,
        "tasks": len(tasks),
        "snapshot_generated_at": generated_at,
        "snapshot_task_count": snap_count,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "archived_empty": archived_empty,
        "archived_dup": archived_dup,
        "archived_unlinked": archived_unlinked,
        "failed": failed,
        "errors": errors,
        "notion": "https://app.notion.com/p/3dee17b68482809fb688e89182385a15",
        "status_dwell": "current_status_calendar_days",
    }
    print(json.dumps(out, ensure_ascii=False))
    if failed:
        raise SystemExit(2)


def selftest() -> None:
    now = parse_ts("2026-09-18T08:00:00Z")
    task_dev = {
        "id": "aaa",
        "s": "Development",
        "start": "2026-09-10T00:00:00Z",
        "created": "2026-09-10T00:00:00Z",
        "edited": "2026-09-17T00:00:00Z",
    }
    days = days_in_status(task_dev, now=now)
    assert days == 8.3, days
    matching = {
        "properties": {
            "Development": {"number": 8.3},
            "New": {"number": 0},
            "Done at": {"date": None},
            "Task": {"relation": [{"id": "aaa"}]},
        }
    }
    assert needs_update(task_dev, matching, now=now) is False
    stale_days = {
        "properties": {
            "Development": {"number": 6.0},
            "Done at": {"date": None},
        }
    }
    assert needs_update(task_dev, stale_days, now=now) is True
    wrong_status = {
        "properties": {
            "Testing": {"number": 8.0},
            "Done at": {"date": None},
        }
    }
    assert needs_update(task_dev, wrong_status, now=now) is True
    task_done = {
        "id": "bbb",
        "s": "Done",
        "start": "2026-09-01T00:00:00Z",
        "created": "2026-09-01T00:00:00Z",
        "edited": "2026-09-15T12:00:00.000Z",
    }
    done_row = {
        "properties": {
            "Development": {"number": 0},
            "Done at": {"date": {"start": "2026-09-15T12:00:00.000Z"}},
        }
    }
    assert needs_update(task_done, done_row, now=now) is False
    done_with_days = {
        "properties": {
            "Development": {"number": 3.0},
            "Done at": {"date": {"start": "2026-09-15T12:00:00.000Z"}},
        }
    }
    assert needs_update(task_done, done_with_days, now=now) is True
    zero_task = {
        "id": "ccc",
        "s": "New",
        "start": "2026-09-18T07:50:00Z",
        "created": "2026-09-18T07:50:00Z",
        "edited": "2026-09-18T07:50:00Z",
    }
    zero_row = {"properties": {"New": {"number": 0}, "Done at": {"date": None}}}
    assert days_in_status(zero_task, now=now) < 1.0
    assert needs_update(zero_task, zero_row, now=now) is False
    drift = {
        "properties": {
            "Development": {"number": 8.9},
            "Done at": {"date": None},
        }
    }
    assert needs_update(task_dev, drift, now=now) is False  # |8.9-8.3| < 1.0 day
    rows = [
        {"id": "p1", "properties": {"Task": {"relation": [{"id": "aaa"}]}}},
        {"id": "p2", "properties": {"Task": {"relation": [{"id": "aaa"}]}}},
        {"id": "p3", "properties": {"Task": {"relation": []}}},
        {"id": "p4", "archived": True, "properties": {"Task": {"relation": [{"id": "zzz"}]}}},
    ]
    mapping, empties, dups = index_log_rows(rows)
    assert list(mapping) == ["aaa"]
    assert empties == ["p3"]
    assert dups == ["p2"]
    print(json.dumps({"ok": True, "mode": "selftest", "days": days}, ensure_ascii=False))


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if cmd in ("selftest", "test"):
        selftest()
    else:
        main()
