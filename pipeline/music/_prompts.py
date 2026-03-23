"""Music prompt templates — shared between music backends."""

from __future__ import annotations

import logging

logger = logging.getLogger("vlog.music.prompts")

# Prompt templates per trip_type + style
MUSIC_PROMPTS: dict[str, dict[str, str]] = {
    "family": {
        "upbeat": "happy upbeat acoustic travel vlog music, ukulele and light percussion, warm and joyful",
        "cinematic": "warm cinematic orchestral travel music, gentle strings and piano, emotional",
        "reflective": "gentle reflective acoustic guitar, peaceful and nostalgic, warm memories",
        "energetic": "fun energetic pop travel music, claps and whistles, happy adventure",
    },
    "solo": {
        "upbeat": "upbeat indie travel vlog music, acoustic guitar and light drums, adventure",
        "cinematic": "cinematic solo journey music, sweeping strings and piano, discovery",
        "reflective": "calm ambient travel music, soft piano and pads, introspective journey",
        "energetic": "energetic electronic travel music, driving beat, exploration",
    },
    "food": {
        "upbeat": "jazzy upbeat cafe background music, light swing, food vibes",
        "cinematic": "elegant restaurant ambiance, soft jazz piano and brushed drums",
        "reflective": "lo-fi chill background music, warm and cozy, cafe atmosphere",
        "energetic": "fun quirky cooking show music, playful and bouncy",
    },
    "adventure": {
        "upbeat": "epic upbeat adventure music, driving drums and bold brass, excitement",
        "cinematic": "cinematic epic adventure soundtrack, dramatic orchestra, exploration",
        "reflective": "ambient nature documentary music, peaceful and vast, wilderness",
        "energetic": "high energy extreme sports music, fast drums and electric guitar",
    },
    "architecture": {
        "upbeat": "modern minimal electronic music, clean beats and synth pads, urban",
        "cinematic": "cinematic ambient music, slow build, grand spaces and design",
        "reflective": "ambient piano and strings, contemplative, architectural beauty",
        "energetic": "tech house electronic music, modern city vibes, precise",
    },
    "general": {
        "upbeat": "upbeat travel vlog background music, acoustic and light, carefree",
        "cinematic": "cinematic travel montage music, emotional strings and piano",
        "reflective": "calm reflective travel music, gentle acoustic guitar, peaceful",
        "energetic": "energetic pop travel music, fun and lively, adventure vibes",
    },
}


def get_prompt(trip_type: str, style: str) -> str:
    """Get the music prompt for a given trip type and style."""
    if trip_type not in MUSIC_PROMPTS:
        logger.warning("Unknown trip_type '%s', falling back to 'general'", trip_type)
        trip_type = "general"
    type_prompts = MUSIC_PROMPTS[trip_type]
    if style not in type_prompts:
        logger.warning("Unknown style '%s' for trip_type '%s', falling back to 'upbeat'", style, trip_type)
        style = "upbeat"
    return type_prompts[style]
