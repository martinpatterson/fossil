#!/usr/bin/env python3
"""Scan for Shelly BLU button presses and print MAC addresses.

Use this to identify which MAC belongs to which colored button.
Press each button a few times — the MAC, name, and event type are logged.

Usage:
    python3 button-scan.py
"""
import asyncio
import logging

from shelly_blu import ShellyBluListener


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print("Press buttons now. Ctrl-C to stop.\n", flush=True)
    print(f"{'MAC':20s} {'NAME':25s} EVENT")
    print("-" * 60)

    listener = ShellyBluListener()
    # Register a catch-all by monkey-patching the dispatcher — we just log
    # every press regardless of MAC.
    original_handle = listener._handle

    def catchall(device, ad):
        from shelly_blu import BTHOME_UUID, _decode_bthome, EVENTS
        sd = ad.service_data.get(BTHOME_UUID)
        if sd is None:
            return
        pid, ev = _decode_bthome(sd)
        if ev is None or ev not in EVENTS:
            return
        mac = device.address.upper()
        if listener._last_pid.get(mac) == pid:
            return
        if pid is not None:
            listener._last_pid[mac] = pid
        print(f"{mac:20s} {device.name or '':25s} {EVENTS[ev]}", flush=True)

    listener._handle = catchall

    try:
        asyncio.run(listener.run())
    except KeyboardInterrupt:
        listener.stop()
        print("\nStopped.")


if __name__ == "__main__":
    main()
