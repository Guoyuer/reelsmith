"""Tests for music generation backends (local MusicGen + Gemini Lyria RealTime)."""

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
    def test_writes_valid_wav_header(self, tmp_path: Path):
        from pipeline.music_gemini import _write_wav

        # 1 second of silence at 48kHz stereo 16-bit
        pcm = b"\x00" * (48000 * 2 * 2)
        path = tmp_path / "test.wav"
        _write_wav(path, pcm, sample_rate=48000, channels=2, bits_per_sample=16)

        assert path.exists()
        data = path.read_bytes()

        # RIFF header
        assert data[:4] == b"RIFF"
        assert data[8:12] == b"WAVE"
        assert data[12:16] == b"fmt "

        # fmt chunk fields
        assert struct.unpack_from("<I", data, 16)[0] == 16      # chunk size
        assert struct.unpack_from("<H", data, 20)[0] == 1       # PCM format
        assert struct.unpack_from("<H", data, 22)[0] == 2       # channels
        assert struct.unpack_from("<I", data, 24)[0] == 48000   # sample rate

        # data chunk
        assert data[36:40] == b"data"
        assert struct.unpack_from("<I", data, 40)[0] == len(pcm)

    def test_file_size_correct(self, tmp_path: Path):
        from pipeline.music_gemini import _write_wav

        pcm = b"\x01\x02" * 1000
        path = tmp_path / "test2.wav"
        _write_wav(path, pcm, sample_rate=44100, channels=1, bits_per_sample=16)

        # 44-byte header + pcm data
        assert path.stat().st_size == 44 + len(pcm)

    def test_mono_wav(self, tmp_path: Path):
        from pipeline.music_gemini import _write_wav

        pcm = b"\x00" * (44100 * 2)  # 1s mono 16-bit at 44.1kHz
        path = tmp_path / "mono.wav"
        _write_wav(path, pcm, sample_rate=44100, channels=1, bits_per_sample=16)

        data = path.read_bytes()
        assert struct.unpack_from("<H", data, 22)[0] == 1       # mono
        assert struct.unpack_from("<I", data, 24)[0] == 44100   # sample rate
        byte_rate = struct.unpack_from("<I", data, 28)[0]
        assert byte_rate == 44100 * 1 * 2  # sr * channels * bytes_per_sample


# ---------------------------------------------------------------------------
# Unit tests: generate_music dispatcher
# ---------------------------------------------------------------------------

class TestGenerateMusic:
    def test_dispatches_to_local(self, tmp_path: Path):
        from pipeline.music import generate_music

        with patch("pipeline.music.fetch_music", return_value=tmp_path / "track.wav") as mock:
            result = generate_music(
                "family", "upbeat", 30, tmp_path, backend="local",
            )

        mock.assert_called_once_with(
            trip_type="family", style="upbeat", target_duration=30,
            cache_dir=tmp_path, mood="", log_fn=None,
        )
        assert result == tmp_path / "track.wav"

    def test_dispatches_to_gemini(self, tmp_path: Path):
        from pipeline.music import generate_music

        with patch("pipeline.music_gemini.fetch_music_gemini", return_value=tmp_path / "track.wav") as mock:
            result = generate_music(
                "family", "upbeat", 30, tmp_path, backend="gemini",
            )

        mock.assert_called_once_with(
            trip_type="family", style="upbeat", target_duration=30,
            cache_dir=tmp_path, mood="", log_fn=None,
        )
        assert result == tmp_path / "track.wav"

    def test_unknown_backend_falls_back_to_local(self, tmp_path: Path):
        from pipeline.music import generate_music

        with patch("pipeline.music.fetch_music", return_value=None) as mock:
            generate_music("family", "upbeat", 30, tmp_path, backend="unknown")

        mock.assert_called_once()

    def test_passes_mood_and_log_fn(self, tmp_path: Path):
        from pipeline.music import generate_music

        log = print
        with patch("pipeline.music.fetch_music", return_value=None) as mock:
            generate_music(
                "solo", "cinematic", 60, tmp_path,
                mood="gentle piano", backend="local", log_fn=log,
            )

        mock.assert_called_once_with(
            trip_type="solo", style="cinematic", target_duration=60,
            cache_dir=tmp_path, mood="gentle piano", log_fn=log,
        )


# ---------------------------------------------------------------------------
# Unit tests: fetch_music_gemini (mocked API)
# ---------------------------------------------------------------------------

class TestFetchMusicGemini:
    def test_returns_none_without_api_key(self, tmp_path: Path, monkeypatch):
        from pipeline.music_gemini import fetch_music_gemini

        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        result = fetch_music_gemini("family", "upbeat", 30, tmp_path)
        assert result is None

    def test_uses_cache(self, tmp_path: Path):
        from pipeline.music_gemini import fetch_music_gemini

        # Create cached file
        cache_key = "gemini_family_upbeat_30s"
        wav_path = tmp_path / f"{cache_key}.wav"
        wav_path.write_bytes(b"RIFF" + b"\x00" * 100)

        meta = tmp_path / f"{cache_key}.json"
        meta.write_text(json.dumps({"path": str(wav_path)}))

        result = fetch_music_gemini("family", "upbeat", 30, tmp_path)
        assert result == wav_path

    def test_generates_and_caches(self, tmp_path: Path, monkeypatch):
        from pipeline.music_gemini import fetch_music_gemini

        # 0.5s of fake PCM audio at 48kHz stereo 16-bit
        fake_pcm = b"\x00" * (48000 * 2 * 2 // 2)

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        async def _fake_generate(*args, **kwargs):
            return fake_pcm

        with patch("pipeline.music_gemini._generate_music", _fake_generate):
            result = fetch_music_gemini("family", "upbeat", 1, tmp_path)

        assert result is not None
        assert result.exists()
        assert result.suffix == ".wav"

        # Verify WAV header
        data = result.read_bytes()
        assert data[:4] == b"RIFF"
        assert data[8:12] == b"WAVE"

        # Cache metadata should exist
        cache_meta = tmp_path / "gemini_family_upbeat_1s.json"
        assert cache_meta.exists()
        meta = json.loads(cache_meta.read_text())
        assert meta["backend"] == "gemini"
        assert meta["model"] == "lyria-realtime-exp"

    def test_returns_none_on_empty_audio(self, tmp_path: Path, monkeypatch):
        from pipeline.music_gemini import fetch_music_gemini

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        async def _fake_generate(*args, **kwargs):
            return b""

        with patch("pipeline.music_gemini._generate_music", _fake_generate):
            result = fetch_music_gemini("family", "upbeat", 10, tmp_path)

        assert result is None

    def test_returns_none_on_api_error(self, tmp_path: Path, monkeypatch):
        from pipeline.music_gemini import fetch_music_gemini

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        async def _fake_generate(*args, **kwargs):
            raise ConnectionError("WebSocket failed")

        with patch("pipeline.music_gemini._generate_music", _fake_generate):
            result = fetch_music_gemini("family", "upbeat", 10, tmp_path)

        assert result is None

    def test_uses_mood_over_template(self, tmp_path: Path, monkeypatch):
        from pipeline.music_gemini import fetch_music_gemini

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        captured_prompt = []
        fake_pcm = b"\x00" * (48000 * 2 * 2)

        async def _fake_generate(api_key, prompt, duration, log_fn):
            captured_prompt.append(prompt)
            return fake_pcm

        with patch("pipeline.music_gemini._generate_music", _fake_generate):
            fetch_music_gemini(
                "family", "upbeat", 1, tmp_path,
                mood="custom mood prompt",
            )

        assert captured_prompt[0] == "custom mood prompt"


# ---------------------------------------------------------------------------
# Dagster config tests
# ---------------------------------------------------------------------------

class TestAssembleConfigMusicBackend:
    def test_default_is_local(self):
        from pipeline.definitions import AssembleConfig

        cfg = AssembleConfig()
        assert cfg.music_backend == "local"

    def test_gemini_backend(self):
        from pipeline.definitions import AssembleConfig

        cfg = AssembleConfig(music_backend="gemini")
        assert cfg.music_backend == "gemini"


# ---------------------------------------------------------------------------
# E2E test: real Gemini API call (requires GEMINI_API_KEY)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestFetchMusicGeminiE2E:
    @pytest.fixture(autouse=True)
    def _skip_without_key(self):
        import os
        if not os.getenv("GEMINI_API_KEY"):
            pytest.skip("GEMINI_API_KEY not set")

    def test_generates_real_music(self, tmp_path: Path):
        from pipeline.music_gemini import fetch_music_gemini

        logs = []
        result = fetch_music_gemini(
            trip_type="general",
            style="upbeat",
            target_duration=5,
            cache_dir=tmp_path,
            mood="gentle acoustic guitar, peaceful travel music",
            log_fn=logs.append,
        )

        assert result is not None, f"Generation failed. Logs: {logs}"
        assert result.exists()
        assert result.stat().st_size > 1000, "WAV file too small"

        # Verify valid WAV
        data = result.read_bytes()
        assert data[:4] == b"RIFF"
        assert data[8:12] == b"WAVE"

        # Verify cache metadata
        cache_meta = tmp_path / "gemini_general_upbeat_5s.json"
        assert cache_meta.exists()
        meta = json.loads(cache_meta.read_text())
        assert meta["backend"] == "gemini"
        assert meta["duration"] > 0

    def test_cache_hit_on_second_call(self, tmp_path: Path):
        from pipeline.music_gemini import fetch_music_gemini

        # First call — generates
        result1 = fetch_music_gemini(
            "general", "upbeat", 5, tmp_path,
            mood="gentle piano, calm travel background",
        )
        assert result1 is not None

        # Second call — should use cache
        logs = []
        result2 = fetch_music_gemini(
            "general", "upbeat", 5, tmp_path,
            mood="gentle piano, calm travel background",
            log_fn=logs.append,
        )
        assert result2 is not None
        assert result2 == result1
        assert any("cached" in str(l).lower() for l in logs)
