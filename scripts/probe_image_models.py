"""Probe candidate OpenRouter image-modality models for the v2.7.5 default.

Quick survey: hit each candidate with one tiny generation and report
HTTP status / shape of the response. Used once to pick the new
`DEFAULT_IMAGE_MODEL`. Skip if `OPENROUTER_API_KEY` is unset.

Cost: ~$0.05 per model that succeeds (~$0.20 total worst-case for all 4
candidates). Run: `uv run python scripts/probe_image_models.py`.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any


CANDIDATES = [
    "google/gemini-2.5-flash-image",
    "google/gemini-3.1-flash-image-preview",
    "google/gemini-3-pro-image-preview",
    "openai/gpt-5-image-mini",
    "openai/gpt-5-image",
]


def _probe(client, model: str) -> tuple[str, str]:
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": "A single tiny abstract square in muted blue. No text.",
            }],
            extra_body={
                "modalities": ["image", "text"],
                "image_config": {"aspect_ratio": "1:1", "image_size": "1K"},
            },
        )
    except Exception as e:
        return ("error", f"{type(e).__name__}: {e}")

    dt = time.time() - t0
    if not resp.choices:
        return ("error", f"no choices ({dt:.1f}s)")

    msg = resp.choices[0].message.model_dump()
    images = msg.get("images") or []
    if images:
        url = (images[0].get("image_url") or {}).get("url") or ""
        head = url[:48] + ("..." if len(url) > 48 else "")
        return ("ok", f"{len(images)} image(s), url[:48]={head} ({dt:.1f}s)")
    content = msg.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                return ("ok", f"image embedded in content_parts ({dt:.1f}s)")
    return ("error", f"no image part returned ({dt:.1f}s) — msg keys: {sorted(msg.keys())}")


def main() -> int:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        # try .env
        from pathlib import Path
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    break
    if not api_key:
        print("OPENROUTER_API_KEY not set — skipping live probe.")
        return 0

    from openai import OpenAI

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )

    print(f"Probing {len(CANDIDATES)} candidates via OpenRouter...\n")
    results: list[tuple[str, str, str]] = []
    for model in CANDIDATES:
        print(f"  -> {model} ...", flush=True)
        status, detail = _probe(client, model)
        results.append((model, status, detail))
        print(f"     [{status}] {detail}")

    print("\nSummary:")
    for model, status, detail in results:
        marker = "PASS" if status == "ok" else "FAIL"
        print(f"  {marker:5s} {model:42s} {detail}")

    any_ok = any(s == "ok" for _, s, _ in results)
    return 0 if any_ok else 1


if __name__ == "__main__":
    sys.exit(main())
