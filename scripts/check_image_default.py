"""Live API check — confirm the v2.7.5 default IMAGE_MODEL actually
generates an image via the production code path (`make_image_backend`
→ `OpenRouterImageBackend.generate(...)`).

Cost: ~$0.0003 per run at the v2.7.5 default
(`google/gemini-2.5-flash-image` ≈ $0.0000003 per image token; one
512×512 generation costs single-digit cents at most). Skip when
`OPENROUTER_API_KEY` is unset.

Run: `uv run python scripts/check_image_default.py`
"""

from __future__ import annotations

import os
import sys
import time

from autodesign.config import load_settings
from autodesign.image_backend import (
    FallbackImageBackend,
    ImageGenerationError,
    make_image_backend,
)


def main() -> int:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("OPENROUTER_API_KEY not set in env — skipping live check.")
        return 0

    settings = load_settings()
    backend = make_image_backend(settings)
    primary_model = settings.image_model
    fallback_model = (settings.image_fallback_model or "").strip() or "<disabled>"
    wrapped = isinstance(backend, FallbackImageBackend)

    print(f"primary  IMAGE_MODEL          = {primary_model}")
    print(f"fallback IMAGE_FALLBACK_MODEL = {fallback_model}")
    print(f"wrapped in FallbackImageBackend = {wrapped}")
    print()
    print(f"requesting one 512x512 1:1 generation against {primary_model} ...")
    t0 = time.time()
    try:
        result = backend.generate(
            prompt=(
                "Single tiny abstract muted blue square on a soft cream "
                "background. Minimalist, geometric, ambient. "
                "No text, no characters, no lettering, no symbols, no logos."
            ),
            aspect_ratio="1:1",
            image_size="1K",
        )
    except ImageGenerationError as e:
        print(f"FAIL: {e.category}: {e}")
        return 1

    dt = time.time() - t0
    served_by = result.model
    note = " (served by FALLBACK)" if served_by != primary_model else ""
    print(
        f"PASS: {len(result.data):,} bytes PNG, "
        f"{result.width}x{result.height}, model={served_by}{note}, "
        f"elapsed={dt:.1f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
