"""TP-Link Kasa cloud control (works around client-isolated WiFi).

When the LAN is isolated (guest WiFi), python-kasa can't discover or
reach Kasa devices directly. This module talks to TP-Link's cloud
endpoint and uses passthrough commands — the device itself maintains a
persistent connection to TP-Link cloud, so commands route through there.

Requires KASA_USERNAME / KASA_PASSWORD env vars.

Usage:
    client = KasaCloud()
    await client.login()
    await client.set_state("Kasa 4", on=True)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

import aiohttp

log = logging.getLogger("kasa_cloud")

CLOUD_URL = "https://wap.tplinkcloud.com/"


class KasaCloud:
    def __init__(self, username: str | None = None, password: str | None = None) -> None:
        self._username = username or os.environ.get("KASA_USERNAME")
        self._password = password or os.environ.get("KASA_PASSWORD")
        if not self._username or not self._password:
            raise ValueError("KASA_USERNAME / KASA_PASSWORD required")
        self._token: str | None = None
        self._terminal_uuid = str(uuid.uuid4())
        self._devices: dict[str, dict] = {}  # alias -> device info dict

    async def _post(self, payload: dict, *, with_token: bool = False) -> dict:
        params = {"token": self._token} if with_token and self._token else None
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(CLOUD_URL, json=payload, params=params) as r:
                return await r.json(content_type=None)

    async def login(self) -> None:
        resp = await self._post({
            "method": "login",
            "params": {
                "appType": "Kasa_Android",
                "cloudUserName": self._username,
                "cloudPassword": self._password,
                "terminalUUID": self._terminal_uuid,
            },
        })
        if resp.get("error_code", 0) != 0:
            raise RuntimeError(f"Kasa cloud login failed: {resp}")
        self._token = resp["result"]["token"]
        log.info("Kasa cloud: logged in as %s", self._username)

    async def list_devices(self) -> list[dict]:
        if not self._token:
            await self.login()
        resp = await self._post({"method": "getDeviceList"}, with_token=True)
        if resp.get("error_code", 0) != 0:
            raise RuntimeError(f"Kasa cloud list devices failed: {resp}")
        devices = resp["result"]["deviceList"]
        self._devices = {d.get("alias", d["deviceId"]): d for d in devices}
        log.info("Kasa cloud: %d devices: %s", len(devices),
                 [d.get("alias") for d in devices])
        return devices

    async def _resolve(self, alias: str) -> dict:
        if not self._devices:
            await self.list_devices()
        if alias not in self._devices:
            # Try a refresh in case the device just came online
            await self.list_devices()
        if alias not in self._devices:
            raise KeyError(f"Kasa cloud: device not found: {alias!r} "
                           f"(have: {list(self._devices.keys())})")
        return self._devices[alias]

    async def _passthrough(self, alias: str, command: dict) -> dict:
        dev = await self._resolve(alias)
        resp = await self._post({
            "method": "passthrough",
            "params": {
                "deviceId": dev["deviceId"],
                "requestData": json.dumps(command),
            },
        }, with_token=True)
        if resp.get("error_code", 0) != 0:
            raise RuntimeError(f"Kasa cloud passthrough failed: {resp}")
        # passthrough returns {"result": {"responseData": "<json string>"}}
        try:
            return json.loads(resp["result"]["responseData"])
        except Exception:
            return resp.get("result", {})

    async def set_state(self, alias: str, on: bool) -> None:
        # Legacy Kasa command; cloud handles protocol translation for KLAP.
        cmd = {"system": {"set_relay_state": {"state": 1 if on else 0}}}
        await self._passthrough(alias, cmd)
        log.info("Kasa cloud: %s -> %s", alias, "ON" if on else "OFF")

    async def is_on(self, alias: str) -> bool:
        cmd = {"system": {"get_sysinfo": {}}}
        resp = await self._passthrough(alias, cmd)
        try:
            return bool(resp["system"]["get_sysinfo"]["relay_state"])
        except Exception:
            # KP-series devices return a different shape
            try:
                return bool(resp["system"]["get_sysinfo"]["children"][0]["state"])
            except Exception:
                raise RuntimeError(f"Kasa cloud: unexpected response shape: {resp}")

    async def on(self, alias: str) -> None:
        await self.set_state(alias, True)

    async def off(self, alias: str) -> None:
        await self.set_state(alias, False)
