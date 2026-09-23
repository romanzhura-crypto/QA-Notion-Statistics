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
| `table_tests` | — | **removed** | TEST fixture rows were removed (board #152); key no longer emitted |
| `token` | — | **forbidden** | Backend must not emit this key |

## `tasks[]` item

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Notion page id |
| `tid` | string\|null | Sprint `ID` unique_id, e.g. `TASK-42` (board #144) |
| `u` | string\|null | Notion page URL — row click target in tasks table (board #144) |
| `r` | string[] | Releases (multi-select names) |
| `d` | string | DEV assignee or `Unassigned` |
| `s` | string | Status name |
| `e` | number\|null | Estimate hours (`1d=8h`) |
| `t` | number\|null | Time spent hours |
| `n` | string | Title (trimmed) |
| `start` | string\|null | Start date |
| `created` | string\|null | `created_time` |
| `edited` | string\|null | `last_edited_time` |

## Cache-bust

Query `?t=<epoch-ms>` plus `Cache-Control: no-store` on fetch. Pages may still CDN-cache the file; busting avoids stale iframe data after a new snapshot.

## Fail-visible

If JSON is absent, Frontend must not look like an empty sprint. Show the Russian message that the snapshot is missing and is produced by Backend.
