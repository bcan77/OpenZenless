#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-1.0.0}"
ARCH="x86_64"
APPDIR="$ROOT/dist/OpenZenless.AppDir"
OUT_APPIMAGE="$ROOT/dist/OpenZenless-${VERSION}-${ARCH}.AppImage"
APPIMAGETOOL="${APPIMAGETOOL:-/tmp/appimagetool}"

if [ ! -x "$APPIMAGETOOL" ]; then
  echo "Downloading appimagetool..."
  curl -fsSL -L https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage -o "$APPIMAGETOOL"
  chmod +x "$APPIMAGETOOL"
fi

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/share/openzenless" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"

cp "$ROOT/openzenless.py" "$ROOT/README.md" "$ROOT/requirements.txt" "$ROOT/texture_sections_index.json" "$APPDIR/usr/share/openzenless/"
cp -r "$ROOT/openzenless" "$ROOT/assets" "$ROOT/docs" "$APPDIR/usr/share/openzenless/"

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
desktop-file-validate "$APPDIR/OpenZenless.desktop"

cat > "$APPDIR/AppRun" <<'RUN'
#!/usr/bin/env bash
set -euo pipefail
APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_MAIN="$APPDIR/usr/share/openzenless/openzenless.py"
if [ "$#" -eq 0 ]; then
  exec python3 "$APP_MAIN" gui
else
  exec python3 "$APP_MAIN" "$@"
fi
RUN
chmod +x "$APPDIR/AppRun"

ARCH="$ARCH" "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUT_APPIMAGE"
chmod +x "$OUT_APPIMAGE"
echo "Built AppImage: $OUT_APPIMAGE"
