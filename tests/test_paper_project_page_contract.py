from __future__ import annotations

import unittest

from autodesign.schema import DesignSpec, HtmlBlock
from autodesign.tools.propose_design_spec import propose_design_spec as _propose_design_spec
from autodesign.util.html_artifact import _audit_landing_artifact
from autodesign.util.paper_project_page import _split_monolithic_sections

del _propose_design_spec


def _paper_artifact(*, with_identity: bool = True) -> dict:
    first_blocks = []
    if with_identity:
        first_blocks = [
            {"block_id": "paper_title", "kind": "text", "role": "title", "text": "A Research Paper"},
            {"block_id": "paper_authors", "kind": "text", "role": "authors", "text": "A. Author"},
        ]
    return {
        "theme": {"page_subtype": "paper_project_page"},
        "frames": [
            {
                "frame_id": "identity",
                "kind": "section",
                "role": "paper_identity",
                "blocks": first_blocks,
            },
            {
                "frame_id": "method",
                "kind": "section",
                "role": "method",
                "blocks": [
                    {"block_id": "method_text", "kind": "text", "text": "Method overview"},
                    {"block_id": "source_figure", "kind": "image", "source_id": "ingest_fig_01"},
                ],
            },
            {
                "frame_id": "resources_and_citation",
                "kind": "section",
                "role": "resources citation",
                "blocks": [
                    {"block_id": "paper_link", "kind": "embed", "href": "https://example.test/paper"},
                    {"block_id": "code_link", "kind": "embed", "href": "https://example.test/code"},
                    {"block_id": "cite", "kind": "text", "text": "Citation"},
                ],
            },
        ],
    }


class PaperProjectPageContractTest(unittest.TestCase):
    def test_paper_page_does_not_inherit_product_cta_or_footer_requirements(self) -> None:
        findings = _audit_landing_artifact(_paper_artifact())
        finding_ids = {str(item.get("id")) for item in findings}

        self.assertNotIn("landing_missing_hero", finding_ids)
        self.assertNotIn("landing_missing_cta", finding_ids)
        self.assertNotIn("landing_missing_footer", finding_ids)

    def test_paper_page_requires_identity_content_in_the_first_section(self) -> None:
        findings = _audit_landing_artifact(_paper_artifact(with_identity=False))
        finding_ids = {str(item.get("id")) for item in findings}

        self.assertIn("paper_project_missing_identity_hero", finding_ids)

    def test_semantic_groups_are_promoted_to_independent_sections(self) -> None:
        spec = _landing_spec_with_groups((
            ("hero", "hero"),
            ("abstract", "abstract"),
            ("method", "method"),
            ("results", "results"),
        ))

        self.assertTrue(_split_monolithic_sections(spec))
        self.assertEqual(
            [node.layer_id for node in spec.layer_graph],
            ["hero", "abstract", "method", "results"],
        )

    def test_layout_only_hero_groups_stay_in_one_section(self) -> None:
        spec = _landing_spec_with_groups((
            ("hero_copy", "copy"),
            ("hero_visual", "visual"),
        ))

        self.assertFalse(_split_monolithic_sections(spec))
        self.assertEqual(len(spec.layer_graph), 1)

    def test_semantic_groups_inside_page_root_are_promoted(self) -> None:
        spec = _landing_spec_with_groups((
            ("hero", "hero"),
            ("method", "method"),
            ("results", "benchmarks"),
        ))
        frame = spec.html_artifact.frames[0]
        frame.blocks = [HtmlBlock.model_validate({
            "block_id": "page_root",
            "kind": "group",
            "role": "page",
            "children": [block.model_dump(mode="json") for block in frame.blocks],
        })]

        self.assertTrue(_split_monolithic_sections(spec))
        self.assertEqual([node.layer_id for node in spec.layer_graph], ["hero", "method", "results"])

    def test_empty_semantic_groups_are_not_promoted(self) -> None:
        spec = _landing_spec_with_groups((
            ("hero", "hero"),
            ("method", "method"),
            ("results", "results"),
        ))
        for block in spec.html_artifact.frames[0].blocks:
            block.children = []

        self.assertFalse(_split_monolithic_sections(spec))
        self.assertEqual(len(spec.layer_graph), 1)

    def test_generic_landing_semantic_groups_are_not_promoted_without_paper_context(self) -> None:
        spec = _landing_spec_with_groups((
            ("hero", "hero"),
            ("approach", "approach"),
            ("results", "results"),
        ))
        spec.html_artifact.theme = {}
        spec.brief = "Create a product website."

        self.assertFalse(_split_monolithic_sections(spec))

    def test_authored_or_planned_frame_is_not_promoted(self) -> None:
        spec = _landing_spec_with_groups((
            ("hero", "hero"),
            ("method", "method"),
            ("results", "results"),
        ))
        frame = spec.html_artifact.frames[0]
        frame.layout_plan = {
            "archetype": "paper_story",
            "slots": [],
        }

        self.assertFalse(_split_monolithic_sections(spec))
        self.assertIsNotNone(spec.html_artifact.frames[0].layout_plan)

    def test_navigation_prefix_attaches_to_first_promoted_section(self) -> None:
        spec = _landing_spec_with_groups((
            ("hero", "hero"),
            ("method", "method"),
            ("results", "results"),
        ))
        frame = spec.html_artifact.frames[0]
        frame.blocks.insert(0, HtmlBlock.model_validate({
            "block_id": "page_nav",
            "kind": "text",
            "role": "navigation",
            "text": "Overview Method Results",
        }))

        self.assertTrue(_split_monolithic_sections(spec))
        self.assertEqual(len(spec.html_artifact.frames), 3)
        self.assertEqual(
            [block.block_id for block in spec.html_artifact.frames[0].blocks[:2]],
            ["page_nav", "hero"],
        )

    def test_empty_shell_before_semantic_frame_is_removed(self) -> None:
        spec = _landing_spec_with_groups((
            ("hero", "hero"),
            ("method", "method"),
            ("results", "results"),
        ))
        empty = spec.html_artifact.frames[0].model_copy(
            update={"frame_id": "empty_shell", "blocks": []},
            deep=True,
        )
        spec.html_artifact.frames.insert(0, empty)

        self.assertTrue(_split_monolithic_sections(spec))
        self.assertEqual(
            [frame.frame_id for frame in spec.html_artifact.frames],
            ["hero", "method", "results"],
        )

    def test_standalone_navigation_frame_merges_into_first_substantive_frame(self) -> None:
        spec = _landing_spec_with_groups((
            ("hero", "hero"),
            ("method", "method"),
            ("results", "results"),
        ))
        frame = spec.html_artifact.frames[0]
        frame.frame_id = "hero"
        frame.role = "hero"
        nav = frame.model_copy(update={
            "frame_id": "paper_project_page",
            "role": "paper_project_page",
            "title": "paper_project_page",
            "blocks": [HtmlBlock.model_validate({
                "block_id": "site_brand",
                "kind": "text",
                "role": "brand",
                "text": "Paper title",
            }), HtmlBlock.model_validate({
                "block_id": "method_nav",
                "kind": "text",
                "role": "nav_link",
                "text": "Method",
            })],
        }, deep=True)
        spec.html_artifact.frames.insert(0, nav)

        self.assertTrue(_split_monolithic_sections(spec))
        self.assertNotIn("paper_project_page", [item.frame_id for item in spec.html_artifact.frames])
        self.assertEqual(
            [block.block_id for block in spec.html_artifact.frames[0].blocks[:2]],
            ["site_brand", "method_nav"],
        )

    def test_navigation_frame_with_authored_metadata_is_preserved(self) -> None:
        spec = _landing_spec_with_groups((("hero", "hero"),))
        nav = spec.html_artifact.frames[0].model_copy(update={
            "frame_id": "authored_nav",
            "role": "navigation",
            "render_mode": "authored_html",
            "authored_body_html": "<nav>Paper navigation</nav>",
            "authored_css": "nav { display: flex; }",
            "layout_plan": {"archetype": "navigation", "slots": []},
            "blocks": [HtmlBlock.model_validate({
                "block_id": "nav_link",
                "kind": "text",
                "role": "nav_link",
                "text": "Method",
            })],
        }, deep=True)
        spec.html_artifact.frames.insert(0, nav)

        _split_monolithic_sections(spec)

        authored = next(frame for frame in spec.html_artifact.frames if frame.frame_id == "authored_nav")
        self.assertEqual(authored.authored_body_html, "<nav>Paper navigation</nav>")
        self.assertIsNotNone(authored.layout_plan)

    def test_zero_block_frame_with_authored_css_or_layout_plan_is_preserved(self) -> None:
        for update in (
            {"authored_css": ".shell { display: grid; }"},
            {"layout_plan": {"archetype": "shell", "slots": []}},
        ):
            with self.subTest(update=update):
                spec = _landing_spec_with_groups((("hero", "hero"),))
                shell = spec.html_artifact.frames[0].model_copy(update={
                    "frame_id": "paper_project_page",
                    "role": "paper_project_page",
                    "title": "paper_project_page",
                    "blocks": [],
                    **update,
                }, deep=True)
                spec.html_artifact.frames.insert(0, shell)

                _split_monolithic_sections(spec)

                self.assertIn("paper_project_page", [frame.frame_id for frame in spec.html_artifact.frames])

    def test_pdf_context_does_not_normalize_generic_product_website(self) -> None:
        spec = _landing_spec_with_groups((("hero", "hero"),))
        spec.brief = "Create a product website from the attached product PDF."
        spec.html_artifact.theme = {}
        nav = spec.html_artifact.frames[0].model_copy(update={
            "frame_id": "site_nav",
            "role": "navigation",
            "blocks": [HtmlBlock.model_validate({
                "block_id": "product_nav",
                "kind": "text",
                "role": "nav_link",
                "text": "Features",
            })],
        }, deep=True)
        spec.html_artifact.frames.insert(0, nav)

        self.assertFalse(_split_monolithic_sections(spec, paper_context_confirmed=True))
        self.assertEqual(spec.html_artifact.frames[0].frame_id, "site_nav")

    def test_navigation_skips_decorative_frame_and_merges_into_hero(self) -> None:
        spec = _landing_spec_with_groups((("hero", "hero"),))
        hero = spec.html_artifact.frames[0]
        hero.frame_id = "hero"
        hero.role = "hero"
        decorative = hero.model_copy(update={
            "frame_id": "background_band",
            "role": "decoration",
            "blocks": [HtmlBlock.model_validate({
                "block_id": "background_shape",
                "kind": "shape",
                "role": "background",
            })],
        }, deep=True)
        nav = hero.model_copy(update={
            "frame_id": "site_nav",
            "role": "navigation",
            "blocks": [HtmlBlock.model_validate({
                "block_id": "paper_nav",
                "kind": "text",
                "role": "nav_link",
                "text": "Method",
            })],
        }, deep=True)
        spec.html_artifact.frames = [nav, decorative, hero]

        self.assertTrue(_split_monolithic_sections(spec))
        decorative_after = next(frame for frame in spec.html_artifact.frames if frame.frame_id == "background_band")
        hero_after = next(frame for frame in spec.html_artifact.frames if frame.frame_id == "hero")
        self.assertNotIn("paper_nav", [block.block_id for block in decorative_after.blocks])
        self.assertEqual(hero_after.blocks[0].block_id, "paper_nav")

    def test_navigation_remains_separate_without_substantive_target(self) -> None:
        spec = _landing_spec_with_groups((("hero", "hero"),))
        base = spec.html_artifact.frames[0]
        nav = base.model_copy(update={
            "frame_id": "site_nav",
            "role": "navigation",
            "blocks": [HtmlBlock.model_validate({
                "block_id": "paper_nav",
                "kind": "text",
                "role": "nav_link",
                "text": "Method",
            })],
        }, deep=True)
        decorative = base.model_copy(update={
            "frame_id": "background_band",
            "role": "decoration",
            "blocks": [HtmlBlock.model_validate({
                "block_id": "background_shape",
                "kind": "shape",
                "role": "background",
            })],
        }, deep=True)
        spec.html_artifact.frames = [nav, decorative]

        _split_monolithic_sections(spec)

        self.assertEqual([frame.frame_id for frame in spec.html_artifact.frames], ["site_nav", "background_band"])

    def test_legacy_paper_landing_skips_shell_normalization(self) -> None:
        spec = _landing_spec_with_groups((("hero", "hero"),))
        spec.html_artifact.theme["_autodesign_legacy_source"] = "deck_html"
        shell = spec.html_artifact.frames[0].model_copy(update={
            "frame_id": "paper_project_page",
            "role": "paper_project_page",
            "title": "paper_project_page",
            "blocks": [],
        }, deep=True)
        spec.html_artifact.frames.insert(0, shell)

        self.assertFalse(_split_monolithic_sections(spec, paper_context_confirmed=True))
        self.assertEqual(spec.html_artifact.frames[0].frame_id, "paper_project_page")

    def test_titled_zero_block_placeholder_shell_is_removed_without_group_promotion(self) -> None:
        spec = _landing_spec_with_groups((("hero", "hero"),))
        frame = spec.html_artifact.frames[0]
        frame.frame_id = "abstract_framework"
        frame.role = "abstract"
        shell = frame.model_copy(update={
            "frame_id": "paper_project_page",
            "role": "paper_project_page",
            "title": "paper_project_page",
            "blocks": [],
        }, deep=True)
        spec.html_artifact.frames.insert(0, shell)

        self.assertTrue(_split_monolithic_sections(spec))
        self.assertEqual(
            [item.frame_id for item in spec.html_artifact.frames],
            ["abstract_framework"],
        )

    def test_generic_landing_is_not_split_by_legacy_heading_path(self) -> None:
        spec = _generic_landing_with_legacy_headings()

        self.assertFalse(_split_monolithic_sections(spec, paper_context_confirmed=False))
        self.assertEqual(len(spec.layer_graph), 1)


def _landing_spec_with_groups(groups: tuple[tuple[str, str], ...]) -> DesignSpec:
    return DesignSpec.model_validate({
        "brief": "Create a paper project page.",
        "artifact_type": "landing",
        "canvas": {"w_px": 1440, "h_px": 900},
        "layer_graph": [{
            "layer_id": "page",
            "name": "page",
            "kind": "section",
            "z_index": 0,
            "children": [],
        }],
        "html_artifact": {
            "target": "landing",
            "theme": {"page_subtype": "paper_project_page"},
            "frames": [{
                "frame_id": "page",
                "kind": "section",
                "role": "page_root",
                "blocks": [{
                    "block_id": block_id,
                    "kind": "group",
                    "role": role,
                    "children": [{
                        "block_id": f"{block_id}_text",
                        "kind": "text",
                        "text": block_id.title(),
                    }],
                } for block_id, role in groups],
            }],
        },
    })


def _generic_landing_with_legacy_headings() -> DesignSpec:
    return DesignSpec.model_validate({
        "brief": "Create a product website.",
        "artifact_type": "landing",
        "canvas": {"w_px": 1440, "h_px": 900},
        "layer_graph": [{
            "layer_id": "page",
            "name": "page",
            "kind": "section",
            "z_index": 0,
            "children": [
                {
                    "layer_id": "hero_heading",
                    "name": "hero heading",
                    "kind": "text",
                    "text": "A better product",
                    "role": "content",
                    "z_index": 0,
                },
                {
                    "layer_id": "approach_heading",
                    "name": "approach heading",
                    "kind": "text",
                    "text": "How it works",
                    "role": "content",
                    "z_index": 1,
                },
            ],
        }],
        "html_artifact": {
            "target": "landing",
            "theme": {},
            "frames": [],
        },
    })


if __name__ == "__main__":
    unittest.main()
