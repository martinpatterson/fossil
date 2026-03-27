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

        # Label quad (bottom center strip)
        label_h = 0.12
        label_verts = np.array(
            [
                -0.4, -1.0,           0.0, 0.0,
                 0.4, -1.0,           1.0, 0.0,
                -0.4, -1.0 + label_h, 0.0, 1.0,
                 0.4, -1.0 + label_h, 1.0, 1.0,
            ],
            dtype="f4",
        )
        self.label_vbo = ctx.buffer(label_verts)
        self.label_vao = ctx.vertex_array(
            self.debug_prog,
            [(self.label_vbo, "2f 2f", "in_position", "in_uv")],
        )

        # Load fossil photograph
        asset_path = os.path.join(os.path.dirname(__file__), "assets", "fossil.png")
        img = Image.open(asset_path).convert("RGB")
        img = img.resize((self.width, self.height), Image.LANCZOS)
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
        self.fossil_tex = ctx.texture(
            (self.width, self.height), 3, img.tobytes()
        )
        self.fossil_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

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
        self.composite_prog["u_persistence"].value = 1
        self.composite_prog["u_silhouette"].value = 2
        self.composite_prog["u_depth_persist"].value = 3
        self.composite_prog["u_fade_rate"].value = config.FADE_RATE
        self.composite_prog["u_trace_intensity"].value = config.TRACE_INTENSITY
        self.composite_prog["u_mode"].value = self.effect_mode

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
        # Always write raw depth vis for debug overlay
        self.debug_tex.write(rgb.tobytes())
        # Update persistent depth color where person is present
        # Use smoothed mask as blend weight (threshold at 0.1 to avoid room bleed)
        if mask is not None:
            alpha = np.clip((mask - 0.1) / 0.9, 0.0, 1.0)[:, :, np.newaxis]
            self._depth_color_persist = (
                self._depth_color_persist.astype(np.float32) * (1.0 - alpha)
                + rgb.astype(np.float32) * alpha
            ).astype(np.uint8)
            self.depth_persist_tex.write(self._depth_color_persist.tobytes())

    def set_fade_rate(self, rate: float):
        self.composite_prog["u_fade_rate"].value = max(0.0, min(1.0, rate))

    def set_trace_intensity(self, intensity: float):
        self.composite_prog["u_trace_intensity"].value = max(0.0, min(1.0, intensity))

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
        # Pass 1: composite
        self.fbos[self.write_idx].use()
        self.fossil_tex.use(location=0)
        self.persist_tex[self.read_idx].use(location=1)
        self.sil_tex.use(location=2)
        self.depth_persist_tex.use(location=3)
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
