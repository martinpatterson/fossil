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
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "pj-config.json")
CERT_DIR = os.path.join(SCRIPT_DIR, ".pj-certs")
POLL_INTERVAL = 15  # seconds

# Coordination marker with button-monitor. When the file exists with a
# timestamp newer than USER_OFF_TTL ago, the user pressed Fossil OFF and
# fossil-app was stopped immediately for visual feedback. Suppress all
# auto-restart paths until the marker expires or is cleared on a Fossil
# ON press. TTL > observed worst-case is_on settle lag (~31s) so any
# reconnect during shutdown that sees lagged is_on=True does NOT restart.
USER_OFF_MARKER = "/tmp/fossil-app-user-off"
USER_OFF_TTL = 90  # seconds

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


def _user_off_active() -> bool:
    """True if a recent Fossil OFF press marked user intent to keep the
    app stopped. Suppresses auto-restart paths during the projector's
    is_on settling lag."""
    try:
        with open(USER_OFF_MARKER) as f:
            return time.time() - float(f.read().strip()) < USER_OFF_TTL
    except (FileNotFoundError, ValueError):
        return False


def ensure_running():
    """App should be running — start it if it's not.

    Respects the user-OFF marker: if the user pressed Fossil OFF within
    USER_OFF_TTL seconds, do NOT start the app even if the projector
    still reports ON. This prevents pj-monitor from fighting the user's
    intent during the projector's is_on settling lag.

    Returns True iff this call actually started the service.
    """
    if _user_off_active():
        print("ensure_running: skipped (user-OFF marker active)", flush=True)
        return False
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
    adb_disabled = bool(config[MONITOR_PJ].get("adb_disabled"))
    certfile = os.path.join(CERT_DIR, f"{MONITOR_PJ}-cert.pem")
    keyfile = os.path.join(CERT_DIR, f"{MONITOR_PJ}-key.pem")
    print(f"Monitoring {MONITOR_PJ} ({ip}) every {POLL_INTERVAL}s"
          f"{' (adb_disabled)' if adb_disabled else ''}", flush=True)

    def pj_is_on():
        """PJ confirmed on: ensure app running and (when ADB is available)
        force input to NUC. When adb_disabled is set, skip the ADB step
        entirely — the auth dialog is hung up and `adb shell` blocks on
        the timeout, visibly slowing PJ wake. Hisense self-selects the
        correct HDMI input on wake anyway, so skipping is safe."""
        ensure_running()
        if input_id and not adb_disabled:
            adb_select_input(ip, input_id)

    # App always starts running — unless a recent user-OFF marker is in
    # effect (e.g. the user pressed Fossil OFF just before pj-monitor
    # restarted, and we'd otherwise re-start the app against their intent).
    if _user_off_active():
        print("Init: skipping default app start — user-OFF marker active", flush=True)
    else:
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
                if _user_off_active():
                    print(f"{MONITOR_PJ}: ON (callback) — start suppressed (user-OFF active)", flush=True)
                else:
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
                            if _user_off_active():
                                print(f"{MONITOR_PJ}: ON — start suppressed (user-OFF active)", flush=True)
                            else:
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
