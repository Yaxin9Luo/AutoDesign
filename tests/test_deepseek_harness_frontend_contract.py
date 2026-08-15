from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class DeepSeekHarnessFrontendContractTests(unittest.TestCase):
    def test_settings_exposes_deepseek_harness_models_key_and_upgrade_guidance(self) -> None:
        api_settings = (REPO_ROOT / "web/src/lib/api_settings.ts").read_text(
            encoding="utf-8"
        )
        settings_drawer = (REPO_ROOT / "web/src/components/SettingsDrawer.tsx").read_text(
            encoding="utf-8"
        )
        translations = (REPO_ROOT / "web/src/lib/i18n.ts").read_text(encoding="utf-8")

        self.assertIn('| "deepseek"', api_settings)
        self.assertIn('id: "deepseek"', settings_drawer)
        self.assertIn('"deepseek-v4-flash"', settings_drawer)
        self.assertIn('"deepseek-v4-pro"', settings_drawer)
        self.assertIn('selectedHarness === "deepseek"', settings_drawer)
        self.assertIn("npm install -g @deepseek-ai/dsh@latest", settings_drawer)
        self.assertIn("DeepSeek Harness", translations)
        self.assertIn("DEEPSEEK_API_KEY", translations)


if __name__ == "__main__":
    unittest.main()
