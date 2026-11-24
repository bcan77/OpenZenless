#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-0.3.0}"
ARCH="${ARCH:-$(uname -m)}"
APPDIR="$ROOT/dist/OpenZenless.AppDir"
OUT_APPIMAGE="$ROOT/dist/OpenZenless-${VERSION}-${ARCH}.AppImage"
APPIMAGETOOL="${APPIMAGETOOL:-/tmp/appimagetool-${ARCH}}"

if [ ! -x "$APPIMAGETOOL" ]; then
  echo "Downloading appimagetool ($ARCH)..."
  curl -fsSL -L "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage" -o "$APPIMAGETOOL"
  chmod +x "$APPIMAGETOOL"
fi

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/share/openzenless" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"

cp "$ROOT/openzenless.py" "$ROOT/README.md" "$ROOT/requirements.txt" "$ROOT/texture_sections_index.json" "$APPDIR/usr/share/openzenless/"
cp "$ROOT/packaging/bootstrap_env.sh" "$APPDIR/usr/share/openzenless/bootstrap_env.sh"
chmod +x "$APPDIR/usr/share/openzenless/bootstrap_env.sh"
cp -r "$ROOT/openzenless" "$ROOT/assets" "$ROOT/docs" "$APPDIR/usr/share/openzenless/"
find "$APPDIR/usr/share/openzenless" -name "__pycache__" -type d -exec rm -rf {} +
find "$APPDIR/usr/share/openzenless" -name "*.py[cod]" -delete

# Generate a square 256x256 PNG icon from OpenZenless_WhiteText.png
python3 - "$ROOT/assets/OpenZenless_WhiteText.png" "$APPDIR/openzenless.png" <<'PY'
import sys
from PIL import Image
src = Image.open(sys.argv[1]).convert("RGBA")
bbox = src.getbbox()
if bbox:
    src = src.crop(bbox)
canvas = Image.new("RGBA", (256, 256), (15, 17, 23, 255))
src.thumbnail((224, 224), Image.Resampling.LANCZOS)
x = (256 - src.width) // 2
y = (256 - src.height) // 2
canvas.paste(src, (x, y), src)
canvas.save(sys.argv[2])
PY

cp "$APPDIR/openzenless.png" "$APPDIR/.DirIcon"
cp "$APPDIR/openzenless.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/openzenless.png"

cat > "$APPDIR/OpenZenless.desktop" <<'DESK'
[Desktop Entry]
Type=Application
Name=OpenZenless
Comment=OpenIV-style proprietary file viewer, audio organizer, and exporter for Zenless Zone Zero
Exec=openzenless
Icon=openzenless
Categories=Utility;Development;
Terminal=false
DESK
cp "$APPDIR/OpenZenless.desktop" "$APPDIR/usr/share/applications/OpenZenless.desktop"
if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$APPDIR/OpenZenless.desktop"
fi

cat > "$APPDIR/AppRun" <<'RUN'
#!/usr/bin/env bash
set -euo pipefail
APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_MAIN="$APPDIR/usr/share/openzenless/openzenless.py"
REQ_FILE="$APPDIR/usr/share/openzenless/requirements.txt"
BOOT="$APPDIR/usr/share/openzenless/bootstrap_env.sh"
if [ "$#" -eq 0 ]; then
  exec bash "$BOOT" "$APP_MAIN" "$REQ_FILE" gui
else
  exec bash "$BOOT" "$APP_MAIN" "$REQ_FILE" "$@"
fi
RUN
chmod +x "$APPDIR/AppRun"

ARCH="$ARCH" "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUT_APPIMAGE"
chmod +x "$OUT_APPIMAGE"
echo "Built AppImage: $OUT_APPIMAGE"
