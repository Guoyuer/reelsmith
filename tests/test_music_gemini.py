"""Tests for pipeline.music._gemini — cache edge cases."""

from __future__ import annotations

from unittest.mock import patch

from pipeline.music._gemini import generate_music_gemini


class TestGenerateMusicGeminiCache:
    def test_cache_miss_without_key(self, tmp_path):
        """Cache miss + no API key = None."""
        cache_dir = tmp_path / "cache"
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}, clear=False):
            result = generate_music_gemini(
                trip_type="solo",
                style="cinematic",
                target_duration=60,
                cache_dir=cache_dir,
                mood="epic orchestral",
            )
        assert result is None
        assert cache_dir.exists()  # dir should still be created
