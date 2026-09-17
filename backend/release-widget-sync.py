#!/usr/bin/env python3
"""QA-only Notion snapshot for Release widgets. Token never printed.

Architecture: iframe is static HTML+JSON on a public TLS origin.
This process talks to Notion REST and writes release-data.json.
Do not expose /sync on the same public vhost as OpenClaw for Notion embeds.
HTTP serve remains loopback-only for operators on QA, not for the iframe.
"""
from __future__ import annotations

import json
import os
import re
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("ROOT") or os.environ.get("WIDGETS_ROOT") or Path(__file__).resolve().parents[1])
CONFIG = Path(os.environ.get("NOTION_CONFIG") or (ROOT / "config" / "notion.json"))
OUT_JSON = Path(os.environ.get("QA_WWW_JSON") or "/var/www/openclaw/widgets/release-data.json")
WS_JSON = Path(os.environ.get("WIDGETS_JSON") or (ROOT / "frontend" / "release-data.json"))
TABLE_TESTS = Path(os.environ.get("TABLE_TESTS") or (ROOT / "frontend" / "status-dwell-table-tests.json"))
DEFAULT_DSID = "2a8e17b6-8482-80b2-87ad-000b68f9d74e"
LOG_DSID = os.environ.get("LOG_STATISTICS_DSID") or "3dee17b6-8482-80a3-9fc4-000bafe19b46"
LISTEN = ("127.0.0.1", 8755)
# Number columns in LOG STATISTICS (New … Ready For Release). Done is date, not a dwell bar.
LOG_STATUS_COLS = [
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
STATUS_DWELL_NOTE = "dwell numbers from LOG STATISTICS 3dee17b6 when present"


def resolve_dsid(cfg: dict | None = None) -> str:
    env = (os.environ.get("NOTION_DATA_SOURCE_ID") or "").strip()
    if env:
        return env
    data = cfg
    if data is None and CONFIG.exists():
        loaded = json.loads(CONFIG.read_text())
        data = loaded if isinstance(loaded, dict) else {}
    if isinstance(data, dict):
        for key in ("widget_data_source_id", "data_source_id"):
            val = str(data.get(key) or "").strip()
            if val:
                return val
    return DEFAULT_DSID


DSID = resolve_dsid()


def _prop_status_name(prop) -> str | None:
    if not prop:
        return None
    t = prop.get("type")
    if t == "status":
        return ((prop.get("status") or {}).get("name"))
    if t == "select":
        return ((prop.get("select") or {}).get("name"))
    if t == "formula":
        f = prop.get("formula") or {}
        return f.get("string")
    if t == "rollup":
        roll = prop.get("rollup") or {}
        arr = roll.get("array") or []
        for item in arr:
            name = _prop_status_name(item)
            if name:
                return name
        if roll.get("type") == "string":
            return roll.get("string")
    return None


def history_from_log_row(props: dict) -> list:
    """Build widget history from one LOG STATISTICS row. Skip invented Version history."""
    history = []
    for col in LOG_STATUS_COLS:
        raw = (props.get(col) or {}).get("number")
        if raw is None:
            continue
        try:
            days = float(raw)
        except (TypeError, ValueError):
            continue
        if days > 0:
            history.append({"s": col, "days": days})
    status_name = _prop_status_name(props.get("Status")) or _prop_status_name(props.get("Current Status"))
    done_at = prop_date_start(props.get("Done at"))
    if (status_name and str(status_name).lower() == "done") or done_at:
        diamond = {"s": "Done"}
        if done_at:
            diamond["at"] = done_at
        history.append(diamond)
    return history


def query_log_rows(dsid: str | None = None) -> list:
    target = dsid or LOG_DSID
    rows = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        q = _req("POST", f"/v1/data_sources/{target}/query", body)
        rows.extend(q.get("results") or [])
        if not q.get("has_more"):
            break
        cursor = q.get("next_cursor")
        time.sleep(0.2)
    return rows


def log_history_by_task(rows: list) -> dict:
    """Task relation id → history. Last non-archived LOG row wins."""
    mapping = {}
    for row in rows:
        if row.get("archived") or row.get("in_trash"):
            continue
        props = row.get("properties") or {}
        rel = props.get("Task") or {}
        ids = [x.get("id") for x in (rel.get("relation") or []) if x.get("id")]
        if not ids:
            continue
        hist = history_from_log_row(props)
        if hist:
            mapping[ids[0]] = hist
    return mapping


def enrich_with_log_statistics(payload: dict, log_rows: list | None = None) -> dict:
    """Attach tasks[].history from LOG STATISTICS. Tasks without LOG rows keep calendar fallback."""
    rows = log_rows if log_rows is not None else query_log_rows()
    by_task = log_history_by_task(rows)
    matched = 0
    for task in payload.get("tasks") or []:
        tid = str(task.get("id") or "")
        if not tid or tid.startswith("table-test"):
            continue
        hist = by_task.get(tid) or by_task.get(task.get("id"))
        if not hist:
            task.pop("history", None)
            continue
        task["history"] = hist
        matched += 1
    payload["status_dwell"] = "log_statistics_when_present"
    payload["status_dwell_note"] = STATUS_DWELL_NOTE
    payload["log_data_source_id"] = LOG_DSID
    payload["log_history_tasks"] = matched
    payload["log_row_count"] = len(rows)
    return payload


def write_payload(payload: dict) -> dict:
    text = json.dumps(payload, ensure_ascii=False)
    WS_JSON.parent.mkdir(parents=True, exist_ok=True)
    WS_JSON.write_text(text, encoding="utf-8")
    try:
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return payload


def enrich_existing() -> dict:
    """Dry-merge LOG dwell onto the current Sprint JSON without re-querying the Sprint DS."""
    payload = json.loads(WS_JSON.read_text(encoding="utf-8"))
    payload = enrich_with_log_statistics(payload)
    payload["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = merge_table_tests(payload)
    return write_payload(payload)


def merge_table_tests(payload: dict) -> dict:
    """Append <=5 QA rows into the table snapshot so the widget can read them via DATA.tasks."""
    if not TABLE_TESTS.exists():
        return payload
    extra = json.loads(TABLE_TESTS.read_text(encoding="utf-8"))
    tests = extra.get("tasks") or []
    if not tests:
        return payload
    ids = {t.get("id") for t in tests if t.get("id")}
    live = [t for t in (payload.get("tasks") or []) if t.get("id") not in ids]
    payload["tasks"] = live + tests
    from_tasks = set(payload.get("releases") or [])
    for tsk in tests:
        from_tasks.update(r for r in (tsk.get("r") or []) if r)
    payload["releases"] = sorted(from_tasks, key=rel_sort_key)
    payload["task_count"] = len(payload["tasks"])
    payload["table_tests"] = len(tests)
    return payload


def _cfg():
    cfg = {}
    if CONFIG.exists():
        cfg = json.loads(CONFIG.read_text())
        if not isinstance(cfg, dict):
            cfg = {}
    token = (os.environ.get("NOTION_TOKEN") or cfg.get("token") or "").strip()
    if not token:
        raise RuntimeError("notion token missing (set NOTION_TOKEN or config/notion.json)")
    version = (
        os.environ.get("NOTION_VERSION")
        or cfg.get("notion_version")
        or "2026-03-11"
    )
    return token, version


def _req(method: str, path: str, body=None):
    import urllib.request

    token, version = _cfg()
    url = "https://api.notion.com" + path
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", "Bearer " + token)
    r.add_header("Notion-Version", version)
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read().decode() or "{}")


def parse_estimate(text):
    if not text:
        return None
    t = text.strip().lower().replace(",", ".")
    t = t.replace("часов", "h").replace("часа", "h").replace("час", "h").replace("ч", "h")
    t = t.replace("дней", "d").replace("дня", "d").replace("день", "d")
    t = t.replace("минут", "m").replace("минуты", "m").replace("мин", "m")
    hours = 0.0
    matched = False
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*d", t):
        hours += float(m.group(1)) * 8.0
        matched = True
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*h", t):
        hours += float(m.group(1))
        matched = True
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*m", t):
        hours += float(m.group(1)) / 60.0
        matched = True
    if matched:
        return round(hours, 2)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)", t)
    return round(float(m.group(1)), 2) if m else None


def rel_sort_key(name: str):
    if name == "Backlog":
        return (0, 0, 0)
    m = re.match(r"^(\d+)\.(\d+)$", name)
    if m:
        return (1, -int(m.group(2)), -int(m.group(1)))
    return (2, 0, name)


def prop_date_start(prop) -> str | None:
    d = (prop or {}).get("date") or {}
    return d.get("start") if d else None


def snapshot() -> dict:
    rows = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        q = _req("POST", f"/v1/data_sources/{DSID}/query", body)
        rows.extend(q.get("results") or [])
        if not q.get("has_more"):
            break
        cursor = q.get("next_cursor")

    tasks = []
    from_tasks = set()
    for row in rows:
        p = row.get("properties") or {}
        releases = [x.get("name") for x in ((p.get("Release") or {}).get("multi_select") or []) if x.get("name")]
        from_tasks.update(releases)
        est_txt = "".join(x.get("plain_text", "") for x in (p.get("Estimate") or {}).get("rich_text") or []).strip()
        spent = (p.get("Time spent (h)") or {}).get("formula") or {}
        spent_h = spent.get("number")
        if spent_h is None:
            secs = (p.get("Time tracking (secs)") or {}).get("number")
            spent_h = round(secs / 3600.0, 2) if secs else None
        else:
            try:
                spent_h = round(float(spent_h), 2)
            except Exception:
                spent_h = None
        title = "".join(x.get("plain_text", "") for x in (p.get("Documentation") or {}).get("title") or [])
        status = ((p.get("Status") or {}).get("status") or {}).get("name") or "Unknown"
        tasks.append({
            "id": row.get("id"),
            "r": releases,
            "d": ((p.get("DEV") or {}).get("select") or {}).get("name") or "Unassigned",
            "s": status,
            "e": parse_estimate(est_txt),
            "t": spent_h,
            "n": title[:140],
            "start": prop_date_start(p.get("Start date")),
            "created": row.get("created_time"),
            "edited": row.get("last_edited_time"),
        })
    ds = _req("GET", f"/v1/data_sources/{DSID}")
    status_opts = (((ds.get("properties") or {}).get("Status") or {}).get("status") or {}).get("options") or []
    status_meta = [{"name": o.get("name"), "color": o.get("color") or "default"} for o in status_opts if o.get("name")]
    payload = {
        "ok": True,
        "source": "Estimate vs Time tracking",
        "data_source_id": DSID,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "task_count": len(tasks),
        "estimate_rule": "1d=8h",
        "status_dwell": "current_status_calendar_days",
        "status_dwell_note": STATUS_DWELL_NOTE,
        "status_meta": status_meta,
        "releases": sorted(from_tasks, key=rel_sort_key),
        "tasks": tasks,
    }
    payload = merge_table_tests(payload)
    payload = enrich_with_log_statistics(payload)
    return write_payload(payload)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, code: int, obj: dict):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self._send(200, {"ok": True})

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        if self.path.split("?")[0] not in ("/sync", "/"):
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            payload = snapshot()
            self._send(200, {
                "ok": True,
                "generated_at": payload["generated_at"],
                "task_count": payload["task_count"],
                "releases": payload["releases"],
                "tasks": payload["tasks"],
                "source": payload["source"],
                "estimate_rule": payload["estimate_rule"],
                "status_dwell": payload.get("status_dwell"),
                "status_dwell_note": payload.get("status_dwell_note"),
                "status_meta": payload.get("status_meta") or [],
            })
        except Exception as exc:
            self._send(500, {"ok": False, "error": str(exc), "trace": traceback.format_exc()[-500:]})


def serve():
    httpd = ThreadingHTTPServer(LISTEN, Handler)
    httpd.serve_forever()


def dry_check() -> dict:
    """Config/path sanity without printing secrets and without calling Notion."""
    cfg = {}
    if CONFIG.exists():
        try:
            loaded = json.loads(CONFIG.read_text())
            if isinstance(loaded, dict):
                cfg = loaded
        except json.JSONDecodeError:
            cfg = {}
    token = (os.environ.get("NOTION_TOKEN") or cfg.get("token") or "").strip()
    return {
        "ok": True,
        "mode": "dry",
        "config_present": CONFIG.exists(),
        "token_present": bool(token),
        "token_len": len(token),
        "token_source": "env" if os.environ.get("NOTION_TOKEN") else ("file" if cfg.get("token") else "none"),
        "database_id_set": bool(cfg.get("database_id") or os.environ.get("NOTION_DATABASE_ID")),
        "data_source_id": DSID,
        "root": str(ROOT),
        "listen": f"{LISTEN[0]}:{LISTEN[1]}",
        "out_json": str(OUT_JSON),
        "ws_json": str(WS_JSON),
        "log_data_source_id": LOG_DSID,
        "note": "HTTP serve is loopback-only. Notion iframe must not call /sync. CI uses NOTION_TOKEN env.",
    }


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    if cmd == "serve":
        serve()
    elif cmd in ("dry", "dry-run"):
        print(json.dumps(dry_check(), ensure_ascii=False))
    elif cmd in ("snapshot", "sync"):
        p = snapshot()
        print(json.dumps({
            "ok": True,
            "generated_at": p["generated_at"],
            "task_count": p["task_count"],
            "releases": p["releases"],
            "log_history_tasks": p.get("log_history_tasks"),
            "status_dwell_note": p.get("status_dwell_note"),
        }, ensure_ascii=False))
    elif cmd in ("enrich", "enrich-log"):
        p = enrich_existing()
        print(json.dumps({
            "ok": True,
            "mode": "enrich",
            "generated_at": p["generated_at"],
            "task_count": p["task_count"],
            "log_row_count": p.get("log_row_count"),
            "log_history_tasks": p.get("log_history_tasks"),
            "status_dwell": p.get("status_dwell"),
            "status_dwell_note": p.get("status_dwell_note"),
        }, ensure_ascii=False))
    else:
        raise SystemExit("usage: release-widget-sync.py [snapshot|enrich|dry|serve]")
