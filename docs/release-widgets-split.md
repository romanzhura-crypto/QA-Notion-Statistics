# Release widgets: process split (review gate)

**Date:** 2026-09-07  
**Board:** #67  
**Verdict for this host:** local split **PASS**; public TLS deploy + Notion embed PATCH **BLOCKED**.

## Goal

Notion iframe must load only static HTML/CSS/JS + public JSON.  
Notion REST, integration token, and snapshot generation stay on a backend that is **not** the public OpenClaw vhost.

## AS-IS (QA 185.47.152.152)

| Piece | Evidence |
|---|---|
| nginx | `/etc/nginx/sites-enabled/openclaw` `location /widgets/` → `/var/www/openclaw/widgets/`; `location = /widgets/sync` → `127.0.0.1:8755` |
| TLS on OpenClaw | self-signed `CN=185.47.152.152` (`/etc/nginx/ssl/openclaw.crt`) — **not** valid public TLS for Notion |
| sync process | `python3 .../scripts/release-widget-sync.py serve` historically pid 964371, loopback `:8755` |
| HTML (old) | `fetch('/widgets/sync')` same-origin from iframe |
| token | `config/notion.json` key `token` (never printed) |
| data source | `8ade17b6-8482-825d-9d87-8789ccdd1242` |
| Notion page Release-28 | id `71de17b6-8482-82d8-b568-01fcb02ae5dd` — GET **404** (`object_not_found`, not shared with integration TEST_ERP_APS) |

## TO-BE

```
[Notion page iframe]
        |  HTTPS Universal SSL (Cloudflare Pages / *.pages.dev)
        v
[static origin]  release-charts.html
                 status-dwell.html
                 release-data.json     ← public snapshot, no token
        ^
        |  publish (cp / wrangler / gh)  — credentials not on this VM
        |
[QA backend, private]
  release-widget-sync.py snapshot
  config/notion.json (token)
  Notion REST query data_source
  optional loopback HTTP :8755 for operators only
```

Rules:

1. Iframe **never** calls `/widgets/sync`, OpenClaw, or Notion REST.
2. Token never appears in HTML, JSON, board comments, or docs.
3. nginx OpenClaw vhost is **not** the Notion origin. Existing `/widgets/sync` may stay for QA operators; it is not used by the widgets.
4. No paid accounts. Prefer Cloudflare Pages Universal SSL when CF/GitHub token exists.
5. This VM has **no** `CF_*` / `GITHUB_*` / `GH_*` credentials → cannot push `*.pages.dev` from here.

## Operator loop (fast)

```bash
# 1) Snapshot (writes widgets/release-data.json + /var/www/openclaw/widgets/release-data.json)
python3 /home/chuck/.openclaw/workspace/scripts/release-widget-sync.py snapshot

# sanity without Notion:
python3 /home/chuck/.openclaw/workspace/scripts/release-widget-sync.py dry

# 2) Stage HTML+JSON (no token, no /sync)
bash /home/chuck/.openclaw/workspace/scripts/publish-release-widgets.sh

# 3) Optional copy to a local Pages working copy:
WIDGETS_PUBLISH_DIR=/path/to/pages-project bash .../publish-release-widgets.sh
```

GitLab CI option B (board #72): `.gitlab-ci.yml` + `docs/release-widgets-gitlab-ci.md`. Schedule runs `snapshot`; Pages job if enabled. Token = CI variable `NOTION_TOKEN` only.

Then deploy `widgets/public/` to Cloudflare Pages, GitLab Pages, or any Universal-SSL static host.  
Notion embeds:

- `https://<pages-host>/release-charts.html`
- `https://<pages-host>/status-dwell.html`

`PUBLIC_BASE` in each HTML is `""` (same-directory JSON). Set it only if HTML and JSON live on different public hosts.

## Notion PATCH

PATCH two embed blocks on page `71de17b6-8482-82d8-b568-01fcb02ae5dd` **only if** GET page is 200 and the page is shared with the integration.

This run: GET page → **404**. Do not invent a share. No PATCH.

Unblock: share Release-28 with integration TEST_ERP_APS, provide a public Pages URL, then PATCH.

## Regression kept in HTML

- Combobox релизов (filter / exact / arrows)
- Estimate vs Time tracking columns + colors
- status-dwell: current status only; Done = diamond (moment), no duration bar
