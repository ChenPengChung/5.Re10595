#!/bin/bash
# Root compatibility entrypoint for the project pipeline.
# Keep all implementation in ./run and chain_code/run.sh.

set -euo pipefail

_SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}" 2>/dev/null || realpath "${BASH_SOURCE[0]:-$0}" 2>/dev/null || echo "${BASH_SOURCE[0]:-$0}")"
PROJECT_ROOT="$(cd "$(dirname "$_SELF")" && pwd)"

exec "$PROJECT_ROOT/run" "$@"
