from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from autodesign.runner import PipelineRunner, _resume_palette_id
from autodesign.tools.ingest_document import (
    _build_poster_content_brief,
    _normalize_reused_poster_content_brief,
)
from autodesign.util.academic_palette import require_academic_color_system
from autodesign.util.poster_plan_contract import build_poster_plan_contract


def _reference_contract(color_system: dict[str, object]) -> dict[str, object]:
    return {
        "version": 4,
        "transfer_mode": "reference_first_reconstruction",
        "style_reference_id": "burgundy_reference",
        "canvas_contract": {
            "w_px": 1440,
            "h_px": 960,
            "aspect_ratio": "3:2",
        },
        "summary": "Burgundy reference with #A4113F primary accent.",
        "color_system": color_system,
        "aesthetic_contract": {
            "reference_priority_policy": "Reconstruct the reference layout from a blank canvas.",
            "canvas_policy": "Use #FFFFFF with #24191D academic ink.",
            "palette_usage_policy": "Use only the mulberry_mint reference palette.",
            "header_surface_policy": "Keep the observed white top-rule header.",
            "lead_band_policy": "Keep the reference lead-band geometry and Burgundy fill.",
            "section_surface_policy": "Keep compact filled section headings.",
            "section_separation_policy": "KEEP SECTION SEPARATION POLICY",
            "body_region_structure_policy": "KEEP BODY REGION POLICY",
            "column_structure_policy": "KEEP COLUMN STRUCTURE POLICY",
            "chrome_policy": "KEEP CHROME POLICY",
            "table_surface_policy": "KEEP TABLE POLICY",
            "formula_surface_policy": "KEEP FORMULA POLICY",
            "source_wrapper_policy": "KEEP SOURCE WRAPPER POLICY",
            "color_dominance_policy": "Transfer the Burgundy color rhythm.",
        },
        "style_tokens": {
            "header_treatment": {"mode": "top_rule_white"},
            "section_heading_treatment": {"mode": "filled_band"},
            "body_region_structure": {
                "layout_mode": "equal_regions",
                "region_count": 3,
                "major_section_count": 6,
                "major_sections_per_region": [2, 2, 2],
                "regions": [
                    {
                        "region_id": f"region_{index}",
                        "region_role": "column",
                        "section_count": 2,
                        "reading_order": index,
                    }
                    for index in range(1, 4)
                ],
                "subsection_treatment": "inline_colored_label",
            },
            "column_structure": {
                "layout_mode": "equal_columns",
                "region_count": 3,
                "major_section_count": 6,
                "major_sections_per_column": [2, 2, 2],
                "region_roles": ["column", "column", "column"],
                "subsection_treatment": "inline_colored_label",
                "compatibility_only": True,
            },
            "table_treatment": {"observed": False, "rule_style": "none"},
            "formula_treatment": {"frame": "none"},
            "reference_palette_roles": {
                "ink": "#24191D",
                "primary": "#A4113F",
                "secondary": "#F0D6DF",
            },
        },
    }


def _author_context(
    run_dir: Path,
    *,
    selected: dict[str, object] | None = None,
    reference: dict[str, object] | None = None,
) -> SimpleNamespace:
    state: dict[str, object] = {
        "raw_user_brief": "Create an academic paper poster.",
        "canvas_plan": {
            "preset_id": "reference-poster" if reference else "cvpr-landscape",
            "canvas": {
                "w_px": 1440 if reference else 3072,
                "h_px": 960 if reference else 1536,
                "dpi": 96,
                "aspect_ratio": "3:2" if reference else "2:1",
            },
        },
        "poster_content_brief": {
            "title": "Paper",
            "sections": [],
        },
        "poster_plan_contract": {
            "required_source_visual_ids": [],
            "selected_visuals": [],
        },
        "paper_visual_storyboard": {},
    }
    if selected is not None:
        state["required_color_system"] = selected
    if reference is not None:
        state["reference_poster"] = {"source_sha256": "fixture"}
        state["reference_style_contract"] = reference
    return SimpleNamespace(run_dir=run_dir, state=state)


def _write_full_author_fixture(
    run_dir: Path,
    *,
    selected: dict[str, object] | None,
    adversarial_reference_attribute: bool = False,
    adversarial_escaped_selector: bool = False,
) -> SimpleNamespace:
    reference = require_academic_color_system("mulberry_mint")
    reference = {**reference, "palette_id": "reference_palette_leak"}
    contract = _reference_contract(reference)
    ctx = _author_context(run_dir, selected=selected, reference=contract)
    ctx.run_id = "run_palette_staging"
    ctx.settings = SimpleNamespace(skills_dir="")

    for name, payload in (
        ("poster_content_brief.json", ctx.state["poster_content_brief"]),
        ("poster_plan_contract.json", ctx.state["poster_plan_contract"]),
        ("paper_memory.json", {"metadata": {"title": "Paper"}}),
        ("reference_style_contract.json", contract),
    ):
        (run_dir / name).write_text(json.dumps(payload), encoding="utf-8")

    adversarial_attribute = ' data-reference-color="#A4113F"' if adversarial_reference_attribute else ""
    adversarial_css = (
        r'#\41 4113F { outline-color: #A4113F; }'
        if adversarial_escaped_selector
        else ""
    )
    blueprint = """<!doctype html>
<html><head><style>
:root { --background: #FFFFFF; --ink: #24191D; --primary: #A4113F; --secondary: #F0D6DF; }
.reference-style-blueprint { position: relative; width: 1440px; height: 960px; background: #FFFFFF; color: #24191D; }
[data-style-role="identity-header"] { position: absolute; left: 123px; top: 17px; width: 1194px; color: #FFFFFF; background: #A4113F; }
[data-style-role="section-heading"] { font-size: 42px; background: #F0D6DF; color: #24191D; border: 3px solid #A4113F; }
__ADVERSARIAL_CSS__
</style></head><body>
<div class="reference-style-blueprint" style="outline-color: #A4113F"__ADVERSARIAL_ATTRIBUTE__>
  <header data-style-role="identity-header"><h1>{{PAPER_TITLE}}</h1><p>{{AUTHORS}}</p><p>{{INSTITUTIONS}}</p></header>
  <main data-style-role="body-regions">
    <section data-style-role="body-region" data-region-id="region_1" data-region-role="column">
      <section data-style-role="section"><h2 data-style-role="section-heading">{{SECTION_TITLE}}</h2><p>{{TARGET_PAPER_CONTENT}}</p></section>
      <section data-style-role="section"><h2 data-style-role="section-heading">{{SECTION_TITLE}}</h2><p>{{TARGET_PAPER_FIGURE}}</p></section>
    </section>
    <section data-style-role="body-region" data-region-id="region_2" data-region-role="column">
      <section data-style-role="section"><h2 data-style-role="section-heading">{{SECTION_TITLE}}</h2><p>{{TARGET_PAPER_CONTENT}}</p></section>
      <section data-style-role="section"><h2 data-style-role="section-heading">{{SECTION_TITLE}}</h2><p>{{TARGET_PAPER_TABLE}}</p></section>
    </section>
    <section data-style-role="body-region" data-region-id="region_3" data-region-role="column">
      <section data-style-role="section"><h2 data-style-role="section-heading">{{SECTION_TITLE}}</h2><p>{{TARGET_PAPER_CONTENT}}</p></section>
      <section data-style-role="section"><h2 data-style-role="section-heading">{{SECTION_TITLE}}</h2><p>{{TARGET_PAPER_FIGURE}}</p></section>
    </section>
  </main>
</div></body></html>
"""
    (run_dir / "reference_style_blueprint.html").write_text(
        blueprint.replace("__ADVERSARIAL_ATTRIBUTE__", adversarial_attribute).replace(
            "__ADVERSARIAL_CSS__",
            adversarial_css,
        ),
        encoding="utf-8",
    )
    reference_dir = run_dir / "reference_poster"
    reference_dir.mkdir()
    preview = Image.new("RGB", (12, 8), "#FFFFFF")
    draw = ImageDraw.Draw(preview)
    draw.rectangle((0, 0, 11, 1), fill="#A4113F")
    draw.rectangle((0, 2, 3, 7), fill="#F0D6DF")
    draw.rectangle((4, 2, 7, 7), fill="#A4113F")
    draw.rectangle((8, 2, 11, 7), fill="#24191D")
    preview.save(reference_dir / "reference.png")
    layers_dir = run_dir / "layers"
    layers_dir.mkdir()
    Image.new("RGB", (6, 4), "#A4113F").save(layers_dir / "paper_figure.png")
    return ctx


class PosterPaletteRuntimeTest(unittest.TestCase):
    def test_structured_palette_overrides_prompt_during_ingest(self) -> None:
        required = require_academic_color_system("plum_sage")
        brief = _build_poster_content_brief(
            summaries=[{"type": "pdf", "manifest": {"title": "Paper"}}],
            rendered={},
            recommended_figures={},
            recommended_text_units={},
            visual_candidate_scores=[],
            canvas_plan={"w_px": 3072, "h_px": 1536},
            raw_brief="Use Cardinal Red",
            required_color_system=required,
        )
        self.assertEqual(brief["color_system"]["palette_id"], "plum_sage")
        self.assertEqual(brief["required_color_system"]["palette_id"], "plum_sage")

    def test_reused_ingest_preserves_structured_palette(self) -> None:
        required = require_academic_color_system("deep_cyan")
        ctx = SimpleNamespace(state={
            "raw_user_brief": "Use Burgundy",
            "required_color_system": required,
        })
        reused = _normalize_reused_poster_content_brief(
            {"kind": "paper_poster_content_brief", "title": "Paper", "sections": []},
            ctx,
            paper_memory={"metadata": {"title": "Paper"}},
            poster_plan_contract={},
        )
        self.assertEqual(reused["color_system"]["palette_id"], "deep_cyan")
        self.assertEqual(reused["required_color_system"]["palette_id"], "deep_cyan")

    def test_plan_contract_round_trips_required_palette(self) -> None:
        required = require_academic_color_system("oxide_red")
        contract = build_poster_plan_contract({
            "kind": "paper_poster_content_brief",
            "title": "Paper",
            "sections": [],
            "color_system": required,
            "required_color_system": required,
        })
        self.assertEqual(contract["required_color_system"]["palette_id"], "oxide_red")

    def test_runner_forwards_palette_without_changing_brief(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runner = PipelineRunner(SimpleNamespace(out_dir=Path(raw_tmp)))
            sentinel = object()
            with patch.object(runner, "_run_inner", return_value=sentinel) as inner:
                result = runner.run(
                    "Keep this brief unchanged",
                    run_id="run_palette_forward",
                    palette_id="plum_sage",
                )
            self.assertIs(result, sentinel)
            self.assertEqual(inner.call_args.args[0], "Keep this brief unchanged")
            self.assertEqual(inner.call_args.kwargs["palette_id"], "plum_sage")

    def test_resume_palette_comes_only_from_persisted_run(self) -> None:
        self.assertEqual(_resume_palette_id({
            "run_brief_json": {"palette_id": "deep_cyan"},
            "resume_state_json": {"palette_id": "oxide_red"},
        }), "deep_cyan")
        self.assertEqual(_resume_palette_id({
            "run_brief_json": {},
            "resume_state_json": {"palette_id": "oxide_red"},
        }), "oxide_red")
        self.assertIsNone(_resume_palette_id({
            "run_brief_json": {},
            "resume_state_json": {},
        }))

    def _render_author_inputs(
        self,
        ctx: SimpleNamespace,
        attempt_dir: Path,
    ) -> tuple[str, str, dict[str, object]]:
        from autodesign.agents.external_designer_author import (
            ExternalDesignerAuthor,
            _synchronize_staged_color_system,
            _write_author_quick_brief,
        )

        color_context = _synchronize_staged_color_system(
            ctx,
            attempt_dir,
            "Create an academic paper poster.",
        )
        self.assertTrue(_write_author_quick_brief(
            ctx,
            attempt_dir,
            "Create an academic paper poster.",
        ))
        quick_brief = (attempt_dir / "author_quick_brief.md").read_text(encoding="utf-8")
        prompt = ExternalDesignerAuthor(
            SimpleNamespace(designer_author_model=""),
            "",
        )._build_prompt(
            ctx,
            brief="Create an academic paper poster.",
            attempt_dir=attempt_dir,
            attempt_index=1,
            max_attempts=1,
        )
        return prompt, quick_brief, color_context

    def test_structured_palette_overrides_reference_palette_in_all_author_inputs(self) -> None:
        selected = require_academic_color_system("plum_sage")
        reference = require_academic_color_system("mulberry_mint")
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp) / "attempt_01"
            attempt_dir.mkdir()
            ctx = _author_context(
                Path(raw_tmp),
                selected=selected,
                reference=_reference_contract(reference),
            )
            prompt, quick_brief, color_context = self._render_author_inputs(ctx, attempt_dir)

        author_text = prompt + "\n" + quick_brief
        self.assertIn("plum_sage", author_text)
        for name, value in selected["css_variables"].items():
            self.assertIn(name, author_text)
            self.assertIn(value, author_text)
        self.assertIn(
            "The user-selected palette is authoritative for color. Apply the reference poster's layout, typography, spacing, section, table, formula, and surface treatment without copying its color system.",
            author_text,
        )
        for rendered in (prompt, quick_brief):
            self.assertIn("grayscale preview is neutralized guidance only", rendered)
            self.assertIn("geometry, hierarchy, spacing, and tonal relationships", rendered)
            self.assertIn("not final authored surface-color guidance", rendered)
        self.assertIn("\nRequired palette:\n", prompt)
        self.assertNotIn("\nPalette options and recommendation:\n", prompt)
        self.assertIn("top_rule_white", author_text)
        self.assertNotIn("mulberry_mint", author_text)
        for value in ("#24191D", "#A4113F", "#F0D6DF"):
            self.assertNotIn(value, author_text)
        self.assertEqual(
            [item["palette_id"] for item in color_context["color_system_options"]],
            ["plum_sage"],
        )
        projected_reference = color_context["reference_style_contract"]
        self.assertEqual(projected_reference["color_system"]["palette_id"], "plum_sage")
        self.assertNotIn("reference_palette_roles", projected_reference["style_tokens"])

    def test_structured_palette_without_reference_is_the_only_author_palette(self) -> None:
        selected = require_academic_color_system("plum_sage")
        alternative = require_academic_color_system("oxide_red")
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp) / "attempt_01"
            attempt_dir.mkdir()
            ctx = _author_context(Path(raw_tmp), selected=selected)
            ctx.state["poster_content_brief"]["color_system_options"] = [selected, alternative]
            prompt, quick_brief, color_context = self._render_author_inputs(ctx, attempt_dir)

        author_text = prompt + "\n" + quick_brief
        self.assertIn("plum_sage", author_text)
        for name, value in selected["css_variables"].items():
            self.assertIn(name, author_text)
            self.assertIn(value, author_text)
        self.assertNotIn("oxide_red", author_text)
        self.assertNotIn(alternative["css_variables"]["--poster-primary"], author_text)
        self.assertNotIn("Choose exactly one academic palette", author_text)
        self.assertNotIn("Recommended default", author_text)
        self.assertIn("- Required palette for this attempt:", author_text)
        self.assertNotIn("- Palette options for this attempt:", author_text)
        self.assertIn("\nRequired palette:\n", prompt)
        self.assertNotIn("\nPalette options and recommendation:\n", prompt)
        self.assertEqual(
            [item["palette_id"] for item in color_context["color_system_options"]],
            ["plum_sage"],
        )

    def test_reference_without_structured_selection_preserves_legacy_palette_behavior(self) -> None:
        reference = require_academic_color_system("mulberry_mint")
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp) / "attempt_01"
            attempt_dir.mkdir()
            ctx = _author_context(
                Path(raw_tmp),
                reference=_reference_contract(reference),
            )
            ctx.state["raw_user_brief"] = "Use Burgundy"
            prompt, quick_brief, color_context = self._render_author_inputs(ctx, attempt_dir)

        author_text = prompt + "\n" + quick_brief
        self.assertIn("The active reference palette is authoritative", author_text)
        self.assertIn("mulberry_mint", author_text)
        self.assertIn("#A4113F", author_text)
        self.assertNotIn("The user-selected palette is authoritative", author_text)
        self.assertIn("the image is the visual fidelity target", author_text)
        self.assertNotIn("grayscale preview is neutralized guidance only", author_text)
        self.assertIn("- Palette options for this attempt", author_text)
        self.assertNotIn("- Required palette for this attempt:", author_text)
        self.assertIn("\nPalette options and recommendation:\n", prompt)
        self.assertEqual(
            [item["palette_id"] for item in color_context["color_system_options"]],
            ["mulberry_mint"],
        )

    def test_full_staging_projects_every_selected_reference_author_input(self) -> None:
        from autodesign.agents.external_designer_author import ExternalDesignerAuthor

        selected = require_academic_color_system("plum_sage")
        repair_feedback = {
            "summary": {
                "issue_id": "paper_poster_html_reference_style_contract_failed",
                "issues": [{"issue_id": "reference_heading_treatment_mismatch"}],
            },
            "payload": {},
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            attempt_dir = run_dir / "designer_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            ctx = _write_full_author_fixture(run_dir, selected=selected)
            author = ExternalDesignerAuthor(
                SimpleNamespace(designer_author_model=""),
                "",
            )

            self.assertTrue(author._stage_inputs(
                ctx,
                brief="Create an academic paper poster.",
                attempt_dir=attempt_dir,
                repair_feedback=repair_feedback,
            ))

            blueprint = (attempt_dir / "reference_style_blueprint.html").read_text(encoding="utf-8")
            plan = json.loads((attempt_dir / "poster_plan_contract.json").read_text(encoding="utf-8"))
            manifest = json.loads((attempt_dir / "author_input_manifest.json").read_text(encoding="utf-8"))
            staged_reference = json.loads((attempt_dir / "reference_style_contract.json").read_text(encoding="utf-8"))
            repair_context = json.loads((attempt_dir / "repair_context.json").read_text(encoding="utf-8"))
            validation_feedback = (attempt_dir / "validation_feedback.json").read_text(encoding="utf-8")
            skeleton = plan["authored_html_skeleton"]
            author_text = "\n".join([
                blueprint,
                json.dumps(skeleton),
                json.dumps(manifest),
                json.dumps(staged_reference),
                json.dumps(repair_context),
                validation_feedback,
            ])

            self.assertIn("plum_sage", author_text)
            self.assertIn("var(--poster-primary)", blueprint)
            self.assertIn("outline-color: var(--poster-primary)", blueprint)
            for name, value in selected["css_variables"].items():
                self.assertIn(f"{name}: {value}", blueprint)
            self.assertIn("left: 123px", blueprint)
            self.assertIn("font-size: 42px", blueprint)
            self.assertIn("{{PAPER_TITLE}}", blueprint)
            self.assertIn("var(--poster-primary)", skeleton["css"])
            self.assertNotIn("reference_palette_leak", author_text)
            self.assertEqual(
                ctx.state["reference_style_contract"]["color_system"]["palette_id"],
                "reference_palette_leak",
            )
            for value in ("#24191D", "#A4113F", "#F0D6DF"):
                self.assertNotIn(value, author_text)

            projected_aesthetic = manifest["aesthetic_contract"]
            self.assertEqual(projected_aesthetic["body_region_structure_policy"], "KEEP BODY REGION POLICY")
            self.assertEqual(projected_aesthetic["column_structure_policy"], "KEEP COLUMN STRUCTURE POLICY")
            self.assertEqual(projected_aesthetic["formula_surface_policy"], "KEEP FORMULA POLICY")
            repair_aesthetic = repair_context["reference_style"]["contract"]["aesthetic_contract"]
            self.assertEqual(repair_aesthetic["body_region_structure_policy"], "KEEP BODY REGION POLICY")

            with Image.open(attempt_dir / "reference_poster" / "reference.png") as preview:
                self.assertEqual(preview.size, (12, 8))
                neutral = preview.convert("RGB")
                red, green, blue = neutral.getpixel((0, 0))
                self.assertEqual(red, green)
                self.assertEqual(green, blue)
                self.assertNotEqual(neutral.getpixel((1, 3)), neutral.getpixel((5, 3)))
                self.assertNotEqual(neutral.getpixel((5, 3)), neutral.getpixel((9, 3)))
            with Image.open(attempt_dir / "layers" / "paper_figure.png") as paper_figure:
                self.assertEqual(paper_figure.convert("RGB").getpixel((0, 0)), (164, 17, 63))

    def test_full_staging_preserves_reference_guidance_without_structured_selection(self) -> None:
        from autodesign.agents.external_designer_author import ExternalDesignerAuthor

        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            attempt_dir = run_dir / "designer_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            ctx = _write_full_author_fixture(run_dir, selected=None)
            author = ExternalDesignerAuthor(
                SimpleNamespace(designer_author_model=""),
                "",
            )

            self.assertTrue(author._stage_inputs(
                ctx,
                brief="Create an academic paper poster.",
                attempt_dir=attempt_dir,
            ))

            blueprint = (attempt_dir / "reference_style_blueprint.html").read_text(encoding="utf-8")
            plan = json.loads((attempt_dir / "poster_plan_contract.json").read_text(encoding="utf-8"))
            self.assertIn("#A4113F", blueprint)
            self.assertIn("#A4113F", plan["authored_html_skeleton"]["css"])
            with Image.open(attempt_dir / "reference_poster" / "reference.png") as preview:
                self.assertEqual(preview.convert("RGB").getpixel((0, 0)), (164, 17, 63))

    def test_selected_staging_has_no_node_subprocess_or_web_runtime_dependency(self) -> None:
        from autodesign.agents.external_designer_author import ExternalDesignerAuthor

        selected = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            attempt_dir = run_dir / "designer_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            ctx = _write_full_author_fixture(run_dir, selected=selected)
            author = ExternalDesignerAuthor(
                SimpleNamespace(designer_author_model=""),
                "",
            )

            with (
                patch.dict(os.environ, {"PATH": ""}),
                patch(
                    "subprocess.run",
                    side_effect=AssertionError("CSS staging must not launch a subprocess"),
                ) as subprocess_run,
            ):
                self.assertTrue(author._stage_inputs(
                    ctx,
                    brief="Create an academic paper poster.",
                    attempt_dir=attempt_dir,
                ))
            subprocess_run.assert_not_called()
            blueprint = (attempt_dir / "reference_style_blueprint.html").read_text(
                encoding="utf-8"
            )
            self.assertIn("var(--poster-primary)", blueprint)
            self.assertNotIn("#A4113F", blueprint)

    def test_selected_staging_fails_closed_on_reference_color_outside_declarations(self) -> None:
        from autodesign.agents.external_designer_author import ExternalDesignerAuthor

        selected = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            attempt_dir = run_dir / "designer_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            ctx = _write_full_author_fixture(
                run_dir,
                selected=selected,
                adversarial_reference_attribute=True,
            )
            author = ExternalDesignerAuthor(
                SimpleNamespace(designer_author_model=""),
                "",
            )

            with self.assertRaisesRegex(
                ValueError,
                r"forbidden reference palette token.*#A4113F",
            ):
                author._stage_inputs(
                    ctx,
                    brief="Create an academic paper poster.",
                    attempt_dir=attempt_dir,
                )
            self.assertFalse((attempt_dir / "reference_style_blueprint.html").exists())

    def test_selected_staging_preserves_matching_reference_style_provenance_id(self) -> None:
        from autodesign.agents.external_designer_author import (
            _write_selected_reference_blueprint,
        )

        selected = require_academic_color_system("plum_sage")
        reference_colors = require_academic_color_system("mulberry_mint")
        style_id = "reference_3ea7500c"
        reference = _reference_contract({
            **reference_colors,
            "palette_id": style_id,
        })
        reference["style_reference_id"] = style_id
        blueprint = (
            '<html><head><style>.paper-poster{background:#FFFFFF;color:#A4113F}</style></head>'
            f'<body><main class="paper-poster" data-reference-style-id="{style_id}">'
            "{{PAPER_TITLE}}</main></body></html>"
        )

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source = root / "reference_style_blueprint.html"
            target = root / "staged_reference_style_blueprint.html"
            source.write_text(blueprint, encoding="utf-8")

            _write_selected_reference_blueprint(
                source,
                target,
                reference=reference,
                selected=selected,
            )

            staged = target.read_text(encoding="utf-8")
            self.assertIn(f'data-reference-style-id="{style_id}"', staged)
            self.assertNotIn("#A4113F", staged)

    def test_selected_staging_fails_closed_on_semantic_escaped_reference_color(self) -> None:
        from autodesign.agents.external_designer_author import ExternalDesignerAuthor

        selected = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            attempt_dir = run_dir / "designer_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            ctx = _write_full_author_fixture(
                run_dir,
                selected=selected,
                adversarial_escaped_selector=True,
            )
            author = ExternalDesignerAuthor(
                SimpleNamespace(designer_author_model=""),
                "",
            )

            with self.assertRaisesRegex(
                ValueError,
                r"forbidden reference palette token.*#A4113F",
            ):
                author._stage_inputs(
                    ctx,
                    brief="Create an academic paper poster.",
                    attempt_dir=attempt_dir,
                )
            self.assertFalse((attempt_dir / "reference_style_blueprint.html").exists())


if __name__ == "__main__":
    unittest.main()
