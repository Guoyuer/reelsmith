"""Tests for pipeline.filtergraph — typed FFmpeg filter graph builder."""

from pipeline.filtergraph import FilterGraph, FilterNode


class TestFilterNode:
    def test_compile_with_params(self):
        node = FilterNode("scale", {"w": "3840", "h": "2160"}, ["0:v"], "scaled")
        assert node.compile() == "[0:v]scale=w=3840:h=2160[scaled]"

    def test_compile_no_params(self):
        node = FilterNode("split", {}, ["0:v"], "bg] [fg")
        assert node.compile() == "[0:v]split[bg] [fg]"

    def test_compile_multiple_inputs(self):
        node = FilterNode("overlay", {"x": "(W-w)/2"}, ["bg", "fg"], "out")
        assert node.compile() == "[bg][fg]overlay=x=(W-w)/2[out]"


class TestFilterGraph:
    def test_empty_graph(self):
        fg = FilterGraph()
        assert fg.compile() == ""
        assert len(fg) == 0

    def test_single_node(self):
        fg = FilterGraph()
        fg.add("scale", {"w": "320", "h": "180"}, ["0:v"], "out")
        assert fg.compile() == "[0:v]scale=w=320:h=180[out]"

    def test_chaining(self):
        fg = FilterGraph()
        fg.add("scale", {"w": "640"}, ["0:v"], "s").add("eq", {"contrast": "1.1"}, ["s"], "out")
        result = fg.compile()
        assert "[0:v]scale=w=640[s]" in result
        assert "[s]eq=contrast=1.1[out]" in result
        assert ";" in result

    def test_raw_filter(self):
        fg = FilterGraph()
        fg.add_raw("zoompan=z='1.1':d=100:s=320x180:fps=24", ["0:v"], "out")
        assert "zoompan=z='1.1'" in fg.compile()

    def test_validate_missing_input(self):
        fg = FilterGraph()
        fg.add("eq", {"contrast": "1.1"}, ["nonexistent"], "out")
        errors = fg.validate()
        assert len(errors) == 1
        assert "nonexistent" in errors[0]

    def test_validate_stream_ref_ok(self):
        fg = FilterGraph()
        fg.add("scale", {"w": "320"}, ["0:v"], "out")
        assert fg.validate() == []

    def test_validate_duplicate_output(self):
        fg = FilterGraph()
        fg.add("scale", {"w": "320"}, ["0:v"], "out")
        fg.add("eq", {"contrast": "1.1"}, ["out"], "out")
        errors = fg.validate()
        assert any("multiple" in e for e in errors)

    def test_validate_connected_chain(self):
        fg = FilterGraph()
        fg.add("scale", {"w": "640"}, ["0:v"], "scaled")
        fg.add("eq", {"contrast": "1.1"}, ["scaled"], "graded")
        fg.add("unsharp", {"lx": "3"}, ["graded"], "out")
        assert fg.validate() == []

    def test_compile_raises_on_invalid(self):
        fg = FilterGraph()
        fg.add("eq", {"contrast": "1.1"}, ["missing_label"], "out")
        try:
            fg.compile()
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "missing_label" in str(e)


