"""Shared tuning constants used across pipeline stages.

Centralises numeric parameters that were previously scattered across
assemble/ and plan/ sub-packages.  Grouping them here makes the full
set of knobs discoverable and avoids silent drift when the same
value (e.g. sample rate) is defined in multiple files.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------
SAMPLE_RATE = 48000  # Hz; used for aevalsrc silence and audio processing

# ---------------------------------------------------------------------------
# Beat sync (_audio.py)
# ---------------------------------------------------------------------------
ENERGY_WINDOW_MS = 10  # ms; window size for BPM energy envelope
MIN_ENERGY_WINDOWS = 200  # minimum energy windows for reliable BPM estimation
MAX_BEAT_SHIFT = 0.4  # seconds; max transition snap distance
MONTAGE_MAX_SHIFT = 0.2  # seconds; tighter snap for montage segments
MIN_PHOTO_DURATION = 2.0  # seconds; floor after beat snap
MIN_VIDEO_DURATION = 3.0  # seconds; floor after beat snap
BEAT_SNAP_PRECISION = 3  # decimal places for snapped durations

# ---------------------------------------------------------------------------
# Video encoding (_encoder.py)
# ---------------------------------------------------------------------------
BITRATE_TIERS: list[tuple[int, int]] = [
    (3840 * 2160, 45),  # 4K  → 45 Mbps (H.264 base)
    (2560 * 1440, 16),  # 2K  → 16 Mbps
    (1920 * 1080, 8),  # 1080p → 8 Mbps
    (1280 * 720, 5),  # 720p → 5 Mbps
    (0, 3),  # fallback → 3 Mbps
]
HFR_MULTIPLIER = 1.5  # bitrate bump for fps > 30
HEVC_RATIO = 0.65  # HEVC bitrate as fraction of H.264

# ---------------------------------------------------------------------------
# Blur / background
# ---------------------------------------------------------------------------
BG_BLUR_SIGMA = 50  # gaussian blur for blurred backgrounds (photos, videos, title card)
UNSHARP_PARAMS = "3:3:0.5:3:3:0.0"  # luma:size:amount:chroma:size:amount

# ---------------------------------------------------------------------------
# Aspect ratio
# ---------------------------------------------------------------------------
ASPECT_RATIO_TOLERANCE = 0.05  # 5%; threshold for aspect-fill decision

# ---------------------------------------------------------------------------
# Text overlay (_filters.py)
# ---------------------------------------------------------------------------
FONT_SCALE_FACTOR = 0.055  # font size relative to output height
LONG_TEXT_THRESHOLD = 20  # characters; reduce font size above this
TEXT_FADE_RATIO = 0.6  # text visible for this fraction of clip duration
TEXT_BOTTOM_PADDING = 60  # pixels; drawtext bottom offset

# ---------------------------------------------------------------------------
# Title card (_render.py)
# ---------------------------------------------------------------------------
TITLE_SCALE = 0.08  # title font size as fraction of output height
TITLE_LONG_THRESHOLD = 25  # characters; reduce size above this
SUBTITLE_Y_RATIO = 0.59  # subtitle vertical position as fraction of height
SEPARATOR_WIDTH_RATIO = 0.15  # separator line width as fraction of output width
SEPARATOR_Y_RATIO = 0.55  # separator Y position as fraction of height
GRADIENT_START = "0x0f0c29"  # fallback gradient dark purple
GRADIENT_END = "0x302b63"  # fallback gradient lighter purple
FADE_IN_DURATION = 0.5  # seconds; title card fade-in
FADE_OUT_DURATION = 0.8  # seconds; title card fade-out

# ---------------------------------------------------------------------------
# Post-processing thresholds (_postprocess.py)
# ---------------------------------------------------------------------------
WARN_REMOVAL_RATE = 0.3  # warn at 30% item removal
FAIL_REMOVAL_RATE = 0.5  # hard-fail at 50% item removal
WARN_PATH_RATE = 0.2  # warn at 20% hallucinated paths

# ---------------------------------------------------------------------------
# Burst dedup / preview (_preview.py)
# ---------------------------------------------------------------------------
BURST_SIMILARITY_THRESHOLD = 0.92  # cosine similarity for burst photo dedup
BURST_WINDOW_SECS = 10  # seconds; burst grouping time window
DEDUP_THUMB_SIZE = 64  # pixels; thumbnail size for histogram comparison
