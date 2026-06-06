"""Shelly BLU button BLE listener.

Passively scans for BTHome v2 advertisements from Shelly BLU buttons
and dispatches press events to per-(device, event_type) handlers.

No BLE connection needed — just reading broadcast advertisements.

Usage:
    listener = ShellyBluListener()
    listener.on("AA:BB:CC:DD:EE:FF", "single", my_on_handler)
    listener.on("AA:BB:CC:DD:EE:FF", "long", my_off_handler)
    await listener.run()
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import defaultdict
from typing import Awaitable, Callable

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

log = logging.getLogger("shelly_blu")

BTHOME_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"
BTHOME_BUTTON_EVENT = 0x3A
BTHOME_BATTERY = 0x01

# Button event values per BTHome v2
EVENTS = {
    1: "single",
    2: "double",
    3: "triple",
    4: "long",
}

Action = Callable[[], Awaitable[None] | None]


def _decode_bthome(data: bytes) -> tuple[int | None, int | None, int | None]:
    """Decode BTHome v2 service data.

    Returns (packet_id, event_value, battery_pct). Any may be None if not
    present in the frame. Shelly BLU Button1 broadcasts all three on press.

    packet_id increments per unique press — used for deduplication across
    multiple advertisements of the same event.
    """
    if len(data) < 2:
        return None, None, None
    if (data[0] & 0xE0) != 0x40:  # not BTHome v2
        return None, None, None

    packet_id: int | None = None
    event: int | None = None
    battery: int | None = None
    i = 1
    # Walk the TLV stream. Only the object IDs we care about are fully parsed;
    # others are skipped by known payload length.
    SKIP_LEN = {
        # 1-byte payload
        0x02: 1, 0x0F: 1, 0x2E: 1,
        # 2-byte payload
        0x03: 2, 0x04: 2, 0x05: 2, 0x09: 2, 0x0A: 2,
        0x0B: 2, 0x0C: 2, 0x0D: 2, 0x3E: 2,
        # 3-byte payload
        0x06: 3, 0x07: 3, 0x08: 3,
        # 4-byte payload
        0x0E: 4,
    }
    while i < len(data):
        obj_id = data[i]
        i += 1
        if i >= len(data):
            break
        if obj_id == 0x00:
            packet_id = data[i]
            i += 1
        elif obj_id == BTHOME_BATTERY:
            battery = data[i]
            i += 1
        elif obj_id == BTHOME_BUTTON_EVENT:
            event = data[i]
            i += 1
        else:
            i += SKIP_LEN.get(obj_id, 1)
    return packet_id, event, battery


class ShellyBluListener:
    """Listen for Shelly BLU press events and dispatch to per-event handlers."""

    def __init__(self) -> None:
        # mac_upper -> {event_name -> action}
        self._handlers: dict[str, dict[str, Action]] = defaultdict(dict)
        # mac -> last seen packet id (for deduplication)
        self._last_pid: dict[str, int] = {}
        # mac_upper -> last seen battery % (BTHome obj 0x01)
        self._battery: dict[str, int] = {}
        # mac_upper -> epoch when battery was last seen
        self._battery_at: dict[str, float] = {}
        self._scanner: BleakScanner | None = None
        self._stop = asyncio.Event()

    def on(self, mac: str, event: str, action: Action) -> None:
        """Register action for a specific press type.

        event: one of 'single', 'double', 'triple', 'long'.
        """
        if event not in EVENTS.values():
            raise ValueError(f"unknown event {event!r}")
        self._handlers[mac.upper()][event] = action

    def observed_devices(self) -> dict[str, str]:
        """Snapshot of (mac -> last seen name) for any Shelly BLU seen so far."""
        return dict(self._seen)

    def battery_snapshot(self) -> dict[str, tuple[int, float]]:
        """Returns {mac_upper: (battery_pct, epoch_seen)}."""
        return {m: (self._battery[m], self._battery_at[m]) for m in self._battery}

    def _handle(self, device: BLEDevice, ad: AdvertisementData) -> None:
        sd = ad.service_data.get(BTHOME_UUID)
        if sd is None:
            return

        pid, ev, batt = _decode_bthome(sd)

        # Capture battery whenever it appears — fires on every press frame
        # since Shelly BLU runs in trigger_based mode (no idle telemetry).
        mac = device.address.upper()
        if batt is not None:
            self._battery[mac] = batt
            self._battery_at[mac] = time.time()

        if ev is None or ev not in EVENTS:
            return

        # Dedup — same packet id = same physical press
        if pid is not None and self._last_pid.get(mac) == pid:
            return
        if pid is not None:
            self._last_pid[mac] = pid

        event_name = EVENTS[ev]
        self._seen[mac] = device.name or ""

        handlers = self._handlers.get(mac)
        if not handlers:
            log.info("Shelly BLU: unregistered press mac=%s name=%r event=%s pid=%s battery=%s",
                     mac, device.name, event_name, pid,
                     f"{batt}%" if batt is not None else "?")
            return

        action = handlers.get(event_name)
        if action is None:
            log.debug("Shelly BLU: no handler for mac=%s event=%s", mac, event_name)
            return

        batt_str = f" battery={batt}%" if batt is not None else ""
        log.info("Shelly BLU: mac=%s event=%s%s", mac, event_name, batt_str)
        result = action()
        if inspect.iscoroutine(result):
            asyncio.get_event_loop().create_task(result)

    async def run(self) -> None:
        self._seen: dict[str, str] = {}
        self._scanner = BleakScanner(detection_callback=self._handle)
        await self._scanner.start()
        log.info("Shelly BLU listener started")
        try:
            await self._stop.wait()
        finally:
            await self._scanner.stop()
            log.info("Shelly BLU listener stopped")

    def stop(self) -> None:
        self._stop.set()
