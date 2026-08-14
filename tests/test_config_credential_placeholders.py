from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from dotenv import dotenv_values as read_dotenv_values

# Keep config's import-time dotenv load from consulting a developer's .env.
with patch("dotenv.dotenv_values", return_value={}):
    from autodesign.config import load_settings


ROOT = Path(__file__).resolve().parents[1]


class ConfigCredentialPlaceholderTest(unittest.TestCase):
    def test_env_example_leaves_openrouter_key_unset(self) -> None:
        values = read_dotenv_values(ROOT / ".env.example")
        self.assertFalse(values.get("OPENROUTER_API_KEY"))

    def test_openrouter_and_anthropic_examples_fail_loudly(self) -> None:
        for name, placeholder in (
            ("OPENROUTER_API_KEY", "sk-or-v1-..."),
            ("ANTHROPIC_API_KEY", "sk-ant-..."),
        ):
            with self.subTest(name=name), patch.dict(
                os.environ,
                {name: placeholder},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "No LLM credential"):
                    load_settings()

    def test_optional_key_examples_are_treated_as_missing_without_rejecting_dummy_keys(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "test-openrouter-key",
                "OPENAI_COMPAT_API_KEY": "dummy",
                "GEMINI_API_KEY": "AIza...",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.openai_compat_api_key, "dummy")
        self.assertEqual(settings.gemini_api_key, "")

    def test_web_bootstrap_openrouter_value_remains_accepted(self) -> None:
        bootstrap_key = "sk-or-v1-bootstrap-placeholder"
        with patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": bootstrap_key},
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.openrouter_api_key, bootstrap_key)
        self.assertEqual(settings.anthropic_api_key, bootstrap_key)

    def test_direct_openai_credentials_keep_designer_on_gpt_5_5(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_COMPAT_API_KEY": "test-openai-key",
                "OPENAI_COMPAT_BASE_URL": "https://api.openai.com/v1",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.openai_compat_api_key, "test-openai-key")
        self.assertEqual(settings.designer_model, "gpt-5.5")
        self.assertEqual(
            {
                settings.enhancer_model,
                settings.claim_graph_model,
                settings.deck_outline_model,
                settings.paper_memory_model,
                settings.critic_model,
                settings.composer_model,
                settings.ingest_model,
            },
            {"gpt-5.4-nano"},
        )

    def test_openrouter_credentials_use_routable_gpt_5_5_designer_fallback(self) -> None:
        with patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": "test-openrouter-key"},
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.designer_model, "openai/gpt-5.5")


if __name__ == "__main__":
    unittest.main()
