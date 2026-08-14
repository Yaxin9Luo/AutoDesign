from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from autodesign.util import vlm


class VlmProviderRoutingTests(unittest.TestCase):
    def test_gpt_model_uses_openai_compat_with_custom_anthropic_base(self) -> None:
        settings = SimpleNamespace(
            anthropic_base_url="https://anthropic.example.test/v1",
            openai_compat_base_url="https://compat.example.test/v1",
        )

        self.assertEqual(
            vlm._provider_for_model("gpt-5.4", settings=settings),
            "openai",
        )

    def test_claude_model_keeps_anthropic_route(self) -> None:
        settings = SimpleNamespace(
            anthropic_base_url="https://anthropic.example.test/v1",
            openai_compat_base_url="https://compat.example.test/v1",
        )

        self.assertEqual(
            vlm._provider_for_model("claude-opus-4-7", settings=settings),
            "anthropic",
        )

    def test_non_anthropic_families_use_openai_compatible_route(self) -> None:
        settings = SimpleNamespace(
            anthropic_base_url="https://anthropic.example.test/v1",
            openai_compat_base_url="https://compat.example.test/v1",
        )
        for model in (
            "qwen/qwen3-vl-plus",
            "google/gemini-2.5-flash",
            "deepseek/deepseek-v3.2",
            "vendor/custom-vision-model",
        ):
            with self.subTest(model=model):
                self.assertEqual(
                    vlm._provider_for_model(model, settings=settings),
                    "openai",
                )

    def test_vlm_call_dispatches_to_openai_transport(self) -> None:
        settings = SimpleNamespace(ingest_http_timeout=30.0)
        with (
            patch.object(vlm, "_call_openai", return_value='{"ok": true}') as openai_call,
            patch.object(vlm, "_call_anthropic") as anthropic_call,
        ):
            result = vlm.vlm_call_json(
                settings=settings,
                model="deepseek/deepseek-v3.2",
                system="system",
                user_text="user",
                max_retries=0,
            )
        self.assertEqual(result, {"ok": True})
        openai_call.assert_called_once()
        anthropic_call.assert_not_called()

    def test_vlm_call_dispatches_to_anthropic_transport(self) -> None:
        settings = SimpleNamespace(ingest_http_timeout=30.0)
        with (
            patch.object(vlm, "_call_anthropic", return_value='{"ok": true}') as anthropic_call,
            patch.object(vlm, "_call_openai") as openai_call,
        ):
            result = vlm.vlm_call_json(
                settings=settings,
                model="claude-opus-4-7",
                system="system",
                user_text="user",
                max_retries=0,
            )
        self.assertEqual(result, {"ok": True})
        anthropic_call.assert_called_once()
        openai_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
