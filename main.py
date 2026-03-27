import sys
import os
import time
import numpy as np
import pygame
import moderngl
import config
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

    clock = pygame.time.Clock()
    running = True

    print(f"Fossil running at {width}x{height}.")
    print(f"Effects (1-{len(EFFECT_NAMES)}): {', '.join(f'{i+1}={n}' for i, n in enumerate(EFFECT_NAMES))}")
    print("Keys: F=fullscreen, D=debug, K=freeze kinect,")
    print("  Up/Down=fade rate, [/]=trace intensity, S=screenshot, Q/Esc=quit")
    print("  V/X=volume up/down, C=recalibrate, L=footstep vis")

    # Show initial effect label
    renderer.set_effect(6)

    while running:
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
                    renderer.set_fade_rate(fade_rate)
                    print(f"Fade rate: {fade_rate:.4f}")
                elif event.key == pygame.K_DOWN:
                    fade_rate = max(0.9, fade_rate - 0.001)
                    renderer.set_fade_rate(fade_rate)
                    print(f"Fade rate: {fade_rate:.4f}")
                elif event.key == pygame.K_RIGHTBRACKET:
                    trace_intensity = min(1.0, trace_intensity + 0.05)
                    renderer.set_trace_intensity(trace_intensity)
                    print(f"Trace intensity: {trace_intensity:.2f}")
                elif event.key == pygame.K_LEFTBRACKET:
                    trace_intensity = max(0.0, trace_intensity - 0.05)
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
                elif event.key == pygame.K_k:
                    kinect.enabled = not kinect.enabled
                    print(f"Kinect frozen: {not kinect.enabled}")
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
        pygame.display.flip()
        clock.tick(config.TARGET_FPS)

    lidar.close()
    audio.close()
    kinect.close()
    pygame.quit()


if __name__ == "__main__":
    main()
