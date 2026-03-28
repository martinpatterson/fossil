"""RPLIDAR C1 footstep detector for Fossil installation."""

import glob
import math
import os
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import serial

import config

RPLIDAR_VID = "10c4"  # Silicon Labs CP2102N (RPLIDAR C1)


@dataclass
class StepEvent:
    x_mm: float   # negative=viewer's left, positive=viewer's right
    y_mm: float   # distance from sensor into gallery


@dataclass
class ClusterState:
    id: int
    centroid: tuple        # (x_mm, y_mm)
    prev_centroid: tuple
    state: str             # 'armed'|'fired'|'suppressed'|'cooling'
    frame_count: int = 0
    fired_count: int = 0
    absent_count: int = 0


# RPLIDAR protocol constants
SYNC_BYTE = 0xA5
SCAN_CMD = 0x20
STOP_CMD = 0x25
RESET_CMD = 0x40
DESCRIPTOR_LEN = 7


class LidarTracker:
    def __init__(self):
        self._serial_port = None
        self._background = None        # median distances per angle bin
        self._running = False
        self._thread = None
        self._event_queue = deque()
        self._lock = threading.Lock()
        self._clusters: dict[int, ClusterState] = {}
        self._next_cluster_id = 0
        self.needs_restart = False

    @staticmethod
    def _find_port():
        """Find RPLIDAR serial port by USB vendor ID, fall back to config."""
        import subprocess
        for dev in sorted(glob.glob("/dev/ttyUSB*")):
            try:
                result = subprocess.run(
                    ["udevadm", "info", "--name", dev],
                    capture_output=True, text=True, timeout=2,
                )
                for line in result.stdout.splitlines():
                    if "ID_VENDOR_ID=" in line and RPLIDAR_VID in line:
                        return dev
            except Exception:
                continue
        return config.LIDAR_PORT

    def setup(self):
        """Connect to RPLIDAR and calibrate. Soft-fails if unavailable."""
        try:
            port_path = self._find_port()
            self._serial_port = serial.Serial(
                port_path,
                baudrate=config.LIDAR_BAUD,
                timeout=1.0,
            )
            # Stop any ongoing scan and reset
            self._send_cmd(STOP_CMD)
            time.sleep(0.1)
            self._serial_port.reset_input_buffer()
            self._send_cmd(RESET_CMD)
            time.sleep(1.0)
            self._serial_port.reset_input_buffer()
            print(f"LiDAR: connected on {port_path}")
        except Exception as e:
            print(f"LiDAR: not available ({e}), running without footstep detection")
            self._serial_port = None
            return

        self.calibrate()

        self._running = True
        self._thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._thread.start()

    def calibrate(self):
        """Capture background scan (room must be empty). Stops scan thread first."""
        if self._serial_port is None:
            return

        # Stop scan thread if running
        if self._running:
            self._running = False
            self._thread.join(timeout=2.0)
            self._thread = None

        print("LiDAR: calibrating background...")

        # Start scan
        self._send_cmd(STOP_CMD)
        time.sleep(0.1)
        self._serial_port.reset_input_buffer()
        self._start_scan()

        # Settle
        settle_end = time.monotonic() + config.LIDAR_BG_SETTLE_SEC
        for scan in self._iter_scans_internal():
            if time.monotonic() >= settle_end:
                break

        frames = []
        for scan in self._iter_scans_internal():
            frames.append(self._scan_to_polar(scan))
            if len(frames) >= config.LIDAR_BG_FRAMES:
                break

        # Stop scan for now (scan_loop will restart it)
        self._send_cmd(STOP_CMD)
        time.sleep(0.1)
        self._serial_port.reset_input_buffer()

        # Compute median distance per angle bin (1° bins)
        bins = np.full(360, np.nan)
        for angle_bin in range(360):
            values = []
            for frame in frames:
                if angle_bin in frame:
                    values.append(frame[angle_bin])
            if values:
                bins[angle_bin] = np.median(values)

        self._background = bins
        self._clusters.clear()
        print("LiDAR: background learned")

        # Restart scan thread
        self._running = True
        self._thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._thread.start()

    def get_step_events(self) -> list[StepEvent]:
        """Drain and return pending step events (thread-safe)."""
        with self._lock:
            events = list(self._event_queue)
            self._event_queue.clear()
        return events

    def get_debug_state(self):
        """Return snapshot of clusters and background for visualization."""
        with self._lock:
            clusters = {cid: ClusterState(
                id=cs.id,
                centroid=cs.centroid,
                prev_centroid=cs.prev_centroid,
                state=cs.state,
                frame_count=cs.frame_count,
                fired_count=cs.fired_count,
                absent_count=cs.absent_count,
            ) for cid, cs in self._clusters.items()}
        bg = self._background.copy() if self._background is not None else None
        return clusters, bg

    def close(self):
        """Stop scanning and disconnect."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._serial_port:
            try:
                self._send_cmd(STOP_CMD)
                time.sleep(0.1)
                self._serial_port.close()
            except Exception:
                pass
            print("LiDAR: stopped")

    # ── Serial protocol ──────────────────────────────────────────────────

    def _send_cmd(self, cmd):
        """Send a command byte to the RPLIDAR."""
        self._serial_port.write(bytes([SYNC_BYTE, cmd]))

    def _start_scan(self):
        """Send scan command and consume the response descriptor."""
        self._send_cmd(SCAN_CMD)
        # Read 7-byte response descriptor
        desc = self._serial_port.read(DESCRIPTOR_LEN)
        if len(desc) < DESCRIPTOR_LEN:
            raise RuntimeError("LiDAR: no response descriptor")

    def _iter_scans_internal(self):
        """Yield complete scans as lists of (quality, angle, distance)."""
        scan = []
        while True:
            # Each measurement is 5 bytes
            raw = self._serial_port.read(5)
            if len(raw) < 5:
                continue

            # Parse measurement packet
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

    # ── Scan processing ──────────────────────────────────────────────────

    def _scan_to_polar(self, scan):
        """Convert raw scan to dict of {angle_bin: distance_mm}."""
        result = {}
        for quality, angle, distance in scan:
            if distance <= 0:
                continue
            bin_idx = int(angle) % 360
            # Keep closest reading per bin
            if bin_idx not in result or distance < result[bin_idx]:
                result[bin_idx] = distance
        return result

    def _is_rear_arc(self, angle_deg):
        """True if angle is in the rear (wall-facing) masked arc."""
        half = config.LIDAR_MASK_REAR_DEG
        diff = abs((angle_deg - 180 + 180) % 360 - 180)
        return diff <= half

    def _polar_to_xy(self, angle_deg, dist_mm):
        """Convert polar to Cartesian. 0°=into gallery, +X=viewer's right."""
        rad = math.radians(angle_deg)
        x = dist_mm * math.sin(rad)
        y = dist_mm * math.cos(rad)
        return (x, y)

    def _scan_loop(self):
        """Background thread: scan, detect foreground, track clusters."""
        try:
            self._start_scan()
            for scan in self._iter_scans_internal():
                if not self._running:
                    break
                self._process_scan(scan)
        except Exception as e:
            print(f"LiDAR scan error: {e}")
            self.needs_restart = True

    def _process_scan(self, scan):
        """Process one scan: background subtract, cluster, track steps."""
        if self._background is None:
            return

        polar = self._scan_to_polar(scan)

        # Foreground detection
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
        """Simple single-linkage clustering of 2D points."""
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
        """Four-state machine for each tracked cluster."""
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
                    id=cid,
                    centroid=(cx, cy),
                    prev_centroid=(cx, cy),
                    state='armed',
                    frame_count=1,
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
        """Per-cluster four-state step detection."""
        if cs.state == 'armed':
            if present and cs.frame_count >= config.LIDAR_STEP_MIN_FRAMES:
                dx = cs.centroid[0] - cs.prev_centroid[0]
                dy = cs.centroid[1] - cs.prev_centroid[1]
                vel = math.sqrt(dx * dx + dy * dy)
                if vel >= config.LIDAR_VELOCITY_MIN_MM or cs.frame_count == 1:
                    event = StepEvent(x_mm=cs.centroid[0], y_mm=cs.centroid[1])
                    with self._lock:
                        self._event_queue.append(event)
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
