#!/bin/bash
# Builds BPReader-x86_64.AppImage from this source folder.
# Run it from anywhere: ./build_appimage.sh (or path/to/build_appimage.sh)
#
# Needs: python3 (3.9+) with pip, git, and wget or curl, plus internet
# access for this one build step. The resulting AppImage runs fully offline.
#
# The bundled Python is a copy of this machine's own, so build on the same
# distro (ideally the same machine) you'll run the AppImage on.
set -euo pipefail

HERE="$(cd "$(dirname "${0}")" && pwd)"
BUILD="$HERE/appimage_build"
APPDIR="$BUILD/AppDir"
APP_ROOT="$APPDIR/usr/share/bpreader"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

# Download to a temporary name first, so a failed download never leaves a
# half-written file behind that a later run would mistake for a good one.
download() {
    if command -v wget >/dev/null 2>&1; then
        wget -q "$1" -O "$2.part"
    else
        curl -fsSL "$1" -o "$2.part"
    fi
    mv "$2.part" "$2"
}

echo "==> Checking build tools"
command -v python3 >/dev/null 2>&1 || die "python3 not found."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
    || die "Python 3.9 or newer is needed."
python3 -m pip --version >/dev/null 2>&1 \
    || die "pip isn't available for python3. Install it (the package is usually called python3-pip), or build inside a toolbox/distrobox container."
command -v git >/dev/null 2>&1 || die "git not found."
command -v wget >/dev/null 2>&1 || command -v curl >/dev/null 2>&1 \
    || die "wget or curl is needed to download appimagetool."

rm -rf "$BUILD"
mkdir -p "$APP_ROOT" "$APPDIR/usr/share/icons/hicolor/256x256/apps"

echo "==> Copying app source"
cp "$HERE/server.py" "$HERE/omron_reader.py" "$APP_ROOT/"
cp -r "$HERE/templates" "$HERE/static" "$APP_ROOT/"

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

# Ask the running Python where its standard library and compiled modules
# (lib-dynload) actually load from, instead of guessing. On Fedora (and
# Bazzite) that's /usr/lib64/python3.x; /usr/lib/python3.x exists there too
# but only holds add-on packages, so guessing /usr/lib first copied the
# wrong folder. (-I: ignore any PYTHON* settings in this shell.)
STDLIB="$(python3 -I -c 'import os; print(os.path.dirname(os.__file__))')"
DYNLOAD="$(python3 -I -c 'import sys; print(next((p for p in sys.path if p.endswith("lib-dynload")), ""))')"

# ...and where it will look for them once PYTHONHOME points at our runtime:
# <PYTHONHOME>/<platlibdir>/python3.x, where platlibdir is "lib64" on
# Fedora and "lib" on most other distros.
PLATLIBDIR="$(python3 -I -c 'import sys; print(getattr(sys, "platlibdir", "lib"))')"

RUNTIME="$APP_ROOT/pyruntime"
RT_STDLIB="$RUNTIME/$PLATLIBDIR/python${PYVER}"
mkdir -p "$RUNTIME/bin" "$RT_STDLIB" "$RUNTIME/site-packages"

cp "$REAL_PYTHON" "$RUNTIME/bin/python3"
chmod +x "$RUNTIME/bin/python3"

[ -f "$STDLIB/os.py" ] || die "Couldn't find Python's standard library (looked in $STDLIB)."
for entry in "$STDLIB"/*; do
    base="$(basename "$entry")"
    case "$base" in
        # Tests, IDE and GUI pieces the app never uses; the system's add-on
        # packages (the app's own dependencies are installed separately
        # below); and the static build library (large).
        test|__pycache__|idlelib|tkinter|turtledemo|site-packages|dist-packages|config-*) continue ;;
    esac
    cp -r "$entry" "$RT_STDLIB/"
done
# Compiled modules usually sit inside the stdlib folder (copied above);
# fetch them separately if this Python keeps them somewhere else.
if [ -n "$DYNLOAD" ] && [ ! -d "$RT_STDLIB/lib-dynload" ]; then
    cp -r "$DYNLOAD" "$RT_STDLIB/lib-dynload"
fi
find "$RUNTIME" -name "__pycache__" -prune -exec rm -rf {} +
[ -d "$RT_STDLIB/lib-dynload" ] || die "Couldn't find Python's compiled modules (lib-dynload)."

# Also grab the shared lib CPython needs to actually start (libpython3.x.so),
# if it's a dynamically-linked build -- copy it alongside so it doesn't
# depend on the host having a matching one on its linker path.
LIBPYTHON=""
while read -r name arrow path _; do
    case "$name" in
        libpython*) [ "$arrow" = "=>" ] && LIBPYTHON="$path"; break ;;
    esac
done < <(ldd "$REAL_PYTHON" 2>/dev/null || true)
if [ -n "$LIBPYTHON" ] && [ -f "$LIBPYTHON" ]; then
    mkdir -p "$RUNTIME/libdyn"
    cp "$LIBPYTHON" "$RUNTIME/libdyn/"
fi

echo "==> Installing dependencies into the flat site-packages dir"
python3 -m pip install --quiet --target="$RUNTIME/site-packages" -r "$HERE/requirements.txt"

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
# The app removes these again once Python is running (see
# _clean_child_environment in server.py), so the browser and other
# programs it starts get the system's own settings.
export BPREADER_APPIMAGE=1
export BPREADER_HOST_LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}"
export PYTHONHOME="$RUNTIME"
export PYTHONPATH="$RUNTIME/site-packages"
export PYTHONNOUSERSITE=1
export LD_LIBRARY_PATH="$RUNTIME/libdyn:${LD_LIBRARY_PATH:-}"
exec "$RUNTIME/bin/python3" "$APP_SRC/server.py" "$@"
EOF
chmod +x "$APPDIR/AppRun"

echo "==> Checking the bundled Python runs on its own"
# Same setup AppRun uses, but with an empty environment so nothing from
# this machine's own Python can cover up a missing piece. Imports the app
# and the omblepy engine (with a throwaway data folder); nothing connects
# to Bluetooth.
SMOKE_DATA="$BUILD/smoke-test-data"
if ! (cd "$APP_ROOT" && env -i HOME="$HOME" PATH="/usr/bin:/bin" \
        BP_READER_DATA_DIR="$SMOKE_DATA" \
        PYTHONHOME="$RUNTIME" PYTHONPATH="$RUNTIME/site-packages" PYTHONNOUSERSITE=1 \
        LD_LIBRARY_PATH="$RUNTIME/libdyn" \
        timeout 120 "$RUNTIME/bin/python3" -c '
import sys
import server, omron_reader
omron_reader._load_omblepy_module()
print("    OK: Python " + sys.version.split()[0] + ", app and omblepy import cleanly")
'); then
    die "The bundled Python couldn't start or load the app (see the error above), so the AppImage wasn't built."
fi
rm -rf "$SMOKE_DATA"

echo "==> Adding desktop file + icon"
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

# The icon lives in the repo (static/icon.svg, with a 256px PNG next to it).
cp "$HERE/static/icon.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/bpreader.png"
cp "$HERE/static/icon.png" "$APPDIR/bpreader.png"

echo "==> Downloading appimagetool"
if [ ! -f "$HERE/appimagetool.AppImage" ]; then
    download "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" \
        "$HERE/appimagetool.AppImage"
    chmod +x "$HERE/appimagetool.AppImage"
fi

echo "==> Packaging AppImage"
cd "$HERE"
ARCH=x86_64 ./appimagetool.AppImage --appimage-extract-and-run "$APPDIR" BPReader-x86_64.AppImage

echo
echo "Done: $HERE/BPReader-x86_64.AppImage"
echo "Next: ./install_appimage.sh $HERE/BPReader-x86_64.AppImage"
