"""Section-number post-processor for deck slides (v2.7.2).

The planner can emit `SlideNode.section_number` (e.g. "§2.1") but is not
required to keep it monotonic across iterations: when a slide is added or
the order shifts, the labels go non-monotonic and audiences see "§3 → §2
→ §3.1" gaps. Cloud Design (Anthropic's consumer artifact tool) shipped
exactly this bug; v2.7.2 ships a defensive renumber pass.

Three policies, dispatched by `apply_section_policy`:

- ``renumber`` — walk the slide list in order and assign §1, §1.1, §1.2,
  §2, … using a cheap title-prefix heuristic for sub-rhythm.
- ``strip``   — clear every section_number so the renderer just shows
  the bare title.
- ``preserve`` — deepcopy passthrough; the planner's labels survive.

All three are pure functions over a list of LayerNode objects (each one
representing a deck slide with ``kind="slide"``). The input list is
NEVER mutated; copies are returned.
"""

from __future__ import annotations

import copy
import re
from typing import Iterable

from ..schema import LayerNode


SectionPolicy = str  # "renumber" | "strip" | "preserve"


_TITLE_SLOT_NAMES = ("title", "headline", "section_title")
_SUBTITLE_HINTS = ("part ii", "part iii", "continued", "(cont.)", "(cont)")


def renumber_sections(slides: list[LayerNode]) -> list[LayerNode]:
    """Walk ``slides`` in order, assign sequential section labels.

    Heuristic for sub-numbering:

    - The first slide always opens a new top-level section (§1).
    - A slide that shares a meaningful title prefix with its predecessor
      (>=2 leading words match, case-insensitive) is treated as a
      continuation of the same parent section: §1, §1.1, §1.2, …
    - Otherwise a new top-level section opens: §2, §3, …

    Cover-style slides (the very first slide, OR slides whose name/role
    contains "cover" / "thank") get NO section number — they're
    self-explanatory and a "§1 · Cover" prefix just adds noise. Same
    treatment for closing / thank-you slides at the end.

    The function returns a NEW list of NEW LayerNode copies; the input
    list and its nodes are never mutated.
    """
    out: list[LayerNode] = []
    parent_idx = 0          # latest §N counter
    sub_idx = 0             # latest §N.M counter (0 = no sub yet)
    prev_title_words: list[str] = []
    prev_was_skipped = True  # treat slot 0 as if "previous" was a non-section

    for i, slide in enumerate(slides):
        if _is_chrome_slide(slide, position=i, total=len(slides)):
            out.append(_with_section_number(slide, None))
            prev_title_words = []
            prev_was_skipped = True
            continue

        title_words = _title_words(slide)
        share = _shared_prefix_len(prev_title_words, title_words)
        is_continuation = (
            not prev_was_skipped
            and share >= 2
            and parent_idx > 0
        )

        if is_continuation:
            sub_idx = max(1, sub_idx + 1)
            label = f"§{parent_idx}.{sub_idx}"
        else:
            parent_idx += 1
            sub_idx = 0
            label = f"§{parent_idx}"

        out.append(_with_section_number(slide, label))
        prev_title_words = title_words
        prev_was_skipped = False

    return out


def strip_sections(slides: list[LayerNode]) -> list[LayerNode]:
    """Return copies of ``slides`` with every ``section_number`` set to None."""
    return [_with_section_number(s, None) for s in slides]


def apply_section_policy(
    slides: list[LayerNode], policy: SectionPolicy,
) -> list[LayerNode]:
    """Dispatch to the policy named by ``policy``.

    Unknown policies fall back to "renumber" — same conservative default
    as `_parse_section_policy` in config.py, so a typo never stops a run.
    """
    if policy == "strip":
        return strip_sections(slides)
    if policy == "preserve":
        return [copy.deepcopy(s) for s in slides]
    return renumber_sections(slides)


# ── helpers ──────────────────────────────────────────────────────────


def _with_section_number(slide: LayerNode, value: str | None) -> LayerNode:
    """Return a deep-copy of ``slide`` with ``section_number`` set.

    `model_copy` is shallow on lists/dicts; we deep-copy the children
    list separately so callers that later mutate child nodes don't
    leak back into the original spec.
    """
    new_children = [copy.deepcopy(c) for c in (slide.children or [])]
    return slide.model_copy(update={
        "section_number": value,
        "children": new_children,
    })


def _is_chrome_slide(slide: LayerNode, *, position: int, total: int) -> bool:
    """True for cover, thank-you, and section-divider slides.

    These slides are visually self-contained and a renderer-prepended
    "§1 · " prefix would clutter them. Detection is deliberately
    conservative — false positives would silently strip section
    numbers off real content slides — so we only trigger when:

    - explicit `role` is "cover" / "closing" / "section_divider"
    - slide `name` contains an unambiguous chrome keyword
      (cover / thank / q&a / closing / outro / divider)
    - first text child reads like a thank-you / Q&A line
    """
    role = (getattr(slide, "role", None) or "").lower()
    if role in ("cover", "closing", "section_divider"):
        return True
    name = (getattr(slide, "name", None) or "").lower()
    chrome_tags = (
        "cover", "thank", "thanks", "q&a", "qna",
        "closing", "outro", "divider", "section_divider",
    )
    if any(tag in name for tag in chrome_tags):
        return True
    first_text = _first_child_text(slide).lower()
    if first_text and any(
        kw in first_text for kw in (
            "thank you", "thanks!", "q & a", "q&a", "questions?",
        )
    ):
        return True
    return False


def _first_child_text(slide: LayerNode) -> str:
    for c in (slide.children or []):
        if getattr(c, "kind", None) == "text":
            t = getattr(c, "text", None)
            if t:
                return t.strip()
    return ""


def _title_words(slide: LayerNode) -> list[str]:
    """Lowercase word tokens from the slide's title.

    Looks at children with name in `_TITLE_SLOT_NAMES` first; falls back
    to the slide's own ``name``. Strips punctuation so "Vision tokenizer"
    and "Vision-tokenizer" share a prefix.
    """
    title = ""
    for c in (slide.children or []):
        cname = (getattr(c, "name", None) or "").lower()
        ckind = getattr(c, "kind", None)
        if ckind == "text" and any(slot in cname for slot in _TITLE_SLOT_NAMES):
            title = (getattr(c, "text", None) or "").strip()
            if title:
                break
    if not title:
        title = (getattr(slide, "name", None) or "").strip()
    # Drop trailing "(continued)" / "(cont.)" / "Part II" markers so they
    # match their parent slide title.
    low = title.lower()
    for hint in _SUBTITLE_HINTS:
        idx = low.find(hint)
        if idx > 0:
            title = title[:idx].strip()
            break
    tokens = re.findall(r"[A-Za-z一-鿿0-9]+", title.lower())
    # Drop one-letter tokens (e.g. "a", "I") — they create false matches.
    return [t for t in tokens if len(t) > 1]


def _shared_prefix_len(a: Iterable[str], b: Iterable[str]) -> int:
    """Number of leading tokens shared between two word lists."""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n
