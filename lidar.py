"""RPLIDAR C1 footstep detector for Fossil installation.

Single-threaded design: main loop calls poll() each frame.
Auto-detects USB port by vendor ID. Reconnects on failure.
"""

import glob
import math
import os
import time
from collections import deque
from dataclasses import dataclass

import numpy as np
import serial

import config

RPLIDAR_VID = "10c4"  # Silicon Labs CP2102N (RPLIDAR C1)
SYNC_BYTE = 0xA5
SCAN_CMD = 0x20
STOP_CMD = 0x25
RESET_CMD = 0x40
DESCRIPTOR_LEN = 7
RECONNECT_INTERVAL = 10.0
MAX_BYTES_PER_POLL = 500  # read up to 100 measurements per poll (5 bytes each)


@dataclass
class StepEvent:
    x_mm: float
    y_mm: float


@dataclass
class ClusterState:
    id: int
    centroid: tuple
    prev_centroid: tuple
    state: str
    frame_count: int = 0
    fired_count: int = 0
    absent_count: int = 0


class LidarTracker:
    def __init__(self):
        self._port = None
        self._background = None
        self._clusters: dict[int, ClusterState] = {}
        self._next_cluster_id = 0
        self._event_queue = deque()
        self._scanning = False
        self._last_reconnect = 0.0
        self._partial_scan = []

    # ── Public API (all called from main thread) ─────────────────────────

    def setup(self):
        """Connect and calibrate. Soft-fails if unavailable."""
        port_path = self._find_port()
        if port_path and self._connect(port_path):
            self._calibrate()
            self._start_scan()

    def poll(self):
        """Call every frame. Reads available serial data, processes scans.
        Returns list of StepEvents since last poll."""
        if self._port is None:
            now = time.monotonic()
            if now - self._last_reconnect >= RECONNECT_INTERVAL:
                self._last_reconnect = now
                port_path = self._find_port()
                if port_path and self._connect(port_path):
                    self._calibrate()
                    self._start_scan()
            return []

        try:
            return self._read_and_process()
        except Exception as e:
            print(f"LiDAR: error ({e}), will reconnect...")
            self._disconnect()
            return []

    def calibrate(self):
        """Recalibrate (called from C key handler). Room must be empty."""
        if self._port is None:
            return
        self._scanning = False
        try:
            self._send_cmd(STOP_CMD)
            time.sleep(2.0)
            self._port.reset_input_buffer()
            self._calibrate()
            self._start_scan()
        except Exception as e:
            print(f"LiDAR: recalibration failed ({e})")
            self._disconnect()

    def get_step_events(self) -> list[StepEvent]:
        """Drain and return pending step events."""
        events = list(self._event_queue)
        self._event_queue.clear()
        return events

    def get_debug_state(self):
        """Return snapshot of clusters and background for visualization."""
        clusters = {cid: ClusterState(
            id=cs.id, centroid=cs.centroid, prev_centroid=cs.prev_centroid,
            state=cs.state, frame_count=cs.frame_count,
            fired_count=cs.fired_count, absent_count=cs.absent_count,
        ) for cid, cs in self._clusters.items()}
        bg = self._background.copy() if self._background is not None else None
        return clusters, bg

    def close(self):
        """Clean shutdown."""
        self._disconnect()
        print("LiDAR: stopped")

    # ── Connection ───────────────────────────────────────────────────────

    @staticmethod
    def _find_port():
        """Find RPLIDAR serial port by USB vendor ID."""
        for dev in glob.glob("/dev/ttyUSB*"):
            try:
                devname = os.path.basename(dev)
                vid_path = f"/sys/class/tty/{devname}/device/../idVendor"
                vid_path = os.path.realpath(vid_path)
                if os.path.exists(vid_path):
                    vid = open(vid_path).read().strip()
                    if vid == RPLIDAR_VID:
                        return dev
            except Exception:
                continue
        # Fallback to config
        if os.path.exists(config.LIDAR_PORT):
            return config.LIDAR_PORT
        return None

    def _connect(self, port_path):
        """Open serial port and reset device."""
        self._disconnect()
        try:
            self._port = serial.Serial(port_path, baudrate=config.LIDAR_BAUD, timeout=0.05)
            self._send_cmd(STOP_CMD)
            time.sleep(0.5)
            self._port.reset_input_buffer()
            self._send_cmd(RESET_CMD)
            time.sleep(2.0)
            self._port.reset_input_buffer()
            print(f"LiDAR: connected on {port_path}")
            return True
        except Exception as e:
            print(f"LiDAR: not available ({e})")
            self._port = None
            return False

    def _disconnect(self):
        """Close serial port."""
        if self._port:
            try:
                self._send_cmd(STOP_CMD)
            except Exception:
                pass
            try:
                self._port.close()
            except Exception:
                pass
            self._port = None
        self._scanning = False

    # ── Calibration ──────────────────────────────────────────────────────

    def _calibrate(self):
        """Blocking calibration scan. Collects background with room empty."""
        print("LiDAR: calibrating background...")
        self._send_cmd(STOP_CMD)
        time.sleep(0.5)
        self._port.reset_input_buffer()

        # Start scan
        self._send_cmd(SCAN_CMD)
        desc = self._port.read(DESCRIPTOR_LEN)
        if len(desc) < DESCRIPTOR_LEN:
            raise RuntimeError("no response descriptor")

        # Collect scans (blocking reads with longer timeout for calibration)
        old_timeout = self._port.timeout
        self._port.timeout = 1.0

        # Settle
        settle_end = time.monotonic() + config.LIDAR_BG_SETTLE_SEC
        for scan in self._iter_scans_blocking():
            if time.monotonic() >= settle_end:
                break

        frames = []
        for scan in self._iter_scans_blocking():
            frames.append(self._scan_to_polar(scan))
            if len(frames) >= config.LIDAR_BG_FRAMES:
                break

        self._port.timeout = old_timeout

        # Stop scan
        self._send_cmd(STOP_CMD)
        time.sleep(0.5)
        self._port.reset_input_buffer()

        # Build background
        bins = np.full(360, np.nan)
        for angle_bin in range(360):
            values = [f[angle_bin] for f in frames if angle_bin in f]
            if values:
                bins[angle_bin] = np.median(values)

        self._background = bins
        self._clusters.clear()
        self._partial_scan = []
        print("LiDAR: background learned")

    def _iter_scans_blocking(self):
        """Yield complete scans (blocking reads, for calibration only)."""
        scan = []
        while True:
            raw = self._port.read(5)
            if len(raw) < 5:
                continue
            b0, b1, b2, b3, b4 = raw
            new_scan = bool(b0 & 0x01)
            quality = b0 >> 2
            angle = ((b1 >> 1) | (b2 << 7)) / 64.0
            distance = (b3 | (b4 << 8)) / 4.0
            if new_scan and scan:
                yield scan
                scan = []
            if quality > 0 and distance > 0:
                scan.append((quality, angle, distance))

    # ── Scanning (non-blocking, called from main loop) ───────────────────

    def _start_scan(self):
        """Send scan command and consume descriptor."""
        self._send_cmd(SCAN_CMD)
        old_timeout = self._port.timeout
        self._port.timeout = 1.0
        desc = self._port.read(DESCRIPTOR_LEN)
        self._port.timeout = old_timeout
        if len(desc) < DESCRIPTOR_LEN:
            raise RuntimeError("no response descriptor")
        self._scanning = True
        self._partial_scan = []

    def _read_and_process(self):
        """Non-blocking read of available bytes. Returns step events."""
        if not self._scanning:
            return []

        waiting = self._port.in_waiting
        if waiting < 5:
            return []

        # Read available data, capped to avoid blocking
        n_bytes = min(waiting, MAX_BYTES_PER_POLL)
        raw = self._port.read(n_bytes)

        events = []
        for i in range(0, len(raw) - 4, 5):
            b0, b1, b2, b3, b4 = raw[i:i+5]
            new_scan = bool(b0 & 0x01)
            quality = b0 >> 2
            angle = ((b1 >> 1) | (b2 << 7)) / 64.0
            distance = (b3 | (b4 << 8)) / 4.0

            if new_scan and self._partial_scan:
                self._process_scan(self._partial_scan)
                events.extend(self._event_queue)
                self._event_queue.clear()
                self._partial_scan = []

            if quality > 0 and distance > 0:
                self._partial_scan.append((quality, angle, distance))

        return events

    # ── Processing ───────────────────────────────────────────────────────

    def _send_cmd(self, cmd):
        self._port.write(bytes([SYNC_BYTE, cmd]))

    def _scan_to_polar(self, scan):
        result = {}
        for quality, angle, distance in scan:
            if distance <= 0:
                continue
            bin_idx = int(angle) % 360
            if bin_idx not in result or distance < result[bin_idx]:
                result[bin_idx] = distance
        return result

    def _is_rear_arc(self, angle_deg):
        half = config.LIDAR_MASK_REAR_DEG
        diff = abs((angle_deg - 180 + 180) % 360 - 180)
        return diff <= half

    def _polar_to_xy(self, angle_deg, dist_mm):
        rad = math.radians(angle_deg)
        return (dist_mm * math.sin(rad), dist_mm * math.cos(rad))

    def _process_scan(self, scan):
        if self._background is None:
            return

        polar = self._scan_to_polar(scan)
        fg_points = []
        for angle_bin, dist in polar.items():
            if self._is_rear_arc(angle_bin):
                continue
            bg_dist = self._background[angle_bin]
            if np.isnan(bg_dist):
                continue
            if dist < bg_dist - config.LIDAR_THRESHOLD_MM:
                x, y = self._polar_to_xy(angle_bin, dist)
                fg_points.append((x, y))

        clusters = self._cluster_points(fg_points)
        self._update_tracking(clusters)

    def _cluster_points(self, points):
        if not points:
            return []
        points = sorted(points, key=lambda p: math.atan2(p[1], p[0]))
        clusters = []
        current = [points[0]]
        for p in points[1:]:
            dx = p[0] - current[-1][0]
            dy = p[1] - current[-1][1]
            if math.sqrt(dx * dx + dy * dy) < 200:
                current.append(p)
            else:
                clusters.append(current)
                current = [p]
        clusters.append(current)
        result = []
        for c in clusters:
            if config.LIDAR_CLUSTER_MIN_PTS <= len(c) <= config.LIDAR_CLUSTER_MAX_PTS:
                cx = sum(p[0] for p in c) / len(c)
                cy = sum(p[1] for p in c) / len(c)
                result.append((cx, cy))
        return result

    def _update_tracking(self, centroids):
        matched_ids = set()
        match_radius = 300
        for cx, cy in centroids:
            best_id = None
            best_dist = float('inf')
            for cid, cs in self._clusters.items():
                dx = cx - cs.centroid[0]
                dy = cy - cs.centroid[1]
                d = math.sqrt(dx * dx + dy * dy)
                if d < best_dist and d < match_radius:
                    best_dist = d
                    best_id = cid
            if best_id is not None:
                matched_ids.add(best_id)
                cs = self._clusters[best_id]
                cs.prev_centroid = cs.centroid
                cs.centroid = (cx, cy)
                cs.frame_count += 1
                cs.absent_count = 0
                self._step_state_machine(cs, present=True)
            else:
                cid = self._next_cluster_id
                self._next_cluster_id += 1
                cs = ClusterState(
                    id=cid, centroid=(cx, cy), prev_centroid=(cx, cy),
                    state='armed', frame_count=1,
                )
                self._clusters[cid] = cs
                matched_ids.add(cid)
                self._step_state_machine(cs, present=True)

        absent_ids = []
        for cid, cs in self._clusters.items():
            if cid not in matched_ids:
                cs.absent_count += 1
                self._step_state_machine(cs, present=False)
                if cs.absent_count > config.LIDAR_REARM_FRAMES + 5:
                    absent_ids.append(cid)
        for cid in absent_ids:
            del self._clusters[cid]

    def _step_state_machine(self, cs: ClusterState, present: bool):
        if cs.state == 'armed':
            if present and cs.frame_count >= config.LIDAR_STEP_MIN_FRAMES:
                dx = cs.centroid[0] - cs.prev_centroid[0]
                dy = cs.centroid[1] - cs.prev_centroid[1]
                vel = math.sqrt(dx * dx + dy * dy)
                if vel >= config.LIDAR_VELOCITY_MIN_MM or cs.frame_count == 1:
                    self._event_queue.append(StepEvent(x_mm=cs.centroid[0], y_mm=cs.centroid[1]))
                    cs.state = 'fired'
                    cs.fired_count += 1
        elif cs.state == 'fired':
            if not present:
                cs.state = 'cooling'
            elif cs.frame_count > config.LIDAR_STEP_MAX_FRAMES:
                cs.state = 'suppressed'
        elif cs.state == 'suppressed':
            if not present:
                cs.state = 'cooling'
        elif cs.state == 'cooling':
            if present:
                cs.absent_count = 0
            elif cs.absent_count >= config.LIDAR_REARM_FRAMES:
                cs.state = 'armed'
                cs.frame_count = 0
