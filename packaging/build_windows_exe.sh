#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-0.3.0-N25.11-01}"
OUT_DIR="$ROOT_DIR/dist"
BUILD_DIR="$ROOT_DIR/build/win64"

CXX="${MINGW_CXX:-x86_64-w64-mingw32-g++}"
WINDRES="${MINGW_WINDRES:-x86_64-w64-mingw32-windres}"

for req in "$CXX" "$WINDRES" python3; do
    if ! command -v "$req" >/dev/null 2>&1; then
        echo "Missing required build tool: $req" >&2
        exit 1
    fi
done

echo "[build_windows_exe] Building OpenZenless-${VERSION}-x86_64.exe..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/payload" "$OUT_DIR"

# Generate Windows .ico from PNG logo + version resource
python3 - "$ROOT_DIR/assets/OpenZenless_WhiteText.png" "$BUILD_DIR/openzenless.ico" "$BUILD_DIR/openzenless.rc" "$VERSION" <<'PY'
import re, struct, sys
from pathlib import Path

png_path = Path(sys.argv[1])
ico_path = Path(sys.argv[2])
rc_path = Path(sys.argv[3])
version = sys.argv[4]

try:
    from PIL import Image
    import io
    im = Image.open(png_path).convert("RGBA")
    sizes = [256, 128, 64, 48, 32, 16]
    frames = []
    for sz in sizes:
        buf = io.BytesIO()
        im.resize((sz, sz), Image.Resampling.LANCZOS).save(buf, format="PNG")
        frames.append((sz, buf.getvalue()))
    hdr = struct.pack("<HHH", 0, 1, len(frames))
    entries = []
    offset = 6 + 16 * len(frames)
    for sz, data in frames:
        w = 0 if sz >= 256 else sz
        entries.append(struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    ico_path.write_bytes(hdr + b"".join(entries) + b"".join(d for _, d in frames))
except Exception:
    # Fallback: wrap raw PNG bytes in a single-entry 256x256 Vista+ PNG-in-ICO container
    data = png_path.read_bytes()
    hdr = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(data), 22)
    ico_path.write_bytes(hdr + entry + data)

nums = [int(x) for x in re.findall(r"\d+", version)[:3]]
while len(nums) < 3:
    nums.append(0)
ver_csv = f"{nums[0]},{nums[1]},{nums[2]},0"

rc_path.write_text(
    f'1 ICON "openzenless.ico"\n'
    f'1 VERSIONINFO\n'
    f'FILEVERSION {ver_csv}\n'
    f'PRODUCTVERSION {ver_csv}\n'
    f'BEGIN\n'
    f'  BLOCK "StringFileInfo"\n'
    f'  BEGIN\n'
    f'    BLOCK "040904B0"\n'
    f'    BEGIN\n'
    f'      VALUE "CompanyName", "OpenZenless"\n'
    f'      VALUE "FileDescription", "OpenZenless - ZZZ Asset Explorer & Studio"\n'
    f'      VALUE "FileVersion", "{version}"\n'
    f'      VALUE "InternalName", "OpenZenless"\n'
    f'      VALUE "OriginalFilename", "OpenZenless.exe"\n'
    f'      VALUE "ProductName", "OpenZenless"\n'
    f'      VALUE "ProductVersion", "{version}"\n'
    f'    END\n'
    f'  END\n'
    f'  BLOCK "VarFileInfo"\n'
    f'  BEGIN\n'
    f'    VALUE "Translation", 0x0409, 1200\n'
    f'  END\n'
    f'END\n',
    encoding="utf-8",
)
PY

(
    cd "$BUILD_DIR"
    "$WINDRES" openzenless.rc -O coff -o openzenless.res
)

"$CXX" -std=c++17 -O2 -municode -mwindows \
    "$ROOT_DIR/packaging/win_launcher.cpp" \
    "$BUILD_DIR/openzenless.res" \
    -o "$BUILD_DIR/stub.exe" \
    -static -static-libgcc -static-libstdc++ -lshell32 -luser32

# Stage clean application payload
PDIR="$BUILD_DIR/payload"
cp "$ROOT_DIR/openzenless.py" "$PDIR/"
cp "$ROOT_DIR/run.bat" "$PDIR/"
cp "$ROOT_DIR/requirements.txt" "$PDIR/"
cp "$ROOT_DIR/texture_sections_index.json" "$PDIR/"
cp "$ROOT_DIR/README.md" "$PDIR/"
[ -f "$ROOT_DIR/LICENSE" ] && cp "$ROOT_DIR/LICENSE" "$PDIR/"
cp -a "$ROOT_DIR/openzenless" "$PDIR/"
cp -a "$ROOT_DIR/assets" "$PDIR/"
cp -a "$ROOT_DIR/docs" "$PDIR/"
mkdir -p "$PDIR/packaging"
cp "$ROOT_DIR/packaging/bootstrap_env.sh" "$PDIR/packaging/"
find "$PDIR" -name "__pycache__" -prune -exec rm -rf {} +
find "$PDIR" -name "*.pyc" -delete

python3 - "$PDIR" "$BUILD_DIR/payload.zip" <<'PY'
import sys, zipfile
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
    for p in sorted(src.rglob("*")):
        if p.is_file():
            zf.write(p, p.relative_to(src).as_posix())
PY

OUT_EXE="$OUT_DIR/OpenZenless-${VERSION}-x86_64.exe"
python3 - "$BUILD_DIR/stub.exe" "$BUILD_DIR/payload.zip" "$OUT_EXE" "$VERSION" <<'PY'
import struct, sys
from pathlib import Path

stub = Path(sys.argv[1]).read_bytes()
payload = Path(sys.argv[2]).read_bytes()
out = Path(sys.argv[3])
ver_bytes = sys.argv[4].encode("utf-8", errors="ignore")[:32]
ver_padded = ver_bytes + b"\x00" * (32 - len(ver_bytes))
footer = struct.pack("<QQ", len(stub), len(payload)) + ver_padded + b"OZWINZIP"
out.write_bytes(stub + payload + footer)
print(f"Built {out} ({out.stat().st_size / (1024 * 1024):.2f} MiB)")
PY
