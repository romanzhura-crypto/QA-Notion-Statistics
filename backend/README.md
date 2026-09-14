# Backend — Notion snapshot for widgets

Private. Talks to Notion REST. Writes JSON for Frontend. Token never printed.

## Scripts

| File | Role |
|---|---|
| `release-widget-sync.py` | `dry` (no Notion call) / `snapshot` (query data source → JSON) / `serve` (loopback `:8755` operators only, **not** for iframe) |
| `publish-release-widgets.sh` | Copy HTML+JSON to `frontend/public/` and run static guards (no `/widgets/sync`, no token-like strings, no JSON key `token`) |

## Token

- CI: `NOTION_TOKEN` (Settings → CI/CD → Variables, Masked + Protected). Optional `NOTION_VERSION`, `NOTION_DATA_SOURCE_ID`.
- Local: env `NOTION_TOKEN` **or** `config/notion.json` (gitignored). Env overrides file.
- Never commit the token. Never log it (`echo`, `set -x`).

## What snapshot writes

Default paths (overridable by env):

- `WIDGETS_JSON` / workspace JSON → `frontend/release-data.json`
- `QA_WWW_JSON` → same in CI; on QA host may also write `/var/www/openclaw/widgets/release-data.json` if that directory exists
- Merges `frontend/status-dwell-table-tests.json` into `tasks`

CI wiring (`.gitlab-ci.yml`):

1. `validate` — `py_compile` + `dry` + no `config/notion.json`
2. `snapshot` — `snapshot` + `publish-release-widgets.sh` → artifacts `frontend/public/`
3. `pages` — copies HTML + JSON to `public/` for GitLab Pages

Schedule the pipeline on `main` (e.g. every 30 minutes). First test: **Run pipeline** (web).

`serve` is not used in CI.
