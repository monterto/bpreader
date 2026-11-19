#!/bin/bash
# One-time setup: installs the app into ~/.local/share/bp-reader/app,
# builds a normal Python venv there, and adds a double-clickable icon
# both to your Desktop and to your application menu.
#
# No FUSE, no AppImage — just a regular venv + .desktop launcher, the
# same mechanism every other installed app on your system uses.
#
# Run once:  ./install.sh
set -euo pipefail

HERE="$(cd "$(dirname "${0}")" && pwd)"
INSTALL_DIR="$HOME/.local/share/bp-reader/app"
DATA_DIR="$HOME/.local/share/bp-reader"
DESKTOP_APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"

echo "==> Installing app to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp "$HERE/server.py" "$HERE/omron_reader.py" "$INSTALL_DIR/"
cp -r "$HERE/templates" "$HERE/static" "$INSTALL_DIR/"

if [ ! -d "$INSTALL_DIR/omblepy" ]; then
    echo "==> Fetching omblepy engine"
    git clone --depth 1 https://github.com/userx14/omblepy.git "$INSTALL_DIR/omblepy"
    rm -rf "$INSTALL_DIR/omblepy/.git"
fi

echo "==> Setting up Python environment (one-time, ~30s)"
if [ ! -d "$INSTALL_DIR/venv" ]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet flask bleak terminaltables

echo "==> Writing launcher"
cat > "$INSTALL_DIR/launch.sh" <<EOF
#!/bin/bash
cd "$INSTALL_DIR"
exec "$INSTALL_DIR/venv/bin/python3" "$INSTALL_DIR/server.py"
EOF
chmod +x "$INSTALL_DIR/launch.sh"

echo "==> Adding icon"
mkdir -p "$ICON_DIR"
cp "$HERE/static/icon.png" "$ICON_DIR/bpreader.png"

echo "==> Adding application menu entry"
mkdir -p "$DESKTOP_APPS_DIR"
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
echo "Done. Your data will live in: $DATA_DIR"
echo
echo "Launch it by:"
echo "  - double-clicking 'BP Reader' on your Desktop (first click may ask"
echo "    you to confirm 'Allow Launching' — that's normal, one-time only)"
echo "  - or finding 'BP Reader' in your application menu"
echo "  - or running: $INSTALL_DIR/launch.sh"
