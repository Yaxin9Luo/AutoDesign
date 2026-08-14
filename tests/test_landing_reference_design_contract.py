from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
import re
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from autodesign.agents.critic_agent import _index_renders
from autodesign.schema import (
    ArtifactType,
    DesignSpec,
    DesignSystem,
    HtmlArtifactSpec,
    LayerNode,
)
from autodesign.skills.registry import SkillRegistry
from autodesign.tools._contract import ToolContext
from autodesign.tools.composite import _composite_landing
from autodesign.tools.critique_tool import _collect_slide_renders
from autodesign.tools.html_renderer import (
    _landing_numeric_value,
    _landing_sortable_columns,
    write_landing_html,
)
from autodesign.util.paper_project_page import _build_paper_project_panel_plan


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "skills" / "landing" / "visual_recipe"


def _text(layer_id: str, name: str, value: str, z_index: int) -> LayerNode:
    return LayerNode(
        layer_id=layer_id,
        name=name,
        kind="text",
        z_index=z_index,
        text=value,
    )


def _section(layer_id: str, children: list[LayerNode], z_index: int) -> LayerNode:
    return LayerNode(
        layer_id=layer_id,
        name=layer_id,
        kind="section",
        z_index=z_index,
        children=children,
    )


class LandingReferenceDesignContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        settings = SimpleNamespace(
            default_text_font="system-ui",
            fonts={},
            fonts_dir=self.root / "fonts",
            repo_root=REPO_ROOT,
        )
        self.ctx = ToolContext(
            settings=settings,
            run_dir=self.root,
            layers_dir=self.root / "layers",
            run_id="landing-contract-test",
        )
        self.figure_path = self.root / "reference-figure.png"
        Image.new("RGB", (320, 180), (52, 96, 146)).save(self.figure_path)
        self.rendered = {
            "ingest_fig_method": {
                "kind": "image",
                "src_path": str(self.figure_path),
                "caption_short": "Method overview from the paper.",
                "visual_role": "method",
                "source_page": 2,
                "output_sha256": "method-sha",
            },
            "ingest_fig_result": {
                "kind": "image",
                "src_path": str(self.figure_path),
                "caption_short": "Result comparison from the paper.",
                "visual_role": "main_evidence",
                "source_page": 4,
                "output_sha256": "result-sha",
            },
            "ingest_table_results": {
                "kind": "table",
                "headers": ["Method", "Accuracy", "Latency"],
                "rows": [
                    ["Baseline", "81.2%", "42 ms"],
                    ["Ours", "87.5%", "31 ms"],
                ],
                "caption": "Reported paper results.",
                "source_page": 5,
                "output_sha256": "table-sha",
            },
        }
        self.ctx.state["rendered_layers"] = self.rendered
        self.ctx.state["paper_visual_provenance"] = {
            "assets": [
                {"asset_id": "ingest_fig_method", "kind": "image", "output_sha256": "method-sha"},
                {"asset_id": "ingest_fig_result", "kind": "image", "output_sha256": "result-sha"},
                {"asset_id": "ingest_table_results", "kind": "table", "output_sha256": "table-sha"},
            ]
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _paper_spec(self) -> DesignSpec:
        return DesignSpec(
            brief="Create a complete paper project page.",
            artifact_type=ArtifactType.LANDING,
            canvas={"w_px": 1200, "h_px": 900},
            html_artifact=HtmlArtifactSpec(
                target="landing",
                theme={"page_subtype": "paper_project_page"},
            ),
            layer_graph=[
                _section("hero", [
                    _text("title", "title", "Paper Project", 1),
                    _text("summary", "summary", "A source-backed research thesis.", 2),
                ], 1),
                _section("framework", [
                    _text("method_heading", "section_heading", "Method", 1),
                    LayerNode(
                        layer_id="ingest_fig_method",
                        name="framework_visual",
                        kind="image",
                        z_index=2,
                        src_path=str(self.figure_path),
                    ),
                    LayerNode(
                        layer_id="ingest_fig_result",
                        name="result_visual",
                        kind="image",
                        z_index=3,
                        src_path=str(self.figure_path),
                    ),
                ], 2),
                _section("benchmarks", [
                    _text("results_heading", "section_heading", "Results", 1),
                    LayerNode(
                        layer_id="ingest_table_results",
                        name="benchmark_table",
                        kind="table",
                        z_index=2,
                        headers=["Method", "Accuracy", "Latency"],
                        rows=[
                            ["Baseline", "81.2%", "42 ms"],
                            ["Ours", "87.5%", "31 ms"],
                        ],
                        caption="Reported paper results.",
                    ),
                ], 3),
                _section("citation_footer", [
                    _text("citation_heading", "section_heading", "Citation", 1),
                    _text("citation", "body", "Paper citation.", 2),
                ], 4),
            ],
        )

    def _render(self, spec: DesignSpec, name: str) -> str:
        self.ctx.state["design_spec"] = spec
        if (spec.html_artifact and
                spec.html_artifact.theme.get("page_subtype") == "paper_project_page"):
            self.ctx.state["paper_project_panel_plan"] = _build_paper_project_panel_plan(
                spec, self.ctx, self.rendered
            )
        else:
            self.ctx.state.pop("paper_project_panel_plan", None)
        path = self.root / name
        write_landing_html(spec, path, self.ctx)
        return path.read_text(encoding="utf-8")

    def test_versioned_taste_resource_is_available_in_an_exposed_runtime_stage(self) -> None:
        manifest = json.loads((SKILL_ROOT / "skill.json").read_text(encoding="utf-8"))
        resource = next(
            item for item in manifest["resources"]
            if item["id"] == "paper_project_page_taste_v1"
        )
        resource_path = SKILL_ROOT / resource["path"]
        self.assertTrue(resource_path.is_file())
        taste = resource_path.read_text(encoding="utf-8")
        self.assertIn("Taste contract version: 1.0.0", taste)
        self.assertIn("source-grounded interaction", taste)
        self.assertIn("generic product marketing", taste)
        self.assertNotRegex(taste, r"<img|https?://")
        self.assertIn("Do not copy", taste)

        registry = SkillRegistry.load(REPO_ROOT / "skills")
        pack = registry.get("landing.visual_recipe")
        self.assertIsNotNone(pack)
        assert pack is not None
        self.assertNotIn("paper_project_page_taste_v1", pack.render("enhance"))
        self.assertIn("Read `paper_project_page_taste_v1`", pack.render("plan"))
        self.assertIsNone(pack.read_resource("paper_project_page_taste_v1", "enhance"))
        self.assertIsNotNone(pack.read_resource("paper_project_page_taste_v1", "plan"))
        self.assertIsNotNone(pack.read_resource("paper_project_page_taste_v1", "repair"))

    def test_panel_plan_selects_interactions_from_source_affordances(self) -> None:
        plan = _build_paper_project_panel_plan(
            self._paper_spec(), self.ctx, self.rendered
        )
        interaction = plan["interaction_contract"]
        self.assertEqual(interaction["version"], 1)
        self.assertIn("source_figure_focus_viewer", interaction["selected"])
        self.assertIn("sortable_result_table", interaction["selected"])
        self.assertTrue(interaction["source_grounded_required"])
        self.assertEqual(
            interaction["eligible_source_ids"],
            {
                "source_figure_focus_viewer": ["ingest_fig_method", "ingest_fig_result"],
                "sortable_result_table": ["ingest_table_results"],
            },
        )

    def test_authored_and_unresolved_sources_do_not_enable_interactions(self) -> None:
        spec = self._paper_spec()
        evidence = spec.layer_graph[1]
        evidence.children = [
            LayerNode(
                layer_id="planner_authored_figure",
                name="authored_figure",
                kind="image",
                z_index=1,
                src_path=str(self.figure_path),
            ),
            LayerNode(
                layer_id="ingest_fig_unresolved",
                name="unresolved_figure",
                kind="image",
                z_index=2,
                src_path=str(self.figure_path),
            ),
            LayerNode(
                layer_id="planner_authored_metrics",
                name="authored_table",
                kind="table",
                z_index=3,
                headers=["Method", "Score"],
                rows=[["A", "10%"], ["B", "20%"]],
            ),
            LayerNode(
                layer_id="ingest_table_unresolved",
                name="unresolved_table",
                kind="table",
                z_index=4,
                headers=["Method", "Score"],
                rows=[["A", "10%"], ["B", "20%"]],
            ),
        ]
        spec.layer_graph[2].children = []

        plan = _build_paper_project_panel_plan(spec, self.ctx, self.rendered)
        interaction = plan["interaction_contract"]
        self.assertEqual(interaction["selected"], [])
        self.assertEqual(
            interaction["eligible_source_ids"],
            {"source_figure_focus_viewer": [], "sortable_result_table": []},
        )

        output = self._render(spec, "unverified-sources.html")
        self.assertNotIn('class="ld-figure-focus-trigger"', output)
        self.assertNotIn('class="ld-sort-button"', output)

    def test_table_identity_does_not_verify_planner_authored_values(self) -> None:
        spec = self._paper_spec()
        table = spec.layer_graph[2].children[1]
        table.rows = [["Baseline", "99.9%", "1 ms"], ["Ours", "1.0%", "9 s"]]

        plan = _build_paper_project_panel_plan(spec, self.ctx, self.rendered)
        self.assertNotIn("sortable_result_table", plan["interaction_contract"]["selected"])
        output = self._render(spec, "mismatched-source-table.html")
        self.assertNotIn('class="ld-sort-button"', output)

    def test_unit_aware_numeric_columns_normalize_compatible_dimensions(self) -> None:
        rows = [
            ["900 ms", "900M", "900 MB", "42%", "30 fps", "1.5x"],
            ["1 s", "1.2B", "1 GB", "87%", "60 fps", "2x"],
        ]
        self.assertEqual(
            _landing_sortable_columns(rows, len(rows[0])),
            {0: "numeric", 1: "numeric", 2: "numeric", 3: "numeric", 4: "numeric", 5: "numeric"},
        )
        self.assertTrue(math.isclose(_landing_numeric_value("900 ms"), 0.9))
        self.assertTrue(math.isclose(_landing_numeric_value("1 s"), 1.0))
        self.assertEqual(_landing_numeric_value("900M"), 900_000_000)
        self.assertEqual(_landing_numeric_value("1.2B"), 1_200_000_000)
        self.assertEqual(_landing_numeric_value("900 MB"), 900_000_000)
        self.assertEqual(_landing_numeric_value("1 GB"), 1_000_000_000)

    def test_incompatible_numeric_dimensions_use_text_sorting(self) -> None:
        rows = [["1 s", "42%"], ["900M", "60 fps"]]
        self.assertEqual(
            _landing_sortable_columns(rows, 2),
            {0: "text", 1: "text"},
        )

    def test_paper_renderer_emits_accessible_local_interaction_hooks(self) -> None:
        output = self._render(self._paper_spec(), "paper.html")

        for marker in (
            'class="ld-figure-focus-trigger"',
            'role="dialog"',
            'aria-modal="true"',
            'data-figure-viewer-close',
            'data-figure-viewer-prev',
            'data-figure-viewer-next',
            'data-sort-direction',
            'aria-sort="none"',
            'class="ld-reading-progress"',
            'aria-current',
            "prefers-reduced-motion: reduce",
            "event.key === 'Escape'",
            "event.key === 'ArrowLeft'",
            "event.key === 'ArrowRight'",
        ):
            self.assertIn(marker, output)

        self.assertNotRegex(output, r"<script[^>]+src=")
        self.assertNotRegex(output, r"<img[^>]+src=[\"']https?://")
        self.assertNotRegex(output, r"url\([\"']?https?://")

    def test_paper_nav_honors_explicit_values_and_uses_auto_only_when_unset(self) -> None:
        expected = {False: False, True: True, None: True}
        for show_nav, should_render in expected.items():
            with self.subTest(show_nav=show_nav):
                spec = self._paper_spec()
                spec.layer_graph = spec.layer_graph[:2]
                spec.design_system = DesignSystem(show_nav=show_nav)
                output = self._render(spec, f"nav-{show_nav}.html")
                self.assertEqual('class="ld-header"' in output, should_render)

    def test_declared_art_directions_have_readable_computed_nav_contrast(self) -> None:
        from playwright.sync_api import sync_playwright
        from autodesign.util.browser_render import _launch_chromium

        profiles = (
            "light_academic_project",
            "demo_first_gallery",
            "benchmark_dashboard",
            "dark_editorial_research",
            "systems_model_card",
        )
        with sync_playwright() as playwright:
            browser = _launch_chromium(playwright)
            page = browser.new_page(viewport={"width": 1200, "height": 900})
            try:
                for profile in profiles:
                    with self.subTest(profile=profile):
                        spec = self._paper_spec()
                        spec.html_artifact.theme["art_direction"] = profile
                        html_path = self.root / f"contrast-{profile}.html"
                        self._render(spec, html_path.name)
                        page.goto(html_path.as_uri(), wait_until="load")
                        colors = page.locator(".ld-nav a").first.evaluate(
                            """element => ({
                              foreground: getComputedStyle(element).color,
                              background: getComputedStyle(element.closest('.ld-header')).backgroundColor
                            })"""
                        )
                        self.assertGreaterEqual(
                            _contrast_ratio(colors["foreground"], colors["background"]),
                            4.5,
                        )
            finally:
                browser.close()

    def test_composite_and_critic_use_desktop_landing_render_only(self) -> None:
        spec = self._paper_spec()
        self.ctx.state["design_spec"] = spec

        def fake_screenshot(_html_path, out_path, **kwargs):
            out_path.write_bytes(b"test-png")
            return SimpleNamespace(warnings=[], backend="playwright")

        with (
            patch("autodesign.tools.composite.screenshot_html", side_effect=fake_screenshot) as screenshot,
            patch("autodesign.tools.composite._lint_composite_html", return_value={"quality_lint_p0_count": 0}),
            patch("autodesign.tools.composite._persist_layout_grounding", return_value=(None, {})),
            patch("autodesign.tools.composite.ground_html_layout", return_value=SimpleNamespace()),
            patch("autodesign.tools.composite._attach_html_artifact_contract", side_effect=lambda payload, **_: payload),
            patch("autodesign.tools.composite._attach_design_feedback", side_effect=lambda payload, **_: payload),
            patch("autodesign.tools.composite._refresh_final_links"),
            patch("autodesign.tools.composite._mark_visual_reference_revision_composited"),
        ):
            result = _composite_landing(spec, self.ctx)

        self.assertEqual(result.status, "ok")
        calls = screenshot.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[1].name, "preview.png")
        self.assertEqual(calls[0].kwargs, {"viewport_width": 1200, "viewport_height": 900, "full_page": True})
        self.assertNotIn("mobile_preview_relative_path", result.payload)
        self.assertNotIn("mobile_viewport_px", result.payload)

        composition = self.ctx.state["composition"]
        desktop = Path(composition.preview_path)
        renders = _collect_slide_renders(spec, composition, desktop)
        self.assertEqual([path.name for path in renders], ["preview.png"])
        self.assertEqual(
            _index_renders(renders, spec),
            {"landing_full": desktop},
        )

    def test_paper_css_is_academic_light_modular_and_responsive(self) -> None:
        output = self._render(self._paper_spec(), "paper-css.html")
        self.assertIn("--paper-surface: #ffffff", output)
        self.assertIn(".ld-figure-viewer", output)
        self.assertIn(".ld-sort-button", output)
        self.assertIn("@media (max-width: 760px)", output)
        self.assertNotIn("linear-gradient", output)
        self.assertNotIn("radial-gradient", output)

    def test_critic_contract_is_paper_specific_without_word_limits(self) -> None:
        critic = (REPO_ROOT / "prompts" / "critic_vision_landing.md").read_text(
            encoding="utf-8"
        )
        for requirement in (
            "complete research narrative",
            "source-grounded interaction",
            "reduced motion",
            "generic marketing CTA",
            "card wall",
        ):
            self.assertIn(requirement, critic)
        self.assertNotIn("mobile", critic.lower())
        self.assertNotRegex(critic, r"(?:<=|≤)\s*\d+\s*words")
        self.assertNotIn("72-140", critic.replace("–", "-"))

    def test_non_paper_landing_keeps_generic_behavior_without_paper_hooks(self) -> None:
        spec = self._paper_spec().model_copy(deep=True)
        spec.brief = "Create a product landing page."
        spec.html_artifact.theme = {}
        output = self._render(spec, "generic.html")

        self.assertIn('data-page-subtype=""', output)
        self.assertIn("data-reveal", output)
        self.assertIn("IntersectionObserver", output)
        for paper_only in (
            "ld-figure-viewer",
            "ld-figure-focus-trigger",
            "ld-reading-progress",
            "ld-sort-button",
        ):
            self.assertNotIn(paper_only, output)

def _contrast_ratio(foreground: str, background: str) -> float:
    def rgb(value: str) -> tuple[float, float, float]:
        channels = [float(item) for item in re.findall(r"[\d.]+", value)[:3]]
        return tuple(channel / 255 for channel in channels)  # type: ignore[return-value]

    def luminance(value: str) -> float:
        linear = [
            channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
            for channel in rgb(value)
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


if __name__ == "__main__":
    unittest.main()
