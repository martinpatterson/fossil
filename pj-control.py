#!/usr/bin/env python3
"""Projector control via Google TV Remote protocol (androidtvremote2).

Usage:
    pj-control.py <name|ip> status     - Power state and current app
    pj-control.py <name|ip> on         - Turn on
    pj-control.py <name|ip> off        - Turn off
    pj-control.py <name|ip> pair       - Pair with projector (one-time)
    pj-control.py list                 - List configured projectors

Projectors are configured in pj-config.json:
    {
        "fossil-pj": {"ip": "10.0.0.10"},
        "haste-pj":  {"ip": "10.0.0.11"}
    }
"""

import asyncio
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "pj-config.json")
CERT_DIR = os.path.join(SCRIPT_DIR, ".pj-certs")


def _local_ip_for(target_ip: str) -> str | None:
    """Return the local IP that would route to target_ip (for mDNS interface selection)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target_ip, 1))
        local = s.getsockname()[0]
        s.close()
        return local
    except Exception:
        return None


async def cast_wake(ip: str, timeout: float = 3.0) -> None:
    """Send mDNS Google Cast discovery to wake a device out of Wake-on-Cast standby.

    The device's network chip pattern-matches mDNS queries for
    _googlecast._tcp.local and wakes the SoC. This works even when
    the IP stack is down (ping fails) because it operates at the NIC
    pattern-match level.

    Binds to the interface that routes to ip so the multicast query
    goes out the correct network.
    """
    local_ip = _local_ip_for(ip)
    try:
        from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser
    except ImportError:
        return
    try:
        kwargs = {}
        if local_ip:
            kwargs["interfaces"] = [local_ip]
        zc = AsyncZeroconf(**kwargs)
        browser = AsyncServiceBrowser(zc.zeroconf, "_googlecast._tcp.local.",
                                       handlers=[lambda *a, **k: None])
        await asyncio.sleep(timeout)
        await browser.async_cancel()
        await zc.async_close()
    except Exception as e:
        print(f"cast_wake error: {e}", file=sys.stderr)


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE) as f:
        return json.load(f)


def resolve_target(name_or_ip):
    config = load_config()
    if name_or_ip in config:
        return name_or_ip, config[name_or_ip]["ip"], config[name_or_ip]
    # Try matching by IP
    for name, cfg in config.items():
        if cfg.get("ip") == name_or_ip:
            return name, name_or_ip, cfg
    # Use as raw IP with generated name
    return name_or_ip.replace(".", "_"), name_or_ip, {}


def cert_paths(name):
    os.makedirs(CERT_DIR, exist_ok=True)
    return (
        os.path.join(CERT_DIR, f"{name}-cert.pem"),
        os.path.join(CERT_DIR, f"{name}-key.pem"),
    )


async def do_pair(name, ip):
    from androidtvremote2 import AndroidTVRemote

    certfile, keyfile = cert_paths(name)
    remote = AndroidTVRemote("Fossil NUC", certfile, keyfile, ip)
    await remote.async_generate_cert_if_missing()
    await remote.async_start_pairing()
    pin = input("Enter PIN from projector screen: ")
    await remote.async_finish_pairing(pin.strip())
    print(f"Paired with {name} ({ip}) successfully.")


async def do_status(name, ip):
    from androidtvremote2 import AndroidTVRemote

    certfile, keyfile = cert_paths(name)
    if not os.path.exists(certfile):
        print(f"Not paired with {name}. Run: pj-control.py {name} pair")
        sys.exit(1)
    remote = AndroidTVRemote("Fossil NUC", certfile, keyfile, ip)
    await remote.async_connect()
    info = remote.device_info
    power = remote.is_on
    app = remote.current_app
    print(f"Projector:  {name} ({ip})")
    print(f"Model:      {info.get('manufacturer', '?')} {info.get('model', '?')}")
    print(f"Software:   {info.get('sw_version', '?')}")
    print(f"Power:      {'ON' if power else 'OFF'}")
    print(f"Running:    {app or 'N/A'}")
    remote.disconnect()


async def _adb_keyevent(ip: str, keycode: str) -> None:
    """Send a key via ADB (used for projectors whose firmware doesn't honor
    Google TV Remote KEYCODE_POWER from sleep — e.g. Hisense PX3-PRO 6.7+)."""
    proc = await asyncio.create_subprocess_exec(
        "adb", "connect", f"{ip}:5555",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    proc = await asyncio.create_subprocess_exec(
        "adb", "-s", f"{ip}:5555", "shell", "input", "keyevent", keycode,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def _adb_select_input(ip: str, input_id: str) -> None:
    """Switch projector to a TvInput passthrough.
    input_id like 'com.vt.source.external/.hdmi.HdmiTvInputService/HW2'.
    KEYCODE_TV_INPUT_HDMI_1 is a no-op on Hisense PX3-PRO; this intent works."""
    from urllib.parse import quote
    uri = f"content://android.media.tv/passthrough/{quote(input_id, safe='')}"
    proc = await asyncio.create_subprocess_exec(
        "adb", "-s", f"{ip}:5555", "shell", "am", "start",
        "-a", "android.intent.action.VIEW", "-d", uri,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def _query_is_on(name: str, ip: str) -> bool | None:
    """Best-effort current power state via Google TV Remote.
    Returns True/False or None if unreachable. Short timeout — never blocks."""
    try:
        from androidtvremote2 import AndroidTVRemote
    except ImportError:
        return None
    certfile, keyfile = cert_paths(name)
    if not os.path.exists(certfile):
        return None
    try:
        remote = AndroidTVRemote("Fossil NUC", certfile, keyfile, ip)
        await asyncio.wait_for(remote.async_connect(), timeout=2.5)
        await asyncio.sleep(0.5)
        is_on = remote.is_on
        try:
            remote.disconnect()
        except Exception:
            pass
        return is_on
    except Exception:
        return None


async def do_power(name, ip, turn_on, cfg=None):
    from androidtvremote2 import AndroidTVRemote
    from androidtvremote2.exceptions import CannotConnect

    cfg = cfg or {}
    # `adb_disabled: true` is a temporary fallback for when ADB is locked out
    # (e.g. the device's USB-debugging auth dialog needs physical OK). It
    # routes power through the Google TV Remote KEYCODE_POWER path instead.
    # That path is less reliable: it may fail to wake from deep sleep on
    # firmwares where KEYCODE_POWER is a no-op for wake. Remove the flag
    # once ADB is restored on the device.
    adb_fallback = bool(cfg.get("adb_power")) and bool(cfg.get("adb_disabled"))
    use_adb = bool(cfg.get("adb_power")) and not adb_fallback
    if adb_fallback:
        print(f"{name}: adb_disabled — using Google TV Remote "
              f"(wake=MENU+BACK, sleep=POWER)")

    if use_adb:
        # Pure ADB path — works regardless of firmware quirks.
        # First check current state via Google TV Remote (best effort) so we
        # don't redundantly fire WAKEUP/SLEEP. Sending WAKEUP to an already
        # awake Hisense PX3-PRO triggers an HDMI re-handshake (black flash).
        action = "on" if turn_on else "off"
        keycode = "KEYCODE_WAKEUP" if turn_on else "KEYCODE_SLEEP"
        is_on = await _query_is_on(name, ip)
        if is_on is True and turn_on:
            print(f"{name}: already on")
            return
        if is_on is False and not turn_on:
            print(f"{name}: already off")
            return
        print(f"{name}: turning {action} via ADB ({keycode})")
        await _adb_keyevent(ip, keycode)
        await asyncio.sleep(1.0)
        input_id = cfg.get("hdmi_input_id")
        if turn_on and input_id:
            await asyncio.sleep(1.5)
            await _adb_select_input(ip, input_id)
            print(f"{name}: selected input {input_id}")
        return

    certfile, keyfile = cert_paths(name)
    if not os.path.exists(certfile):
        print(f"Not paired with {name}. Run: pj-control.py {name} pair")
        sys.exit(1)
    remote = AndroidTVRemote("Fossil NUC", certfile, keyfile, ip)
    # When turning on, projector may be in Wake-on-Cast standby; send mDNS
    # query first to wake the network stack before attempting TCP connect.
    if turn_on:
        try:
            await asyncio.wait_for(remote.async_connect(), timeout=3.0)
        except (CannotConnect, asyncio.TimeoutError):
            print(f"{name}: unreachable — sending cast wake...")
            await cast_wake(ip, timeout=4.0)
            # Retry after wake
            remote = AndroidTVRemote("Fossil NUC", certfile, keyfile, ip)
            await remote.async_connect()
    else:
        await remote.async_connect()
    # androidtvremote2 populates is_on via an async server message after
    # connect — reading it immediately can return None/stale. Settle delay
    # mirrors what _query_is_on() already does. Important when sleep uses
    # KEYCODE_POWER (toggle), where a stale "off" guard would mis-fire and
    # wake the projector instead.
    await asyncio.sleep(0.5)
    is_on = remote.is_on
    if turn_on and is_on:
        print(f"{name}: already on")
    elif not turn_on and not is_on:
        print(f"{name}: already off")
    else:
        if turn_on and adb_fallback:
            # haste-pj quirk: in deep sleep, KEYCODE_POWER and KEYCODE_WAKEUP
            # are both no-ops. KEYCODE_MENU wakes it. MENU also surfaces the
            # Hisense overlay strip, so we follow with KEYCODE_BACK to dismiss.
            remote.send_key_command("KEYCODE_MENU", "SHORT")
            await asyncio.sleep(1.5)
            remote.send_key_command("KEYCODE_BACK", "SHORT")
            print(f"{name}: turning on (MENU+BACK)")
        else:
            remote.send_key_command("KEYCODE_POWER", "SHORT")
            action = "on" if turn_on else "off"
            print(f"{name}: turning {action}")
        # send_key_command is async/buffered; wait for it to flush before disconnect
        await asyncio.sleep(1.0)
    remote.disconnect()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    if sys.argv[1] == "list":
        config = load_config()
        if not config:
            print("No projectors configured. Create pj-config.json")
        for name, cfg in config.items():
            certfile, _ = cert_paths(name)
            paired = "paired" if os.path.exists(certfile) else "not paired"
            print(f"  {name:15s} {cfg['ip']:15s} ({paired})")
        sys.exit(0)

    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    target = sys.argv[1]
    command = sys.argv[2].lower()
    name, ip, cfg = resolve_target(target)

    if command == "pair":
        asyncio.run(do_pair(name, ip))
    elif command == "status":
        asyncio.run(do_status(name, ip))
    elif command == "on":
        asyncio.run(do_power(name, ip, True, cfg))
    elif command == "off":
        asyncio.run(do_power(name, ip, False, cfg))
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
