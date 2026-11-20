#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -e /dev/nvidia0 ] && [[ "${OPENZENLESS_GPU:-}" != "igpu" && "${OPENZENLESS_GPU:-}" != "cpu" && "${OPENZENLESS_GPU:-}" != "0" && "${OPENZENLESS_GPU:-}" != "off" ]]; then
  export __NV_PRIME_RENDER_OFFLOAD="${__NV_PRIME_RENDER_OFFLOAD:-1}"
  export __GLX_VENDOR_LIBRARY_NAME="${__GLX_VENDOR_LIBRARY_NAME:-nvidia}"
  export __VK_LAYER_NV_optimus="${__VK_LAYER_NV_optimus:-NVIDIA_only}"
fi
if [ "$#" -eq 0 ]; then
  exec python3 "$HERE/openzenless.py" gui
else
  exec python3 "$HERE/openzenless.py" "$@"
fi
