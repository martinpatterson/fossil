"""TP-Link Kasa adapter with KLAP v2 transport workaround.

python-kasa 0.10.2 misassigns KlapTransport (v1/MD5) to IOT smart-plugs
that need KlapTransportV2 (SHA). This wrapper detects those devices
during discovery and rebuilds them with the correct transport.

See: python-kasa issue #1648.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable

from kasa import Credentials, Device, DeviceConfig, Discover
from kasa.deviceconfig import DeviceEncryptionType, DeviceFamily
from kasa.iot.iotplug import IotPlug
from kasa.protocols.iotprotocol import IotProtocol
from kasa.transports.klaptransport import KlapTransportV2

log = logging.getLogger("kasa_adapter")


def _needs_klap_v2(dev: Device) -> bool:
    ct = dev.config.connection_type
    return (
        ct.encryption_type == DeviceEncryptionType.Klap
        and getattr(ct, "login_version", None) == 2
        and ct.device_family == DeviceFamily.IotSmartPlugSwitch
    )


def _rebuild_with_klap_v2(dev: Device, creds: Credentials | None) -> Device:
    cfg = DeviceConfig(
        host=dev.host,
        credentials=creds,
        connection_type=dev.config.connection_type,
    )
    protocol = IotProtocol(transport=KlapTransportV2(config=cfg))
    return IotPlug(host=dev.host, config=cfg, protocol=protocol)


class KasaClient:
    """Discovers Kasa devices on the LAN and controls them by alias.

    Requires TP-Link cloud credentials for newer KLAP devices. Pass
    directly, or set KASA_USERNAME / KASA_PASSWORD env vars.
    """

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        discovery_timeout: float = 5.0,
    ) -> None:
        self._username = username or os.environ.get("KASA_USERNAME")
        self._password = password or os.environ.get("KASA_PASSWORD")
        self._discovery_timeout = discovery_timeout
        self._devices: dict[str, Device] = {}  # alias -> Device

    def _creds(self) -> Credentials | None:
        if self._username and self._password:
            return Credentials(username=self._username, password=self._password)
        return None

    async def discover(self) -> None:
        """Scan LAN and connect to all reachable Kasa devices."""
        creds = self._creds()
        found = await Discover.discover(
            discovery_timeout=int(self._discovery_timeout),
            credentials=creds,
        )
        self._devices.clear()
        for host, dev in found.items():
            if _needs_klap_v2(dev):
                log.info("Kasa %s: applying KLAP v2 workaround", host)
                dev = _rebuild_with_klap_v2(dev, creds)
            try:
                await dev.update()
                alias = dev.alias or host
                self._devices[alias] = dev
                log.info("Kasa connected: alias=%r host=%s on=%s", alias, host, dev.is_on)
            except Exception as e:
                log.warning("Kasa update failed for %s: %s", host, e)

    def aliases(self) -> Iterable[str]:
        return self._devices.keys()

    async def _get(self, alias: str) -> Device:
        if alias not in self._devices:
            # Re-discover in case the device was offline at startup
            await self.discover()
        if alias not in self._devices:
            raise KeyError(f"Kasa outlet not found: {alias!r}")
        return self._devices[alias]

    async def on(self, alias: str) -> None:
        dev = await self._get(alias)
        await dev.turn_on()
        log.info("Kasa %s -> ON", alias)

    async def off(self, alias: str) -> None:
        dev = await self._get(alias)
        await dev.turn_off()
        log.info("Kasa %s -> OFF", alias)

    async def toggle(self, alias: str) -> None:
        dev = await self._get(alias)
        await dev.update()
        if dev.is_on:
            await dev.turn_off()
            log.info("Kasa %s -> OFF", alias)
        else:
            await dev.turn_on()
            log.info("Kasa %s -> ON", alias)

    async def is_on(self, alias: str) -> bool:
        dev = await self._get(alias)
        await dev.update()
        return bool(dev.is_on)
