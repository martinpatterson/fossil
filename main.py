import json
import sys
import os
import time
import numpy as np
import pygame
import moderngl
import config

CONFIG_LOCAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_local.json")

# Tunable parameters that can be saved/loaded
TUNABLE_KEYS = [
    "LIDAR_THRESHOLD_MM", "LIDAR_CLUSTER_MIN_PTS", "LIDAR_VELOCITY_MIN_MM",
    "AUDIO_VOL_MAX", "AUDIO_PAN_RANGE", "FADE_RATE", "TRACE_INTENSITY",
    "DEPTH_MODE",
]

DEPTH_MODES = ["WFOV_2X2BINNED", "NFOV_2X2BINNED", "NFOV_UNBINNED"]  # WFOV_UNBINNED excluded (15fps only)


def load_local_config():
    """Load saved tuning overrides from config_local.json."""
    if os.path.exists(CONFIG_LOCAL_PATH):
        try:
            with open(CONFIG_LOCAL_PATH) as f:
                overrides = json.load(f)
            for key, val in overrides.items():
                if hasattr(config, key):
                    setattr(config, key, val)
            print(f"Config: loaded {len(overrides)} overrides from {CONFIG_LOCAL_PATH}")
        except Exception as e:
            print(f"Config: failed to load {CONFIG_LOCAL_PATH} — {e}")


def save_local_config():
    """Save current tunable values to config_local.json."""
    data = {key: getattr(config, key) for key in TUNABLE_KEYS}
    with open(CONFIG_LOCAL_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Config: saved to {CONFIG_LOCAL_PATH}")


def print_tuning():
    """Print current tunable values."""
    print("── Current tuning ──")
    for key in TUNABLE_KEYS:
        print(f"  {key} = {getattr(config, key)}")
    print("────────────────────")
from kinect import KinectCapture
from renderer import Renderer, EFFECT_NAMES
from audio import AudioEngine
from lidar import LidarTracker
from footstep_vis import FootstepVis


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    pygame.init()

    info = pygame.display.Info()
    screen_w, screen_h = info.current_w, info.current_h
    print(f"Display: {screen_w}x{screen_h}")

    # Render internally at WUXGA, display at native resolution
    render_w, render_h = config.OUTPUT_WIDTH, config.OUTPUT_HEIGHT

    if config.FULLSCREEN:
        width, height = screen_w, screen_h
        flags = pygame.OPENGL | pygame.DOUBLEBUF | pygame.FULLSCREEN
    else:
        width, height = config.OUTPUT_WIDTH, config.OUTPUT_HEIGHT
        flags = pygame.OPENGL | pygame.DOUBLEBUF

    pygame.display.set_mode((width, height), flags)
    pygame.display.set_caption("Fossil")
    pygame.mouse.set_visible(False)

    ctx = moderngl.create_context()
    ctx.viewport = (0, 0, width, height)

    load_local_config()

    renderer = Renderer(ctx, render_w, render_h, screen_size=(width, height))
    kinect = KinectCapture(render_w, render_h)

    lidar = LidarTracker()
    lidar.setup()
    audio = AudioEngine()
    audio.setup()

    footstep_vis = FootstepVis(render_w, render_h)
    show_footstep_vis = False

    fade_rate = config.FADE_RATE
    trace_intensity = config.TRACE_INTENSITY
    pixel_scale = 1.0

    clock = pygame.time.Clock()
    running = True

    print(f"Fossil running at {width}x{height}.")
    print(f"Effects (1-{len(EFFECT_NAMES)}): {', '.join(f'{i+1}={n}' for i, n in enumerate(EFFECT_NAMES))}")
    print("Keys: F=fullscreen, D=debug, K=freeze kinect,")
    print("  Up/Down=fade rate, [/]=trace intensity, S=screenshot, Q/Esc=quit")
    print("  V/X=volume up/down, C=recalibrate, L=footstep vis")
    print("  ,/.=threshold, ;/'=velocity, -/+=cluster pts, P=print, W=save")
    print("  N/M=pixel size, R=mute, O=depth mode. Pixel variants: G=Mono, I=Warm")

    # Show initial effect label
    renderer.set_effect(6)

    last_error_time = 0.0

    while running:
      try:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_q, pygame.K_ESCAPE):
                    running = False
                elif event.key == pygame.K_f:
                    config.FULLSCREEN = not config.FULLSCREEN
                    if config.FULLSCREEN:
                        pygame.display.set_mode(
                            (screen_w, screen_h),
                            pygame.OPENGL | pygame.DOUBLEBUF | pygame.FULLSCREEN,
                        )
                        pygame.mouse.set_visible(False)
                    else:
                        pygame.display.set_mode(
                            (config.OUTPUT_WIDTH, config.OUTPUT_HEIGHT),
                            pygame.OPENGL | pygame.DOUBLEBUF,
                        )
                        pygame.mouse.set_visible(True)
                elif event.key == pygame.K_UP:
                    fade_rate = min(1.0, fade_rate + 0.001)
                    config.FADE_RATE = fade_rate
                    renderer.set_fade_rate(fade_rate)
                    print(f"Fade rate: {fade_rate:.4f}")
                elif event.key == pygame.K_DOWN:
                    fade_rate = max(0.9, fade_rate - 0.001)
                    config.FADE_RATE = fade_rate
                    renderer.set_fade_rate(fade_rate)
                    print(f"Fade rate: {fade_rate:.4f}")
                elif event.key == pygame.K_RIGHTBRACKET:
                    trace_intensity = min(1.0, trace_intensity + 0.05)
                    config.TRACE_INTENSITY = trace_intensity
                    renderer.set_trace_intensity(trace_intensity)
                    print(f"Trace intensity: {trace_intensity:.2f}")
                elif event.key == pygame.K_LEFTBRACKET:
                    trace_intensity = max(0.0, trace_intensity - 0.05)
                    config.TRACE_INTENSITY = trace_intensity
                    renderer.set_trace_intensity(trace_intensity)
                    print(f"Trace intensity: {trace_intensity:.2f}")
                elif event.key == pygame.K_z:
                    renderer.debug_overlay = not renderer.debug_overlay
                    print(f"Debug overlay: {renderer.debug_overlay}")
                elif event.key == pygame.K_c:
                    print("Recalibrating — clear the room...")
                    kinect.calibrate()
                    lidar.calibrate()
                    print("Recalibration complete")
                elif event.key == pygame.K_v:
                    config.AUDIO_VOL_MAX = min(1.0, config.AUDIO_VOL_MAX + 0.05)
                    print(f"Audio volume max: {config.AUDIO_VOL_MAX:.2f}")
                elif event.key == pygame.K_x:
                    config.AUDIO_VOL_MAX = max(0.1, config.AUDIO_VOL_MAX - 0.05)
                    print(f"Audio volume max: {config.AUDIO_VOL_MAX:.2f}")
                elif event.key == pygame.K_l:
                    show_footstep_vis = not show_footstep_vis
                    print(f"Footstep vis: {'ON' if show_footstep_vis else 'OFF'}")
                elif event.key == pygame.K_j:
                    config.AUDIO_PAN_RANGE = max(0.0, config.AUDIO_PAN_RANGE - 0.05)
                    print(f"Audio pan range: {config.AUDIO_PAN_RANGE:.2f}")
                elif event.key == pygame.K_h:
                    config.AUDIO_PAN_RANGE = min(1.0, config.AUDIO_PAN_RANGE + 0.05)
                    print(f"Audio pan range: {config.AUDIO_PAN_RANGE:.2f}")
                elif event.key == pygame.K_k:
                    kinect.enabled = not kinect.enabled
                    print(f"Kinect frozen: {not kinect.enabled}")
                elif event.key == pygame.K_PERIOD:
                    config.LIDAR_THRESHOLD_MM = min(200, config.LIDAR_THRESHOLD_MM + 10)
                    print(f"LiDAR threshold: {config.LIDAR_THRESHOLD_MM}mm")
                elif event.key == pygame.K_COMMA:
                    config.LIDAR_THRESHOLD_MM = max(10, config.LIDAR_THRESHOLD_MM - 10)
                    print(f"LiDAR threshold: {config.LIDAR_THRESHOLD_MM}mm")
                elif event.key == pygame.K_QUOTE:
                    config.LIDAR_VELOCITY_MIN_MM = min(100, config.LIDAR_VELOCITY_MIN_MM + 5)
                    print(f"LiDAR velocity min: {config.LIDAR_VELOCITY_MIN_MM}mm")
                elif event.key == pygame.K_SEMICOLON:
                    config.LIDAR_VELOCITY_MIN_MM = max(0, config.LIDAR_VELOCITY_MIN_MM - 5)
                    print(f"LiDAR velocity min: {config.LIDAR_VELOCITY_MIN_MM}mm")
                elif event.key == pygame.K_EQUALS:
                    config.LIDAR_CLUSTER_MIN_PTS = min(10, config.LIDAR_CLUSTER_MIN_PTS + 1)
                    print(f"LiDAR cluster min pts: {config.LIDAR_CLUSTER_MIN_PTS}")
                elif event.key == pygame.K_MINUS:
                    config.LIDAR_CLUSTER_MIN_PTS = max(1, config.LIDAR_CLUSTER_MIN_PTS - 1)
                    print(f"LiDAR cluster min pts: {config.LIDAR_CLUSTER_MIN_PTS}")
                elif event.key == pygame.K_n:
                    pixel_scale = max(0.25, pixel_scale - 0.25)
                    renderer.set_pixel_scale(pixel_scale)
                    print(f"Pixel scale: {pixel_scale:.2f}")
                elif event.key == pygame.K_m:
                    pixel_scale = min(5.0, pixel_scale + 0.25)
                    renderer.set_pixel_scale(pixel_scale)
                    print(f"Pixel scale: {pixel_scale:.2f}")
                elif event.key == pygame.K_o:
                    idx = DEPTH_MODES.index(config.DEPTH_MODE) if config.DEPTH_MODE in DEPTH_MODES else 0
                    idx = (idx + 1) % len(DEPTH_MODES)
                    config.DEPTH_MODE = DEPTH_MODES[idx]
                    save_local_config()
                    print(f"Depth mode: {config.DEPTH_MODE} — restarting...")
                    kinect.needs_restart = True
                elif event.key == pygame.K_r:
                    audio.muted = not audio.muted
                    print(f"Audio: {'MUTED' if audio.muted else 'unmuted'}")
                elif event.key == pygame.K_p:
                    print_tuning()
                elif event.key == pygame.K_w:
                    save_local_config()
                elif event.key == pygame.K_s:
                    pixels = ctx.screen.read(components=3)
                    img = pygame.image.fromstring(
                        pixels, (width, height), "RGB"
                    )
                    img = pygame.transform.flip(img, False, True)
                    fname = f"screenshot_{int(time.time())}.png"
                    pygame.image.save(img, fname)
                    print(f"Screenshot saved: {fname}")
                elif pygame.K_1 <= event.key <= pygame.K_9:
                    idx = event.key - pygame.K_1
                    if idx < len(EFFECT_NAMES):
                        renderer.set_effect(idx)
                elif event.key == pygame.K_0:
                    idx = 9
                    if idx < len(EFFECT_NAMES):
                        renderer.set_effect(idx)
                elif pygame.K_a <= event.key <= pygame.K_z:
                    idx = 10 + (event.key - pygame.K_a)
                    if idx < len(EFFECT_NAMES):
                        renderer.set_effect(idx)

        mask = kinect.capture()
        renderer.update_silhouette(mask)

        renderer.update_debug(kinect.depth_vis, mask)

        # Audio/LiDAR integration — use LiDAR y_mm directly for volume
        step_events = lidar.get_step_events()
        for step in step_events:
            audio.trigger(step.x_mm, step.y_mm)
            footstep_vis.record_trigger(step.x_mm, step.y_mm, step.y_mm)
        audio.update()

        if show_footstep_vis:
            clusters, bg = lidar.get_debug_state()
            # Use LiDAR y_mm directly for depth display
            for cid, cs in clusters.items():
                footstep_vis.update_cluster_depth(cid, cs.centroid[1])
            vis_frame = footstep_vis.render(clusters, bg)
            renderer.blit_rgb(vis_frame)
        else:
            renderer.render()
        # Check if sensors need a full process restart
        if kinect.needs_restart or lidar.needs_restart:
            print("Sensor failure — restarting process...")
            break

        pygame.display.flip()
        clock.tick(config.TARGET_FPS)

      except Exception as e:
        now = time.monotonic()
        if now - last_error_time > 5.0:
            print(f"Main loop error: {e}")
            last_error_time = now

    restart = kinect.needs_restart or lidar.needs_restart
    lidar.close()
    audio.close()
    kinect.close()
    pygame.quit()
    if restart:
        sys.exit(75)  # signal wrapper to restart


if __name__ == "__main__":
    main()
