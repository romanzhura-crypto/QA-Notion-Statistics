#!/usr/bin/env python3
"""Durable webhook run-state store (board #273, fix of #272 A27 flakiness).

Why not actions/cache: cache keys are IMMUTABLE and restore-keys matches ANY
older entry — mutable state (run-state counters/clocks) was restored from
random stale generations, so quality.webhook in the published JSON flapped
between fresh and 4-day-old values and the gate could false-fail on a stale
gap (incident 2026-10-13, board #272).

Instead the run-state lives in a dedicated git branch (default
"webhook-run-state") of the same repo, one JSON file per widget profile:
  config/status-webhook-run.json      (sprint, legacy name)
  config/status-webhook-run-1c.json   (profile "1c")

Access is the GitHub Contents API with the job token (optimistic concurrency
via blob sha, retry on 409) — no extra secrets, no git identity setup.

Subcommands:
  pull  — download remote state files into the workspace (missing = no-op).
  push  — merge local state files into the remote branch (winner-merge, see
          below) and commit. Retries on sha conflicts. Never raises on a
          single-file failure: metrics must not break event handling.

Merge rules (winner-merge, monotonic — same semantics as run_state_note):
  - *_at timestamps:  monotonic MAX (a clock never moves back).
  - events_* counters: MAX (two racing writers both started from the same
    base — SUM would inflate; MAX is best-effort, matching the documented
    "metrics are best-effort, segments are not").
  - everything else:  local wins.

Usage:
  python3 backend/run-state-store.py pull [--path P ...]
  python3 backend/run-state-store.py push [--path P ...]

Env:
  GITHUB_TOKEN (or GH_TOKEN)  — required; job token with contents:write for
                                push, contents:read for pull.
  GITHUB_REPOSITORY           — "owner/repo" (auto-set in Actions).
  RUN_STATE_BRANCH            — branch name (default "webhook-run-state").

Never prints tokens. selftest runs offline.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEFAULT_PATHS = [
    "config/status-webhook-run.json",
    "config/status-webhook-run-1c.json",
    # A28 (board #206): collector run-state the same durable way (the same
    # mutable-state problem as #273 — ephemeral runners lose counters).
    "config/log-statistics-run.json",
    "config/log-statistics-run-1c.json",
]
DEFAULT_BRANCH = "webhook-run-state"
API = "https://api.github.com"


# ---------- merge (pure, selftested) ----------

def _ts(s):
    """ISO timestamp -> comparable datetime; None for missing/unparseable."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def merge_state(local: dict | None, remote: dict | None) -> dict | None:
    """Winner-merge two run-state dicts (docstring rules). None = keep remote."""
    if not isinstance(local, dict) or not local:
        return None  # nothing local: leave remote as-is
    if not isinstance(remote, dict) or not remote:
        return dict(local)  # no remote: publish local
    out = dict(remote)
    for k, v in local.items():
        if k.endswith("_at"):
            a, b = _ts(v), _ts(out.get(k))
            out[k] = v if (a and (not b or a > b)) else out.get(k)
        elif k.startswith("events_") or k.endswith("_total") or k == "last_run":
            # Monotonic counters: webhook events_* + A28 collector cumulative
            # metrics (runs_/created_/updated_/skipped_/failed_*) and the run
            # number. MAX — two racing writers started from the same base,
            # SUM would inflate (best-effort metrics, see docstring).
            try:
                out[k] = max(int(v), int(out.get(k) or 0))
            except (TypeError, ValueError):
                out[k] = v
        else:
            out[k] = v
    # any remote-only events_* key: keep (already in out)
    return out


# ---------- GitHub Contents API ----------

def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not tok:
        raise SystemExit("run-state-store: GITHUB_TOKEN not set")
    return tok


def _repo() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY") or ""
    if not repo or "/" not in repo:
        raise SystemExit("run-state-store: GITHUB_REPOSITORY not set")
    return repo


def _branch() -> str:
    return os.environ.get("RUN_STATE_BRANCH") or DEFAULT_BRANCH


def _req(method: str, url: str, body: dict | None, token: str):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "authorization": "Bearer " + token,
            "accept": "application/vnd.github+json",
            "user-agent": "run-state-store",
            **({"content-type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except ValueError:
            return e.code, {}
    except OSError as e:
        return 0, {"message": str(e)}


def _get_file(path: str, token: str):
    """-> ("ok", sha, data) | ("missing", None, None) | ("error", None, msg)."""
    url = (f"{API}/repos/{_repo()}/contents/{path}"
           f"?ref={urllib.parse.quote(_branch())}")
    code, body = _req("GET", url, None, token)
    if code == 200:
        try:
            content = base64.b64decode(body.get("content") or "").decode("utf-8")
            data = json.loads(content)
            return "ok", body.get("sha"), (data if isinstance(data, dict) else {})
        except (ValueError, TypeError):
            return "ok", body.get("sha"), {}  # corrupt remote: treat as empty
    if code == 404:
        return "missing", None, None
    return "error", None, f"http {code}: {str(body.get('message'))[:120]}"


def _put_file(path: str, data: dict, sha: str | None, token: str) -> int:
    payload = {
        "message": f"run-state update {path} ({datetime.now(timezone.utc).isoformat()})",
        "content": base64.b64encode(
            json.dumps(data, ensure_ascii=False, indent=1).encode()
        ).decode(),
        "branch": _branch(),
    }
    if sha:
        payload["sha"] = sha
    code, _body = _req("PUT", f"{API}/repos/{_repo()}/contents/{path}", payload, token)
    return code


# ---------- subcommands ----------

def cmd_pull(paths: list[str]) -> int:
    token = _token()
    ok = True
    for p in paths:
        status, _sha, remote = _get_file(p, token)
        if status == "missing":
            print(f"pull {p}: absent on branch {_branch()} (first run?)")
            continue
        if status == "error":
            print(f"pull {p}: ERROR {remote}", file=sys.stderr)
            ok = False
            continue
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(remote, f, ensure_ascii=False)
        os.replace(tmp, p)
        print(f"pull {p}: ok last_delivery={remote.get('last_delivery_at')}")
    return 0 if ok else 1


def _ensure_branch(token: str) -> bool:
    """Create the run-state branch from the repo default branch when absent."""
    code, body = _req("GET", f"{API}/repos/{_repo()}/git/ref/heads/{_branch()}", None, token)
    if code == 200:
        return True
    code, repo = _req("GET", f"{API}/repos/{_repo()}", None, token)
    if code != 200:
        return False
    default = repo.get("default_branch") or "main"
    code, ref = _req("GET", f"{API}/repos/{_repo()}/git/ref/heads/{default}", None, token)
    if code != 200:
        return False
    sha = ((ref.get("object") or {}).get("sha"))
    if not sha:
        return False
    code, _b = _req("POST", f"{API}/repos/{_repo()}/git/refs",
                    {"ref": f"refs/heads/{_branch()}", "sha": sha}, token)
    if code in (200, 201):
        print(f"branch {_branch()} created from {default}")
        return True
    return False


def cmd_push(paths: list[str]) -> int:
    token = _token()
    _ensure_branch(token)
    ok = True
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                local = json.load(f)
        except (OSError, ValueError):
            print(f"push {p}: no local file, skipped")
            continue
        if not isinstance(local, dict) or not local:
            print(f"push {p}: empty local state, skipped")
            continue
        for attempt in range(5):
            status, sha, remote = _get_file(p, token)
            if status == "error":
                print(f"push {p}: ERROR get {remote}", file=sys.stderr)
                ok = False
                break
            remote_dict = remote if status == "ok" else None
            merged = merge_state(local, remote_dict)
            if merged is None:
                print(f"push {p}: remote already current")
                break
            if remote_dict is not None and merged == remote_dict:
                print(f"push {p}: local is subset of remote, no write")
                break
            code = _put_file(p, merged, sha if status == "ok" else None, token)
            if code in (200, 201):
                print(f"push {p}: ok (attempt {attempt + 1}) "
                      f"last_delivery={merged.get('last_delivery_at')}")
                break
            if code == 409:
                time.sleep(1 + attempt)  # concurrent writer: re-merge & retry
                continue
            print(f"push {p}: ERROR http {code}", file=sys.stderr)
            ok = False
            break
        else:
            print(f"push {p}: ERROR conflict after retries", file=sys.stderr)
            ok = False
    return 0 if ok else 1


# ---------- selftest (offline) ----------

def selftest() -> None:
    # 1) timestamp monotonic max
    a = {"last_delivery_at": "2026-10-13T09:00:00.000Z", "events_total": 10}
    b = {"last_delivery_at": "2026-10-13T08:00:00.000Z", "events_total": 3}
    m = merge_state(a, b)
    assert m["last_delivery_at"] == "2026-10-13T09:00:00.000Z", m
    assert m["events_total"] == 10, m
    # 2) remote newer clock wins even from remote side
    m = merge_state(b, a)
    assert m["last_delivery_at"] == "2026-10-13T09:00:00.000Z", m
    assert m["events_total"] == 10, m
    # 3) counters = max, never sum (racing writers from same base)
    m = merge_state({"events_ping": 5}, {"events_ping": 7})
    assert m["events_ping"] == 7, m
    # 4) remote-only keys preserved, local-only added
    m = merge_state({"events_total": 1}, {"events_skipped": 2, "last_event_at": "2026-10-01T00:00:00.000Z"})
    assert m["events_skipped"] == 2 and m["last_event_at"] and m["events_total"] == 1, m
    # 5) missing sides
    assert merge_state(None, {"a": 1}) is None
    assert merge_state({"a": 1}, None) == {"a": 1}
    assert merge_state({}, {"a": 1}) is None
    # 6) mixed timestamp formats compare correctly
    m = merge_state({"last_delivery_at": "2026-10-13T09:30:00Z"},
                    {"last_delivery_at": "2026-10-13T09:29:59.000Z"})
    assert m["last_delivery_at"] == "2026-10-13T09:30:00Z", m
    # 7) unparseable timestamps: local does not clobber remote silently
    m = merge_state({"last_delivery_at": "garbage"}, {"last_delivery_at": "2026-10-13T09:00:00.000Z"})
    assert m["last_delivery_at"] == "2026-10-13T09:00:00.000Z", m
    # 8) A28 (board #206): collector cumulative counters merge with MAX
    m = merge_state({"runs_total": 5, "created_total": 2, "failed_total": 0},
                    {"runs_total": 7, "created_total": 9, "failed_total": 1})
    assert m["runs_total"] == 7 and m["created_total"] == 9 and m["failed_total"] == 1, m
    # 9) A28: last_run is a monotonic run number (MAX), local-only metrics win
    m = merge_state({"last_run": 3, "last_duration_s": 12.5},
                    {"last_run": 5, "last_duration_s": 88.0})
    assert m["last_run"] == 5 and m["last_duration_s"] == 12.5, m
    # 10) A28: last_ok_at is a *_at clock — monotonic max
    m = merge_state({"last_ok_at": "2026-10-14T10:00:00.000Z"},
                    {"last_ok_at": "2026-10-14T09:00:00.000Z"})
    assert m["last_ok_at"] == "2026-10-14T10:00:00.000Z", m
    print(json.dumps({"ok": True, "mode": "selftest"}))


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] in ("selftest", "test"):
        selftest()
        sys.exit(0)
    cmd = args[0]
    paths = DEFAULT_PATHS
    if "--path" in args:
        paths = args[args.index("--path") + 1:]
    if cmd == "pull":
        sys.exit(cmd_pull(paths))
    if cmd == "push":
        sys.exit(cmd_push(paths))
    sys.exit(f"unknown subcommand: {cmd}")
