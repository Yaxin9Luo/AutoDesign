from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest

from pydantic import ValidationError

from autodesign.skills.registry import SkillManifest, SkillRegistry


_BUILTIN_SKILL_IDS = {
    "common.export_qa",
    "common.pdf_render_qa",
    "common.pdf_visual_curation",
    "common.playwright_browser_qa",
    "common.source_analysis_flow",
    "deck.html_ppt_general",
    "deck.paper2deck_provenance",
    "deck.ppt_beautify",
    "deck.report2deck_general",
    "landing.visual_recipe",
    "poster.paper_poster_revision",
    "poster.reference_style_extraction",
    "poster.table_craft",
    "poster.visual_recipe",
    "video.conference_video",
}


class RuntimeSkillsV2Test(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _v2_manifest(self, **overrides: object) -> dict[str, object]:
        manifest: dict[str, object] = {
            "manifest_version": 2,
            "id": "test.progressive_disclosure",
            "version": "1.0.0",
            "description": "Focused runtime guidance for a test artifact.",
            "applies_to": ["poster"],
            "stages": ["plan", "repair"],
            "triggers": [],
            "priority": 50,
            "enabled_by_default": True,
            "source": {"kind": "test"},
            "assets": ["assets/non_prompt_data.json"],
            "outputs": [],
            "resources": [
                {
                    "id": "plan-json",
                    "path": "resources/plan.json",
                    "description": "Structured planning constraints.",
                    "stages": ["plan"],
                    "when_to_read": "Read before producing a plan.",
                    "media_type": "application/json",
                },
            ],
        }
        manifest.update(overrides)
        return manifest

    def _write_v2_pack(
        self,
        name: str,
        *,
        manifest: dict[str, object] | None = None,
        files: dict[str, str] | None = None,
        markdown: str | None = None,
    ) -> Path:
        pack = self.root / name
        pack.mkdir(parents=True)
        payload = manifest or self._v2_manifest()
        (pack / "skill.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        (pack / "SKILL.md").write_text(
            markdown
            or "## Stage: plan\nPLAN-ONLY\n\n## Stage: repair\nREPAIR-ONLY\n",
            encoding="utf-8",
        )
        for relative_path, content in (files or {
            "assets/non_prompt_data.json": '{"asset":"must not be prompt context"}',
            "resources/plan.json": '{"instruction":"read on demand"}',
        }).items():
            path = pack / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return pack

    def _load(self) -> SkillRegistry:
        return SkillRegistry.load(self.root)

    def test_v1_manifest_remains_loadable_with_legacy_assets(self) -> None:
        pack = self.root / "legacy"
        (pack / "assets").mkdir(parents=True)
        (pack / "skill.json").write_text(
            json.dumps({
                "id": "legacy.runtime_skill",
                "version": "0.1.0",
                "applies_to": ["poster"],
                "stages": ["plan"],
                "assets": ["assets/legacy.json"],
            }),
            encoding="utf-8",
        )
        (pack / "SKILL.md").write_text("## Stage: plan\nLEGACY-PLAN\n", encoding="utf-8")
        (pack / "assets" / "legacy.json").write_text('{"legacy":true}', encoding="utf-8")

        registry = self._load()

        self.assertEqual([pack.id for pack in registry.packs], ["legacy.runtime_skill"])
        loaded = registry.packs[0]
        self.assertEqual(loaded.manifest.manifest_version, 1)
        self.assertEqual(loaded.manifest.resources, [])
        rendered = loaded.render("plan")
        self.assertIn("LEGACY-PLAN", rendered)
        self.assertIn('"legacy": true', rendered)

    def test_v2_manifest_rejects_invalid_contract_fields(self) -> None:
        cases = {
            "empty skill id": {"id": ""},
            "description over 160 chars": {"description": "x" * 161},
            "empty description": {"description": ""},
            "empty resource id": {
                "resources": [{
                    "id": "",
                    "path": "resources/plan.json",
                    "description": "Structured planning constraints.",
                    "stages": ["plan"],
                    "when_to_read": "Read before planning.",
                    "media_type": "application/json",
                }],
            },
            "missing resource description": {
                "resources": [{
                    "id": "plan-json",
                    "path": "resources/plan.json",
                    "stages": ["plan"],
                    "when_to_read": "Read before planning.",
                    "media_type": "application/json",
                }],
            },
            "missing when_to_read": {
                "resources": [{
                    "id": "plan-json",
                    "path": "resources/plan.json",
                    "description": "Structured planning constraints.",
                    "stages": ["plan"],
                    "media_type": "application/json",
                }],
            },
            "unknown resource stage": {
                "resources": [{
                    "id": "plan-json",
                    "path": "resources/plan.json",
                    "description": "Structured planning constraints.",
                    "stages": ["preview"],
                    "when_to_read": "Read before planning.",
                    "media_type": "application/json",
                }],
            },
            "multiword manifest stage": {"stages": ["plan review"]},
            "multiword resource stage": {
                "resources": [{
                    "id": "plan-json",
                    "path": "resources/plan.json",
                    "description": "Structured planning constraints.",
                    "stages": ["plan review"],
                    "when_to_read": "Read before planning.",
                    "media_type": "application/json",
                }],
            },
            "resource stage outside skill stages": {
                "stages": ["plan"],
                "resources": [{
                    "id": "repair-json",
                    "path": "resources/repair.json",
                    "description": "Repair constraints.",
                    "stages": ["repair"],
                    "when_to_read": "Read before repair.",
                    "media_type": "application/json",
                }],
            },
        }

        for label, overrides in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ValidationError):
                    SkillManifest.model_validate(self._v2_manifest(**overrides))

        coerced_v2 = self._v2_manifest(manifest_version="2")
        coerced_v2["unknown_field"] = "must not be ignored"
        with self.assertRaises(ValidationError):
            SkillManifest.model_validate(coerced_v2)

    def test_v2_skill_stage_headings_exactly_match_manifest(self) -> None:
        for name, markdown in {
            "missing-repair": "## Stage: plan\nPLAN\n",
            "duplicate-plan": (
                "## Stage: plan\nONE\n\n## Stage: plan\nTWO\n\n"
                "## Stage: repair\nREPAIR\n"
            ),
        }.items():
            with self.subTest(name=name):
                self._write_v2_pack(name, markdown=markdown)
                self.assertEqual(SkillRegistry.load(self.root / name).packs, [])

    def test_builtin_skill_ids_are_the_stable_set(self) -> None:
        skills_root = Path(__file__).resolve().parents[1] / "skills"

        registry = SkillRegistry.load(skills_root)

        loaded_ids = [pack.id for pack in registry.packs]
        self.assertEqual(set(loaded_ids), _BUILTIN_SKILL_IDS)
        self.assertEqual(len(loaded_ids), 15)
        for pack in registry.packs:
            for resource in pack.manifest.resources:
                self.assertTrue(
                    set(resource.stages).issubset({"plan", "repair"}),
                    f"{pack.id}:{resource.id} is not reachable through an on-demand consumer",
                )
            for stage in ("enhance", "critique"):
                self.assertNotIn("#### Runtime resources", pack.render(stage))

    def test_v2_load_rejects_unsafe_or_invalid_resources(self) -> None:
        cases: list[tuple[str, dict[str, object], dict[str, str]]] = [
            (
                "duplicate-id",
                self._v2_manifest(resources=[
                    {
                        "id": "same",
                        "path": "resources/one.txt",
                        "description": "First resource.",
                        "stages": ["plan"],
                        "when_to_read": "Read before planning.",
                        "media_type": "text/plain",
                    },
                    {
                        "id": "same",
                        "path": "resources/two.txt",
                        "description": "Second resource.",
                        "stages": ["plan"],
                        "when_to_read": "Read before planning.",
                        "media_type": "text/plain",
                    },
                ]),
                {"resources/one.txt": "one", "resources/two.txt": "two"},
            ),
            (
                "missing-file",
                self._v2_manifest(),
                {"assets/non_prompt_data.json": "{}"},
            ),
            (
                "invalid-json",
                self._v2_manifest(),
                {
                    "assets/non_prompt_data.json": "{}",
                    "resources/plan.json": "{invalid json",
                },
            ),
            (
                "traversal",
                self._v2_manifest(resources=[
                    {
                        "id": "escape",
                        "path": "../outside.json",
                        "description": "Must remain inside this pack.",
                        "stages": ["plan"],
                        "when_to_read": "Never, because this is invalid.",
                        "media_type": "application/json",
                    },
                ]),
                {"assets/non_prompt_data.json": "{}"},
            ),
        ]

        for name, manifest, files in cases:
            with self.subTest(name=name):
                self._write_v2_pack(name, manifest=manifest, files=files)
                registry = SkillRegistry.load(self.root / name)
                self.assertEqual(registry.packs, [])

        outside = self.root / "outside.json"
        outside.write_text('{"outside":true}', encoding="utf-8")
        symlink_manifest = self._v2_manifest(resources=[
            {
                "id": "escape",
                "path": "resources/escape.json",
                "description": "Must remain inside this pack.",
                "stages": ["plan"],
                "when_to_read": "Never, because this is invalid.",
                "media_type": "application/json",
            },
        ])
        symlink_pack = self._write_v2_pack(
            "symlink-escape",
            manifest=symlink_manifest,
            files={"assets/non_prompt_data.json": "{}"},
        )
        link = symlink_pack / "resources" / "escape.json"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)

        registry = SkillRegistry.load(symlink_pack)

        self.assertEqual(registry.packs, [])

    def test_v2_rejects_resources_over_twelve_thousand_characters(self) -> None:
        self._write_v2_pack(
            "too-large",
            files={
                "assets/non_prompt_data.json": "{}",
                "resources/plan.json": "x" * 12_001,
            },
        )

        registry = self._load()

        self.assertEqual(registry.packs, [])

    def test_v2_render_has_no_stage_leak_or_eager_resource_content(self) -> None:
        self._write_v2_pack("render")
        pack = self._load().packs[0]

        plan = pack.render("plan")
        repair = pack.render("repair")

        self.assertIn("PLAN-ONLY", plan)
        self.assertNotIn("REPAIR-ONLY", plan)
        self.assertNotIn("read on demand", plan)
        self.assertNotIn("must not be prompt context", plan)
        self.assertIn("REPAIR-ONLY", repair)
        self.assertNotIn("PLAN-ONLY", repair)
        self.assertNotIn("read on demand", repair)

    def test_v2_content_hash_covers_canonical_manifest_skill_and_resources(self) -> None:
        resources = [
            {
                "id": "zeta",
                "path": "resources/zeta.txt",
                "description": "Zeta resource.",
                "stages": ["plan"],
                "when_to_read": "Read when zeta guidance is needed.",
                "media_type": "text/plain",
            },
            {
                "id": "alpha",
                "path": "resources/alpha.txt",
                "description": "Alpha resource.",
                "stages": ["plan"],
                "when_to_read": "Read when alpha guidance is needed.",
                "media_type": "text/plain",
            },
        ]
        files = {
            "assets/non_prompt_data.json": "{}",
            "resources/alpha.txt": "alpha-v1",
            "resources/zeta.txt": "zeta-v1",
        }
        self._write_v2_pack("hash-a", manifest=self._v2_manifest(resources=resources), files=files)
        self._write_v2_pack("hash-b", manifest=self._v2_manifest(resources=list(reversed(resources))), files=files)
        self._write_v2_pack(
            "hash-skill-change",
            manifest=self._v2_manifest(resources=resources),
            files=files,
            markdown=(
                "## Stage: plan\nDIFFERENT-SKILL-BODY\n\n"
                "## Stage: repair\nREPAIR-ONLY\n"
            ),
        )
        changed_files = dict(files)
        changed_files["resources/alpha.txt"] = "alpha-v2"
        self._write_v2_pack(
            "hash-resource-change",
            manifest=self._v2_manifest(resources=resources),
            files=changed_files,
        )

        hashes = {
            pack.root.name: pack.content_hash
            for pack in self._load().packs
        }

        self.assertEqual(hashes["hash-a"], hashes["hash-b"])
        self.assertNotEqual(hashes["hash-a"], hashes["hash-skill-change"])
        self.assertNotEqual(hashes["hash-a"], hashes["hash-resource-change"])

    def test_resource_reads_are_selected_stage_limited_and_tamper_safe(self) -> None:
        repair_resource = {
            "id": "repair-notes",
            "path": "resources/repair.txt",
            "description": "Repair-only guidance.",
            "stages": ["repair"],
            "when_to_read": "Read while repairing the artifact.",
            "media_type": "text/plain",
        }
        manifest = self._v2_manifest(resources=[
            *self._v2_manifest()["resources"],  # type: ignore[arg-type]
            repair_resource,
        ])
        pack_dir = self._write_v2_pack(
            "reads",
            manifest=manifest,
            files={
                "assets/non_prompt_data.json": "{}",
                "resources/plan.json": '{"instruction":"read on demand"}',
                "resources/repair.txt": "repair-only guidance",
            },
        )
        registry = self._load()
        selected = registry.select(brief="create a poster", attachments=[], artifact_hint="poster")
        unselected = registry.select(brief="create a deck", attachments=[], artifact_hint="deck")

        self.assertEqual(
            selected.read_resource(
                skill_id="test.progressive_disclosure",
                resource_id="plan-json",
                stage="plan",
            ),
            '{"instruction":"read on demand"}',
        )
        self.assertIsNone(
            unselected.read_resource(
                skill_id="test.progressive_disclosure",
                resource_id="plan-json",
                stage="plan",
            )
        )
        self.assertIsNone(
            selected.read_resource(
                skill_id="test.progressive_disclosure",
                resource_id="repair-notes",
                stage="plan",
            )
        )
        self.assertIsNone(
            selected.read_resource(
                skill_id="test.progressive_disclosure",
                resource_id="does-not-exist",
                stage="plan",
            )
        )

        (pack_dir / "resources" / "plan.json").write_text(
            '{"instruction":"tampered after load"}',
            encoding="utf-8",
        )

        self.assertIsNone(
            selected.read_resource(
                skill_id="test.progressive_disclosure",
                resource_id="plan-json",
                stage="plan",
            )
        )

    def test_resource_read_returns_complete_valid_json_without_truncation(self) -> None:
        resource_text = json.dumps({"payload": "x" * 11_000}, separators=(",", ":"))
        self.assertLessEqual(len(resource_text), 12_000)
        self._write_v2_pack(
            "complete-json",
            files={
                "assets/non_prompt_data.json": "{}",
                "resources/plan.json": resource_text,
            },
        )
        bundle = self._load().select(
            brief="create a poster",
            attachments=[],
            artifact_hint="poster",
        )

        resource = bundle.read_resource(
            skill_id="test.progressive_disclosure",
            resource_id="plan-json",
            stage="plan",
        )

        self.assertEqual(resource, resource_text)
        self.assertEqual(json.loads(resource), {"payload": "x" * 11_000})

    def test_runtime_summary_round_trip_preserves_hash_relevant_manifest_fields(self) -> None:
        manifest = self._v2_manifest(
            priority=93,
            triggers=["paper", "render"],
            enabled_by_default=False,
        )
        self._write_v2_pack("round-trip", manifest=manifest)
        pack = self._load().packs[0]

        restored = type(pack).from_runtime_summary(pack.runtime_summary())

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.manifest.priority, 93)
        self.assertEqual(restored.manifest.triggers, ["paper", "render"])
        self.assertFalse(restored.manifest.enabled_by_default)
        self.assertEqual(
            restored.read_resource("plan-json", "plan"),
            '{"instruction":"read on demand"}',
        )

    def test_run_snapshot_re_roots_resources_and_survives_source_changes(self) -> None:
        from autodesign.runner import (
            _runtime_skill_state,
            _write_runtime_skill_snapshot,
        )
        from autodesign.skills.registry import SkillBundle

        pack_dir = self._write_v2_pack(
            "snapshot",
            manifest=self._v2_manifest(priority=93, triggers=["poster"]),
        )
        bundle = self._load().select(
            brief="create a poster",
            attachments=[],
            artifact_hint="poster",
        )
        run_dir = self.root / "run"
        snapshot = _write_runtime_skill_snapshot(
            run_dir,
            skill_bundle=bundle,
            skill_contexts=bundle.render_all(),
        )
        state = _runtime_skill_state(bundle, snapshot)
        restored = SkillBundle.from_runtime_state(state)
        (pack_dir / "resources" / "plan.json").write_text(
            '{"instruction":"changed in repository"}',
            encoding="utf-8",
        )

        self.assertTrue((run_dir / "runtime_skills" / "snapshot.json").is_file())
        self.assertTrue((run_dir / "runtime_skills" / "index.md").is_file())
        self.assertEqual(
            restored.read_resource(
                skill_id="test.progressive_disclosure",
                resource_id="plan-json",
                stage="plan",
            ),
            '{"instruction":"read on demand"}',
        )
        changed_bundle = self._load().select(
            brief="create a poster",
            attachments=[],
            artifact_hint="poster",
        )
        with self.assertRaisesRegex(ValueError, "snapshot conflicts"):
            _write_runtime_skill_snapshot(
                run_dir,
                skill_bundle=changed_bundle,
                skill_contexts=changed_bundle.render_all(),
            )

    def test_skill_audit_rejects_output_contract_drift(self) -> None:
        from scripts.audit_runtime_skills import _audit_pack

        source = Path(__file__).resolve().parents[1] / "skills/common/export_qa"
        pack = self.root / "audit-output-drift"
        shutil.copytree(source, pack)
        manifest_path = pack / "skill.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["outputs"] = ["silently_changed_output"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        _, violations, _ = _audit_pack(manifest_path)

        self.assertTrue(any("outputs changed" in item for item in violations), violations)

    def test_designer_input_keeps_control_prologues_at_byte_zero(self) -> None:
        from autodesign.runner import _compose_designer_input

        composed = _compose_designer_input(
            "## Enhanced brief\n\nMake the poster.",
            runner_prologues=(
                "Attached files:\n  - /tmp/paper.pdf\n\n---\n\n"
                "Template:\n  name: cvpr-landscape"
            ),
            plan_context="## AutoDesign Runtime Skills Context (plan)\nPLAN INDEX",
        )

        self.assertTrue(composed.startswith("Attached files:"))
        self.assertLess(composed.index("## Enhanced brief"), composed.index("PLAN INDEX"))
        self.assertEqual(composed.count("Runtime Skills Context (plan)"), 1)

    def test_reference_style_prompt_uses_mandatory_resource_instead_of_inline_schema(self) -> None:
        from autodesign.agents.reference_style_agent import _reference_style_prompt

        resource_path = (
            Path(__file__).resolve().parents[1]
            / "skills/poster/reference_style_extraction/references/output_contract_v4.md"
        )
        prompt = _reference_style_prompt(
            self.root,
            {
                "canvas_contract": {
                    "w_px": 4096,
                    "h_px": 2048,
                    "aspect_ratio": "2:1",
                }
            },
            model_hint="",
            runtime_skill={
                "resources": [{
                    "id": "output_contract_v4",
                    "path": "references/output_contract_v4.md",
                }]
            },
        )

        contract_text = resource_path.read_text(encoding="utf-8")
        self.assertIn('"body_region_structure"', contract_text)
        self.assertIn("runtime_skills/references/output_contract_v4.md", prompt)
        self.assertNotIn('"body_region_structure"', prompt)
        self.assertLess(len(prompt), 5_000)

    def test_external_author_stages_active_stage_resources_and_reads_index_first(self) -> None:
        from autodesign.agents.external_designer_author import (
            _author_must_read_first,
            _stage_runtime_skills,
        )
        from autodesign.runner import _write_runtime_skill_snapshot

        repair_resource = {
            "id": "repair-notes",
            "path": "resources/repair.txt",
            "description": "Repair-only guidance.",
            "stages": ["repair"],
            "when_to_read": "Read while repairing.",
            "media_type": "text/plain",
        }
        self._write_v2_pack(
            "external",
            manifest=self._v2_manifest(resources=[
                *self._v2_manifest()["resources"],  # type: ignore[arg-type]
                repair_resource,
            ]),
            files={
                "assets/non_prompt_data.json": "{}",
                "resources/plan.json": '{"instruction":"plan only"}',
                "resources/repair.txt": "repair only",
            },
        )
        bundle = self._load().select(
            brief="create a poster",
            attachments=[],
            artifact_hint="poster",
        )
        run_dir = self.root / "run"
        snapshot = _write_runtime_skill_snapshot(
            run_dir,
            skill_bundle=bundle,
            skill_contexts=bundle.render_all(),
        )
        attempt_dir = self.root / "attempt"
        attempt_dir.mkdir()
        ctx = SimpleNamespace(
            run_dir=run_dir,
            state={"skills": snapshot["runtime_state"]},
            settings=SimpleNamespace(skills_dir=self.root),
        )
        staged = _stage_runtime_skills(
            ctx,
            attempt_dir,
            stage="plan",
        )

        self.assertTrue((attempt_dir / "runtime_skills/index.md").is_file())
        self.assertTrue(any(path.endswith("resources/plan.json") for path in staged["files"]))
        self.assertFalse(any(path.endswith("resources/repair.txt") for path in staged["files"]))
        self.assertEqual(
            _author_must_read_first([], staged["files"])[0],
            "runtime_skills/index.md",
        )

        snapshot_resource = next(
            (run_dir / "runtime_skills/packs").glob("*/resources/plan.json")
        )
        snapshot_resource.write_text('{"instruction":"tampered"}', encoding="utf-8")
        tampered_attempt = self.root / "tampered-attempt"
        tampered_attempt.mkdir()
        with self.assertRaises(ValueError):
            _stage_runtime_skills(ctx, tampered_attempt, stage="plan")

        snapshot_resource.write_text('{"instruction":"plan only"}', encoding="utf-8")
        snapshot_path = run_dir / "runtime_skills/snapshot.json"
        snapshot_json = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot_json["selected"][0]["pack_path"] = "../../escaped"
        snapshot_path.write_text(json.dumps(snapshot_json), encoding="utf-8")
        traversal_attempt = self.root / "traversal-attempt"
        traversal_attempt.mkdir()
        with self.assertRaises(ValueError):
            _stage_runtime_skills(ctx, traversal_attempt, stage="plan")
        self.assertFalse((self.root / "escaped/SKILL.md").exists())

    def test_external_author_preserves_enhanced_brief_while_stripping_runtime_index(self) -> None:
        from autodesign.agents.external_designer_author import _author_visible_brief

        ctx = SimpleNamespace(
            state={
                "raw_user_brief": "RAW USER REQUEST",
                "reference_style_contract": {},
            }
        )
        enhanced = (
            "## Enhanced brief\n\nENHANCED CONTENT\n\n---\n\n"
            "## AutoDesign Runtime Skills Context (plan)\nPLAN INDEX\n\n---\n\n"
            "Trailing authored requirements"
        )

        visible = _author_visible_brief(ctx, enhanced)

        self.assertIn("ENHANCED CONTENT", visible)
        self.assertIn("Trailing authored requirements", visible)
        self.assertNotIn("Runtime Skills Context", visible)
        self.assertNotIn("PLAN INDEX", visible)

    def test_external_code_editor_does_not_apply_logo_specific_validation(self) -> None:
        from autodesign.agents.external_code_editor import (
            ExternalCodeEditor,
            _snapshot_staged_file_hashes,
        )

        attempt = self.root / "logo-policy"
        attempt.mkdir()
        base_html = (
            "<!doctype html><html><body><main class='paper-poster'>"
            "<header><h1>Paper title</h1><p>Authors</p><p>Institution</p></header>"
            "<section><h2>Method</h2><p>Grounded poster content.</p></section>"
            "</main></body></html>"
        )
        (attempt / "current_poster.html").write_text(base_html, encoding="utf-8")
        logo_path = attempt / "assets" / "provided-logo.png"
        logo_path.parent.mkdir()
        logo_path.write_bytes(b"provided-logo")
        staged_hashes = _snapshot_staged_file_hashes(attempt)
        editor = ExternalCodeEditor(SimpleNamespace())
        prompt = editor._build_prompt(
            attempt_dir=attempt,
            parent_run_id="parent-run",
            instruction="Search for and add the official institution logo.",
            conversation_history=[],
            required_color_system={"palette_id": "test-palette"},
            selection_context=None,
            repair_feedback=None,
        )
        self.assertIn("Do not look up or add logos unless the user explicitly requests", prompt)
        self.assertIn("official website/media kit", prompt)
        self.assertIn("fetched_identity_assets", prompt)

        def validate(
            body: str,
            instruction: str,
            *,
            done_summary: dict[str, object] | None = None,
        ) -> dict[str, object]:
            poster = attempt / "poster.html"
            poster.write_text(
                "<!doctype html><html><body><main class='paper-poster'>"
                + body
                + "<section><h2>Method</h2><p>Grounded poster content.</p></section>"
                + "</main></body></html>",
                encoding="utf-8",
            )
            done_path = attempt / "code_editor_done.json"
            if done_summary is None:
                done_path.unlink(missing_ok=True)
            else:
                done_path.write_text(json.dumps(done_summary), encoding="utf-8")
            return editor._validate_output(
                attempt,
                poster,
                instruction=instruction,
                staged_file_hashes=staged_hashes,
            )

        unrequested = validate(
            "<header><img src='assets/provided-logo.png'><h1>Paper title</h1></header>",
            "Make the title blue.",
        )
        self.assertTrue(unrequested["ok"], unrequested)

        generated_logo = attempt / "assets" / "generated-logo.png"
        generated_logo.write_bytes(b"generated-after-staging")
        unstaged = validate(
            "<header><img class='institution-logo' src='assets/generated-logo.png'><h1>Paper title</h1></header>",
            "Add the supplied institution logo.",
        )
        self.assertTrue(unstaged["ok"], unstaged)

        fetched_official = validate(
            "<header><img class='institution-logo' src='assets/generated-logo.png'><h1>Paper title</h1></header>",
            "Search for and add the official institution logo.",
            done_summary={
                "status": "completed",
                "fetched_identity_assets": [
                    {
                        "path": "assets/generated-logo.png",
                        "source_url": "https://www.example.edu/brand/logo",
                        "source_type": "official",
                    }
                ],
            },
        )
        self.assertTrue(fetched_official["ok"], fetched_official)

        unverified_fetch = validate(
            "<header><img class='institution-logo' src='assets/generated-logo.png'><h1>Paper title</h1></header>",
            "Search for and add the official institution logo.",
            done_summary={
                "status": "completed",
                "fetched_identity_assets": [
                    {
                        "path": "assets/generated-logo.png",
                        "source_url": "http://unverified.example/logo",
                        "source_type": "search_result",
                    }
                ],
            },
        )
        self.assertTrue(unverified_fetch["ok"], unverified_fetch)

        remote_hotlink = validate(
            "<header><img class='institution-logo' src='https://example.edu/logo.png'><h1>Paper title</h1></header>",
            "Add the institution logo.",
        )
        self.assertFalse(remote_hotlink["ok"])
        self.assertTrue(any("remote asset reference" in item for item in remote_hotlink["errors"]))

        allowed = validate(
            "<header><img class='institution-logo' src='assets/provided-logo.png'><h1>Paper title</h1></header>",
            "Add the supplied institution logo.",
        )
        self.assertTrue(allowed["ok"], allowed)

        poster = attempt / "poster.html"
        poster.write_text(
            "<!doctype html><html><body><main class='paper-poster'>"
            "<header><img class='institution-logo' src='assets/provided-logo.png'>"
            "<h1>Paper title</h1></header>"
            "<section><h2>Method</h2><p>Grounded poster content.</p></section>"
            "</main></body></html>",
            encoding="utf-8",
        )
        allowed_by_selection_instruction = editor._validate_output(
            attempt,
            poster,
            instruction="Apply the selected edits.",
            selection_context={
                "kind": "multi",
                "items": [{"instruction": "Add the supplied institution logo."}],
            },
            staged_file_hashes=staged_hashes,
        )
        self.assertTrue(allowed_by_selection_instruction["ok"], allowed_by_selection_instruction)

        text_badge = validate(
            "<header><div class='institution-logo-badge'>University X</div><h1>Paper title</h1></header>",
            "Add the supplied institution logo.",
        )
        self.assertTrue(text_badge["ok"], text_badge)

        neutral_image = validate(
            "<div class='identity-chip'><img src='assets/generated-logo.png'></div>",
            "Make the title blue.",
        )
        self.assertTrue(neutral_image["ok"], neutral_image)

        styled_badge = validate(
            "<header><div class='affiliation' style='border:1px solid #333'>University X</div></header>",
            "Make the title blue.",
        )
        self.assertTrue(styled_badge["ok"], styled_badge)

        negated = validate(
            "<header><img src='assets/provided-logo.png'><h1>Paper title</h1></header>",
            "Do not add a logo; make the title blue.",
        )
        self.assertTrue(negated["ok"], negated)

        class_styled_badge = validate(
            "<style>.affiliation{border:1px solid #333}</style>"
            "<div class='top-row'><div class='affiliation'>University X</div></div>"
            "<section><h2>Overview</h2></section>",
            "Make the title blue.",
        )
        self.assertTrue(class_styled_badge["ok"], class_styled_badge)

        existing_attempt = self.root / "existing-logo-policy"
        existing_attempt.mkdir()
        existing_logo = existing_attempt / "assets" / "existing.png"
        existing_logo.parent.mkdir()
        existing_logo.write_bytes(b"original")
        existing_html = (
            "<!doctype html><html><body><main class='paper-poster'>"
            "<header><img src='assets/existing.png'><h1>Paper title</h1></header>"
            "<section><h2>Method</h2><p>Grounded poster content.</p></section>"
            "</main></body></html>"
        )
        (existing_attempt / "current_poster.html").write_text(existing_html, encoding="utf-8")
        existing_hashes = _snapshot_staged_file_hashes(existing_attempt)
        existing_logo.write_bytes(b"replaced")
        existing_output = existing_attempt / "poster.html"
        existing_output.write_text(
            existing_html.replace("Paper title", "Revised paper title"),
            encoding="utf-8",
        )
        mutated = editor._validate_output(
            existing_attempt,
            existing_output,
            instruction="Make the title blue.",
            staged_file_hashes=existing_hashes,
        )
        self.assertTrue(mutated["ok"], mutated)

        moved_output = existing_attempt / "poster.html"
        moved_output.write_text(
            "<!doctype html><html><body><main class='paper-poster'>"
            "<div class='top-row'><img src='assets/existing.png'></div>"
            "<section><h2>Method</h2><p>Grounded poster content with revised supporting detail.</p></section>"
            "</main></body></html>",
            encoding="utf-8",
        )
        moved = editor._validate_output(
            existing_attempt,
            moved_output,
            instruction="Make the title blue.",
            staged_file_hashes=existing_hashes,
        )
        self.assertTrue(moved["ok"], moved)

        moved_output.write_text(
            "<!doctype html><html><body><main class='paper-poster'>"
            "<section><h2>Method</h2><p>Grounded poster content with revised supporting detail.</p>"
            "<img src='assets/existing.png?v=2#mark'></section>"
            "</main></body></html>",
            encoding="utf-8",
        )
        equivalent_ref = editor._validate_output(
            existing_attempt,
            moved_output,
            instruction="Make the title blue.",
            staged_file_hashes=existing_hashes,
        )
        self.assertTrue(equivalent_ref["ok"], equivalent_ref)

    def test_external_code_editor_allows_existing_identity_header_text_revision(self) -> None:
        from autodesign.agents.external_code_editor import (
            ExternalCodeEditor,
            _snapshot_staged_file_hashes,
        )

        attempt = self.root / "identity-header-text-revision"
        attempt.mkdir()
        baseline = (
            "<!doctype html><html><head><style>"
            ".poster-header{background:#fff;border-top:8px solid #B84A16}"
            "</style></head><body><main class='paper-poster'>"
            "<header class='poster-header' data-block-id='panel_identity_header_01'>"
            "<h1>Paper title</h1><p>Research Team</p><p>Institution</p></header>"
            "<section><h2>Method</h2><p>Grounded poster content.</p></section>"
            "</main></body></html>"
        )
        (attempt / "current_poster.html").write_text(baseline, encoding="utf-8")
        staged_hashes = _snapshot_staged_file_hashes(attempt)
        poster = attempt / "poster.html"
        poster.write_text(
            baseline.replace(
                "<p>Research Team</p>",
                "<p>A. Researcher, B. Scientist, et al.</p>",
            ),
            encoding="utf-8",
        )

        result = ExternalCodeEditor(SimpleNamespace())._validate_output(
            attempt,
            poster,
            instruction="Replace the team label with the paper authors.",
            staged_file_hashes=staged_hashes,
        )

        self.assertTrue(result["ok"], result)

    def test_external_code_editor_rejects_unchanged_parent_as_successful_revision(self) -> None:
        from autodesign.agents.external_code_editor import (
            ExternalCodeEditor,
            _snapshot_staged_file_hashes,
        )

        attempt = self.root / "unchanged-poster-revision"
        attempt.mkdir()
        baseline = (
            "<!doctype html><html><body><main class='paper-poster'>"
            "<header><h1>Paper title</h1><p>Research Team</p></header>"
            "<section><h2>Method</h2><p>Grounded poster content with enough detail "
            "to exercise revision validation independently of the minimum HTML "
            "size guard.</p></section>"
            "</main></body></html>"
        )
        (attempt / "current_poster.html").write_text(baseline, encoding="utf-8")
        staged_hashes = _snapshot_staged_file_hashes(attempt)
        poster = attempt / "poster.html"
        poster.write_text(baseline, encoding="utf-8")

        result = ExternalCodeEditor(SimpleNamespace())._validate_output(
            attempt,
            poster,
            instruction="Replace the team label with the paper authors.",
            staged_file_hashes=staged_hashes,
        )

        self.assertFalse(result["ok"])
        self.assertTrue(
            any("unchanged from the parent poster" in item for item in result["errors"]),
            result,
        )

    def test_external_code_editor_rejects_formatting_only_parent_rewrite(self) -> None:
        from autodesign.agents.external_code_editor import (
            ExternalCodeEditor,
            _snapshot_staged_file_hashes,
        )

        attempt = self.root / "formatting-only-poster-revision"
        attempt.mkdir()
        baseline = (
            "<!doctype html><html><body><main class='paper-poster'>"
            "<header><h1>Paper title</h1><p>Research Team</p></header>"
            "<section><h2>Method</h2><p>Grounded poster content with enough detail "
            "to exercise revision validation independently of the minimum HTML "
            "size guard.</p></section>"
            "</main></body></html>"
        )
        (attempt / "current_poster.html").write_text(baseline, encoding="utf-8")
        staged_hashes = _snapshot_staged_file_hashes(attempt)
        poster = attempt / "poster.html"
        poster.write_text(baseline.replace("><", ">\n  <"), encoding="utf-8")

        result = ExternalCodeEditor(SimpleNamespace())._validate_output(
            attempt,
            poster,
            instruction="Replace the team label with the paper authors.",
            staged_file_hashes=staged_hashes,
        )

        self.assertFalse(result["ok"])
        self.assertTrue(
            any("unchanged from the parent poster" in item for item in result["errors"]),
            result,
        )


if __name__ == "__main__":
    unittest.main()
