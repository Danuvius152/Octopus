#!/bin/bash
set -euo pipefail

# Preview by default. --apply binds the existing paths and writes the unit
# only after the installation has naturally drained and its service is stopped.
# Cloudflared and other independently supervised services retain their owners.
octopus_project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$octopus_project"
exec "$octopus_project/.venv/bin/python" -m server.service_installation "$@"
