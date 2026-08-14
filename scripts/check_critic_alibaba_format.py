"""Live API check — sends the exact v2.7.4 critic message shape to
`qwen/qwen-vl-max` via OpenRouter (which routes to Alibaba's strict
endpoint) and confirms the request returns 200.

Reproduces the 2026-04-26 dogfood failure surface:
- assistant turn that emits ONLY tool_calls (no text content)
- tool messages echoing the tool_call_ids
- ONE collapsed user message carrying 4 (text, image_url) content
  blocks for the deferred slide PNGs
- a second assistant turn that emits more tool_calls (mirrors the
  cycle that failed on turn 4 in the real trajectory)

Cost: ~$0.05–$0.10 per run. Skip when OPENROUTER_API_KEY is unset.

Run: `uv run python scripts/check_critic_alibaba_format.py`
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys

from PIL import Image


def _tiny_png_b64() -> str:
    img = Image.new("RGB", (32, 32), (123, 45, 67))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def main() -> int:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("OPENROUTER_API_KEY not set — skipping live check.")
        return 0

    from openai import OpenAI

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )

    img_b64 = _tiny_png_b64()
    media_type = "image/jpeg"
    data_uri = f"data:{media_type};base64,{img_b64}"

    tools = [{
        "type": "function",
        "function": {
            "name": "read_slide_render",
            "description": "Fetch a slide PNG by id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slide_id": {"type": "string"},
                },
                "required": ["slide_id"],
            },
        },
    }]

    tc_ids_t1 = [f"call_strict_{i}" for i in range(4)]
    tc_ids_t2 = [f"call_strict_b{i}" for i in range(4)]
    tc_ids_t3 = [f"call_strict_c{i}" for i in range(4)]

    messages = [
        {"role": "system", "content": "You are a critic. Use the tool."},
        {"role": "user", "content": "Inspect slides S0..S7 then say 'done'."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": tc_ids_t1[i], "type": "function",
                 "function": {"name": "read_slide_render",
                              "arguments": json.dumps({"slide_id": f"S{i}"})}}
                for i in range(4)
            ],
        },
        *[
            {"role": "tool", "tool_call_id": tc_ids_t1[i],
             "content": json.dumps({"slide_id": f"S{i}",
                                    "delivered_as": "user_image_block"})}
            for i in range(4)
        ],
        {
            "role": "user",
            "content": [
                block
                for i in range(4)
                for block in (
                    {"type": "text", "text": f"[render of slide_id=S{i}]"},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                )
            ],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": tc_ids_t2[i], "type": "function",
                 "function": {"name": "read_slide_render",
                              "arguments": json.dumps({"slide_id": f"S{4 + i}"})}}
                for i in range(4)
            ],
        },
        *[
            {"role": "tool", "tool_call_id": tc_ids_t2[i],
             "content": json.dumps({"slide_id": f"S{4 + i}",
                                    "delivered_as": "user_image_block"})}
            for i in range(4)
        ],
        {
            "role": "user",
            "content": [
                block
                for i in range(4)
                for block in (
                    {"type": "text", "text": f"[render of slide_id=S{4 + i}]"},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                )
            ],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": tc_ids_t3[i], "type": "function",
                 "function": {"name": "read_slide_render",
                              "arguments": json.dumps({"slide_id": f"S{8 + i}"})}}
                for i in range(4)
            ],
        },
        *[
            {"role": "tool", "tool_call_id": tc_ids_t3[i],
             "content": json.dumps({"slide_id": f"S{8 + i}",
                                    "delivered_as": "user_image_block"})}
            for i in range(4)
        ],
        {
            "role": "user",
            "content": [
                block
                for i in range(4)
                for block in (
                    {"type": "text", "text": f"[render of slide_id=S{8 + i}]"},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                )
            ],
        },
    ]

    print(f"Sending {len(messages)} messages to qwen/qwen-vl-max via OpenRouter…")
    try:
        resp = client.chat.completions.create(
            model="qwen/qwen-vl-max",
            messages=messages,
            tools=tools,
            max_tokens=256,
        )
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return 1

    if not resp.choices:
        err = getattr(resp, "error", None)
        print(f"FAIL: empty choices; error={err}")
        return 1

    choice = resp.choices[0]
    print(f"OK: finish_reason={choice.finish_reason}; "
          f"text={(choice.message.content or '')[:80]!r}; "
          f"tool_calls={len(choice.message.tool_calls or [])}")
    usage = resp.usage
    if usage is not None:
        print(f"usage: prompt={usage.prompt_tokens} completion={usage.completion_tokens}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
