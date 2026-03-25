# Fossil — Audio Extension Handoff
## Addendum to existing Claude Code project

---

## Critical: Preserve all existing code

The existing project has a working Kinect pipeline, background subtraction,
shader effects, and room calibration. **Do not modify any of that.**
This handoff adds two new files (`audio.py`, `lidar.py`) and makes
minimal additions to `config.py` and `main.py` only. If in doubt, add
rather than change.

---

## New Hardware

**RPLIDAR C1** — wall-mounted 15–20cm above floor, directly below the
Kinect, scanning horizontally into the gallery. USB serial connection.

**Setup on remote host (one time):**
```bash
echo 'KERNEL=="ttyUSB*", MODE="0666"' | sudo tee /etc/udev/rules.d/99-rplidar.rules
sudo udevadm control --reload-rules
```

**Install into existing `.venv`:**
```bash
source .venv/bin/activate
pip install rplidarc1 pyserial sounddevice soundfile scipy
```

**Verify detection:**
```bash
ls /dev/ttyUSB*
python3 -c "import sounddevice as sd; print(sd.query_devices())"
```

Audio output: 3.5mm stereo jack on the NUC → two powered floor-level
speakers at left and right edges of the screen. No USB audio interface.

---

## What the extension does

Each footstep detected by the RPLIDAR triggers a plastic-crush sound
(recordings provided by Tyler Burton). Each event is spatialised:

- **Pan (L/R):** LiDAR X position of foot → stereo pan
- **Volume:** Kinect depth of nearest blob → louder when closer to screen

Three protection layers prevent excessive triggering:
1. Minimum 120ms between any two events
2. Maximum 4 events per second rolling cap
3. Scene goes fully silent 2 seconds after last movement, with 0.5s fade

---

## New config values — add to existing `config.py`

```python
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
AUDIO_DEVICE            = None      # None = system default stereo out
AUDIO_SAMPLE_RATE       = 44100
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
```

---

## New file: `lidar.py`

Runs a background scan thread. Detects footsteps as transient foreground
clusters using four interlocked mechanisms:

1. **Background subtraction** — median of 30 calibration frames removes
   static room. Only pixels *closer* than background by >80mm count.
2. **Rear arc masking** — ±60° around 180° (screen wall) discarded.
3. **Velocity gate** — cluster centroid must move ≥20mm/scan or ignored.
4. **Four-state machine per cluster:**
   - `armed` → fires StepEvent on first confirmed appearance
   - `fired` → suppresses re-trigger while foot is still present
   - `suppressed` → ignores cluster if it stayed >8 frames (furniture)
   - `cooling` → must be absent 3 frames before returning to `armed`

```python
@dataclass
class StepEvent:
    x_mm: float    # negative=viewer's left, positive=viewer's right
    y_mm: float    # distance from sensor into gallery

@dataclass
class ClusterState:
    id:            int
    centroid:      tuple   # (x_mm, y_mm)
    prev_centroid: tuple
    state:         str     # 'armed'|'fired'|'suppressed'|'cooling'
    frame_count:   int
    fired_count:   int
    absent_count:  int

class LidarTracker:
    def setup(self): ...         # connect, calibrate, start scan thread
    def calibrate(self): ...     # 30-frame median background, room empty
    def get_step_events(self) -> list[StepEvent]: ...  # drain queue
    def close(self): ...

    # Test pattern when hardware not connected:
    # Emits a step event every ~600ms at a slowly moving x position
```

**Coordinate convention:** sensor at origin, 0° = into gallery.
X positive = viewer's right. No additional flip needed — sensor faces
same direction as viewer.

---

## New file: `audio.py`

```python
class AudioEngine:
    def setup(self): ...
    # Load all clips, resample to AUDIO_SAMPLE_RATE, open stereo stream

    def trigger(self, x_mm: float, depth_mm: float):
    # Rate limiting (all must pass):
    #   1. now - last_event > AUDIO_MIN_INTERVAL_MS
    #   2. events in last 1s < AUDIO_MAX_EVENTS_PER_SEC
    # Then:
    #   Select clip (weighted random: 40% Group A, 60% Group B)
    #   Pan  = sign(x_norm) * |x_norm|^2.0 * 0.7   (equal-power law)
    #   Vol  = lerp(VOL_MAX, VOL_MIN, depth normalised near→far)
    #   Pitch shift ±8% via scipy resample
    #   Add to active voice list

    def update(self):
    # Call every frame from main loop
    # If no step events for AUDIO_IDLE_TIMEOUT_SEC:
    #   Ramp master_vol → 0 over AUDIO_FADE_OUT_SEC
    #   Clear all voices when master_vol == 0
    # On resume: ramp master_vol → 1.0 over 0.1s

    def _audio_callback(self, outdata, frames, time, status):
    # Mix active voices, apply master_vol, soft clip with tanh

    def close(self): ...
```

---

## Changes to `main.py` — additions only

```python
# Imports (add)
from audio import AudioEngine
from lidar import LidarTracker

# Setup (add after existing kinect.calibrate() call)
lidar = LidarTracker()
lidar.setup()          # connects and calibrates LiDAR
audio = AudioEngine()
audio.setup()

# Extend existing C key recalibration handler:
if event.key == pygame.K_c:
    renderer.show_message("Recalibrating — clear the room...")
    kinect.calibrate()      # existing
    lidar.calibrate()       # add this line
    renderer.clear_message()

# New keyboard controls (add to existing handler):
if event.key == pygame.K_v:
    config.AUDIO_VOL_MAX = min(1.0, config.AUDIO_VOL_MAX + 0.05)
if event.key == pygame.K_x:
    config.AUDIO_VOL_MAX = max(0.1, config.AUDIO_VOL_MAX - 0.05)
if event.key == pygame.K_k:
    config.AUDIO_PAN_RANGE = min(1.0, config.AUDIO_PAN_RANGE + 0.05)
if event.key == pygame.K_j:
    config.AUDIO_PAN_RANGE = max(0.0, config.AUDIO_PAN_RANGE - 0.05)

# Main loop (add after existing kinect/renderer calls):
step_events = lidar.get_step_events()
for step in step_events:
    depth_mm = kinect.get_nearest_blob_depth(step.x_mm, step.y_mm)
    audio.trigger(step.x_mm, depth_mm)
audio.update()   # idle detection — every frame

# Cleanup (add to existing shutdown):
lidar.close()
audio.close()
```

---

## New method needed in existing `kinect.py`

```python
def get_nearest_blob_depth(self, x_mm: float, y_mm: float) -> float:
    """
    Map LiDAR x_mm to Kinect blob column:
      kinect_col = (x_mm / (LIDAR_GALLERY_WIDTH_MM/2) + 1) / 2 * OUTPUT_WIDTH
    Return mean_depth of nearest blob, or AUDIO_VOL_FAR_MM if none.
    This is a lightweight addition — does not touch existing blob tracking.
    """
```

---

## Audio assets

Place 8 mono WAV/AIFF/MP3 files (provided by Tyler) in `assets/audio/`.
Filenames must match AUDIO_GROUP_A and AUDIO_GROUP_B lists in config,
or update those lists to match whatever filenames Tyler provides.

---

## Keyboard controls added

| Key | Action |
|-----|--------|
| `C` | Recalibrate Kinect + LiDAR (extends existing — room must be empty) |
| `V` / `X` | Volume up / down |
| `K` / `J` | Pan width wider / narrower |

All existing controls unchanged.

---

## Startup sequence

```
1. Existing calibration message shows
2. kinect.calibrate()    — existing, unchanged
3. lidar.calibrate()     — new, adds ~3s to startup (room must be empty)
4. Existing render loop starts
5. Audio fires on first detected footstep
```

---

## Run command (unchanged)

```bash
ssh martin@192.168.68.88 \
  "cd ~/fossil-optionA && source .venv/bin/activate && DISPLAY=:0 python main.py"
```
