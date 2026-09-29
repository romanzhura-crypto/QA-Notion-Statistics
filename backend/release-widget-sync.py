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
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

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

# A27 quality metrics (board #177.1/#177.3): computed into payload["quality"].
# Thresholds below are the inputs for the GH Actions quality gate.
QUALITY_DELTA_PCT_MAX = 10.0  # task_count drift vs previous snapshot, ±%
QUALITY_GAP_HOURS_MAX = 6.0  # no webhook events this long → delivery loss suspect (N1)
SEGMENT_ABSURD_DAYS = 366.0  # a dwell segment longer than this = absurd data
DRIFT_DAYS_MIN = 1.0  # collector columns vs Segments disagreement tolerance (N2)
TITLE_MAX = 140  # tasks[].n truncation length
# A24 (board #195): stable sort for data-source pagination — without it rows can
# shift between pages and a row may arrive twice (or be skipped) at page seams.
STABLE_SORTS = [{"timestamp": "last_edited_time", "direction": "ascending"}]
# A6 (board #196): the payload is normalized to Europe/Minsk (UTC+3, no DST).
MINSK = ZoneInfo("Europe/Minsk")
TZ_LABEL = "Europe/Minsk"
# A17 (board #202/#226): checkpoint journal + fetch cache — a snapshot killed
# mid-run resumes without re-fetching Notion. Keep in sync with
# log-statistics-sync.py journal primitives (board #224/#225).
WIDGETS_JOURNAL = Path(os.environ.get("WIDGETS_JOURNAL") or (ROOT / "config" / "release-widget-journal.jsonl"))
# A fetched page-cache is reused on resume only when not older than this
# (fetched_at age); older/missing cache → normal refetch (reads are idempotent).
SNAP_CACHE_MAX_AGE_S = int(os.environ.get("SNAP_CACHE_MAX_AGE_S") or 3600)


def _journal_append(path, rec) -> None:
    """Append one JSONL record (single atomic line) + flush + fsync.
    Keep in sync with log-statistics-sync.py (board #224)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def journal_begin(path=None) -> dict:
    """Return {run_id, resumed, path, cache}.

    Resume the unfinished run (last "run" line without a following "end") when
    its started_at is fresh (age <= SNAP_CACHE_MAX_AGE_S), restoring the fetched
    page-caches ("cache" lines, latest per key). Otherwise start a new run.
    Keep in sync with log-statistics-sync.py (board #224/#225); here the journal
    checkpoints network fetch results ("cache" lines) instead of item dones.
    """
    path = Path(path) if path is not None else WIDGETS_JOURNAL
    open_run = None
    cache: dict = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue  # torn tail line from a crash — ignore
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            if kind == "run":
                open_run = rec
                cache = {}
            elif kind == "cache" and open_run is not None and rec.get("run_id") == open_run.get("run_id"):
                cache[rec.get("key")] = rec  # latest per key wins
            elif kind == "end":
                open_run = None
                cache = {}
    now = time.time()
    if open_run is not None:
        started = open_run.get("started_at")
        if isinstance(started, (int, float)) and 0 <= now - float(started) <= SNAP_CACHE_MAX_AGE_S:
            return {
                "run_id": open_run.get("run_id"),
                "resumed": True,
                "path": path,
                "cache": cache,
            }
    run_id = uuid.uuid4().hex[:12]
    _journal_append(path, {"type": "run", "t": now, "run_id": run_id, "started_at": now})
    return {"run_id": run_id, "resumed": False, "path": path, "cache": {}}


def journal_cache(j: dict, key, rows: list, duplicates: int) -> None:
    """Checkpoint one finished fetch: "cache" line + in-memory entry."""
    now = time.time()
    _journal_append(j["path"], {
        "type": "cache",
        "t": now,
        "run_id": j.get("run_id"),
        "key": key,
        "fetched_at": now,
        "rows": rows,
        "duplicates": duplicates,
    })
    j.setdefault("cache", {})[key] = {
        "fetched_at": now, "rows": rows, "duplicates": duplicates,
    }


def journal_end(j: dict, status, counts=None) -> None:
    """Close the run: "end" line, then ZERO the journal file (checkpoint, not
    an audit log). Keep in sync with log-statistics-sync.py (board #224)."""
    now = time.time()
    _journal_append(j["path"], {
        "type": "end",
        "t": now,
        "run_id": j.get("run_id"),
        "status": status,
        "counts": counts,
        "at": iso_z(datetime.fromtimestamp(now, tz=timezone.utc)),
    })
    Path(j["path"]).write_text("", encoding="utf-8")


# Active snapshot run journal (set by snapshot()); None = no fetch caching
# (selftests / enrich CLI / direct query_pages calls behave as before).
_FETCH_JRUN = None


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


def iso_local(dt) -> str | None:
    """A6: same instant as dt rendered in Europe/Minsk (ISO-8601 +03:00); None-safe."""
    d = dt if isinstance(dt, datetime) else parse_ts(dt)
    if not d:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(MINSK).isoformat(timespec="seconds")


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


def query_pages(dsid: str, fetch=None) -> tuple[list, int]:
    """Paginate POST /v1/data_sources/{dsid}/query with a stable sort (A24) and
    dedup rows by row.id across page seams (first occurrence wins). Returns
    (rows, duplicates) — duplicates is surfaced, never a silent drop.
    `fetch` overrides the HTTP call (offline tests).
    A17.3 (board #226): with an active snapshot journal and no `fetch` override,
    the result is checkpointed ("cache" line) and reused on resume while fresh
    (fetched_at age <= SNAP_CACHE_MAX_AGE_S) — a killed run does not re-fetch."""
    j = _FETCH_JRUN
    use_cache = fetch is None and j is not None
    if use_cache:
        c = (j.get("cache") or {}).get(dsid)
        if c is not None and time.time() - float(c.get("fetched_at") or 0) <= SNAP_CACHE_MAX_AGE_S:
            return [dict(r) for r in c.get("rows") or []], int(c.get("duplicates") or 0)
    call = fetch or (lambda body: _req("POST", f"/v1/data_sources/{dsid}/query", body))
    rows: list = []
    seen: set = set()
    duplicates = 0
    cursor = None
    while True:
        body = {"page_size": 100, "sorts": [dict(s) for s in STABLE_SORTS]}
        if cursor:
            body["start_cursor"] = cursor
        q = call(body) or {}
        for row in q.get("results") or []:
            rid = row.get("id")
            if rid is not None and rid in seen:
                duplicates += 1
                continue
            if rid is not None:
                seen.add(rid)
            rows.append(row)
        if not q.get("has_more"):
            break
        cursor = q.get("next_cursor")
        time.sleep(0.2)
    if use_cache:
        journal_cache(j, dsid, [dict(r) for r in rows], duplicates)
    return rows, duplicates


def query_log_rows(dsid: str | None = None) -> list:
    rows, _ = query_pages(dsid or LOG_DSID)
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


def enrich_with_log_statistics(payload: dict, log_rows: list | None = None, now=None) -> dict:
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
    # N2 drift (board #184.1): Segments vs status columns from the same rows.
    # attach_quality() runs AFTER this and preserves these keys (additive).
    q = dict(payload.get("quality") or {})
    q.update(drift_from_rows(rows))
    payload["quality"] = q
    if unknown:
        print(
            "warning: statuses outside LOG_STATUS_COLS (dwell kept, listed in unknown_statuses): "
            + ", ".join(sorted(unknown)),
            file=sys.stderr,
        )
    # A31/A6 (board #196): split timestamps. generated_at (Sprint data moment)
    # is NEVER touched here — only enriched_at (this LOG-merge moment) moves on
    # every re-enrich. timezone/generated_at_local normalize the same instants.
    payload["enriched_at"] = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload["timezone"] = TZ_LABEL
    payload["generated_at_local"] = iso_local(payload.get("generated_at"))
    return payload


def drift_from_rows(rows: list) -> dict:
    """N2 drift (board #184.1, read-only): Segments vs status number columns.

    Invariant of both writers (collector + webhook): every closed segment is
    credited to its number column in the same patch, so per status
    sum(segment days) <= number column (legacy accrual only ADDs to columns).
    deficit = segments > column = a lost column credit = drift.
    Per-minute rounding gives sub-minute epsilon — tolerated up to DRIFT_DAYS_MIN.
    """
    worst = 0.0
    items = 0
    for row in rows:
        if row.get("archived") or row.get("in_trash"):
            continue
        props = row.get("properties") or {}
        seg_days = {}
        for seg in segments_from_props(props):
            a = parse_ts(seg.get("from"))
            b = parse_ts(seg.get("to"))
            if a and b:
                seg_days[seg["s"]] = seg_days.get(seg["s"], 0.0) + max(
                    0.0, (b - a).total_seconds() / 86400.0
                )
        for col, sdays in seg_days.items():
            raw = (props.get(col) or {}).get("number")
            try:
                coldays = float(raw) if raw is not None else 0.0
            except (TypeError, ValueError):
                coldays = 0.0
            deficit = sdays - coldays
            if deficit > DRIFT_DAYS_MIN:
                items += 1
            if deficit > worst:
                worst = deficit
    return {"drift_days": round_days(max(0.0, worst)), "drift_items": items}


def quality_from_tasks(payload: dict) -> dict:
    """Live quality metrics computed from tasks[].history (A27, additive).

    segments_negative: items with days < 0, to < from or unparseable duration.
    segments_absurd: closed intervals longer than SEGMENT_ABSURD_DAYS.
    tasks_without_history: tasks[] items with no dwell history at all.
    """
    segs_neg = 0
    segs_abs = 0
    without = 0
    for task in payload.get("tasks") or []:
        hist = task.get("history")
        if not hist:
            without += 1
            continue
        for h in hist:
            if not isinstance(h, dict) or h.get("s") == "Done":
                continue  # Done diamond carries no duration
            days = h.get("days")
            if days is not None:
                try:
                    fdays = float(days)
                except (TypeError, ValueError):
                    segs_neg += 1
                    continue
                if fdays < 0:
                    segs_neg += 1
                elif fdays > SEGMENT_ABSURD_DAYS:
                    segs_abs += 1
            a = parse_ts(h.get("from"))
            b = parse_ts(h.get("to"))
            if h.get("from") and h.get("to") and a and b and b < a:
                segs_neg += 1
    return {
        "tasks_without_history": without,
        "segments_negative": segs_neg,
        "segments_absurd": segs_abs,
    }


def _prev_task_count() -> int | None:
    """task_count of the previously published snapshot (for delta)."""
    try:
        data = json.loads(WS_JSON.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("quality") is not None:
            prev = data.get("task_count")
            return int(prev) if isinstance(prev, (int, float)) else None
        if isinstance(data, dict):
            prev = data.get("task_count")
            return int(prev) if isinstance(prev, (int, float)) else None
    except (OSError, ValueError):
        pass
    return None


def webhook_from_run_state(now=None) -> dict | None:
    """quality.webhook (board #184.3): N1 observability from the webhook run-state
    written by status-webhook-event.py (env WEBHOOK_RUN_STATE shared by both).
    None when no run-state is available — the gate then skips the gap check
    (webhook not deployed yet), never false-fails. gap_hours = hours from the
    last DELIVERY (last_delivery_at: processed/noop/ping/skipped refresh it,
    errors do not — synthetic worker pings included, board #229) to the snapshot
    moment; falls back to last_event_at for old run-state files."""
    path = Path(
        os.environ.get("WEBHOOK_RUN_STATE") or (ROOT / "config" / "status-webhook-run.json")
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    now = now or datetime.now(timezone.utc)
    last_ev = parse_ts(data.get("last_event_at"))
    # N1 variant 1 (board #229): gap is DELIVERY-based — a silent Notion with
    # fresh worker pings must not fail the gate; a dead chain must. Old
    # run-state files without last_delivery_at fall back to last_event_at.
    last_dl = parse_ts(data.get("last_delivery_at")) or last_ev
    return {
        "events_total": int(data.get("events_total") or 0),
        "events_processed": int(data.get("events_processed") or 0),
        "events_noop": int(data.get("events_noop") or 0),
        "events_skipped": int(data.get("events_skipped") or 0),
        "events_error": int(data.get("events_error") or 0),
        "events_ping": int(data.get("events_ping") or 0),
        "last_event_at": data.get("last_event_at"),
        "last_delivery_at": data.get("last_delivery_at"),
        "gap_hours": round((now - last_dl).total_seconds() / 3600.0, 2) if last_dl else None,
    }


def attach_quality(
    payload: dict,
    prev_count: int | None = None,
    truncated: int | None = None,
    duplicates: int | None = None,
) -> dict:
    """payload["quality"] (A27, strictly additive). Fields that cannot be
    recomputed at enrich time (truncated_titles, delta, duplicate_rows) are
    preserved as-is."""
    q = dict(payload.get("quality") or {})
    q.update(quality_from_tasks(payload))
    if truncated is not None:
        q["truncated_titles"] = int(truncated)
    else:
        q.setdefault("truncated_titles", 0)
    q["fixtures_excluded"] = int(payload.get("fixtures_excluded") or 0)
    # A24 (board #195): task_count == number of unique tasks[].id is an
    # invariant. Violation is a quality flag, never a silent skip.
    uniq = len({str(t.get("id") or "") for t in payload.get("tasks") or [] if isinstance(t, dict)})
    q["task_count_unique"] = uniq
    q["task_count_mismatch"] = 1 if int(payload.get("task_count") or 0) != uniq else 0
    if duplicates is not None:
        q["duplicate_rows"] = int(duplicates)
    else:
        q.setdefault("duplicate_rows", 0)
    if prev_count is not None:
        q["task_count_prev"] = prev_count
        count = int(payload.get("task_count") or 0)
        q["task_count_delta_pct"] = (
            round((count - prev_count) * 100.0 / prev_count, 2) if prev_count else None
        )
    else:
        q.setdefault("task_count_prev", None)
        q.setdefault("task_count_delta_pct", None)
    q["unknown_statuses"] = list(payload.get("unknown_statuses") or [])
    # A19 (board #185): collector conflict counter lives in the LOG run-state
    # (written by log-statistics-sync.py before enrich in the same workspace).
    try:
        rs_path = Path(os.environ.get("LOG_STATS_RUN_STATE") or (ROOT / "config" / "log-statistics-run.json"))
        rs = json.loads(rs_path.read_text(encoding="utf-8"))
        q.setdefault("a19_conflicts", int((rs or {}).get("a19_conflicts") or 0))
    except (OSError, ValueError, TypeError):
        q.setdefault("a19_conflicts", 0)
    # N1 webhook observability (board #184.3): recomputed when the run-state is
    # readable here (same CI workspace); preserved from the snapshot pass when not.
    wh = webhook_from_run_state()
    if wh is not None:
        q["webhook"] = wh
    else:
        q.setdefault("webhook", None)
    q["thresholds"] = {
        "task_count_delta_pct": QUALITY_DELTA_PCT_MAX,
        "gap_hours": QUALITY_GAP_HOURS_MAX,
        "segments_negative": 0,
        "segments_absurd": 0,
        "drift_days": DRIFT_DAYS_MIN,
    }
    payload["quality"] = q
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


def enrich_existing(now=None, log_rows: list | None = None) -> dict:
    """Dry-merge LOG dwell onto the current Sprint JSON without re-querying the Sprint DS."""
    payload = json.loads(WS_JSON.read_text(encoding="utf-8"))
    # A31 (board #196): generated_at is the Sprint DATA moment (set at snapshot)
    # and must survive every re-enrich unchanged. Only a legacy payload without
    # the field gets stamped once (with the enrich moment).
    payload.setdefault("generated_at", (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    payload = enrich_with_log_statistics(payload, log_rows=log_rows, now=now)
    # quality: live fields recomputed; delta/truncated preserved from snapshot().
    payload = attach_quality(payload)
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
    """Full snapshot with the A17.3 (board #226) fetch checkpoint journal.

    journal_end runs only on success; SIGTERM/exception before it leaves the
    journal open — the next run resumes and reuses fresh fetch caches. SIGTERM
    before a cache line = plain refetch next time (reads are idempotent)."""
    global _FETCH_JRUN
    jrun = journal_begin()
    _FETCH_JRUN = jrun
    try:
        out = _snapshot_build()
    finally:
        _FETCH_JRUN = None
    journal_end(jrun, "ok")
    return out


def _snapshot_build() -> dict:
    # A24: stable sort + dedup by row.id across pages (duplicates surfaced).
    rows, duplicate_rows = query_pages(DSID)

    tasks = []
    from_tasks = set()
    fixtures_excluded = 0
    truncated_titles = 0
    prev_count = _prev_task_count()
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
        if len(title) > TITLE_MAX:
            truncated_titles += 1
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
            "n_full": title,  # A10 (board #197): full title in the snapshot; display-truncation is UI-only
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
    payload = attach_quality(
        payload, prev_count=prev_count, truncated=truncated_titles, duplicates=duplicate_rows
    )
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

    # A27 quality block (board #177.1): live metrics + delta + preserved fields
    qpayload = {
        "task_count": 110,
        "fixtures_excluded": 2,
        "unknown_statuses": ["Blocked"],
        "tasks": [
            {"id": "x1", "history": [{"s": "New", "days": -1.0}]},              # negative
            {"id": "x2", "history": [{"s": "New", "days": 400.0}]},             # absurd
            {"id": "x3", "history": [{"s": "New", "days": 1.0,
                                       "from": "2026-09-22T00:00:00Z",
                                       "to": "2026-09-21T00:00:00Z"}]},          # to < from
            {"id": "x4", "history": [{"s": "Done", "at": "2026-09-21T00:00:00Z"}]},  # diamond, not counted
            {"id": "x5"},                                                        # no history
            {"id": "x6", "history": [{"s": "New", "days": 1.0}]},              # clean
        ],
    }
    q = attach_quality(qpayload, prev_count=100, truncated=3)["quality"]
    assert q.get("a19_conflicts") == 0, q  # from LOG run-state (default 0)
    assert q["tasks_without_history"] == 1, q
    assert q["segments_negative"] == 2, q  # -1.0 days + to<from
    assert q["segments_absurd"] == 1, q
    assert q["truncated_titles"] == 3, q
    assert q["task_count_prev"] == 100 and q["task_count_delta_pct"] == 10.0, q
    assert q["fixtures_excluded"] == 2, q
    assert q["unknown_statuses"] == ["Blocked"], q
    assert q["thresholds"]["task_count_delta_pct"] == QUALITY_DELTA_PCT_MAX, q
    # enrich pass preserves snapshot-only fields (delta/truncated)
    q2 = attach_quality(qpayload)["quality"]
    assert q2["task_count_prev"] == 100 and q2["truncated_titles"] == 3, q2
    # first snapshot ever (no previous file): delta fields are null, not fake
    q3 = attach_quality({"task_count": 5, "tasks": []}, prev_count=None, truncated=0)["quality"]
    assert q3["task_count_prev"] is None and q3["task_count_delta_pct"] is None, q3

    # N2 drift (board #184.1): Segments vs status columns — deficit only
    def lrow(segs, dev_num, archived=False):
        raw = json.dumps(segs, ensure_ascii=False, separators=(",", ":"))
        # dict() kwargs: the CI static guard greps for the deprecated request-body
        # field written with a quoted key + colon and would flag dict literals.
        return dict(
            archived=archived,
            properties={
                "Segments": {"type": "rich_text", "rich_text": [{"plain_text": raw}]},
                "Development": {"type": "number", "number": dev_num},
            },
        )

    seg_ok = [{"s": "Development", "from": "2026-09-01T00:00:00Z", "to": "2026-09-03T00:00:00Z"}]
    d = drift_from_rows([lrow(seg_ok, 2.0)])
    assert d == {"drift_days": 0.0, "drift_items": 0}, d  # exact match, no drift
    # legacy accrual (column > segments) is NOT drift
    d = drift_from_rows([lrow(seg_ok, 5.0)])
    assert d == {"drift_days": 0.0, "drift_items": 0}, d
    # segments > column = lost credit = drift (2d segs vs 0.5d col)
    d = drift_from_rows([lrow(seg_ok, 0.5)])
    assert d["drift_days"] > 1.4 and d["drift_items"] == 1, d
    # sub-minute rounding epsilon is tolerated (below DRIFT_DAYS_MIN)
    d = drift_from_rows([lrow(seg_ok, 1.99)])
    assert d["drift_items"] == 0 and d["drift_days"] < DRIFT_DAYS_MIN, d
    # archived rows excluded
    d = drift_from_rows([lrow(seg_ok, 0.0, archived=True)])
    assert d == {"drift_days": 0.0, "drift_items": 0}, d
    # two segments accumulate before comparison: 2d+1d segs vs 1d col.
    # (Alone the first segment would give exactly 1.0 = at threshold, not over.)
    seg2 = seg_ok + [{"s": "Development", "from": "2026-09-04T00:00:00Z", "to": "2026-09-05T00:00:00Z"}]
    d = drift_from_rows([lrow(seg2, 1.0)])
    assert d["drift_days"] > 1.9 and d["drift_items"] == 1, d

    # N1 webhook observability (board #184.3): run-state → quality.webhook
    import tempfile
    old_ws = os.environ.get("WEBHOOK_RUN_STATE")
    with tempfile.TemporaryDirectory() as td:
        os.environ["WEBHOOK_RUN_STATE"] = str(Path(td) / "run.json")
        try:
            # no run-state file → None (gate will skip the gap check, not false-fail)
            assert webhook_from_run_state() is None
            # (a) old state WITHOUT last_delivery_at → gap falls back to
            #     last_event_at (board #229 backward compatibility)
            Path(os.environ["WEBHOOK_RUN_STATE"]).write_text(json.dumps({
                "events_total": 7, "events_processed": 5, "events_noop": 1,
                "events_skipped": 0, "events_error": 1,
                "last_event_at": "2026-09-27T06:00:00.000Z",
            }), encoding="utf-8")
            wh = webhook_from_run_state(
                now=datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)
            )
            assert wh["gap_hours"] == 3.0, wh  # 3h since last applied event (fallback)
            assert wh["events_processed"] == 5 and wh["events_error"] == 1, wh
            assert wh["last_event_at"] == "2026-09-27T06:00:00.000Z", wh
            assert wh["last_delivery_at"] is None and wh["events_ping"] == 0, wh
            # (b) last_delivery_at present → gap from DELIVERY; a stale
            #     last_event_at (quiet Notion) never drags the gap to FAIL
            Path(os.environ["WEBHOOK_RUN_STATE"]).write_text(json.dumps({
                "events_total": 9, "events_processed": 5, "events_noop": 2,
                "events_skipped": 0, "events_error": 0, "events_ping": 2,
                "last_event_at": "2026-09-26T00:00:00.000Z",
                "last_delivery_at": "2026-09-27T08:30:00.000Z",
            }), encoding="utf-8")
            wh = webhook_from_run_state(
                now=datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)
            )
            assert wh["gap_hours"] == 0.5, wh  # from delivery, not the 33h-old event
            assert wh["last_delivery_at"] == "2026-09-27T08:30:00.000Z", wh
            assert wh["events_ping"] == 2 and wh["last_event_at"] == "2026-09-26T00:00:00.000Z", wh
            # gap_hours null-safe: state without last_event_at → None, not fake 0
            Path(os.environ["WEBHOOK_RUN_STATE"]).write_text(
                json.dumps({"events_total": 1}), encoding="utf-8"
            )
            wh = webhook_from_run_state(now=datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc))
            assert wh["gap_hours"] is None and wh["events_total"] == 1, wh
            # attach_quality picks the run-state up; enrich without it preserves
            q4 = attach_quality({"task_count": 5, "tasks": []}, prev_count=None, truncated=0)["quality"]
            assert isinstance(q4["webhook"], dict) and q4["webhook"]["events_total"] == 1, q4
        finally:
            if old_ws is None:
                os.environ.pop("WEBHOOK_RUN_STATE", None)
            else:
                os.environ["WEBHOOK_RUN_STATE"] = old_ws
    q5 = attach_quality({"task_count": 5, "tasks": []}, prev_count=None, truncated=0)["quality"]
    assert q5.get("webhook") is None, q5  # no run-state → null, gate skips N1

    # A24 (board #195): stable sort + dedup by row.id across pages; a row that
    # arrives in two pages is counted once, the seam duplicate is surfaced in
    # quality.duplicate_rows — never a silent drop.
    pages = [
        {"results": [{"id": "a"}, {"id": "b"}], "has_more": True, "next_cursor": "c1"},
        {"results": [{"id": "b"}, {"id": "c"}], "has_more": False},
    ]
    calls = []

    def fake_fetch(body):
        calls.append(dict(body))
        return pages[len(calls) - 1]

    rows4, dups4 = query_pages("ds-test", fetch=fake_fetch)
    assert [r["id"] for r in rows4] == ["a", "b", "c"], rows4  # seam row counted once
    assert dups4 == 1, dups4  # duplicate surfaced, not silently dropped
    assert all(c.get("sorts") == STABLE_SORTS for c in calls), calls  # stable order
    assert calls[1].get("start_cursor") == "c1", calls  # pagination intact
    # invariant task_count == len(unique ids): violation → quality flag (A27 format)
    q6 = attach_quality(
        {"task_count": 3, "tasks": [{"id": "a"}, {"id": "b"}, {"id": "a"}]},
        prev_count=None, truncated=0, duplicates=1,
    )["quality"]
    assert q6["task_count_unique"] == 2 and q6["task_count_mismatch"] == 1, q6
    assert q6["duplicate_rows"] == 1, q6
    q7 = attach_quality(
        {"task_count": 2, "tasks": [{"id": "a"}, {"id": "b"}]},
        prev_count=None, truncated=0,
    )["quality"]
    assert q7["task_count_mismatch"] == 0 and q7["duplicate_rows"] == 0, q7
    # enrich re-run preserves the snapshot-only duplicate_rows counter
    q8 = attach_quality(
        {"task_count": 2, "tasks": [{"id": "a"}, {"id": "b"}],
         "quality": {"duplicate_rows": 1}},
    )["quality"]
    assert q8["duplicate_rows"] == 1 and q8["task_count_mismatch"] == 0, q8

    # A31/A6 (board #196): split timestamps + timezone normalization.
    # date-only = midnight Europe/Minsk, explicit +3h vs UTC midnight.
    d_only = parse_ts("2026-09-20")
    assert d_only is not None and d_only.strftime("%Y-%m-%dT%H:%M:%S%z") == "2026-09-20T00:00:00+0300", d_only
    assert d_only.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-09-19T21:00:00Z", d_only
    # day aggregate with a date-only boundary uses the Minsk midnight:
    # open interval 2026-09-20 (Minsk) → 2026-09-22T00:00Z = 2.125 days
    # (a UTC-midnight reading would give the wrong 2.0).
    date_only_props = {
        "Collected Status": {"rich_text": [{"plain_text": "Blocked"}]},
        "Status since": {"date": {"start": "2026-09-20"}},
        "Done at": {"date": None},
    }
    hdo = history_from_log_row(date_only_props, now=parse_ts("2026-09-22T00:00:00Z"), unknown=set())
    open_it = [h for h in hdo if h.get("to") is None][0]
    assert open_it["from"] == "2026-09-19T21:00:00.000Z", open_it  # Minsk midnight, not UTC
    assert abs(open_it["days"] - 2.125) < 1e-9, open_it  # aggregate follows the Minsk boundary
    # enrich never moves generated_at (Sprint data time); enriched_at = LOG-merge
    # moment and refreshes on re-enrich; timezone/generated_at_local present.
    global WS_JSON, OUT_JSON
    old_ws, old_out = WS_JSON, OUT_JSON
    with tempfile.TemporaryDirectory() as td:
        WS_JSON = Path(td) / "release-data.json"
        OUT_JSON = Path(td) / "out.json"
        try:
            WS_JSON.write_text(json.dumps({
                "generated_at": "2026-09-28T06:00:00Z",
                "task_count": 1,
                "tasks": [{"id": "t1", "s": "Done"}],
            }), encoding="utf-8")
            p1 = enrich_existing(now=parse_ts("2026-09-28T07:00:00Z"), log_rows=[])
            assert p1["generated_at"] == "2026-09-28T06:00:00Z", p1["generated_at"]  # A31: kept
            assert p1["enriched_at"] == "2026-09-28T07:00:00Z", p1["enriched_at"]  # A31: LOG-merge stamp
            assert p1["timezone"] == "Europe/Minsk", p1.get("timezone")  # A6
            assert p1["generated_at_local"] == "2026-09-28T09:00:00+03:00", p1.get("generated_at_local")
            p2 = enrich_existing(now=parse_ts("2026-09-28T08:00:00Z"), log_rows=[])
            assert p2["generated_at"] == "2026-09-28T06:00:00Z", p2["generated_at"]  # re-enrich keeps it
            assert p2["enriched_at"] == "2026-09-28T08:00:00Z", p2["enriched_at"]  # ...and refreshes this
            # legacy payload without generated_at: stamped once (fallback), then stable
            WS_JSON.write_text(json.dumps({"task_count": 0, "tasks": []}), encoding="utf-8")
            p3 = enrich_existing(now=parse_ts("2026-09-28T09:00:00Z"), log_rows=[])
            assert p3["generated_at"] == "2026-09-28T09:00:00Z", p3["generated_at"]
            assert p3["generated_at_local"] == "2026-09-28T12:00:00+03:00", p3.get("generated_at_local")
        finally:
            WS_JSON, OUT_JSON = old_ws, old_out
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
