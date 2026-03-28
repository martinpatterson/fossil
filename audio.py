"""Spatial audio engine for Fossil installation footstep sounds."""

import math
import os
import random
import time
from collections import deque

import numpy as np
import sounddevice as sd
import soundfile as sf
from scipy import signal

import config


class AudioEngine:
    def __init__(self):
        self._clips = []       # All loaded audio clips
        self.muted = False
        self._voices = []      # Active playback voices
        self._master_vol = 1.0
        self._target_vol = 1.0
        self._stream = None
        self._lock = __import__('threading').Lock()

        # Rate limiting state
        self._last_event_time = 0.0
        self._event_times = deque()

        # Idle detection
        self._last_activity_time = 0.0
        self._is_idle = False

    def setup(self):
        """Load all audio clips from assets directory and open stereo output stream."""
        clip_dir = config.AUDIO_CLIP_DIR
        audio_exts = ('.wav', '.aiff', '.aif', '.mp3', '.flac', '.ogg')

        # Scan directory for audio files
        self._clips = []
        if os.path.isdir(clip_dir):
            for f in sorted(os.listdir(clip_dir)):
                if f.lower().endswith(audio_exts):
                    clip = self._load_clip(os.path.join(clip_dir, f))
                    if clip is not None:
                        self._clips.append(clip)

        if not self._clips:
            print("Audio: WARNING — no clips loaded, audio will be silent")
        else:
            print(f"Audio: loaded {len(self._clips)} clips from {clip_dir}")

        try:
            self._stream = sd.OutputStream(
                samplerate=config.AUDIO_SAMPLE_RATE,
                channels=2,
                dtype='float32',
                callback=self._audio_callback,
                device=config.AUDIO_DEVICE,
                blocksize=1024,
            )
            self._stream.start()
            self._last_activity_time = time.monotonic()
            print("Audio: stream started")
        except Exception as e:
            print(f"Audio: failed to open stream ({e}), will retry")
            self._stream = None

    def trigger(self, x_mm: float, depth_mm: float):
        """Trigger a footstep sound at the given spatial position."""
        if self._stream is None or self.muted:
            return
        now = time.monotonic()

        # Rate limit check 1: minimum interval
        if (now - self._last_event_time) < (config.AUDIO_MIN_INTERVAL_MS / 1000.0):
            return

        # Rate limit check 2: max events per second
        cutoff = now - 1.0
        while self._event_times and self._event_times[0] < cutoff:
            self._event_times.popleft()
        if len(self._event_times) >= config.AUDIO_MAX_EVENTS_PER_SEC:
            return

        # Select clip (random)
        if not self._clips:
            return
        clip = random.choice(self._clips).copy()

        # Pitch shift via resampling
        variance = config.AUDIO_PITCH_VARIANCE
        if variance > 0:
            factor = 1.0 + random.uniform(-variance, variance)
            new_len = int(len(clip) / factor)
            if new_len > 0:
                clip = signal.resample(clip, new_len)

        # Pan calculation (equal-power)
        half_width = config.LIDAR_GALLERY_WIDTH_MM / 2.0
        x_norm = max(-1.0, min(1.0, -x_mm / half_width)) if half_width > 0 else 0.0
        pan = math.copysign(abs(x_norm) ** config.AUDIO_PAN_CURVE, x_norm) * config.AUDIO_PAN_RANGE
        # Equal-power pan law
        angle = (pan + 1.0) / 2.0 * (math.pi / 2.0)
        gain_l = math.cos(angle)
        gain_r = math.sin(angle)

        # Volume from depth
        depth_range = config.AUDIO_VOL_FAR_MM - config.AUDIO_VOL_NEAR_MM
        if depth_range > 0:
            t = (depth_mm - config.AUDIO_VOL_NEAR_MM) / depth_range
            t = max(0.0, min(1.0, t))
        else:
            t = 0.0
        vol = config.AUDIO_VOL_MAX + (config.AUDIO_VOL_MIN - config.AUDIO_VOL_MAX) * t

        # Create stereo voice
        stereo = np.column_stack([clip * gain_l * vol, clip * gain_r * vol])

        with self._lock:
            # Cap max simultaneous voices to prevent memory growth
            if len(self._voices) >= 16:
                self._voices.pop(0)
            self._voices.append({'data': stereo, 'pos': 0})

        self._last_event_time = now
        self._event_times.append(now)
        self._last_activity_time = now

        # Resume from idle
        if self._is_idle:
            self._is_idle = False
            self._target_vol = 1.0

    def update(self):
        """Call every frame. Handles idle fade-out, resume, and stream recovery."""
        # Try to open stream if not yet open
        if self._stream is None:
            if not hasattr(self, '_last_stream_retry'):
                self._last_stream_retry = 0.0
            now = time.monotonic()
            if now - self._last_stream_retry >= 5.0:
                self._last_stream_retry = now
                try:
                    self._stream = sd.OutputStream(
                        samplerate=config.AUDIO_SAMPLE_RATE,
                        channels=2,
                        dtype='float32',
                        callback=self._audio_callback,
                        device=config.AUDIO_DEVICE,
                        blocksize=1024,
                    )
                    self._stream.start()
                    print("Audio: stream started")
                except Exception:
                    self._stream = None
            return

        now = time.monotonic()
        elapsed_since_activity = now - self._last_activity_time

        if not self._is_idle and elapsed_since_activity >= config.AUDIO_IDLE_TIMEOUT_SEC:
            self._is_idle = True
            self._target_vol = 0.0

        # Ramp master volume toward target
        if self._master_vol != self._target_vol:
            if self._target_vol < self._master_vol:
                # Fade out
                rate = 1.0 / max(config.AUDIO_FADE_OUT_SEC * config.TARGET_FPS, 1)
                self._master_vol = max(self._target_vol, self._master_vol - rate)
            else:
                # Fade in (fast resume)
                rate = 1.0 / max(0.1 * config.TARGET_FPS, 1)
                self._master_vol = min(self._target_vol, self._master_vol + rate)

            # Clear voices when fully silent
            if self._master_vol <= 0.0:
                with self._lock:
                    self._voices.clear()

    def close(self):
        """Stop audio stream."""
        if self._stream:
            self._stream.stop()
            self._stream.close()
            print("Audio: stopped")

    # ── Internal ──────────────────────────────────────────────────────────

    def _load_clip(self, path):
        """Load and resample a mono audio clip."""
        if not os.path.exists(path):
            print(f"Audio: clip not found — {path}")
            return None
        try:
            data, sr = sf.read(path, dtype='float32')
            # Convert stereo to mono if needed
            if data.ndim > 1:
                data = data.mean(axis=1)
            # Resample if needed
            if sr != config.AUDIO_SAMPLE_RATE:
                num_samples = int(len(data) * config.AUDIO_SAMPLE_RATE / sr)
                data = signal.resample(data, num_samples)
            return data
        except Exception as e:
            print(f"Audio: failed to load {path} — {e}")
            return None

    def _audio_callback(self, outdata, frames, time_info, status):
        """Sounddevice callback: mix active voices, apply master vol, soft clip."""
        if status:
            pass  # Silently handle underruns

        output = np.zeros((frames, 2), dtype=np.float32)

        with self._lock:
            finished = []
            for i, voice in enumerate(self._voices):
                data = voice['data']
                pos = voice['pos']
                remaining = len(data) - pos
                n = min(frames, remaining)
                if n > 0:
                    output[:n] += data[pos:pos + n]
                voice['pos'] = pos + n
                if pos + n >= len(data):
                    finished.append(i)

            for i in reversed(finished):
                self._voices.pop(i)

        # Apply master volume and soft clip
        output *= self._master_vol
        np.tanh(output, out=output)

        outdata[:] = output
