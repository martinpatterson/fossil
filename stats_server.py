#!/usr/bin/env python3
"""Footstep stats web frontend (read-only).

Serves a single-page dashboard with two charts (hourly/daily) and JSON APIs.
Opens the SQLite DB read-only — concurrent with the writer in fossil-app.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request

import config

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
DB_PATH = Path(__file__).resolve().parent / getattr(
    config, "STEP_COUNTER_DB", "data/footsteps.db"
)
PORT = int(os.environ.get("STATS_PORT", "5050"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("stats")

app = Flask(__name__)


def _connect_ro() -> sqlite3.Connection:
    """Open DB read-only via URI mode."""
    uri = f"file:{DB_PATH}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


def _utc_to_local_iso(hour_utc: str) -> str:
    """Convert 'YYYY-MM-DDTHH' UTC bucket to local ISO 'YYYY-MM-DD HH:00'."""
    try:
        dt = datetime.strptime(hour_utc, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
        local = dt.astimezone(LOCAL_TZ)
        return local.strftime("%Y-%m-%d %H:00")
    except Exception:
        return hour_utc


@app.route("/api/hourly")
def api_hourly():
    days = int(request.args.get("days", "7"))
    days = max(1, min(days, 365))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_key = cutoff.strftime("%Y-%m-%dT%H")
    try:
        conn = _connect_ro()
        rows = conn.execute(
            "SELECT hour_utc, count FROM hourly_steps "
            "WHERE hour_utc >= ? ORDER BY hour_utc ASC",
            (cutoff_key,),
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return jsonify([])
    return jsonify([
        {"hour": _utc_to_local_iso(h), "count": c} for h, c in rows
    ])


@app.route("/api/daily")
def api_daily():
    try:
        conn = _connect_ro()
        rows = conn.execute(
            "SELECT substr(hour_utc, 1, 10) AS day_utc, SUM(count) AS total "
            "FROM hourly_steps GROUP BY day_utc ORDER BY day_utc ASC"
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return jsonify([])
    return jsonify([{"day": d, "count": int(t)} for d, t in rows])


@app.route("/api/total")
def api_total():
    try:
        conn = _connect_ro()
        row = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM hourly_steps"
        ).fetchone()
        conn.close()
    except sqlite3.OperationalError:
        return jsonify({"total": 0})
    return jsonify({"total": int(row[0])})


INDEX_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Fossil Footstep Stats</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <style>
    body { font-family: -apple-system, system-ui, sans-serif; background: #111; color: #ddd; margin: 24px; }
    h1 { font-weight: 300; margin-bottom: 4px; }
    .total { color: #888; margin-bottom: 28px; }
    .total span { color: #fff; font-weight: 600; }
    .chart-wrap { background: #1a1a1a; padding: 16px; border-radius: 6px; margin-bottom: 24px; }
    .chart-wrap h2 { font-size: 16px; font-weight: 400; color: #aaa; margin: 0 0 12px 0; }
    canvas { max-height: 320px; }
  </style>
</head>
<body>
  <h1>Fossil Footsteps</h1>
  <div class="total">Total recorded: <span id="total">…</span></div>

  <div class="chart-wrap">
    <h2>Hourly (last 7 days, local time)</h2>
    <canvas id="hourly"></canvas>
  </div>

  <div class="chart-wrap">
    <h2>Daily (life of exhibit)</h2>
    <canvas id="daily"></canvas>
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

async function draw() {
  const total = await fetch('/api/total').then(r => r.json());
  document.getElementById('total').textContent = total.total.toLocaleString();

  const hourly = await fetch('/api/hourly?days=7').then(r => r.json());
  new Chart(document.getElementById('hourly'), {
    type: 'bar',
    data: { labels: hourly.map(r => r.hour),
            datasets: [{ data: hourly.map(r => r.count), backgroundColor: '#4ea1ff' }] },
    options: opts
  });

  const daily = await fetch('/api/daily').then(r => r.json());
  new Chart(document.getElementById('daily'), {
    type: 'bar',
    data: { labels: daily.map(r => r.day),
            datasets: [{ data: daily.map(r => r.count), backgroundColor: '#7bd88f' }] },
    options: opts
  });
}
draw();
setInterval(draw, 60000);  // refresh every minute
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return INDEX_HTML


if __name__ == "__main__":
    log.info("Stats server starting on :%d (db=%s)", PORT, DB_PATH)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
