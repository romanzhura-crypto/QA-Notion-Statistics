# Contract: `release-data.json` (Frontend ↔ Backend)

Path on Pages / artifact: **same directory as the HTML**, filename `release-data.json`.

Repo path written by Backend: `frontend/release-data.json`  
Staged for Pages: `frontend/public/release-data.json` → CI `public/release-data.json`

## Fetch rules (Frontend)

- URL: `assetUrl("release-data.json") + "?t=" + Date.now()`
- `cache: "no-store"`
- `PUBLIC_BASE = ""` unless HTML and JSON are on different hosts
- HTTP not OK or parse error → visible error in `syncMsg` (fail-visible)
- `ok === false` → treat as error (`p.error`)

## Top-level schema

| Field | Type | Required | Notes |
|---|---|---|---|
| `ok` | boolean | yes | `true` for a usable snapshot |
| `generated_at` | string ISO-8601 UTC `…Z` | yes | Shown in UI |
| `source` | string | yes | e.g. `Estimate vs Time tracking` |
| `data_source_id` | string UUID | yes | Notion data source (not a secret) |
| `task_count` | number | yes | `len(tasks)` |
| `estimate_rule` | string | yes | `1d=8h` |
| `status_dwell` | string | yes | `current_status_calendar_days` |
| `status_dwell_note` | string | yes | Changelog limitation note |
| `status_meta` | array `{name, color}` | yes | Notion status colors |
| `releases` | string[] | yes | Sorted; includes `Backlog` when present |
| `tasks` | object[] | yes | See below |
| `fixtures_excluded` | number | yes (board #164) | Fixture rows excluded from `tasks[]` (synthetic id / «тест» title marker) |
| `unknown_statuses` | string[] | yes (board #164 phase 2) | Statuses seen in data outside the hardcoded status lists; empty `[]` when none. Never silently dropped: their dwell stays in `tasks[].history` |
| `table_tests` | — | **removed** | TEST fixture rows were removed (board #152); key no longer emitted |
| `token` | — | **forbidden** | Backend must not emit this key |

## `tasks[]` item

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Notion page id |
| `tid` | string\|null | Sprint `ID` unique_id, e.g. `TASK-42` (board #144) |
| `u` | string\|null | Notion page URL — row click target in tasks table (board #144) |
| `r` | string[] | Releases (multi-select names) |
| `p` | string | Project (select name) or `""` when unset — «Проект» filter in status-dwell (board #153) |
| `d` | string | DEV assignee or `Unassigned` |
| `s` | string | Status name |
| `e` | number\|null | Estimate hours (`1d=8h`) |
| `t` | number\|null | Time spent hours |
| `n` | string | Title (trimmed) |
| `start` | string\|null | Start date |
| `created` | string\|null | `created_time` |
| `edited` | string\|null | `last_edited_time` |
| `history` | object[] | Status dwell timeline (board #165) — see below |

## `tasks[].history` (board #165, 2026-09-25)

One item per **interval**, in chronological order. Repeated statuses stay
separate items (e.g. `Ready For Dev → Development → Ready For QA → Development`
yields 4 items) — never summed into one bar per status.

| Kind | Shape | Meaning |
|---|---|---|
| Interval with timestamps | `{s, days, from, to}` | Closed interval. `from` = when the status was obtained (ISO-8601 UTC `…Z`), `to` = when it was left. `days` — minute-precision duration (`round(days*1440)/1440`); intervals of minutes survive (no hour rounding) |
| Open interval | `{s, days, from, to: null}` | Current status since `from` (collector `Status since`) — always its own item, even for legacy rows |
| Legacy remainder | `{s, days}` | Dwell accrued **before** per-interval collection existed (LOG number-column aggregate not covered by intervals). **No timestamps** — they are unknowable (Notion REST has no Version History) and must never be invented |
| Done | `{s: "Done", at}` | Terminal marker (Done at), not a dwell bar |

Rules:

- Source of intervals: LOG STATISTICS `Segments` rich_text property (JSON array
  `[{s, from, to}]`, chunked ≤2000 chars per text item), written by the
  collector `log-statistics-sync.py` on each status transition.
- Rows without `Segments` (or with empty/corrupt JSON) fall back to the legacy
  aggregate format: number columns per status + separate open-interval item.
  Tolerant parsing — bad JSON yields `[]`, never an error.
- Dwell number columns stay in the LOG schema for backward compatibility and
  remain **aggregates**; the snapshot emits only the part not covered by
  `Segments` (the legacy remainder) so totals never change.
- `from`/`to` are only present when actually known; the Frontend must render
  intervals without timestamps as-is (no invented dates).
- `days` is always ≥ 0; a >0 duration is never dropped (minute precision).

## Cache-bust

Query `?t=<epoch-ms>` plus `Cache-Control: no-store` on fetch. Pages may still CDN-cache the file; busting avoids stale iframe data after a new snapshot.

## Fail-visible

If JSON is absent, Frontend must not look like an empty sprint. Show the Russian message that the snapshot is missing and is produced by Backend.

## Board #164 quick wins (additive, no breaking changes)

- `fixtures_excluded`: number of QA-fixture rows dropped at `snapshot()` input. Fixture = synthetic id (non-UUID / `table-test*`) or title marker «тест»/«тестовая/ый/ое»/«table-test» at title start (brackets/dashes allowed). Conservative: «тестирование», «тест-драйв», mid-title «тест» are NOT fixtures.
- `Done at` (LOG STATISTICS) is immutable: fixed at the first Done transition; later `last_edited_time` never rewrites it (memory: LOG row value, fallback — Done `at` diamond in the published snapshot JSON).
- Date-only values (`YYYY-MM-DD`) are interpreted as midnight **Europe/Minsk (UTC+3)**, not UTC — server (`parse_ts`) and client (`parseTs` in status-dwell). Output strings keep their original format.

## Board #164 phase 2: unknown status coverage (additive)

- Statuses outside the hardcoded `LOG_STATUS_COLS` / `STATUS_COLS` lists (+ `Done`) are **never silently lost**. Read side collects dwell columns and the open interval dynamically from LOG row data; write side accounts closed elapsed for them in `unknown_statuses` and writes their own number column when LOG has one (schema check — never PATCHes a missing column).
- `unknown_statuses`: sorted list of unknown status names (LOG number columns, `Collected Status`, task current status). Warning is printed to stderr when non-empty. `Done` and the synthetic `Unknown` placeholder (missing Status) are not reported.

## Board #177 (phase 3, A27): `quality` block (additive)

Top-level `quality` object — snapshot self-diagnostics for the GH Actions quality
gate. Strictly additive: Frontend may ignore it. `token` stays forbidden.

| Field | Type | Meaning |
|---|---|---|
| `task_count_prev` | number\|null | `task_count` of the previously published snapshot; `null` on first ever snapshot |
| `task_count_delta_pct` | number\|null | Delta vs previous snapshot, ±% (round 2); `null` when no previous |
| `fixtures_excluded` | number | Copy of top-level `fixtures_excluded` |
| `tasks_without_history` | number | `tasks[]` items with no dwell `history` at all |
| `segments_negative` | number | History items with `days < 0`, `to < from` or unparseable duration (Done diamond excluded) |
| `segments_absurd` | number | Closed dwell intervals longer than 366 days (`SEGMENT_ABSURD_DAYS`) |
| `truncated_titles` | number | Titles longer than 140 chars (truncated into `tasks[].n`) |
| `unknown_statuses` | string[] | Copy of top-level `unknown_statuses` |
| `thresholds` | object | Gate inputs: `task_count_delta_pct` 10.0, `gap_hours` 6.0, `segments_negative` 0, `segments_absurd` 0, `drift_days` 1.0 |
| `webhook` | object\|null | Webhook observability (board #177.2): `events_processed`, `events_noop_stale`, `events_dupes`, `last_event_at`, `gap_hours` (N1 — hours since last event, `null` when unknown), `collector_vs_webhook_drift` (N2). `null`/absent when no run-state is available |

Rules:

- The quality gate (GH Actions step, board #177.3) FAILs the run when a metric
  exceeds `thresholds` — a red run IS the alert (no new runtime services on QA).
- Fields that cannot be recomputed at `enrich` time (`task_count_prev`,
  `task_count_delta_pct`, `truncated_titles`) are preserved from the snapshot
  pass, not zeroed by re-enrich.
- `collector_vs_webhook_drift` (N2): disagreement between LOG number-column
  aggregates and `Segments` coverage (webhook-written intervals) per LOG row;
  measured in days (`max`/`rows_over`) with tolerance `DRIFT_DAYS_MIN`.
