#!/bin/bash
# Install the lefthook git hooks for this repo. Idempotent.
#
# Usage: ./scripts/setup-hooks.sh

set -euo pipefail

cd "$(dirname "$0")/.."
. ./scripts/toolchain-env.sh

if [ "${1:-}" = "--check" ]; then
  hook="$(git rev-parse --git-path hooks/pre-commit)"
  if [ ! -x "$hook" ] || ! command -v lefthook >/dev/null 2>&1 || ! lefthook check-install; then
    echo "Pre-commit hook is missing. Run ./scripts/setup-hooks.sh" >&2
    exit 1
  fi
  echo "Lefthook pre-commit hook is installed"
  exit 0
fi

if ! command -v lefthook >/dev/null 2>&1; then
  echo "lefthook not on PATH. Install with one of:"
  echo "  brew install lefthook"
  echo "  npm install -g lefthook"
  echo "  go install github.com/evilmartians/lefthook@latest"
  exit 1
fi

lefthook install
test -x "$(git rev-parse --git-path hooks/pre-commit)"

echo "Hooks installed. From now on, commits will run:"
echo "  - eslint + typecheck (tsc -b --force) + vitest   (when web TS changes)"
echo "  - ruff + mypy + full hermetic pytest + architecture advice   (when Python changes)"
echo "  - test support import check (when Python changes, including scripts/)"
echo "  - contracts.ts drift check   (when routes or pydantic models change)"
echo "  - docs index check         (when contracts, plans or their index change)"
echo
echo "During implementation, run focused checks; code commits require full checks (AGENTS.md)."
echo "For the full commit/integration checks, including the hermetic backend suite:"
echo "  ./scripts/check.sh            (add --e2e for Playwright)"
