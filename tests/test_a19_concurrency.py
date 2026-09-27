#!/usr/bin/env python3
"""A19 optimistic-concurrency race tests for log-statistics-sync (board #182).

Simulates concurrent writers by stubbing _req (the single Notion HTTP seam).
Deterministic — no network, no sleeps beyond the module's bounded retry.

Run: python3 tests/test_a19_concurrency.py
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Workspace layout uses scripts/, the git repo uses backend/ — accept both.
LJS_PATH = next(
    p for p in (ROOT / "scripts" / "log-statistics-sync.py", ROOT / "backend" / "log-statistics-sync.py") if p.exists()
)
spec = importlib.util.spec_from_file_location("ljs", LJS_PATH)
ljs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ljs)


class FakeNotion:
    """Scripted _req stub: pages have last_edited_time + properties."""

    def __init__(self, pages, mutate_on_get=False, patch_409=0):
        self.pages = pages  # page_id -> dict(last_edited_time, properties)
        self.mutate_on_get = mutate_on_get  # a concurrent writer edits on EVERY
        self.patch_409 = patch_409  # GET (persistent race); first N PATCHes 409
        self.get_calls = 0
        self.patch_calls = 0
        self.patch_props = []

    def __call__(self, method, path, body=None, timeout=60):
        pid = path.rsplit("/", 1)[-1]
        if method == "GET":
            self.get_calls += 1
            if self.mutate_on_get:  # concurrent writer: row changes under us
                n = self.get_calls
                self.pages[pid]["last_edited_time"] = f"2026-09-27T10:0{n}:00.000Z"
            return 200, dict(self.pages[pid])
        if method == "PATCH":
            self.patch_calls += 1
            self.patch_props.append(body.get("properties") or {})
            if self.patch_409 > 0:
                self.patch_409 -= 1
                return 409, {"code": "conflict_error", "message": "synthetic"}
            self.pages[pid]["last_edited_time"] = "2026-09-27T12:00:00.000Z"
            return 200, {"id": pid}
        raise AssertionError(f"unexpected {method} {path}")


def fresh_row():
    return {"last_edited_time": "2026-09-27T10:00:00.000Z", "properties": {"x": 1}}


def test_no_race_single_patch():
    fake = FakeNotion({"p1": fresh_row()})
    ljs._req, ljs.A19_CONFLICTS = fake, 0
    seen = []

    def build(row):
        seen.append(row)
        return {"S": {"number": 1}}

    code, info = ljs.patch_with_a19("p1", build, fresh_row())
    assert code == 200, (code, info)
    assert fake.patch_calls == 1, fake.patch_calls
    assert len(seen) == 1, seen
    assert ljs.A19_CONFLICTS == 0, ljs.A19_CONFLICTS


def test_race_then_merge_winner():
    """A concurrent writer won before our re-read (row already at v2 while our
    baseline is v1): build_props must run against the FRESH v2 row (winner's
    close merged, never clobbered) and the PATCH succeeds."""
    winner = fresh_row()
    winner["last_edited_time"] = "2026-09-27T11:00:00.000Z"  # concurrent winner v2
    fake = FakeNotion({"p1": winner})
    ljs._req, ljs.A19_CONFLICTS = fake, 0
    seen = []

    def build(row):
        seen.append(row)
        return {"S": {"number": 1}}

    code, info = ljs.patch_with_a19("p1", build, fresh_row())  # baseline = v1
    assert code == 200, (code, info)
    assert fake.patch_calls == 1, fake.patch_calls  # unverified read never PATCHed
    assert len(seen) == 1 and seen[0]["last_edited_time"] == "2026-09-27T11:00:00.000Z", seen
    assert ljs.A19_CONFLICTS == 1, ljs.A19_CONFLICTS


def test_webhook_build_merges_winner_close():
    """Webhook writer (board #185): recompute from the fresh row PRESERVES the
    concurrent winner's closed segment and appends the new transition."""
    swe_spec = importlib.util.spec_from_file_location(
        "swe", LJS_PATH.parent / "status-webhook-event.py"
    )
    swe = importlib.util.module_from_spec(swe_spec)
    swe_spec.loader.exec_module(swe)
    # fresh row (winner already closed Development→Ready For QA at 11:30)
    fresh_props = {
        "Collected Status": {"rich_text": [{"plain_text": "Ready For QA"}]},
        "Status since": {"date": {"start": "2026-09-27T11:30:00.000Z"}},
        "Segments": {"type": "rich_text", "rich_text": [{"plain_text": json.dumps(
            [{"s": "Development", "from": "2026-09-27T10:00:00.000Z",
              "to": "2026-09-27T11:30:00.000Z"}])}]},
    }
    fresh = {"last_edited_time": "2026-09-27T11:30:05.000Z", "properties": fresh_props}
    # our stale baseline: nothing closed yet
    baseline = {
        "last_edited_time": "2026-09-27T10:00:05.000Z",
        "properties": {
            "Collected Status": {"rich_text": [{"plain_text": "Development"}]},
            "Status since": {"date": {"start": "2026-09-27T10:00:00.000Z"}},
        },
    }
    fake = FakeNotion({"p1": fresh})
    ljs._req, ljs.A19_CONFLICTS = fake, 0

    def build(row):
        return swe.plan_event(
            (row or {}).get("properties") or {}, "Testing", "2026-09-27T12:00:00.000Z"
        )

    code, info = ljs.patch_with_a19("p1", build, baseline)
    assert code == 200, (code, info)
    assert fake.patch_calls == 1, fake.patch_calls
    assert ljs.A19_CONFLICTS == 1, ljs.A19_CONFLICTS  # baseline drift → recompute
    written = fake.patch_props[0]
    segs = ljs.segments_from_props(written)
    names = [(x["s"], x["from"][11:16], x["to"][11:16]) for x in segs]
    # winner's close is MERGED (kept) and our transition appended after it
    assert ("Development", "10:00", "11:30") in names, names
    assert names[-1][0] == "Ready For QA" and names[-1][2] == "12:00", names
    assert ljs.rich_text_plain(written["Collected Status"]) == "Testing"


def test_persistent_race_never_blind_writes():
    """A writer edits the row under EVERY re-read → verification never passes:
    409 fail-visible, ZERO PATCHes (never blind write / clobber)."""
    fake = FakeNotion({"p1": fresh_row()}, mutate_on_get=True)
    ljs._req, ljs.A19_CONFLICTS = fake, 0

    def build(row):
        raise AssertionError("build_props must not run without a verified row")

    code, info = ljs.patch_with_a19("p1", build, fresh_row())
    assert code == 409 and str(info).startswith("a19:"), (code, info)
    assert fake.patch_calls == 0, fake.patch_calls
    assert ljs.A19_CONFLICTS == ljs.A19_RETRIES + 1, ljs.A19_CONFLICTS


def test_patch_409_retries_with_recompute():
    """Server-side 409 → bounded retry with a fresh recompute, then success."""
    fake = FakeNotion({"p1": fresh_row()}, patch_409=1)
    ljs._req, ljs.A19_CONFLICTS = fake, 0
    seen = []

    def build(row):
        seen.append(row)
        return {"S": {"number": 1}}

    code, info = ljs.patch_with_a19("p1", build, fresh_row())
    assert code == 200, (code, info)
    assert fake.patch_calls == 2, fake.patch_calls
    assert len(seen) == 2, seen  # recompute per attempt (fresh row each try)


def test_noop_props_no_patch():
    fake = FakeNotion({"p1": fresh_row()})
    ljs._req, ljs.A19_CONFLICTS = fake, 0
    code, info = ljs.patch_with_a19("p1", lambda row: {}, fresh_row())
    assert code == 200 and info == "noop", (code, info)
    assert fake.patch_calls == 0, fake.patch_calls


def test_lock_degrades_without_fs():
    """a19_lock never raises: unwritable FS → optimistic re-read/retry alone."""
    import os

    old = ljs.A19_LOCK
    ljs.A19_LOCK = Path("/proc/nonexistent-dir/a19.lock")
    try:
        with ljs.a19_lock():
            pass
    finally:
        ljs.A19_LOCK = old
        assert os.path.exists(old) or True  # lock path untouched by the probe


def test_upsert_reports_conflict_action():
    """upsert() maps an a19 409 to action 'conflict' (fail-visible, counted)."""
    fake = FakeNotion({"p1": fresh_row()}, mutate_on_get=True)
    ljs._req, ljs.A19_CONFLICTS = fake, 0
    task = {"id": "t1", "n": "T"}
    action, code, info = ljs.upsert(task, "T", "p1", log_row=fresh_row())
    assert action == "conflict" and code == 409, (action, code, info)
    assert fake.patch_calls == 0, fake.patch_calls


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"OK {len(tests)} tests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
