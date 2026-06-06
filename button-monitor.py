#!/usr/bin/env python3
"""Shelly BLU button dispatcher.

Reads button MAC mapping from pj-config.json and dispatches:
  single/double/triple press -> ON
  long press                  -> OFF

Targets:
  fossil  -> fossil-app.service (systemctl) + pj-control fossil-pj
  haste   -> pj-control haste-pj
  bloom   -> Kasa outlet "Kasa 4"

If no button MACs are configured, service exits cleanly (not an error).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from shelly_blu import ShellyBluListener

log = logging.getLogger("button-monitor")

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "pj-config.json"
SECRETS_FILE = SCRIPT_DIR / "secrets.env"

# Bloom button now toggles both exhibit lighting (Kasa 5) and the Bloom
# fixture (Kasa 4). Staff are standing next to the lights when pressing,
# so light first, then bloom.
BLOOM_OUTLETS = ("Kasa 5", "Kasa 4")
KASA_OUTLET = BLOOM_OUTLETS[-1]  # back-compat for any older references

# Shelly BLU low-battery notification settings.
LOW_BATTERY_THRESHOLD = 50  # percent
BATTERY_STATE_PATH = "/tmp/shelly-battery.json"
BATTERY_POLL_SEC = 60  # how often to refresh the state file

# Coordination marker with pj-monitor. When set, pj-monitor will NOT
# auto-restart fossil-app even if it sees the projector reporting ON.
# Lets us stop fossil-app immediately on Fossil OFF press for instant
# visual feedback (NUC outputs black), and prevents pj-monitor from
# fighting that decision during the projector's is_on settling lag.
USER_OFF_MARKER = "/tmp/fossil-app-user-off"

PO_TOKEN = "avmrkpiuza87mcofkwn98s5prd2ukn"
PO_USER = "umhhs2kiz34wq91k76a1skjrq6vft5"


def pushover(title: str, msg: str) -> None:
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


def load_secrets() -> None:
    if not SECRETS_FILE.exists():
        return
    for line in SECRETS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, capture_output=True, timeout=15)
    except Exception as e:
        log.warning("run %s failed: %s", cmd, e)


# --- Fossil actions ---

def fossil_on() -> None:
    log.info("Fossil ON")
    pushover("Fossil Button", "Fossil ON")
    # Clear any prior user-OFF marker so pj-monitor can resume normal
    # behavior. Then explicitly start fossil-app — symmetric with
    # fossil_off()'s explicit stop. pj-monitor only auto-starts on PJ
    # state TRANSITIONS, so if the projector is already ON (toggle
    # bug, retry press, accidental ON) it would never fire. Starting
    # here is idempotent.
    try:
        os.remove(USER_OFF_MARKER)
    except FileNotFoundError:
        pass
    subprocess.run(["sudo", "systemctl", "start", "fossil-app.service"],
                   capture_output=True, timeout=5)
    run([
        sys.executable, str(SCRIPT_DIR / "pj-control.py"),
        "fossil-pj", "on",
    ])


def fossil_off() -> None:
    log.info("Fossil OFF")
    pushover("Fossil Button", "Fossil OFF")
    # Write the marker BEFORE stopping the service so pj-monitor's
    # poll (15 s) can't race us and restart the app between stop and
    # marker-write.
    try:
        with open(USER_OFF_MARKER, "w") as f:
            f.write(str(time.time()))
    except OSError as e:
        log.warning("could not write %s: %s", USER_OFF_MARKER, e)
    # Stop fossil-app immediately for instant user-visible feedback
    # (NUC outputs black). Decoupled from the projector's slow
    # is_on lag so staff don't perceive the OFF as a no-op.
    subprocess.run(["sudo", "systemctl", "stop", "fossil-app.service"],
                   capture_output=True, timeout=5)
    run([
        sys.executable, str(SCRIPT_DIR / "pj-control.py"),
        "fossil-pj", "off",
    ])


# --- Haste actions ---

def haste_on() -> None:
    log.info("Haste ON")
    pushover("Haste Button", "Haste ON")
    run([
        sys.executable, str(SCRIPT_DIR / "pj-control.py"),
        "haste-pj", "on",
    ])


def haste_off() -> None:
    log.info("Haste OFF")
    pushover("Haste Button", "Haste OFF")
    run([
        sys.executable, str(SCRIPT_DIR / "pj-control.py"),
        "haste-pj", "off",
    ])


# --- Light bloom (Kasa) actions ---

_kasa_client = None


async def _kasa():
    """TP-Link cloud control. Works on isolated/guest WiFi where LAN
    discovery is blocked — the device maintains its own connection to
    TP-Link cloud, so commands route through there."""
    global _kasa_client
    if _kasa_client is None:
        from kasa_cloud import KasaCloud
        _kasa_client = KasaCloud()
        await _kasa_client.login()
    return _kasa_client


async def bloom_on() -> None:
    log.info("Bloom ON")
    pushover("Bloom Button", "Light Bloom ON")
    try:
        client = await _kasa()
    except Exception as e:
        log.error("bloom_on login failed: %s", e)
        pushover("Bloom Button", f"Bloom login failed: {e}")
        return
    # Per-outlet error isolation: if one fails, the other still toggles.
    for outlet in BLOOM_OUTLETS:
        try:
            await client.on(outlet)
        except Exception as e:
            log.error("bloom_on %s failed: %s", outlet, e)
            pushover("Bloom Button", f"{outlet} ON failed: {e}")


async def bloom_off() -> None:
    log.info("Bloom OFF")
    pushover("Bloom Button", "Light Bloom OFF")
    try:
        client = await _kasa()
    except Exception as e:
        log.error("bloom_off login failed: %s", e)
        pushover("Bloom Button", f"Bloom login failed: {e}")
        return
    for outlet in BLOOM_OUTLETS:
        try:
            await client.off(outlet)
        except Exception as e:
            log.error("bloom_off %s failed: %s", outlet, e)
            pushover("Bloom Button", f"{outlet} OFF failed: {e}")


# --- Wiring ---

ROLES = {
    "fossil": (fossil_on, fossil_off),
    "haste": (haste_on, haste_off),
    "bloom": (bloom_on, bloom_off),
}


def wire(listener: ShellyBluListener, buttons: dict[str, str]) -> int:
    """Register handlers; returns number of buttons wired."""
    count = 0
    for role, mac in buttons.items():
        if not mac or mac.upper().startswith("AA:BB"):  # placeholder / unset
            log.info("Button %r: MAC not configured, skipping", role)
            continue
        if role not in ROLES:
            log.warning("Button %r: unknown role, skipping", role)
            continue
        on_action, off_action = ROLES[role]
        listener.on(mac, "single", on_action)
        for event in ("double", "triple", "long"):
            listener.on(mac, event, off_action)
        log.info("Button %r -> %s", role, mac.upper())
        count += 1
    return count


# --- Battery monitoring ---

# mac -> battery_pct at the last time we Pushovered about it. Used so we
# only notify once per "low-battery session" — a new Pushover only fires
# after the device recovers above the threshold (replaced batteries).
_battery_notified: dict[str, int] = {}


def _maybe_battery_pushover(role: str, mac: str, batt: int) -> None:
    if batt >= LOW_BATTERY_THRESHOLD:
        # Healthy — clear any prior notification so a future drop re-fires.
        _battery_notified.pop(mac, None)
        return
    if mac in _battery_notified:
        return  # already notified for this low-battery session
    pushover("Shelly BLU Battery",
             f"{role} button low battery: {batt}% (replace CR2032)")
    log.info("Low-battery Pushover sent for %s (%s): %d%%", role, mac, batt)
    _battery_notified[mac] = batt


def _publish_battery_state(snapshot: dict[str, tuple[int, float]],
                           buttons: dict[str, str]) -> None:
    """Write role-keyed battery JSON for the dashboard to consume."""
    out: dict[str, dict] = {}
    for role, mac in buttons.items():
        m = mac.upper()
        if m in snapshot:
            batt, at = snapshot[m]
            out[role] = {"battery": batt, "at": at, "mac": m}
    try:
        with open(BATTERY_STATE_PATH, "w") as f:
            json.dump(out, f)
    except OSError as e:
        log.warning("could not write %s: %s", BATTERY_STATE_PATH, e)


async def battery_monitor_loop(listener: ShellyBluListener,
                               buttons: dict[str, str]) -> None:
    """Periodically refresh /tmp/shelly-battery.json and fire low-battery
    Pushovers. Shelly BLU only broadcasts on press, so battery updates
    are driven by user activity — this loop's job is to publish state and
    apply the low-battery threshold check on a steady cadence."""
    while True:
        try:
            snapshot = listener.battery_snapshot()
            _publish_battery_state(snapshot, buttons)
            for role, mac in buttons.items():
                m = mac.upper()
                if m in snapshot:
                    batt, _at = snapshot[m]
                    _maybe_battery_pushover(role, m, batt)
        except Exception as e:
            log.warning("battery_monitor_loop iteration failed: %s", e)
        await asyncio.sleep(BATTERY_POLL_SEC)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    load_secrets()

    cfg = json.loads(CONFIG_FILE.read_text())
    buttons = cfg.get("buttons", {})

    listener = ShellyBluListener()
    wired = wire(listener, buttons)

    if wired == 0:
        log.info("No buttons configured. Exiting.")
        return

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, listener.stop)

    asyncio.create_task(battery_monitor_loop(listener, buttons))

    await listener.run()


if __name__ == "__main__":
    asyncio.run(main())
