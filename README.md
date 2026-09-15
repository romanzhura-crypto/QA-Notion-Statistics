# QA-Notion-Statistics — Release widgets

Public GitHub repo for Notion iframe widgets. Split:

| Path | Role | Public? |
|---|---|---|
| [`frontend/`](frontend/) | Static HTML/JS/CSS + `release-data.json` for Notion iframe | Yes (GitHub Pages) |
| [`backend/`](backend/) | Snapshot from Notion REST, publish staging | No (Actions only; `NOTION_TOKEN` is a repo secret) |
| [`.github/workflows/release-widgets.yml`](.github/workflows/release-widgets.yml) | validate → snapshot → Pages | — |
| [`docs/`](docs/) | Architecture, GitHub and GitLab CI | — |

Frontend **never** calls Notion, **never** embeds `NOTION_TOKEN` / `GITHUB_TOKEN`, and **never** calls OpenClaw `/widgets/sync`. JSON load is same-directory `release-data.json`. Button **Обновить данные** POSTs public CORS proxy `https://185.47.152.152.sslip.io/sync-notion` (token stays on QA) which dispatches Actions job **Sync Notion** and refuses a second dispatch while a run is queued/in_progress.

Backend job writes `frontend/release-data.json`; Pages artifact is `frontend/public/` (HTML + JSON same directory).

## Embed URLs (after GitHub Pages deploy)

- `https://romanzhura-crypto.github.io/QA-Notion-Statistics/release-charts.html`
- `https://romanzhura-crypto.github.io/QA-Notion-Statistics/status-dwell.html`

Setup: [`docs/release-widgets-github.md`](docs/release-widgets-github.md). Secret name: `NOTION_TOKEN`. Pages source: GitHub Actions.

GitHub workflow: [`.github/workflows/release-widgets.yml`](.github/workflows/release-widgets.yml) — validate on push/PR; snapshot + Pages on schedule or **Run workflow**.

GitLab mirror (optional, private Pages not usable as Notion origin): `.gitlab-ci.yml` + `docs/release-widgets-gitlab-ci.md`.

## Local

```bash
python3 backend/release-widget-sync.py dry
python3 backend/release-widget-sync.py snapshot   # needs NOTION_TOKEN
QA_WWW="" bash backend/publish-release-widgets.sh
```

Do not commit `config/notion.json`, `config/gitlab.env`, or `config/github.env`.
