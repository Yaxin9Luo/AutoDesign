"""generate_background — provider-neutral text-to-image wrapper for non-paper
poster backgrounds (v2.5 — was Gemini-only through v2.4).

Hard guarantee: appends a no-text directive to every prompt regardless of what
the planner sent, since the SDK has no native negative_prompt and our entire
pipeline assumes background rasters carry zero text. Academic paper posters
default to a native white / cream canvas and paper figures, so this tool
rejects that path unless explicitly overridden.

The actual provider (Gemini / OpenRouter+Seedream / etc) is resolved by
`image_backend.make_image_backend(settings)` from `IMAGE_MODEL` +
`IMAGE_PROVIDER` env vars. This file knows nothing about Gemini or OpenRouter.
"""

from __future__ import annotations

from typing import Any

from ._contract import ToolContext, obs_error, obs_ok
from ..image_backend import ImageGenerationError, make_image_backend
from ..schema import ToolResultRecord
from ..util.io import sha256_file
from ..util.logging import log


NO_TEXT_SUFFIX = (
    "No text, no characters, no lettering, no symbols, no logos, no watermarks."
)


def _ensure_no_text(prompt: str) -> str:
    if NO_TEXT_SUFFIX.lower() in prompt.lower():
        return prompt
    sep = "" if prompt.rstrip().endswith(".") else "."
    return f"{prompt.rstrip()}{sep} {NO_TEXT_SUFFIX}"


def generate_background(args: dict[str, Any], *, ctx: ToolContext) -> ToolResultRecord:
    layer_id = args["layer_id"]
    raw_prompt = args["prompt"]
    aspect_ratio = args.get("aspect_ratio", "3:4")
    image_size = args.get("image_size", "2K")
    safe_zones = args.get("safe_zones", [])

    if _is_paper_poster_context(ctx) and not bool(args.get("allow_paper_background")):
        return obs_error(
            "Academic paper posters should not call generate_background by default. "
            "Use a native white/cream editorial canvas and place ingested "
            "paper figures/tables as the visual content. Set "
            "allow_paper_background=true only if the user explicitly asked "
            "for generated background art.",
            category="validation",
            payload={
                "paper_poster_background_policy": "skip_generate_background",
                "allow_override_arg": "allow_paper_background",
            },
        )

    prompt = _ensure_no_text(raw_prompt)

    prior = ctx.state["rendered_layers"].get(layer_id) or {}
    prior_sha = prior.get("sha256")
    version = ctx.next_layer_version(layer_id)
    out_path = ctx.layers_dir / f"bg_{layer_id}.v{version}.png"

    backend = make_image_backend(ctx.settings)
    log("nbp.request", model=ctx.settings.image_model, provider=backend.name,
        aspect_ratio=aspect_ratio, image_size=image_size, prompt_len=len(prompt))

    try:
        request_kwargs: dict[str, Any] = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "image_size": image_size,
        }
        if getattr(ctx.cancellation_token, "can_cancel", True):
            request_kwargs["cancellation_token"] = ctx.cancellation_token
        result = backend.generate(**request_kwargs)
    except ImageGenerationError as e:
        return obs_error(str(e), category=e.category)
    except Exception as e:
        return obs_error(
            f"Image backend ({backend.name}/{ctx.settings.image_model}) error: {e}",
            category="api",
        )

    ctx.raise_if_cancelled("generate_background.after_image_request")
    ctx.raise_if_cancelled("generate_background.before_write")
    out_path.write_bytes(result.data)
    ctx.raise_if_cancelled("generate_background.after_write")
    sha = sha256_file(out_path)
    ctx.state["rendered_layers"][layer_id] = {
        "layer_id": layer_id,
        "name": "background",
        "kind": "background",
        "z_index": 0,
        "bbox": _full_canvas_bbox(ctx),
        "src_path": str(out_path),
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "image_size": image_size,
        "safe_zones": safe_zones,
        "sha256": sha,
        "version": version,
    }
    log("nbp.saved", path=str(out_path), sha=sha[:12], version=version,
        provider=backend.name, model=ctx.settings.image_model)

    payload: dict[str, Any] = {
        "layer_id": layer_id,
        "sha256": sha,
        "width": result.width,
        "height": result.height,
        "relative_path": f"layers/bg_{layer_id}.v{version}.png",
        "version": version,
    }
    if prior_sha:
        payload["supersedes_sha256"] = prior_sha
    return obs_ok(payload)


def _full_canvas_bbox(ctx: ToolContext) -> dict[str, int]:
    spec = ctx.state.get("design_spec")
    if spec is None:
        return {"x": 0, "y": 0, "w": 0, "h": 0}
    canvas = spec.canvas
    return {"x": 0, "y": 0, "w": int(canvas["w_px"]), "h": int(canvas["h_px"])}


def _is_paper_poster_context(ctx: ToolContext) -> bool:
    artifact_type = str(ctx.state.get("artifact_type") or "poster")
    spec = ctx.state.get("design_spec")
    if spec is not None:
        spec_type = getattr(getattr(spec, "artifact_type", None), "value", None)
        artifact_type = str(spec_type or artifact_type)
    if artifact_type != "poster":
        return False

    ingested = ctx.state.get("ingested") or []
    if any((s.get("type") or "").lower() == "pdf" for s in ingested if isinstance(s, dict)):
        return True

    rendered = ctx.state.get("rendered_layers") or {}
    return any(
        str(layer_id).startswith(("ingest_fig_", "ingest_table_"))
        for layer_id in rendered
    )
