import numpy as np
import cv2
import config


class KinectCapture:
    def __init__(self, output_width: int, output_height: int):
        self._device = None
        self._enabled = True
        self._output_width = output_width
        self._output_height = output_height
        self._last_mask = np.zeros(
            (output_height, output_width), dtype=np.float32
        )
        self._last_depth_vis = np.zeros(
            (output_height, output_width, 3), dtype=np.uint8
        )
        self._background = None
        self._bg_frames_collected = 0
        self._bg_frames_needed = 30  # Collect 1 second of frames for background
        self._bg_threshold_mm = 150  # Person must be this much closer than background
        self._try_open()

    def _try_open(self):
        try:
            from pyk4a import PyK4A, Config as K4AConfig, ColorResolution, DepthMode, FPS

            depth_modes = {
                "WFOV_2X2BINNED": DepthMode.WFOV_2X2BINNED,
                "WFOV_UNBINNED": DepthMode.WFOV_UNBINNED,
                "NFOV_UNBINNED": DepthMode.NFOV_UNBINNED,
            }
            fps_modes = {15: FPS.FPS_15, 30: FPS.FPS_30}

            k4a_config = K4AConfig(
                color_resolution=ColorResolution.OFF,
                depth_mode=depth_modes.get(
                    config.DEPTH_MODE, DepthMode.WFOV_2X2BINNED
                ),
                camera_fps=fps_modes.get(config.COLOR_FPS, FPS.FPS_30),
                synchronized_images_only=False,
            )
            self._device = PyK4A(k4a_config)
            self._device.start()
            print("Kinect: connected")
            print("Kinect: learning background (hold still)...")
        except Exception as e:
            print(f"Kinect: not available ({e}), using blank mask")
            self._device = None

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = value

    @property
    def depth_vis(self):
        return self._last_depth_vis

    @property
    def learning_background(self):
        return self._background is None and self._device is not None

    def capture(self):
        if not self._enabled or self._device is None:
            return self._last_mask

        try:
            cap = self._device.get_capture()
            if cap.depth is None:
                return self._last_mask

            depth = cap.depth.astype(np.float32)

            # Build depth visualization
            depth_clipped = np.clip(depth, config.DEPTH_MIN_MM, config.DEPTH_MAX_MM)
            depth_norm = ((depth_clipped - config.DEPTH_MIN_MM)
                          / (config.DEPTH_MAX_MM - config.DEPTH_MIN_MM) * 255).astype(np.uint8)
            depth_norm[depth < config.DEPTH_MIN_MM] = 0
            depth_norm[depth > config.DEPTH_MAX_MM] = 0
            depth_color = cv2.applyColorMap(depth_norm, cv2.COLORMAP_TURBO)
            depth_color[depth < 1] = 0
            depth_color = cv2.flip(depth_color, -1)
            self._last_depth_vis = cv2.resize(
                depth_color,
                (self._output_width, self._output_height),
                interpolation=cv2.INTER_LINEAR,
            )

            # Background learning phase
            if self._background is None:
                if self._bg_frames_collected == 0:
                    self._bg_accumulator = np.zeros_like(depth)
                    self._bg_count = np.zeros_like(depth)

                valid = depth > 0
                self._bg_accumulator[valid] += depth[valid]
                self._bg_count[valid] += 1
                self._bg_frames_collected += 1

                if self._bg_frames_collected >= self._bg_frames_needed:
                    # Average background depth
                    valid = self._bg_count > 0
                    self._background = np.zeros_like(depth)
                    self._background[valid] = self._bg_accumulator[valid] / self._bg_count[valid]
                    del self._bg_accumulator, self._bg_count
                    print("Kinect: background learned")

                return self._last_mask

            # Foreground detection
            valid_depth = (depth > config.DEPTH_MIN_MM) & (depth < config.DEPTH_MAX_MM)
            valid_bg = self._background > 0
            # Where background exists: person must be closer by threshold
            fg_with_bg = valid_depth & valid_bg & (depth < (self._background - self._bg_threshold_mm))
            # Where no background (reflective surfaces, dead zones): accept any valid depth
            fg_no_bg = valid_depth & ~valid_bg
            foreground = fg_with_bg | fg_no_bg

            mask = foreground.astype(np.uint8)

            # Floor removal
            h = mask.shape[0]
            floor_rows = int(
                h * config.FLOOR_SLICE_MM / (config.DEPTH_MAX_MM - config.DEPTH_MIN_MM)
            )
            if floor_rows > 0:
                mask[-floor_rows:, :] = 0

            # Morphological cleanup
            kernel = np.ones(
                (config.BLOB_DILATE_KERNEL, config.BLOB_DILATE_KERNEL), np.uint8
            )
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

            # Contour filtering
            contours, _ = cv2.findContours(
                mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            filtered = np.zeros_like(mask)
            thickness = getattr(config, "CONTOUR_THICKNESS", 0)
            draw_thickness = cv2.FILLED if thickness == 0 else thickness
            for c in contours:
                if cv2.contourArea(c) >= config.BLOB_MIN_AREA_PX:
                    cv2.drawContours(filtered, [c], -1, 1, thickness=draw_thickness)

            filtered = filtered.astype(np.float32)
            filtered = cv2.flip(filtered, -1)

            if config.SILHOUETTE_BLUR > 0:
                ksize = config.SILHOUETTE_BLUR * 2 + 1
                filtered = cv2.GaussianBlur(filtered, (ksize, ksize), 0)

            result = cv2.resize(
                filtered,
                (self._output_width, self._output_height),
                interpolation=cv2.INTER_LINEAR,
            )

            self._last_mask = result
            return result

        except Exception as e:
            print(f"Kinect capture error: {e}")
            return self._last_mask

    def calibrate(self):
        """Reset background learning (room must be empty)."""
        self._background = None
        self._bg_frames_collected = 0
        if self._device is not None:
            print("Kinect: recalibrating background (hold still)...")

    def get_nearest_blob_depth(self, x_mm: float, y_mm: float) -> float:
        """Map LiDAR x_mm to Kinect blob column and return nearest blob depth.

        Returns mean depth of nearest blob column, or AUDIO_VOL_FAR_MM if none.
        """
        if self._background is None or self._device is None:
            return config.AUDIO_VOL_FAR_MM

        # Map x_mm to kinect column
        half_width = config.LIDAR_GALLERY_WIDTH_MM / 2.0
        x_norm = (x_mm / half_width + 1.0) / 2.0 if half_width > 0 else 0.5
        col = int(x_norm * self._output_width)
        col = max(0, min(self._output_width - 1, col))

        # Search the last mask for nearest active column
        mask = self._last_mask
        search_radius = config.SLOT_MATCH_THRESHOLD_PX

        for offset in range(search_radius):
            for c in (col + offset, col - offset):
                if 0 <= c < self._output_width:
                    col_slice = mask[:, c]
                    if col_slice.max() > 0.5:
                        # Found active blob — estimate depth from background
                        active_rows = np.where(col_slice > 0.5)[0]
                        if len(active_rows) > 0 and self._background is not None:
                            # Use center active row in original depth frame
                            mid_row = active_rows[len(active_rows) // 2]
                            # Scale to depth frame coordinates
                            bg_h, bg_w = self._background.shape
                            dr = int(mid_row * bg_h / self._output_height)
                            dc = int(c * bg_w / self._output_width)
                            dr = max(0, min(bg_h - 1, dr))
                            dc = max(0, min(bg_w - 1, dc))
                            bg_depth = self._background[dr, dc]
                            if bg_depth > 0:
                                # Person is closer than background
                                return max(config.DEPTH_MIN_MM,
                                           bg_depth - self._bg_threshold_mm)
                        return config.AUDIO_VOL_NEAR_MM

        return config.AUDIO_VOL_FAR_MM

    def close(self):
        if self._device is not None:
            self._device.stop()
            print("Kinect: stopped")
