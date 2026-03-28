"""End-to-end assemble tests with hand-crafted EDLs.

Creates minimal valid EDLs from real media files in the workspace,
renders at low resolution, and validates the output.
"""

import json
import subprocess
from pathlib import Path

import pytest

WORKSPACE = Path("workspace/runs/singapore")
MEDIA_DIR = Path("workspace/media")


def _probe(path: Path) -> dict:
    """Probe a video file for duration, width, height, fps, codecs."""
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return json.loads(result.stdout) if result.returncode == 0 else {}


def _get_media_samples() -> tuple[list[str], list[dict]]:
    """Get sample photos and videos from manifest + per-item caches."""
    manifest_path = WORKSPACE / "manifest.json"
    if not manifest_path.exists():
        pytest.skip("No manifest.json — run prepare first")
    from pipeline.config import Config
    from pipeline.prepare import load_analysis

    cfg = Config(workspace=WORKSPACE)
    analysis = load_analysis(cfg)
    photos = [
        a
        for a in analysis
        if a.get("media_type") == "photo" and Path(a["local_path"]).exists()
    ]
    videos = [
        a
        for a in analysis
        if a.get("media_type") == "video"
        and Path(a["local_path"]).exists()
        and a.get("video_duration", 0) > 3
    ]
    if len(photos) < 3 or len(videos) < 2:
        pytest.skip("Not enough media files")
    return photos, videos


def _make_edl(
    photos,
    videos,
    *,
    n_photo=4,
    n_video=2,
    transition="crossfade",
    transition_duration=0.4,
    keep_audio_idx=None,
    title="Test EDL",
) -> dict:
    """Build a minimal valid EDL dict from real media files."""
    items = []
    for i, p in enumerate(photos[:n_photo]):
        items.append(
            {
                "source_file": p["local_path"],
                "media_type": "photo",
                "display_duration": 3.0,
                "effect": ["ken_burns_in", "ken_burns_out", "none", "ken_burns_left"][
                    i % 4
                ],
            }
        )
    for i, v in enumerate(videos[:n_video]):
        dur = min(v.get("video_duration", 10), 8.0)
        items.append(
            {
                "source_file": v["local_path"],
                "media_type": "video",
                "start_time": 0.0,
                "end_time": dur,
                "display_duration": dur,
                "effect": "none",
                "keep_audio": (keep_audio_idx is not None and i in keep_audio_idx),
            }
        )

    return {
        "title": title,
        "target_duration": 60,
        "segments": [
            {
                "name": "Test Segment",
                "music_mood": "test",
                "items": items,
                "transition": transition,
                "transition_duration": transition_duration,
                "mode": "narrative",
                "color_temp": "neutral",
            }
        ],
        "music": None,
        "music_mode": "none",
        "trip_type": "family",
        "style": "upbeat",
        "date_range": "",
        "language": "cn",
    }


def validate_edl(edl: dict) -> list[str]:
    """Validate an EDL dict. Returns list of error strings (empty = valid)."""
    errors = []

    # Required fields
    for field in ["title", "target_duration", "segments"]:
        if field not in edl:
            errors.append(f"Missing required field: {field}")

    fps = edl.get("fps", 30)
    if fps <= 0 or fps > 120:
        errors.append(f"Invalid fps: {fps}")

    segments = edl.get("segments", [])
    if not segments:
        errors.append("No segments")

    total_dur = 0
    all_sources = set()
    for si, seg in enumerate(segments):
        if not seg.get("items"):
            errors.append(f"Segment {si} '{seg.get('name', '')}' has no items")
            continue

        for ii, item in enumerate(seg["items"]):
            path = Path(item.get("source_file", ""))
            if not path.exists():
                errors.append(f"Seg{si} item{ii}: file not found: {path}")

            media_type = item.get("media_type")
            if media_type not in ("photo", "video"):
                errors.append(f"Seg{si} item{ii}: invalid media_type: {media_type}")

            dur = item.get("display_duration", 0)
            if dur <= 0 or dur > 60:
                errors.append(f"Seg{si} item{ii}: invalid display_duration: {dur}")
            total_dur += dur

            if media_type == "video":
                if item.get("effect", "none") != "none":
                    errors.append(f"Seg{si} item{ii}: video must have effect='none'")
                st = item.get("start_time")
                et = item.get("end_time")
                if st is not None and et is not None and st >= et:
                    errors.append(f"Seg{si} item{ii}: start_time >= end_time")

            if media_type == "photo":
                if item.get("effect") == "none":
                    errors.append(
                        f"Seg{si} item{ii}: photo should not have effect='none'"
                    )

            src = item.get("source_file", "")
            if src in all_sources:
                errors.append(f"Seg{si} item{ii}: duplicate source: {Path(src).name}")
            all_sources.add(src)

        td = seg.get("transition_duration", 0)
        if td < 0 or td > 3:
            errors.append(f"Segment {si}: invalid transition_duration: {td}")

    if total_dur < 10:
        errors.append(f"Total display_duration too short: {total_dur:.1f}s")

    return errors


def validate_output(
    video_path: Path, edl: dict, expected_resolution: tuple[int, int] = (640, 360)
) -> list[str]:
    """Validate rendered video against expected resolution and EDL content."""
    errors = []
    if not video_path.exists():
        return [f"Output file missing: {video_path}"]

    info = _probe(video_path)
    if not info:
        return [f"Cannot probe output: {video_path}"]

    fmt = info.get("format", {})
    duration = float(fmt.get("duration", 0))
    size = int(fmt.get("size", 0))

    if duration < 5:
        errors.append(f"Output too short: {duration:.1f}s")
    if size < 1000:
        errors.append(f"Output too small: {size} bytes")

    # Check video stream
    v_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not v_streams:
        errors.append("No video stream in output")
    else:
        vs = v_streams[0]
        w = vs.get("width", 0)
        h = vs.get("height", 0)
        exp_w, exp_h = expected_resolution
        if w != exp_w or h != exp_h:
            errors.append(f"Resolution mismatch: {w}x{h} vs expected {exp_w}x{exp_h}")

    # Check audio stream exists if any keep_audio items
    has_speech = any(
        item.get("keep_audio")
        for seg in edl.get("segments", [])
        for item in seg.get("items", [])
    )
    a_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if has_speech and not a_streams:
        errors.append("EDL has keep_audio items but output has no audio stream")

    # Duration check: should be within 30% of expected
    from pipeline.edl import EDL as EDLModel

    expected = EDLModel.model_validate(edl).estimated_duration()
    if expected > 0 and abs(duration - expected) / expected > 0.30:
        errors.append(
            f"Duration off by >30%: {duration:.1f}s vs expected {expected:.1f}s"
        )

    return errors


def _run_assemble(
    edl_dict: dict,
    run_name: str = "singapore",
    width: int = 640,
    height: int = 360,
    fps: int = 15,
) -> Path:
    """Write EDL to a temp file and run assemble."""
    edl_path = Path("workspace/test_edl.json")
    edl_path.write_text(json.dumps(edl_dict, indent=2))

    result = subprocess.run(
        [
            "python",
            "cli.py",
            "assemble",
            "-n",
            run_name,
            "--edl",
            str(edl_path),
            "-r",
            f"{width}x{height}x{fps}",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(Path.cwd()),
    )
    if result.returncode != 0:
        pytest.fail(f"Assemble failed:\n{result.stderr[-1000:]}")

    # Find the output
    run_dir = Path(f"workspace/runs/{run_name}/output")
    outputs = sorted(run_dir.glob("reelsmith_v*.mp4"), key=lambda f: f.stat().st_mtime)
    if not outputs:
        pytest.fail("No output video found")
    return outputs[-1]


# ── Tests ──────────────────────────────────────────────────────────────


class TestEDLValidation:
    """Test the EDL validation function itself."""

    def test_valid_edl(self):
        photos, videos = _get_media_samples()
        edl = _make_edl(photos, videos)
        errors = validate_edl(edl)
        assert errors == [], f"Unexpected errors: {errors}"

    def test_missing_file(self):
        photos, videos = _get_media_samples()
        edl = _make_edl(photos, videos)
        edl["segments"][0]["items"][0]["source_file"] = "nonexistent.jpg"
        errors = validate_edl(edl)
        assert any("not found" in e for e in errors)

    def test_duplicate_source(self):
        photos, videos = _get_media_samples()
        edl = _make_edl(photos, videos)
        edl["segments"][0]["items"][1]["source_file"] = edl["segments"][0]["items"][0][
            "source_file"
        ]
        errors = validate_edl(edl)
        assert any("duplicate" in e for e in errors)

    def test_video_with_ken_burns(self):
        photos, videos = _get_media_samples()
        edl = _make_edl(photos, videos)
        # Find a video item and set wrong effect
        for item in edl["segments"][0]["items"]:
            if item["media_type"] == "video":
                item["effect"] = "ken_burns_in"
                break
        errors = validate_edl(edl)
        assert any("effect" in e for e in errors)

    def test_zero_duration(self):
        photos, videos = _get_media_samples()
        edl = _make_edl(photos, videos)
        edl["segments"][0]["items"][0]["display_duration"] = 0
        errors = validate_edl(edl)
        assert any("display_duration" in e for e in errors)


@pytest.mark.integration
class TestAssembleE2E:
    """End-to-end render tests at low resolution."""

    def test_basic_render_360p15(self):
        """Basic photos + videos at 360p 15fps."""
        photos, videos = _get_media_samples()
        edl = _make_edl(photos, videos)
        errors = validate_edl(edl)
        assert errors == [], f"EDL validation failed: {errors}"

        output = _run_assemble(edl)
        errors = validate_output(output, edl)
        assert errors == [], f"Output validation failed: {errors}"

    def test_with_audio_360p15(self):
        """Test keep_audio on video clips."""
        photos, videos = _get_media_samples()
        edl = _make_edl(photos, videos, keep_audio_idx={0})
        output = _run_assemble(edl)
        errors = validate_output(output, edl)
        assert errors == [], f"Output validation failed: {errors}"

    def test_crossfade_transition(self):
        """Test crossfade transitions with custom duration."""
        photos, videos = _get_media_samples()
        edl = _make_edl(photos, videos, transition="crossfade", transition_duration=0.6)
        output = _run_assemble(edl)
        errors = validate_output(output, edl)
        assert errors == [], f"Output validation failed: {errors}"

    def test_720p30(self):
        """Test at 720p 30fps."""
        photos, videos = _get_media_samples()
        edl = _make_edl(photos, videos, title="720p30 Test")
        output = _run_assemble(edl)
        errors = validate_output(output, edl)
        assert errors == [], f"Output validation failed: {errors}"

    def test_multi_segment(self):
        """Test EDL with multiple segments and different transitions."""
        photos, videos = _get_media_samples()
        # Need more items
        if len(photos) < 6 or len(videos) < 4:
            pytest.skip("Need more media files")

        edl = {
            "title": "Multi-Segment Test",
            "target_duration": 60,
            "segments": [
                {
                    "name": "Segment A",
                    "music_mood": "upbeat",
                    "items": [
                        {
                            "source_file": photos[0]["local_path"],
                            "media_type": "photo",
                            "display_duration": 3.0,
                            "effect": "ken_burns_in",
                        },
                        {
                            "source_file": videos[0]["local_path"],
                            "media_type": "video",
                            "start_time": 0,
                            "end_time": min(videos[0]["video_duration"], 6),
                            "display_duration": min(videos[0]["video_duration"], 6),
                            "effect": "none",
                            "keep_audio": True,
                        },
                        {
                            "source_file": photos[1]["local_path"],
                            "media_type": "photo",
                            "display_duration": 4.0,
                            "effect": "ken_burns_out",
                        },
                    ],
                    "transition": "crossfade",
                    "transition_duration": 0.4,
                    "mode": "narrative",
                    "color_temp": "warm",
                },
                {
                    "name": "Segment B",
                    "music_mood": "calm",
                    "items": [
                        {
                            "source_file": photos[2]["local_path"],
                            "media_type": "photo",
                            "display_duration": 3.0,
                            "effect": "none",
                        },
                        {
                            "source_file": videos[1]["local_path"],
                            "media_type": "video",
                            "start_time": 0,
                            "end_time": min(videos[1]["video_duration"], 8),
                            "display_duration": min(videos[1]["video_duration"], 8),
                            "effect": "none",
                        },
                        {
                            "source_file": photos[3]["local_path"],
                            "media_type": "photo",
                            "display_duration": 3.0,
                            "effect": "ken_burns_right",
                        },
                    ],
                    "transition": "crossfade",
                    "transition_duration": 0.6,
                    "mode": "narrative",
                    "color_temp": "cool",
                },
            ],
            "music": None,
            "music_mode": "none",
            "trip_type": "family",
            "style": "upbeat",
            "date_range": "June 2025",
            "language": "cn",
            "quality": 0.5,
        }
        errors = validate_edl(edl)
        assert errors == [], f"EDL validation failed: {errors}"
        output = _run_assemble(edl)
        errors = validate_output(output, edl)
        assert errors == [], f"Output validation failed: {errors}"

    def test_text_overlays(self):
        """Test text overlays on photos and videos."""
        photos, videos = _get_media_samples()
        edl = _make_edl(photos, videos, title="Text Overlay Test")
        # Add text overlays
        edl["segments"][0]["items"][0]["text_overlay"] = {
            "text": "测试文字覆盖",
            "position": "bottom",
            "font_size": 48,
        }
        for item in edl["segments"][0]["items"]:
            if item["media_type"] == "video":
                item["text_overlay"] = {
                    "text": "Video overlay",
                    "position": "top",
                    "font_size": 48,
                }
                break
        edl["language"] = "cn"
        output = _run_assemble(edl)
        errors = validate_output(output, edl)
        assert errors == [], f"Output validation failed: {errors}"

    def test_speed_ramp(self):
        """Test playback_speed on video clips."""
        photos, videos = _get_media_samples()
        edl = _make_edl(photos, videos)
        for item in edl["segments"][0]["items"]:
            if item["media_type"] == "video":
                item["playback_speed"] = 1.5
                break
        output = _run_assemble(edl)
        errors = validate_output(output, edl)
        assert errors == [], f"Output validation failed: {errors}"

    def test_photos_only(self):
        """Test EDL with only photo items (no video)."""
        photos, videos = _get_media_samples()
        edl = _make_edl(photos, videos, n_video=0, n_photo=6, title="Photos Only")
        output = _run_assemble(edl)
        errors = validate_output(output, edl)
        assert errors == [], f"Output validation failed: {errors}"

    def test_videos_only(self):
        """Test EDL with only video items (no photos)."""
        photos, videos = _get_media_samples()
        if len(videos) < 4:
            pytest.skip("Need at least 4 videos")
        edl = _make_edl(
            photos,
            videos,
            n_photo=0,
            n_video=4,
            title="Videos Only",
            keep_audio_idx={0, 2},
        )
        output = _run_assemble(edl)
        errors = validate_output(output, edl)
        assert errors == [], f"Output validation failed: {errors}"

    def test_with_music_two_pass_loudnorm(self, tmp_path):
        """Test two-pass loudnorm with generated sine wave as music."""
        photos, videos = _get_media_samples()
        edl = _make_edl(photos, videos, keep_audio_idx={0})

        # Generate a short sine wave as music
        music_path = tmp_path / "test_music.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=30",
                str(music_path),
            ],
            capture_output=True,
            timeout=30,
        )
        assert music_path.exists(), "Failed to generate test music"

        edl["music"] = {
            "file": str(music_path),
            "volume": 0.4,
            "fade_in": 1.0,
            "fade_out": 2.0,
        }
        output = _run_assemble(edl)
        errors = validate_output(output, edl)
        assert errors == [], f"Output validation failed: {errors}"

        # Verify output has audio
        info = _probe(output)
        a_streams = [
            s for s in info.get("streams", []) if s.get("codec_type") == "audio"
        ]
        assert len(a_streams) >= 1, "Output should have audio stream with music"
