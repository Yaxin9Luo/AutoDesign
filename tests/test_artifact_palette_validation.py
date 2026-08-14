from __future__ import annotations

import unittest

from autodesign.util.academic_palette import require_academic_color_system
from autodesign.util.artifact_palette_validation import validate_artifact_palette


def _artifact_html(
    color_system: dict[str, object],
    *,
    palette_id: str | None = None,
    include_palette_id: bool = True,
    omit_variable: str = "",
    variable_overrides: dict[str, str] | None = None,
    extra_css: str = "",
    extra_body: str = "",
) -> str:
    overrides = variable_overrides or {}
    declarations = ";".join(
        f"{name}:{overrides.get(name, value)}"
        for name, value in color_system["css_variables"].items()
        if name != omit_variable
    )
    selected_id = palette_id or str(color_system["palette_id"])
    palette_attribute = (
        f' data-palette-id="{selected_id}"'
        if include_palette_id
        else ""
    )
    return (
        "<!doctype html><html lang=\"en\""
        f"{palette_attribute}><head><style>"
        f":root{{{declarations}}}{extra_css}"
        "</style></head><body><main><h1>Paper artifact</h1>"
        f"{extra_body}</main></body></html>"
    )


class ArtifactPaletteValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.required = require_academic_color_system("plum_sage")

    def test_exact_selected_palette_passes(self) -> None:
        result = validate_artifact_palette(
            _artifact_html(self.required),
            "",
            self.required,
            "landing",
        )

        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["blocking_findings"], [])
        self.assertTrue(result["debug_metrics"]["palette_contract_pass"])
        self.assertEqual(result["debug_metrics"]["actual_palette_id"], "plum_sage")

    def test_collected_css_is_included_in_exact_palette_audit(self) -> None:
        declarations = ";".join(
            f"{name}:{value}"
            for name, value in self.required["css_variables"].items()
        )
        html = _artifact_html(self.required).replace(
            f":root{{{declarations}}}",
            "",
            1,
        )

        result = validate_artifact_palette(
            html,
            f":root{{{declarations}}}",
            self.required,
            "landing",
        )

        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["blocking_findings"], [])

    def test_missing_palette_id_is_blocking(self) -> None:
        result = validate_artifact_palette(
            _artifact_html(self.required, include_palette_id=False),
            "",
            self.required,
            "landing",
        )

        self.assertEqual(
            {item["issue_id"] for item in result["blocking_findings"]},
            {"landing_required_palette_id_missing"},
        )

    def test_wrong_palette_id_is_blocking(self) -> None:
        result = validate_artifact_palette(
            _artifact_html(self.required, palette_id="mulberry_mint"),
            "",
            self.required,
            "slides",
        )

        self.assertIn(
            "slides_required_palette_id_mismatch",
            {item["issue_id"] for item in result["blocking_findings"]},
        )

    def test_wrong_palette_variable_is_blocking(self) -> None:
        result = validate_artifact_palette(
            _artifact_html(
                self.required,
                variable_overrides={"--poster-primary": "#FF00AA"},
            ),
            "",
            self.required,
            "landing",
        )

        self.assertIn(
            "landing_required_palette_css_variable_mismatch",
            {item["issue_id"] for item in result["blocking_findings"]},
        )

    def test_missing_palette_variable_is_blocking(self) -> None:
        result = validate_artifact_palette(
            _artifact_html(self.required, omit_variable="--poster-accent"),
            "",
            self.required,
            "slides",
        )

        self.assertIn(
            "slides_required_palette_css_variable_mismatch",
            {item["issue_id"] for item in result["blocking_findings"]},
        )

    def test_foreign_shell_color_is_blocking(self) -> None:
        result = validate_artifact_palette(
            _artifact_html(self.required, extra_css=".rogue{background:#FF00AA}"),
            "",
            self.required,
            "landing",
        )

        finding = next(
            item
            for item in result["blocking_findings"]
            if item["issue_id"] == "landing_required_palette_foreign_shell_color"
        )
        self.assertEqual(finding["shell_extra_colors"], ["#FF00AA"])

    def test_source_visual_extra_color_is_debug_only(self) -> None:
        result = validate_artifact_palette(
            _artifact_html(
                self.required,
                extra_css="[data-source-id='figure:1'] path{fill:#FF00AA}",
                extra_body=(
                    "<svg data-source-id='figure:1' data-block-kind='chart'>"
                    "<path></path></svg>"
                ),
            ),
            "",
            self.required,
            "slides",
        )

        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["blocking_findings"], [])
        self.assertEqual(
            result["debug_metrics"]["source_visual_extra_colors"],
            ["#FF00AA"],
        )

    def test_authored_figcaption_color_cannot_hide_inside_source_figure(self) -> None:
        result = validate_artifact_palette(
            _artifact_html(
                self.required,
                extra_css="figure[data-source-id='figure:1'] figcaption{color:#FF00AA}",
                extra_body=(
                    "<figure data-source-id='figure:1'><img src='data:image/png;base64,'>"
                    "<figcaption>Authored interpretation</figcaption></figure>"
                ),
            ),
            "",
            self.required,
            "landing",
        )

        self.assertFalse(result["accepted"])
        self.assertIn(
            "landing_required_palette_foreign_shell_color",
            {item["issue_id"] for item in result["blocking_findings"]},
        )


if __name__ == "__main__":
    unittest.main()
