"""Top-down plan view of LiDAR footstep detection for debugging."""

import math
import time

import cv2
import numpy as np

import config

# State colors (BGR for cv2)
STATE_COLORS = {
    'armed':      (0, 220, 0),      # green
    'fired':      (0, 100, 255),    # orange
    'suppressed': (100, 100, 100),  # gray
    'cooling':    (220, 180, 0),    # cyan
}

# Flash duration for fired events
FLASH_DURATION = 0.3


class FootstepVis:
    def __init__(self, width, height):
        self.w = width
        self.h = height
        self._flashes = []  # (x_mm, y_mm, time, depth_mm)
        # View bounds in mm — centered on sensor
        self._x_range = config.LIDAR_GALLERY_WIDTH_MM * 0.7  # half-width shown
        self._y_range = config.LIDAR_GALLERY_WIDTH_MM * 0.8  # depth range shown
        self._cluster_depths = {}  # cluster_id -> depth_mm

    def record_trigger(self, x_mm, y_mm, depth_mm):
        """Record a fired step event for flash display."""
        self._flashes.append((x_mm, y_mm, time.monotonic(), depth_mm))

    def update_cluster_depth(self, cluster_id, depth_mm):
        """Store latest kinect depth for a cluster."""
        self._cluster_depths[cluster_id] = depth_mm

    def render(self, clusters, background, active_depths=None):
        """Render top-down plan view. Returns RGB numpy array (h, w, 3)."""
        img = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        img[:] = (20, 20, 25)  # dark background

        now = time.monotonic()
        # Prune old flashes
        self._flashes = [(x, y, t, d) for x, y, t, d in self._flashes
                         if now - t < FLASH_DURATION]

        cx, cy = self.w // 2, 60  # sensor at top center

        # Draw FOV arc and masked zone
        self._draw_fov(img, cx, cy, background)

        # Draw gallery width markers (pan range)
        self._draw_gallery_bounds(img, cx, cy)

        # Draw background outline
        if background is not None:
            self._draw_background(img, cx, cy, background)

        # Draw clusters
        for cid, cs in clusters.items():
            sx, sy = self._mm_to_screen(cs.centroid[0], cs.centroid[1], cx, cy)
            color = STATE_COLORS.get(cs.state, (255, 255, 255))
            radius = 12

            # Draw cluster dot
            cv2.circle(img, (sx, sy), radius, color, -1)
            cv2.circle(img, (sx, sy), radius + 2, color, 1)

            # State label
            label = cs.state.upper()
            if cs.state == 'fired':
                label += f" #{cs.fired_count}"
            cv2.putText(img, label, (sx + 18, sy - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)

            # Frame count
            cv2.putText(img, f"f:{cs.frame_count}", (sx + 18, sy + 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (140, 140, 140), 1, cv2.LINE_AA)

            # Depth from kinect
            depth = self._cluster_depths.get(cid)
            if depth is not None:
                depth_str = f"{int(depth)}mm"
                cv2.putText(img, depth_str, (sx + 18, sy + 24),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (180, 160, 100), 1, cv2.LINE_AA)

                # Volume bar
                vol_t = (depth - config.AUDIO_VOL_NEAR_MM) / max(1, config.AUDIO_VOL_FAR_MM - config.AUDIO_VOL_NEAR_MM)
                vol_t = max(0.0, min(1.0, vol_t))
                vol = config.AUDIO_VOL_MAX + (config.AUDIO_VOL_MIN - config.AUDIO_VOL_MAX) * vol_t
                bar_w = int(vol * 40)
                cv2.rectangle(img, (sx + 18, sy + 28), (sx + 18 + bar_w, sy + 34),
                              (0, int(vol * 200), int((1 - vol) * 200)), -1)

            # Pan indicator
            half_gw = config.LIDAR_GALLERY_WIDTH_MM / 2.0
            x_norm = -cs.centroid[0] / half_gw if half_gw > 0 else 0
            x_norm = max(-1.0, min(1.0, x_norm))
            pan = math.copysign(abs(x_norm) ** config.AUDIO_PAN_CURVE, x_norm) * config.AUDIO_PAN_RANGE
            pan_str = f"L{abs(pan):.1f}" if pan < -0.05 else f"R{pan:.1f}" if pan > 0.05 else "C"
            cv2.putText(img, pan_str, (sx + 18, sy + 48),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (100, 180, 180), 1, cv2.LINE_AA)

        # Draw flash markers for recent triggers
        for x_mm, y_mm, t, depth_mm in self._flashes:
            age = now - t
            alpha = 1.0 - age / FLASH_DURATION
            sx, sy = self._mm_to_screen(x_mm, y_mm, cx, cy)
            r = int(20 + age * 60)
            brightness = int(255 * alpha)
            cv2.circle(img, (sx, sy), r, (0, brightness, brightness), 2)
            # Show depth and computed volume for this trigger
            if alpha > 0.5:
                vol_t = (depth_mm - config.AUDIO_VOL_NEAR_MM) / max(1, config.AUDIO_VOL_FAR_MM - config.AUDIO_VOL_NEAR_MM)
                vol_t = max(0.0, min(1.0, vol_t))
                vol = config.AUDIO_VOL_MAX + (config.AUDIO_VOL_MIN - config.AUDIO_VOL_MAX) * vol_t
                cv2.putText(img, f"{int(depth_mm)}mm vol:{vol:.2f}", (sx - 40, sy - r - 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, brightness, brightness), 1, cv2.LINE_AA)

        # Draw sensor marker
        cv2.circle(img, (cx, cy), 6, (255, 255, 255), -1)
        cv2.putText(img, "SENSOR", (cx - 28, cy - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1, cv2.LINE_AA)

        # Legend
        self._draw_legend(img)

        # Title
        cv2.putText(img, "FOOTSTEP DETECTION", (20, 35),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2, cv2.LINE_AA)

        # Live tuning readout
        y_text = 65
        tuning = [
            ("Threshold ,/.", f"{config.LIDAR_THRESHOLD_MM}mm"),
            ("Velocity  ;/'", f"{config.LIDAR_VELOCITY_MIN_MM}mm"),
            ("Cluster   -/=", f"{config.LIDAR_CLUSTER_MIN_PTS} pts"),
            ("Vol Max   V/X", f"{config.AUDIO_VOL_MAX:.2f}"),
            ("Pan Range H/J", f"{config.AUDIO_PAN_RANGE:.2f}"),
            ("Fade Rate",     f"{config.FADE_RATE:.4f}"),
            ("Trace Int",     f"{config.TRACE_INTENSITY:.2f}"),
            ("Rate Limit",    f"{config.AUDIO_MIN_INTERVAL_MS}ms / {config.AUDIO_MAX_EVENTS_PER_SEC}/s"),
            ("Depth Mode O",  config.DEPTH_MODE),
        ]
        for label, val in tuning:
            cv2.putText(img, f"{label}: {val}", (20, y_text),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (140, 140, 140), 1, cv2.LINE_AA)
            y_text += 18
        cv2.putText(img, "P=print  W=save", (20, y_text + 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, (100, 100, 100), 1, cv2.LINE_AA)

        # Flip vertically for OpenGL texture upload
        img = cv2.flip(img, 0)
        # BGR to RGB
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        return img

    def _mm_to_screen(self, x_mm, y_mm, cx, cy):
        """Convert mm coordinates to screen pixel position. Sensor at top, gallery extends down."""
        sx = cx + int(-x_mm / self._x_range * (self.w // 2))
        sy = cy + int(y_mm / self._y_range * (self.h - 80))
        return (max(0, min(self.w - 1, sx)), max(0, min(self.h - 1, sy)))

    def _draw_fov(self, img, cx, cy, background):
        """Draw the FOV arc and masked rear zone."""
        max_r = int(self.h * 0.85)
        mask_half = config.LIDAR_MASK_REAR_DEG

        # Active FOV (faint green arc fill) — negate x to mirror for viewer perspective
        for angle in range(0, 360):
            if self._is_rear_arc(angle):
                continue
            rad = math.radians(angle)
            x1 = cx - int(max_r * math.sin(rad))
            y1 = cy + int(max_r * math.cos(rad))
            cv2.line(img, (cx, cy), (x1, y1), (15, 25, 15), 1)

        # Rear mask boundaries
        for edge_angle in [180 - mask_half, 180 + mask_half]:
            rad = math.radians(edge_angle)
            x1 = cx - int(max_r * math.sin(rad))
            y1 = cy + int(max_r * math.cos(rad))
            cv2.line(img, (cx, cy), (x1, y1), (0, 0, 80), 1)

        # Label the masked zone
        cv2.putText(img, "MASKED", (cx - 30, cy - 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 0, 100), 1, cv2.LINE_AA)

    def _is_rear_arc(self, angle_deg):
        half = config.LIDAR_MASK_REAR_DEG
        diff = abs((angle_deg - 180 + 180) % 360 - 180)
        return diff <= half

    def _draw_background(self, img, cx, cy, background):
        """Draw the background scan outline (room walls)."""
        pts = []
        for angle in range(360):
            if self._is_rear_arc(angle):
                continue
            dist = background[angle]
            if np.isnan(dist):
                continue
            sx, sy = self._mm_to_screen(
                dist * math.sin(math.radians(angle)),
                dist * math.cos(math.radians(angle)),
                cx, cy
            )
            pts.append((sx, sy))

        if len(pts) > 2:
            for i in range(len(pts) - 1):
                cv2.line(img, pts[i], pts[i + 1], (60, 50, 40), 1)

    def _draw_gallery_bounds(self, img, cx, cy):
        """Draw gallery width markers showing pan L/R mapping."""
        half_gw = config.LIDAR_GALLERY_WIDTH_MM / 2.0

        for sign, label in [(-1, "R"), (1, "L")]:
            x_mm = sign * half_gw
            sx, _ = self._mm_to_screen(x_mm, 0, cx, cy)
            cv2.line(img, (sx, 40), (sx, self.h - 40), (50, 50, 60), 1, cv2.LINE_AA)
            cv2.putText(img, f"{label} pan", (sx - 20, self.h - 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (80, 80, 100), 1, cv2.LINE_AA)

        # Center line
        cv2.line(img, (cx, 40), (cx, self.h - 60), (40, 40, 50), 1, cv2.LINE_AA)

    def _draw_legend(self, img):
        """Draw state color legend."""
        x, y = self.w - 180, 30
        for state, color in STATE_COLORS.items():
            cv2.circle(img, (x, y), 6, color, -1)
            cv2.putText(img, state.upper(), (x + 14, y + 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)
            y += 22
