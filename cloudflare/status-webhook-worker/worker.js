// Cloudflare Worker — minimal edge adapter (board #171, architecture B).
// Role: (1) verify Notion webhook HMAC, (2) forward events to GitHub Actions
// via repository_dispatch, (3) serve the widget "Обновить данные" button as a
// CORS relay to workflow_dispatch (replaces github-sync-notion-proxy on QA VM).
// NO business logic here. GitHub Actions owns all Notion writes.
//
// Secrets (wrangler secret put ...):
//   GH_PAT              — GitHub PAT, minimal scope: actions:write (+ contents:read)
//   VERIFICATION_TOKEN  — Notion webhook verification_token (optional in setup mode)
// Vars:
//   GH_OWNER            — default romanzhura-crypto
//   GH_REPO             — default QA-Notion-Statistics
//   SYNC_WORKFLOW       — default release-widgets.yml (job "Sync Notion")

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "POST, OPTIONS",
  "access-control-allow-headers": "content-type",
};

function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status: status || 200,
    headers: { "content-type": "application/json; charset=utf-8", ...CORS },
  });
}

async function hmacHex(secret, body) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, "0")).join("");
}

async function ghApi(env, method, path, body) {
  const owner = env.GH_OWNER || "romanzhura-crypto";
  const repo = env.GH_REPO || "QA-Notion-Statistics";
  const res = await fetch("https://api.github.com" + path, {
    method,
    headers: {
      "authorization": "Bearer " + env.GH_PAT,
      "accept": "application/vnd.github+json",
      "user-agent": "status-webhook-worker",
      ...(body ? { "content-type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  return { status: res.status, data };
}

async function handleNotion(env, raw, request) {
  let body;
  try { body = JSON.parse(raw); } catch { return json({ ok: false, error: "bad json" }, 400); }

  // 1) One-time subscription verification POST: {"verification_token": "secret_..."}
  if (body && body.verification_token && !body.type) {
    // Capture for the operator: read it once from Worker logs, then store it as
    // the VERIFICATION_TOKEN secret. Never forwarded anywhere else.
    console.log("NOTION_VERIFICATION_TOKEN (store as secret VERIFICATION_TOKEN):", body.verification_token);
    return json({ ok: true, verified: true });
  }

  // 2) HMAC-SHA256 over the raw body, signed with the verification_token.
  const token = env.VERIFICATION_TOKEN || "";
  const sig = request.headers.get("x-notion-signature") || "";
  if (token) {
    const expected = "sha256=" + await hmacHex(token, raw);
    if (sig !== expected) return json({ ok: false, error: "bad signature" }, 401);
  } else {
    console.warn("VERIFICATION_TOKEN not set — accepting without signature check (setup mode)");
  }

  // 3) Forward to GitHub Actions. Payload carries only ids/timestamps — the
  //    handler fetches fresh state from Notion (webhook payload has no values).
  const dispatched = await ghApi(env, "POST", `/repos/${env.GH_OWNER || "romanzhura-crypto"}/${env.GH_REPO || "QA-Notion-Statistics"}/dispatches`, {
    event_type: "notion-webhook",
    client_payload: {
      type: body.type || null,
      timestamp: body.timestamp || null,
      entity_id: (body.entity && body.entity.id) || null,
      entity_type: (body.entity && body.entity.type) || null,
      attempt_number: body.attempt_number || 1,
      authors: (body.authors || []).map(a => a && a.id).filter(Boolean),
    },
  });
  if (dispatched.status !== 204) {
    console.error("repository_dispatch failed", dispatched.status, JSON.stringify(dispatched.data));
    return json({ ok: false, error: "dispatch failed" }, 502);
  }
  return json({ ok: true, forwarded: true }, 202);
}

async function handleSync(env) {
  // Button "Обновить данные" / external cron → workflow_dispatch with in-flight
  // reuse (same contract the QA proxy served: {ok, reused, run_id}).
  const owner = env.GH_OWNER || "romanzhura-crypto";
  const repo = env.GH_REPO || "QA-Notion-Statistics";
  const wf = encodeURIComponent(env.SYNC_WORKFLOW || "release-widgets.yml");
  const list = await ghApi(env, "GET", `/repos/${owner}/${repo}/actions/workflows/${wf}/runs?status=in_progress&per_page=5`);
  const runs = (list.data && list.data.workflow_runs) || [];
  const active = runs.find(r => r.event === "workflow_dispatch" || r.event === "schedule");
  if (active) return json({ ok: true, reused: true, run_id: active.id, status: active.status });
  const disp = await ghApi(env, "POST", `/repos/${owner}/${repo}/actions/workflows/${wf}/dispatches`, {
    ref: "main", inputs: {},
  });
  if (disp.status !== 204) return json({ ok: false, error: "dispatch failed" }, 502);
  return json({ ok: true, reused: false, status: "queued" }, 202);
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(request.url);
    if (request.method !== "POST") return json({ ok: false, error: "POST only" }, 405);
    const raw = await request.text();
    if (url.pathname === "/webhook/notion") return handleNotion(env, raw, request);
    if (url.pathname === "/sync-notion") return handleSync(env);
    return json({ ok: false, error: "unknown route" }, 404);
  },
};
