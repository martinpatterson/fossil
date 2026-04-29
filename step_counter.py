"""Hourly footstep counter with SQLite persistence.

Hot path (main thread): record() does only an int increment + queue.put_nowait,
never touches disk. A daemon writer thread drains the queue every
STEP_COUNTER_FLUSH_SEC and UPSERTs hourly totals into SQLite (WAL mode for
concurrent read access from the stats web frontend).

Failure isolation: every internal code path is try/except wrapped. record() is
guaranteed never to raise — worst case the counter goes silent. The fossil app
keeps running. STEP_COUNTER_ENABLED=False disables the counter entirely.
"""
from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import config

log = logging.getLogger("step_counter")


def _hour_key(ts: float) -> str:
    """UTC hour bucket as 'YYYY-MM-DDTHH'."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H")


class StepCounter:
    def __init__(self) -> None:
        self._enabled = bool(getattr(config, "STEP_COUNTER_ENABLED", True))
        self._db_path = Path(__file__).resolve().parent / getattr(
            config, "STEP_COUNTER_DB", "data/footsteps.db"
        )
        self._flush_sec = float(getattr(config, "STEP_COUNTER_FLUSH_SEC", 30))
        self._queue: queue.Queue[float] = queue.Queue(maxsize=10000)
        self._stop = threading.Event()
        self._writer: threading.Thread | None = None

        if not self._enabled:
            log.info("step counter disabled by config")
            return

        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
        except Exception as e:
            log.error("init failed, disabling counter: %s", e)
            self._enabled = False
            return

        self._writer = threading.Thread(
            target=self._writer_loop, name="step-counter-writer", daemon=True
        )
        self._writer.start()
        log.info("step counter started (db=%s flush=%ss)", self._db_path, self._flush_sec)

    def _init_db(self) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS hourly_steps ("
                "  hour_utc TEXT PRIMARY KEY,"
                "  count    INTEGER NOT NULL DEFAULT 0"
                ")"
            )
            conn.commit()
        finally:
            conn.close()

    def record(self, n: int = 1) -> None:
        """Record n steps. Non-blocking, never raises."""
        if not self._enabled:
            return
        try:
            ts = time.time()
            for _ in range(n):
                self._queue.put_nowait(ts)
        except queue.Full:
            # Queue overflow — drop silently. Audio/render must never stall.
            pass
        except Exception:
            pass

    def _drain(self, conn: sqlite3.Connection) -> int:
        """Drain queue and UPSERT counts. Returns total records written."""
        per_hour: dict[str, int] = {}
        drained = 0
        while True:
            try:
                ts = self._queue.get_nowait()
            except queue.Empty:
                break
            per_hour[_hour_key(ts)] = per_hour.get(_hour_key(ts), 0) + 1
            drained += 1

        if not per_hour:
            return 0

        try:
            for hour, count in per_hour.items():
                conn.execute(
                    "INSERT INTO hourly_steps(hour_utc, count) VALUES(?, ?) "
                    "ON CONFLICT(hour_utc) DO UPDATE SET count = count + excluded.count",
                    (hour, count),
                )
            conn.commit()
        except Exception as e:
            log.error("flush failed (drained %d, will retry next cycle): %s", drained, e)
            # Re-enqueue what we lost so we don't drop them
            for hour, count in per_hour.items():
                # Convert hour bucket back to a representative timestamp
                # (use start of hour). Best-effort.
                try:
                    dt = datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
                    ts = dt.timestamp()
                    for _ in range(count):
                        self._queue.put_nowait(ts)
                except Exception:
                    pass
            return 0
        return drained

    def _writer_loop(self) -> None:
        try:
            conn = sqlite3.connect(self._db_path, isolation_level=None,
                                   check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception as e:
            log.error("writer thread DB connect failed: %s", e)
            return

        try:
            while not self._stop.is_set():
                self._stop.wait(self._flush_sec)
                try:
                    self._drain(conn)
                except Exception as e:
                    log.error("writer iteration error: %s", e)
            # Final flush on shutdown
            try:
                self._drain(conn)
            except Exception as e:
                log.error("final flush error: %s", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def close(self) -> None:
        if not self._enabled or self._writer is None:
            return
        self._stop.set()
        try:
            self._writer.join(timeout=10.0)
        except Exception:
            pass
        log.info("step counter closed")
