# Release widgets — GitLab CI (option B)

Board #72. Token never in git, chat, or job logs.

## What CI replaces

| Before (QA) | After (GitLab) |
|---|---|
| `python3 scripts/release-widget-sync.py serve` on `:8755` | **Not used** |
| Manual / cron `snapshot` | Pipeline **schedule** |
| nginx `/widgets/` self-signed | GitLab Pages (if enabled) or artifact copy to a CA-TLS host |

Iframe still loads only HTML + `release-data.json`.

## Repo files

- `.gitlab-ci.yml`
- `backend/release-widget-sync.py` — `NOTION_TOKEN` env, `ROOT` / `WIDGETS_JSON` / `QA_WWW_JSON` (defaults write `frontend/release-data.json`)
- `backend/publish-release-widgets.sh` — stages `frontend/public/`
- `frontend/` — HTML + JSON for the iframe
- `.gitignore` — `config/notion.json`, `config/gitlab.env`, `frontend/public/`

## GitLab setup

Public SaaS target (board #73): https://gitlab.com/qa-notion/Notion-statistic  
Pages URL after first `pages` job: `https://qa-notion.gitlab.io/Notion-statistic/`

Self-hosted `git.rdo.belhard.com` is optional; same CI file.

1. New project; push **without** `config/notion.json`.
2. **Settings → CI/CD → Variables**
   - `NOTION_TOKEN` — Masked + Protected. Integration token, not in chat.
   - Optional: `NOTION_VERSION` = `2026-03-11`
   - Optional: `NOTION_DATA_SOURCE_ID` (default in script is AI-Test ERP-Sprint).
3. Runner with egress to `https://api.notion.com`. Prefer a **project/private runner** on a clean VM, not a shared runner, because of the token.
4. **CI/CD → Schedules** — e.g. `*/30 * * * *` on the default branch. First run: **Run pipeline** (web) to test.
5. If **Pages** is enabled: job `pages` publishes `public/*.html` + `release-data.json`.
   Embed URLs:
   - `https://<pages-host>/<path>/release-charts.html`
   - `https://<pages-host>/<path>/status-dwell.html`
6. If Pages is **off**: download `frontend/public/` artifact or add a deploy job (`rsync`/`scp` to nginx+Let's Encrypt). Do not point Notion at a self-signed IP.

## Local / QA still works

```bash
python3 backend/release-widget-sync.py dry
python3 backend/release-widget-sync.py snapshot
bash backend/publish-release-widgets.sh
```

File `config/notion.json` remains valid on QA. Env `NOTION_TOKEN` overrides file token.

## Notion

Share Release-28 with integration **TEST_ERP_APS**, then PATCH embeds to the Pages URLs. GET page `71de17b6-8482-82d8-b568-01fcb02ae5dd` must be 200.

## Do not

- Commit token or `config/notion.json`
- Run `serve` in CI
- Log `$NOTION_TOKEN` (`echo`, `set -x` on the variable)
- Use REMnux as runner
