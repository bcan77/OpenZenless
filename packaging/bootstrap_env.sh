#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "Usage: bootstrap_env.sh <openzenless.py> <requirements.txt> [args...]" >&2
  exit 2
fi

APP_MAIN="$1"
REQ_FILE="$2"
shift 2

if [ "$#" -eq 0 ]; then
  set -- gui
fi

DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/openzenless"
VENV_DIR="$DATA_DIR/venv"
TOOLS_DIR="$DATA_DIR/tools"
UV_BIN="$TOOLS_DIR/uv/uv"
LOG_FILE="$DATA_DIR/bootstrap.log"

# Expose any portable tools installed by OpenZenless
for tool_bin in \
  "$TOOLS_DIR/platform-tools" \
  "$TOOLS_DIR/dotnet" \
  "$TOOLS_DIR/vgmstream" \
  "$TOOLS_DIR/wine/bin" \
  "$TOOLS_DIR/blender"
do
  if [ -d "$tool_bin" ]; then
    export PATH="$tool_bin:$PATH"
  fi
done

# Cross-device Wayland (XWayland) & discrete GPU PRIME offload configuration
if [ -n "${WAYLAND_DISPLAY:-}" ] && [ -n "${DISPLAY:-}" ] && [ -z "${QT_QPA_PLATFORM:-}" ]; then
  case "${OPENZENLESS_WAYLAND:-}" in
    1|true|TRUE|yes|YES) ;;
    *) export QT_QPA_PLATFORM="xcb" ;;
  esac
fi

if [ "${QT_QPA_PLATFORM:-}" = "xcb" ] && [ -z "${PYOPENGL_PLATFORM:-}" ]; then
  export PYOPENGL_PLATFORM="glx"
fi

case "${OPENZENLESS_GPU:-}" in
  igpu|cpu|0|off) ;;
  *)
    if [ -e /proc/driver/nvidia/version ]; then
      export __NV_PRIME_RENDER_OFFLOAD="${__NV_PRIME_RENDER_OFFLOAD:-1}"
      export __GLX_VENDOR_LIBRARY_NAME="${__GLX_VENDOR_LIBRARY_NAME:-nvidia}"
      export __VK_LAYER_NV_optimus="${__VK_LAYER_NV_optimus:-NVIDIA_only}"
    else
      # Enable DRI_PRIME=1 on hybrid AMD/Intel multi-GPU laptops if a non-boot discrete card exists
      _boot_gpu=0
      _discrete_gpu=0
      for _dev in /sys/class/drm/card[0-9]*/device; do
        [ -f "$_dev/vendor" ] || continue
        _v="$(tr '[:upper:]' '[:lower:]' < "$_dev/vendor" 2>/dev/null || true)"
        _b="$(cat "$_dev/boot_vga" 2>/dev/null || true)"
        [ "$_b" = "1" ] && _boot_gpu=1
        if [ "$_b" = "0" ] && { [ "$_v" = "0x1002" ] || [ "$_v" = "0x8086" ]; }; then
          _discrete_gpu=1
        fi
      done
      if [ "$_boot_gpu" = "1" ] && [ "$_discrete_gpu" = "1" ]; then
        export DRI_PRIME="${DRI_PRIME:-1}"
      fi
    fi
    ;;
esac

_py_ok() {
  local py="$1"
  [ -x "$py" ] || return 1
  "$py" - <<'PY' >/dev/null 2>&1
import sys
if sys.version_info < (3, 10):
    raise SystemExit(1)
for mod in ("PySide6", "PIL", "numpy", "OpenGL", "pygltflib"):
    __import__(mod)
try:
    import compression.zstd
except ImportError:
    import zstandard
PY
}

_req_hash() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$REQ_FILE" 2>/dev/null | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$REQ_FILE" 2>/dev/null | awk '{print $1}'
  else
    cksum "$REQ_FILE" 2>/dev/null | awk '{print $1 ":" $2}'
  fi
}

_sys_py310() {
  local py
  py="$(command -v python3 2>/dev/null || true)"
  [ -n "$py" ] || return 1
  "$py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1 || return 1
  echo "$py"
}

REQ_HASH="$(_req_hash)"

# 1. Fast path: managed OpenZenless venv matches current requirements
if [ -x "$VENV_DIR/bin/python" ] && [ -f "$VENV_DIR/.req.sha256" ] && [ "$(cat "$VENV_DIR/.req.sha256" 2>/dev/null || true)" = "$REQ_HASH" ]; then
  if _py_ok "$VENV_DIR/bin/python"; then
    exec "$VENV_DIR/bin/python" "$APP_MAIN" "$@"
  fi
fi

# 2. Active virtualenv or system Python already satisfies all dependencies
if [ -n "${VIRTUAL_ENV:-}" ] && _py_ok "${VIRTUAL_ENV}/bin/python"; then
  exec "${VIRTUAL_ENV}/bin/python" "$APP_MAIN" "$@"
fi
if [ "${OPENZENLESS_FORCE_VENV:-0}" != "1" ]; then
  if SYS_PY="$(command -v python3 2>/dev/null)" && _py_ok "$SYS_PY"; then
    exec "$SYS_PY" "$APP_MAIN" "$@"
  fi
fi

if [ "${OPENZENLESS_NO_AUTO_INSTALL:-0}" = "1" ]; then
  echo "[OpenZenless] Missing Python dependencies and OPENZENLESS_NO_AUTO_INSTALL=1." >&2
  echo "Install with: pip install -r \"$REQ_FILE\"" >&2
  exit 1
fi

mkdir -p "$DATA_DIR" "$TOOLS_DIR"
: > "$LOG_FILE"
_log() {
  printf '[OpenZenless Bootstrap] %s\n' "$*" >&2
  printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >> "$LOG_FILE"
}

PROG_PID=""
_cleanup() {
  if [ -n "$PROG_PID" ]; then
    kill "$PROG_PID" >/dev/null 2>&1 || true
    PROG_PID=""
  fi
}
trap _cleanup EXIT

if [ ! -t 2 ] && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }; then
  if command -v zenity >/dev/null 2>&1; then
    (while kill -0 $$ 2>/dev/null; do sleep 1; done) | \
      zenity --progress --pulsate --no-cancel --auto-close \
        --title="OpenZenless — First Run Setup" \
        --text="Installing Python runtime & GUI dependencies into $VENV_DIR..." >/dev/null 2>&1 &
    PROG_PID=$!
  elif command -v kdialog >/dev/null 2>&1; then
    kdialog --passivepopup "OpenZenless is installing Python dependencies into $VENV_DIR (first run)..." 20 >/dev/null 2>&1 &
    PROG_PID=$!
  fi
fi

_log "Setting up Python dependencies into $VENV_DIR (log: $LOG_FILE)..."

_install_with_uv() {
  local uv_exe="$1"
  local py_sys
  py_sys="$(_sys_py310 || true)"
  if [ -n "$py_sys" ]; then
    "$uv_exe" venv --clear --python "$py_sys" "$VENV_DIR" >> "$LOG_FILE" 2>&1 || \
      "$uv_exe" venv --clear --python 3.12 "$VENV_DIR" >> "$LOG_FILE" 2>&1
  else
    "$uv_exe" venv --clear --python 3.12 "$VENV_DIR" >> "$LOG_FILE" 2>&1
  fi
  "$uv_exe" pip install --python "$VENV_DIR/bin/python" -r "$REQ_FILE" >> "$LOG_FILE" 2>&1
}

_download_uv() {
  local arch target_triple url tmp_dir tmp_tar found_uv
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) target_triple="uv-x86_64-unknown-linux-gnu.tar.gz" ;;
    aarch64|arm64) target_triple="uv-aarch64-unknown-linux-gnu.tar.gz" ;;
    *)
      _log "Unsupported architecture '$arch' for automatic uv download."
      return 1
      ;;
  esac
  url="https://github.com/astral-sh/uv/releases/latest/download/$target_triple"
  tmp_dir="$(mktemp -d)"
  tmp_tar="$tmp_dir/uv.tar.gz"
  _log "Downloading portable uv ($arch) for isolated Python + dependency setup..."
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL -L "$url" -o "$tmp_tar" >> "$LOG_FILE" 2>&1
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tmp_tar" "$url" >> "$LOG_FILE" 2>&1
  else
    rm -rf "$tmp_dir"
    _log "Neither curl nor wget is installed to download uv."
    return 1
  fi
  tar -xzf "$tmp_tar" -C "$tmp_dir" >> "$LOG_FILE" 2>&1
  found_uv="$(find "$tmp_dir" -type f -name uv | head -n 1 || true)"
  if [ -z "$found_uv" ]; then
    rm -rf "$tmp_dir"
    return 1
  fi
  mkdir -p "$(dirname "$UV_BIN")"
  cp "$found_uv" "$UV_BIN"
  chmod +x "$UV_BIN"
  rm -rf "$tmp_dir"
  return 0
}

INSTALLED=0

# Strategy 1: existing uv
UV_EXISTING="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV_EXISTING" ] && [ -x "$UV_BIN" ]; then
  UV_EXISTING="$UV_BIN"
fi
if [ -n "$UV_EXISTING" ]; then
  _log "Installing packages with uv ($UV_EXISTING)..."
  if _install_with_uv "$UV_EXISTING"; then
    INSTALLED=1
  else
    _log "uv install failed; trying fallback..."
  fi
fi

# Strategy 2: system python3 >= 3.10 + venv + pip
if [ "$INSTALLED" -eq 0 ]; then
  if SYS_PY="$(_sys_py310)"; then
    _log "Creating virtualenv using system Python ($SYS_PY)..."
    rm -rf "$VENV_DIR"
    if "$SYS_PY" -m venv "$VENV_DIR" >> "$LOG_FILE" 2>&1 && "$VENV_DIR/bin/python" -m pip --version >> "$LOG_FILE" 2>&1; then
      _log "Installing packages via pip (first run may take 30-90s)..."
      if "$VENV_DIR/bin/python" -m pip install --upgrade pip >> "$LOG_FILE" 2>&1 && \
         "$VENV_DIR/bin/python" -m pip install -r "$REQ_FILE" >> "$LOG_FILE" 2>&1; then
        INSTALLED=1
      fi
    fi
  fi
fi

# Strategy 3: download portable uv and bootstrap isolated Python 3.12 + deps
if [ "$INSTALLED" -eq 0 ]; then
  if _download_uv && [ -x "$UV_BIN" ]; then
    if _install_with_uv "$UV_BIN"; then
      INSTALLED=1
    fi
  fi
fi

if [ "$INSTALLED" -eq 0 ] || ! _py_ok "$VENV_DIR/bin/python"; then
  _log "Automatic setup failed! See $LOG_FILE for details."
  cat "$LOG_FILE" >&2 || true
  if [ ! -t 2 ] && command -v zenity >/dev/null 2>&1; then
    zenity --error --title="OpenZenless Setup Failed" \
      --text="Could not install Python dependencies.\nSee log: $LOG_FILE" >/dev/null 2>&1 || true
  fi
  exit 1
fi

printf '%s\n' "$REQ_HASH" > "$VENV_DIR/.req.sha256"
_log "Python environment ready!"
_cleanup
exec "$VENV_DIR/bin/python" "$APP_MAIN" "$@"
