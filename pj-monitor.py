#!/usr/bin/env python3
"""Monitor projector state and control fossil-app.service.

Single authority for fossil app lifecycle:
- PJ responds ON  → start fossil-app.service
- PJ responds OFF → stop fossil-app.service
- PJ unreachable  → no change, alert after 3 failures
- App crashed while PJ ON → restart fossil-app.service

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

# Which projector to monitor — change when fossil-pj is available
MONITOR_PJ = "haste-pj"

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


async def get_pj_power(name, ip):
    """Query projector power state. Returns True/False/None on error."""
    from androidtvremote2 import AndroidTVRemote
    from androidtvremote2.exceptions import CannotConnect, ConnectionClosed

    certfile = os.path.join(CERT_DIR, f"{name}-cert.pem")
    keyfile = os.path.join(CERT_DIR, f"{name}-key.pem")
    if not os.path.exists(certfile):
        return None
    try:
        remote = AndroidTVRemote("Fossil NUC", certfile, keyfile, ip)
        await remote.async_connect()
        power = remote.is_on
        remote.disconnect()
        return power
    except (CannotConnect, ConnectionClosed, OSError):
        return None


async def monitor():
    config = load_config()
    if MONITOR_PJ not in config:
        print(f"Projector '{MONITOR_PJ}' not in pj-config.json", flush=True)
        sys.exit(1)

    ip = config[MONITOR_PJ]["ip"]
    print(f"Monitoring {MONITOR_PJ} ({ip}) every {POLL_INTERVAL}s", flush=True)

    last_state = None
    conn_failures = 0

    while True:
        power = await get_pj_power(MONITOR_PJ, ip)

        if power is None:
            conn_failures += 1
            if conn_failures == 3:
                print(f"{MONITOR_PJ}: unreachable — app unchanged", flush=True)
                pushover("Fossil PJ Monitor", f"Cannot reach {MONITOR_PJ} ({ip})")
            # Never change app state on connection failure
            await asyncio.sleep(POLL_INTERVAL)
            continue

        conn_failures = 0

        if power != last_state:
            if power:
                # Projector ON → start app
                print(f"{MONITOR_PJ}: ON — starting app", flush=True)
                if not service_active():
                    service_start()
                pushover("Fossil PJ Monitor", f"{MONITOR_PJ} ON — app started")
            else:
                # Projector OFF → stop app
                print(f"{MONITOR_PJ}: OFF — stopping app", flush=True)
                if service_active():
                    service_stop()
                pushover("Fossil PJ Monitor", f"{MONITOR_PJ} OFF — app stopped")
            last_state = power
        elif power and not service_active():
            # PJ is on but app crashed — restart it
            print(f"{MONITOR_PJ}: app crashed — restarting", flush=True)
            service_start()
            pushover("Fossil PJ Monitor", "App crashed — restarting")

        await asyncio.sleep(POLL_INTERVAL)


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    asyncio.run(monitor())


if __name__ == "__main__":
    main()
