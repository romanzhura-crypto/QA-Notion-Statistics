# Release widgets — GitHub Actions + Pages (GitLab alternative)

Board #93. Token never in git, chat, or job logs.

GitLab path stays in `.gitlab-ci.yml`. This file is the **same Frontend←Backend JSON contract** on GitHub, because:

- GitLab.com pipeline `2847859925` failed with **0 jobs**
- project/Pages are **private** → Notion iframe cannot load GitLab Pages
- GitHub Pages on a **public** repo is a CA-TLS origin Notion can embed

## Flow

```
[GitHub Actions, private secret]
  backend/release-widget-sync.py snapshot   # NOTION_TOKEN
  backend/publish-release-widgets.sh
        |
        v
[GitHub Pages, public static]
  /release-charts.html
  /status-dwell.html
  /release-data.json     ← Frontend fetch only this
        ^
        |
[Notion iframe]
```

Frontend still never calls Notion or OpenClaw `/widgets/sync`. Button **Обновить данные** POSTs `https://185.47.152.152.sslip.io/sync-notion` (Let's Encrypt). QA proxy uses `config/github.env` (gitignored) to `workflow_dispatch` job **Sync Notion** and reuses an in-flight run instead of starting a second snapshot.

## Create the GitHub repo

1. New repo (recommended **public**, otherwise Pages needs a paid plan).
2. No `config/notion.json` / tokens in git.
3. Push this tree (`frontend/`, `backend/`, `.github/workflows/release-widgets.yml`).
4. **Settings → Secrets and variables → Actions → New repository secret**
   - Name **exactly** `NOTION_TOKEN`
   - Value = Notion integration token (same as GitLab intended secret)
5. **Settings → Pages → Source = GitHub Actions**
6. **Actions → release-widgets → Run workflow** (workflow_dispatch). Schedule is every 30 minutes after that.
7. Embed (replace owner/repo):
   - `https://<owner>.github.io/<repo>/release-charts.html`
   - `https://<owner>.github.io/<repo>/status-dwell.html`

User site (`https://<owner>.github.io/`) works if Pages is deployed to root; this workflow uploads `frontend/public/` as the Pages artifact root, so HTML and JSON are same-directory (`PUBLIC_BASE=""`).

## Sync Notion button (board #96)

1. Widget POST `/sync-notion` on `185.47.152.152.sslip.io`.
2. QA `github-sync-notion-proxy` (loopback `:8756`, nginx SNI) reads `GITHUB_TOKEN` from `config/github.env` only.
3. If Actions already has queued/in_progress run for this workflow → HTTP 200 `reused: true` (no second dispatch).
4. Else POST `actions/workflows/release-widgets.yml/dispatches` with `inputs.source=widget`.
5. Workflow concurrency group `sync-notion-pages` (`cancel-in-progress: false`) serializes snapshot+Pages.
6. Widget polls GET `/sync-notion?run_id=` until `status=completed`, then re-fetches `release-data.json`.

Do not put the PAT in HTML, Pages, board comments, or chat.

## QA still

```bash
python3 backend/release-widget-sync.py dry
python3 backend/release-widget-sync.py snapshot   # needs NOTION_TOKEN
QA_WWW="" bash backend/publish-release-widgets.sh
```

## Do not

- Commit the token
- Point Notion at `https://185.47.152.152/widgets/` (self-signed)
- Expect a **private** GitHub repo Pages URL to work in Notion without a paid Pages plan
