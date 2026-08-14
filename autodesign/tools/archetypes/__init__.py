"""v2.8.1 deck slide archetypes — Phase 1.

Each archetype is a self-contained layout function that takes a
LayerNode (kind="slide"), a python-pptx Slide, slide dimensions, and
the runtime ToolContext, then emits native shapes onto the slide.
The dispatcher in `pptx_renderer._render_slide` looks up the renderer
by `slide.archetype` and falls back to the original inline logic when:

- the archetype is missing from `ARCHETYPE_RENDERERS` (Phase 2/3
  placeholders), OR
- the archetype is `"evidence_snapshot"` and the slide has no obvious
  big-number child (preserves byte-identical rendering for every
  pre-v2.8.1 deck whose default value is `"evidence_snapshot"`).

`PLACEHOLDER_ARCHETYPES` enumerates the archetype labels declared in
the schema for v2.8.2/v2.8.3 but not yet renderable; the dispatcher
treats them as "fall through to default render".
"""

from __future__ import annotations

from typing import Any, Callable

from .cover_editorial import render_cover_editorial
from .evidence_snapshot import has_big_number, render_evidence_snapshot
from .takeaway_list import render_takeaway_list
from .thanks_qa import render_thanks_qa


ArchetypeRenderer = Callable[[Any, Any, int, int, Any], None]


ARCHETYPE_RENDERERS: dict[str, ArchetypeRenderer] = {
    "cover_editorial": render_cover_editorial,
    "evidence_snapshot": render_evidence_snapshot,
    "takeaway_list": render_takeaway_list,
    "thanks_qa": render_thanks_qa,
}


# Phase 2/3 archetype labels declared in the schema. The dispatcher
# treats any value in this set as "fall through to default render"
# — the archetype is reserved but its renderer hasn't shipped yet.
PLACEHOLDER_ARCHETYPES: frozenset[str] = frozenset({
    "cover_technical",
    "pipeline_horizontal",
    "tension_two_column",
    "section_divider",
    "residual_stack_vertical",
    "conflict_vs_cooperation",
})


def get_renderer(archetype: str | None) -> ArchetypeRenderer | None:
    """Return the archetype renderer for `archetype`, or None when:

    - `archetype` is None / empty / unknown
    - `archetype` is a placeholder for a future phase

    The caller (pptx_renderer._render_slide) treats `None` as the
    signal to use the existing inline default-render path.
    """
    if not archetype:
        return None
    if archetype in PLACEHOLDER_ARCHETYPES:
        return None
    return ARCHETYPE_RENDERERS.get(archetype)


__all__ = [
    "ARCHETYPE_RENDERERS",
    "PLACEHOLDER_ARCHETYPES",
    "ArchetypeRenderer",
    "get_renderer",
    "has_big_number",
    "render_cover_editorial",
    "render_evidence_snapshot",
    "render_takeaway_list",
    "render_thanks_qa",
]
