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
| `generated_at` | string ISO-8601 UTC `…Z` | yes | Shown in UI. Since board #196: the **Sprint data moment** (snapshot time) — never moved by re-enrich |
| `enriched_at` | string ISO-8601 UTC `…Z` | yes (board #196) | LOG-merge moment; refreshed on every `enrich` pass |
| `timezone` | string | yes (board #196) | Always `Europe/Minsk` — the payload display/aggregation timezone |
| `generated_at_local` | string ISO-8601 with offset (e.g. `2026-09-28T09:00:00+03:00`) | yes (board #196) | Same instant as `generated_at`, rendered in `Europe/Minsk` |
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
| `n` | string | Title (trimmed to ≤140 chars) |
| `n_full` | string | **Full** untruncated title (board #197, A10), same source as `n`. Display truncation happens in the UI only; `n` keeps its ≤140-char form for backward compatibility |
| `start` | string\|null | Start date |
| `created` | string\|null | `created_time` |
| `edited` | string\|null | `last_edited_time` |
| `history` | object[] | Status dwell timeline (board #165) — see below |

> **Identity note (board #197, A10):** task identity in diffs (e.g. the widget
> «Добавлены новые данные» block) is `tasks[].id` **only**. Never match tasks by
> `n`/`d`/`r` — truncated titles collide across different tasks. Use `n_full`
> for display of complete titles, not for identity.

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

## Board #195 (A24): stable pagination + dedup (additive)

- The data-source query (`POST /v1/data_sources/{id}/query`) is sent with a
  stable sort — `sorts: [{"timestamp": "last_edited_time", "direction":
  "ascending"}]` — so row order does not shift between pages.
- Rows arriving twice across a page seam are deduplicated by `row.id` (first
  occurrence wins) in **both** collectors (`snapshot()` Sprint rows and
  `query_log_rows()` LOG rows). The seam duplicate is counted, not dropped
  silently: `quality.duplicate_rows`.
- Invariant: `task_count == len(unique tasks[].id)`. A violation sets
  `quality.task_count_mismatch = 1` (plus `quality.task_count_unique` for
  diagnosis) — the gate/alert path sees it; nothing is silently skipped.

## Board #196 (A31+A6): split timestamps + timezone normalization (additive)

- **A31 — timestamp roles are separated:**
  - `generated_at` = moment the **Sprint data** was taken (`snapshot()`). It is
    immutable for the payload lifetime: `enrich` (LOG merge) must never move it.
    A legacy payload without the field is stamped once as a fallback.
  - `enriched_at` = moment of the **LOG merge**. It is refreshed on every
    `enrich`/re-enrich pass (and equals `generated_at` right after a snapshot,
    which merges LOG in the same run). Together they answer «данные от …,
    история от …» without overloading one field.
- **A6 — timezone normalization:**
  - `timezone: "Europe/Minsk"` and `generated_at_local` (same instant as
    `generated_at`, ISO-8601 with `+03:00`) travel with every payload.
  - `parse_ts` (both collectors) treats date-only `YYYY-MM-DD` as midnight
    **Europe/Minsk** (UTC+3, no DST) — never midnight UTC. Day aggregates and
    calendar day-boundaries are computed from these Minsk-midnight instants.
  - **Expected numeric shift:** tasks whose boundaries are date-only contribute
    durations differing by up to ±3 h (≈ ±0.05 day) from a UTC-midnight reading.
    This shift is by design — day boundaries now land at 21:00 `…Z` of the
    previous day (midnight Minsk).

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
| `task_count_unique` | number | A24 (board #195): number of unique `tasks[].id` values |
| `task_count_mismatch` | number | A24: invariant flag — `1` when `task_count` ≠ `task_count_unique`, else `0`. Never a silent skip |
| `duplicate_rows` | number | A24: page-seam duplicate rows dropped by the pagination dedup (first occurrence kept); snapshot-only, preserved by re-enrich |
| `drift_days` | number | N2 (board #184.1): worst per-task deficit `sum(segment days) − status column days` over all statuses; 0.0 when segments never exceed their columns (legacy accrual above columns is not drift) |
| `drift_items` | number | Count of (row, status) pairs with deficit > `DRIFT_DAYS_MIN` (1.0 day) |
| `unknown_statuses` | string[] | Copy of top-level `unknown_statuses` |
| `a19_conflicts` | number | A19 (board #185): concurrent-close collisions of the last collector run (from `config/log-statistics-run.json` `a19_conflicts`; 0 when absent). Webhook-writer collisions surface via `webhook.events_error` |
| `thresholds` | object | Gate inputs: `task_count_delta_pct` 10.0, `gap_hours` 6.0, `segments_negative` 0, `segments_absurd` 0, `drift_days` 1.0 |
| `webhook` | object\|null | Webhook observability (board #177.2/#184.3/#229): `events_total`, `events_processed`, `events_noop`, `events_skipped`, `events_error`, `events_ping` (synthetic worker heartbeats, additive), `last_event_at` (last real business event), `last_delivery_at` (last delivered event incl. ping/skipped), `gap_hours` (N1 — hours from the last delivery incl. synthetic ping to the snapshot; falls back to `last_event_at` for old run-state; `null` when unknown). `null`/absent when no run-state is available |

Rules:

- The quality gate (GH Actions step, board #177.3) FAILs the run when a metric
  exceeds `thresholds` — a red run IS the alert (no new runtime services on QA).
- Fields that cannot be recomputed at `enrich` time (`task_count_prev`,
  `task_count_delta_pct`, `truncated_titles`) are preserved from the snapshot
  pass, not zeroed by re-enrich.
- N2 drift is computed read-only from LOG rows in the enrich pass: both
  writers credit closed segments to their number column in the same patch, so
  per status `sum(segment days) <= column` is the invariant; a deficit above
  `DRIFT_DAYS_MIN` (rounding tolerance) means a lost column credit.
  `quality.drift_days` > `thresholds.drift_days` fails the gate.

## Board #202 (A17): checkpoint journal + resume (additive, NOT in payload)

Journals are QA/CI-side resume checkpoints — they never enter `release-data.json`
(no payload fields added). The audit trail remains run-state
(`config/log-statistics-run.json`, webhook run-state), not the journal.

| Journal | File (env override) | Written by | Entries |
|---|---|---|---|
| collector | `config/log-statistics-journal.jsonl` (`LOG_STATS_JOURNAL`) | `backend/log-statistics-sync.py` | `run` / `done` / `end` |
| snapshot fetch cache | `config/release-widget-journal.jsonl` (`WIDGETS_JOURNAL`) | `backend/release-widget-sync.py` | `run` / `cache` / `end` |

Rules:
- **Resume:** an unfinished run (last `run` line without a following `end`) is
  resumed when its `started_at` age ≤ 24h (`JOURNAL_MAX_AGE_S=86400`,
  `WIDGETS_JOURNAL_MAX_AGE_S` for the snapshot journal); older or finished →
  fresh `run_id`.
- **Collector:** items journaled as `done` (any outcome — created / updated /
  skipped / conflict / failed = attempted, no repeat needed) are skipped on
  resume; surfaced additively as `resumed` (bool) / `resume_skipped` (int) in
  the run-state/output. A19 winner-merge makes a replay safe anyway.
- **Snapshot fetch cache:** `query_pages` results (deduped rows + duplicates,
  A24) are checkpointed as `cache` lines and reused on resume only while fresh:
  `fetched_at` age ≤ 3600s (`SNAP_CACHE_MAX_AGE_S`, env). Older or missing
  cache → normal refetch (reads are idempotent). Applies to the task rows and
  to the LOG STATISTICS rows used by enrich.
- **`end` semantics:** the `end` line plus ZEROING the journal file = the run
  checkpoint is complete (journal ≠ audit log). On SIGTERM / exception `end` is
  NOT written — the next run resumes; a kill before a `cache` line just
  re-fetches next time (nothing lost, nothing duplicated).
- **CI persistence:** in `release-widgets.yml` both journal files survive
  ephemeral runners via `actions/cache` restore/save (key `resume-journal-*`,
  restore-keys prefix, last-write-wins).

## Board #205 (A26 остаток): staleness + reconciliation window (additive)

`quality` (дополнительно к A27):
- `staleness_hours` — возраст counts-снимка (`generated_at`, момент данных
  Sprint) в момент вычисления quality. `null` для payload без `generated_at`
  (никогда не подменяется на 0). Новые задачи/метрики (counts) старше окна —
  видимый quality-флаг, не тихая устаревшость.
- `staleness_warn_hours` (3.0) — порог WARN (non-blocking warning в gate).
- Порог `thresholds.staleness_hours = 12.0` — hard FAIL старше 12ч. Обоснование:
  штатный темп снимков = GH schedule `*/10` + кнопка + QA cron `2-59/10`, но
  GitHub schedule best-effort (наблюдались gap 2–5ч). WARN от 3ч (выше
  типичного gap), FAIL от 12ч (ночной простой + два неудачных прогона подряд —
  это уже потеря актуальности counts, релиз-гейт должен светиться).
- `recon_drift_hours` / `recon_window_hours` (6.0) — явное reconciliation-окно
  сверки двух писателей истории (webhook vs collector): модуль расхождения
  `last_delivery_at` (webhook run-state) и `last_at` (LOG run-state) в часах.
  Оба пишут Segments; расхождение > окна = дрейф источников = quality-флаг
  (fail-visible). `null` при ненаблюдаемой цепочке — проверка пропускается
  (правило как для `webhook: null`). Окно 6.0ч = тот же ритм, что N1 `gap_hours`.
- `recon_collector_last_at` / `recon_webhook_last_at` — опорные таймстампы для
  отладки расхождений.

Gate (`quality-gate.py`):
- `staleness_hours > thresholds.staleness_hours` → FAIL; между warn и max →
  non-blocking warning; `recon_drift_hours > thresholds.recon_drift_hours` → FAIL.
- Отсутствие полей (legacy-снимок) → проверка пропускается (никогда не
  false-fail).

## Board #203 (A18): детерминированные имена строк LOG STATISTICS (additive)

Решение владельца 2026-09-29: написание **`Sprint`** (старое «Sprit» — опечатка,
исправлено); формат ключа строки — вариант B.

- **Name строки LOG STATISTICS** (при CREATE / reuse пустой строки / rewrite):
  `Sprint: <Название задачи> [<id>]` — чистая функция задачи (без номера
  прогона и таймстампа): пересозданная строка получает идентичное имя,
  визуальных дублей нет. Поле имени: `n_full` (полное) иначе `n`; перевод
  строки сворачивается в пробел; имя обрезается до 300 символов.
- **`[<id>]` — полный task id** (UUID), не префикс: короткие префиксы внутри
  одной базы Notion не уникальны (общие префиксы UUID) — полный id и есть
  устойчивый ключ.
- **Run-метка** `Sprint №{run}({job}) {DD.MM.YYYY HH:MM:SS}` (Europe/Minsk)
  остаётся только меткой прогона в run-state (`last_title`) и output — в Name
  строк больше не попадает (раньше строки одного прогона получали одно имя,
  а resume-строки — другой таймстамп).
- **`LOG_STATS_TITLE`** — ручной override имени строк (ops/ручные прогоны);
  инкрементальный PATCH по-прежнему не переписывает Name каждый прогон.
  Миграция легаси-имён: однократный прогон с `LOG_STATS_REWRITE_TITLE=1`.
- Иммутабельность: детерминированное имя зависит только от `n_full`/`n`/`id`;
  переименование задачи в Notion меняет имя строки только при rewrite-прогоне.
