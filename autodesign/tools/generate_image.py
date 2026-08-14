"""generate_image — provider-neutral inline image wrapper for landing
sections and deck slides (v2.5 — was Gemini-only through v2.4).

Semantically distinct from `generate_background`:
- `generate_background` = one full-canvas raster behind EVERYTHING in a
  non-paper poster. Always text-free. Has `safe_zones` for text overlay regions.
- `generate_image` = an inline image that sits inside a landing `<section>` or
  deck slide (hero product shot, feature-card illustration, cover visual, etc.).
  No safe_zones.
  Still required to be text-free — the renderer emits any real text as
  separate contenteditable HTML.

The planner appends this layer under a section node's `children` list so
the HTML renderer can emit `<img>` alongside text flow. No PSD/SVG output
for landing — the image is inlined into the final HTML as a `data:` URI by
composite → html_renderer → write_landing_html.

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


def generate_image(args: dict[str, Any], *, ctx: ToolContext) -> ToolResultRecord:
    layer_id = args["layer_id"]
    raw_prompt = args["prompt"]
    aspect_ratio = args.get("aspect_ratio", "16:9")
    image_size = args.get("image_size", "1K")
    name = args.get("name", layer_id)

    prompt = _ensure_no_text(raw_prompt)

    prior = ctx.state["rendered_layers"].get(layer_id) or {}
    prior_sha = prior.get("sha256")
    version = ctx.next_layer_version(layer_id)
    out_path = ctx.layers_dir / f"img_{layer_id}.v{version}.png"

    backend = make_image_backend(ctx.settings)
    log("nbp.image.request", model=ctx.settings.image_model, provider=backend.name,
        aspect_ratio=aspect_ratio, image_size=image_size,
        prompt_len=len(prompt), layer_id=layer_id)

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

    ctx.raise_if_cancelled("generate_image.after_image_request")
    ctx.raise_if_cancelled("generate_image.before_write")
    out_path.write_bytes(result.data)
    ctx.raise_if_cancelled("generate_image.after_write")
    sha = sha256_file(out_path)
    ctx.state["rendered_layers"][layer_id] = {
        "layer_id": layer_id,
        "name": name,
        "kind": "image",
        "z_index": int(args.get("z_index", 1)),
        "bbox": None,                # flow layout — no pixel bbox
        "src_path": str(out_path),
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "image_size": image_size,
        "sha256": sha,
        "version": version,
    }
    log("nbp.image.saved", path=str(out_path), sha=sha[:12],
        layer_id=layer_id, version=version,
        provider=backend.name, model=ctx.settings.image_model)

    payload: dict[str, Any] = {
        "layer_id": layer_id,
        "sha256": sha,
        "width": result.width,
        "height": result.height,
        "relative_path": f"layers/img_{layer_id}.v{version}.png",
        "version": version,
    }
    if prior_sha:
        payload["supersedes_sha256"] = prior_sha
    return obs_ok(payload)
