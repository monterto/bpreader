#!/bin/bash
# Installs BPReader-x86_64.AppImage as a proper double-click desktop app,
# launched in a way that completely bypasses FUSE (which Bazzite doesn't
# ship by default). No FUSE, no rpm-ostree layering, no reboot needed.
#
# Run once: ./install_appimage.sh /path/to/BPReader-x86_64.AppImage
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 /path/to/BPReader-x86_64.AppImage"
    exit 1
fi
SRC_APPIMAGE="$(readlink -f "$1")"
if [ ! -f "$SRC_APPIMAGE" ]; then
    echo "File not found: $SRC_APPIMAGE"
    exit 1
fi

INSTALL_DIR="$HOME/.local/share/bp-reader"
DESKTOP_APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"

mkdir -p "$INSTALL_DIR" "$DESKTOP_APPS_DIR" "$ICON_DIR"

echo "==> Installing AppImage to $INSTALL_DIR"
cp "$SRC_APPIMAGE" "$INSTALL_DIR/BPReader-x86_64.AppImage"
chmod +x "$INSTALL_DIR/BPReader-x86_64.AppImage"

echo "==> Writing FUSE-free launcher"
cat > "$INSTALL_DIR/launch.sh" <<EOF
#!/bin/bash
# APPIMAGE_EXTRACT_AND_RUN skips FUSE mounting entirely: the AppImage is
# unpacked to a temp dir and executed directly. Same effect as passing
# --appimage-extract-and-run, just settable from a .desktop Exec= line.
export APPIMAGE_EXTRACT_AND_RUN=1
exec "$INSTALL_DIR/BPReader-x86_64.AppImage" "\$@"
EOF
chmod +x "$INSTALL_DIR/launch.sh"

echo "==> Extracting icon from the AppImage"
(
    cd "$INSTALL_DIR"
    APPIMAGE_EXTRACT_AND_RUN=1 "$INSTALL_DIR/BPReader-x86_64.AppImage" --appimage-extract bpreader.png >/dev/null 2>&1 || true
    if [ -f squashfs-root/bpreader.png ]; then
        cp squashfs-root/bpreader.png "$ICON_DIR/bpreader.png"
    fi
    rm -rf squashfs-root
)

echo "==> Adding application menu entry"
cat > "$DESKTOP_APPS_DIR/bpreader.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=BP Reader
Comment=Local reader for Omron BP7455CAN blood pressure monitor
Exec=$INSTALL_DIR/launch.sh
Icon=bpreader
Categories=Utility;
Terminal=false
EOF
chmod +x "$DESKTOP_APPS_DIR/bpreader.desktop"

echo "==> Adding Desktop icon"
if [ -d "$HOME/Desktop" ]; then
    cp "$DESKTOP_APPS_DIR/bpreader.desktop" "$HOME/Desktop/BP Reader.desktop"
    chmod +x "$HOME/Desktop/BP Reader.desktop"
fi

update-desktop-database "$DESKTOP_APPS_DIR" 2>/dev/null || true

echo
echo "Done."
echo "Launch it by double-clicking 'BP Reader' on your Desktop or in your"
echo "application menu (first double-click may ask you to confirm 'Allow"
echo "Launching' -- that's a one-time OS prompt, not related to FUSE)."
echo
echo "Your readings will live in: ~/.local/share/bp-reader/"
