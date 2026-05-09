#!/usr/bin/env python3
"""Footstep stats web frontend (read-only).

Single-page dashboard with footstep charts + system status pane,
behind a simple login. The footstep DB is opened read-only so this
service can NEVER affect the running fossil-app.

System-status data is collected by a background thread every 30s and
cached. Per-request handlers do at most a stat() of /sys/class/thermal,
never blocking the page render with TLS handshakes etc. Every collector
is defensive — any failure logs and continues; status fields fall back
to "?" rather than crashing the server.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template_string, request, session, url_for

import config

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
DB_PATH = Path(__file__).resolve().parent / getattr(
    config, "STEP_COUNTER_DB", "data/footsteps.db"
)
PORT = int(os.environ.get("STATS_PORT", "5050"))

# Auth (simple — site sits behind LAN/Tailscale anyway)
USERNAME = "tyler"
PASSWORD = "evidence"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("stats")

app = Flask(__name__)
app.secret_key = os.environ.get("STATS_SECRET_KEY") or os.urandom(32)


# ───────────────────────────── auth ─────────────────────────────

def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped


LOGIN_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>Tyler</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif;
         background: #111; color: #ddd; height: 100vh; margin: 0;
         display: flex; align-items: center; justify-content: center; }
  .box { width: 280px; text-align: center; }
  h1 { font-weight: 300; font-size: 32px; margin: 0 0 32px 0; }
  input { width: 100%; padding: 10px 12px; margin-bottom: 10px;
          background: #1a1a1a; color: #ddd; border: 1px solid #333;
          border-radius: 4px; font-size: 14px; box-sizing: border-box; }
  button { width: 100%; padding: 10px; background: #4ea1ff; color: #fff;
           border: 0; border-radius: 4px; font-size: 14px; cursor: pointer; }
  .err { color: #f87; margin-bottom: 12px; min-height: 1em; font-size: 13px; }
</style>
</head><body>
<form class="box" method="POST">
  <h1>Tyler</h1>
  <div class="err">{{ err or '' }}</div>
  <input name="u" type="text" placeholder="Username" autofocus autocomplete="off">
  <input name="p" type="password" placeholder="Password">
  <button type="submit">Sign in</button>
</form>
</body></html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        if request.form.get("u") == USERNAME and request.form.get("p") == PASSWORD:
            session["auth"] = True
            return redirect(url_for("index"))
        err = "Incorrect"
    return render_template_string(LOGIN_HTML, err=err)


@app.route("/logout")
def logout():
    session.pop("auth", None)
    return redirect(url_for("login"))


# ──────────────────────── footstep DB queries ──────────────────────

def _connect_ro() -> sqlite3.Connection:
    uri = f"file:{DB_PATH}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


def _utc_to_local_iso(hour_utc: str) -> str:
    try:
        dt = datetime.strptime(hour_utc, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:00")
    except Exception:
        return hour_utc


@app.route("/api/hourly")
@login_required
def api_hourly():
    days = max(1, min(int(request.args.get("days", "7")), 365))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H")
    try:
        conn = _connect_ro()
        rows = conn.execute(
            "SELECT hour_utc, count FROM hourly_steps "
            "WHERE hour_utc >= ? ORDER BY hour_utc ASC",
            (cutoff,),
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return jsonify([])
    return jsonify([{"hour": _utc_to_local_iso(h), "count": c} for h, c in rows])


@app.route("/api/daily")
@login_required
def api_daily():
    try:
        conn = _connect_ro()
        rows = conn.execute(
            "SELECT substr(hour_utc, 1, 10) AS day_utc, SUM(count) "
            "FROM hourly_steps GROUP BY day_utc ORDER BY day_utc ASC"
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return jsonify([])
    return jsonify([{"day": d, "count": int(t)} for d, t in rows])


@app.route("/api/total")
@login_required
def api_total():
    try:
        conn = _connect_ro()
        row = conn.execute("SELECT COALESCE(SUM(count), 0) FROM hourly_steps").fetchone()
        conn.close()
    except sqlite3.OperationalError:
        return jsonify({"total": 0})
    return jsonify({"total": int(row[0])})


# ──────────────────────── system status collector ──────────────────────
# Background thread polls every STATUS_POLL_SEC. Page reads cached value.

STATUS_POLL_SEC = 30
SCRIPT_DIR = Path(__file__).resolve().parent
PJ_CONFIG = SCRIPT_DIR / "pj-config.json"
PJ_CERT_DIR = SCRIPT_DIR / ".pj-certs"
SECRETS_FILE = SCRIPT_DIR / "secrets.env"

_status_lock = threading.Lock()
_status_cache: dict = {
    "updated": None,
    "app": "?",
    "fossil_pj": "?",
    "haste_pj": "?",
    "bloom": "?",
    "temp_c": None,
    "ups_status": "?",
    "ups_battery": None,
    "ups_runtime_min": None,
    "wifi_ssid": "?",
    "wifi_signal_dbm": None,
    "wifi_signal_pct": None,
    "kinect_present": None,
    "uptime": "?",
}


def _safe_run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ""


def _read_temp_c() -> float | None:
    try:
        # Pick the hottest zone (CPU package is usually zone1 or 2)
        zones = list(Path("/sys/class/thermal").glob("thermal_zone*/temp"))
        max_milli = 0
        for z in zones:
            try:
                v = int(z.read_text().strip())
                max_milli = max(max_milli, v)
            except Exception:
                pass
        return max_milli / 1000 if max_milli else None
    except Exception:
        return None


def _read_app_state() -> str:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "fossil-app.service"],
            capture_output=True, text=True, timeout=2.0,
        )
        return r.stdout.strip() or "?"
    except Exception:
        return "?"


def _read_uptime() -> str:
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        d, rem = divmod(int(secs), 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        if d:
            return f"{d}d {h}h"
        if h:
            return f"{h}h {m}m"
        return f"{m}m"
    except Exception:
        return "?"


def _read_kinect_present() -> bool:
    try:
        r = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=2.0)
        return "045e:097c" in r.stdout
    except Exception:
        return False


def _read_ups() -> tuple[str, int | None, int | None]:
    """Returns (status, battery%, runtime_min)."""
    try:
        out = _safe_run(["upsc", "ups"], timeout=3.0)
        st = "?"
        batt = None
        runtime = None
        for line in out.splitlines():
            if line.startswith("ups.status:"):
                st = line.split(":", 1)[1].strip()
            elif line.startswith("battery.charge:"):
                try:
                    batt = int(line.split(":", 1)[1].strip())
                except Exception:
                    pass
            elif line.startswith("battery.runtime:"):
                try:
                    runtime = int(line.split(":", 1)[1].strip()) // 60
                except Exception:
                    pass
        return st, batt, runtime
    except Exception:
        return "?", None, None


_WIFI_RE_SSID = re.compile(r'ESSID:"([^"]*)"')
_WIFI_RE_SIG = re.compile(r"Signal level=(-?\d+)")
_WIFI_RE_QUAL = re.compile(r"Link Quality=(\d+)/(\d+)")


def _read_wifi() -> tuple[str, int | None, int | None]:
    """Returns (ssid, signal_dbm, signal_pct)."""
    out = _safe_run(["iwconfig", "wlo1"], timeout=2.0)
    ssid = "?"
    dbm = None
    pct = None
    m = _WIFI_RE_SSID.search(out)
    if m:
        ssid = m.group(1) or "(none)"
    m = _WIFI_RE_SIG.search(out)
    if m:
        try:
            dbm = int(m.group(1))
        except Exception:
            pass
    m = _WIFI_RE_QUAL.search(out)
    if m:
        try:
            num, den = int(m.group(1)), int(m.group(2))
            if den:
                pct = round(num * 100 / den)
        except Exception:
            pass
    return ssid, dbm, pct


def _load_pj_config() -> dict:
    try:
        return json.loads(PJ_CONFIG.read_text())
    except Exception:
        return {}


async def _query_pj(name: str, ip: str) -> str:
    """Query a projector's power state. Returns 'on'/'off'/'unreachable'."""
    try:
        from androidtvremote2 import AndroidTVRemote
        from androidtvremote2.exceptions import CannotConnect, ConnectionClosed
    except ImportError:
        return "?"
    cert = PJ_CERT_DIR / f"{name}-cert.pem"
    key = PJ_CERT_DIR / f"{name}-key.pem"
    if not cert.exists():
        return "unpaired"
    remote = None
    try:
        remote = AndroidTVRemote("Stats Server", str(cert), str(key), ip)
        await asyncio.wait_for(remote.async_connect(), timeout=3.0)
        await asyncio.sleep(0.5)
        is_on = remote.is_on
        if is_on is None:
            return "?"
        return "on" if is_on else "off"
    except (CannotConnect, ConnectionClosed, asyncio.TimeoutError, OSError):
        return "unreachable"
    except Exception:
        return "?"
    finally:
        # Always disconnect — error paths used to leak the transport, and
        # the per-poll event loop closes immediately after this returns,
        # so we also yield long enough for the asyncio close callback to
        # actually send FIN. Without this, sockets pile up in CLOSE-WAIT.
        if remote is not None:
            try:
                remote.disconnect()
            except Exception:
                pass
            try:
                await asyncio.sleep(0.1)
            except Exception:
                pass


def _load_secrets() -> None:
    if not SECRETS_FILE.exists():
        return
    try:
        for line in SECRETS_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass


_kasa_client = None
_kasa_lock = threading.Lock()


async def _query_bloom() -> str:
    """Query 'Kasa 4' outlet state via TP-Link cloud (LAN may be isolated)."""
    global _kasa_client
    try:
        from kasa_cloud import KasaCloud
    except ImportError:
        return "?"
    try:
        with _kasa_lock:
            if _kasa_client is None:
                _kasa_client = KasaCloud()
                await _kasa_client.login()
        return "on" if await _kasa_client.is_on("Kasa 4") else "off"
    except Exception as e:
        log.debug("bloom query failed: %s", e)
        # Reset on error so next attempt re-logs in
        with _kasa_lock:
            _kasa_client = None
        return "?"


async def _collect_async() -> dict:
    cfg = _load_pj_config()
    # Run PJ queries concurrently — both can take a few seconds
    fossil_pj_task = asyncio.create_task(
        _query_pj("fossil-pj", cfg.get("fossil-pj", {}).get("ip", "10.0.0.10"))
    )
    haste_pj_task = asyncio.create_task(
        _query_pj("haste-pj", cfg.get("haste-pj", {}).get("ip", "10.0.0.11"))
    )
    bloom_task = asyncio.create_task(_query_bloom())
    fossil_pj = await fossil_pj_task
    haste_pj = await haste_pj_task
    bloom = await bloom_task
    return {"fossil_pj": fossil_pj, "haste_pj": haste_pj, "bloom": bloom}


def _collect_status() -> None:
    """One pass of status collection. Updates _status_cache atomically."""
    new = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    # Cheap synchronous queries
    new["app"] = _read_app_state()
    new["temp_c"] = _read_temp_c()
    new["uptime"] = _read_uptime()
    new["kinect_present"] = _read_kinect_present()
    st, batt, runtime = _read_ups()
    new["ups_status"] = st
    new["ups_battery"] = batt
    new["ups_runtime_min"] = runtime
    ssid, dbm, pct = _read_wifi()
    new["wifi_ssid"] = ssid
    new["wifi_signal_dbm"] = dbm
    new["wifi_signal_pct"] = pct

    # Async queries (PJs + Kasa) — protected by overall try/except
    try:
        loop = asyncio.new_event_loop()
        try:
            async_data = loop.run_until_complete(
                asyncio.wait_for(_collect_async(), timeout=20.0)
            )
            new.update(async_data)
        finally:
            loop.close()
    except Exception as e:
        log.debug("async status collection failed: %s", e)

    with _status_lock:
        _status_cache.update(new)


def _status_loop() -> None:
    """Daemon thread: periodically refresh status cache."""
    _load_secrets()
    while True:
        try:
            _collect_status()
        except Exception as e:
            log.error("status collection error: %s", e)
        time.sleep(STATUS_POLL_SEC)


@app.route("/api/status")
@login_required
def api_status():
    with _status_lock:
        return jsonify(dict(_status_cache))


# ──────────────────────────── dashboard ────────────────────────────

INDEX_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Tyler</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <style>
    body { font-family: -apple-system, system-ui, sans-serif; background: #111; color: #ddd; margin: 24px; }
    h1 { font-weight: 300; margin-bottom: 4px; }
    .total { color: #888; margin-bottom: 28px; }
    .total span { color: #fff; font-weight: 600; }
    .chart-wrap { background: #1a1a1a; padding: 16px; border-radius: 6px; margin-bottom: 24px; }
    .chart-wrap h2 { font-size: 16px; font-weight: 400; color: #aaa; margin: 0 0 12px 0; }
    canvas { max-height: 320px; }
    .status-pane { background: #1a1a1a; padding: 16px; border-radius: 6px; margin-bottom: 24px; }
    .status-pane h2 { font-size: 16px; font-weight: 400; color: #aaa; margin: 0 0 12px 0; }
    .status-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 8px 24px; }
    .status-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #222; font-size: 14px; }
    .label { color: #888; }
    .value { color: #fff; font-variant-numeric: tabular-nums; }
    .v-on { color: #7bd88f; }
    .v-off { color: #888; }
    .v-warn { color: #e8c468; }
    .v-bad { color: #f87; }
    .v-unknown { color: #666; }

    /* Prominent badge styling for PJ / Light bloom state */
    .badge { display: inline-block; padding: 2px 10px; border-radius: 3px;
             font-weight: 700; font-size: 13px; letter-spacing: 0.5px;
             text-transform: uppercase; }
    .badge.v-on  { background: #1f4429; color: #7bd88f; }
    .badge.v-off { background: #3a1f1f; color: #f87; }
    .badge.v-unknown { background: #2a2a2a; color: #888; }
    .updated { font-size: 12px; color: #555; margin-top: 12px; text-align: right; }
    .top { display: flex; justify-content: space-between; align-items: baseline; }
    .logout { color: #555; text-decoration: none; font-size: 13px; }
    .logout:hover { color: #888; }
  </style>
</head>
<body>
  <div class="top">
    <div>
      <h1>Tyler — Fossil Footsteps</h1>
      <div class="total">Total recorded: <span id="total">…</span></div>
    </div>
    <a class="logout" href="/logout">log out</a>
  </div>

  <div class="chart-wrap">
    <h2>Hourly (last 7 days, local time)</h2>
    <canvas id="hourly"></canvas>
  </div>

  <div class="chart-wrap">
    <h2>Daily (life of exhibit)</h2>
    <canvas id="daily"></canvas>
  </div>

  <div class="status-pane">
    <h2>System status</h2>
    <div class="status-grid" id="status">
      <div class="status-row"><span class="label">Fossil app</span><span class="value" id="s-app">…</span></div>
      <div class="status-row"><span class="label">Fossil projector</span><span class="value" id="s-fossil-pj">…</span></div>
      <div class="status-row"><span class="label">Haste projector</span><span class="value" id="s-haste-pj">…</span></div>
      <div class="status-row"><span class="label">Light bloom</span><span class="value" id="s-bloom">…</span></div>
      <div class="status-row"><span class="label">Kinect</span><span class="value" id="s-kinect">…</span></div>
      <div class="status-row"><span class="label">Uptime</span><span class="value" id="s-uptime">…</span></div>
      <div class="status-row"><span class="label">CPU temp</span><span class="value" id="s-temp">…</span></div>
      <div class="status-row"><span class="label">UPS</span><span class="value" id="s-ups">…</span></div>
      <div class="status-row"><span class="label">UPS battery</span><span class="value" id="s-batt">…</span></div>
      <div class="status-row"><span class="label">UPS runtime</span><span class="value" id="s-runtime">…</span></div>
      <div class="status-row"><span class="label">WiFi SSID</span><span class="value" id="s-ssid">…</span></div>
      <div class="status-row"><span class="label">WiFi signal</span><span class="value" id="s-wifi">…</span></div>
    </div>
    <div class="updated" id="s-updated"></div>
  </div>

<script>
const opts = {
  responsive: true,
  scales: {
    x: { ticks: { color: '#888' }, grid: { color: '#222' } },
    y: { beginAtZero: true, ticks: { color: '#888' }, grid: { color: '#222' } }
  },
  plugins: { legend: { display: false } }
};

let hourlyChart = null, dailyChart = null;

async function drawCharts() {
  const total = await fetch('/api/total').then(r => r.json());
  document.getElementById('total').textContent = total.total.toLocaleString();

  const hourly = await fetch('/api/hourly?days=7').then(r => r.json());
  const dailyData = await fetch('/api/daily').then(r => r.json());

  if (hourlyChart) hourlyChart.destroy();
  hourlyChart = new Chart(document.getElementById('hourly'), {
    type: 'bar',
    data: { labels: hourly.map(r => r.hour),
            datasets: [{ data: hourly.map(r => r.count), backgroundColor: '#4ea1ff' }] },
    options: opts
  });

  if (dailyChart) dailyChart.destroy();
  dailyChart = new Chart(document.getElementById('daily'), {
    type: 'bar',
    data: { labels: dailyData.map(r => r.day),
            datasets: [{ data: dailyData.map(r => r.count), backgroundColor: '#7bd88f' }] },
    options: opts
  });
}

function classFor(v) {
  if (v === 'on' || v === 'active' || v === true) return 'v-on';
  if (v === 'off' || v === 'inactive' || v === false) return 'v-off';
  if (v === 'unreachable' || v === '?' || v === 'unpaired') return 'v-unknown';
  if (v === 'OL' || (typeof v === 'string' && v.startsWith('OL'))) return 'v-on';
  if (typeof v === 'string' && v.includes('OB')) return 'v-warn';
  return '';
}

function setVal(id, value, klass) {
  const el = document.getElementById(id);
  el.textContent = value;
  el.className = 'value ' + (klass || '');
}

function setBadge(id, value) {
  const el = document.getElementById(id);
  // Map raw values to display labels
  const display = {
    'on': 'ON', 'off': 'OFF',
    'active': 'ON', 'inactive': 'OFF',
    'unreachable': 'UNREACH', 'unpaired': 'UNPAIRED',
  }[value] || value.toUpperCase();
  el.textContent = display;
  el.className = 'value badge ' + classFor(value);
}

async function drawStatus() {
  let s;
  try { s = await fetch('/api/status').then(r => r.json()); }
  catch (e) { return; }

  setBadge('s-app', s.app || '?');
  setBadge('s-fossil-pj', s.fossil_pj || '?');
  setBadge('s-haste-pj', s.haste_pj || '?');
  setBadge('s-bloom', s.bloom || '?');
  setVal('s-kinect', s.kinect_present === true ? 'present' : (s.kinect_present === false ? 'missing' : '?'),
         s.kinect_present === true ? 'v-on' : (s.kinect_present === false ? 'v-bad' : 'v-unknown'));
  setVal('s-uptime', s.uptime || '?');

  let tempCls = '';
  if (s.temp_c !== null && s.temp_c !== undefined) {
    if (s.temp_c >= 85) tempCls = 'v-bad';
    else if (s.temp_c >= 75) tempCls = 'v-warn';
    setVal('s-temp', s.temp_c.toFixed(0) + '°C', tempCls);
  } else { setVal('s-temp', '?', 'v-unknown'); }

  setVal('s-ups', s.ups_status || '?', classFor(s.ups_status));
  setVal('s-batt', s.ups_battery !== null ? s.ups_battery + '%' : '?');
  setVal('s-runtime', s.ups_runtime_min !== null ? s.ups_runtime_min + ' min' : '?');
  setVal('s-ssid', s.wifi_ssid || '?');

  let wifi = '?';
  let wifiCls = 'v-unknown';
  if (s.wifi_signal_dbm !== null) {
    wifi = s.wifi_signal_dbm + ' dBm';
    if (s.wifi_signal_pct !== null) wifi += ' (' + s.wifi_signal_pct + '%)';
    if (s.wifi_signal_dbm <= -80) wifiCls = 'v-bad';
    else if (s.wifi_signal_dbm <= -70) wifiCls = 'v-warn';
    else wifiCls = 'v-on';
  }
  setVal('s-wifi', wifi, wifiCls);

  if (s.updated) {
    document.getElementById('s-updated').textContent = 'updated ' + new Date(s.updated).toLocaleTimeString();
  }
}

drawCharts();
drawStatus();
setInterval(drawCharts, 60000);  // charts every minute
setInterval(drawStatus, 15000);  // status every 15s
</script>
</body>
</html>
"""


@app.route("/")
@login_required
def index():
    return INDEX_HTML


# Start the status collector before serving
threading.Thread(target=_status_loop, name="status-collector", daemon=True).start()

if __name__ == "__main__":
    log.info("Stats server starting on :%d (db=%s)", PORT, DB_PATH)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
