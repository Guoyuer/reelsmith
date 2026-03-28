"""Tests for pipeline.music._gemini — cache logic, WAV writing, prompt resolution."""

from __future__ import annotations

import json
import wave
from unittest.mock import patch

from pipeline.music._gemini import _write_wav, generate_music_gemini


class TestWriteWav:
    def test_creates_valid_wav(self, tmp_path):
        pcm = b"\x00\x01" * 48000  # 0.5s of stereo 16-bit 48kHz
        path = tmp_path / "test.wav"
        _write_wav(path, pcm, sample_rate=48000, channels=2, bits_per_sample=16)
        assert path.exists()
        with wave.open(str(path)) as w:
            assert w.getframerate() == 48000
            assert w.getnchannels() == 2
            assert w.getsampwidth() == 2

    def test_riff_header(self, tmp_path):
        pcm = b"\x00" * 100
        path = tmp_path / "test.wav"
        _write_wav(path, pcm, 44100, 1, 16)
        data = path.read_bytes()
        assert data[:4] == b"RIFF"
        assert data[8:12] == b"WAVE"
        assert data[12:16] == b"fmt "


class TestGenerateMusicGeminiCache:
    def test_returns_cached_path(self, tmp_path):
        """If cache hit exists, return immediately without API call."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        # We need to match the actual cache key generation
        import hashlib

        mood = "warm acoustic"
        mood_hash = hashlib.md5(mood.encode()).hexdigest()[:8]
        cache_key = f"gemini_family_upbeat_30s_{mood_hash}"
        actual_wav = cache_dir / f"{cache_key}.wav"
        actual_wav.write_bytes(b"\x00" * 100)
        actual_meta = cache_dir / f"{cache_key}.json"
        actual_meta.write_text(json.dumps({"path": str(actual_wav)}))

        result = generate_music_gemini(
            trip_type="family",
            style="upbeat",
            target_duration=30,
            cache_dir=cache_dir,
            mood="warm acoustic",
        )
        assert result == actual_wav

    def test_no_api_key_returns_none(self, tmp_path):
        """Without GEMINI_API_KEY, should return None gracefully."""
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}, clear=False):
            result = generate_music_gemini(
                trip_type="family",
                style="upbeat",
                target_duration=30,
                cache_dir=tmp_path / "cache",
                mood="test",
            )
        assert result is None

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


class TestMusicOrchestrate:
    def test_segment_duration_with_transitions(self):
        from pipeline.edl import EditItem, Segment
        from pipeline.music._orchestrate import _segment_duration

        seg = Segment(
            name="S",
            items=[
                EditItem(source_file="a.jpg", media_type="photo", display_duration=5.0),
                EditItem(source_file="b.jpg", media_type="photo", display_duration=5.0),
                EditItem(source_file="c.jpg", media_type="photo", display_duration=5.0),
            ],
            transition="crossfade",
            transition_duration=0.5,
        )
        # 15 - 2*0.5 = 14
        assert _segment_duration(seg) == 14.0

    def test_segment_duration_cut(self):
        from pipeline.edl import EditItem, Segment
        from pipeline.music._orchestrate import _segment_duration

        seg = Segment(
            name="S",
            items=[
                EditItem(source_file="a.jpg", media_type="photo", display_duration=3.0),
                EditItem(source_file="b.jpg", media_type="photo", display_duration=3.0),
            ],
            transition="cut",
            transition_duration=0.5,
        )
        # cut transition — no overlap subtracted
        assert _segment_duration(seg) == 6.0

    def test_segment_duration_minimum_5s(self):
        from pipeline.edl import EditItem, Segment
        from pipeline.music._orchestrate import _segment_duration

        seg = Segment(
            name="S",
            items=[
                EditItem(source_file="a.jpg", media_type="photo", display_duration=2.0),
            ],
            transition="crossfade",
            transition_duration=0.5,
        )
        # 2s total, but minimum is 5
        assert _segment_duration(seg) == 5.0
