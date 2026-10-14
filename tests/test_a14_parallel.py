#!/usr/bin/env python3
"""A14 (board #210) tests: parallel collector writes + per-page stripe locks +
request pacing. Deterministic, no network — _req is stubbed (the single HTTP
seam), state files live in temp dirs.

Run: python3 tests/test_a14_parallel.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LJS_PATH = next(
    p for p in (ROOT / "scripts" / "log-statistics-sync.py", ROOT / "backend" / "log-statistics-sync.py") if p.exists()
)


def load_ljs(tmp: Path, workers: str = "4", extra: dict | None = None):
    """Fresh module instance with isolated env (env is read at import time)."""
    env = {
        "ROOT": str(tmp),
        "WIDGETS_JSON": str(tmp / "snapshot.json"),
        "LOG_STATS_RUN_STATE": str(tmp / "run-state.json"),
        "LOG_STATS_JOURNAL": str(tmp / "journal.jsonl"),
        "LOG_STATS_LOCK": str(tmp / ".log-write.lock"),
        "LOG_STATISTICS_DSID": "ds-test",
        "LOG_STATS_WORKERS": workers,
        "LOG_STATS_RPS": "0",  # pacing off for test speed (throttle tested below)
        "LOG_STATS_WRITE_SLEEP_S": "0",
    }
    if extra:
        env.update(extra)
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(
            f"ljs_{workers}_{next(tempfile._get_candidate_names())}", LJS_PATH
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
    return mod


class FakeNotion:
    """In-memory Notion: pages store (last_edited_time, properties)."""

    def __init__(self, rows, latency=0.0):
        self.latency = latency
        self.pages = {r["id"]: {"last_edited_time": "2026-10-14T10:00:00.000Z",
                                "properties": dict(r.get("properties") or {})}
                      for r in rows}
        self.calls = {"GET_page": 0, "GET_ds": 0, "PATCH": 0, "POST_page": 0, "query": 0}
        self.patched = {}
        self.mtx = threading.Lock()
        self.created_ids = []

    def __call__(self, method, path, body=None, timeout=60):
        if self.latency:
            time.sleep(self.latency)
        with self.mtx:
            if method == "POST" and path.endswith("/query"):
                self.calls["query"] += 1
                return 200, {"results": [dict(r, object="page") for r in (
                    {"id": pid, "properties": pg["properties"]} for pid, pg in self.pages.items()
                )], "has_more": False}
            if method == "GET" and path.startswith("/v1/data_sources/"):
                self.calls["GET_ds"] += 1
                return 200, {"properties": {}}
            if method == "POST" and path == "/v1/pages":
                self.calls["POST_page"] += 1
                nid = f"new-{len(self.created_ids)}"
                self.created_ids.append(nid)
                self.pages[nid] = {"last_edited_time": "2026-10-14T12:00:00.000Z",
                                   "properties": dict(body.get("properties") or {})}
                return 200, {"id": nid}
            pid = path.rsplit("/", 1)[-1]
            if method == "GET":
                self.calls["GET_page"] += 1
                return 200, dict(self.pages[pid])
            if method == "PATCH":
                self.calls["PATCH"] += 1
                self.patched[pid] = self.patched.get(pid, 0) + 1
                self.pages[pid]["properties"].update(body.get("properties") or {})
                self.pages[pid]["last_edited_time"] = "2026-10-14T12:00:00.000Z"
                return 200, {"id": pid}
        raise AssertionError(f"unexpected {method} {path}")


def task(tid, name, status="Development"):
    return {"id": tid, "n": name, "n_full": name, "s": status,
            "start": "2026-10-01T00:00:00Z", "created": "2026-10-01T00:00:00Z",
            "edited": "2026-10-10T00:00:00Z"}


def fixture():
    """12 dirty update rows + 1 clean row + 1 empty (reuse) + 2 create tasks."""
    tasks = []
    rows = []
    for i in range(12):
        tid = f"aaaaaaaa-0000-4000-8000-000000000{i:03d}"
        tasks.append(task(tid, f"Задача {i}"))
        rows.append({"id": f"p{i:03d}", "properties": {"Task": {"relation": [{"id": tid}]}}})
    # clean row: seeded so needs_update is False (collected == current)
    tid_clean = "bbbbbbbb-0000-4000-8000-000000000001"
    tasks.append(task(tid_clean, "Чистая"))
    rows.append({"id": "pClean", "properties": {"Task": {"relation": [{"id": tid_clean}]}}})
    # empty row (no Task relation) -> reuse pool
    rows.append({"id": "pEmpty", "properties": {}})
    # two tasks with no LOG row -> create
    for i in range(2):
        tid = f"cccccccc-0000-4000-8000-00000000000{i}"
        tasks.append(task(tid, f"Новая {i}"))
    return tasks, rows


def run_main(ljs, tasks, rows, fake):
    ljs._req = fake
    tmp = Path(ljs.SNAPSHOT).parent
    (tmp / "snapshot.json").write_text(json.dumps({"tasks": tasks, "generated_at": "2026-10-14T10:00:00Z"}))
    # clean row must look "already collected": seed it from the collector itself
    for r in rows:
        if r["id"] == "pClean":
            tid = r["properties"]["Task"]["relation"][0]["id"]
            t = next(x for x in tasks if x["id"] == tid)
            seed = ljs.collector_transition(t, {"properties": {}}, now=ljs.parse_ts("2026-10-14T11:00:00Z"))
            r["properties"].update(seed)
            r["properties"]["Task"] = {"relation": [{"id": tid}]}
    # keep the fake in sync with the (seeded) row fixtures
    for r in rows:
        fake.pages[r["id"]]["properties"] = dict(r.get("properties") or {})
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        ljs.main()
    out = json.loads(buf.getvalue().strip().splitlines()[-1])
    return out


def test_parallel_main_matches_serial():
    """workers=4 must produce the same counters as legacy workers=1 and patch
    every dirty page exactly once (no lost/duplicated writes)."""
    results = {}
    fakes = {}
    for workers in ("1", "4"):
        tmp = Path(tempfile.mkdtemp(prefix=f"a14-w{workers}-"))
        ljs = load_ljs(tmp, workers=workers)
        tasks, rows = fixture()
        fake = FakeNotion(rows)
        out = run_main(ljs, tasks, rows, fake)
        results[workers] = out
        fakes[workers] = fake
        assert out["ok"] is True, out
        assert out["a19_conflicts"] == 0, out
    a, b = results["1"], results["4"]
    for key in ("created", "updated", "skipped", "failed", "archived_empty", "archived_dup", "archived_unlinked"):
        assert a[key] == b[key], (key, a, b)
    assert a["created"] == 1, a          # one POST create (pEmpty reused by the other)
    assert a["updated"] == 13, a         # 12 dirty rows + pEmpty reuse
    assert a["skipped"] == 1, a          # clean row
    for workers, fake in fakes.items():
        for pid, n in fake.patched.items():
            assert n == 1, (workers, pid, n)  # exactly one PATCH per row
        assert fake.calls["POST_page"] == 1, fake.calls
    print("PASS test_parallel_main_matches_serial")


def test_parallel_wall_faster_than_serial():
    """The regression test the counter comparison cannot see: a global (unkeyed)
    a19_lock serializes writes and silently kills the A14 speedup. With 50ms
    simulated RTT and 14 writes, workers=4 must clearly beat workers=1."""
    walls = {}
    for workers in ("1", "4"):
        tmp = Path(tempfile.mkdtemp(prefix=f"a14-wall-{workers}-"))
        ljs = load_ljs(tmp, workers=workers)
        tasks, rows = fixture()
        fake = FakeNotion(rows, latency=0.05)
        t0 = time.monotonic()
        run_main(ljs, tasks, rows, fake)
        walls[workers] = time.monotonic() - t0
    assert walls["4"] < walls["1"] * 0.75, walls  # ~4x on 14 writes, 0.75 is safe
    print(f"PASS test_parallel_wall_faster_than_serial ({walls['1']:.2f}s -> {walls['4']:.2f}s)")


def test_stripe_lock_same_key_serializes():
    tmp = Path(tempfile.mkdtemp(prefix="a14-lock-"))
    ljs = load_ljs(tmp)
    events = []

    def worker(tag):
        with ljs.a19_lock("page-1"):
            events.append((tag, "enter"))
            time.sleep(0.08)
            events.append((tag, "exit"))

    ths = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b")]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    # critical sections must not interleave: exits immediately follow enters
    assert [k for _, k in events] == ["enter", "exit", "enter", "exit"], events
    print("PASS test_stripe_lock_same_key_serializes")


def test_stripe_lock_different_keys_parallel():
    tmp = Path(tempfile.mkdtemp(prefix="a14-lock2-"))
    ljs = load_ljs(tmp)
    t0 = time.monotonic()

    def worker(key):
        with ljs.a19_lock(key):
            time.sleep(0.3)

    ths = [threading.Thread(target=worker, args=(f"page-{i}",)) for i in range(2)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = time.monotonic() - t0
    assert wall < 0.55, f"same-stripe false sharing? wall={wall}"  # ~0.3 if parallel
    # legacy no-arg lock still works (global file)
    with ljs.a19_lock():
        pass
    print("PASS test_stripe_lock_different_keys_parallel")


def test_throttle_paces_requests():
    tmp = Path(tempfile.mkdtemp(prefix="a14-rps-"))
    ljs = load_ljs(tmp)
    ljs.RPS = 50.0
    t0 = time.monotonic()
    for _ in range(5):
        ljs._throttle()
    wall = time.monotonic() - t0
    assert wall >= 0.05, wall  # 4 intervals x 1/50s = 80ms nominal
    ljs.RPS = 0
    t0 = time.monotonic()
    for _ in range(5):
        ljs._throttle()
    assert time.monotonic() - t0 < 0.05
    print("PASS test_throttle_paces_requests")


if __name__ == "__main__":
    test_parallel_main_matches_serial()
    test_parallel_wall_faster_than_serial()
    test_stripe_lock_same_key_serializes()
    test_stripe_lock_different_keys_parallel()
    test_throttle_paces_requests()
    print("OK 5 tests")
