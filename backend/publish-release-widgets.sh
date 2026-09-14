#!/usr/bin/env bash
# Publish static Release widgets (HTML + JSON only). No Notion token. No /sync.
set -euo pipefail

ROOT="${ROOT:-${CI_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
SRC_WIDGETS="${ROOT}/frontend"
STAGE="${SRC_WIDGETS}/public"
: "${QA_WWW=/var/www/openclaw/widgets}"
DEST="${WIDGETS_PUBLISH_DIR:-}"

need=(
  release-charts.html
  status-dwell.html
  release-data.json
)

mkdir -p "$STAGE"
for f in "${need[@]}"; do
  if [[ ! -f "${SRC_WIDGETS}/${f}" ]]; then
    echo "missing ${SRC_WIDGETS}/${f}" >&2
    exit 1
  fi
  cp -f "${SRC_WIDGETS}/${f}" "${STAGE}/${f}"
done

# Local QA copy (self-signed OpenClaw vhost). Useful for file checks, not Notion TLS.
if [[ -n "${QA_WWW}" && -d "$QA_WWW" ]]; then
  for f in "${need[@]}"; do
    cp -f "${SRC_WIDGETS}/${f}" "${QA_WWW}/${f}"
  done
  echo "copied_qa_www ${QA_WWW}"
fi

if [[ -n "$DEST" ]]; then
  mkdir -p "$DEST"
  for f in "${need[@]}"; do
    cp -f "${STAGE}/${f}" "${DEST}/${f}"
  done
  echo "copied_dest ${DEST}"
fi

python3 - <<PY
import json, os, pathlib, re, sys
root = pathlib.Path(os.environ.get("ROOT") or "${ROOT}") / "frontend"
stage = root / "public"
bad = []
for p in list(stage.glob("*.html")) + list(stage.glob("*.json")):
    text = p.read_text(encoding="utf-8", errors="replace")
    if re.search(r"fetch\s*\(\s*['\"][^'\"]*widgets/sync|URL\(\s*['\"]/?widgets/sync", text):
        bad.append(f"{p.name}: runtime call to widgets/sync")
    if re.search(r"secret_[A-Za-z0-9]{10,}|ntn_[A-Za-z0-9]{10,}", text):
        bad.append(f"{p.name}: looks like a Notion token")
    if p.suffix == ".json":
        data = json.loads(text)
        if isinstance(data, dict) and "token" in data:
            bad.append(f"{p.name}: json key token")
if bad:
    print("publish_guard_fail", *bad, sep="\n", file=sys.stderr)
    sys.exit(2)
print("publish_guard_ok html+json static-only")
PY

echo "staged ${STAGE}"
echo "hint: GitLab Pages origin after pages job: public/release-charts.html + public/status-dwell.html + public/release-data.json"
echo "hint: Notion embed URLs = https://<pages-host>/release-charts.html and .../status-dwell.html"
