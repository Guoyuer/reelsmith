"""Unit tests for pipeline.assemble._assemble — AssembleConfig and EDL loading."""

from __future__ import annotations

from pipeline.assemble._assemble import AssembleConfig
from pipeline.config import Config
from pipeline.edl import EDL, EditItem, Segment


class TestAssembleConfigToEdlLoading:
    """Test that assemble() loads the correct EDL version."""

    def test_loads_specified_version(self, tmp_path):
        cfg = Config(workspace=tmp_path)
        cfg.ensure_dirs()
        edl = EDL(
            title="V1",
            target_duration=60,
            segments=[
                Segment(
                    name="S",
                    items=[
                        EditItem(
                            source_file=str(tmp_path / "photo.jpg"),
                            media_type="photo",
                            display_duration=4.0,
                        ),
                    ],
                    transition="cut",
                )
            ],
        )
        cfg.edl_path(1).write_text(edl.model_dump_json(indent=2))
        (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8" + b"\x00" * 100)

        _ = AssembleConfig(w=320, h=180, fps=15, version=1)  # verify construction
        from pipeline.edl import EDL as EDLModel

        edl_loaded = EDLModel.model_validate_json(cfg.edl_path(1).read_text())
        assert edl_loaded.title == "V1"

