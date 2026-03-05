#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "ShareSentinel pre-public check"
echo "=============================="

failures=0

section() {
  echo
  echo "[$1]"
}

warn() {
  echo "WARN: $1"
}

fail() {
  echo "FAIL: $1"
  failures=$((failures + 1))
}

pass() {
  echo "OK: $1"
}

section "Compose config"
if command -v docker >/dev/null 2>&1; then
  if docker compose config >/dev/null 2>&1; then
    pass "docker compose config parses successfully"
  else
    fail "docker compose config failed"
  fi
else
  warn "docker not found; skipping compose validation"
fi

section "Tracked secret-like files"
tracked_secret_like="$(git ls-files | rg -n '(^|/)(\\.env|.*\\.(pem|key|pfx|p12))$' || true)"
if [[ -n "$tracked_secret_like" ]]; then
  fail "potential secret files are tracked in git:"
  echo "$tracked_secret_like"
else
  pass "no .env/.pem/.key/.pfx/.p12 files tracked"
fi

section "High-risk token patterns in tracked files"
secret_matches="$(
  git grep -nEI \
    '(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z\\-_]{35}|-----BEGIN (RSA|EC|DSA|OPENSSH|PRIVATE) KEY-----|ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{20,})' \
    || true
)"
if [[ -n "$secret_matches" ]]; then
  fail "high-risk secret patterns detected:"
  echo "$secret_matches"
else
  pass "no high-risk token patterns detected in tracked files"
fi

section "History author metadata (manual review)"
git log --all --format='%ae' | sort -u
warn "review author emails above before public release (history rewriting may be needed)"

echo
if [[ "$failures" -eq 0 ]]; then
  echo "Pre-public checks passed."
  exit 0
fi

echo "Pre-public checks failed: $failures issue(s)."
exit 1

