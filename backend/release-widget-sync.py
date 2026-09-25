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
import sys
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("ROOT") or os.environ.get("WIDGETS_ROOT") or "/home/chuck/.openclaw/workspace")
CONFIG = Path(os.environ.get("NOTION_CONFIG") or (ROOT / "config" / "notion.json"))
OUT_JSON = Path(os.environ.get("QA_WWW_JSON") or "/var/www/openclaw/widgets/release-data.json")
WS_JSON = Path(os.environ.get("WIDGETS_JSON") or (ROOT / "widgets" / "release-data.json"))
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
STATUS_DWELL_NOTE = (
    "dwell: LOG collector closed segments + open interval from Status since. "
    "No Notion Version History backfill."
)
# Done is a terminal marker (Done at date), not a dwell bar. "Unknown" is the
# synthetic snapshot placeholder for a missing Status — not a Notion status.
KNOWN_STATUSES = set(LOG_STATUS_COLS) | {"Done", "Unknown"}


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


def parse_ts(v):
    if not v:
        return None
    s = str(v).strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        # Date-only = midnight Europe/Minsk (UTC+3, no DST), not midnight UTC:
        # removes ±3h day-boundary errors for display in Minsk.
        s = s + "T00:00:00+03:00"
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
    # Minute precision (was: whole hours <1 day, 0.1 day above). Hour rounding
    # turned <30-min segments into 0 and the widget dropped them entirely.
    return round(days * 1440.0) / 1440.0


def iso_z(dt) -> str | None:
    """ISO-8601 UTC (…Z) for segment boundaries; None-safe."""
    d = dt if isinstance(dt, datetime) else parse_ts(dt)
    if not d:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def segments_from_props(props: dict) -> list:
    """Closed dwell segments [{s, from, to}] from the Segments rich_text JSON.

    Tolerant to absent/corrupt values: bad data yields [], never an exception.
    """
    raw = rich_text_plain(props.get("Segments"))
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and item.get("s") and item.get("from") and item.get("to"):
            out.append({"s": str(item["s"]), "from": str(item["from"]), "to": str(item["to"])})
    return out


def is_number_prop(value) -> bool:
    """True for a Notion number property dict (dwell column), incl. bare {"number": x}."""
    return isinstance(value, dict) and "number" in value and value.get("type") in (None, "number")


def status_dwell_cols(props: dict) -> list:
    """Dwell columns from actual row data: hardcoded order first, then unknown (sorted).

    Statuses outside LOG_STATUS_COLS are collected dynamically instead of dropped.
    """
    cols = [c for c in LOG_STATUS_COLS if c in props]
    cols += sorted(c for c, v in props.items() if c not in LOG_STATUS_COLS and is_number_prop(v))
    return cols


# Fixture (synthetic/QA-test) row detector. Keep in sync with log-statistics-sync.py.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Title markers: «table-test» or «тест» (+ short adjective forms), optionally
# prefixed by brackets/dashes. Conservative: «тестирование», «тест-драйв» and
# mid-title «тест» do NOT match (legit tasks stay in stats).
_FIXTURE_TITLE_RE = re.compile(
    r"^[\s\[\(\-]*(?:table-test|тест(?:овая|овый|овое|ый|ая|ое)?(?![\w-]))",
    re.IGNORECASE,
)


def is_fixture_row(row) -> bool:
    """True for synthetic id (non-UUID / table-test*) or fixture title marker.
    Accepts Notion pages (properties title) and snapshot task dicts (n/title)."""
    if not isinstance(row, dict):
        return False
    rid = str(row.get("id") or "").strip()
    if not rid:
        return False
    if "table-test" in rid.lower():
        return True
    if not _UUID_RE.match(rid):
        return True
    title = str(row.get("n") or row.get("title") or "")
    if not title:
        for prop in (row.get("properties") or {}).values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                title = "".join(x.get("plain_text", "") for x in (prop.get("title") or []))
                break
    return bool(_FIXTURE_TITLE_RE.match(title or ""))


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


def history_from_log_row(props: dict, now=None, unknown: set | None = None) -> list:
    """Closed collector segments + open interval. No invented Version History.

    With Segments (rich_text JSON) each closed interval is its own history item
    {s, days, from, to} — repeats stay separate. Dwell number columns remain
    status aggregates; only the part not covered by segments (legacy accrual
    from before Segments existed) is emitted, WITHOUT invented timestamps.
    The open interval (Collected Status + Status since) is always its own item
    {s, days, from, to: null} — the moment the current status was obtained is
    collector-known even for legacy rows. Done is a diamond {s, at}, never a
    dwell bar.

    Statuses outside LOG_STATUS_COLS never lose time: their number columns and
    the open interval are collected dynamically; names go to `unknown` (if given).
    """
    now = now or datetime.now(timezone.utc)
    history = []
    collected = rich_text_plain(props.get("Collected Status"))
    since = parse_ts(prop_date_start(props.get("Status since")))
    segs = segments_from_props(props)
    covered = {}
    for seg in segs:
        a = parse_ts(seg.get("from"))
        b = parse_ts(seg.get("to"))
        days = round_days(max(0.0, (b - a).total_seconds() / 86400.0)) if a and b else 0.0
        history.append({"s": seg["s"], "days": days, "from": seg["from"], "to": seg["to"]})
        covered[seg["s"]] = round_days(covered.get(seg["s"], 0.0) + days)
        if unknown is not None and seg["s"] not in KNOWN_STATUSES:
            unknown.add(seg["s"])
    for col in status_dwell_cols(props):
        raw = (props.get(col) or {}).get("number")
        if raw is None:
            continue
        try:
            days = float(raw)
        except (TypeError, ValueError):
            continue
        if days > 0:
            rest = round_days(days - covered.get(col, 0.0))
            if rest > 0:
                # Legacy remainder (pre-Segments accrual): no timestamps known.
                history.append({"s": col, "days": rest})
            if unknown is not None and col not in KNOWN_STATUSES:
                unknown.add(col)
    status_name = (
        collected
        or _prop_status_name(props.get("Status"))
        or _prop_status_name(props.get("Current Status"))
    )
    if unknown is not None and status_name and str(status_name) not in KNOWN_STATUSES:
        unknown.add(str(status_name))
    done_at = prop_date_start(props.get("Done at"))
    is_done = (status_name and str(status_name).lower() == "done") or bool(done_at)
    if is_done:
        diamond = {"s": "Done"}
        if done_at:
            diamond["at"] = done_at
        history.append(diamond)
        return history
    if collected and since:
        open_days = round_days(max(0.0, (now - since).total_seconds() / 86400.0))
        if open_days > 0:
            # Exact open interval: when the current status was obtained is known.
            history.append({"s": collected, "days": open_days, "from": iso_z(since), "to": None})
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


def log_history_by_task(rows: list, unknown: set | None = None) -> dict:
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
        hist = history_from_log_row(props, unknown=unknown)
        if hist:
            mapping[ids[0]] = hist
    return mapping


def enrich_with_log_statistics(payload: dict, log_rows: list | None = None) -> dict:
    """Attach tasks[].history from LOG STATISTICS. Tasks without LOG rows keep calendar fallback."""
    rows = log_rows if log_rows is not None else query_log_rows()
    unknown: set = set()
    by_task = log_history_by_task(rows, unknown=unknown)
    matched = 0
    for task in payload.get("tasks") or []:
        tid = str(task.get("id") or "")
        if not tid or is_fixture_row(task):
            continue
        cur = str(task.get("s") or "").strip()
        if cur and cur not in KNOWN_STATUSES:
            unknown.add(cur)
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
    # Additive: statuses seen in data but outside LOG_STATUS_COLS. Never dropped
    # silently — their dwell stays in tasks[].history and the names are listed here.
    payload["unknown_statuses"] = sorted(unknown)
    if unknown:
        print(
            "warning: statuses outside LOG_STATUS_COLS (dwell kept, listed in unknown_statuses): "
            + ", ".join(sorted(unknown)),
            file=sys.stderr,
        )
    return payload


def _atomic_write(path: Path, text: str) -> None:
    """tmp+rename: readers never see a truncated/partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_payload(payload: dict) -> dict:
    text = json.dumps(payload, ensure_ascii=False)
    _atomic_write(WS_JSON, text)
    try:
        _atomic_write(OUT_JSON, text)
    except OSError:
        pass
    return payload


def enrich_existing() -> dict:
    """Dry-merge LOG dwell onto the current Sprint JSON without re-querying the Sprint DS."""
    payload = json.loads(WS_JSON.read_text(encoding="utf-8"))
    payload = enrich_with_log_statistics(payload)
    payload["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return write_payload(payload)


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
    import urllib.error
    import urllib.request

    token, version = _cfg()
    url = "https://api.notion.com" + path
    data = None if body is None else json.dumps(body).encode()
    # Retry/backoff: 3 attempts, exponential pause on 429/5xx/timeouts.
    for attempt in range(3):
        r = urllib.request.Request(url, data=data, method=method)
        r.add_header("Authorization", "Bearer " + token)
        r.add_header("Notion-Version", version)
        r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            retryable = e.code == 429 or 500 <= e.code < 600
            if not retryable or attempt >= 2:
                raise
            retry_after = e.headers.get("Retry-After") if e.headers else None
            try:
                wait = float(retry_after) if retry_after else 0.0
            except (TypeError, ValueError):
                wait = 0.0
            time.sleep(min(max(wait, float(2 ** attempt)), 30.0))
        except (TimeoutError, urllib.error.URLError):
            if attempt >= 2:
                raise
            time.sleep(min(float(2 ** attempt), 8.0))


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
    fixtures_excluded = 0
    for row in rows:
        if is_fixture_row(row):
            fixtures_excluded += 1
            continue
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
        uid = (p.get("ID") or {}).get("unique_id") or {}
        uid_num = uid.get("number")
        tid = (str(uid.get("prefix") or "TASK") + "-" + str(uid_num)) if uid_num is not None else None
        tasks.append({
            "id": row.get("id"),
            "tid": tid,
            "u": row.get("url"),
            "r": releases,
            "p": ((p.get("Project") or {}).get("select") or {}).get("name") or "",
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
        "fixtures_excluded": fixtures_excluded,
        "estimate_rule": "1d=8h",
        "status_dwell": "current_status_calendar_days",
        "status_dwell_note": STATUS_DWELL_NOTE,
        "status_meta": status_meta,
        "releases": sorted(from_tasks, key=rel_sort_key),
        "tasks": tasks,
    }
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


def selftest() -> dict:
    """Offline checks: unknown statuses keep their time and surface in unknown_statuses."""
    now = parse_ts("2026-09-22T00:00:00Z")
    props = {
        "Collected Status": {"rich_text": [{"plain_text": "Blocked"}]},
        "Status since": {"date": {"start": "2026-09-20T00:00:00Z"}},
        "New": {"type": "number", "number": 1.0},
        "Blocked": {"type": "number", "number": 2.5},
        "Done at": {"date": None},
    }
    unknown: set = set()
    hist = history_from_log_row(props, now=now, unknown=unknown)
    seg = {}
    for h in hist:
        seg[h["s"]] = round_days(seg.get(h["s"], 0.0) + (h.get("days") or 0))
    assert seg.get("New") == 1.0, seg
    assert seg.get("Blocked") == 4.5, seg  # 2.5 closed + 2.0 open — time not lost
    open_blk = [h for h in hist if h["s"] == "Blocked" and "to" in h and h["to"] is None]
    assert len(open_blk) == 1 and open_blk[0]["days"] == 2.0, hist  # open = own item
    assert open_blk[0]["from"].startswith("2026-09-20"), open_blk  # obtained-at is collector-known
    assert "Blocked" in unknown, unknown

    open_only = {
        "Collected Status": {"rich_text": [{"plain_text": "QA Hold"}]},
        "Status since": {"date": {"start": "2026-09-21T00:00:00Z"}},
        "Done at": {"date": None},
    }
    u2: set = set()
    hist2 = history_from_log_row(open_only, now=now, unknown=u2)
    assert {h["s"]: h["days"] for h in hist2}.get("QA Hold") == 1.0, hist2
    assert hist2[0]["to"] is None and hist2[0]["from"].startswith("2026-09-21"), hist2
    assert u2 == {"QA Hold"}, u2

    payload = {"tasks": [{"id": "3c8e17b6-8482-8166-0000-000000000000", "s": "Code Freeze"}]}
    enrich_with_log_statistics(payload, log_rows=[])
    assert "unknown_statuses" in payload, payload.keys()
    assert payload["unknown_statuses"] == ["Code Freeze"], payload["unknown_statuses"]
    clean = {"tasks": [{"id": "3c8e17b6-8482-8166-0000-000000000001", "s": "Done"}]}
    enrich_with_log_statistics(clean, log_rows=[])
    assert clean["unknown_statuses"] == [], clean["unknown_statuses"]

    # Segments: each interval is its own item (repeats never merged), exact
    # from/to, open interval keeps to=null, legacy remainder has no timestamps.
    seg_json = json.dumps([
        {"s": "Ready For Dev", "from": "2026-09-21T08:00:00.000Z", "to": "2026-09-21T09:00:00.000Z"},
        {"s": "Development", "from": "2026-09-21T09:00:00.000Z", "to": "2026-09-21T09:05:00.000Z"},
        {"s": "Ready For QA", "from": "2026-09-21T09:05:00.000Z", "to": "2026-09-21T10:00:00.000Z"},
        {"s": "Development", "from": "2026-09-21T10:00:00.000Z", "to": "2026-09-21T11:00:00.000Z"},
    ], ensure_ascii=False)
    seg_props = {
        "Collected Status": {"rich_text": [{"plain_text": "Testing"}]},
        "Status since": {"date": {"start": "2026-09-21T11:00:00Z"}},
        "Segments": {"rich_text": [{"plain_text": seg_json}]},
        "Development": {"type": "number", "number": 1.0 + 65 / 1440},  # 65 min covered + 1.0 legacy
        "Done at": {"date": None},
    }
    u3: set = set()
    hist3 = history_from_log_row(seg_props, now=parse_ts("2026-09-23T11:00:00Z"), unknown=u3)
    names = [h["s"] for h in hist3]
    assert names.count("Development") == 3, names  # 2 interval segments + 1 legacy remainder
    assert names == ["Ready For Dev", "Development", "Ready For QA", "Development", "Development", "Testing"], names
    dev5 = hist3[1]
    assert dev5["days"] > 0 and dev5["days"] < 0.01, dev5  # 5-minute interval survives
    assert dev5["from"].startswith("2026-09-21T09:00") and dev5["to"].startswith("2026-09-21T09:05"), dev5
    legacy_rest = hist3[4]
    assert legacy_rest["days"] == 1.0 and "from" not in legacy_rest and "to" not in legacy_rest, legacy_rest
    opened = hist3[5]
    assert opened["s"] == "Testing" and opened["to"] is None and opened["from"].startswith("2026-09-21T11:00"), opened
    assert opened["days"] == 2.0, opened

    # legacy rows (no Segments): closed aggregates stay timestamp-free (no
    # invented from/to); only the collector-known open interval carries exact
    # boundaries (it is always its own item)
    legacy_items = [h for h in hist if "from" not in h and h.get("s") != "Done"]
    assert legacy_items and all("to" not in h for h in legacy_items), hist
    return {"ok": True, "mode": "selftest"}


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
    elif cmd in ("selftest", "test"):
        print(json.dumps(selftest(), ensure_ascii=False))
    else:
        raise SystemExit("usage: release-widget-sync.py [snapshot|enrich|dry|selftest|serve]")
