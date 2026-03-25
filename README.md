# Fossil

Interactive art installation combining Kinect depth sensing, LiDAR footstep detection, and GPU shader effects. Visitors' silhouettes drive evolving visual patterns on a fossil photograph while their footsteps trigger spatially-positioned plastic-crush sounds.

## Hardware

- **Azure Kinect** — overhead depth camera, WFOV 2x2 binned @ 30fps
- **RPLIDAR C1** — wall-mounted horizontal scanner, 15–20cm above floor
- **Intel NUC** — runs the application
- **LG TV** — primary display
- **Two powered speakers** — floor-level, left/right of screen, 3.5mm stereo

## How It Works

### Visual Pipeline

1. **Kinect** captures depth, learns a background model (30 frames), and extracts foreground silhouettes via background subtraction (150mm threshold), floor removal, morphological cleanup, and contour filtering.
2. **GPU renderer** composites the silhouette onto a fossil photograph using ping-pong framebuffers for persistence. Each frame, the persistence buffer decays by a configurable fade rate and fresh silhouette data is stamped in — creating motion trails and ghosting.
3. **15 shader effects** transform the result in different ways (see below).

### Audio Pipeline

1. **RPLIDAR** scans the gallery floor in a background thread. A four-state cluster machine (`armed` → `fired` → `suppressed` → `cooling`) distinguishes real footsteps from furniture by tracking cluster lifetime and velocity.
2. Each **step event** carries an (x, y) position. The x-coordinate drives stereo panning (equal-power law, quadratic curve). The Kinect depth at that position drives volume (closer = louder).
3. **Clips** are selected randomly: 40% heavy crush sounds (Group A), 60% lighter sounds (Group B). Each is pitch-shifted ±8% for variation.
4. **Rate limiting**: 120ms minimum interval, 4 events/sec cap. Scene goes silent 2s after last movement with a 0.5s fade-out.

Both subsystems soft-fail — the app runs without Kinect (blank mask), without LiDAR (no audio triggers), and without audio clips (silent).

## Effects

| Key | Effect | Description |
|-----|--------|-------------|
| 1 | Smudge | Darkening ghost trail via subtraction |
| 2 | Bleach | Lightening ghost trail via addition |
| 3 | Edge Trace | Sobel edge detection on silhouette boundaries |
| 4 | Color Shift | Hue rotation driven by accumulation |
| 5 | Strata | Banded horizontal fade with blue-to-orange tint |
| 6 | Erosion | Inverted silhouette blend |
| 7 | Pixel | Depth-adaptive pixelation (4–40px blocks) |
| 8 | Dots | Jittered circles on a coarse grid |
| 9 | Depth Dots | Dots colored by depth (Turbo colormap) |
| 10 | Depth | Depth color overlay |
| 11 | Lens 1 | Moderate glass distortion via silhouette normals |
| 12 | Lens 2 | Extreme distortion (×200 texel spread) |
| 13 | Lens 3 | Gentle distortion |
| 14 | Topo 1 | Depth-colored contour lines |
| 15 | 3D | Chromatic aberration glass with specular highlights |

## Controls

| Key | Action |
|-----|--------|
| 1–9, 0, A–F | Select effect |
| Up / Down | Fade rate (trail persistence) |
| [ / ] | Trace intensity |
| C | Recalibrate Kinect + LiDAR (room must be empty) |
| V / X | Audio volume up / down |
| J | Narrow stereo pan |
| K | Freeze/unfreeze Kinect |
| Z | Toggle debug depth overlay |
| F | Toggle fullscreen |
| S | Screenshot |
| Q / Esc | Quit |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install rplidarc1 pyserial sounddevice soundfile scipy
```

RPLIDAR permissions (one-time):
```bash
echo 'KERNEL=="ttyUSB*", MODE="0666"' | sudo tee /etc/udev/rules.d/99-rplidar.rules
sudo udevadm control --reload-rules
```

PortAudio system library:
```bash
sudo apt install libportaudio2
```

Place 8 audio clips in `assets/audio/` (filenames configured in `config.py`).

## Run

```bash
source .venv/bin/activate
DISPLAY=:0 python main.py
```

## Architecture

```
main.py            Entry point, event loop, glue
kinect.py          Depth capture, background subtraction, blob extraction
renderer.py        OpenGL compositing, ping-pong FBOs, 15 effect modes
lidar.py           RPLIDAR scan thread, cluster tracking, step detection
audio.py           Spatial audio engine, rate limiting, idle fade
config.py          All tunable parameters
shaders/
  composite.frag   15 visual effects
  fullscreen.vert  Fullscreen quad
  passthrough.frag Screen blit
  debug.frag       Debug/label overlay
assets/
  fossil.png       Base photograph
  audio/           Footstep sound clips
```

## Design Notes

- **Ping-pong FBOs** alternate read/write targets each frame to avoid GPU read-after-write hazards on the persistence buffer.
- **LiDAR state machine** suppresses clusters present >8 frames (furniture) and requires 3 frames of absence before re-arming — balancing responsiveness with false-trigger rejection.
- **Equal-power pan law** (cos/sin gains) maintains consistent perceived loudness across the stereo field.
- **Kinect↔LiDAR handoff** for volume: LiDAR gives spatial position, Kinect gives depth. `get_nearest_blob_depth()` maps LiDAR x to Kinect column and searches ±200px for an active silhouette.
- **Soft clipping** via `tanh` on the audio mix prevents digital clipping when multiple voices overlap.
- Internal render resolution is independent of display resolution — GPU scales the final blit.
