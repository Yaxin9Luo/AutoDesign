from __future__ import annotations

import unittest
from typing import get_args

from autodesign.config import ImageProviderChoice, _parse_image_provider
from autodesign.image_backend import _infer_image_provider


class PublicProviderContractTests(unittest.TestCase):
    def test_image_provider_choices_are_public_backends(self) -> None:
        self.assertEqual(
            set(get_args(ImageProviderChoice)),
            {"auto", "gemini", "openrouter", "openai_compat"},
        )
        self.assertEqual(_parse_image_provider("custom"), "auto")
        self.assertEqual(_infer_image_provider("vendor/model"), "openrouter")


if __name__ == "__main__":
    unittest.main()
