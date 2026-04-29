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
from pathlib import Path

from shelly_blu import ShellyBluListener

log = logging.getLogger("button-monitor")

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "pj-config.json"
SECRETS_FILE = SCRIPT_DIR / "secrets.env"
KASA_OUTLET = "Kasa 4"

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
    run(["sudo", "systemctl", "start", "fossil-app.service"])
    run([
        sys.executable, str(SCRIPT_DIR / "pj-control.py"),
        "fossil-pj", "on",
    ])


def fossil_off() -> None:
    log.info("Fossil OFF")
    pushover("Fossil Button", "Fossil OFF")
    run(["sudo", "systemctl", "stop", "fossil-app.service"])
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


async def _kasa() -> "KasaClient":
    global _kasa_client
    if _kasa_client is None:
        from kasa_adapter import KasaClient
        _kasa_client = KasaClient()
        await _kasa_client.discover()
    return _kasa_client


async def bloom_on() -> None:
    log.info("Bloom ON")
    pushover("Bloom Button", "Light Bloom ON")
    try:
        client = await _kasa()
        await client.on(KASA_OUTLET)
    except Exception as e:
        log.error("bloom_on failed: %s", e)
        pushover("Bloom Button", f"Bloom ON failed: {e}")


async def bloom_off() -> None:
    log.info("Bloom OFF")
    pushover("Bloom Button", "Light Bloom OFF")
    try:
        client = await _kasa()
        await client.off(KASA_OUTLET)
    except Exception as e:
        log.error("bloom_off failed: %s", e)
        pushover("Bloom Button", f"Bloom OFF failed: {e}")


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

    await listener.run()


if __name__ == "__main__":
    asyncio.run(main())
