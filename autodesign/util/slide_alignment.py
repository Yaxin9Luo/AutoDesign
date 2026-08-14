"""Naive noun-phrase overlap between slide title and body content.

Detects title-body claim drift (B2). NO embeddings, NO LLM — just set overlap.
False positives are tolerable; the planner gets a hint and decides.
Operates on structural properties of the spec — no per-paper or per-source-type
heuristics.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autodesign.schema import DesignSpec, LayerNode

# Common English stopwords + filler. Conservative — better to keep tokens that
# might be meaningful than aggressively strip them.
STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "at", "for", "with", "by",
    "and", "or", "but", "as", "is", "are", "was", "were", "be", "been", "being",
    "this", "that", "these", "those", "it", "its", "we", "our", "you", "your",
    "from", "into", "via", "vs", "based", "using", "within", "while",
    "has", "have", "had", "do", "does", "did", "will", "would", "should",
    "can", "could", "may", "might", "shall", "not", "no", "nor",
    "than", "then", "there", "here", "where", "when", "why", "how", "what",
    "which", "who", "whom", "whose", "all", "any", "some", "each", "every",
    "more", "most", "other", "such", "only", "own", "same", "so", "very",
    "out", "over", "under", "again", "further", "also", "just",
})

# Min token length: drop tokens shorter than 3 chars (catches a/an/of/to/etc
# that slipped past stopwords plus single letters and most acronyms aren't
# what we want to align on)
_MIN_TOKEN_LEN = 3

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def extract_noun_phrases(text: str | None) -> set[str]:
    """Lowercase, split on non-alphanumeric, drop stopwords + tokens < 3 chars."""
    if not text:
        return set()
    tokens = _TOKEN_RE.findall(text.lower())
    return {t for t in tokens if len(t) >= _MIN_TOKEN_LEN and t not in STOPWORDS}


def collect_visible_text(slide: "LayerNode") -> str:
    """Concatenate all text-bearing descendant content (title + body + captions
    + table headers + cell text). Skips kind='background' / kind='image' which
    have no text.
    """
    parts: list[str] = []
    _collect_text_recursive(slide, parts, skip_title=False)
    return " ".join(parts)


def _collect_text_recursive(
    node: "LayerNode", parts: list[str], skip_title: bool,
) -> None:
    kind = getattr(node, "kind", None)
    role = getattr(node, "role", None)
    # Skip pure-visual nodes (no text). Text titles are identified from their
    # semantic role or conventional layer name.
    if kind in ("background", "image"):
        return
    text = getattr(node, "text", None)
    if text:
        name = (getattr(node, "name", None) or "").lower()
        is_title = (
            role == "title"
            or ("title" in name and "subtitle" not in name)
            or "headline" in name
        )
        if not (skip_title and is_title):
            parts.append(str(text))
    # Caption text on tables / images
    caption = getattr(node, "caption", None)
    if caption:
        parts.append(str(caption))
    # Table content (headers + cell rows are direct fields on LayerNode
    # when kind=='table'; not nested under a `.table` object).
    for header in (getattr(node, "headers", None) or []):
        parts.append(str(header))
    for row in (getattr(node, "rows", None) or []):
        for cell in row:
            parts.append(str(cell))
    # Recurse into children
    for child in (getattr(node, "children", None) or []):
        _collect_text_recursive(child, parts, skip_title=skip_title)


def _find_title(slide: "LayerNode") -> str | None:
    """Find title text from a text child's semantic role or layer name."""
    return _find_title_recursive(slide, is_root=True)


def _find_title_recursive(node: "LayerNode", *, is_root: bool) -> str | None:
    text = getattr(node, "text", None)
    role = getattr(node, "role", None)
    name = (getattr(node, "name", None) or "").lower()
    # Don't treat the slide itself as a title even if it has stray text.
    if not is_root and text:
        if role == "title" or ("title" in name and "subtitle" not in name) or "headline" in name:
            return str(text)
    for child in (getattr(node, "children", None) or []):
        found = _find_title_recursive(child, is_root=False)
        if found:
            return found
    return None


def slide_alignment_score(slide: "LayerNode") -> tuple[float, set[str]]:
    """Returns (score, missing_keywords).

    score = |title_phrases ∩ body_phrases| / |title_phrases|
    Returns 1.0 if title yields zero noun phrases (single stopword titles like
    "Results"). missing_keywords = title_phrases - body_phrases.
    """
    title_text = _find_title(slide)
    title_phrases = extract_noun_phrases(title_text)
    if not title_phrases:
        return 1.0, set()
    # Collect body text excluding the title to avoid trivial self-match
    body_parts: list[str] = []
    _collect_text_recursive(slide, body_parts, skip_title=True)
    body_phrases = extract_noun_phrases(" ".join(body_parts))
    intersection = title_phrases & body_phrases
    score = len(intersection) / len(title_phrases)
    missing = title_phrases - body_phrases
    return score, missing


def detect_alignment_warnings(spec: "DesignSpec") -> list[dict]:
    """Walk layer_graph for slides (kind='slide'), score each, and emit
    warnings for low-alignment slides.

    Thresholds (naive on purpose):
    - score >= 0.30: pass silently
    - 0.10 <= score < 0.30: severity="warning"
    - score < 0.10: severity="blocker"

    Warning shape: {slide_id, title, score, missing_keywords, severity}.
    """
    warnings: list[dict] = []
    for node in (getattr(spec, "layer_graph", None) or []):
        if getattr(node, "kind", None) != "slide":
            continue
        score, missing = slide_alignment_score(node)
        if score >= 0.30:
            continue
        severity = "blocker" if score < 0.10 else "warning"
        warnings.append({
            "slide_id": (
                getattr(node, "layer_id", "")
                or getattr(node, "name", "")
                or "<unknown>"
            ),
            "title": _find_title(node) or "",
            "score": round(score, 3),
            "missing_keywords": sorted(missing),
            "severity": severity,
        })
    return warnings
