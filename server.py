"""
Local-only web server for downloading readings from an Omron BP7455CAN
(and similar 10-Series/HEM-7342T) blood pressure monitor over Bluetooth LE.

Everything stays on this machine: no accounts, no cloud sync, no telemetry.
Run this, open http://127.0.0.1:5757 in your browser.
"""
import asyncio
import csv
import io
import os
import threading
import urllib.request
import webbrowser

from flask import Flask, jsonify, request, send_file, render_template, Response

import omron_reader as reader

app = Flask(__name__, template_folder=str(reader.resource_path("templates")))

PORT = 5757

# Only these Host headers are accepted. A page on any other website can
# point its own domain at 127.0.0.1 ("DNS rebinding") and then read
# /api/records -- i.e. your blood-pressure history -- from this server.
# Checking Host stops that, since the browser still sends the attacker's
# hostname.
_ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
_ALLOWED_ORIGINS = {f"http://{h}" for h in _ALLOWED_HOSTS}

# omblepy is driven by swapping the process-wide sys.argv and cwd (see
# omron_reader._run_omblepy), and the monitor only talks to one client at a
# time anyway. Flask serves requests on multiple threads, so a double-click
# on Sync, or Scan during a Sync, could interleave and corrupt both runs.
_ble_lock = threading.Lock()


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


@app.route("/")
def index():
    # The saved MAC (from a previous pairing) gets pre-filled in the page
    # so you don't have to re-scan every time you open the app.
    cfg = reader.load_config()
    return render_template("index.html", saved_mac=cfg.get("mac", ""))


@app.route("/api/ping")
def api_ping():
    # Used both by the browser and by our own startup check (see main()
    # below) to recognize "yes, this is our app already running" as
    # opposed to some unrelated thing that happens to hold this port.
    return jsonify({"ok": True, "app": "bp-reader"})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    def _stop():
        import time
        time.sleep(0.3)  # let the HTTP response actually get sent first
        os._exit(0)
    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/scan")
def api_scan():
    # Live BLE scan, triggered from the page's "Scan for device" button.
    if not _ble_lock.acquire(blocking=False):
        return _busy()
    try:
        devices = run_async(reader.scan_devices())
        return jsonify({"ok": True, "devices": devices})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        _ble_lock.release()


@app.route("/api/pair", methods=["POST"])
def api_pair():
    # One-time step: writes our own pairing key to the monitor. Only
    # needed again if you re-pair with a different app (e.g. Omron Connect).
    mac = str(_json_body().get("mac", "")).strip()
    if not mac:
        return jsonify({"ok": False, "error": "No MAC address provided"}), 400
    if not _ble_lock.acquire(blocking=False):
        return _busy()
    try:
        # pair_device() saves the MAC to config itself on success.
        return jsonify(reader.pair_device(mac))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        _ble_lock.release()


@app.route("/api/sync", methods=["POST"])
def api_sync():
    data = _json_body()
    mac = str(data.get("mac", "")).strip()
    new_only = bool(data.get("new_only", False))
    time_sync = bool(data.get("time_sync", False))
    if not mac:
        return jsonify({"ok": False, "error": "No MAC address provided"}), 400
    if not _ble_lock.acquire(blocking=False):
        return _busy()
    try:
        # Snapshot what's on disk *before* syncing, so we can tell the
        # difference between "just downloaded" and "already had this" --
        # the page uses that to highlight new readings.
        before = reader.load_records()
        before_ids = {u: {r["datetime"] for r in recs} for u, recs in before.items()}
        result = reader.sync_device(mac, new_only=new_only, time_sync=time_sync)
        records = reader.load_records() if result["ok"] else {}
        new_datetimes = {
            u: [r["datetime"] for r in recs if r["datetime"] not in before_ids.get(u, set())]
            for u, recs in records.items()
        }
        return jsonify({**result, "records": records, "new_datetimes": new_datetimes})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        _ble_lock.release()


@app.route("/api/records")
def api_records():
    # Whatever's already on disk -- used on page load, no BLE involved.
    try:
        return jsonify({"ok": True, "records": reader.load_records()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/export/<int:user_idx>")
def api_export(user_idx):
    # Serves readings as a CSV download. ?averaged=true computes averaged
    # sessions on the fly (see build_averaged_export) and streams that
    # straight from memory -- the raw per-user CSV on disk is never
    # touched either way.
    if user_idx not in (1, 2):
        return "Invalid user", 400

    if request.args.get("averaged") == "true":
        records = reader.load_records().get(f"user{user_idx}", [])
        if not records:
            return "No data synced yet", 404
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
            "Content-Disposition": f"attachment; filename=user{user_idx}_averaged.csv"
        })

    paths = reader.csv_paths()
    path = paths[user_idx - 1]
    if not path.exists():
        return "No data synced yet", 404
    return send_file(path, as_attachment=True, download_name=path.name)


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    # "Open readings folder" button -- pops the OS's own file manager
    # rather than us trying to build a file browser into the page.
    try:
        reader.open_readings_folder()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _already_running():
    """True if our own server is already listening on PORT. Used so that
    double-clicking the app icon a second time -- after you closed the
    browser tab but left the app running in the background, which is the
    normal state for a local server -- just reopens the browser instead of
    silently failing to bind an already-taken port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=1) as resp:
            import json
            data = json.loads(resp.read())
            return data.get("app") == "bp-reader"
    except Exception:
        return False


def _open_browser_soon():
    # Small delay so the Flask server is actually accepting connections
    # before the browser tries to load the page.
    def _open():
        import time
        time.sleep(1.0)
        try:
            webbrowser.open(f"http://127.0.0.1:{PORT}")
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


def main():
    if _already_running():
        # Nothing new to start -- an instance is already up (likely because
        # the browser tab got closed, not the app itself). Just reopen the
        # page in the browser and exit; there's only ever one server.
        print("BP Reader is already running -- reopening the browser tab.")
        if not os.environ.get("BP_READER_NO_BROWSER"):
            try:
                webbrowser.open(f"http://127.0.0.1:{PORT}")
            except Exception:
                pass
        return

    try:
        print(f"Omron reader running locally at http://127.0.0.1:{PORT}")
        print("Nothing here talks to the internet or to Omron's servers.")
        print(f"Your readings live in: {reader.READINGS_DIR}")
        print("Closing the browser tab does NOT stop this app -- it keeps")
        print("running in the background so you can reopen it any time by")
        print("launching it again. Use the Quit button in the page, or")
        print("Ctrl+C here, to actually stop it.")

        # When launched by double-clicking (no terminal attached), open the
        # browser automatically. Skip if BP_READER_NO_BROWSER is set (handy
        # for dev/debugging) so it doesn't pop a tab every reload.
        if not os.environ.get("BP_READER_NO_BROWSER"):
            _open_browser_soon()

        app.run(host="127.0.0.1", port=PORT, debug=False)
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
