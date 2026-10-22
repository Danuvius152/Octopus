#!/bin/bash
# Run the full commit/integration checks documented in AGENTS.md.
#
# Usage:
#   ./scripts/check.sh          # includes the full hermetic backend suite
#   ./scripts/check.sh --e2e    # also run Playwright (needs signed-in CLIs)
#
# Runs every gate even after one fails, so you get the full picture in one
# pass instead of discovering problems one commit at a time. Exit status is
# non-zero if any gate failed.
#
# `ruff format` is deliberately NOT a gate. It would rewrite 124 of 177
# files; on a codebase with hand-wrapped prose comments that is an
# unreviewable diff that destroys git blame for no behavioural gain. Ruff is
# used as a linter only. Adopting the formatter is a separate decision, best
# taken after the SessionManager extraction rather than before it.

set -uo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

# The JS toolchain lives under nvm, not on the default PATH (see AGENTS.md
# Conventions). Resolve it here so callers don't have to.
if [ -d "$HOME/.nvm/versions/node" ]; then
  NODE_BIN="$HOME/.nvm/versions/node/$(ls "$HOME/.nvm/versions/node" | sort -V | tail -1)/bin"
  export PATH="$NODE_BIN:$PATH"
fi

# Python block-buffers stdout when it is a pipe rather than a tty, so a long
# pytest run can emit nothing for its whole duration and then flush at the
# end. That looks indistinguishable from a hung process to anything watching
# stdio — including Octopus's own bg-task idle watchdog, which SIGTERMs a
# task silent for 60s (server/bg_tasks.py IDLE_AFTER_OUTPUT_TIMEOUT_SECS).
# Unbuffered output keeps progress visible and the run alive.
export PYTHONUNBUFFERED=1

FAILED=()
PASSED=()

run_gate() {
  local name="$1"; shift
  printf '\n\033[1m── %s\033[0m\n' "$name"
  if "$@"; then
    PASSED+=("$name")
  else
    FAILED+=("$name")
  fi
}

gate_pytest() {
  # Full correctness excludes live providers, answer evaluation and Wiki benchmarks.
  "$ROOT/.venv/bin/pytest" tests/ -m "not real and not knowledge_eval and not wiki_perf" -q
}

gate_vitest() { cd "$ROOT/web" && bun run test; }

gate_tsc() { cd "$ROOT/web" && bun run typecheck; }

gate_contracts() { "$ROOT/scripts/check-contracts.sh"; }

# docs/README.md's plans table is generated from each plan's own Status line
# (polish-2026-09.md §6 D2), so a new plan cannot be added without appearing
# in the index.
gate_docs_index() { "$ROOT/.venv/bin/python" "$ROOT/scripts/gen-docs-index.py" --check; }

gate_ruff() { "$ROOT/.venv/bin/ruff" check .; }

gate_mypy() { "$ROOT/.venv/bin/mypy"; }

gate_eslint() { cd "$ROOT/web" && bun run lint; }

gate_e2e() { cd "$ROOT/web" && bun run test:e2e; }

run_gate "backend lint (ruff)"                 gate_ruff
run_gate "backend types (mypy)"                gate_mypy
run_gate "backend unit (hermetic release selection)" gate_pytest
run_gate "frontend lint (eslint)"              gate_eslint
run_gate "frontend unit (vitest)"              gate_vitest
run_gate "typecheck (tsc -b)"                  gate_tsc
run_gate "generated contracts in sync"         gate_contracts
run_gate "docs plans index in sync"            gate_docs_index

if [ "${1:-}" = "--e2e" ]; then
  run_gate "e2e (playwright)" gate_e2e
else
  printf '\n\033[2mskipped: e2e — run ./scripts/check.sh --e2e (needs signed-in CLIs)\033[0m\n'
fi

printf '\n\033[1m── summary\033[0m\n'
for g in "${PASSED[@]}"; do printf '  \033[32mpass\033[0m  %s\n' "$g"; done
for g in "${FAILED[@]}"; do printf '  \033[31mFAIL\033[0m  %s\n' "$g"; done

if [ ${#FAILED[@]} -gt 0 ]; then
  printf '\n%d of %d gates failed.\n' "${#FAILED[@]}" "$((${#PASSED[@]} + ${#FAILED[@]}))"
  exit 1
fi
printf '\nAll %d gates passed.\n' "${#PASSED[@]}"
