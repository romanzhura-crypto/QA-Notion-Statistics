#!/usr/bin/env bash
# Local replay of the CI "static guards" grep-checks (board #191).
# Run BEFORE every push to romanzhura-crypto/QA-Notion-Statistics — these
# greps live only in CI; local selftests do not run them.
# Works in both layouts: git repo (backend/, frontend/) and QA workspace
# (scripts/, widgets/). Exit 0 = clean.
# Mirrors .github/workflows/release-widgets.yml validate job.
set -u
cd "$(dirname "$0")/.." || exit 2
fail=0

# layout: repo tree vs workspace tree
CODE_DIRS=""
for d in backend frontend scripts widgets tests; do
  [ -d "$d" ] && CODE_DIRS="$CODE_DIRS $d"
done
HTML_DIR=""
for d in frontend widgets; do
  [ -d "$d" ] && HTML_DIR="$d"
done

# 1) token shapes (secrets must never be committed)
if grep -RInE 'secret_[A-Za-z0-9]{10,}|ntn_[A-Za-z0-9]{10,}' $CODE_DIRS 2>/dev/null; then
  echo "FAIL: token-shaped string in code dirs" >&2
  fail=1
fi

# 2) deprecated Notion request-body field (board #161): use in_trash.
#    Matches dict-literals in test fixtures too — write dict(archived=...) kwargs.
PY_DIRS=""
for d in backend scripts tests; do
  [ -d "$d" ] && PY_DIRS="$PY_DIRS $d"
done
if grep -RInE "[\"']archived[\"'][[:space:]]*:" $PY_DIRS 2>/dev/null; then
  echo "FAIL: deprecated request-body key literal in code — use in_trash / dict() kwargs in fixtures" >&2
  fail=1
fi

# 3) committed secrets files must not exist in the committed tree.
#    Only meaningful inside a git checkout: in the QA workspace config/ holds
#    legitimate local secrets (gitignored, never pushed).
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  for f in config/notion.json config/gitlab.env config/github.env; do
    if [ -e "$f" ]; then
      echo "FAIL: $f must not be committed" >&2
      fail=1
    fi
  done
fi

# 4) widget HTML must not reference removed /widgets/sync nor leak token names
if [ -n "$HTML_DIR" ]; then
  python3 - "$HTML_DIR" <<'EOF' || fail=1
import sys
from pathlib import Path
d = Path(sys.argv[1])
t = "".join(p.read_text() for p in sorted(d.glob("*.html")))
assert "widgets/sync" not in t, "removed /widgets/sync referenced"
assert "GITHUB_TOKEN" not in t, "GITHUB_TOKEN leaked into HTML"
assert "ntn_" not in t, "token shape in HTML"
print("html guards ok")
EOF
fi

if [ "$fail" = 0 ]; then
  echo "ci-static-guards: CLEAN"
fi
exit $fail
