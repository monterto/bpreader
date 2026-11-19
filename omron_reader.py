"""
Thin wrapper around the omblepy tool (https://github.com/userx14/omblepy).

omblepy already implements the reverse-engineered Omron BLE protocol
correctly for our device family (BP7450/BP7455 = HEM-7342T) and many
others. Rather than re-implementing that protocol (risky -- a wrong EEPROM
write can corrupt the monitor's pressure-sensor calibration data), this
module imports omblepy.py directly as a Python module and calls its main()
coroutine in-process, with sys.argv temporarily swapped to the arguments
we want.

We do NOT shell out to a separate "python omblepy.py" subprocess: once this
app is frozen into a single .exe (PyInstaller), there is no standalone
Python interpreter left to invoke that way -- the frozen exe *is* the
interpreter. Calling omblepy's main() in-process works identically whether
we're running from source or frozen.

How data is organised:
  - "devices" are monitors this computer has paired with (model + MAC).
  - "people" are reading histories (one CSV each, in READINGS_DIR).
  - Each device maps its own user slots (most monitors have 2) to a person,
    or to nobody. So your monitor's user 1 can feed "Me", and a friend's
    monitor can feed a separate "Alex" history without touching yours.
  - Every sync is a full, read-only download into a fresh scratch folder;
    the app then merges those readings into the mapped people's CSVs.

Everything runs locally. No data leaves this machine.
"""
import asyncio
import contextlib
import csv
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import sys
import threading
from datetime import datetime

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

# User-facing home for readings: one folder + CSV per person.
READINGS_DIR = DATA_DIR / "readings"

# Scratch folder each sync/pair runs omblepy in. Cleared before every run,
# so what omblepy writes there is exactly what's on the monitor.
WORK_DIR = DATA_DIR / "sync-work"

# The monitor this app was originally written for. BP7455CAN == Omron
# 10 Series == BP7450 internally == HEM-7342T in omblepy's device table.
DEFAULT_MODEL = "hem-7342t"

# omblepy's model ids are the part numbers on the monitor's label; these
# are the names they're sold under (from omblepy's support table).
MODEL_NAMES = {
    "hem-6232t": "RS7 Intelli IT",
    "hem-7143t": "X2 Smart",
    "hem-7150t": "BP7250",
    "hem-7155t": "M400 / M4 / X4 Smart",
    "hem-7280t": "MIT5s Connect",
    "hem-7322t": "M700 Intelli IT",
    "hem-7342t": "BP7450 / BP7455CAN",
    "hem-7361t": "M500 / M7 Intelli IT",
    "hem-7377t": "BP5360",
    "hem-7380t": "X7 Smart AFib / M7 Intelli IT AFib",
    "hem-7381t": "BP7465",
    "hem-7530t": "Omron Complete",
    "hem-7600t": "Omron Evolv",
}

CSV_FIELDS = ["datetime", "dia", "sys", "bpm", "mov", "ihb"]

_config_lock = threading.RLock()


# ---------------------------------------------------------------- config

def load_config():
    """Small local JSON file: paired devices, people, and settings."""
    with _config_lock:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text())
        return {}


def save_config(cfg):
    # Write to a temp file and swap it in, so a crash mid-write can't leave
    # a half-written config behind.
    with _config_lock:
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2))
        os.replace(tmp, CONFIG_FILE)


def update_config(change):
    """Load, apply change(cfg), save -- as one step, so two requests can't
    overwrite each other's edits."""
    with _config_lock:
        cfg = load_config()
        result = change(cfg)
        save_config(cfg)
        return result


# ---------------------------------------------------------------- omblepy

def _resource_root():
    """Where bundled resources (omblepy source) ship from, in every mode
    this app can run in:
      - PyInstaller onefile: sys._MEIPASS (temp extraction dir)
      - PyInstaller onedir: the folder next to the .exe
      - plain dev checkout / AppImage: this file's own folder
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


_bundled_fp_cache = {}


def _fingerprint(folder):
    """Hash of omblepy's code, to tell whether the bundled copy changed."""
    if folder in _bundled_fp_cache:
        return _bundled_fp_cache[folder]
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*.py")):
        h.update(p.relative_to(folder).as_posix().encode())
        h.update(p.read_bytes())
    _bundled_fp_cache[folder] = h.hexdigest()
    return _bundled_fp_cache[folder]


def get_omblepy_dir():
    """Return a writable directory containing omblepy.py and its
    deviceSpecific/ folder, copied from the copy bundled with this app.

    The copy is refreshed whenever the bundled omblepy changes, so a rebuilt
    app also brings omblepy's fixes and newly supported monitors. (Readings
    no longer live in this folder, so refreshing it is safe; any CSVs left
    here by older versions are never touched.)"""
    cfg = load_config()
    if cfg.get("omblepy_dir"):
        return pathlib.Path(cfg["omblepy_dir"])  # user-managed copy

    target = DATA_DIR / "omblepy"
    source = _find_bundled_omblepy()
    marker = target / ".bundled-version"
    if source is not None:
        fp = _fingerprint(source)
        current = marker.read_text().strip() if marker.exists() else None
        if current != fp or not (target / "omblepy.py").exists():
            target.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(target / "deviceSpecific", ignore_errors=True)
            shutil.copytree(source, target, dirs_exist_ok=True)
            marker.write_text(fp)
    elif not (target / "omblepy.py").exists():
        raise FileNotFoundError(
            "Could not find omblepy.py bundled with this app, and none "
            f"is set up yet at {target}. If running from source, clone "
            "https://github.com/userx14/omblepy next to this app's folder."
        )
    _make_importable(target / "omblepy.py")
    return target


def _make_importable(omblepy_py_path):
    """omblepy.py calls asyncio.run(main()) unconditionally at the bottom
    of the file, with no 'if __name__ == "__main__":' guard -- fine when
    it's run as a standalone script, but it means simply *importing* the
    module immediately tries to run it with whatever sys.argv happens to
    be at import time. We patch that one line, on our own local writable
    copy only (never the bundled/upstream source), so importing it behaves
    like a normal library. This changes zero protocol logic."""
    text = omblepy_py_path.read_text()
    unguarded = "\nasyncio.run(main())"
    guarded = '\nif __name__ == "__main__":\n    asyncio.run(main())'
    if unguarded in text and guarded not in text:
        text = text.replace(unguarded, guarded, 1)
        omblepy_py_path.write_text(text)


_omblepy_module = None


def _load_omblepy_module():
    """Import omblepy.py as a module, once, from its writable copy."""
    global _omblepy_module
    if _omblepy_module is not None:
        return _omblepy_module

    omblepy_dir = get_omblepy_dir()
    # omblepy finds its per-model drivers via a *relative* "./deviceSpecific"
    # path, which only works when run from its own folder. Syncs now run in
    # a scratch folder, so give Python the absolute path too.
    for p in (omblepy_dir / "deviceSpecific", omblepy_dir):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))

    import importlib
    _omblepy_module = importlib.import_module("omblepy")
    return _omblepy_module


def list_models():
    """Monitor models omblepy can read, for the Devices screen."""
    folder = get_omblepy_dir() / "deviceSpecific"
    models = []
    for f in sorted(folder.glob("hem-*.py")):
        model_id = f.stem.lower()
        models.append({
            "id": model_id,
            "name": model_display_name(model_id),
            "users": _model_user_count(f),
        })
    return models


def model_display_name(model_id):
    base = MODEL_NAMES.get(model_id) or MODEL_NAMES.get(model_id.rstrip("0123456789").rstrip("t") + "t")
    label = model_id.upper()
    return f"{base} ({label})" if base else label


def _model_user_count(driver_file):
    """How many user slots a model has: the length of its driver's
    userStartAdressesList (sic, omblepy's spelling). Defaults to 2."""
    try:
        m = re.search(r"userStartAdressesList\s*=\s*\[([^\]]*)\]", driver_file.read_text())
        if m:
            n = len([x for x in m.group(1).split(",") if x.strip()])
            if n:
                return n
    except OSError:
        pass
    return 2


def model_user_count(model_id):
    f = get_omblepy_dir() / "deviceSpecific" / f"{model_id}.py"
    return _model_user_count(f) if f.exists() else 2


# ---------------------------------------------------------------- Bluetooth

async def scan_devices(timeout=6.0):
    """Scan for nearby BLE devices and return them sorted by signal strength.

    We don't filter by name because Omron cuffs advertise differently across
    firmware versions/modes -- simplest to show everything nearby and let the
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


def _fresh_work_dir():
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    return WORK_DIR


def _run_omblepy(model, mac, pair=False, workdir=None, new_only=False, time_sync=False):
    """Call omblepy's main() in-process, with sys.argv temporarily swapped
    and the working directory set to workdir (where omblepy writes its
    user1.csv/user2.csv)."""
    module = _load_omblepy_module()
    omblepy_dir = get_omblepy_dir()

    argv = [str(omblepy_dir / "omblepy.py"), "-d", model, "-m", mac]
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
        os.chdir(workdir or omblepy_dir)
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

    return {
        "ok": ok,
        "stdout": buf.getvalue(),
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


# ---------------------------------------------------------------- people

def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:30] or "person"


def get_people():
    return load_config().get("people", [])


def person_csv_path(person_id):
    """readings/<id>/<id>.csv. Kept in its own folder so it's obvious at a
    glance whose file is whose, even if a CSV gets copied elsewhere."""
    if not re.fullmatch(r"[a-z0-9-]+", person_id or ""):
        raise ValueError("Invalid person")
    return READINGS_DIR / person_id / f"{person_id}.csv"


def add_person(name):
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("Enter a name.")

    def change(cfg):
        people = cfg.setdefault("people", [])
        taken = {p["id"] for p in people}
        base = "p-" + _slug(name)
        pid, n = base, 2
        while pid in taken:
            pid, n = f"{base}-{n}", n + 1
        person = {"id": pid, "name": name}
        people.append(person)
        return person
    return update_config(change)


def rename_person(person_id, name):
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("Enter a name.")

    def change(cfg):
        for p in cfg.get("people", []):
            if p["id"] == person_id:
                p["name"] = name
                return p
        raise ValueError("Unknown person")
    return update_config(change)


# ---------------------------------------------------------------- devices

def get_devices():
    return load_config().get("devices", [])


def get_device(device_id):
    for d in get_devices():
        if d["id"] == device_id:
            return d
    return None


def _clean_users_map(users, slots, people_ids):
    """Device slot -> person id (or None for "don't import")."""
    out = {}
    for i in range(1, slots + 1):
        v = (users or {}).get(str(i))
        out[str(i)] = v if v in people_ids else None
    return out


def save_paired_device(mac, model, name, users):
    """Add (or update) a device after a successful pairing, and make it the
    one Sync uses."""
    slots = model_user_count(model)

    def change(cfg):
        people_ids = {p["id"] for p in cfg.get("people", [])}
        devices = cfg.setdefault("devices", [])
        existing = next((d for d in devices if d["mac"].upper() == mac.upper()), None)
        if existing is None:
            taken = {d["id"] for d in devices}
            n = 1
            while f"dev{n}" in taken:
                n += 1
            existing = {"id": f"dev{n}", "mac": mac}
            devices.append(existing)
        existing.update({
            "model": model,
            "name": (name or "").strip()[:60] or MODEL_NAMES.get(model) or model.upper(),
            "users": _clean_users_map(users, slots, people_ids),
        })
        cfg["active_device"] = existing["id"]
        cfg["mac"] = mac  # kept for older versions of this app
        return existing
    return update_config(change)


def update_device(device_id, name=None, users=None):
    def change(cfg):
        dev = next((d for d in cfg.get("devices", []) if d["id"] == device_id), None)
        if dev is None:
            raise ValueError("Unknown device")
        if name is not None and name.strip():
            dev["name"] = name.strip()[:60]
        if users is not None:
            people_ids = {p["id"] for p in cfg.get("people", [])}
            dev["users"] = _clean_users_map(users, model_user_count(dev["model"]), people_ids)
        return dev
    return update_config(change)


def set_active_device(device_id):
    def change(cfg):
        if not any(d["id"] == device_id for d in cfg.get("devices", [])):
            raise ValueError("Unknown device")
        cfg["active_device"] = device_id
    update_config(change)


def remove_device(device_id):
    """Forget a monitor. Readings already imported from it stay put."""
    def change(cfg):
        cfg["devices"] = [d for d in cfg.get("devices", []) if d["id"] != device_id]
        if cfg.get("active_device") == device_id:
            cfg["active_device"] = cfg["devices"][0]["id"] if cfg["devices"] else None
    update_config(change)


# ---------------------------------------------------------------- pair / sync

def pair_device(mac, model=DEFAULT_MODEL):
    """Writes this computer's pairing key to the monitor (omblepy -p)."""
    _trust_device_linux(mac)
    result = _run_omblepy(model, mac, pair=True, workdir=_fresh_work_dir())
    if result["ok"]:
        _trust_device_linux(mac)  # device is now definitely known to BlueZ
    return result


def sync_device(device_id):
    """Full, read-only download from one monitor, then merge each of its
    user slots into the person that slot is mapped to.

    Full rather than "new only": omblepy's new-only mode writes the
    monitor's unread-record counter (an EEPROM write that omblepy lists as
    untested on the HEM-7342T), while a full read only reads -- and takes a
    few seconds. New readings are still found by comparing with what's
    already stored."""
    dev = get_device(device_id)
    if dev is None:
        return {"ok": False, "stdout": "", "stderr": "Unknown device", "returncode": 1}
    _trust_device_linux(dev["mac"])
    work = _fresh_work_dir()
    result = _run_omblepy(dev["model"], dev["mac"], workdir=work)
    new_by_person = {}
    if result["ok"]:
        for slot, person_id in (dev.get("users") or {}).items():
            if not person_id:
                continue
            rows = _read_rows(work / f"user{slot}.csv")
            added = _merge_into_person(person_id, rows)
            new_by_person.setdefault(person_id, []).extend(added)

        def mark(cfg):
            for d in cfg.get("devices", []):
                if d["id"] == device_id:
                    d["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        update_config(mark)
    result["new_datetimes"] = new_by_person
    return result


# ---------------------------------------------------------------- CSV storage

def _read_rows(path):
    """Raw CSV rows (omblepy's column names), or [] if there's no file."""
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: row.get(k, "") for k in CSV_FIELDS} for row in csv.DictReader(f) if row.get("datetime")]


def _write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in sorted(rows, key=lambda r: r["datetime"]):
            writer.writerow(r)
    os.replace(tmp, path)


def _merge_into_person(person_id, rows):
    """Add readings to a person's history; returns the datetimes that are
    new. A reading already stored (same date and time) is replaced by the
    monitor's copy, never duplicated."""
    path = person_csv_path(person_id)
    existing = {r["datetime"]: r for r in _read_rows(path)}
    added = []
    for r in rows:
        if r["datetime"] not in existing:
            added.append(r["datetime"])
        existing[r["datetime"]] = r
    if rows:
        _write_rows(path, existing.values())
    return added


def _to_record(row):
    return {
        "datetime": row["datetime"],
        "systolic": int(row["sys"]),
        "diastolic": int(row["dia"]),
        "pulse": int(row["bpm"]),
        "movement": row.get("mov", "0") == "1",
        "irregular_heartbeat": row.get("ihb", "0") == "1",
    }


def load_records():
    """All readings, newest first, keyed by person id."""
    out = {}
    for p in get_people():
        recs = [_to_record(r) for r in _read_rows(person_csv_path(p["id"]))]
        recs.sort(key=lambda r: r["datetime"], reverse=True)
        out[p["id"]] = recs
    return out


def open_readings_folder():
    """Opens READINGS_DIR in the OS's file manager. Best-effort: if nothing
    matches (unrecognized platform, no GUI file manager available, etc.)
    the caller gets an exception and the API surfaces it."""
    READINGS_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(READINGS_DIR))
    elif sys.platform == "darwin":
        import subprocess
        subprocess.run(["open", str(READINGS_DIR)], check=True)
    else:
        import subprocess
        subprocess.run(["xdg-open", str(READINGS_DIR)], check=True)


def build_averaged_export(records, window_minutes=6):
    """Group readings taken close together in time -- a typical
    multiple-reading "session", per common BP guidance to take a couple of
    readings a minute or two apart and average them -- into a single row
    each. Purely a computed view: reads already-loaded records and returns
    new dicts, never touches the underlying CSV files.

    Grouping is chain-based, not window-from-start: a reading joins the
    current group if it's within `window_minutes` of the *previous*
    reading in that group. That way a session of readings taken ~1-2
    minutes apart stays together even if the session as a whole runs
    longer than window_minutes, while a single large gap still correctly
    starts a new session.
    """
    if not records:
        return []

    from datetime import timedelta as _td
    fmt = "%Y-%m-%d %H:%M:%S"
    window = _td(minutes=window_minutes)

    chronological = sorted(records, key=lambda r: r["datetime"])
    groups = [[chronological[0]]]
    for rec in chronological[1:]:
        prev_time = datetime.strptime(groups[-1][-1]["datetime"], fmt)
        this_time = datetime.strptime(rec["datetime"], fmt)
        if this_time - prev_time <= window:
            groups[-1].append(rec)
        else:
            groups.append([rec])

    averaged = []
    for group in groups:
        n = len(group)
        averaged.append({
            "datetime": group[0]["datetime"],  # session start
            "systolic": round(sum(r["systolic"] for r in group) / n),
            "diastolic": round(sum(r["diastolic"] for r in group) / n),
            "pulse": round(sum(r["pulse"] for r in group) / n),
            # A flag on any reading in the session is worth carrying
            # forward -- better to over-flag a possible quality issue than
            # let it get averaged away and disappear.
            "movement": any(r["movement"] for r in group),
            "irregular_heartbeat": any(r["irregular_heartbeat"] for r in group),
            "readings_averaged": n,
        })
    averaged.sort(key=lambda r: r["datetime"], reverse=True)
    return averaged


# ---------------------------------------------------------------- setup

def ensure_setup():
    """Brings data from earlier versions forward, once:
      - people "User 1" / "User 2" for the original readings/user1, user2;
      - readings that only existed in omblepy's own folder (very early
        versions kept them there) are merged in;
      - the previously paired monitor becomes a device mapped 1->User 1,
        2->User 2, exactly as before.
    Nothing is deleted; it only ever adds."""
    def change(cfg):
        if "people" not in cfg:
            cfg["people"] = [{"id": "user1", "name": "User 1"}, {"id": "user2", "name": "User 2"}]
            for idx in (1, 2):
                legacy = DATA_DIR / "omblepy" / f"user{idx}.csv"
                rows = _read_rows(legacy)
                if rows:
                    _merge_into_person(f"user{idx}", rows)
        if "devices" not in cfg:
            cfg["devices"] = []
            if cfg.get("mac"):
                cfg["devices"].append({
                    "id": "dev1",
                    "mac": cfg["mac"],
                    "model": DEFAULT_MODEL,
                    "name": "BP7455CAN",
                    "users": {"1": "user1", "2": "user2"},
                })
                cfg["active_device"] = "dev1"
    update_config(change)


ensure_setup()
