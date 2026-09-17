#!/usr/bin/env python3
"""Sync Sprint snapshot → LOG STATISTICS (wide by-task table).

Title format (always): Sprit №{run}({job}) {DD.MM.YYYY HH:MM:SS}
  timezone Europe/Minsk. Orthography "Sprit" is required by owner.

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
    req = urllib.request.Request("https://api.notion.com" + path, data=data, method=method)
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
        return e.code, obj


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


def days_in_status(task: dict) -> float:
    start = parse_ts(task.get("start")) or parse_ts(task.get("created"))
    if not start:
        return 0.0
    done = str(task.get("s") or "").lower() == "done"
    end = parse_ts(task.get("edited")) if done else datetime.now(timezone.utc)
    if end is None:
        end = datetime.now(timezone.utc)
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


def existing_by_task(rows: list) -> dict[str, str]:
    mapping = {}
    empties = []
    for row in rows:
        if row.get("archived") or row.get("in_trash"):
            continue
        props = row.get("properties") or {}
        task = props.get("Task") or {}
        ids = relation_ids(task)
        pid = row.get("id")
        if ids:
            mapping[ids[0]] = pid
        else:
            empties.append(pid)
    return mapping, empties


def number_props_for(task: dict) -> dict:
    status = task.get("s") or ""
    days = days_in_status(task)
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


def page_properties(task: dict, title: str) -> dict:
    props = {
        "Name": title_prop(title),
        "Task": {"relation": [{"id": task["id"]}]},
    }
    props.update(number_props_for(task))
    props["Done at"] = done_at_prop(task)
    return props


def upsert(task: dict, title: str, page_id: str | None) -> tuple[str, int, str | None]:
    props = page_properties(task, title)
    if page_id:
        code, obj = _req("PATCH", f"/v1/pages/{page_id}", {"properties": props})
        return "updated", code, obj.get("id") if code == 200 else f"{obj.get('code')}: {obj.get('message')}"
    body = {
        "parent": {"type": "data_source_id", "data_source_id": LOG_DSID},
        "properties": props,
    }
    code, obj = _req("POST", "/v1/pages", body)
    return "created", code, obj.get("id") if code == 200 else f"{obj.get('code')}: {obj.get('message')}"


def archive_page(page_id: str) -> tuple[int, str]:
    code, obj = _req("PATCH", f"/v1/pages/{page_id}", {"archived": True})
    if code == 200:
        return code, "archived"
    return code, f"{obj.get('code')}: {obj.get('message')}"


def load_tasks() -> list:
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    tasks = []
    for t in data.get("tasks") or []:
        tid = str(t.get("id") or "")
        if not tid or tid.startswith("table-test"):
            continue
        tasks.append(t)
    return tasks, data.get("generated_at"), data.get("task_count")


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
    title = (os.environ.get("LOG_STATS_TITLE") or "").strip() or format_title(run_no, job, when)
    skip_existing = (os.environ.get("LOG_STATS_SKIP_EXISTING") or "1").strip().lower() not in ("0", "false", "no")
    tasks, generated_at, snap_count = load_tasks()
    rows = query_all(LOG_DSID)
    mapping, empties = existing_by_task(rows)
    created = updated = failed = archived = skipped = 0
    errors = []
    reuse_empty = list(empties)
    for i, task in enumerate(tasks):
        page_id = mapping.get(task["id"])
        if page_id and skip_existing:
            skipped += 1
            if (i + 1) % 50 == 0:
                print(json.dumps({"progress": i + 1, "created": created, "updated": updated, "skipped": skipped, "failed": failed}, ensure_ascii=False), flush=True)
            continue
        if not page_id and reuse_empty:
            page_id = reuse_empty.pop(0)
        action, code, info = upsert(task, title, page_id)
        if code == 200:
            if action == "created":
                created += 1
            else:
                updated += 1
            mapping[task["id"]] = info
        else:
            failed += 1
            if len(errors) < 15:
                errors.append({"id": task.get("id"), "n": task.get("n"), "http": code, "err": info})
            if code == 429:
                time.sleep(2.0)
        time.sleep(0.35)
        if (i + 1) % 50 == 0:
            print(json.dumps({"progress": i + 1, "created": created, "updated": updated, "skipped": skipped, "failed": failed}, ensure_ascii=False), flush=True)
    for pid in reuse_empty:
        code, msg = archive_page(pid)
        if code == 200:
            archived += 1
        else:
            errors.append({"id": pid, "err": msg, "http": code})
        time.sleep(0.2)
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
        "snapshot_generated_at": generated_at,
    })
    save_run_state(st)
    out = {
        "ok": failed == 0,
        "title": title,
        "run": run_no,
        "job": job,
        "tasks": len(tasks),
        "snapshot_generated_at": generated_at,
        "snapshot_task_count": snap_count,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "archived_empty": archived,
        "failed": failed,
        "errors": errors,
        "notion": "https://app.notion.com/p/3dee17b68482809fb688e89182385a15",
        "status_dwell": "current_status_calendar_days",
    }
    print(json.dumps(out, ensure_ascii=False))
    if failed:
        raise SystemExit(2)



if __name__ == "__main__":
    main()
