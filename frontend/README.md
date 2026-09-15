# Frontend — Release widgets (static)

Public iframe assets. **No Notion token. No OpenClaw `/widgets/sync`.**

| File | Role |
|---|---|
| `release-charts.html` | Estimate vs Time tracking (combobox, estimate vs spent) |
| `status-dwell.html` | Current status dwell; Done = diamond (moment), no duration bar |
| `release-data.json` | Snapshot written by Backend CI (`backend/release-widget-sync.py snapshot`) |
| `status-dwell-table-tests.json` | ≤5 QA rows merged into the snapshot by Backend |

## How the iframe loads data

1. `PUBLIC_BASE` is `""` → JSON is **same directory** as the HTML.
2. `assetUrl("release-data.json")` + cache-bust `?t=<Date.now()>`, `cache: "no-store"`.
3. If JSON is missing or HTTP error → fail-visible message in the widget (`syncMsg`), not a silent empty chart.
4. Refresh button **Обновить данные** POSTs the public CORS proxy `https://185.47.152.152.sslip.io/sync-notion` (no token in the iframe). The proxy dispatches GitHub Actions job **Sync Notion** (`workflow_dispatch` on `.github/workflows/release-widgets.yml`) with in-flight reuse so a second click does not start a parallel snapshot. After the job completes, the widget re-fetches `release-data.json`.

Do not set `PUBLIC_BASE` to any OpenClaw URL. Set it only if HTML and JSON live on different public hosts.

## Origin

GitLab Pages publishes these files from the `pages` job (copy of staged `frontend/public/`). Until the first successful snapshot job, the committed `release-data.json` is the bootstrap snapshot (no token).

Embed:

- `…/release-charts.html`
- `…/status-dwell.html`

Backend: [`../backend/README.md`](../backend/README.md)  
Contract: GitLab issue «Контракт данных Frontend↔Backend».
