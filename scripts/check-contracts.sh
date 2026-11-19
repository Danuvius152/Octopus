#!/bin/bash
# Compare generated types in the checkout or in the exact staged source snapshot.
set -euo pipefail

cd "$(dirname "$0")/.."
root="$PWD"
. ./scripts/toolchain-env.sh

tmp="$(mktemp -d)"
tmp="$(cd "$tmp" && pwd -P)"
trap 'rm -rf -- "$tmp"' EXIT

source_root="$tmp/source"
tree=""
if [[ "${1:-}" == --staged ]]; then
  cp -- "$(git rev-parse --git-path index)" "$tmp/index"
  tree="$(GIT_INDEX_FILE="$tmp/index" git write-tree)"
fi
.venv/bin/python scripts/snapshot-contract-source.py "$source_root" "$tree"

contract="web/src/api/contracts.ts"
if [[ ! -f "$source_root/$contract" || -L "$source_root/$contract" ]]; then
  echo "$contract is missing or is not a regular file. Regenerate it and include it with the schema changes." >&2
  exit 1
fi
expected="$tmp/expected.ts"
cp -- "$source_root/$contract" "$expected"

generated="$tmp/contracts.ts"
(
  cd "$source_root/web"
  TMPDIR="$tmp" OCTOPUS_CONTRACTS_TOOLCHAIN="$root" OCTOPUS_CONTRACTS_OUTPUT="$generated" bun run generate:contracts
)
if [[ ! -f "$generated" ]]; then
  echo "generate:contracts did not produce the requested temporary output." >&2
  exit 1
fi

if cmp -s "$expected" "$generated"; then
  echo "contracts.ts is in sync with the OpenAPI schema"
  exit 0
fi

cat >&2 <<'MSG'
contracts.ts is STALE. Regenerate it with 'cd web && bun run generate:contracts'.
When committing, stage the generated types along with the schema inputs.
MSG
diff -u --label "$contract" --label "freshly generated $contract" "$expected" "$generated" | head -n 40 >&2 || true
exit 1
