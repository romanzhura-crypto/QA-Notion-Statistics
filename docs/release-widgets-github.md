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

Frontend still never calls Notion or `/widgets/sync`.

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

## Why not push from QA now

QA VM has **no** `GITHUB_TOKEN` / `GH_*` / `config/github.env` / `gh` CLI. Chuck cannot create the GitHub repo or push until the owner provides:

- GitHub org or user + repo URL
- PAT or deploy key with `repo` + `workflow` (write to `config/github.env` on QA only — **do not paste in chat**)

Mirror option: GitHub repo import from `https://gitlab.com/qa-notion/Notion-statistic` after workflow files are on `main`.

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
