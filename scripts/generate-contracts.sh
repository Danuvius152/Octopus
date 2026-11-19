#!/bin/bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
. "$root/scripts/toolchain-env.sh"
# Checks generate from a temporary source copy using the checkout's installed tools.
toolchain="${OCTOPUS_CONTRACTS_TOOLCHAIN:-$root}"
output="${OCTOPUS_CONTRACTS_OUTPUT:-$root/web/src/api/contracts.ts}"

if (( $# != 0 )); then
  echo 'Set OpenAPI generator options in scripts/generate-contracts.sh.' >&2
  exit 2
fi

tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT

# Run in an empty directory so the FastAPI schema is the generator's only input.
(
  cd "$tmp"
  "$toolchain/.venv/bin/python" "$root/scripts/dump-openapi.py" |
    "$toolchain/web/node_modules/.bin/openapi-typescript" > contracts.ts
)
mv -- "$tmp/contracts.ts" "$output"
