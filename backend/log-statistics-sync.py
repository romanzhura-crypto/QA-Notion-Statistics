#!/usr/bin/env python3
"""Sync Sprint snapshot → LOG STATISTICS (wide by-task table).

Title format on CREATE (and empty-row reuse): Sprit №{run}({job}) {DD.MM.YYYY HH:MM:SS}
  timezone Europe/Minsk. Orthography "Sprit" is required by owner.
  Incremental PATCH does not rewrite Name every run.

Default mode: incremental
  - CREATE if Sprint id is missing in LOG
  - PATCH if Collected Status / Status since / closed-segment days / Done at differ
  - Archive LOG rows with no Task, duplicate Task, or Task id not in Sprint
  - Count change is not a full-rewrite switch

Dwell collector (forward-looking, option C):
  State on each LOG row: Collected Status + Status since.
  On Status change: add elapsed (Status since → last_edited_time) to the
  previous status number column; do not zero other columns.
  First see: seed collector only — no invented Version History backfill.
  Open interval is not written into number cols; snapshot adds it from
  Status since. Done is date-only (diamond), not a dwell column.

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


def round_days(days: float) -> float:
    days = max(0.0, float(days) or 0.0)
    if days < 1.0:
        return round(days * 24.0) / 24.0
    return round(days * 10.0) / 10.0


def elapsed_days(start, end) -> float:
    a = start if isinstance(start, datetime) else parse_ts(start)
    b = end if isinstance(end, datetime) else parse_ts(end)
    if not a or not b:
        return 0.0
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return round_days(max(0.0, (b - a).total_seconds() / 86400.0))


def days_in_status(task: dict, now: datetime | None = None) -> float:
    start = parse_ts(task.get("start")) or parse_ts(task.get("created"))
    if not start:
        return 0.0
    done = str(task.get("s") or "").lower() == "done"
    end = parse_ts(task.get("edited")) if done else (now or datetime.now(timezone.utc))
    if end is None:
        end = now or datetime.now(timezone.utc)
    return elapsed_days(start, end)


def rich_text_plain(prop) -> str:
    if not prop:
        return ""
    arr = prop.get("rich_text") if isinstance(prop, dict) else None
    if not arr:
        return ""
    parts = []
    for x in arr:
        parts.append(x.get("plain_text") or ((x.get("text") or {}).get("content") or ""))
    return "".join(parts).strip()


def rich_text_prop(text: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": (text or "")[:2000]}}]}


def date_prop(dt) -> dict:
    parsed = dt if isinstance(dt, datetime) else parse_ts(dt)
    if not parsed:
        return {"date": None}
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return {"date": {"start": parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}}


def collected_from_props(props: dict) -> str | None:
    text = rich_text_plain(props.get("Collected Status"))
    return text or None


def since_from_props(props: dict):
    d = (props.get("Status since") or {}).get("date") or {}
    return parse_ts(d.get("start") if d else None)


def number_from_props(props: dict, col: str) -> float:
    raw = (props.get(col) or {}).get("number")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


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


def done_at_prop(task: dict) -> dict:
    if str(task.get("s") or "").lower() != "done":
        return {"date": None}
    edited = parse_ts(task.get("edited")) or parse_ts(task.get("start")) or parse_ts(task.get("created"))
    if not edited:
        return {"date": None}
    return date_prop(edited)


def title_prop(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text[:2000]}}]}


def collector_transition(task: dict, log_row: dict | None, now: datetime | None = None) -> dict:
    """Closed segments + collector seed. Never zeros other status columns.

    Returns number patches (only columns that change), Collected Status,
    Status since, Done at.
    """
    now = now or datetime.now(timezone.utc)
    status = (task.get("s") or "").strip()
    done = status.lower() == "done"
    edited = parse_ts(task.get("edited")) or now
    created = parse_ts(task.get("created")) or parse_ts(task.get("start")) or edited
    props = (log_row or {}).get("properties") or {}
    have_collected = collected_from_props(props)
    have_since = since_from_props(props)
    out: dict = {"Done at": done_at_prop(task)}

    if not have_collected:
        seed_status = "Done" if done else status
        # First see: do not invent Version History. Open tasks start the clock now.
        # Zero leftover calendar-only numbers so they are not shown as history.
        seed_since = edited if done else now
        for col in STATUS_COLS:
            out[col] = {"number": 0}
        out["Collected Status"] = rich_text_prop(seed_status)
        out["Status since"] = date_prop(seed_since)
        return out

    if have_collected == status or (done and have_collected.lower() == "done"):
        out["Collected Status"] = rich_text_prop(have_collected)
        out["Status since"] = date_prop(have_since or (edited if done else created))
        return out

    close_end = edited
    added = elapsed_days(have_since or created, close_end)
    if have_collected in STATUS_COLS and added > 0:
        prev = number_from_props(props, have_collected)
        out[have_collected] = {"number": round_days(prev + added)}
    new_status = "Done" if done else status
    out["Collected Status"] = rich_text_prop(new_status)
    out["Status since"] = date_prop(close_end)
    return out


def page_properties(task: dict, title: str | None, now: datetime | None = None, log_row: dict | None = None) -> dict:
    props = {
        "Task": {"relation": [{"id": task["id"]}]},
    }
    if title:
        props["Name"] = title_prop(title)
    props.update(collector_transition(task, log_row, now=now))
    return props


def log_done_at(props: dict) -> str | None:
    d = (props.get("Done at") or {}).get("date") or {}
    return d.get("start") if d else None


def norm_dt_key(v) -> str | None:
    dt = parse_ts(v)
    if not dt:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def needs_update(task: dict, log_row: dict, now: datetime | None = None) -> bool:
    want = collector_transition(task, log_row, now=now)
    have = log_row.get("properties") or {}
    want_collected = rich_text_plain(want.get("Collected Status"))
    have_collected = collected_from_props(have)
    if (want_collected or "") != (have_collected or ""):
        return True
    want_since = ((want.get("Status since") or {}).get("date") or {}).get("start")
    have_since = ((have.get("Status since") or {}).get("date") or {}).get("start")
    if not have_collected:
        return True
    if norm_dt_key(want_since) != norm_dt_key(have_since):
        return True
    want_done = ((want.get("Done at") or {}).get("date") or None)
    have_done = ((have.get("Done at") or {}).get("date") or None)
    want_done_start = want_done.get("start") if isinstance(want_done, dict) else None
    have_done_start = have_done.get("start") if isinstance(have_done, dict) else None
    if norm_dt_key(want_done_start) != norm_dt_key(have_done_start):
        return True
    for col in STATUS_COLS:
        if col not in want:
            continue
        try:
            want_n = float((want.get(col) or {}).get("number") or 0)
        except (TypeError, ValueError):
            want_n = 0.0
        have_n = number_from_props(have, col)
        if abs(want_n - have_n) >= DAYS_EPS:
            return True
    return False


def upsert(task: dict, title: str | None, page_id: str | None, now: datetime | None = None, log_row: dict | None = None) -> tuple[str, int, str | None]:
    props = page_properties(task, title, now=now, log_row=log_row)
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
    code, obj = _req("PATCH", f"/v1/pages/{page_id}", {"in_trash": True})
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
        action, code, info = upsert(task, use_title, page_id, now=now_utc, log_row=None if was_empty else log_row)
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
        "status_dwell": "collector_closed_segments",
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
    six_h = {
        "id": "ddd",
        "s": "New",
        "start": "2026-09-18T02:00:00Z",
        "created": "2026-09-18T02:00:00Z",
        "edited": "2026-09-18T02:00:00Z",
    }
    assert days_in_status(six_h, now=now) == 0.25

    empty = {"properties": {"Development": {"number": 8.3}, "Done at": {"date": None}}}
    assert needs_update(task_dev, empty, now=now) is True
    seed = collector_transition(task_dev, empty, now=now)
    assert rich_text_plain(seed["Collected Status"]) == "Development"
    assert seed["Development"]["number"] == 0
    assert seed["New"]["number"] == 0

    seeded = {
        "properties": {
            "Collected Status": seed["Collected Status"],
            "Status since": seed["Status since"],
            "Development": {"number": 0},
            "New": {"number": 0},
            "Done at": {"date": None},
        }
    }
    assert needs_update(task_dev, seeded, now=now) is False

    later = parse_ts("2026-09-20T08:00:00Z")
    task_test = {
        "id": "aaa",
        "s": "Testing",
        "start": "2026-09-10T00:00:00Z",
        "created": "2026-09-10T00:00:00Z",
        "edited": "2026-09-20T08:00:00Z",
    }
    change = collector_transition(task_test, seeded, now=later)
    assert rich_text_plain(change["Collected Status"]) == "Testing"
    assert change["Development"]["number"] == 2.0, change["Development"]
    assert "Testing" not in change  # open interval not written
    changed_row = {
        "properties": {
            **seeded["properties"],
            "Collected Status": change["Collected Status"],
            "Status since": change["Status since"],
            "Development": change["Development"],
        }
    }
    assert needs_update(task_test, changed_row, now=later) is False

    task_done = {
        "id": "aaa",
        "s": "Done",
        "start": "2026-09-10T00:00:00Z",
        "created": "2026-09-10T00:00:00Z",
        "edited": "2026-09-21T08:00:00.000Z",
    }
    to_done = collector_transition(task_done, changed_row, now=parse_ts("2026-09-21T08:00:00Z"))
    assert rich_text_plain(to_done["Collected Status"]) == "Done"
    assert to_done["Testing"]["number"] == 1.0, to_done
    assert to_done["Done at"]["date"]["start"].startswith("2026-09-21T08:00:00")
    done_row = {
        "properties": {
            **changed_row["properties"],
            "Collected Status": to_done["Collected Status"],
            "Status since": to_done["Status since"],
            "Testing": to_done["Testing"],
            "Done at": to_done["Done at"],
        }
    }
    assert needs_update(task_done, done_row, now=parse_ts("2026-09-21T08:00:00Z")) is False
    assert number_from_props(done_row["properties"], "Development") == 2.0

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
