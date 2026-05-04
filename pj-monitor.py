#!/usr/bin/env python3
"""Monitor projector state and control fossil-app.service.

Watchdog model: app is ALWAYS running by default.
Only a confirmed, active OFF signal from the projector stops the app.
The moment that signal is lost (unreachable, error, IP change),
the app returns to running.

Runs as pj-monitor.service (systemd), independent of X session.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "pj-config.json")
CERT_DIR = os.path.join(SCRIPT_DIR, ".pj-certs")
POLL_INTERVAL = 15  # seconds

# Which projector to monitor (app lifecycle follows this projector)
MONITOR_PJ = "fossil-pj"

# Pushover config
PO_TOKEN = "avmrkpiuza87mcofkwn98s5prd2ukn"
PO_USER = "umhhs2kiz34wq91k76a1skjrq6vft5"


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def pushover(title, msg):
    try:
        subprocess.run(
            [
                "curl", "-fsS", "-m", "10", "-X", "POST",
                "https://api.pushover.net/1/messages.json",
                "--data-urlencode", f"token={PO_TOKEN}",
                "--data-urlencode", f"user={PO_USER}",
                "--data-urlencode", f"title={title}",
                "--data-urlencode", f"message={msg}",
                "--data-urlencode", "priority=0",
            ],
            capture_output=True, timeout=15,
        )
    except Exception:
        pass


def service_active():
    result = subprocess.run(
        ["systemctl", "is-active", "fossil-app.service"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() == "active"


def adb_select_input(ip, input_id):
    """Force projector input to NUC's HDMI port. Otherwise the PJ resumes
    whatever app was last on screen (Netflix, Disney+, etc.) and never
    shows the NUC."""
    from urllib.parse import quote
    uri = f"content://android.media.tv/passthrough/{quote(input_id, safe='')}"
    try:
        subprocess.run(["adb", "connect", f"{ip}:5555"],
                       capture_output=True, timeout=5)
        subprocess.run(
            ["adb", "-s", f"{ip}:5555", "shell", "am", "start",
             "-a", "android.intent.action.VIEW", "-d", uri],
            capture_output=True, timeout=10,
        )
    except Exception as e:
        print(f"select-input failed: {e}", flush=True)


def service_start():
    subprocess.run(
        ["sudo", "systemctl", "start", "fossil-app.service"],
        capture_output=True,
    )


def service_stop():
    subprocess.run(
        ["sudo", "systemctl", "stop", "fossil-app.service"],
        capture_output=True,
    )


def ensure_running():
    """App should be running — start it if it's not."""
    if not service_active():
        service_start()
        return True
    return False


def ensure_stopped():
    """App should be stopped — stop it if it's running."""
    if service_active():
        service_stop()
        return True
    return False


async def monitor():
    from androidtvremote2 import AndroidTVRemote
    from androidtvremote2.exceptions import CannotConnect, ConnectionClosed

    config = load_config()
    if MONITOR_PJ not in config:
        print(f"Projector '{MONITOR_PJ}' not in pj-config.json", flush=True)
        sys.exit(1)

    ip = config[MONITOR_PJ]["ip"]
    input_id = config[MONITOR_PJ].get("hdmi_input_id")
    certfile = os.path.join(CERT_DIR, f"{MONITOR_PJ}-cert.pem")
    keyfile = os.path.join(CERT_DIR, f"{MONITOR_PJ}-key.pem")
    print(f"Monitoring {MONITOR_PJ} ({ip}) every {POLL_INTERVAL}s", flush=True)

    def pj_is_on():
        """PJ confirmed on: ensure app running and force input to NUC."""
        ensure_running()
        if input_id:
            adb_select_input(ip, input_id)

    # App always starts running
    ensure_running()
    print("App started (default state)", flush=True)

    last_confirmed_off = False
    notified_unreachable = False
    remote = None

    def on_is_on_updated(is_on):
        """Instant callback when PJ power state changes."""
        nonlocal last_confirmed_off, notified_unreachable
        if is_on:
            if last_confirmed_off:
                print(f"{MONITOR_PJ}: ON (callback) — starting app", flush=True)
                pushover("Fossil PJ Monitor", f"{MONITOR_PJ} ON — app started")
            last_confirmed_off = False
            notified_unreachable = False
            pj_is_on()
        else:
            if not last_confirmed_off:
                print(f"{MONITOR_PJ}: OFF (callback) — stopping app", flush=True)
                pushover("Fossil PJ Monitor", f"{MONITOR_PJ} OFF — app stopped")
            last_confirmed_off = True
            notified_unreachable = False
            ensure_stopped()

    while True:
        # Maintain connection — reconnect if lost
        if remote is None:
            try:
                remote = AndroidTVRemote("Fossil NUC", certfile, keyfile, ip)
                remote.add_is_on_updated_callback(on_is_on_updated)
                await remote.async_connect()
                await asyncio.sleep(1)
                # Read initial state
                if remote._transport and not remote._transport.is_closing():
                    power = remote.is_on
                    if power is True:
                        if last_confirmed_off:
                            print(f"{MONITOR_PJ}: ON — starting app", flush=True)
                            pushover("Fossil PJ Monitor", f"{MONITOR_PJ} ON — app started")
                        last_confirmed_off = False
                        pj_is_on()
                    elif power is False:
                        if not last_confirmed_off:
                            print(f"{MONITOR_PJ}: OFF — stopping app", flush=True)
                            pushover("Fossil PJ Monitor", f"{MONITOR_PJ} OFF — app stopped")
                        last_confirmed_off = True
                        ensure_stopped()
                    notified_unreachable = False
                else:
                    remote = None
            except Exception:
                remote = None
        else:
            # Check connection still alive
            if not remote._transport or remote._transport.is_closing():
                print(f"{MONITOR_PJ}: connection lost", flush=True)
                remote = None

        # If no connection, decide based on last known state.
        if remote is None:
            if last_confirmed_off:
                # PJ was just confirmed OFF (e.g. user pressed power, or PJ
                # auto-slept). Going unreachable from OFF is expected — the
                # device drops its TCP socket as part of deeper standby.
                # Keep the app stopped and stay quiet (no Pushover, no log
                # spam — only one "unreachable" line per disconnect).
                if not notified_unreachable:
                    print(f"{MONITOR_PJ}: unreachable (was OFF — staying stopped)", flush=True)
                    notified_unreachable = True
                ensure_stopped()
            else:
                # Last seen ON or unknown — watchdog: app should run.
                if not notified_unreachable:
                    print(f"{MONITOR_PJ}: unreachable", flush=True)
                    pushover("Fossil PJ Monitor", f"Cannot reach {MONITOR_PJ} ({ip})")
                    notified_unreachable = True
                ensure_running()

        await asyncio.sleep(POLL_INTERVAL)


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    asyncio.run(monitor())


if __name__ == "__main__":
    main()
