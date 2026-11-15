#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$#" -eq 0 ]; then
  exec python3 "$HERE/openzenless.py" gui
else
  exec python3 "$HERE/openzenless.py" "$@"
fi
