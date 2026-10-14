#!/usr/bin/env python3
"""A14 bench (board #210): synthetic write-heavy + real-call accounting.

Model: 68 dirty rows, RTT 1.0s/call (measured ~1.2s on 2026-09-30),
legacy = workers=1 + WRITE_SLEEP=0.35 + QUERY_SLEEP=0.2 (pre-A14 defaults),
new    = workers=4 + RPS=2.5 pacing (A14 defaults).
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path("/home/chuck/.openclaw/workspace")
# Workspace layout uses scripts/, the git repo uses backend/ — accept both.
LJS_PATH = next(
    p for p in (ROOT / "scripts" / "log-statistics-sync.py", ROOT / "backend" / "log-statistics-sync.py") if p.exists()
)

# RTT model = measured Notion API latency (2026-09-30 baseline: ~1.2s/call,
# 15 query POSTs = 17.7s). Using 1.0s keeps the bench conservative.
RTT = {"GET": 1.0, "PATCH": 1.0, "POST": 1.0, "query": 1.0}


def load_ljs(env):
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("ljs_bench", LJS_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
    return mod


class SlowNotion:
    def __init__(self, rows, dirty_ids):
        self.pages = {r["id"]: {"last_edited_time": "2026-10-14T10:00:00.000Z",
                                "properties": dict(r.get("properties") or {})} for r in rows}
        self.dirty = set(dirty_ids)
        self.calls = 0
        self.mtx = threading.Lock()

    def __call__(self, method, path, body=None, timeout=60):
        with self.mtx:
            self.calls += 1
        if method == "POST" and path.endswith("/query"):
            time.sleep(RTT["query"])
            return 200, {"results": [{"id": pid, "properties": pg["properties"]} for pid, pg in self.pages.items()], "has_more": False}
        if method == "GET" and path.startswith("/v1/data_sources/"):
            time.sleep(RTT["GET"])
            return 200, {"properties": {}}
        if method == "POST" and path == "/v1/pages":
            time.sleep(RTT["POST"])
            return 200, {"id": "new-x"}
        pid = path.rsplit("/", 1)[-1]
        if method == "GET":
            time.sleep(RTT["GET"])
            return 200, dict(self.pages[pid])
        if method == "PATCH":
            time.sleep(RTT["PATCH"])
            self.pages[pid]["last_edited_time"] = "2026-10-14T12:00:00.000Z"
            self.pages[pid]["properties"].update(body.get("properties") or {})
            return 200, {"id": pid}
        raise AssertionError(method + path)


def bench(label, env, tasks, rows):
    tmp = Path(tempfile.mkdtemp(prefix="a14-bench-"))
    env = {**env, "ROOT": str(tmp), "WIDGETS_JSON": str(tmp / "s.json"),
           "LOG_STATS_RUN_STATE": str(tmp / "rs.json"), "LOG_STATS_JOURNAL": str(tmp / "j.jsonl"),
           "LOG_STATS_LOCK": str(tmp / "lock"), "LOG_STATISTICS_DSID": "ds"}
    ljs = load_ljs(env)
    (tmp / "s.json").write_text(json.dumps({"tasks": tasks, "generated_at": "2026-10-14T10:00:00Z"}))
    fake = SlowNotion(rows, [t["id"] for t in tasks])
    ljs._req = fake
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    t0 = time.monotonic()
    with redirect_stdout(buf):
        ljs.main()
    wall = time.monotonic() - t0
    out = json.loads(buf.getvalue().strip().splitlines()[-1])
    print(f"{label}: wall={wall:.1f}s calls={fake.calls} updated={out['updated']} failed={out['failed']}")
    return wall


def task(tid, name):
    return {"id": tid, "n": name, "n_full": name, "s": "Development",
            "start": "2026-10-01T00:00:00Z", "created": "2026-10-01T00:00:00Z",
            "edited": "2026-10-10T00:00:00Z"}


def build(n_dirty):
    tasks, rows = [], []
    for i in range(n_dirty):
        tid = f"aaaaaaaa-0000-4000-8000-{i:012d}"
        tasks.append(task(tid, f"Задача {i}"))
        rows.append({"id": f"p{i:03d}", "properties": {"Task": {"relation": [{"id": tid}]}}})
    return tasks, rows


if __name__ == "__main__":
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 68
    tasks, rows = build(N)
    legacy = {"LOG_STATS_WORKERS": "1", "LOG_STATS_WRITE_SLEEP_S": "0.35",
              "LOG_STATS_QUERY_SLEEP_S": "0.2", "LOG_STATS_RPS": "0"}
    new = {"LOG_STATS_WORKERS": "4", "LOG_STATS_WRITE_SLEEP_S": "0",
           "LOG_STATS_QUERY_SLEEP_S": "0", "LOG_STATS_RPS": "2.5"}
    w_old = bench(f"legacy-serial-{N}", legacy, tasks, rows)
    w_new = bench(f"a14-parallel-{N}", new, tasks, rows)
    print(f"speedup: {w_old / w_new:.2f}x ({w_old:.1f}s -> {w_new:.1f}s)")
