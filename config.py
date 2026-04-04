# --- Display ---
OUTPUT_WIDTH  = 1920
OUTPUT_HEIGHT = 1080
FULLSCREEN    = True
TARGET_FPS    = 60

# --- Kinect ---
DEPTH_MODE    = "WFOV_2X2BINNED"
COLOR_FPS     = 30
DEPTH_MIN_MM  = 500
DEPTH_MAX_MM  = 3000

# --- People Detection ---
FLOOR_SLICE_MM        = 200
BLOB_MIN_AREA_PX      = 2000
BLOB_DILATE_KERNEL    = 3
SILHOUETTE_BLUR       = 7
CONTOUR_THICKNESS     = 0          # 0 = filled silhouette

# --- Shader / Effect ---
FADE_RATE             = 0.99      # Per-frame decay (~4 sec visible trace at 60fps)
TRACE_INTENSITY       = 0.3       # Subtle per-frame stamp, builds up over ~1 sec of standing
TRACE_COLOR           = (0.0, 0.0, 0.0)   # Black trace (darken strata)

# ── RPLIDAR ────────────────────────────────────────────────────────────────
LIDAR_PORT              = "/dev/ttyUSB0"
LIDAR_BAUD              = 460800
LIDAR_MASK_REAR_DEG     = 60        # mask wall-facing arc
LIDAR_BG_FRAMES         = 30
LIDAR_BG_SETTLE_SEC     = 1.5
LIDAR_THRESHOLD_MM      = 80        # foreground sensitivity
LIDAR_CLUSTER_MIN_PTS   = 3
LIDAR_CLUSTER_MAX_PTS   = 40
LIDAR_STEP_MIN_FRAMES   = 1         # frames to confirm a step
LIDAR_STEP_MAX_FRAMES   = 8         # frames before treating as stationary
LIDAR_REARM_FRAMES      = 3         # frames absent before re-triggering
LIDAR_VELOCITY_MIN_MM   = 20        # min movement to count as a step
LIDAR_GALLERY_WIDTH_MM  = 4000      # physical gallery width for pan mapping

# ── Audio ──────────────────────────────────────────────────────────────────
AUDIO_DEVICE            = None    # None = system default (PipeWire → headphone)
AUDIO_SAMPLE_RATE       = 48000
AUDIO_CLIP_DIR          = "assets/audio"
AUDIO_GROUP_A           = ["fossil_A1_water_bottle_full.wav",
                            "fossil_A2_ribbed_bottle_full.wav",
                            "fossil_A3_food_tray_full.wav",
                            "fossil_A4_pill_bottle_full.wav"]
AUDIO_GROUP_B           = ["fossil_B1_bottle_cap_light.wav",
                            "fossil_B2_plastic_film_light.wav",
                            "fossil_B3_bottle_flex_light.wav",
                            "fossil_B4_straw_snap_light.wav"]
AUDIO_GROUP_A_WEIGHT    = 0.4       # probability of heavier crush clip
AUDIO_PAN_RANGE         = 0.7       # max stereo pan (0=centre, 1=hard)
AUDIO_PAN_CURVE         = 2.0       # >1 = gentle centre, wide at edges
AUDIO_VOL_NEAR_MM       = 800       # depth → max volume
AUDIO_VOL_FAR_MM        = 2800      # depth → min volume
AUDIO_VOL_MIN           = 0.25
AUDIO_VOL_MAX           = 1.0
AUDIO_PITCH_VARIANCE    = 0.08      # ±8% random pitch per event
AUDIO_MAX_EVENTS_PER_SEC = 4
AUDIO_MIN_INTERVAL_MS   = 120
AUDIO_IDLE_TIMEOUT_SEC  = 2.0
AUDIO_FADE_OUT_SEC      = 0.5
SLOT_MATCH_THRESHOLD_PX = 200
