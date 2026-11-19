"""
Local-only app for downloading readings from Omron blood pressure monitors
(built for the BP7455CAN / HEM-7342T) over Bluetooth LE.

Everything stays on this machine: no accounts, no cloud sync, no telemetry.
It runs a small web server on 127.0.0.1:5757 and opens it in its own
app-style browser window when a Chromium-based browser is available (a
normal tab otherwise). Closing the window stops the app.
"""
import asyncio
import csv
import io
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from flask import Flask, jsonify, request, send_file, render_template, Response

import omron_reader as reader

app = Flask(__name__,
            template_folder=str(reader.resource_path("templates")),
            static_folder=str(reader.resource_path("static")))

PORT = 5757
URL = f"http://127.0.0.1:{PORT}"

# Only these Host headers are accepted. A page on any other website can
# point its own domain at 127.0.0.1 ("DNS rebinding") and then read
# /api/records -- i.e. your blood-pressure history -- from this server.
# Checking Host stops that, since the browser still sends the attacker's
# hostname.
_ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
_ALLOWED_ORIGINS = {f"http://{h}" for h in _ALLOWED_HOSTS}

# omblepy is driven by swapping the process-wide sys.argv and cwd (see
# omron_reader._run_omblepy), and a monitor only talks to one client at a
# time anyway. Flask serves requests on multiple threads, so a double-click
# on Sync, or Scan during a Sync, could interleave and corrupt both runs.
_ble_lock = threading.Lock()

# Closing the window stops the app: every open page keeps one /api/events
# connection open. Once the last one has been gone for CLOSE_GRACE_S (long
# enough to survive a page reload), the server exits -- but never in the
# middle of a Bluetooth operation.
CLOSE_GRACE_S = 15
_pages_lock = threading.Lock()
_open_pages = 0
_ever_had_page = False
_last_page_left = None


@app.before_request
def _local_only_guard():
    if request.host not in _ALLOWED_HOSTS:
        return "Forbidden host", 403
    # Block cross-site form posts (e.g. a random web page silently hitting
    # /api/shutdown). Same-origin fetch() from our own page sends a matching
    # Origin; requests with no Origin at all (curl, etc.) are allowed.
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin is not None and origin not in _ALLOWED_ORIGINS:
            return "Forbidden origin", 403
    return None


def run_async(coro):
    return asyncio.run(coro)


def _json_body():
    return request.get_json(silent=True) or {}


def _busy():
    return jsonify({"ok": False, "error": "Another Bluetooth operation is already running. Wait for it to finish."}), 409


def _fail(message, code=400):
    return jsonify({"ok": False, "error": message}), code


def _state():
    """Everything the page needs to draw itself."""
    cfg = reader.load_config()
    devices = []
    for d in cfg.get("devices", []):
        devices.append({**d,
                        "model_name": reader.model_display_name(d["model"]),
                        "slots": reader.model_user_count(d["model"])})
    return {
        "devices": devices,
        "active_device": cfg.get("active_device"),
        "people": cfg.get("people", []),
        "models": reader.list_models(),
        "default_model": reader.DEFAULT_MODEL,
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ping")
def api_ping():
    # Used both by the page and by our own startup check (see main()
    # below) to recognize "yes, this is our app already running" as
    # opposed to some unrelated thing that happens to hold this port.
    return jsonify({"ok": True, "app": "bp-reader"})


@app.route("/api/state")
def api_state():
    try:
        return jsonify({"ok": True, **_state()})
    except Exception as e:
        return _fail(str(e), 500)


@app.route("/api/events")
def api_events():
    """Held open by each page; when the last one closes, the app exits.
    The comment lines are keep-alives -- writing them is also how a closed
    window gets noticed."""
    def stream():
        global _open_pages, _ever_had_page, _last_page_left
        with _pages_lock:
            _open_pages += 1
            _ever_had_page = True
        try:
            yield "retry: 3000\n\n"
            while True:
                yield ": keep-alive\n\n"
                time.sleep(4)
        finally:
            with _pages_lock:
                _open_pages -= 1
                _last_page_left = time.monotonic()
    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _exit_when_window_closed():
    while True:
        time.sleep(2)
        with _pages_lock:
            idle = (_ever_had_page and _open_pages == 0 and _last_page_left is not None
                    and time.monotonic() - _last_page_left > CLOSE_GRACE_S)
        if idle and not _ble_lock.locked():
            print("Window closed -- stopping BP Reader.")
            os._exit(0)


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    def _stop():
        # Finish any Bluetooth operation first, then let the response go out.
        with _ble_lock:
            time.sleep(0.3)
            os._exit(0)
    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/scan")
def api_scan():
    # Live BLE scan, used while adding a monitor.
    if not _ble_lock.acquire(blocking=False):
        return _busy()
    try:
        devices = run_async(reader.scan_devices())
        return jsonify({"ok": True, "devices": devices})
    except Exception as e:
        return _fail(str(e), 500)
    finally:
        _ble_lock.release()


@app.route("/api/pair", methods=["POST"])
def api_pair():
    # One-time step per monitor: writes this computer's pairing key to it,
    # then saves it as a device (with its user-slot -> person mapping).
    data = _json_body()
    mac = str(data.get("mac", "")).strip()
    model = str(data.get("model") or reader.DEFAULT_MODEL).strip().lower()
    if not mac:
        return _fail("Pick your monitor from the scan list first.")
    if model not in {m["id"] for m in reader.list_models()}:
        return _fail("Unknown monitor model.")
    if not _ble_lock.acquire(blocking=False):
        return _busy()
    try:
        result = reader.pair_device(mac, model)
        if result["ok"]:
            users = data.get("users") if isinstance(data.get("users"), dict) else {}
            reader.save_paired_device(mac, model, str(data.get("name") or ""), users)
        return jsonify({**result, **_state()})
    except Exception as e:
        return _fail(str(e), 500)
    finally:
        _ble_lock.release()


@app.route("/api/sync", methods=["POST"])
def api_sync():
    device_id = str(_json_body().get("device_id") or reader.load_config().get("active_device") or "")
    if not reader.get_device(device_id):
        return _fail("Set up a monitor first.")
    if not _ble_lock.acquire(blocking=False):
        return _busy()
    try:
        result = reader.sync_device(device_id)
        return jsonify({**result, "records": reader.load_records(), **_state()})
    except Exception as e:
        return _fail(str(e), 500)
    finally:
        _ble_lock.release()


@app.route("/api/devices/<device_id>/update", methods=["POST"])
def api_device_update(device_id):
    data = _json_body()
    try:
        users = data.get("users") if isinstance(data.get("users"), dict) else None
        reader.update_device(device_id, name=data.get("name"), users=users)
        return jsonify({"ok": True, **_state()})
    except ValueError as e:
        return _fail(str(e))


@app.route("/api/devices/<device_id>/activate", methods=["POST"])
def api_device_activate(device_id):
    try:
        reader.set_active_device(device_id)
        return jsonify({"ok": True, **_state()})
    except ValueError as e:
        return _fail(str(e))


@app.route("/api/devices/<device_id>/remove", methods=["POST"])
def api_device_remove(device_id):
    reader.remove_device(device_id)
    return jsonify({"ok": True, **_state()})


@app.route("/api/people", methods=["POST"])
def api_people_add():
    try:
        person = reader.add_person(_json_body().get("name"))
        return jsonify({"ok": True, "person": person, **_state()})
    except ValueError as e:
        return _fail(str(e))


@app.route("/api/people/<person_id>/rename", methods=["POST"])
def api_people_rename(person_id):
    try:
        reader.rename_person(person_id, _json_body().get("name"))
        return jsonify({"ok": True, **_state()})
    except ValueError as e:
        return _fail(str(e))


@app.route("/api/records")
def api_records():
    # Whatever's already on disk -- used on page load, no BLE involved.
    try:
        return jsonify({"ok": True, "records": reader.load_records()})
    except Exception as e:
        return _fail(str(e), 500)


@app.route("/api/export/<person_id>")
def api_export(person_id):
    # Serves one person's readings as a CSV download. ?averaged=true
    # computes averaged sessions on the fly (see build_averaged_export);
    # the CSV on disk is never touched either way.
    person = next((p for p in reader.get_people() if p["id"] == person_id), None)
    if person is None:
        return "Unknown person", 404
    filename = reader._slug(person["name"])
    path = reader.person_csv_path(person_id)

    if request.args.get("averaged") == "true":
        records = reader.load_records().get(person_id, [])
        if not records:
            return "No readings yet", 404
        rows = reader.build_averaged_export(records)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["datetime", "dia", "sys", "bpm", "mov", "ihb", "readings_averaged"])
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "datetime": r["datetime"], "dia": r["diastolic"], "sys": r["systolic"],
                "bpm": r["pulse"], "mov": 1 if r["movement"] else 0,
                "ihb": 1 if r["irregular_heartbeat"] else 0,
                "readings_averaged": r["readings_averaged"],
            })
        return Response(buf.getvalue(), mimetype="text/csv", headers={
            "Content-Disposition": f"attachment; filename={filename}_averaged.csv"
        })

    if not path.exists():
        return "No readings yet", 404
    return send_file(path, as_attachment=True, download_name=f"{filename}.csv")


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    # Pops the OS's own file manager at the readings folder.
    try:
        reader.open_readings_folder()
        return jsonify({"ok": True})
    except Exception as e:
        return _fail(str(e), 500)


# ---------------------------------------------------------------- window

def _clean_child_environment():
    """Inside the AppImage, PYTHONHOME etc. point at the bundled Python.
    Python has already started, so drop them (and restore the system's own
    library path) before we launch a browser, file manager or bluetoothctl,
    so they don't inherit settings meant only for us."""
    if not os.environ.get("BPREADER_APPIMAGE"):
        return
    for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE"):
        os.environ.pop(k, None)
    host_ld = os.environ.pop("BPREADER_HOST_LD_LIBRARY_PATH", "")
    if host_ld:
        os.environ["LD_LIBRARY_PATH"] = host_ld
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)


# Chromium-based browsers can open a page as a standalone app window
# (--app=URL: its own window and taskbar entry, no tabs or address bar).
# Firefox can't, so it -- or anything else -- gets a normal tab instead.
_APP_BROWSERS = ["google-chrome-stable", "google-chrome", "chromium", "chromium-browser",
                 "brave-browser", "brave", "microsoft-edge-stable", "microsoft-edge",
                 "vivaldi-stable", "vivaldi"]
_APP_BROWSER_FLATPAKS = ["com.google.Chrome", "org.chromium.Chromium", "com.brave.Browser",
                         "com.microsoft.Edge", "io.github.ungoogled_software.ungoogled_chromium",
                         "com.vivaldi.Vivaldi"]


def _find_app_browser():
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                     os.environ.get("LOCALAPPDATA")):
            for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
                if base and os.path.exists(os.path.join(base, rel)):
                    return [os.path.join(base, rel)]
        return None
    for name in _APP_BROWSERS:
        path = shutil.which(name)
        if path:
            return [path]
    if shutil.which("flatpak"):
        try:
            out = subprocess.run(["flatpak", "list", "--app", "--columns=application"],
                                 capture_output=True, text=True, timeout=10).stdout.split()
        except Exception:
            out = []
        for app_id in _APP_BROWSER_FLATPAKS:
            if app_id in out:
                return ["flatpak", "run", app_id]
    return None


def open_window():
    if os.environ.get("BP_READER_NO_BROWSER"):
        return
    cmd = _find_app_browser()
    if cmd:
        try:
            subprocess.Popen(cmd + [f"--app={URL}", "--window-size=980,860"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
        except Exception:
            pass
    try:
        webbrowser.open(URL)
    except Exception:
        pass


def _open_window_soon():
    # Small delay so the server is actually accepting connections first.
    def _open():
        time.sleep(1.0)
        open_window()
    threading.Thread(target=_open, daemon=True).start()


def _already_running():
    """True if our own server is already listening on PORT. Launching the
    app again then just opens another window onto it."""
    try:
        with urllib.request.urlopen(f"{URL}/api/ping", timeout=1) as resp:
            import json
            data = json.loads(resp.read())
            return data.get("app") == "bp-reader"
    except Exception:
        return False


def main():
    _clean_child_environment()
    if _already_running():
        print("BP Reader is already running -- opening a window.")
        open_window()
        return

    try:
        print(f"BP Reader running locally at {URL}")
        print("Nothing here talks to the internet or to Omron's servers.")
        print(f"Your readings live in: {reader.READINGS_DIR}")
        print("Closing the app window stops it.")

        if not os.environ.get("BP_READER_KEEP_RUNNING"):
            threading.Thread(target=_exit_when_window_closed, daemon=True).start()
        _open_window_soon()

        app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
    except Exception:
        # --windowed builds (Windows .exe, no console) would otherwise fail
        # completely silently. Write details to disk and, if possible, pop
        # a visible error dialog instead of just vanishing.
        import traceback
        crash_log = reader.DATA_DIR / "crash.log"
        crash_log.write_text(traceback.format_exc())
        try:
            import tkinter
            import tkinter.messagebox
            root = tkinter.Tk()
            root.withdraw()
            tkinter.messagebox.showerror(
                "BP Reader failed to start",
                f"Something went wrong on startup.\n\nDetails saved to:\n{crash_log}"
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
