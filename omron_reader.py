"""
Thin wrapper around the omblepy tool (https://github.com/userx14/omblepy).

omblepy already implements the reverse-engineered Omron BLE protocol
correctly for our device family (BP7450/BP7455 = HEM-7342T). Rather than
re-implementing that protocol (risky — a wrong EEPROM write can corrupt the
monitor's pressure-sensor calibration data), this module imports omblepy.py
directly as a Python module and calls its main() coroutine in-process, with
sys.argv temporarily swapped to the arguments we want.

We do NOT shell out to a separate "python omblepy.py" subprocess: once this
app is frozen into a single .exe (PyInstaller), there is no standalone
Python interpreter left to invoke that way — the frozen exe *is* the
interpreter, and re-launching it with script arguments doesn't work the way
a normal "python3 script.py" call does. Calling omblepy's main() directly,
in-process, works identically whether we're running from source or frozen.

Everything runs locally. No data leaves this machine.
"""
import asyncio
import contextlib
import io
import json
import os
import pathlib
import shutil
import sys

import bleak

APP_DIR = pathlib.Path(__file__).parent.resolve()


def _default_data_dir():
    """Cross-platform, persistent, writable location for our data.
    A frozen .exe (or an AppImage) may live somewhere read-only/ephemeral,
    so readings and the bootstrapped omblepy copy must live elsewhere."""
    override = os.environ.get("BP_READER_DATA_DIR")
    if override:
        return pathlib.Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home())
        return pathlib.Path(base) / "BPReader"
    return pathlib.Path.home() / ".local" / "share" / "bp-reader"


DATA_DIR = _default_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"

# BP7455CAN == Omron 10 Series == BP7450 internally == HEM-7342T in omblepy's
# device table (confirmed by matching EEPROM layout: 200 total records / 100
# per user across 2 user profiles, matching the BP7455CAN spec sheet).
DEVICE_PROFILE = "hem-7342t"


def load_config():
    """Small local JSON file for the one thing we persist outside of
    readings themselves: the last-paired MAC address."""
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def _resource_root():
    """Where bundled resources (omblepy source) ship from, in every mode
    this app can run in:
      - PyInstaller onefile: sys._MEIPASS (temp extraction dir)
      - PyInstaller onedir: the folder next to the .exe
      - plain dev checkout: this file's own folder
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return pathlib.Path(meipass)
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).parent
    return APP_DIR


def resource_path(*parts):
    """Path to a bundled resource (e.g. the templates/ folder), correct
    whether we're running from source or as a frozen build."""
    return _resource_root().joinpath(*parts)


def _find_bundled_omblepy():
    """Locate the bundled omblepy source, checking every place it could
    ship from depending on how this app was packaged."""
    candidates = [
        _resource_root() / "omblepy",   # bundled with the frozen app
        APP_DIR.parent / "omblepy",     # sibling folder, dev checkout
    ]
    for c in candidates:
        if (c / "omblepy.py").exists():
            return c
    return None


def get_omblepy_dir():
    """Return a writable, persistent directory containing omblepy.py and
    its deviceSpecific/ folder. Bootstraps it from the bundled copy the
    first time it's needed (one-time copy, not a per-run operation) —
    omblepy needs to read/write its CSV files and resolve relative imports
    from its own directory, so it has to live somewhere writable."""
    cfg = load_config()
    if cfg.get("omblepy_dir"):
        return pathlib.Path(cfg["omblepy_dir"])

    target = DATA_DIR / "omblepy"
    if not (target / "omblepy.py").exists():
        source = _find_bundled_omblepy()
        if source is None:
            raise FileNotFoundError(
                "Could not find omblepy.py bundled with this app, and none "
                f"is set up yet at {target}. If running from source, clone "
                "https://github.com/userx14/omblepy next to this app's folder."
            )
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, dirs_exist_ok=True)
    _make_importable(target / "omblepy.py")
    return target


def _make_importable(omblepy_py_path):
    """omblepy.py calls asyncio.run(main()) unconditionally at the bottom
    of the file, with no 'if __name__ == "__main__":' guard — fine when
    it's run as a standalone script, but it means simply *importing* the
    module immediately tries to run it with whatever sys.argv happens to
    be at import time. We patch that one line, on our own local writable
    copy only (never the bundled/upstream source), so importing it behaves
    like a normal library. This changes zero protocol logic — it only
    makes the existing "run as a script" behavior conditional, exactly the
    standard Python idiom, so `python3 omblepy.py ...` still works exactly
    as before too."""
    text = omblepy_py_path.read_text()
    unguarded = "\nasyncio.run(main())"
    guarded = '\nif __name__ == "__main__":\n    asyncio.run(main())'
    if unguarded in text and guarded not in text:
        text = text.replace(unguarded, guarded, 1)
        omblepy_py_path.write_text(text)


_omblepy_module = None


def _load_omblepy_module():
    """Import omblepy.py as a module, once, from its writable bootstrapped
    location (so its own relative sys.path tricks for deviceSpecific/
    resolve correctly)."""
    global _omblepy_module
    if _omblepy_module is not None:
        return _omblepy_module

    omblepy_dir = get_omblepy_dir()
    if str(omblepy_dir) not in sys.path:
        sys.path.insert(0, str(omblepy_dir))

    import importlib
    _omblepy_module = importlib.import_module("omblepy")
    return _omblepy_module


async def scan_devices(timeout=6.0):
    """Scan for nearby BLE devices and return them sorted by signal strength.

    We don't filter by name because Omron cuffs advertise differently across
    firmware versions/modes — simplest to show everything nearby and let the
    person pick their monitor by RSSI (it'll be the closest one) or name.
    """
    devices = await bleak.BleakScanner.discover(timeout=timeout, return_adv=True)
    results = []
    for mac, (dev, adv) in devices.items():
        results.append({
            "mac": mac,
            "name": dev.name or "(unnamed)",
            "rssi": adv.rssi,
        })
    results.sort(key=lambda d: d["rssi"], reverse=True)
    return results


def _run_omblepy(mac, pair=False, new_only=False, time_sync=False):
    """Call omblepy's main() in-process, with sys.argv temporarily swapped.
    omblepy itself must run with cwd set to its own (writable, bootstrapped)
    directory, since it resolves 'deviceSpecific' and writes its CSVs
    relative to cwd."""
    module = _load_omblepy_module()
    omblepy_dir = get_omblepy_dir()

    argv = [str(omblepy_dir / "omblepy.py"), "-d", DEVICE_PROFILE, "-m", mac]
    if pair:
        argv.append("-p")
    if new_only:
        argv.append("-n")
    if time_sync:
        argv.append("-t")

    old_argv = sys.argv
    old_cwd = os.getcwd()
    buf = io.StringIO()
    ok = True
    err = ""
    try:
        sys.argv = argv
        os.chdir(omblepy_dir)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            asyncio.run(module.main())
    except SystemExit as e:
        ok = (e.code in (None, 0))
    except Exception as e:
        ok = False
        err = str(e)
    finally:
        sys.argv = old_argv
        os.chdir(old_cwd)

    output = buf.getvalue()
    return {
        "ok": ok,
        "stdout": output,
        "stderr": err,
        "returncode": 0 if ok else 1,
    }


def _trust_device_linux(mac):
    """On Linux, BlueZ can gate GATT access behind a 'RequestAuthorization'
    prompt that only an interactive agent (e.g. `bluetoothctl`) can answer --
    otherwise every write silently fails. Marking the device Trusted tells
    BlueZ to stop asking, without needing an agent open at all. Best-effort:
    failures here are non-fatal, since the device may not be known to BlueZ
    yet on a very first attempt (before any scan has registered it)."""
    if sys.platform != "linux":
        return
    try:
        import subprocess
        subprocess.run(["bluetoothctl", "trust", mac], capture_output=True, timeout=10)
    except Exception:
        pass


def pair_device(mac):
    _trust_device_linux(mac)
    result = _run_omblepy(mac, pair=True)
    if result["ok"]:
        cfg = load_config()
        cfg["mac"] = mac
        save_config(cfg)
        _trust_device_linux(mac)  # device is now definitely known to BlueZ; make sure trust stuck
    return result


def sync_device(mac, new_only=False, time_sync=False):
    _trust_device_linux(mac)
    return _run_omblepy(mac, new_only=new_only, time_sync=time_sync)


def _read_user_csv(path):
    """Load one user's CSV into a list of plain dicts. Missing file just
    means no readings synced for that user yet -- not an error."""
    if not path.exists():
        return []
    import csv
    records = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            records.append({
                "datetime": row["datetime"],
                "systolic": int(row["sys"]),
                "diastolic": int(row["dia"]),
                "pulse": int(row["bpm"]),
                "movement": row.get("mov", "0") == "1",
                "irregular_heartbeat": row.get("ihb", "0") == "1",
            })
    return records


def load_records():
    """Read the CSV files omblepy writes (user1.csv, user2.csv) and return
    all readings, newest first, per user."""
    omblepy_dir = get_omblepy_dir()
    users = {}
    for idx in (1, 2):
        recs = _read_user_csv(omblepy_dir / f"user{idx}.csv")
        recs.sort(key=lambda r: r["datetime"], reverse=True)
        users[f"user{idx}"] = recs
    return users


def csv_paths():
    """Paths to the two per-user CSV files, for export/download."""
    omblepy_dir = get_omblepy_dir()
    return [omblepy_dir / "user1.csv", omblepy_dir / "user2.csv"]
