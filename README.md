# BP Reader

## Disclaimer

This personal project was created using AI tools.
It is not affiliated with or endorsed by Omron. Not a medical
device, not validated for clinical use — it downloads numbers your monitor
already measured, it doesn't measure anything itself. Use at your own
judgment.

## Description

A small local app for downloading readings off an Omron BP7455CAN (and
similar "10 Series" / HEM-7342T) blood pressure monitor over Bluetooth.
Intended to avoid uploading personal health information to Omron's servers.

Omron's own app works fine, but it requires an account and syncs your
readings to their cloud to use it at all. This app talks to the monitor
directly and writes readings straight to a CSV file on your machine.

## How it works

The Bluetooth protocol these monitors use is proprietary and undocumented
by Omron, but it's been reverse-engineered by the community in
[`omblepy`](https://github.com/userx14/omblepy). This app doesn't
re-implement that protocol — it drives `omblepy` directly (in-process, not
as a subprocess) and wraps it in a small Flask server with a browser-based
UI.

```
Browser UI  --->  Flask server  --->  omblepy  --->  Monitor
(index.html)      (server.py)        (BLE protocol)  (Bluetooth)
```

Everything after the Flask server stays on your machine. Readings live in
plain CSV files you can open in any spreadsheet, back up, or throw away.

## Compatibility

Built and tested against the **Omron BP7455CAN**. This is Omron's "10
Series" monitor, which internally identifies as a **HEM-7342T** — the same
core hardware sold under a few different model numbers/regions (e.g.
BP7450). If your monitor is a 10 Series device, there's a decent chance
this works for you as-is.

For other Omron models, check
[omblepy's device compatibility table](https://github.com/userx14/omblepy#omron-device-support-matrix) —
if your model is listed there, you can likely get this working by changing
`DEVICE_PROFILE` in `omron_reader.py` to match. Contributions adding proper
multi-device support are welcome.

## Installing

### Linux — AppImage (recommended)

```bash
chmod +x install_appimage.sh
./install_appimage.sh BPReader-x86_64.AppImage
```

Run once. This adds a real double-click icon to your Desktop and
application menu, launched in a way that works even on systems without
FUSE installed (Bazzite, some minimal distros, etc. — AppImages normally
need FUSE to mount themselves; this sidesteps that entirely).

Don't have the AppImage built yet? `./build_appimage.sh` builds it from
source (needs internet for that one step; the result runs fully offline
after that).

### Linux — plain install, no AppImage

```bash
./install.sh
```

Same double-click-icon result, but via a normal Python venv instead of an
AppImage. Useful if you'd rather avoid packaged binaries entirely.

### Windows

```
build_windows.bat
```

Run once (needs Python from python.org, with "Add to PATH" checked during
install). Produces `dist\BPReader.exe` — a normal double-click executable
from then on, no console window, no re-running the build step.

### From source (any OS)

```bash
git clone https://github.com/userx14/omblepy.git   # sibling folder, or see omron_reader.py
python3 -m venv .venv
source .venv/bin/activate        # .venv\Scripts\activate on Windows
pip install -r requirements.txt
python3 server.py
```

## Using it

1. **Pair (first time only):** hold the monitor's Bluetooth button until
   its display shows a blinking `-P-`. In the app, click **Scan for
   device**, pick your monitor from the list, then click **Pair**. Success
   shows `[OK]` on the monitor.
2. **Sync:** wake the monitor (press its power button once so the screen
   is on), then click **Sync now** in the app. Readings appear in the
   table, newest first, with just-downloaded ones highlighted.
3. **Export:** the "Download CSV" button gives you the raw file directly;
   it's also always sitting on disk (see below) if you'd rather script
   against it.

A monitor can only stay paired with one device at a time — pairing here
means the official Omron Connect app (or a paired phone) will stop being
able to talk to it, until/unless you re-pair it there. This is reversible
any time by re-entering pairing mode and pairing with whichever app you
want.

## Where your data lives

| OS | Location |
|---|---|
| Linux | `~/.local/share/bp-reader/omblepy/user1.csv` (+ `user2.csv`) |
| Windows | `%LOCALAPPDATA%\BPReader\omblepy\user1.csv` |

Plain CSV, one row per reading (`datetime,dia,sys,bpm,mov,ihb`). No
database, no proprietary format.

## Troubleshooting

Bluetooth stacks are inconsistent across OSes and distros, so real-world
setups can hit a few different snags. These are the ones encountered while
building this:

**AppImage won't launch on Linux, or hangs indefinitely.**
Missing FUSE (common on Bazzite, Silverblue, and other minimal/immutable
distros). Either use `install_appimage.sh` (works around it entirely), or
launch manually with `APPIMAGE_EXTRACT_AND_RUN=1 ./BPReader-x86_64.AppImage`.

**Pairing fails with a fast (~1 second) disconnect.**
Something else is already holding the monitor's one available BLE
connection slot — usually a phone with Omron Connect still running.
Disconnect it there first.

**Pairing fails after ~20+ seconds with "Could not enter key programming
mode."**
On Linux, check for a `Request authorization` prompt: run `bluetoothctl`,
then `agent on` and `default-agent`, and retry pairing — if a prompt
appears, answer `yes`. To stop this happening every time, mark the device
trusted: `bluetoothctl trust <MAC>` (the app also does this automatically
after a successful pair, going forward).

**Sync fails with "not advertising within 30s."**
The monitor's screen has gone idle and it's stopped broadcasting
Bluetooth. Press its power button once to wake it, then sync again
promptly.

**Nothing seems to have changed after an update.**
The server keeps running in the background after you close the browser
tab (by design — see below) — a stale instance can end up serving an old
build. `pkill -f "bpreader/server.py"` (this matches the real process even
under AppImage's extract-and-run mode, unlike matching on the `.AppImage`
filename) then relaunch.

## Design notes

- **Closing the browser tab doesn't stop the app.** It keeps running
  locally so you can reopen the page without a fresh Bluetooth handshake
  each time. Use the **Quit app** button (or `Ctrl+C` if running from a
  terminal) to actually stop it.
- **No re-implemented protocol.** All BLE communication goes through
  `omblepy`, called in-process rather than reimplemented, specifically to
  avoid the risk of a subtly wrong reverse-engineered command corrupting
  the monitor's calibration data.
- **No auto-update, no phone-home, no analytics.** The manifest/build
  scripts declare no network capability beyond the Bluetooth stack itself.

## Project layout

```
server.py             Flask app: routes, background-server lifecycle, browser auto-open
omron_reader.py        Wraps omblepy: bootstraps it, calls it in-process, reads/writes CSVs
templates/index.html   The entire UI: styling, pairing/sync flow, readings table, chart
install.sh             Linux install via plain venv + desktop icon
install_appimage.sh    Linux install of a pre-built AppImage, FUSE-free
build_appimage.sh      Builds the self-contained Linux AppImage from source
build_windows.bat      Builds a Windows .exe via PyInstaller
```

## A note on licensing

This project depends on and bundles [`omblepy`](https://github.com/userx14/omblepy)
to talk to the monitor. At time of writing, this upstream project doesn't
carry an explicit license file.
