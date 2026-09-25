# Cloudflare edge adapter (board #171, architecture B)

Minimal edge: Notion Webhooks → GitHub Actions (`repository_dispatch`), widget
button → `workflow_dispatch`. No Notion writes here — GitHub Actions owns all
business logic (`backend/status-webhook-event.py`, `backend/log-statistics-sync.py`).

## Deploy (one-time)

```bash
cd status-webhook-worker
npx wrangler login                # or CLOUDFLARE_API_TOKEN
npx wrangler deploy               # → https://notion-status-webhook.<account>.workers.dev
npx wrangler secret put GH_PAT              # GitHub PAT: actions:write
npx wrangler secret put VERIFICATION_TOKEN  # from the one-time verification POST (see below)
```

## Notion subscription (connection settings UI, owner action)

1. https://app.notion.com/developers/connections → your connection → **Webhooks** → **+ Create a subscription**.
2. Webhook URL = `https://notion-status-webhook.<account>.workers.dev/webhook/notion`.
3. Subscribe to **page.properties_updated** (event for Status changes).
4. Notion sends a one-time verification POST → read `verification_token` from
   Worker logs (`NOTION_VERIFICATION_TOKEN ...`) → `wrangler secret put VERIFICATION_TOKEN`
   → click **Verify subscription**.

## Routes

| Route | Purpose | Response |
|---|---|---|
| `POST /webhook/notion` | verify HMAC (X-Notion-Signature) → `repository_dispatch` type `notion-webhook` | `{ok, forwarded}` |
| `POST /sync-notion` | widget button / cron → `workflow_dispatch` of `release-widgets.yml` with in-flight reuse | `{ok, reused, run_id?}` |
