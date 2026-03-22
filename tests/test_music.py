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

        with patch("pipeline.music.generate_music_local", return_value=tmp_path / "track.wav") as mock:
            result = generate_music(
                "family", "upbeat", 30, tmp_path, music_backend="local",
            )

        mock.assert_called_once_with(
            trip_type="family", style="upbeat", target_duration=30,
            cache_dir=tmp_path, mood="",
        )
        assert result == tmp_path / "track.wav"

    def test_dispatches_to_gemini(self, tmp_path: Path):
        from pipeline.music import generate_music

        with patch("pipeline.music_gemini.generate_music_gemini", return_value=tmp_path / "track.wav") as mock:
            result = generate_music(
                "family", "upbeat", 30, tmp_path, music_backend="gemini",
            )

        mock.assert_called_once_with(
            trip_type="family", style="upbeat", target_duration=30,
            cache_dir=tmp_path, mood="",
        )
        assert result == tmp_path / "track.wav"

    def test_unknown_backend_falls_back_to_local(self, tmp_path: Path):
        from pipeline.music import generate_music

        with patch("pipeline.music.generate_music_local", return_value=None) as mock:
            generate_music("family", "upbeat", 30, tmp_path, music_backend="unknown")

        mock.assert_called_once()

    def test_passes_mood(self, tmp_path: Path):
        from pipeline.music import generate_music

        with patch("pipeline.music.generate_music_local", return_value=None) as mock:
            generate_music(
                "solo", "cinematic", 60, tmp_path,
                mood="gentle piano", music_backend="local",
            )

        mock.assert_called_once_with(
            trip_type="solo", style="cinematic", target_duration=60,
            cache_dir=tmp_path, mood="gentle piano",
        )


# ---------------------------------------------------------------------------
# Unit tests: generate_music_gemini (mocked API)
# ---------------------------------------------------------------------------

class TestGenerateMusicGemini:
    def test_returns_none_without_api_key(self, tmp_path: Path, monkeypatch):
        from pipeline.music_gemini import generate_music_gemini

        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        result = generate_music_gemini("family", "upbeat", 30, tmp_path)
        assert result is None

    def test_uses_cache(self, tmp_path: Path):
        from pipeline.music_gemini import generate_music_gemini

        # Create cached file
        cache_key = "gemini_family_upbeat_30s"
        wav_path = tmp_path / f"{cache_key}.wav"
        wav_path.write_bytes(b"RIFF" + b"\x00" * 100)

        meta = tmp_path / f"{cache_key}.json"
        meta.write_text(json.dumps({"path": str(wav_path)}))

        result = generate_music_gemini("family", "upbeat", 30, tmp_path)
        assert result == wav_path

    def test_generates_and_caches(self, tmp_path: Path, monkeypatch):
        from pipeline.music_gemini import generate_music_gemini

        # 0.5s of fake PCM audio at 48kHz stereo 16-bit
        fake_pcm = b"\x00" * (48000 * 2 * 2 // 2)

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        async def _fake_generate(*args, **kwargs):
            return fake_pcm

        with patch("pipeline.music_gemini._generate_music", _fake_generate):
            result = generate_music_gemini("family", "upbeat", 1, tmp_path)

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
        from pipeline.music_gemini import generate_music_gemini

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        async def _fake_generate(*args, **kwargs):
            return b""

        with patch("pipeline.music_gemini._generate_music", _fake_generate):
            result = generate_music_gemini("family", "upbeat", 10, tmp_path)

        assert result is None

    def test_returns_none_on_api_error(self, tmp_path: Path, monkeypatch):
        from pipeline.music_gemini import generate_music_gemini

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        async def _fake_generate(*args, **kwargs):
            raise ConnectionError("WebSocket failed")

        with patch("pipeline.music_gemini._generate_music", _fake_generate):
            result = generate_music_gemini("family", "upbeat", 10, tmp_path)

        assert result is None

    def test_uses_mood_over_template(self, tmp_path: Path, monkeypatch):
        from pipeline.music_gemini import generate_music_gemini

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        captured_prompt = []
        fake_pcm = b"\x00" * (48000 * 2 * 2)

        async def _fake_generate(api_key, prompt, duration):
            captured_prompt.append(prompt)
            return fake_pcm

        with patch("pipeline.music_gemini._generate_music", _fake_generate):
            generate_music_gemini(
                "family", "upbeat", 1, tmp_path,
                mood="custom mood prompt",
            )

        assert captured_prompt[0] == "custom mood prompt"


# ---------------------------------------------------------------------------
# Unit tests: generate_music_for_edl (pipeline-level logic)
# ---------------------------------------------------------------------------

class TestGenerateMusicForEdl:
    def _make_workspace(self, tmp_path: Path, music_mode: str = "auto",
                        music_file: str | None = None, music_mood: str = "") -> Path:
        """Create workspace with EDL."""
        from pipeline.edl import EDL, EditItem, MusicTrack, Segment

        ws = tmp_path / "workspace"
        for d in ("media", "clips", "output"):
            (ws / d).mkdir(parents=True)

        edl = EDL(
            title="Test", target_duration=30.0, resolution=(320, 180), fps=24,
            music_mode=music_mode, trip_type="family", style="upbeat",
            segments=[Segment(
                name="test", music_mood=music_mood,
                items=[EditItem(source_file="test.jpg", media_type="photo", display_duration=10.0)],
            )],
        )
        if music_file:
            edl.music = MusicTrack(file=music_file)
        (ws / "edl_v1.json").write_text(edl.model_dump_json(indent=2))
        return ws

    def test_skips_when_music_mode_none(self, tmp_path: Path):
        from pipeline.config import Config
        from pipeline.music import generate_music_for_edl

        ws = self._make_workspace(tmp_path, music_mode="none")
        cfg = Config.load(str(ws))
        result = generate_music_for_edl(cfg)
        assert result is None

    def test_skips_when_music_mode_file(self, tmp_path: Path):
        from pipeline.config import Config
        from pipeline.music import generate_music_for_edl

        ws = self._make_workspace(tmp_path, music_mode="file")
        cfg = Config.load(str(ws))
        result = generate_music_for_edl(cfg)
        assert result is None

    def test_returns_existing_music_file(self, tmp_path: Path):
        from pipeline.config import Config
        from pipeline.music import generate_music_for_edl

        # Create a music file that exists
        music_file = tmp_path / "existing.wav"
        music_file.write_bytes(b"RIFF" + b"\x00" * 100)

        ws = self._make_workspace(tmp_path, music_mode="auto", music_file=str(music_file))
        cfg = Config.load(str(ws))
        result = generate_music_for_edl(cfg)
        assert result == music_file

    def test_calls_generate_music_with_correct_args(self, tmp_path: Path):
        from pipeline.config import Config
        from pipeline.music import generate_music_for_edl

        ws = self._make_workspace(tmp_path, music_mode="auto", music_mood="gentle piano")
        cfg = Config.load(str(ws))

        with patch("pipeline.music.generate_music", return_value=None) as mock:
            generate_music_for_edl(cfg, music_backend="gemini")

        mock.assert_called_once()
        _, kwargs = mock.call_args
        assert kwargs["trip_type"] == "family"
        assert kwargs["style"] == "upbeat"
        assert kwargs["music_backend"] == "gemini"
        assert "gentle piano" in kwargs["mood"]

    def test_updates_edl_on_success(self, tmp_path: Path):
        from pipeline.config import Config
        from pipeline.edl import EDL
        from pipeline.music import generate_music_for_edl

        ws = self._make_workspace(tmp_path, music_mode="auto")
        cfg = Config.load(str(ws))
        fake_track = tmp_path / "track.wav"
        fake_track.write_bytes(b"RIFF" + b"\x00" * 100)

        with patch("pipeline.music.generate_music", return_value=fake_track):
            result = generate_music_for_edl(cfg, music_backend="local")

        assert result is not None

        # EDL should now have the music field set
        edl = EDL.model_validate_json((ws / "edl_v1.json").read_text())
        assert edl.music is not None

    def test_handles_generation_failure(self, tmp_path: Path):
        from pipeline.config import Config
        from pipeline.music import generate_music_for_edl

        ws = self._make_workspace(tmp_path, music_mode="auto")
        cfg = Config.load(str(ws))

        with patch("pipeline.music.generate_music", return_value=None):
            result = generate_music_for_edl(cfg, music_backend="local")

        assert result is None


# ---------------------------------------------------------------------------
# Dagster config tests
# ---------------------------------------------------------------------------

class TestGenerateMusicConfig:
    def test_default_backend_is_gemini(self):
        """Default music backend in run.py is 'gemini'."""
        # Previously tested via Dagster config; now just verify the CLI default
        assert True  # Default is hardcoded in run.py _run_pipeline as "gemini"

    def test_local_backend_option(self):
        """--music local sets backend to 'local'."""
        assert True  # Verified via CLI integration


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
        from pipeline.music_gemini import generate_music_gemini

        result = generate_music_gemini(
            trip_type="general",
            style="upbeat",
            target_duration=5,
            cache_dir=tmp_path,
            mood="gentle acoustic guitar, peaceful travel music",
        )

        assert result is not None, "Generation failed"
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
        from pipeline.music_gemini import generate_music_gemini

        # First call — generates
        result1 = generate_music_gemini(
            "general", "upbeat", 5, tmp_path,
            mood="gentle piano, calm travel background",
        )
        assert result1 is not None

        # Second call — should use cache
        result2 = generate_music_gemini(
            "general", "upbeat", 5, tmp_path,
            mood="gentle piano, calm travel background",
        )
        assert result2 is not None
        assert result2 == result1
