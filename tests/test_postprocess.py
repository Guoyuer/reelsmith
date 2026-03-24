"""Tests for pipeline.plan._postprocess — EDL post-processing functions."""

from __future__ import annotations

import json

from pipeline.edl import EDL, EditItem, Segment
from pipeline.plan._postprocess import (
    deduplicate_items,
    fix_hallucinated_paths,
    log_edl_summary,
    parse_and_convert_timestamps,
    validate_and_fix_edl,
    validate_trim_points,
)


def _make_edl(items=None, segments=None) -> EDL:
    if segments:
        return EDL(title="Test", target_duration=60.0, segments=segments)
    if items is None:
        items = [
            EditItem(
                source_file="/media/photo.jpg", media_type="photo", display_duration=4.0
            )
        ]
    return EDL(
        title="Test",
        target_duration=60.0,
        segments=[Segment(name="S1", items=items, transition="cut")],
    )


# ---------------------------------------------------------------------------
# parse_and_convert_timestamps
# ---------------------------------------------------------------------------


class TestParseAndConvertTimestamps:
    def test_basic_parse(self):
        raw = {
            "title": "Test",
            "target_duration": 60,
            "segments": [
                {
                    "name": "S1",
                    "items": [
                        {
                            "source_file": "a.jpg",
                            "media_type": "photo",
                            "display_duration": 4.0,
                        }
                    ],
                }
            ],
        }
        edl = parse_and_convert_timestamps(json.dumps(raw), [])
        assert edl.title == "Test"
        assert len(edl.all_items()) == 1

    def test_music_string_becomes_none(self):
        raw = {
            "title": "T",
            "target_duration": 60,
            "music": "some string",
            "segments": [
                {
                    "name": "S",
                    "items": [
                        {
                            "source_file": "a.jpg",
                            "media_type": "photo",
                            "display_duration": 4.0,
                        }
                    ],
                }
            ],
        }
        edl = parse_and_convert_timestamps(json.dumps(raw), [])
        assert edl.music is None

    def test_preview_timestamps_converted(self):
        # offset_table: item 1 starts at 0.0, duration 30s
        offset_table = [(1, 30.0, 0.0)]
        raw = {
            "title": "T",
            "target_duration": 60,
            "segments": [
                {
                    "name": "S",
                    "items": [
                        {
                            "source_file": "v.mp4",
                            "media_type": "video",
                            "display_duration": 5.0,
                            "preview_start": "00:05",
                            "preview_end": "00:10",
                        }
                    ],
                }
            ],
        }
        edl = parse_and_convert_timestamps(json.dumps(raw), offset_table)
        item = edl.all_items()[0]
        assert item.start_time == 5.0
        assert item.end_time == 10.0

    def test_preview_not_in_any_clip_kept_as_is(self):
        # offset_table doesn't cover the preview timestamp range
        offset_table = [(1, 10.0, 0.0)]  # only covers 0-10s
        raw = {
            "title": "T",
            "target_duration": 60,
            "segments": [
                {
                    "name": "S",
                    "items": [
                        {
                            "source_file": "v.mp4",
                            "media_type": "video",
                            "display_duration": 5.0,
                            "preview_start": "01:00",  # 60s, way past the offset table
                            "preview_end": "01:05",
                        }
                    ],
                }
            ],
        }
        edl = parse_and_convert_timestamps(json.dumps(raw), offset_table)
        item = edl.all_items()[0]
        # Should not have start_time/end_time set (preview not found)
        assert item.start_time is None

    def test_strips_markdown_fences(self):
        raw = {
            "title": "T",
            "target_duration": 60,
            "segments": [
                {
                    "name": "S",
                    "items": [
                        {
                            "source_file": "a.jpg",
                            "media_type": "photo",
                            "display_duration": 4.0,
                        }
                    ],
                }
            ],
        }
        fenced = "```json\n" + json.dumps(raw) + "\n```"
        edl = parse_and_convert_timestamps(fenced, [])
        assert edl.title == "T"

    def test_minimum_2s_clip_guard(self):
        # Preview range is only 1s → should expand to at least 2s
        offset_table = [(1, 30.0, 0.0)]
        raw = {
            "title": "T",
            "target_duration": 60,
            "segments": [
                {
                    "name": "S",
                    "items": [
                        {
                            "source_file": "v.mp4",
                            "media_type": "video",
                            "display_duration": 5.0,
                            "preview_start": "00:10",
                            "preview_end": "00:11",  # only 1s
                        }
                    ],
                }
            ],
        }
        edl = parse_and_convert_timestamps(json.dumps(raw), offset_table)
        item = edl.all_items()[0]
        assert item.end_time - item.start_time >= 2.0


# ---------------------------------------------------------------------------
# fix_hallucinated_paths
# ---------------------------------------------------------------------------


class TestFixHallucinatedPaths:
    def test_existing_path_kept(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"\xff\xd8")
        edl = _make_edl(
            [EditItem(source_file=str(photo), media_type="photo", display_duration=4.0)]
        )
        removed = fix_hallucinated_paths(edl, tmp_path)
        assert removed == 0
        assert len(edl.all_items()) == 1

    def test_missing_path_removed(self, tmp_path):
        edl = _make_edl(
            [
                EditItem(
                    source_file="/nonexistent/photo.jpg",
                    media_type="photo",
                    display_duration=4.0,
                )
            ]
        )
        removed = fix_hallucinated_paths(edl, tmp_path)
        assert removed == 1
        assert len(edl.all_items()) == 0

    def test_fuzzy_match_fixes_prefixed_file(self, tmp_path):
        real = tmp_path / "87681_IMG_001.jpg"
        real.write_bytes(b"\xff\xd8")
        edl = _make_edl(
            [
                EditItem(
                    source_file="/wrong/path/IMG_001.jpg",
                    media_type="photo",
                    display_duration=4.0,
                )
            ]
        )
        removed = fix_hallucinated_paths(edl, tmp_path)
        assert removed == 0
        assert edl.all_items()[0].source_file == str(real)

    def test_empty_segment_removed(self, tmp_path):
        edl = _make_edl(
            segments=[
                Segment(
                    name="S1",
                    items=[
                        EditItem(
                            source_file="/nonexistent.jpg",
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="cut",
                ),
                Segment(
                    name="S2",
                    items=[
                        EditItem(
                            source_file=str(tmp_path / "exists.jpg"),
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="cut",
                ),
            ]
        )
        (tmp_path / "exists.jpg").write_bytes(b"\xff\xd8")
        fix_hallucinated_paths(edl, tmp_path)
        assert len(edl.segments) == 1
        assert edl.segments[0].name == "S2"


# ---------------------------------------------------------------------------
# validate_trim_points
# ---------------------------------------------------------------------------


class TestValidateTrimPoints:
    def test_valid_trims_unchanged(self):
        edl = _make_edl(
            [
                EditItem(
                    source_file="/media/clip.mp4",
                    media_type="video",
                    display_duration=5.0,
                    start_time=10.0,
                    end_time=20.0,
                )
            ]
        )
        analysis = {"1": {"local_path": "/media/clip.mp4", "video_duration": 60.0}}
        fixed, removed = validate_trim_points(edl, analysis)
        assert fixed == 0
        assert removed == 0
        assert edl.all_items()[0].start_time == 10.0

    def test_start_past_duration_clamped(self):
        edl = _make_edl(
            [
                EditItem(
                    source_file="/media/clip.mp4",
                    media_type="video",
                    display_duration=5.0,
                    start_time=120.0,
                    end_time=125.0,
                )
            ]
        )
        analysis = {"1": {"local_path": "/media/clip.mp4", "video_duration": 60.0}}
        fixed, removed = validate_trim_points(edl, analysis)
        assert fixed == 1  # start clamped to vid_dur-2, end clamped to vid_dur
        assert removed == 0
        assert edl.all_items()[0].start_time == 58.0
        assert edl.all_items()[0].end_time == 60.0

    def test_end_past_duration_clamped(self):
        edl = _make_edl(
            [
                EditItem(
                    source_file="/media/clip.mp4",
                    media_type="video",
                    display_duration=5.0,
                    start_time=50.0,
                    end_time=90.0,
                )
            ]
        )
        analysis = {"1": {"local_path": "/media/clip.mp4", "video_duration": 60.0}}
        fixed, _ = validate_trim_points(edl, analysis)
        assert fixed >= 1
        assert edl.all_items()[0].end_time == 60.0

    def test_photo_items_unaffected(self):
        edl = _make_edl(
            [
                EditItem(
                    source_file="/media/photo.jpg",
                    media_type="photo",
                    display_duration=4.0,
                )
            ]
        )
        fixed, removed = validate_trim_points(edl, {})
        assert fixed == 0
        assert removed == 0
        assert len(edl.all_items()) == 1


# ---------------------------------------------------------------------------
# deduplicate_items
# ---------------------------------------------------------------------------


class TestDeduplicateItems:
    def test_no_duplicates_unchanged(self):
        edl = _make_edl(
            [
                EditItem(source_file="a.jpg", media_type="photo", display_duration=4.0),
                EditItem(source_file="b.jpg", media_type="photo", display_duration=3.0),
            ]
        )
        removed = deduplicate_items(edl)
        assert removed == 0
        assert len(edl.all_items()) == 2

    def test_duplicate_removed(self):
        edl = _make_edl(
            segments=[
                Segment(
                    name="S1",
                    items=[
                        EditItem(
                            source_file="a.mp4",
                            media_type="video",
                            display_duration=5.0,
                        )
                    ],
                    transition="cut",
                ),
                Segment(
                    name="S2",
                    items=[
                        EditItem(
                            source_file="a.mp4",
                            media_type="video",
                            display_duration=5.0,
                        ),
                        EditItem(
                            source_file="b.jpg",
                            media_type="photo",
                            display_duration=3.0,
                        ),
                    ],
                    transition="cut",
                ),
            ]
        )
        removed = deduplicate_items(edl)
        assert removed == 1
        all_files = [i.source_file for i in edl.all_items()]
        assert len(set(all_files)) == len(all_files)

    def test_empty_segment_cleaned(self):
        edl = _make_edl(
            segments=[
                Segment(
                    name="S1",
                    items=[
                        EditItem(
                            source_file="a.jpg",
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="cut",
                ),
                Segment(
                    name="S2",
                    items=[
                        EditItem(
                            source_file="a.jpg",
                            media_type="photo",
                            display_duration=3.0,
                        )
                    ],
                    transition="cut",
                ),
            ]
        )
        deduplicate_items(edl)
        assert len(edl.segments) == 1


# ---------------------------------------------------------------------------
# validate_and_fix_edl
# ---------------------------------------------------------------------------


class TestValidateAndFixEdl:
    def test_valid_edl_no_crash(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"\xff\xd8")
        edl = _make_edl(
            [EditItem(source_file=str(photo), media_type="photo", display_duration=4.0)]
        )
        validate_and_fix_edl(edl)  # should not raise

    def test_fixes_video_media_type_on_photo_file(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"\xff\xd8")
        edl = _make_edl(
            [EditItem(source_file=str(photo), media_type="video", display_duration=5.0)]
        )
        validate_and_fix_edl(edl)
        assert edl.all_items()[0].media_type == "photo"
        assert edl.all_items()[0].effect == "ken_burns_in"
        assert edl.all_items()[0].keep_audio is False


# ---------------------------------------------------------------------------
# log_edl_summary
# ---------------------------------------------------------------------------


class TestLogEdlSummary:
    def test_logs_summary_without_error(self, caplog):
        import logging

        edl = _make_edl(
            [
                EditItem(source_file="a.jpg", media_type="photo", display_duration=4.0),
                EditItem(
                    source_file="b.mp4",
                    media_type="video",
                    display_duration=5.0,
                    keep_audio=True,
                    playback_speed=1.5,
                ),
            ]
        )
        with caplog.at_level(logging.INFO, logger="vlog.plan"):
            log_edl_summary(edl, 60)
        # Verify key info was logged
        log_text = caplog.text
        assert "PARSED EDL" in log_text
        assert "Test" in log_text  # title
        assert "1 photos" in log_text or "photo" in log_text
        assert "1 videos" in log_text or "video" in log_text
