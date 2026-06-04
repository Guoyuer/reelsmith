# Render Pipeline Refactor TODO

Last updated: 2026-06-04

Follow-up cleanup candidates after the RenderContext and graph-builder refactor:

1. Split `resolve_render_items()` out of `pipeline/assemble/_graph.py` into a resolver module.
   The graph builder is now mostly pure, but the resolver still imports HEIC decode and render context in the same file.

2. Split `pipeline/assemble/_assemble.py` by responsibility.
   Good seams: segment command construction, concat/music mix, output validation, and title-card routing.

3. Unify media probing.
   `pipeline/assemble/_encoder.py` has `MediaProbe`; `pipeline/plan/_preview.py` still has its own dimension probe. Prefer one shared probe service.

4. Split `tests/test_graph.py`.
   Suggested targets: fade logic, resolved-item construction, graph builder, and per-item filters.
