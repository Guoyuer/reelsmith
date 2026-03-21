"""Typed FFmpeg filter graph builder with label validation.

Replaces raw string concatenation for filter_complex construction.
Build filter chains as typed nodes, then compile() to validated string.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FilterNode:
    """A single FFmpeg filter operation."""
    name: str               # e.g. "scale", "zoompan", "eq"
    params: dict[str, str]  # e.g. {"w": "3840", "h": "2160"}
    inputs: list[str]       # e.g. ["0:v"] or ["blurred", "sharp"]
    output: str             # e.g. "scaled"

    def compile(self) -> str:
        """Compile to FFmpeg filter string: [in1][in2]name=k=v:k=v[out]"""
        in_labels = "".join(f"[{i}]" for i in self.inputs)
        out_label = f"[{self.output}]" if self.output else ""
        if self.params:
            params_str = ":".join(f"{k}={v}" for k, v in self.params.items())
            return f"{in_labels}{self.name}={params_str}{out_label}"
        return f"{in_labels}{self.name}{out_label}"


@dataclass
class FilterGraph:
    """Ordered list of filter nodes that compiles to a validated filter_complex string."""
    nodes: list[FilterNode] = field(default_factory=list)

    def add(self, name: str, params: dict[str, str] | None = None,
            inputs: list[str] | None = None, output: str = "") -> "FilterGraph":
        """Add a filter node. Returns self for chaining."""
        self.nodes.append(FilterNode(
            name=name,
            params=params or {},
            inputs=inputs or [],
            output=output,
        ))
        return self

    def add_raw(self, raw_filter: str, inputs: list[str] | None = None,
                output: str = "") -> "FilterGraph":
        """Add a raw filter string (for complex expressions not easily parameterized)."""
        self.nodes.append(FilterNode(
            name=raw_filter,
            params={},  # params baked into name
            inputs=inputs or [],
            output=output,
        ))
        return self

    def validate(self) -> list[str]:
        """Check label connectivity. Returns list of errors (empty = valid)."""
        errors = []
        produced = set()

        for node in self.nodes:
            for inp in node.inputs:
                # Stream refs like "0:v", "1:a" are always available
                if ":" in inp:
                    continue
                if inp not in produced:
                    errors.append(f"Input label [{inp}] not produced by any earlier node")
            if node.output:
                # Handle multi-output nodes like split: "bg] [fg" → ["bg", "fg"]
                labels = [lbl.strip().strip("[]") for lbl in node.output.replace("] [", "]|[").split("|")]
                for lbl in labels:
                    if lbl in produced:
                        errors.append(f"Output label [{lbl}] produced by multiple nodes")
                    produced.add(lbl)

        return errors

    def compile(self) -> str:
        """Compile all nodes to a semicolon-separated filter_complex string.

        Raises ValueError if validation fails.
        """
        errors = self.validate()
        if errors:
            raise ValueError(f"FilterGraph validation failed: {'; '.join(errors)}")

        parts = []
        for node in self.nodes:
            parts.append(node.compile())
        return ";".join(parts)

    def __len__(self) -> int:
        return len(self.nodes)


# ---------------------------------------------------------------------------
# Convenience builders for common filter patterns
# ---------------------------------------------------------------------------

def portrait_photo_graph(
    out_w: int, out_h: int, frames: int, fps: int, zoom_rate: float,
) -> FilterGraph:
    """Build filter graph for portrait photo: blurred BG + sharp FG + Ken Burns."""
    fg = FilterGraph()
    fg.add("split", inputs=["0:v"], output="bg] [fg")  # split produces two outputs
    # Blurred background
    fg.add_raw(
        f"scale=960:-1:force_original_aspect_ratio=increase,crop=960:540,"
        f"gblur=sigma=20,scale={out_w}:{out_h}",
        inputs=["bg"], output="blurred",
    )
    # Sharp foreground
    fg.add_raw(f"scale=-1:{out_h}", inputs=["fg"], output="sharp")
    # Overlay
    fg.add("overlay", {"x": "(W-w)/2", "y": "(H-h)/2"},
           inputs=["blurred", "sharp"], output="comp")
    # Ken Burns
    fg.add_raw(
        f"zoompan=z='min(zoom+{zoom_rate:.6f},1.08)':d={frames}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={out_w}x{out_h}:fps={fps}",
        inputs=["comp"], output="",
    )
    return fg


def color_grade_filter(color_temp: str = "neutral") -> str:
    """Return color grade filter string (no labels, for inline use)."""
    base = "eq=contrast=1.02:brightness=0.01:saturation=1.05"
    if color_temp == "warm":
        return f"{base},colorbalance=rs=0.02:gs=0.01:bs=-0.02"
    elif color_temp == "cool":
        return f"{base},colorbalance=rs=-0.02:gs=0.0:bs=0.02"
    return base
