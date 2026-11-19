# BP Reader

## Disclaimer

This personal project was created using AI tools.
It is not affiliated with or endorsed by Omron. Not a medical
device, not validated for clinical use — it downloads numbers your monitor
already measured, it doesn't measure anything itself. Use at your own
judgment.

It's built for personal use and **tested on one setup only: Bazzite
(Fedora 45) with an Omron BP7455CAN.** Anything else may work, but hasn't
been tried, and there's no support.

## Description

A small local app for downloading readings off an Omron BP7455CAN (and
similar "10 Series" / HEM-7342T) blood pressure monitor over Bluetooth.
Intended to avoid uploading personal health information to Omron's servers.

Omron's own app works fine, but it requires an account and syncs your
readings to their cloud to use it at all. This app talks to the monitor
directly and writes readings straight to CSV files on your machine.

- One-button sync, with the monitor's own steps on screen
- Readings table and trend line per person, with new readings highlighted
- CSV export, optionally averaging readings taken minutes apart
- Several monitors and people: a borrowed monitor's readings can go to
  their own person instead of mixing with yours
- Opens in its own window and stops when you close it

## How it works

The Bluetooth protocol these monitors use is proprietary and undocumented
by Omron, but it's been reverse-engineered by the community in
[`omblepy`](https://github.com/userx14/omblepy). This app doesn't
re-implement that protocol — it drives `omblepy` directly (in-process, not
as a subprocess) and wraps it in a small Flask server, shown in an
app-style browser window.

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

Other Omron models that omblepy supports can be picked when adding a monitor
(gear icon → Devices). Check
[omblepy's device compatibility table](https://github.com/userx14/omblepy#omron-device-support-matrix)
for how well each one is tested.

## Installing

> **Tested on one setup only.** This is a personal project, built for and
> tested on the author's own machine: **Bazzite (Fedora 45) with an Omron
> BP7455CAN**, using the AppImage built on Fedora 45. Every other option
> below is provided as-is: the scripts exist and should work, but they
> haven't been tested, and there's no support. Use at your own risk.

### Linux — AppImage from Releases (the tested route)

Download `BPReader-x86_64.AppImage` from the
[Releases page](https://github.com/monterto/bpreader/releases). It's built
on Fedora 45, so it's meant for Fedora-based systems (Bazzite, Fedora,
Silverblue, …). On other distros, build your own (below).

- **With [Gear Lever](https://flathub.org/apps/it.mijorus.gearlever):** add
  the AppImage, then set its update source to *GitHub*, repo
  `monterto/bpreader`, file name `BPReader-x86_64.AppImage`. New releases
  then show up as updates.
- **Without it:** `./install_appimage.sh BPReader-x86_64.AppImage` adds a
  menu entry and Desktop icon, launched in a way that works even without
  FUSE (AppImages normally need it to mount themselves).

### Linux — build the AppImage yourself *(untested outside Fedora 45)*

Needs python3 (3.9+) with pip, git, and wget or curl, plus internet for the
build; the result runs offline.

```bash
git clone https://github.com/monterto/bpreader.git
cd bpreader
./build_appimage.sh
```

Build on the distro you'll run it on: the AppImage bundles that system's
own Python, which relies on that system's libraries. The script checks the
bundled Python actually starts before packaging, so a mismatch fails the
build instead of the app.

On immutable distros (Bazzite, Silverblue), build inside a Fedora toolbox
or distrobox of the same Fedora version as your system, after
`sudo dnf install python3-pip git-core` inside it.

### Windows — build the Linux AppImage with WSL *(how the tested AppImage was first made)*

```powershell
wsl --install FedoraLinux-45
```

Then, inside that Fedora shell:

```bash
sudo dnf install -y python3-pip git-core
git clone https://github.com/monterto/bpreader.git
cd bpreader && ./build_appimage.sh
```

Clone inside WSL rather than building from a Windows folder: a Windows
checkout can have Windows line endings, which bash can't run. Copy the
finished AppImage over to your Linux machine.

### GitHub Actions (forks)

`.github/workflows/build-appimage.yml` builds the AppImage on Fedora 45 and
publishes a release when you push a version tag (like `2.0.1`), or when
you run it from the Actions tab. Change the Fedora image to match the
system you run it on.

### Linux — plain install, no AppImage *(untested)*

```bash
./install.sh
```

Same menu entry and Desktop icon, but via a normal Python venv instead of
an AppImage. Needs python3 with venv, and git.

### Windows — .exe *(untested)*

```
build_windows.bat
```

Needs Python from python.org (with "Add to PATH" checked) and git.
Produces `dist\BPReader.exe`. Bluetooth on Windows goes through a different
backend than Linux, and none of it has been tried.

### From source, any OS *(untested beyond Linux)*

```bash
git clone https://github.com/monterto/bpreader.git
git clone https://github.com/userx14/omblepy.git   # next to the bpreader folder
cd bpreader
python3 -m venv .venv
source .venv/bin/activate        # .venv\Scripts\activate on Windows
pip install -r requirements.txt
python3 server.py
```

On macOS, Bluetooth devices show up with an ID instead of a MAC address;
omblepy accepts either, but this hasn't been tried.

## Using it

The app opens in its own window (an app-style window if Chrome, Chromium,
Brave, Edge or Vivaldi is installed, including as a Flatpak; otherwise a
normal browser tab). Closing the window stops it.

1. **Set up a monitor (once):** the gear icon opens **Devices**. Under *Add
   or replace a monitor*, pick the model, hold the **[Connection]** button
   on the back of the monitor until a blinking `P` appears, click **Scan**,
   pick your monitor, choose whose readings are on it, then **Pair**.
   Success shows `OK` on the monitor.
2. **Sync:** with the monitor off, press the **[Connection]** button on its
   back once (a short press, don't hold it) so the transfer symbol flashes,
   then click **Sync**. New readings are highlighted. (Don't use the front
   START/STOP button for this: it starts a measurement.)
3. **Export:** **Export CSV** downloads the person you're viewing; the
   files are also always on disk (see below).

**People and monitors.** Readings are kept per person. Each monitor maps
its own user slots (most have two) to a person, or to *Don't import*. Your
monitor might feed *Me* and *Partner*; a friend's borrowed monitor can feed
a new person (*New person…* in the list), so their readings get their own
table and CSV and never mix with yours. Replacing your monitor? Add the
new one: it starts with the same mapping, so your history just continues.
The mappings can be changed any time in Devices.

A monitor can only stay paired with one device at a time — pairing here
means the official Omron Connect app (or a paired phone) will stop being
able to talk to it, until/unless you re-pair it there. This is reversible
any time by re-entering pairing mode and pairing with whichever app you
want.

## Where your data lives

| OS | Location |
|---|---|
| Linux | `~/.local/share/bp-reader/readings/<person>/<person>.csv` |
| Windows | `%LOCALAPPDATA%\BPReader\readings\<person>\<person>.csv` |

Plain CSV, one row per reading (`datetime,dia,sys,bpm,mov,ihb`). The
original two users keep their `user1` / `user2` folders; people added later
get folders like `p-alex`. Paired monitors, people and their names are in
`config.json` next to the `readings` folder.

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
The monitor isn't broadcasting over Bluetooth. Press the **[Connection]**
button on its back once (short press) so the transfer symbol flashes, then
click Sync again right away; the monitor turns itself off after 2 minutes.
If `oFF` flashes when you press it, the monitor's Bluetooth has been
switched off (that happens if the button is held for more than 10 seconds
while the monitor is off): hold the button for more than 2 seconds until
`on` appears. Per the BP7455CAN manual, sections 4.2 and 4.3.

**Nothing seems to have changed after an update.**
An older copy may still be running: it stops about 15 seconds after its
last window closes. If it doesn't, `pkill -f "bpreader/server.py"` (this
matches the real process even under AppImage's extract-and-run mode,
unlike matching on the `.AppImage` filename) then relaunch.

## Design notes

- **Closing the window stops the app.** Each open window holds a
  connection to the local server; about 15 seconds after the last one
  closes, the server exits (never in the middle of a sync). A page reload
  doesn't count. **Quit BP Reader** in Devices stops it immediately. To
  keep it running anyway (e.g. while developing), set
  `BP_READER_KEEP_RUNNING=1`.
- **Sync is always a full, read-only download.** omblepy's "new readings
  only" mode writes the monitor's unread counter to its memory, which
  omblepy lists as untested on the HEM-7342T; a full read only reads and
  takes a few seconds. New readings are found by comparing with what's
  already stored.
- **No re-implemented protocol.** All BLE communication goes through
  `omblepy`, called in-process rather than reimplemented, specifically to
  avoid the risk of a subtly wrong reverse-engineered command corrupting
  the monitor's calibration data.
- **No phone-home, no analytics.** The app itself never connects to the
  internet; it only listens on 127.0.0.1. Updates arrive only through an
  AppImage manager like Gear Lever, if you set one up.

## Project layout

```
server.py             Flask app: routes, app window, stop-when-closed lifecycle
omron_reader.py        Wraps omblepy; monitors, people and their readings CSVs
templates/index.html   The entire UI: sync, readings, Devices screen, chart
static/icon.svg        App icon (icon.png is the same at 256px, used by the builds)
.github/workflows/     Builds the AppImage on Fedora and publishes a release
install.sh             Linux install via plain venv + desktop icon
install_appimage.sh    Linux install of a pre-built AppImage, FUSE-free
build_appimage.sh      Builds the self-contained Linux AppImage from source
build_windows.bat      Builds a Windows .exe via PyInstaller
```

## A note on licensing

This project depends on and bundles [`omblepy`](https://github.com/userx14/omblepy)
to talk to the monitor. At time of writing, this upstream project doesn't
carry an explicit license file.
