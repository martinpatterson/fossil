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
    certfile = os.path.join(CERT_DIR, f"{MONITOR_PJ}-cert.pem")
    keyfile = os.path.join(CERT_DIR, f"{MONITOR_PJ}-key.pem")
    print(f"Monitoring {MONITOR_PJ} ({ip}) every {POLL_INTERVAL}s", flush=True)

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
            ensure_running()
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
                        ensure_running()
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

        # If no connection, treat as unreachable — watchdog: app runs
        if remote is None:
            if not notified_unreachable:
                print(f"{MONITOR_PJ}: unreachable", flush=True)
                pushover("Fossil PJ Monitor", f"Cannot reach {MONITOR_PJ} ({ip})")
                notified_unreachable = True
            if last_confirmed_off:
                print(f"{MONITOR_PJ}: lost OFF signal — starting app", flush=True)
                pushover("Fossil PJ Monitor", f"Lost {MONITOR_PJ} — app started")
                last_confirmed_off = False
            ensure_running()

        await asyncio.sleep(POLL_INTERVAL)


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    asyncio.run(monitor())


if __name__ == "__main__":
    main()
