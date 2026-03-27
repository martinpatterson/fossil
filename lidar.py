"""RPLIDAR C1 footstep detector for Fossil installation.

Single background thread owns all serial I/O. Main thread communicates
via thread-safe event queue and calibration flag. Auto-detects USB port.
"""

import glob
import math
import os
import threading
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
RECONNECT_INTERVAL = 10.0


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
        self._background = None
        self._clusters: dict[int, ClusterState] = {}
        self._next_cluster_id = 0
        self._event_queue = deque()
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._calibrate_requested = False

    def setup(self):
        """Start background worker thread."""
        self._running = True
        self._calibrate_requested = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def poll(self):
        """Drain and return pending step events."""
        with self._lock:
            events = list(self._event_queue)
            self._event_queue.clear()
        return events

    def calibrate(self):
        """Request recalibration (non-blocking)."""
        self._calibrate_requested = True

    def get_debug_state(self):
        """Snapshot of clusters and background for visualization."""
        with self._lock:
            clusters = {cid: ClusterState(
                id=cs.id, centroid=cs.centroid, prev_centroid=cs.prev_centroid,
                state=cs.state, frame_count=cs.frame_count,
                fired_count=cs.fired_count, absent_count=cs.absent_count,
            ) for cid, cs in self._clusters.items()}
        bg = self._background.copy() if self._background is not None else None
        return clusters, bg

    def close(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        print("LiDAR: stopped")

    # ── Worker thread ────────────────────────────────────────────────────

    def _worker(self):
        """Connect → calibrate → scan. Reconnect on any failure."""
        while self._running:
            port = self._open_port()
            if port is None:
                time.sleep(RECONNECT_INTERVAL)
                continue

            try:
                # Calibrate on first connect or when requested
                if self._calibrate_requested:
                    self._calibrate_requested = False
                    self._run_calibration(port)

                # Scan continuously
                self._run_scan(port)

            except Exception as e:
                print(f"LiDAR: error ({e}), will reconnect...")

            # Cleanup
            try:
                port.write(bytes([SYNC_BYTE, STOP_CMD]))
                time.sleep(0.1)
                port.close()
            except Exception:
                pass

            if self._running:
                time.sleep(RECONNECT_INTERVAL)

    def _open_port(self):
        """Find and open RPLIDAR serial port."""
        port_path = self._find_port()
        if not port_path:
            return None
        try:
            port = serial.Serial(port_path, baudrate=config.LIDAR_BAUD, timeout=3.0)
            # STOP, wait, flush, RESET, wait, flush — proven sequence
            port.write(bytes([SYNC_BYTE, STOP_CMD]))
            time.sleep(0.5)
            port.reset_input_buffer()
            port.write(bytes([SYNC_BYTE, RESET_CMD]))
            time.sleep(2.0)
            port.reset_input_buffer()
            print(f"LiDAR: connected on {port_path}")
            return port
        except Exception as e:
            print(f"LiDAR: not available ({e})")
            return None

    def _run_calibration(self, port):
        """Blocking calibration: start scan, collect background, stop scan."""
        print("LiDAR: calibrating background...")

        # Start scan
        port.write(bytes([SYNC_BYTE, SCAN_CMD]))
        desc = port.read(7)
        if len(desc) < 7:
            raise RuntimeError("no calibration descriptor")

        # Settle
        settle_end = time.monotonic() + config.LIDAR_BG_SETTLE_SEC
        for scan in self._read_scans(port):
            if time.monotonic() >= settle_end:
                break

        # Collect frames
        frames = []
        for scan in self._read_scans(port):
            frames.append(self._scan_to_polar(scan))
            if len(frames) >= config.LIDAR_BG_FRAMES:
                break

        # Keep motor running — don't stop, don't flush.
        # _run_scan will pick up from wherever the stream is.

        # Compute background
        bins = np.full(360, np.nan)
        for angle_bin in range(360):
            values = [f[angle_bin] for f in frames if angle_bin in f]
            if values:
                bins[angle_bin] = np.median(values)

        self._background = bins
        with self._lock:
            self._clusters.clear()
        self._next_cluster_id = 0
        print("LiDAR: background learned")

    def _run_scan(self, port):
        """Continuous scanning. Blocks until error or calibration requested.
        Motor and scan already running from calibration — just keep reading."""
        scan = []
        buf = bytearray()
        while self._running:
            if self._calibrate_requested:
                port.write(bytes([SYNC_BYTE, STOP_CMD]))
                time.sleep(1.0)
                port.reset_input_buffer()
                buf.clear()
                self._calibrate_requested = False
                self._run_calibration(port)
                scan = []
                continue

            chunk = port.read(max(5, port.in_waiting))
            if not chunk:
                continue
            buf.extend(chunk)

            while len(buf) >= 5:
                b0, b1, b2, b3, b4 = buf[0], buf[1], buf[2], buf[3], buf[4]
                new_scan = bool(b0 & 0x01)
                quality = b0 >> 2
                angle = ((b1 >> 1) | (b2 << 7)) / 64.0
                distance = (b3 | (b4 << 8)) / 4.0
                del buf[:5]

                if new_scan and scan:
                    self._process_scan(scan)
                    scan = []

                if quality > 0 and distance > 0:
                    scan.append((quality, angle, distance))

    def _read_scans(self, port):
        """Yield complete scans (blocking, buffered)."""
        scan = []
        buf = bytearray()
        while True:
            chunk = port.read(max(5, port.in_waiting))
            if not chunk:
                continue
            buf.extend(chunk)

            while len(buf) >= 5:
                b0, b1, b2, b3, b4 = buf[0], buf[1], buf[2], buf[3], buf[4]
                new_scan = bool(b0 & 0x01)
                quality = b0 >> 2
                angle = ((b1 >> 1) | (b2 << 7)) / 64.0
                distance = (b3 | (b4 << 8)) / 4.0
                del buf[:5]

                if new_scan and scan:
                    yield scan
                    scan = []
                if quality > 0 and distance > 0:
                    scan.append((quality, angle, distance))

    # ── Port detection ───────────────────────────────────────────────────

    @staticmethod
    def _find_port():
        for dev in glob.glob("/dev/ttyUSB*"):
            try:
                devname = os.path.basename(dev)
                vid_path = os.path.realpath(f"/sys/class/tty/{devname}/device/../idVendor")
                if os.path.exists(vid_path):
                    vid = open(vid_path).read().strip()
                    if vid == RPLIDAR_VID:
                        return dev
            except Exception:
                continue
        if os.path.exists(config.LIDAR_PORT):
            return config.LIDAR_PORT
        return None

    # ── Scan processing ──────────────────────────────────────────────────

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
                rad = math.radians(angle_bin)
                fg_points.append((dist * math.sin(rad), dist * math.cos(rad)))

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
                result.append((sum(p[0] for p in c) / len(c), sum(p[1] for p in c) / len(c)))
        return result

    def _update_tracking(self, centroids):
        matched_ids = set()
        for cx, cy in centroids:
            best_id = None
            best_dist = float('inf')
            for cid, cs in self._clusters.items():
                d = math.sqrt((cx - cs.centroid[0])**2 + (cy - cs.centroid[1])**2)
                if d < best_dist and d < 300:
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
                cs = ClusterState(id=cid, centroid=(cx, cy), prev_centroid=(cx, cy), state='armed', frame_count=1)
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

    def _step_state_machine(self, cs, present):
        if cs.state == 'armed':
            if present and cs.frame_count >= config.LIDAR_STEP_MIN_FRAMES:
                dx = cs.centroid[0] - cs.prev_centroid[0]
                dy = cs.centroid[1] - cs.prev_centroid[1]
                vel = math.sqrt(dx * dx + dy * dy)
                if vel >= config.LIDAR_VELOCITY_MIN_MM or cs.frame_count == 1:
                    with self._lock:
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
