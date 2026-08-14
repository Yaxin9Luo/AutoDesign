from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from autodesign.config import Settings
from autodesign.tools._contract import ToolContext
from autodesign.tools.composite import _write_landing_preview
from autodesign.tools.render_text_layer import render_text_png


class OptionalLocalFontsTest(unittest.TestCase):
    def test_text_render_falls_back_when_local_fonts_are_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                anthropic_api_key="",
                anthropic_base_url=None,
                gemini_api_key="",
                designer_model="designer",
                critic_model="critic",
                fonts_dir=root / "missing-fonts",
            )
            ctx = ToolContext(
                settings=settings,
                run_dir=root / "run",
                layers_dir=root / "run" / "layers",
                run_id="optional-fonts",
            )
            out_path = root / "fallback.png"

            resolved_family, was_fallback = render_text_png(
                text="AutoDesign",
                font_family="NotoSerifSC",
                font_size=48,
                fill="#111111",
                bbox={"x": 10, "y": 10, "w": 380, "h": 100},
                align="left",
                effects={},
                canvas_w=400,
                canvas_h=120,
                out_path=out_path,
                ctx=ctx,
            )

            self.assertEqual(resolved_family, "NotoSerifSC")
            self.assertTrue(was_fallback)
            self.assertTrue(out_path.exists())
            self.assertGreater(out_path.stat().st_size, 0)

    def test_landing_preview_falls_back_when_local_fonts_are_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                anthropic_api_key="",
                anthropic_base_url=None,
                gemini_api_key="",
                designer_model="designer",
                critic_model="critic",
                fonts_dir=root / "missing-fonts",
            )
            ctx = ToolContext(
                settings=settings,
                run_dir=root / "run",
                layers_dir=root / "run" / "layers",
                run_id="optional-fonts-landing",
            )
            spec = type("Spec", (), {"canvas": {"w_px": 800}, "layer_graph": []})()
            out_path = root / "landing-preview.png"

            _write_landing_preview(spec, out_path, ctx)

            self.assertTrue(out_path.exists())
            self.assertGreater(out_path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
