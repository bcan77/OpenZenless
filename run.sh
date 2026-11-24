#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$HERE/packaging/bootstrap_env.sh" "$HERE/openzenless.py" "$HERE/requirements.txt" "$@"
