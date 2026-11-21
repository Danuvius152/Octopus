#!/bin/bash
# Run the commit or scoped verification checks documented in docs/testing.md.
#
# Usage:
#   ./scripts/check.sh          # includes the full hermetic backend suite
#   ./scripts/check.sh --e2e    # also run Playwright (needs signed-in CLIs)
#   ./scripts/check.sh --release --output <new-directory>  # Codex release matrix
#   ./scripts/check.sh --gate ruff --gate docs-index
#   ./scripts/check.sh --pytest-target tests/test_check_runner.py
#   ./scripts/check.sh --output <new-directory>  # logs and machine-readable evidence
#
# Runs every selected check even after one fails. Exit status is nonzero if
# any selected check failed.
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
. "$ROOT/scripts/toolchain-env.sh"

# Python block-buffers stdout when it is a pipe rather than a tty, so a long
# pytest run can emit nothing for its whole duration and then flush at the
# end. That looks indistinguishable from a hung process to anything watching
# stdio — including Octopus's own bg-task idle watchdog, which SIGTERMs a
# task silent for 60s (server/bg_tasks.py IDLE_AFTER_OUTPUT_TIMEOUT_SECS).
# Unbuffered output keeps progress visible and the run alive.
export PYTHONUNBUFFERED=1

exec "$ROOT/.venv/bin/python" -m scripts.check_runner "$@"
