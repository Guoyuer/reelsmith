"""Tests for music generation (Gemini Lyria RealTime)."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Unit tests: WAV writer
# ---------------------------------------------------------------------------


class TestWriteWav:
    @pytest.mark.parametrize(
        "sample_rate, channels, bits, pcm_seconds",
        [
            (48000, 2, 16, 1),
            (44100, 1, 16, 1),
        ],
        ids=["stereo_48k", "mono_44k"],
    )
    def test_writes_valid_wav(self, tmp_path, sample_rate, channels, bits, pcm_seconds):
        from pipeline.music._gemini import _write_wav

        pcm = b"\x00" * (sample_rate * channels * (bits // 8) * pcm_seconds)
        path = tmp_path / "test.wav"
        _write_wav(
            path, pcm, sample_rate=sample_rate, channels=channels, bits_per_sample=bits
        )

        assert path.exists()
        data = path.read_bytes()

        # RIFF header
        assert data[:4] == b"RIFF"
        assert data[8:12] == b"WAVE"
        assert data[12:16] == b"fmt "

        # fmt chunk
        assert struct.unpack_from("<I", data, 16)[0] == 16  # chunk size
        assert struct.unpack_from("<H", data, 20)[0] == 1  # PCM format
        assert struct.unpack_from("<H", data, 22)[0] == channels
        assert struct.unpack_from("<I", data, 24)[0] == sample_rate

        # byte rate = sr * channels * bytes_per_sample
        byte_rate = struct.unpack_from("<I", data, 28)[0]
        assert byte_rate == sample_rate * channels * (bits // 8)

        # data chunk
        assert data[36:40] == b"data"
        assert struct.unpack_from("<I", data, 40)[0] == len(pcm)

        # Total size = 44-byte header + pcm
        assert path.stat().st_size == 44 + len(pcm)


# ---------------------------------------------------------------------------
# Unit tests: generate_music_gemini (mocked API)
# ---------------------------------------------------------------------------


class TestGenerateMusicGemini:
    def test_returns_none_without_api_key(self, tmp_path: Path, monkeypatch):
        from pipeline.music._gemini import generate_music_gemini

        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert generate_music_gemini("family", "upbeat", 30, tmp_path) is None

    def test_uses_cache(self, tmp_path: Path):
        from pipeline.music._gemini import generate_music_gemini

        cache_key = "gemini_family_upbeat_30s_default"
        wav_path = tmp_path / f"{cache_key}.wav"
        wav_path.write_bytes(b"RIFF" + b"\x00" * 100)
        meta = tmp_path / f"{cache_key}.json"
        meta.write_text(json.dumps({"path": str(wav_path)}))

        assert generate_music_gemini("family", "upbeat", 30, tmp_path) == wav_path

    def test_generates_and_caches(self, tmp_path: Path, monkeypatch):
        from pipeline.music._gemini import generate_music_gemini

        fake_pcm = b"\x00" * (48000 * 2 * 2 // 2)  # 0.5s stereo 16-bit
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        async def _fake_generate(*args, **kwargs):
            return fake_pcm

        with patch("pipeline.music._gemini._generate_music", _fake_generate):
            result = generate_music_gemini("family", "upbeat", 1, tmp_path)

        assert result is not None
        assert result.exists()
        assert result.suffix == ".wav"

        data = result.read_bytes()
        assert data[:4] == b"RIFF"
        assert data[8:12] == b"WAVE"

        cache_meta = tmp_path / "gemini_family_upbeat_1s_default.json"
        assert cache_meta.exists()
        meta = json.loads(cache_meta.read_text())
        assert meta["backend"] == "gemini"
        assert meta["model"] == "lyria-realtime-exp"

    @pytest.mark.parametrize(
        "fake_result, description",
        [
            (b"", "empty audio"),
            (ConnectionError("WebSocket failed"), "API error"),
        ],
        ids=["empty_audio", "api_error"],
    )
    def test_returns_none_on_failure(
        self, tmp_path, monkeypatch, fake_result, description
    ):
        from pipeline.music._gemini import generate_music_gemini

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        async def _fake_generate(*args, **kwargs):
            if isinstance(fake_result, Exception):
                raise fake_result
            return fake_result

        with patch("pipeline.music._gemini._generate_music", _fake_generate):
            assert generate_music_gemini("family", "upbeat", 10, tmp_path) is None

    def test_uses_mood_over_template(self, tmp_path: Path, monkeypatch):
        from pipeline.music._gemini import generate_music_gemini

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        captured_prompt = []
        fake_pcm = b"\x00" * (48000 * 2 * 2)

        async def _fake_generate(api_key, prompt, duration):
            captured_prompt.append(prompt)
            return fake_pcm

        with patch("pipeline.music._gemini._generate_music", _fake_generate):
            generate_music_gemini(
                "family", "upbeat", 1, tmp_path, mood="custom mood prompt"
            )

        assert captured_prompt[0] == "custom mood prompt"


# ---------------------------------------------------------------------------
# E2E test: real Gemini API call (requires GEMINI_API_KEY)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGenerateMusicGeminiE2E:
    @pytest.fixture(autouse=True)
    def _skip_without_key(self):
        import os

        if not os.getenv("GEMINI_API_KEY"):
            pytest.skip("GEMINI_API_KEY not set")

    def test_generates_real_music(self, tmp_path: Path):
        from pipeline.music._gemini import generate_music_gemini

        result = generate_music_gemini(
            trip_type="general",
            style="upbeat",
            target_duration=5,
            cache_dir=tmp_path,
            mood="gentle acoustic guitar, peaceful travel music",
        )

        assert result is not None, "Generation failed"
        assert result.exists()
        assert result.stat().st_size > 1000

        data = result.read_bytes()
        assert data[:4] == b"RIFF"
        assert data[8:12] == b"WAVE"

        cache_meta = tmp_path / "gemini_general_upbeat_5s.json"
        assert cache_meta.exists()
        meta = json.loads(cache_meta.read_text())
        assert meta["backend"] == "gemini"
        assert meta["duration"] > 0

    def test_cache_hit_on_second_call(self, tmp_path: Path):
        from pipeline.music._gemini import generate_music_gemini

        result1 = generate_music_gemini(
            "general",
            "upbeat",
            5,
            tmp_path,
            mood="gentle piano, calm travel background",
        )
        assert result1 is not None

        result2 = generate_music_gemini(
            "general",
            "upbeat",
            5,
            tmp_path,
            mood="gentle piano, calm travel background",
        )
        assert result2 == result1
