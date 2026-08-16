from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_skills._shared import portable_core as core


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS = (
    "autodesign-poster",
    "autodesign-ppt",
    "autodesign-webpage",
    "autodesign-video",
)


class PortableSkillRunStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skill = self.root / "installed-skill"
        (self.skill / "scripts").mkdir(parents=True)
        (self.skill / "references").mkdir()
        (self.skill / "SKILL.md").write_text("# Fixture skill\n", encoding="utf-8")
        (self.skill / "scripts" / "tool.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.skill / "references" / "grounding.md").write_text(
            "Ground every claim.\n", encoding="utf-8"
        )
        self.run = self.root / "workspace" / "run"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _initialize(self) -> dict[str, object]:
        return core.initialize_run(
            self.run,
            self.skill,
            release_version="0.1.0",
            archive_sha256="a" * 64,
        )

    def _begin_attempt(self) -> str:
        self._initialize()
        core.save_plan(self.run, {"artifact_type": "poster", "visual_allocations": []})
        return core.begin_attempt(self.run)

    def _deterministic_attempt(self) -> tuple[str, dict[str, object]]:
        attempt = self._begin_attempt()
        attempt_root = self.run / "attempts" / attempt
        core.atomic_write_bytes(
            attempt_root / "artifact" / "poster.html", b"<h1>Grounded poster</h1>\n"
        )
        core.atomic_write_bytes(attempt_root / "qa" / "previews" / "poster.png", b"png")
        report = core.record_deterministic_result(
            self.run,
            attempt,
            passed=True,
            checks=[{"id": "local_assets", "passed": True}],
            artifact_paths=["artifact/poster.html"],
            preview_paths={"poster": "qa/previews/poster.png"},
        )
        return attempt, report

    def _semantic_attempt(self) -> tuple[str, dict[str, object]]:
        attempt, _ = self._deterministic_attempt()
        context = core.create_review_context(
            self.run,
            attempt,
            rubric={"format_version": 1, "dimensions": ["fidelity", "legibility"]},
        )
        review = {
            "format_version": 1,
            "attempt_id": attempt,
            "review_context_sha256": context["context_sha256"],
            "artifact_hashes": context["artifact_hashes"],
            "preview_hashes": context["preview_hashes"],
            "reviewed_frame_ids": sorted(context["preview_hashes"]),
            "source_manifest_sha256": context["source_manifest_sha256"],
            "rubric_sha256": context["rubric_sha256"],
            "reviewer_mode": "fresh_host_vlm",
            "dimension_scores": {"fidelity": 4, "legibility": 4},
            "blockers": [],
            "localized_repairs": [],
            "verdict": "pass",
            "complete": True,
        }
        stored = core.record_semantic_review(self.run, attempt, review)
        return attempt, stored

    def test_safe_path_rejects_absolute_traversal_and_symlink_escape(self) -> None:
        root = self.root / "safe"
        root.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (root / "link").symlink_to(outside, target_is_directory=True)

        self.assertEqual(core.safe_path(root, "a/b.json"), root / "a" / "b.json")
        for candidate in ("../outside/file", str(outside / "file"), "link/file"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(core.PathSafetyError):
                    core.safe_path(root, candidate)

    def test_atomic_writes_and_append_only_jsonl_are_complete(self) -> None:
        target = self.root / "state" / "record.json"
        core.atomic_write_json(target, {"second": 2, "first": 1})
        self.assertEqual(target.read_text(encoding="utf-8"), '{\n  "first": 1,\n  "second": 2\n}\n')
        core.atomic_write_bytes(target, b"replacement\n")
        self.assertEqual(target.read_bytes(), b"replacement\n")

        events = self.root / "state" / "events.jsonl"
        core.append_jsonl(events, {"event": "one"})
        core.append_jsonl(events, {"event": "two"})
        self.assertEqual(
            [json.loads(line)["event"] for line in events.read_text().splitlines()],
            ["one", "two"],
        )
        self.assertFalse(any(target.parent.glob(".*.tmp-*")))

    def test_secret_redaction_covers_nested_values_and_event_text(self) -> None:
        redacted = core.redact_secrets(
            {
                "api_key": "not-a-real-value",
                "nested": {"Authorization": "Bearer example-secret", "ok": "visible"},
                "message": "PASSWORD=hunter2 request failed",
            }
        )
        self.assertEqual(redacted["api_key"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["Authorization"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["ok"], "visible")
        self.assertNotIn("hunter2", redacted["message"])

    def test_initialize_snapshots_all_runtime_files_without_writing_install(self) -> None:
        before = core.tree_hash(self.skill)
        state = self._initialize()
        self.assertEqual(state["state"], "initialized")
        self.assertEqual(core.tree_hash(self.skill), before)

        manifest = json.loads(
            (self.run / "skill_snapshot" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["release_version"], "0.1.0")
        self.assertEqual(manifest["archive_sha256"], "a" * 64)
        self.assertEqual(
            [entry["path"] for entry in manifest["files"]],
            ["SKILL.md", "references/grounding.md", "scripts/tool.py"],
        )
        self.assertTrue((self.run / "skill_snapshot" / "files" / "scripts" / "tool.py").is_file())

    def test_snapshot_verification_fails_closed_on_tamper_drift_and_traversal(self) -> None:
        self._initialize()
        core.verify_skill_snapshot(self.run, skill_root=self.skill)
        snapshot_file = self.run / "skill_snapshot" / "files" / "scripts" / "tool.py"
        snapshot_file.write_text("VALUE = 2\n", encoding="utf-8")
        with self.assertRaises(core.IntegrityError):
            core.verify_skill_snapshot(self.run, skill_root=self.skill)

        self._initialize_fresh("drift")
        (self.skill / "scripts" / "tool.py").write_text("VALUE = 3\n", encoding="utf-8")
        with self.assertRaises(core.IntegrityError):
            core.verify_skill_snapshot(self.run, skill_root=self.skill)

        self._initialize_fresh("traversal")
        manifest_path = self.run / "skill_snapshot" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["path"] = "../escape"
        core.atomic_write_json(manifest_path, manifest)
        with self.assertRaises(core.IntegrityError):
            core.verify_skill_snapshot(self.run)

    def _initialize_fresh(self, suffix: str) -> None:
        self.run = self.root / "workspace" / suffix
        self._initialize()

    def test_markdown_source_builds_stable_evidence_anchors_and_retrieval(self) -> None:
        self._initialize()
        source = self.root / "paper.md"
        source.write_text(
            "# Method\n\nOur sparse router uses three experts.\n\n"
            "## Results\n\nAccuracy rises from 80% to 85%.\n",
            encoding="utf-8",
        )
        manifest = core.prepare_source(self.run, source)
        evidence = core.load_evidence(self.run)

        self.assertEqual(manifest["status"], "ready")
        self.assertEqual(manifest["source_type"], "markdown")
        self.assertEqual([item["id"] for item in evidence], ["ev-001", "ev-002"])
        self.assertEqual(evidence[0]["anchor"]["heading"], "Method")
        self.assertEqual(evidence[1]["anchor"]["line_start"], 5)
        self.assertEqual(
            core.lexical_retrieve(evidence, "sparse experts", limit=1)[0]["id"],
            "ev-001",
        )
        self.assertEqual(
            manifest["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest()
        )

    def test_resume_rejects_tampered_source_evidence(self) -> None:
        self._initialize()
        source = self.root / "paper.txt"
        source.write_text("Stable source evidence.\n", encoding="utf-8")
        core.prepare_source(self.run, source)
        (self.run / "evidence" / "evidence.jsonl").write_text(
            '{"id":"ev-999","text":"tampered"}\n', encoding="utf-8"
        )
        with self.assertRaises(core.IntegrityError):
            core.resume_run(self.run)

    def test_pdf_source_routes_all_poppler_tools_or_records_blocked_state(self) -> None:
        self._initialize()
        source = self.root / "paper.pdf"
        source.write_bytes(b"%PDF-1.4\nfixture\n")
        manifest = core.prepare_source(
            self.run,
            source,
            tool_paths={"pdftotext": None, "pdfinfo": None, "pdftoppm": None, "pdfimages": None},
        )
        self.assertEqual(manifest["status"], "blocked")
        self.assertEqual(
            manifest["missing_tools"], ["pdfimages", "pdfinfo", "pdftoppm", "pdftotext"]
        )
        self.assertEqual(json.loads((self.run / "run.json").read_text())["state"], "blocked")

    def test_pdf_source_successfully_routes_every_poppler_command(self) -> None:
        self._initialize()
        source = self.root / "paper.pdf"
        source.write_bytes(b"%PDF-1.4\nfixture\n")
        bin_dir = self.root / "fake-poppler"
        bin_dir.mkdir()
        log_path = self.root / "poppler.log"
        script = """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
with Path(os.environ["FAKE_POPPLER_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(name + " " + " ".join(sys.argv[1:]) + "\\n")
if name == "pdfinfo":
    print("Pages: 1")
elif name == "pdftotext":
    Path(sys.argv[-1]).write_text("Sparse routing reaches 85% accuracy.\\n", encoding="utf-8")
elif name == "pdftoppm":
    Path(sys.argv[-1] + "-1.png").write_bytes(b"page")
elif name == "pdfimages" and "-list" in sys.argv:
    print("page num type width height")
elif name == "pdfimages":
    Path(sys.argv[-1] + "-000.png").write_bytes(b"figure")
"""
        tools: dict[str, Path] = {}
        for name in ("pdftotext", "pdfinfo", "pdftoppm", "pdfimages"):
            executable = bin_dir / name
            executable.write_text(script, encoding="utf-8")
            executable.chmod(0o755)
            tools[name] = executable

        previous = os.environ.get("FAKE_POPPLER_LOG")
        os.environ["FAKE_POPPLER_LOG"] = str(log_path)
        try:
            manifest = core.prepare_source(self.run, source, tool_paths=tools)
        finally:
            if previous is None:
                os.environ.pop("FAKE_POPPLER_LOG", None)
            else:
                os.environ["FAKE_POPPLER_LOG"] = previous

        self.assertEqual(manifest["status"], "ready")
        calls = log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual([line.split()[0] for line in calls], [
            "pdfinfo", "pdftotext", "pdftoppm", "pdfimages", "pdfimages"
        ])
        visuals = json.loads(
            (self.run / "evidence" / "source_visuals.json").read_text(encoding="utf-8")
        )["visuals"]
        self.assertEqual(visuals[0]["origin"], "pdf_extracted")
        self.assertEqual(visuals[0]["eligibility"], "review_required")
        self.assertEqual(core.load_evidence(self.run)[0]["anchor"]["page"], 1)

    def test_read_only_install_tree_remains_unchanged(self) -> None:
        before = core.tree_hash(self.skill)
        for path in sorted(self.skill.rglob("*"), reverse=True):
            path.chmod(0o555 if path.is_dir() else 0o444)
        self.skill.chmod(0o555)
        try:
            self._initialize()
            source = self.root / "paper.txt"
            source.write_text("Grounded fixture.\n", encoding="utf-8")
            core.prepare_source(self.run, source)
            self.assertEqual(core.tree_hash(self.skill), before)
            self.assertTrue((self.run / "evidence" / "source.txt").is_file())
        finally:
            self.skill.chmod(0o755)
            for path in self.skill.rglob("*"):
                path.chmod(0o755 if path.is_dir() else 0o644)

    def test_explicit_assets_are_eligible_but_pdf_visuals_require_host_vlm_binding(self) -> None:
        self._initialize()
        source = self.root / "paper.txt"
        source.write_text("Figure 1 shows the sparse routing method.\n", encoding="utf-8")
        asset = self.root / "figure.png"
        asset.write_bytes(b"image")
        core.prepare_source(self.run, source, extra_assets=[asset])
        visuals_path = self.run / "evidence" / "source_visuals.json"
        visuals = json.loads(visuals_path.read_text(encoding="utf-8"))
        self.assertEqual(visuals["visuals"][0]["eligibility"], "eligible")

        pdf_candidate = {
            "id": "vis-002",
            "path": "assets/pdf-figure.png",
            "sha256": hashlib.sha256(b"pdf-image").hexdigest(),
            "origin": "pdf_extracted",
            "page": 1,
            "bbox": None,
            "caption_evidence_id": "ev-001",
            "crop": False,
            "compound": False,
            "vlm_review": None,
            "eligibility": "review_required",
            "allowed_content_roles": [],
            "max_reuse": 1,
        }
        (self.run / "evidence" / "assets" / "pdf-figure.png").write_bytes(b"pdf-image")
        visuals["visuals"].append(pdf_candidate)
        core.atomic_write_json(visuals_path, visuals)
        with self.assertRaises(core.ContractError):
            core.validate_visual_plan(
                self.run, [{"visual_id": "vis-002", "role": "method"}]
            )

        core.bind_host_vlm_visuals(
            self.run,
            {
                "reviewer_mode": "fresh_host_vlm",
                "matches": [
                    {
                        "visual_id": "vis-002",
                        "caption_evidence_id": "ev-001",
                        "confidence": 0.91,
                        "allowed_content_roles": ["method"],
                    }
                ],
            },
        )
        self.assertTrue(
            core.validate_visual_plan(
                self.run, [{"visual_id": "vis-002", "role": "method"}]
            )["valid"]
        )

    def test_reference_images_are_separate_and_style_only(self) -> None:
        self._initialize()
        source = self.root / "paper.txt"
        source.write_text("Source content.\n", encoding="utf-8")
        reference = self.root / "reference.png"
        reference.write_bytes(b"style-reference")
        core.prepare_source(self.run, source, reference_images=[reference])
        visuals = json.loads(
            (self.run / "evidence" / "source_visuals.json").read_text(encoding="utf-8")
        )["visuals"]
        self.assertEqual(visuals[0]["origin"], "style_reference")
        self.assertEqual(visuals[0]["eligibility"], "style_only")
        self.assertTrue(
            (self.run / "evidence" / "reference_images" / "reference-001.png").is_file()
        )
        with self.assertRaises(core.ContractError):
            core.validate_visual_plan(
                self.run, [{"visual_id": visuals[0]["id"], "role": "method"}]
            )

    def test_grounding_checks_quotes_numbers_formulas_and_lexical_overlap(self) -> None:
        evidence = [
            {
                "id": "ev-001",
                "kind": "text",
                "text": "The sparse router improves accuracy from 80% to 85%.",
                "safe_to_quote": True,
                "anchor": {"line_start": 1, "line_end": 1},
                "sha256": hashlib.sha256(
                    b"The sparse router improves accuracy from 80% to 85%."
                ).hexdigest(),
            }
        ]
        valid = core.validate_grounding(
            [
                {"id": "c1", "text": 'The paper says "sparse router".', "source_ids": ["ev-001"], "direct_quote": "sparse router"},
                {"id": "c2", "text": "Accuracy is 85%.", "source_ids": ["ev-001"]},
                {
                    "id": "c3",
                    "text": "The absolute improvement is 5%.",
                    "source_ids": ["ev-001"],
                    "derived_formula": {"expression": "85 - 80 = 5", "inputs": ["85", "80"], "result": "5"},
                },
            ],
            evidence,
        )
        self.assertTrue(valid["valid"], valid)

        invalid = core.validate_grounding(
            [
                {"id": "bad-quote", "text": 'It is "dense".', "source_ids": ["ev-001"], "direct_quote": "dense"},
                {"id": "bad-number", "text": "Accuracy is 91%.", "source_ids": ["ev-001"]},
                {"id": "bad-overlap", "text": "Latency collapses dramatically.", "source_ids": ["ev-001"]},
            ],
            evidence,
        )
        self.assertFalse(invalid["valid"])
        self.assertEqual(
            {error["code"] for error in invalid["errors"]},
            {"quote_not_found", "unsupported_numeric", "insufficient_lexical_overlap"},
        )
        bad_formula = core.validate_grounding(
            [
                {
                    "id": "bad-formula",
                    "text": "The absolute improvement is 6%.",
                    "source_ids": ["ev-001"],
                    "derived_formula": {
                        "expression": "85 - 80 = 6",
                        "inputs": ["85", "80"],
                        "result": "6",
                    },
                }
            ],
            evidence,
        )
        self.assertFalse(bad_formula["valid"])
        self.assertIn("invalid_derived_formula", {error["code"] for error in bad_formula["errors"]})

    def test_visual_role_and_reuse_limits_are_enforced(self) -> None:
        self._initialize()
        source = self.root / "paper.txt"
        source.write_text("Method figure.\n", encoding="utf-8")
        asset = self.root / "figure.png"
        asset.write_bytes(b"image")
        core.prepare_source(self.run, source, extra_assets=[asset])
        visual_id = json.loads(
            (self.run / "evidence" / "source_visuals.json").read_text()
        )["visuals"][0]["id"]
        result = core.validate_visual_plan(
            self.run,
            [
                {"visual_id": visual_id, "role": "method"},
                {"visual_id": visual_id, "role": "result"},
            ],
        )
        self.assertFalse(result["valid"])
        self.assertIn("visual_reuse_limit", {error["code"] for error in result["errors"]})

    def test_source_map_is_validated_and_hash_bound(self) -> None:
        source = self.root / "paper.txt"
        source.write_text("Accuracy reaches 85% with sparse routing.\n", encoding="utf-8")
        # Source preparation must precede planning in a real run; use a fresh initialized run.
        self.run = self.root / "workspace" / "source-map"
        self._initialize()
        core.prepare_source(self.run, source)
        core.save_plan(self.run, {"artifact_type": "poster"})
        attempt = core.begin_attempt(self.run)
        contract = core.write_source_map(
            self.run,
            attempt,
            [{"id": "claim-001", "text": "Sparse routing reaches 85%.", "source_ids": ["ev-001"]}],
        )
        self.assertEqual(contract["attempt_id"], attempt)
        self.assertEqual(contract["grounding"]["valid"], True)
        with self.assertRaises(core.ContractError):
            core.write_source_map(
                self.run,
                attempt,
                [{"id": "claim-002", "text": "Accuracy reaches 99%.", "source_ids": ["ev-001"]}],
            )

    def test_state_machine_and_idempotent_resume_recover_artifact_and_qa_writes(self) -> None:
        attempt = self._begin_attempt()
        self.assertEqual(attempt, "01")
        self.assertEqual(core.begin_attempt(self.run), "01")
        status = core.resume_run(self.run, skill_root=self.skill)
        self.assertEqual(status["next_action"], "author")

        attempt_root = self.run / "attempts" / attempt
        core.atomic_write_bytes(attempt_root / "artifact" / "poster.html", b"artifact")
        self.assertEqual(core.resume_run(self.run)["next_action"], "validate")

        with self.assertRaises(core.SimulatedCrash):
            core.record_deterministic_result(
                self.run,
                attempt,
                passed=True,
                checks=[],
                artifact_paths=["artifact/poster.html"],
                preview_paths={},
                fail_after_write=True,
            )
        recovered = core.resume_run(self.run)
        self.assertEqual(recovered["state"], "deterministic_passed")
        self.assertEqual(recovered["next_action"], "semantic_review")

    def test_invalid_state_transition_is_rejected(self) -> None:
        self._initialize()
        with self.assertRaises(core.StateError):
            core.begin_attempt(self.run)
        with self.assertRaises(core.StateError):
            core.transition_state(self.run, "finalized")

    def test_side_states_are_explicit_and_resume_reports_them(self) -> None:
        self._initialize()
        core.mark_side_state(self.run, "blocked", reason="missing pdftotext")
        status = core.resume_run(self.run)
        self.assertEqual(status["state"], "blocked")
        self.assertEqual(status["next_action"], "resolve_blocker")
        self.assertEqual(status["reason"], "missing pdftotext")

    def test_successful_source_retry_clears_blocked_state(self) -> None:
        self._initialize()
        core.mark_side_state(self.run, "blocked", reason="source tool unavailable")
        source = self.root / "paper.txt"
        source.write_text("Recovered source.\n", encoding="utf-8")
        core.prepare_source(self.run, source)
        status = core.resume_run(self.run)
        self.assertEqual(status["state"], "initialized")
        self.assertEqual(status["next_action"], "plan")

    def test_failed_attempt_starts_targeted_repair_without_overwriting_history(self) -> None:
        attempt, _ = self._deterministic_attempt()
        context = core.create_review_context(
            self.run, attempt, rubric={"format_version": 1, "dimensions": ["fidelity"]}
        )
        review = {
            "format_version": 1,
            "attempt_id": attempt,
            "review_context_sha256": context["context_sha256"],
            "artifact_hashes": context["artifact_hashes"],
            "preview_hashes": context["preview_hashes"],
            "reviewed_frame_ids": sorted(context["preview_hashes"]),
            "source_manifest_sha256": context["source_manifest_sha256"],
            "rubric_sha256": context["rubric_sha256"],
            "reviewer_mode": "fresh_subagent",
            "dimension_scores": {"fidelity": 2},
            "blockers": ["unsupported claim"],
            "localized_repairs": [{"target": "results", "action": "replace claim"}],
            "verdict": "fail",
            "complete": True,
        }
        core.record_semantic_review(self.run, attempt, review)
        repaired = core.begin_attempt(self.run)
        self.assertEqual(repaired, "02")
        self.assertTrue((self.run / "attempts" / "01" / "qa" / "semantic-review.json").is_file())
        self.assertEqual(core.resume_run(self.run)["next_action"], "author")

    def test_review_is_hash_bound_and_rejects_wrong_partial_stale_or_incomplete_input(self) -> None:
        attempt, _ = self._deterministic_attempt()
        context = core.create_review_context(
            self.run, attempt, rubric={"format_version": 1, "dimensions": ["fidelity"]}
        )
        base = {
            "format_version": 1,
            "attempt_id": attempt,
            "review_context_sha256": context["context_sha256"],
            "artifact_hashes": context["artifact_hashes"],
            "preview_hashes": context["preview_hashes"],
            "reviewed_frame_ids": sorted(context["preview_hashes"]),
            "source_manifest_sha256": context["source_manifest_sha256"],
            "rubric_sha256": context["rubric_sha256"],
            "reviewer_mode": "fresh_subagent",
            "dimension_scores": {"fidelity": 4},
            "blockers": [],
            "localized_repairs": [],
            "verdict": "pass",
            "complete": True,
        }
        mutations = (
            {"attempt_id": "99"},
            {"review_context_sha256": "0" * 64},
            {"reviewed_frame_ids": []},
            {"complete": False},
            {"dimension_scores": {"unbound_dimension": 4}},
        )
        for mutation in mutations:
            review = dict(base)
            review.update(mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaises(core.ContractError):
                    core.record_semantic_review(self.run, attempt, review)

        (self.run / "attempts" / attempt / "artifact" / "poster.html").write_bytes(b"changed")
        with self.assertRaises(core.IntegrityError):
            core.record_semantic_review(self.run, attempt, base)

    def test_review_context_requires_at_least_one_rendered_preview(self) -> None:
        attempt = self._begin_attempt()
        attempt_root = self.run / "attempts" / attempt
        core.atomic_write_bytes(attempt_root / "artifact" / "poster.html", b"artifact")
        core.record_deterministic_result(
            self.run,
            attempt,
            passed=True,
            checks=[],
            artifact_paths=["artifact/poster.html"],
            preview_paths={},
        )
        with self.assertRaises(core.ContractError):
            core.create_review_context(
                self.run,
                attempt,
                rubric={"format_version": 1, "dimensions": ["fidelity"]},
            )

    def test_resume_recovers_semantic_review_written_before_state_update(self) -> None:
        attempt, _ = self._deterministic_attempt()
        context = core.create_review_context(
            self.run, attempt, rubric={"format_version": 1, "dimensions": ["fidelity"]}
        )
        review = {
            "format_version": 1,
            "attempt_id": attempt,
            "review_context_sha256": context["context_sha256"],
            "artifact_hashes": context["artifact_hashes"],
            "preview_hashes": context["preview_hashes"],
            "reviewed_frame_ids": sorted(context["preview_hashes"]),
            "source_manifest_sha256": context["source_manifest_sha256"],
            "rubric_sha256": context["rubric_sha256"],
            "reviewer_mode": "fresh_subagent",
            "dimension_scores": {"fidelity": 4},
            "blockers": [],
            "localized_repairs": [],
            "verdict": "pass",
            "complete": True,
        }
        with self.assertRaises(core.SimulatedCrash):
            core.record_semantic_review(
                self.run, attempt, review, fail_after_write=True
            )
        status = core.resume_run(self.run)
        self.assertEqual(status["state"], "semantic_passed")
        self.assertEqual(status["next_action"], "finalize")

    def test_finalization_is_staged_non_overwriting_and_recovers_after_rename(self) -> None:
        attempt, _ = self._semantic_attempt()
        with self.assertRaises(core.SimulatedCrash):
            core.finalize_attempt(self.run, attempt, fail_at="after_rename")
        self.assertTrue((self.run / "final" / "delivery-manifest.json").is_file())
        recovered = core.resume_run(self.run)
        self.assertEqual(recovered["state"], "finalized")
        self.assertEqual(recovered["next_action"], "complete")

        manifest_before = (self.run / "final" / "delivery-manifest.json").read_bytes()
        self.assertEqual(core.finalize_attempt(self.run, attempt)["attempt_id"], attempt)
        self.assertEqual(
            (self.run / "final" / "delivery-manifest.json").read_bytes(), manifest_before
        )
        core.atomic_write_json(self.run / "run.json", {**json.loads((self.run / "run.json").read_text()), "state": "semantic_passed"})
        with self.assertRaises(core.IntegrityError):
            core.finalize_attempt(self.run, "02")

    def test_finalization_rejects_symlinked_final_directory(self) -> None:
        attempt, _ = self._semantic_attempt()
        outside = self.root / "outside-final"
        outside.mkdir()
        (self.run / "final").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(core.PathSafetyError):
            core.finalize_attempt(self.run, attempt)

    def test_incomplete_final_staging_is_never_promoted(self) -> None:
        attempt, _ = self._semantic_attempt()
        with self.assertRaises(core.SimulatedCrash):
            core.finalize_attempt(self.run, attempt, fail_at="after_copy")
        self.assertFalse((self.run / "final").exists())
        status = core.resume_run(self.run)
        self.assertEqual(status["next_action"], "finalize")
        self.assertFalse((self.run / "final").exists())

    def test_complete_staging_recovers_after_delivery_manifest_write(self) -> None:
        attempt, _ = self._semantic_attempt()
        with self.assertRaises(core.SimulatedCrash):
            core.finalize_attempt(self.run, attempt, fail_at="after_manifest")
        self.assertFalse((self.run / "final").exists())
        self.assertTrue(
            (self.run / f".final.staging-{attempt}" / "delivery-manifest.json").is_file()
        )
        status = core.resume_run(self.run)
        self.assertEqual(status["state"], "finalized")
        self.assertEqual(status["next_action"], "complete")
        self.assertTrue((self.run / "final" / "poster.html").is_file())

    def test_sync_script_keeps_all_four_vendored_copies_byte_identical(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "sync_agent_skill_core.py")],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        core_bytes = (REPO_ROOT / "agent_skills" / "_shared" / "portable_core.py").read_bytes()
        grounding_bytes = (REPO_ROOT / "agent_skills" / "_shared" / "source-grounding.md").read_bytes()
        for skill_name in SKILLS:
            with self.subTest(skill=skill_name):
                self.assertEqual(
                    (REPO_ROOT / "agent_skills" / skill_name / "scripts" / "_portable.py").read_bytes(),
                    core_bytes,
                )
                self.assertEqual(
                    (REPO_ROOT / "agent_skills" / skill_name / "references" / "source-grounding.md").read_bytes(),
                    grounding_bytes,
                )


if __name__ == "__main__":
    unittest.main()
