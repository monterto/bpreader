#!/bin/bash
# Builds BPReader-x86_64.AppImage from this source folder.
# Run from inside the bpapp/ folder: ./build_appimage.sh
#
# Needs: python3, git, wget, internet access for this one build step.
# The resulting AppImage runs fully offline.
set -euo pipefail

HERE="$(cd "$(dirname "${0}")" && pwd)"
BUILD="$HERE/appimage_build"
APPDIR="$BUILD/AppDir"
APP_ROOT="$APPDIR/usr/share/bpreader"

rm -rf "$BUILD"
mkdir -p "$APP_ROOT" "$APPDIR/usr/share/icons/hicolor/256x256/apps"

echo "==> Copying app source"
cp "$HERE/server.py" "$HERE/omron_reader.py" "$APP_ROOT/"
cp -r "$HERE/templates" "$APP_ROOT/"

echo "==> Fetching omblepy engine (bundled directly alongside server.py,"
echo "    matching omron_reader.py's resource lookup)"
git clone --depth 1 https://github.com/userx14/omblepy.git "$APP_ROOT/omblepy"
rm -rf "$APP_ROOT/omblepy/.git"

echo "==> Building a self-contained Python runtime (not a venv)"
echo "    venvs assume they stay next to the system Python that created"
echo "    them, which breaks the moment this AppImage runs on a different"
echo "    machine. Instead we bundle the real interpreter binary, its full"
echo "    stdlib, and dependencies as a flat site-packages dir, wired"
echo "    together with PYTHONHOME/PYTHONPATH in AppRun -- no venv"
echo "    machinery, no assumptions about the host's Python layout."
PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
REAL_PYTHON="$(readlink -f "$(command -v python3)")"
RUNTIME="$APP_ROOT/pyruntime"
mkdir -p "$RUNTIME/bin" "$RUNTIME/lib/python${PYVER}" "$RUNTIME/site-packages"

cp "$REAL_PYTHON" "$RUNTIME/bin/python3"
chmod +x "$RUNTIME/bin/python3"

SYSTEM_STDLIB="/usr/lib/python${PYVER}"
if [ ! -d "$SYSTEM_STDLIB" ]; then
    SYSTEM_STDLIB="/usr/lib64/python${PYVER}"
fi
for entry in "$SYSTEM_STDLIB"/*; do
    base="$(basename "$entry")"
    case "$base" in
        test|__pycache__|idlelib|tkinter|turtledemo) continue ;;
    esac
    cp -r "$entry" "$RUNTIME/lib/python${PYVER}/" 2>/dev/null || true
done
find "$RUNTIME/lib" -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Also grab the shared lib CPython needs to actually start (libpython3.x.so),
# if it's a dynamically-linked build -- copy it alongside so it doesn't
# depend on the host having a matching one on its linker path.
LIBPYTHON="$( (ldconfig -p 2>/dev/null | grep -m1 "libpython${PYVER}" | awk '{print $NF}') || true )"
if [ -n "$LIBPYTHON" ] && [ -f "$LIBPYTHON" ]; then
    mkdir -p "$RUNTIME/libdyn"
    cp "$LIBPYTHON" "$RUNTIME/libdyn/"
fi

echo "==> Installing dependencies into the flat site-packages dir"
python3 -m pip install --quiet --target="$RUNTIME/site-packages" flask bleak terminaltables

echo "==> Writing AppRun"
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/bash
set -e
HERE="$(cd "$(dirname "$(readlink -f "${0}")")" && pwd)"
export APPDIR="$HERE"
APP_SRC="$HERE/usr/share/bpreader"
RUNTIME="$APP_SRC/pyruntime"
cd "$APP_SRC"
# Fully self-contained interpreter: stdlib, dynamic lib, and dependencies
# all resolved from inside this AppImage, nothing borrowed from the host.
export PYTHONHOME="$RUNTIME"
export PYTHONPATH="$RUNTIME/site-packages"
export PYTHONNOUSERSITE=1
export LD_LIBRARY_PATH="$RUNTIME/libdyn:${LD_LIBRARY_PATH:-}"
exec "$RUNTIME/bin/python3" "$APP_SRC/server.py" "$@"
EOF
chmod +x "$APPDIR/AppRun"

echo "==> Writing desktop file + icon"
cat > "$APPDIR/bpreader.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=BP Reader
Comment=Local reader for Omron BP7455CAN blood pressure monitor
Exec=AppRun
Icon=bpreader
Categories=Utility;
Terminal=false
EOF

python3 - <<'PYEOF'
from PIL import Image, ImageDraw
size = 256
img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
# Neutral grey background + slate blue accent, matching the app's theme.
d.rounded_rectangle([8, 8, size-8, size-8], radius=48, fill=(26,26,28,255), outline=(124,147,201,255), width=6)
# Two trend lines (systolic/diastolic), echoing the app's own sparkline chart.
pts_sys = [(36,160),(76,160),(96,96),(116,196),(136,64),(156,160),(220,160)]
pts_dia = [(36,180),(76,178),(96,150),(116,190),(136,120),(156,175),(220,172)]
d.line(pts_dia, fill=(90,127,174,255), width=7, joint='curve')
d.line(pts_sys, fill=(124,147,201,255), width=9, joint='curve')
for p in ["appimage_build/AppDir/usr/share/icons/hicolor/256x256/apps/bpreader.png",
          "appimage_build/AppDir/bpreader.png"]:
    img.save(p)
PYEOF

echo "==> Downloading appimagetool"
if [ ! -f "$HERE/appimagetool.AppImage" ]; then
    wget -q "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" \
        -O "$HERE/appimagetool.AppImage"
    chmod +x "$HERE/appimagetool.AppImage"
fi

echo "==> Packaging AppImage"
cd "$HERE"
ARCH=x86_64 ./appimagetool.AppImage --appimage-extract-and-run "$APPDIR" BPReader-x86_64.AppImage

echo
echo "Done: $HERE/BPReader-x86_64.AppImage"
echo "Next: ./install_appimage.sh $HERE/BPReader-x86_64.AppImage"
