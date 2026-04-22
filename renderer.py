import os
import time
import numpy as np
import moderngl
from PIL import Image, ImageDraw, ImageFont
import config


EFFECT_NAMES = [
    "Smudge",
    "Bleach",
    "Edge Trace",
    "Color Shift",
    "Strata",
    "Erosion",
    "Pixel",
    "Dots",
    "Depth Dots",
    "Depth",
    "Lens 1",
    "Lens 2",
    "Lens 3",
    "Topo 1",
    "3D",
    "Pixel Mono",
    "Pixel Inv",
    "Pixel Warm",
    "Pixel Depth",
    "Pixel Large",
    "Pixel Tiny",
]


class Renderer:
    def __init__(self, ctx: moderngl.Context, width: int, height: int, screen_size: tuple = None):
        self.ctx = ctx
        self.width = width
        self.height = height
        self.screen_w, self.screen_h = screen_size or (width, height)
        self.debug_overlay = False
        self.effect_mode = 0
        self._label_time = 0.0
        self._label_duration = 2.0

        # Load shaders
        shader_dir = os.path.join(os.path.dirname(__file__), "shaders")
        vert_src = open(os.path.join(shader_dir, "fullscreen.vert")).read()
        composite_src = open(os.path.join(shader_dir, "composite.frag")).read()
        passthrough_src = open(os.path.join(shader_dir, "passthrough.frag")).read()
        debug_src = open(os.path.join(shader_dir, "debug.frag")).read()

        self.composite_prog = ctx.program(
            vertex_shader=vert_src, fragment_shader=composite_src
        )
        self.passthrough_prog = ctx.program(
            vertex_shader=vert_src, fragment_shader=passthrough_src
        )
        self.debug_prog = ctx.program(
            vertex_shader=vert_src, fragment_shader=debug_src
        )

        # Fullscreen quad
        vertices = np.array(
            [
                -1.0, -1.0,  0.0, 0.0,
                 1.0, -1.0,  1.0, 0.0,
                -1.0,  1.0,  0.0, 1.0,
                 1.0,  1.0,  1.0, 1.0,
            ],
            dtype="f4",
        )
        self.vbo = ctx.buffer(vertices)
        self.composite_vao = ctx.vertex_array(
            self.composite_prog,
            [(self.vbo, "2f 2f", "in_position", "in_uv")],
        )
        self.passthrough_vao = ctx.vertex_array(
            self.passthrough_prog,
            [(self.vbo, "2f 2f", "in_position", "in_uv")],
        )
        self.debug_vao = ctx.vertex_array(
            self.debug_prog,
            [(self.vbo, "2f 2f", "in_position", "in_uv")],
        )

        # Label quad (top center strip)
        label_h = 0.12
        label_verts = np.array(
            [
                -0.4, 1.0 - label_h, 0.0, 0.0,
                 0.4, 1.0 - label_h, 1.0, 0.0,
                -0.4, 1.0,           0.0, 1.0,
                 0.4, 1.0,           1.0, 1.0,
            ],
            dtype="f4",
        )
        self.label_vbo = ctx.buffer(label_verts)
        self.label_vao = ctx.vertex_array(
            self.debug_prog,
            [(self.label_vbo, "2f 2f", "in_position", "in_uv")],
        )

        # Load background images from assets/backgrounds/ (or fall back to fossil.png)
        bg_dir = os.path.join(os.path.dirname(__file__), "assets", "backgrounds")
        self._bg_images = []
        if os.path.isdir(bg_dir):
            for f in sorted(os.listdir(bg_dir)):
                if f.lower().endswith((".png", ".jpg", ".jpeg")):
                    self._bg_images.append(os.path.join(bg_dir, f))
        if not self._bg_images:
            # Fallback to fossil.png / fossil2.png
            for name in ("fossil.png", "fossil2.png"):
                p = os.path.join(os.path.dirname(__file__), "assets", name)
                if os.path.exists(p):
                    self._bg_images.append(p)
        print(f"Backgrounds: {len(self._bg_images)} images loaded")

        # Per-image hold time. Burton_2/3/4 hold 20s, others hold 60s.
        # Crossfade is 5s for all.
        self._bg_fade = 5.0

        def hold_for(path):
            base = os.path.basename(path)
            if base.startswith(("Burton_2", "Burton_3", "Burton_4")):
                return 20.0
            return 60.0

        self._bg_hold_for = hold_for
        self._bg_idx = 0        # current background index
        self._bg_show_start = time.time()  # when current image started holding

        # Load first two backgrounds into textures
        def load_bg(path):
            img = Image.open(path).convert("RGB")
            img.thumbnail((self.width, self.height), Image.LANCZOS)
            canvas = Image.new("RGB", (self.width, self.height), (255, 255, 255))
            px = (self.width - img.width) // 2
            py = (self.height - img.height) // 2
            canvas.paste(img, (px, py))
            return canvas.transpose(Image.FLIP_TOP_BOTTOM)

        self._load_bg = load_bg

        img0 = load_bg(self._bg_images[0])
        self.fossil_tex = ctx.texture(
            (self.width, self.height), 3, img0.tobytes()
        )
        self.fossil_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

        next_idx = 1 % len(self._bg_images)
        img1 = load_bg(self._bg_images[next_idx])
        self.fossil2_tex = ctx.texture(
            (self.width, self.height), 3, img1.tobytes()
        )
        self.fossil2_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self._bg_next_idx = next_idx

        # Silhouette texture
        blank = np.zeros((self.height, self.width), dtype="f4")
        self.sil_tex = ctx.texture(
            (self.width, self.height), 1, blank.tobytes(), dtype="f4"
        )
        self.sil_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

        # Debug depth texture (raw depth vis, always updated)
        blank_rgb = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        self.debug_tex = ctx.texture(
            (self.width, self.height), 3, blank_rgb.tobytes()
        )
        self.debug_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

        # Persistent depth color texture (person-only, holds last seen color)
        self._depth_color_persist = blank_rgb.copy()
        self.depth_persist_tex = ctx.texture(
            (self.width, self.height), 3, blank_rgb.tobytes()
        )
        self.depth_persist_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

        # Label texture (512x64 RGBA)
        self.label_tex = ctx.texture((512, 64), 4)
        self.label_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

        # Ping-pong FBOs
        self.persist_tex = [
            ctx.texture((self.width, self.height), 1, dtype="f4")
            for _ in range(2)
        ]
        self.display_tex = [
            ctx.texture((self.width, self.height), 4)
            for _ in range(2)
        ]
        for t in self.persist_tex + self.display_tex:
            t.filter = (moderngl.LINEAR, moderngl.LINEAR)

        self.fbos = [
            ctx.framebuffer(
                color_attachments=[self.display_tex[i], self.persist_tex[i]]
            )
            for i in range(2)
        ]
        for fbo in self.fbos:
            fbo.use()
            ctx.clear(0.0, 0.0, 0.0, 0.0)

        self.read_idx = 0
        self.write_idx = 1

        # Set uniforms
        self.composite_prog["u_fossil"].value = 0
        self.composite_prog["u_fossil2"].value = 4
        self.composite_prog["u_persistence"].value = 1
        self.composite_prog["u_silhouette"].value = 2
        self.composite_prog["u_depth_persist"].value = 3
        self.composite_prog["u_fossil_blend"].value = 0.0
        self.composite_prog["u_fade_rate"].value = config.FADE_RATE
        self.composite_prog["u_trace_intensity"].value = config.TRACE_INTENSITY
        self.composite_prog["u_mode"].value = self.effect_mode
        self.composite_prog["u_pixel_scale"].value = 1.0

        self.passthrough_prog["u_texture"].value = 0
        self.debug_prog["u_texture"].value = 0
        self.debug_prog["u_alpha"].value = 0.5

    def set_effect(self, mode: int):
        self.effect_mode = mode % len(EFFECT_NAMES)
        self.composite_prog["u_mode"].value = self.effect_mode
        # Clear persistence buffer when switching effects
        for fbo in self.fbos:
            fbo.use()
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._depth_color_persist[:] = 0
        self.depth_persist_tex.write(self._depth_color_persist.tobytes())
        self._label_time = time.time()
        self._render_label(EFFECT_NAMES[self.effect_mode])
        print(f"Effect: {self.effect_mode + 1}. {EFFECT_NAMES[self.effect_mode]}")

    def _render_label(self, text: str):
        img = Image.new("RGBA", (512, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
        except Exception:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (512 - tw) // 2
        y = (64 - th) // 2 - bbox[1]
        draw.text((x, y), text, fill=(255, 255, 255, 220), font=font)
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
        self.label_tex.write(img.tobytes())

    def update_silhouette(self, mask: np.ndarray):
        self.sil_tex.write(mask.tobytes())

    def update_debug(self, depth_vis: np.ndarray, mask: np.ndarray = None):
        import cv2
        rgb = cv2.cvtColor(depth_vis, cv2.COLOR_BGR2RGB)
        # Only write raw debug tex when overlay is active
        if self.debug_overlay:
            self.debug_tex.write(rgb.tobytes())
        # Update persistent depth color — cv2.copyTo is ~17x faster than numpy alpha blend
        if mask is not None and mask.max() > 0.3:
            mask8 = (mask > 0.3).astype(np.uint8) * 255
            cv2.copyTo(rgb, mask8, self._depth_color_persist)
            self.depth_persist_tex.write(self._depth_color_persist.tobytes())

    def set_fade_rate(self, rate: float):
        self.composite_prog["u_fade_rate"].value = max(0.0, min(1.0, rate))

    def set_trace_intensity(self, intensity: float):
        self.composite_prog["u_trace_intensity"].value = max(0.0, min(1.0, intensity))

    def set_pixel_scale(self, scale: float):
        self.composite_prog["u_pixel_scale"].value = max(0.25, min(5.0, scale))

    def blit_rgb(self, rgb_array):
        """Blit an RGB numpy array full-screen (for debug views)."""
        h, w = rgb_array.shape[:2]
        if not hasattr(self, '_blit_tex') or self._blit_tex.size != (w, h):
            if hasattr(self, '_blit_tex'):
                self._blit_tex.release()
            self._blit_tex = self.ctx.texture((w, h), 3)
            self._blit_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self._blit_tex.write(rgb_array.tobytes())

        self.ctx.screen.use()
        self.ctx.viewport = (0, 0, self.screen_w, self.screen_h)
        self._blit_tex.use(location=0)
        self.passthrough_vao.render(moderngl.TRIANGLE_STRIP)

    def render(self):
        # Per-image background crossfade. Each image has its own hold time
        # followed by a shared fade duration to the next image.
        hold = self._bg_hold_for(self._bg_images[self._bg_idx])
        elapsed = time.time() - self._bg_show_start
        if elapsed < hold:
            blend = 0.0
        elif elapsed < hold + self._bg_fade:
            blend = (elapsed - hold) / self._bg_fade
        else:
            # Fade complete — advance to next image
            self._bg_idx = (self._bg_idx + 1) % len(self._bg_images)
            self._bg_show_start = time.time()
            self.fossil_tex.write(self.fossil2_tex.read())
            self._bg_next_idx = (self._bg_idx + 1) % len(self._bg_images)
            next_img = self._load_bg(self._bg_images[self._bg_next_idx])
            self.fossil2_tex.write(next_img.tobytes())
            name = os.path.splitext(os.path.basename(self._bg_images[self._bg_idx]))[0]
            print(f"Background: {name} (hold {self._bg_hold_for(self._bg_images[self._bg_idx]):.0f}s)", flush=True)
            blend = 0.0

        self.composite_prog["u_fossil_blend"].value = blend

        # Pass 1: composite
        self.fbos[self.write_idx].use()
        self.fossil_tex.use(location=0)
        self.persist_tex[self.read_idx].use(location=1)
        self.sil_tex.use(location=2)
        self.depth_persist_tex.use(location=3)
        self.fossil2_tex.use(location=4)
        self.composite_vao.render(moderngl.TRIANGLE_STRIP)

        # Swap
        self.read_idx, self.write_idx = self.write_idx, self.read_idx

        # Pass 2: blit to screen (scale to full display)
        self.ctx.screen.use()
        self.ctx.viewport = (0, 0, self.screen_w, self.screen_h)
        self.display_tex[self.read_idx].use(location=0)
        self.passthrough_vao.render(moderngl.TRIANGLE_STRIP)

        # Pass 3: debug overlay
        if self.debug_overlay:
            self.ctx.enable(moderngl.BLEND)
            self.ctx.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA)
            self.debug_tex.use(location=0)
            self.debug_vao.render(moderngl.TRIANGLE_STRIP)
            self.ctx.disable(moderngl.BLEND)

        # Pass 4: effect label overlay (fades out after 2 seconds)
        elapsed = time.time() - self._label_time
        if elapsed < self._label_duration:
            alpha = 1.0 - (elapsed / self._label_duration)
            self.ctx.enable(moderngl.BLEND)
            self.ctx.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA)
            self.debug_prog["u_alpha"].value = alpha
            self.label_tex.use(location=0)
            self.label_vao.render(moderngl.TRIANGLE_STRIP)
            self.debug_prog["u_alpha"].value = 0.5  # restore for debug overlay
            self.ctx.disable(moderngl.BLEND)
