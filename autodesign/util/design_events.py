"""Append-only design-session events for taste-memory research."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .io import ensure_dirs
from ..quality_assets import get_visual_profile


EDITABLE_LAYER_FIELDS: tuple[str, ...] = (
    "text",
    "font_family",
    "font_size_px",
    "align",
    "bbox",
    "z_index",
    "effects",
)

# Unicode characters injected by the in-place editor's drag-handle span.
# These appear as leading/trailing noise in text payloads captured from
# contenteditable divs before the handle span has been decomposed.
_HANDLE_CHARS_RE = re.compile(r"[\u2922\u2923\u21F1\u21F2\u2B0C\u2B0D]+")


def session_events_path(out_dir: Path, conversation_id: str) -> Path:
    safe = _safe_id(conversation_id)
    return out_dir / "design_sessions" / f"{safe}.jsonl"


def append_design_event(
    out_dir: Path,
    conversation_id: str,
    event: str,
    *,
    run_id: str | None = None,
    artifact_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> Path:
    path = session_events_path(out_dir, conversation_id)
    ensure_dirs(path.parent)
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        "conversation_id": conversation_id,
        "run_id": run_id,
        "artifact_id": artifact_id,
        "data": data or {},
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    return path


def attachment_event_data(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return {
        "name": path.name,
        "suffix": path.suffix.lower(),
        "size": size,
    }


def layer_edit_events(
    before_layers: list[dict[str, Any]],
    after_layers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    before = {
        str(layer.get("layer_id")): _editable_layer_snapshot(layer)
        for layer in before_layers
        if layer.get("layer_id")
    }
    after = {
        str(layer.get("layer_id")): _editable_layer_snapshot(layer)
        for layer in after_layers
        if layer.get("layer_id")
    }
    events: list[dict[str, Any]] = []
    for layer_id in sorted(set(before) | set(after)):
        b = before.get(layer_id)
        a = after.get(layer_id)
        if b == a:
            continue
        events.append({
            "layer_id": layer_id,
            "before": b,
            "after": a,
            "edit_intent": _classify_edit_intent(b, a),
        })
    return events


def style_snapshot_from_spec(spec: Any) -> dict[str, Any]:
    """Extract a compact style fingerprint from a `DesignSpec` instance.

    Captures the dimensions of design taste that are hardest to recover
    post-hoc: palette choices, font hierarchy, layout density (text vs
    image layer ratio), figure source (ingest vs generated), and design
    system name for landings/decks.

    Called by `runner.py` immediately after a successful run and written
    as an ``artifact.style_snapshot`` event so the memory extractor can
    diff user-requested-style vs final-accepted-style without re-parsing
    HTML artifacts.
    """
    if spec is None:
        return {}

    layer_graph: list[Any] = getattr(spec, "layer_graph", None) or []

    # --- palette & typography (declared by planner on DesignSpec) ---
    palette: list[str] = list(getattr(spec, "palette", None) or [])
    typography: dict[str, str] = dict(getattr(spec, "typography", None) or {})
    mood: list[str] = list(getattr(spec, "mood", None) or [])
    composition_notes: str = str(getattr(spec, "composition_notes", "") or "")
    visual_profile_id: str | None = getattr(spec, "visual_profile", None)
    visual_profile = get_visual_profile(visual_profile_id)

    # --- layout density: ratio of image/bg layers to text layers ---
    n_text = sum(1 for n in layer_graph if getattr(n, "kind", None) == "text")
    n_image = sum(
        1 for n in layer_graph
        if getattr(n, "kind", None) in ("image", "background")
    )
    n_total = len(layer_graph)

    # --- figure source: ingest (from paper) vs generated (NBP) ---
    # Ingest layer_ids start with "ingest_"; generated images use generate_image tool
    # and tend to have layer_ids like "bg_*" or "img_*" without the prefix.
    n_ingest_figures = sum(
        1 for n in layer_graph
        if getattr(n, "kind", None) in ("image", "background")
        and str(getattr(n, "layer_id", "")).startswith("ingest_")
    )
    n_generated_figures = n_image - n_ingest_figures

    # --- design system (landing) ---
    ds = getattr(spec, "design_system", None)
    design_system: str | None = getattr(ds, "style", None)
    accent_color: str | None = getattr(ds, "accent_color", None)

    # --- canvas dimensions ---
    canvas: dict[str, Any] = dict(getattr(spec, "canvas", None) or {})

    # --- artifact type ---
    artifact_type_raw = getattr(spec, "artifact_type", None)
    artifact_type: str = (
        artifact_type_raw.value
        if hasattr(artifact_type_raw, "value")
        else str(artifact_type_raw or "poster")
    )

    return {
        "artifact_type": artifact_type,
        "palette": palette,
        "typography": typography,
        "mood": mood,
        "composition_notes": composition_notes[:200] if composition_notes else "",
        "visual_profile": (
            visual_profile.snapshot() if visual_profile is not None
            else visual_profile_id
        ),
        "design_system": design_system,
        "accent_color": accent_color,
        "canvas": {k: canvas[k] for k in ("w_px", "h_px") if k in canvas},
        "n_layers": n_total,
        "n_text_layers": n_text,
        "n_image_layers": n_image,
        "n_ingest_figures": n_ingest_figures,
        "n_generated_figures": n_generated_figures,
        "layout_density": round(n_image / n_total, 2) if n_total > 0 else 0.0,
    }


def _editable_layer_snapshot(layer: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in EDITABLE_LAYER_FIELDS:
        if key not in layer:
            continue
        value = layer.get(key)
        if key == "text" and isinstance(value, str):
            # Strip drag-handle characters that the in-place editor may
            # inject as leading/trailing noise inside contenteditable divs.
            value = _HANDLE_CHARS_RE.sub("", value).strip()
        if key == "effects" and isinstance(value, dict):
            fill = value.get("fill")
            if fill is not None:
                out["fill"] = fill
            continue
        out[key] = value
    return out


def _classify_edit_intent(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> str:
    """Return a coarse edit intent label for the memory extractor.

    Labels (ordered by priority — first match wins):
      ``style_change``      — any of fill / font_family changed
      ``font_size_change``  — font_size_px changed (no other style fields)
      ``alignment_change``  — align changed
      ``layout_change``     — bbox changed
      ``text_content_change`` — only text changed
      ``multi_field_change``  — more than one field category changed
      ``unknown``           — before or after is None (layer added/removed)
    """
    if before is None or after is None:
        return "unknown"

    changed: set[str] = set()
    for field in EDITABLE_LAYER_FIELDS:
        bv = before.get(field)
        av = after.get(field)
        # "fill" is normalised onto the top-level snapshot (from effects)
        bfill = before.get("fill")
        afill = after.get("fill")
        if bv != av:
            changed.add(field)
        if bfill != afill and "fill" not in changed:
            changed.add("fill")

    if not changed:
        return "no_change"

    style_fields = {"fill", "font_family"}
    is_style   = bool(changed & style_fields)
    is_size    = "font_size_px" in changed
    is_align   = "align" in changed
    is_bbox    = "bbox" in changed
    is_text    = "text" in changed

    n_categories = sum([is_style, is_size, is_align, is_bbox, is_text])
    if n_categories > 1:
        return "multi_field_change"
    if is_style:
        return "style_change"
    if is_size:
        return "font_size_change"
    if is_align:
        return "alignment_change"
    if is_bbox:
        return "layout_change"
    if is_text:
        return "text_content_change"
    return "unknown"


def infer_follow_up_sentiment(brief: str) -> str:
    """Classify a follow-up brief as a weak signal about the prior artifact.

    Used when ``has_baseline=True`` (the user is sending a new request in the
    same conversation, so a previous artifact exists). The classification is
    intentionally coarse — it's a research heuristic, not a product feature.

    Returns one of:
      ``"positive"``   — user builds on / extends the existing artifact
                         (e.g. "also add a results section", "make it bigger")
      ``"negative"``   — user rejects / restarts from scratch
                         (e.g. "that's wrong", "redo this", "start over")
      ``"corrective"`` — user points out a specific fix needed
                         (e.g. "the title font is too small", "wrong color")
      ``"neutral"``    — can't tell (new topic, vague brief, short text)
    """
    text = brief.lower().strip()

    # Negative / rejection patterns (check first — higher signal)
    _NEGATIVE = (
        "start over", "redo", "restart", "from scratch", "completely different",
        "that's wrong", "that is wrong", "thats wrong", "not what i wanted",
        "not what i asked", "totally wrong", "nope", "no good", "terrible",
        "awful", "horrible", "ugly", "garbage", "trash", "delete it",
        "discard", "forget it", "try again", "try a completely",
        "don't like", "dont like", "i don't like", "i dont like",
        "doesn't look", "doesnt look", "looks bad", "looks wrong",
        "重新", "重做", "不对", "完全不", "不是我想要的", "重新来", "全部重来",
    )
    for pat in _NEGATIVE:
        if pat in text:
            return "negative"

    # Positive / extension patterns
    _POSITIVE = (
        "also add", "also include", "in addition", "additionally", "furthermore",
        "extend", "add a", "add more", "add the", "more sections", "another section",
        "looks good", "nice", "great", "perfect", "love it", "keep the",
        "build on", "based on this", "using this", "on top of this",
        "now add", "next add", "and add", "and include",
        "好的", "不错", "很好", "在此基础上", "继续", "另外加", "还有",
    )
    for pat in _POSITIVE:
        if pat in text:
            return "positive"

    # Corrective patterns (specific fixup without full rejection)
    _CORRECTIVE = (
        "too small", "too big", "too large", "too dark", "too light",
        "too long", "too short", "wrong color", "wrong font", "wrong size",
        "change the", "fix the", "fix this", "update the", "adjust the",
        "replace the", "swap the", "move the", "resize", "recolor",
        "font is", "color is", "size is", "should be", "needs to be",
        "make it", "make the", "use instead", "use a different",
        "太小", "太大", "太暗", "太亮", "换一个", "改一下", "调整",
        "字体", "颜色", "大小", "换成",
    )
    for pat in _CORRECTIVE:
        if pat in text:
            return "corrective"

    return "neutral"


def _safe_id(raw: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw.strip())
    return clean[:120] or "unknown"
