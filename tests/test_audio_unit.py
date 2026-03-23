"""Unit tests for pipeline.assemble._audio — write_chapters."""

from __future__ import annotations

from pipeline.edl import EDL, EditItem, Segment


class TestWriteChapters:
    """Test YouTube chapter marker generation."""

    def test_writes_chapters_with_empty_durations(self, tmp_path):
        from pipeline.assemble._audio import write_chapters

        edl = EDL(
            title="T",
            target_duration=60,
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
            ],
        )
        out = tmp_path / "chapters.txt"
        write_chapters(edl, [], out)
        assert out.exists()
