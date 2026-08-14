"""edit_layer — apply a targeted subset-diff to a previously rendered text layer.

Semantics:
  - Reads current layer state from ctx.state["rendered_layers"][layer_id].
  - Merges the `diff` onto it (nested merge for bbox + effects; replace otherwise).
  - Delegates to render_text_layer which overwrites both the PNG on disk and
    the ctx.state entry. No side effects on other layers, no implicit composite.

Scope (v1.0 #5): text layers only. Background edits go through
`generate_background`; brand-asset edits go through `fetch_brand_asset`.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..schema import ToolResultRecord
from ._contract import ToolContext, obs_error, obs_ok
from .render_text_layer import render_text_layer


# Fields the planner may pass inside `diff`. Anything else is rejected so we
# don't silently accept a misspelled field (e.g. `color` instead of `fill`).
_ALLOWED_DIFF_FIELDS: frozenset[str] = frozenset({
    "text", "font_family", "font_size_px", "fill",
    "font_weight", "font_style", "line_height", "letter_spacing",
    "text_transform", "bbox", "align", "z_index", "effects",
})


def edit_layer(args: dict[str, Any], *, ctx: ToolContext) -> ToolResultRecord:
    layer_id = args.get("layer_id")
    diff = args.get("diff") or {}

    if not layer_id:
        return obs_error("edit_layer: 'layer_id' is required", category="validation")
    if not isinstance(diff, dict) or not diff:
        return obs_error(
            "edit_layer: 'diff' must be a non-empty object "
            "(subset of editable fields to merge onto current layer state)",
            category="validation",
        )

    unknown = sorted(set(diff) - _ALLOWED_DIFF_FIELDS)
    if unknown:
        return obs_error(
            f"edit_layer: unknown diff field(s) {unknown}. "
            f"Allowed: {sorted(_ALLOWED_DIFF_FIELDS)}",
            category="validation",
        )

    rendered = ctx.state.get("rendered_layers", {})
    current = rendered.get(layer_id)
    if current is None:
        html_result = _edit_html_artifact_block(
            str(layer_id),
            diff,
            finding_id=str(args.get("finding_id") or "manual"),
            ctx=ctx,
        )
        if html_result is not None:
            return html_result
        return obs_error(
            f"edit_layer: layer '{layer_id}' not found. "
            f"Available layer_ids: {sorted(rendered.keys()) or '[]'}.",
            category="not_found",
            payload={
                "available_layer_ids": sorted(rendered.keys()),
                "available_html_block_ids": _html_block_ids(ctx),
                "hint": (
                    "For HTML-first artifacts, target ids usually live in "
                    "available_html_block_ids. Use apply_design_ops html_* ops "
                    "with block_id, or call edit_layer with an editable HTML "
                    "text/caption/metric block id."
                ),
            },
        )

    if current.get("kind") != "text":
        return obs_error(
            f"edit_layer: layer '{layer_id}' has kind='{current.get('kind')}', "
            "but edit_layer only supports kind='text'.",
            category="validation",
            payload={"layer_id": layer_id, "kind": current.get("kind")},
        )

    merged = deepcopy(current)
    for k, v in diff.items():
        if k == "bbox" and isinstance(v, dict):
            merged["bbox"] = {**(merged.get("bbox") or {}), **v}
        elif k == "effects" and isinstance(v, dict):
            merged["effects"] = {**(merged.get("effects") or {}), **v}
        else:
            merged[k] = v

    render_args = {
        "layer_id": layer_id,
        "name": merged.get("name") or layer_id,
        "text": merged.get("text", ""),
        "font_family": merged.get("font_family"),
        "font_size_px": int(merged.get("font_size_px", 0)),
        "font_weight": merged.get("font_weight"),
        "font_style": merged.get("font_style"),
        "line_height": merged.get("line_height"),
        "letter_spacing": merged.get("letter_spacing"),
        "text_transform": merged.get("text_transform"),
        "fill": merged.get("fill", "#000000"),
        "bbox": merged["bbox"],
        "align": merged.get("align", "left"),
        "z_index": int(merged.get("z_index", 1)),
        "effects": merged.get("effects") or {},
    }

    result = render_text_layer(render_args, ctx=ctx)
    if result.status != "ok":
        return result

    # Augment payload with the diff fields the policy requested. render_text_layer
    # already returned sha256 + layer_id; we add `fields_changed` so the policy
    # has a record of what its edit actually touched.
    payload = dict(result.payload)
    payload["fields_changed"] = sorted(diff.keys())
    return obs_ok(payload)


def _edit_html_artifact_block(
    block_id: str,
    diff: dict[str, Any],
    *,
    finding_id: str,
    ctx: ToolContext,
) -> ToolResultRecord | None:
    if not _html_block_exists(ctx, block_id):
        return None
    ops: list[dict[str, Any]] = []
    if "bbox" in diff:
        ops.append({
            "op": "html_set_block_bbox",
            "finding_id": finding_id,
            "block_id": block_id,
            "bbox": diff["bbox"],
            "merge": True,
        })
    if "text" in diff:
        ops.append({
            "op": "html_replace_text",
            "finding_id": finding_id,
            "block_id": block_id,
            "text": diff["text"],
        })

    style_patch: dict[str, Any] = {}
    for key in (
        "font_family", "font_size_px", "fill", "font_weight", "font_style",
        "line_height", "letter_spacing", "text_transform", "align", "z_index",
    ):
        if key in diff:
            style_patch[key] = diff[key]
    if "effects" in diff:
        style_patch["effects"] = diff["effects"]
    if style_patch:
        ops.append({
            "op": "html_set_block_style",
            "finding_id": finding_id,
            "block_id": block_id,
            "style": style_patch,
            "merge": True,
        })

    if not ops:
        return obs_error(
            "edit_layer: no HTML-compatible diff fields were provided",
            category="validation",
            payload={"layer_id": block_id, "fields": sorted(diff.keys())},
        )

    from .apply_design_ops import apply_design_ops

    result = apply_design_ops({
        "ops": ops,
        "notes": "edit_layer compatibility path for html_artifact block",
    }, ctx=ctx)
    if result.status != "ok":
        return result
    payload = dict(result.payload)
    payload["layer_id"] = block_id
    payload["fields_changed"] = sorted(diff.keys())
    payload["compat_path"] = "html_artifact"
    return obs_ok(payload)


def _html_block_exists(ctx: ToolContext, block_id: str) -> bool:
    return block_id in set(_html_block_ids(ctx))


def _html_block_ids(ctx: ToolContext) -> list[str]:
    spec = ctx.state.get("design_spec")
    if spec is None:
        return []
    data = spec.model_dump(mode="json")
    out: list[str] = []

    def visit(blocks: list[Any]) -> None:
        for block in blocks:
            if not isinstance(block, dict):
                continue
            candidate = block.get("block_id") or block.get("layer_id")
            if candidate:
                out.append(str(candidate))
            children = block.get("children")
            if isinstance(children, list):
                visit(children)

    artifact = data.get("html_artifact") if isinstance(data, dict) else None
    frames = artifact.get("frames") if isinstance(artifact, dict) else []
    for frame in frames or []:
        if isinstance(frame, dict):
            visit(list(frame.get("blocks") or []))
    return sorted(set(out))
