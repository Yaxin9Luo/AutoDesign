"""generate_visual_reference — wireframe-to-visual-reference prototype.

This tool implements the article-inspired loop:
content-correct editable draft -> composite preview -> image-conditioned visual
reference -> compact style guidance for the planner's next editable revision.

The generated reference PNGs are advisory only. They must never become the
final artifact because final outputs need native text, editable layers, and
source-grounded figures.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from ._contract import ToolContext, obs_error, obs_ok
from ..image_backend import ImageGenerationError, ReferenceImage, make_image_backend
from ..llm_backend import make_backend
from ..schema import ArtifactType, ToolResultRecord
from ..util.design_feedback import build_design_feedback
from ..util.io import atomic_write_json, sha256_file
from ..util.logging import log
from ..util.visual_reference_contract import (
    build_visual_reference_contract,
    record_visual_reference_attempt,
)


_MAX_DECK_REFERENCES = 6
_ALLOWED_ASPECTS: tuple[str, ...] = (
    "1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "4:5", "5:4", "21:9",
)


@dataclass(frozen=True)
class _ReferenceSource:
    source_id: str
    path: Path
    kind: str
    index: int
    prompt: str
    aspect_ratio: str


def generate_visual_reference(
    args: dict[str, Any],
    *,
    ctx: ToolContext,
) -> ToolResultRecord:
    """Generate visual reference PNGs from the latest composite preview."""
    ctx.raise_if_cancelled("visual_reference.start")
    spec = ctx.state.get("design_spec")
    if spec is None:
        return obs_error(
            "generate_visual_reference requires propose_design_spec first",
            category="validation",
        )
    composite_payload = ctx.state.get("last_composite_payload") or {}
    if not composite_payload:
        return obs_error(
            "generate_visual_reference requires a prior composite preview",
            category="validation",
        )

    artifact_type = getattr(spec, "artifact_type", ArtifactType.POSTER)
    if isinstance(artifact_type, str):
        artifact_type = ArtifactType(artifact_type)
    max_items = max(1, min(int(args.get("max_items") or _MAX_DECK_REFERENCES), 12))
    image_size = str(args.get("image_size") or "2K")

    sources = _reference_sources(spec, composite_payload, ctx, max_items=max_items)
    if not sources:
        _record_visual_reference_status(
            ctx,
            status="not_found",
            error_category="not_found",
            message="generate_visual_reference could not find composite preview images",
        )
        return obs_error(
            "generate_visual_reference could not find composite preview images",
            category="not_found",
        )

    iteration = int(ctx.state.get("visual_reference_iter") or 0) + 1
    ctx.state["visual_reference_iter"] = iteration
    out_dir = ctx.run_dir / "visual_refs" / f"iter_{iteration:02d}"
    ctx.raise_if_cancelled("visual_reference.before_output_dir")
    out_dir.mkdir(parents=True, exist_ok=True)

    backend = make_image_backend(ctx.settings)
    paths: list[str] = []
    reports: list[dict[str, Any]] = []
    generated: list[dict[str, Any]] = []

    for source in sources:
        ctx.raise_if_cancelled("visual_reference.before_image_request")
        ref = _reference_image_from_path(source.path)
        log(
            "visual_reference.request",
            artifact_type=artifact_type.value,
            backend=backend.name,
            source_id=source.source_id,
            aspect_ratio=source.aspect_ratio,
            image_size=image_size,
        )
        try:
            request_kwargs: dict[str, Any] = {
                "prompt": source.prompt,
                "aspect_ratio": source.aspect_ratio,
                "image_size": image_size,
                "reference_images": [ref],
            }
            if getattr(ctx.cancellation_token, "can_cancel", True):
                request_kwargs["cancellation_token"] = ctx.cancellation_token
            result = backend.generate(**request_kwargs)
        except ImageGenerationError as e:
            _record_visual_reference_status(
                ctx,
                status=e.category,
                error_category=e.category,
                message=str(e),
            )
            return obs_error(str(e), category=e.category)
        except Exception as e:
            message = (
                f"Image backend ({backend.name}/{ctx.settings.image_model}) "
                f"reference-generation error: {type(e).__name__}: {e}"
            )
            _record_visual_reference_status(
                ctx,
                status="api",
                error_category="api",
                message=message,
            )
            return obs_error(
                message,
                category="api",
            )

        ctx.raise_if_cancelled("visual_reference.after_image_request")
        out_path = out_dir / f"{source.source_id}_reference.png"
        ctx.raise_if_cancelled("visual_reference.before_image_write")
        out_path.write_bytes(result.data)
        ctx.raise_if_cancelled("visual_reference.after_image_write")
        rel = _rel_run_path(out_path, ctx)
        paths.append(rel)
        generated.append({
            "source_id": source.source_id,
            "source_relative_path": _rel_run_path(source.path, ctx),
            "relative_path": rel,
            "sha256": sha256_file(out_path),
            "width": result.width,
            "height": result.height,
            "model": result.model,
            "backend": backend.name,
            "kind": source.kind,
            "index": source.index,
        })

        report = _interpret_visual_reference(
            out_path,
            source=source,
            spec=spec,
            ctx=ctx,
        )
        reports.append(report)

    style_anchor = _merge_style_anchor(reports, spec)
    payload: dict[str, Any] = {
        "artifact_type": artifact_type.value,
        "visual_reference_paths": paths,
        "visual_reference_reports": reports,
        "style_anchor": style_anchor,
        "visual_reference_iteration": iteration,
        "visual_reference_backend": backend.name,
        "visual_reference_model": getattr(backend, "model", ctx.settings.image_model),
        "source_spec_revision": int(ctx.state.get("spec_revision_count") or 0),
        "revision_required": bool(paths),
        "generated": generated,
    }
    atomic_write_json(out_dir / "visual_reference.json", payload)
    ctx.state["visual_reference"] = payload
    if paths:
        record_visual_reference_attempt(ctx, status="success")
        ctx.state["visual_reference_revision_required"] = True
        ctx.state["visual_reference_revision_source_spec_revision"] = int(
            ctx.state.get("spec_revision_count") or 0
        )
        ctx.state["visual_reference_revision_iteration"] = iteration
        ctx.state.pop("visual_reference_revision_spec_revision", None)
        ctx.state.pop("visual_reference_revision_composited", None)

    log(
        "visual_reference.done",
        artifact_type=artifact_type.value,
        iteration=iteration,
        references=len(paths),
        backend=backend.name,
    )
    _refresh_last_composite_feedback(ctx)
    return obs_ok(payload)


def _record_visual_reference_status(
    ctx: ToolContext,
    *,
    status: str,
    error_category: str | None = None,
    message: str | None = None,
) -> None:
    record_visual_reference_attempt(
        ctx,
        status=status,
        error_category=error_category,
        message=message,
    )
    _refresh_last_composite_feedback(ctx)


def _refresh_last_composite_feedback(ctx: ToolContext) -> None:
    payload = ctx.state.get("last_composite_payload")
    if not isinstance(payload, dict):
        return
    contract = build_visual_reference_contract(payload, ctx=ctx)
    payload.update(contract)
    iter_num = int(payload.get("iteration") or ctx.state.get("composite_iter") or 0)
    feedback = build_design_feedback(
        payload,
        artifact_type=str(payload.get("artifact_type") or ctx.state.get("artifact_type") or "unknown"),
        iteration=iter_num,
    )
    payload["design_feedback"] = feedback.model_dump(mode="json")
    ctx.state["last_design_feedback"] = feedback
    ctx.state["last_composite_payload"] = payload
    iter_dir = _iter_dir_from_payload(payload, ctx)
    if iter_dir is None:
        return
    try:
        atomic_write_json(iter_dir / "visual_reference_contract.json", {
            "artifact_type": payload.get("artifact_type"),
            "iteration": iter_num,
            **contract,
        })
        atomic_write_json(iter_dir / "design_feedback.json", payload["design_feedback"])
    except OSError:
        pass


def _reference_sources(
    spec: Any,
    payload: dict[str, Any],
    ctx: ToolContext,
    *,
    max_items: int,
) -> list[_ReferenceSource]:
    artifact_type = getattr(spec, "artifact_type", ArtifactType.POSTER)
    if isinstance(artifact_type, str):
        artifact_type = ArtifactType(artifact_type)
    if artifact_type == ArtifactType.DECK:
        return _deck_reference_sources(spec, payload, ctx, max_items=max_items)
    path = _resolve_payload_path(payload, "preview_relative_path", ctx)
    if path is None or not path.exists():
        return []
    return [_ReferenceSource(
        source_id=f"{artifact_type.value}_full",
        path=path,
        kind=artifact_type.value,
        index=0,
        prompt=_visual_prompt(spec, artifact_type=artifact_type, source_label="full artifact"),
        aspect_ratio=_aspect_ratio_for(path, spec),
    )]


def _deck_reference_sources(
    spec: Any,
    payload: dict[str, Any],
    ctx: ToolContext,
    *,
    max_items: int,
) -> list[_ReferenceSource]:
    iter_dir = _iter_dir_from_payload(payload, ctx)
    if iter_dir is None:
        return []
    slides_dir = iter_dir / "slides"
    slide_paths = sorted(slides_dir.glob("slide_*.png"))
    if not slide_paths:
        preview = _resolve_payload_path(payload, "preview_relative_path", ctx)
        if preview is None or not preview.exists():
            return []
        return [_ReferenceSource(
            source_id="deck_grid",
            path=preview,
            kind="deck",
            index=0,
            prompt=_visual_prompt(spec, artifact_type=ArtifactType.DECK, source_label="deck grid"),
            aspect_ratio=_aspect_ratio_for(preview, spec),
        )]

    slides = [
        node for node in list(getattr(spec, "layer_graph", []) or [])
        if getattr(node, "kind", None) == "slide"
    ]

    ranked: list[tuple[int, int, Path]] = []
    for idx, path in enumerate(slide_paths):
        slide = slides[idx] if idx < len(slides) else None
        ranked.append((_slide_priority(slide, idx), idx, path))
    ranked.sort(key=lambda item: (item[0], item[1]))

    selected = sorted(ranked[:max_items], key=lambda item: item[1])
    return [
        _ReferenceSource(
            source_id=f"slide_{idx:02d}",
            path=path,
            kind="deck_slide",
            index=idx,
            prompt=_visual_prompt(
                spec,
                artifact_type=ArtifactType.DECK,
                source_label=f"slide {idx + 1}",
            ),
            aspect_ratio=_aspect_ratio_for(path, spec),
        )
        for _priority, idx, path in selected
    ]


def _slide_priority(slide: Any, idx: int) -> int:
    if idx == 0:
        return 0
    role = str(getattr(slide, "role", "") or "").lower()
    name = str(getattr(slide, "name", "") or "").lower()
    if role == "cover" or "cover" in name or "title" in name:
        return 0
    children = list(getattr(slide, "children", []) or []) if slide is not None else []
    if any(getattr(c, "kind", None) in ("image", "background", "table") for c in children):
        return 1
    if any(len(str(getattr(c, "text", "") or "")) > 80 for c in children):
        return 1
    return 2


def _visual_prompt(spec: Any, *, artifact_type: ArtifactType, source_label: str) -> str:
    palette = ", ".join(str(c) for c in (getattr(spec, "palette", []) or [])[:6])
    mood = ", ".join(str(m) for m in (getattr(spec, "mood", []) or [])[:6])
    profile = str(getattr(spec, "visual_profile", "") or "unspecified")
    refs = ", ".join(str(r) for r in (getattr(spec, "references", []) or [])[:5])
    return (
        "Use the attached editable wireframe preview as a strict structure reference. "
        "Create a high-fidelity visual design reference for the next editable "
        f"{artifact_type.value} revision, focused on {source_label}. Preserve the "
        "canvas aspect ratio, reading order, content blocks, and locked asset "
        "positions. Improve layout rhythm, hierarchy, background treatment, "
        "spacing, component styling, and visual polish. Do not invent new facts, "
        "numbers, logos, or extra content. Do not make the output the final file; "
        "it is only an art-direction reference. Avoid legible new text; represent "
        "text as clean editorial blocks when possible. "
        f"Visual profile: {profile}. Palette hints: {palette or 'derive from brief'}. "
        f"Mood: {mood or 'coherent, polished, product-grade'}. "
        f"Design references: {refs or 'none specified'}."
    )


def _interpret_visual_reference(
    path: Path,
    *,
    source: _ReferenceSource,
    spec: Any,
    ctx: ToolContext,
) -> dict[str, Any]:
    ctx.raise_if_cancelled("visual_reference.interpret.start")
    fallback = _fallback_report(source=source, spec=spec)
    try:
        image_b64, media_type = _image_b64(path, max_edge=1400)
        backend = make_backend(ctx.settings, ctx.settings.critic_model, role="critic")
        prompt = (
            "Analyze this visual reference for an editable artifact revision. "
            "Return strict JSON only with keys: style_anchor, layout_guidance, "
            "reusable_motifs, spacing_type_color_notes, locked_content_warnings. "
            "Keep every list compact. Do not suggest rasterizing final text. "
            "Do not add facts or copy; focus on visual system and layout guidance. "
            f"Source id: {source.source_id}. Artifact brief: {getattr(spec, 'brief', '')[:1200]}"
        )
        messages = [backend.vision_user_message(
            image_b64=image_b64,
            media_type=media_type,
            text=prompt,
        )]
        request_kwargs: dict[str, Any] = {
            "system": (
                "You are a visual design interpreter. You convert one generated "
                "reference image into compact implementation guidance for an "
                "editable design-spec designer. Output JSON only."
            ),
            "messages": messages,
            "tools": [],
            "thinking_budget": 0,
            "max_tokens": 1800,
        }
        if getattr(ctx.cancellation_token, "can_cancel", True):
            request_kwargs["cancellation_token"] = ctx.cancellation_token
        resp = backend.create_turn(**request_kwargs)
        ctx.raise_if_cancelled("visual_reference.interpret.after_model")
        parsed = _parse_json_object(resp.text)
        if not isinstance(parsed, dict):
            return {**fallback, "interpretation_warning": "vision output was not JSON"}
        return {
            "source_id": source.source_id,
            "source_kind": source.kind,
            "index": source.index,
            "style_anchor": _as_dict(parsed.get("style_anchor")),
            "layout_guidance": _as_list(parsed.get("layout_guidance")),
            "reusable_motifs": _as_list(parsed.get("reusable_motifs")),
            "spacing_type_color_notes": _as_list(parsed.get("spacing_type_color_notes")),
            "locked_content_warnings": _as_list(parsed.get("locked_content_warnings")),
        }
    except Exception as e:
        return {
            **fallback,
            "interpretation_warning": f"{type(e).__name__}: {str(e)[:240]}",
        }


def _fallback_report(*, source: _ReferenceSource, spec: Any) -> dict[str, Any]:
    return {
        "source_id": source.source_id,
        "source_kind": source.kind,
        "index": source.index,
        "style_anchor": {
            "visual_profile": getattr(spec, "visual_profile", None),
            "palette": list(getattr(spec, "palette", []) or [])[:6],
            "typography": dict(getattr(spec, "typography", {}) or {}),
            "mood": list(getattr(spec, "mood", []) or [])[:6],
        },
        "layout_guidance": [
            "Keep the editable wireframe's content order and use the reference only for polish.",
        ],
        "reusable_motifs": [],
        "spacing_type_color_notes": [],
        "locked_content_warnings": [
            "Do not rasterize final text; preserve native text and source-grounded assets.",
        ],
    }


def _merge_style_anchor(reports: list[dict[str, Any]], spec: Any) -> dict[str, Any]:
    first = next((r.get("style_anchor") for r in reports if isinstance(r.get("style_anchor"), dict)), {})
    anchor = dict(first or {})
    anchor.setdefault("visual_profile", getattr(spec, "visual_profile", None))
    anchor.setdefault("palette", list(getattr(spec, "palette", []) or [])[:6])
    anchor.setdefault("typography", dict(getattr(spec, "typography", {}) or {}))
    anchor.setdefault("mood", list(getattr(spec, "mood", []) or [])[:6])
    anchor["reference_count"] = len(reports)
    return anchor


def _reference_image_from_path(path: Path) -> ReferenceImage:
    data = path.read_bytes()
    mime = "image/png"
    try:
        with Image.open(path) as img:
            fmt = (img.format or "").lower()
        if fmt in {"jpeg", "jpg"}:
            mime = "image/jpeg"
        elif fmt == "webp":
            mime = "image/webp"
    except Exception:
        pass
    return ReferenceImage(data=data, mime=mime, name=path.name)


def _image_b64(path: Path, *, max_edge: int) -> tuple[str, str]:
    with Image.open(path) as img:
        img = img.convert("RGB")
        longest = max(img.size)
        if longest > max_edge:
            scale = max_edge / longest
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
        from io import BytesIO
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"


def _parse_json_object(text: str) -> Any:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _resolve_payload_path(payload: dict[str, Any], key: str, ctx: ToolContext) -> Path | None:
    rel = payload.get(key)
    if not isinstance(rel, str) or not rel:
        return None
    path = Path(rel)
    if path.is_absolute():
        return path
    return ctx.run_dir / path


def _iter_dir_from_payload(payload: dict[str, Any], ctx: ToolContext) -> Path | None:
    preview = _resolve_payload_path(payload, "preview_relative_path", ctx)
    return preview.parent if preview is not None else None


def _rel_run_path(path: Path, ctx: ToolContext) -> str:
    try:
        return str(path.relative_to(ctx.run_dir))
    except ValueError:
        return str(path)


def _aspect_ratio_for(path: Path, spec: Any) -> str:
    canvas = getattr(spec, "canvas", {}) or {}
    declared = str(canvas.get("aspect_ratio") or "")
    if declared in _ALLOWED_ASPECTS:
        return declared
    try:
        with Image.open(path) as img:
            ratio = img.width / max(1, img.height)
    except Exception:
        w = float(canvas.get("w_px") or 16)
        h = float(canvas.get("h_px") or 9)
        ratio = w / max(1.0, h)
    return min(_ALLOWED_ASPECTS, key=lambda item: abs(_ratio_value(item) - ratio))


def _ratio_value(label: str) -> float:
    left, right = label.split(":", 1)
    return float(left) / float(right)
