"""RPLIDAR C1 footstep detector for Fossil installation."""

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

import config


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


class LidarTracker:
    def __init__(self):
        self._lidar = None
        self._background = None        # median distances per angle bin
        self._running = False
        self._thread = None
        self._event_queue = deque()
        self._lock = threading.Lock()
        self._clusters: dict[int, ClusterState] = {}
        self._next_cluster_id = 0

    def setup(self):
        """Connect to RPLIDAR and calibrate. Soft-fails if unavailable."""
        try:
            from rplidarc1 import RPLidarC1
            self._lidar = RPLidarC1(config.LIDAR_PORT, baudrate=config.LIDAR_BAUD)
            self._lidar.connect()
            print("LiDAR: connected")
        except Exception as e:
            print(f"LiDAR: not available ({e}), running without footstep detection")
            self._lidar = None
            return

        self.calibrate()

        self._running = True
        self._thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._thread.start()

    def calibrate(self):
        """Capture background scan (room must be empty)."""
        if self._lidar is None:
            return

        print("LiDAR: calibrating background...")
        frames = []
        scan_iter = self._lidar.iter_scans()
        # Settle
        settle_end = time.monotonic() + config.LIDAR_BG_SETTLE_SEC
        for scan in scan_iter:
            if time.monotonic() >= settle_end:
                break

        for scan in scan_iter:
            frames.append(self._scan_to_polar(scan))
            if len(frames) >= config.LIDAR_BG_FRAMES:
                break

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

    def get_step_events(self) -> list[StepEvent]:
        """Drain and return pending step events (thread-safe)."""
        with self._lock:
            events = list(self._event_queue)
            self._event_queue.clear()
        return events

    def close(self):
        """Stop scanning and disconnect."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._lidar:
            try:
                self._lidar.disconnect()
            except Exception:
                pass
            print("LiDAR: stopped")

    # ── Internal ──────────────────────────────────────────────────────────

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
        # Rear = ±LIDAR_MASK_REAR_DEG around 180°
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
            for scan in self._lidar.iter_scans():
                if not self._running:
                    break
                self._process_scan(scan)
        except Exception as e:
            print(f"LiDAR scan error: {e}")

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
            # Foreground = closer than background by threshold
            if dist < bg_dist - config.LIDAR_THRESHOLD_MM:
                x, y = self._polar_to_xy(angle_bin, dist)
                fg_points.append((x, y))

        # Cluster foreground points (simple distance-based)
        clusters = self._cluster_points(fg_points)

        # Track clusters through state machine
        self._update_tracking(clusters)

    def _cluster_points(self, points):
        """Simple single-linkage clustering of 2D points."""
        if not points:
            return []

        points = sorted(points, key=lambda p: math.atan2(p[1], p[0]))
        clusters = []
        current = [points[0]]

        for p in points[1:]:
            # Check distance to last point in current cluster
            dx = p[0] - current[-1][0]
            dy = p[1] - current[-1][1]
            if math.sqrt(dx * dx + dy * dy) < 200:  # 200mm linkage
                current.append(p)
            else:
                clusters.append(current)
                current = [p]
        clusters.append(current)

        # Filter by size
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
        match_radius = 300  # mm

        # Match new centroids to existing clusters
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
                # New cluster
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

        # Handle absent clusters
        absent_ids = []
        for cid, cs in self._clusters.items():
            if cid not in matched_ids:
                cs.absent_count += 1
                self._step_state_machine(cs, present=False)
                # Remove clusters absent too long
                if cs.absent_count > config.LIDAR_REARM_FRAMES + 5:
                    absent_ids.append(cid)

        for cid in absent_ids:
            del self._clusters[cid]

    def _step_state_machine(self, cs: ClusterState, present: bool):
        """Per-cluster four-state step detection."""
        if cs.state == 'armed':
            if present and cs.frame_count >= config.LIDAR_STEP_MIN_FRAMES:
                # Check velocity
                dx = cs.centroid[0] - cs.prev_centroid[0]
                dy = cs.centroid[1] - cs.prev_centroid[1]
                vel = math.sqrt(dx * dx + dy * dy)
                if vel >= config.LIDAR_VELOCITY_MIN_MM or cs.frame_count == 1:
                    # Fire step event
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
                # Reset if it reappeared during cooldown
                cs.absent_count = 0
            elif cs.absent_count >= config.LIDAR_REARM_FRAMES:
                cs.state = 'armed'
                cs.frame_count = 0
