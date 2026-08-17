from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from agent_skills._shared import portable_core as core


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(width: int, height: int, value: int) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    rows = b"".join(
        b"\0" + bytes([value, value, value, 255]) * width
        for _ in range(height)
    )
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(rows))
        + _chunk(b"IEND", b"")
    )


class PosterAgentFirstV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skill = self.root / "installed-skill"
        (self.skill / "scripts").mkdir(parents=True)
        (self.skill / "references").mkdir()
        (self.skill / "SKILL.md").write_text(
            "# Fixture skill\n", encoding="utf-8"
        )
        (self.skill / "scripts" / "tool.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        (self.skill / "references" / "grounding.md").write_text(
            "Ground every claim.\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _api(self, name: str):
        value = getattr(core, name, None)
        self.assertTrue(callable(value), f"portable core must expose {name}")
        return value

    def _fake_poppler(self, name: str) -> dict[str, Path]:
        tools_root = self.root / name
        tools_root.mkdir()
        first_page = tools_root / "first-page.png"
        second_page = tools_root / "second-page.png"
        extracted = tools_root / "extracted.png"
        first_page.write_bytes(_png(10, 6, 10))
        second_page.write_bytes(_png(7, 5, 20))
        extracted.write_bytes(_png(2, 2, 30))
        script = f'''#!/usr/bin/env python3
import shutil
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
if name == "pdfinfo":
    print("Pages: 2")
elif name == "pdftotext":
    Path(sys.argv[-1]).write_text("Central method.\\fPrimary result.\\n", encoding="utf-8")
elif name == "pdftoppm":
    shutil.copyfile({str(first_page)!r}, sys.argv[-1] + "-1.png")
    shutil.copyfile({str(second_page)!r}, sys.argv[-1] + "-2.png")
elif name == "pdfimages" and "-list" in sys.argv:
    print("page num type width height color comp bpc enc interp object ID x-ppi y-ppi size ratio")
    print("2 7 image 2 2 rgb 3 8 image no 41 0 72 72 1B 1%")
elif name == "pdfimages":
    shutil.copyfile({str(extracted)!r}, sys.argv[-1] + "-000.png")
'''
        tools: dict[str, Path] = {}
        for tool_name in ("pdftotext", "pdfinfo", "pdftoppm", "pdfimages"):
            executable = tools_root / tool_name
            executable.write_text(script, encoding="utf-8")
            executable.chmod(0o755)
            tools[tool_name] = executable
        return tools

    def _crop_request(
        self,
        inspection: dict[str, object],
        *,
        page: int,
        role: str,
        claim: str,
        bbox: list[float] | None = None,
    ) -> dict[str, object]:
        source = inspection["source"]
        pages = inspection["pages"]
        assert isinstance(source, dict) and isinstance(pages, list)
        page_value = pages[page - 1]
        assert isinstance(page_value, dict)
        return {
            "run_format_version": 2,
            "source_sha256": source["sha256"],
            "page_manifest_sha256": inspection["page_manifest_sha256"],
            "page": page,
            "page_sha256": page_value["sha256"],
            "bbox_normalized": bbox or [0.0, 0.0, 1.0, 1.0],
            "role": role,
            "claim": claim,
            "max_reuse": 1,
        }

    def _passing_source_review(
        self, context: dict[str, object]
    ) -> dict[str, object]:
        return {
            "run_format_version": 2,
            "source_review_context_sha256": context["context_sha256"],
            "reviewer_kind": "fresh_subagent",
            "dimension_scores": {
                "importance": 4,
                "crop_completeness": 4,
                "caption_claim_match": 4,
                "label_axis_legend_readability": 4,
                "duplicate_or_ornamental_content": 4,
                "method_result_coverage": 4,
                "poster_area_fit": 4,
            },
            "asset_findings": [],
            "coverage_findings": [],
            "blockers": [],
            "localized_repairs": [],
            "verdict": "pass",
            "complete": True,
        }

    def _unreviewed_run(
        self, name: str
    ) -> tuple[
        Path,
        dict[str, dict[str, object]],
        dict[str, object],
    ]:
        run = self.root / "runs" / name
        core.initialize_run(
            run,
            self.skill,
            release_version="0.1.0",
            archive_sha256="a" * 64,
            run_format_version=2,
        )
        source = self.root / f"{name}.pdf"
        source.write_bytes(f"%PDF-1.4\n{name}\n".encode())
        core.prepare_source(
            run, source, tool_paths=self._fake_poppler(f"poppler-{name}")
        )
        inspection = core.inspect_source(run)
        method = core.crop_source(
            run,
            self._crop_request(
                inspection,
                page=1,
                role="method",
                claim="The first page contains the central method.",
            ),
        )
        result = core.crop_source(
            run,
            self._crop_request(
                inspection,
                page=2,
                role="result",
                claim="The second page contains the primary result.",
            ),
        )
        selection = {
            "run_format_version": 2,
            "assets": [
                {
                    "asset_id": method["asset_id"],
                    "roles": ["method"],
                    "max_reuse": 1,
                    "importance": "essential",
                },
                {
                    "asset_id": result["asset_id"],
                    "roles": ["result"],
                    "max_reuse": 1,
                    "importance": "essential",
                },
            ],
            "source_story": {
                "central_method": {
                    "status": "covered",
                    "asset_ids": [method["asset_id"]],
                    "evidence_ids": ["ev-001"],
                    "rationale": "The first crop shows the complete central method.",
                },
                "primary_result": {
                    "status": "covered",
                    "asset_ids": [result["asset_id"]],
                    "evidence_ids": ["ev-002"],
                    "rationale": "The second crop shows the complete primary result.",
                },
            },
        }
        return run, {"method": method, "result": result}, selection

    def _reviewed_run(
        self, name: str
    ) -> tuple[Path, dict[str, dict[str, object]]]:
        run, assets, selection = self._unreviewed_run(name)
        context = core.create_source_review_context(run, selection)
        core.record_source_review(
            run, context["context_path"], self._passing_source_review(context)
        )
        return run, assets

    def _poster_plan(
        self,
        assets: dict[str, dict[str, object]],
        *,
        title: str = "Revision-bound poster",
    ) -> dict[str, object]:
        return {
            "format_version": 1,
            "artifact_type": "poster",
            "title": title,
            "visual_allocations": [
                {"visual_id": assets["method"]["asset_id"], "role": "method"},
                {"visual_id": assets["result"]["asset_id"], "role": "result"},
            ],
        }

    def _started_attempt(
        self, name: str
    ) -> tuple[Path, dict[str, dict[str, object]], str]:
        run, assets = self._reviewed_run(name)
        self._api("save_plan_revision")(run, self._poster_plan(assets))
        attempt = core.begin_attempt(run)
        core.write_source_map(
            run,
            attempt,
            [
                {
                    "id": "claim-method",
                    "text": "Central method.",
                    "source_ids": ["ev-001"],
                }
            ],
        )
        return run, assets, attempt

    def _deterministic_attempt(
        self, name: str
    ) -> tuple[
        Path,
        dict[str, dict[str, object]],
        str,
        dict[str, object],
    ]:
        run, assets, attempt = self._started_attempt(name)
        attempt_root = run / "attempts" / attempt
        core.atomic_write_bytes(
            attempt_root / "artifact" / "poster.html",
            b"<main>Central method.</main>\n",
        )
        core.atomic_write_bytes(
            attempt_root / "qa" / "previews" / "poster.png", b"preview"
        )
        core.record_deterministic_result(
            run,
            attempt,
            passed=True,
            checks=[{"id": "poster_contract", "passed": True}],
            artifact_paths=["artifact/poster.html"],
            preview_paths={"poster": "qa/previews/poster.png"},
        )
        context = core.create_review_context(
            run,
            attempt,
            rubric={"format_version": 1, "dimensions": ["fidelity", "legibility"]},
        )
        return run, assets, attempt, context

    def _semantic_review(
        self,
        attempt: str,
        context: dict[str, object],
        *,
        verdict: str,
        repair_route: str | None,
        route_findings: list[dict[str, object]],
        blockers: list[str] | None = None,
    ) -> dict[str, object]:
        return {
            "format_version": 1,
            "attempt_id": attempt,
            "review_context_sha256": context["context_sha256"],
            "artifact_hashes": context["artifact_hashes"],
            "preview_hashes": context["preview_hashes"],
            "reviewed_frame_ids": sorted(context["preview_hashes"]),
            "source_manifest_sha256": context["source_manifest_sha256"],
            "source_map_sha256": context["source_map_sha256"],
            "rubric_sha256": context["rubric_sha256"],
            "reviewer_mode": "fresh_subagent",
            "dimension_scores": {"fidelity": 4, "legibility": 4},
            "blockers": blockers or [],
            "localized_repairs": [],
            "repair_route": repair_route,
            "route_findings": route_findings,
            "verdict": verdict,
            "complete": True,
        }

    def _route_finding(
        self,
        finding_id: str,
        minimum_route: str,
        *,
        code: str = "poster-owned-code",
        block_id: str = "results",
    ) -> dict[str, object]:
        return {
            "finding_id": finding_id,
            "code": code,
            "minimum_route": minimum_route,
            "block_id": block_id,
            "message": f"{finding_id} requires {minimum_route}.",
        }

    def _record_valid_v2_review(
        self,
        run: Path,
        attempt: str,
        review: dict[str, object],
    ) -> dict[str, object]:
        try:
            return core.record_semantic_review(run, attempt, review)
        except core.ContractError as error:
            self.fail(f"valid v2 semantic review was rejected: {error}")

    def _reopen_request(
        self,
        run: Path,
        attempt: str,
        route: str,
        finding_ids: list[str],
    ) -> dict[str, object]:
        state = json.loads((run / "run.json").read_text(encoding="utf-8"))
        return {
            "run_format_version": 2,
            "attempt_id": attempt,
            "semantic_review_sha256": core.sha256_file(
                run / "attempts" / attempt / "qa" / "semantic-review.json"
            ),
            "repair_route": route,
            "reason": f"Repair {route} findings before the next attempt.",
            "finding_ids": finding_ids,
            "expected_curation_revision": state["active_curation_revision"],
            "expected_plan_revision": state["active_plan_revision"],
        }

    def _ledger_entries(self, run: Path) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in (run / "provenance" / "supersessions.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]

    def _canonical_hash(self, value: object) -> str:
        data = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def test_plan_revision_is_immutable_catalog_bound_and_idempotent(self) -> None:
        run, assets = self._reviewed_run("plan-revision")
        save_plan_revision = self._api("save_plan_revision")
        load_active_plan = self._api("load_active_plan")
        plan = self._poster_plan(assets)

        saved = save_plan_revision(run, plan)
        replayed = save_plan_revision(run, json.loads(json.dumps(plan)))

        self.assertEqual(replayed, saved)
        self.assertEqual(load_active_plan(run), plan)
        revision = run / "plans" / "001"
        self.assertEqual(
            {path.name for path in revision.iterdir()},
            {"plan.json", "manifest.json", "COMMIT.json"},
        )
        manifest = json.loads((revision / "manifest.json").read_text(encoding="utf-8"))
        state = json.loads((run / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (
                manifest["catalog_revision"],
                manifest["catalog_sha256"],
                manifest["plan_sha256"],
                state["state"],
                state["active_plan_revision"],
                state["active_plan_sha256"],
                state["attempt_count"],
            ),
            (
                1,
                state["active_curation_sha256"],
                core.sha256_file(revision / "plan.json"),
                "planned",
                1,
                core.sha256_file(revision / "plan.json"),
                0,
            ),
        )
        with self.assertRaises(core.StateError):
            save_plan_revision(run, self._poster_plan(assets, title="Changed"))
        self.assertFalse((run / "plans" / "002").exists())

    def test_attempt_snapshots_exact_source_catalog_plan_and_authorized_assets(self) -> None:
        run, assets = self._reviewed_run("attempt-binding")
        self._api("save_plan_revision")(run, self._poster_plan(assets))
        attempt = core.begin_attempt(run)

        self.assertEqual(attempt, "01")
        attempt_root = run / "attempts" / attempt
        context = json.loads(
            (attempt_root / "attempt-context.json").read_text(encoding="utf-8")
        )
        catalog = json.loads(
            (attempt_root / "catalog-snapshot.json").read_text(encoding="utf-8")
        )
        plan = json.loads(
            (attempt_root / "plan-snapshot.json").read_text(encoding="utf-8")
        )
        expected_assets = {
            item["asset_id"]: item["sha256"] for item in catalog["assets"]
        }
        self.assertEqual(
            context,
            {
                "run_format_version": 2,
                "attempt_id": "01",
                "source_manifest_sha256": core.sha256_file(
                    run / "evidence" / "source_manifest.json"
                ),
                "catalog_revision": 1,
                "catalog_sha256": core.sha256_file(
                    run / "curations" / "001" / "catalog.json"
                ),
                "plan_revision": 1,
                "plan_sha256": core.sha256_file(
                    run / "plans" / "001" / "plan.json"
                ),
                "authorized_assets": [
                    {
                        "asset_id": item["visual_id"],
                        "sha256": expected_assets[item["visual_id"]],
                    }
                    for item in plan["visual_allocations"]
                ],
                "parent_attempt": None,
                "supersession_ledger": {
                    "path": "provenance/supersessions.jsonl",
                    "sha256": core.sha256_bytes(b""),
                    "size": 0,
                    "entry_count": 0,
                },
            },
        )
        self.assertEqual(self._api("load_attempt_plan")(run, attempt), plan)
        self.assertEqual(
            self._api("load_attempt_visual_catalog")(run, attempt), catalog
        )

    def test_semantic_review_enforces_generic_repair_route_order(self) -> None:
        run, _assets, attempt, context = self._deterministic_attempt(
            "route-order"
        )
        findings = [
            self._route_finding("layout", "layout_repair"),
            self._route_finding("source", "source_reingest"),
        ]
        downgraded = self._semantic_review(
            attempt,
            context,
            verdict="fail",
            repair_route="content_replan",
            route_findings=findings,
        )
        before = (run / "run.json").read_bytes()
        with self.assertRaises(core.ContractError):
            core.record_semantic_review(run, attempt, downgraded)
        self.assertEqual((run / "run.json").read_bytes(), before)
        self.assertFalse(
            (run / "attempts" / attempt / "qa" / "semantic-review.json").exists()
        )

        escalated = self._semantic_review(
            attempt,
            context,
            verdict="fail",
            repair_route="source_reingest",
            route_findings=findings,
        )
        stored = self._record_valid_v2_review(run, attempt, escalated)
        self.assertEqual(stored["repair_route"], "source_reingest")
        state = json.loads((run / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (state["state"], state["failure_origin"], state["repair_route"]),
            ("failed", "semantic_review", "source_reingest"),
        )

    def test_semantic_review_requires_exact_findings_and_coherent_verdict(self) -> None:
        invalid_mutations = (
            {
                "verdict": "pass",
                "repair_route": "layout_repair",
                "route_findings": [
                    self._route_finding("unexpected-pass-route", "layout_repair")
                ],
            },
            {"verdict": "fail", "repair_route": None, "route_findings": []},
            {
                "verdict": "fail",
                "repair_route": "layout_repair",
                "route_findings": [],
            },
            {
                "verdict": "fail",
                "repair_route": "layout_repair",
                "route_findings": [
                    {
                        **self._route_finding("extra", "layout_repair"),
                        "unexpected": True,
                    }
                ],
            },
            {
                "verdict": "fail",
                "repair_route": "layout_repair",
                "route_findings": [
                    self._route_finding("duplicate", "layout_repair"),
                    self._route_finding("duplicate", "layout_repair"),
                ],
            },
        )
        for index, mutation in enumerate(invalid_mutations):
            with self.subTest(index=index):
                run, _assets, attempt, context = self._deterministic_attempt(
                    f"route-schema-{index}"
                )
                review = self._semantic_review(
                    attempt,
                    context,
                    verdict=str(mutation["verdict"]),
                    repair_route=mutation["repair_route"],
                    route_findings=mutation["route_findings"],
                )
                with self.assertRaises(core.ContractError):
                    core.record_semantic_review(run, attempt, review)

        run, _assets, attempt, context = self._deterministic_attempt("route-pass")
        passed = self._semantic_review(
            attempt,
            context,
            verdict="pass",
            repair_route=None,
            route_findings=[],
        )
        self.assertEqual(
            self._record_valid_v2_review(run, attempt, passed)["verdict"], "pass"
        )

    def test_layout_failure_starts_a_new_attempt_on_the_same_revisions(self) -> None:
        run, _assets, attempt, context = self._deterministic_attempt("layout-route")
        review = self._semantic_review(
            attempt,
            context,
            verdict="fail",
            repair_route="layout_repair",
            route_findings=[self._route_finding("layout", "layout_repair")],
        )
        self._record_valid_v2_review(run, attempt, review)
        self.assertEqual(
            core.resume_run(run, skill_root=self.skill)["next_action"], "author"
        )

        repaired = core.begin_attempt(run)
        first = json.loads(
            (run / "attempts" / "01" / "attempt-context.json").read_text()
        )
        second = json.loads(
            (run / "attempts" / repaired / "attempt-context.json").read_text()
        )
        self.assertEqual(repaired, "02")
        self.assertEqual(second["parent_attempt"], "01")
        self.assertEqual(
            (second["catalog_revision"], second["plan_revision"]),
            (first["catalog_revision"], first["plan_revision"]),
        )

    def test_runtime_failure_retries_the_current_attempt_without_consuming_budget(self) -> None:
        run, _assets, attempt = self._started_attempt("runtime-retry")
        core.mark_side_state(run, "failed", reason="browser startup failed")

        status = core.resume_run(run, skill_root=self.skill)
        self.assertEqual(status["next_action"], "retry_current_attempt")
        self.assertEqual(core.begin_attempt(run), attempt)
        state = json.loads((run / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (state["state"], state["active_attempt"], state["attempt_count"]),
            ("authoring", "01", 1),
        )

    def test_content_replan_keeps_catalog_and_requires_a_new_plan_revision(self) -> None:
        run, assets, attempt, context = self._deterministic_attempt(
            "content-replan"
        )
        review = self._semantic_review(
            attempt,
            context,
            verdict="fail",
            repair_route="content_replan",
            route_findings=[
                self._route_finding(
                    "narrative", "content_replan", code="narrative_hierarchy"
                )
            ],
        )
        self._record_valid_v2_review(run, attempt, review)
        reopen_curation = self._api("reopen_curation")
        request = self._reopen_request(
            run, attempt, "content_replan", ["narrative"]
        )

        reopened = reopen_curation(run, request)

        self.assertEqual(
            (reopened["state"], reopened["next_action"]), ("curated", "plan")
        )
        state = json.loads((run / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (
                state["active_curation_revision"],
                state["active_plan_revision"],
                state["attempt_count"],
            ),
            (1, 1, 1),
        )
        entries = self._ledger_entries(run)
        self.assertEqual(len(entries), 1)
        unsigned = dict(entries[0])
        entry_hash = unsigned.pop("entry_sha256")
        self.assertIsNone(unsigned["previous_entry_sha256"])
        self.assertEqual(entry_hash, self._canonical_hash(unsigned))
        self.assertEqual(
            core.resume_run(run, skill_root=self.skill)["next_action"], "plan"
        )

        revised = self._poster_plan(assets, title="Replanned narrative")
        saved = core.save_plan_revision(run, revised)
        state_before_attempt = json.loads(
            (run / "run.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            (saved["plan_revision"], state_before_attempt["attempt_count"]),
            (2, 1),
        )
        second = core.begin_attempt(run)
        self.assertEqual(second, "02")
        self.assertEqual(core.load_attempt_plan(run, "01")["title"], "Revision-bound poster")
        self.assertEqual(core.load_attempt_plan(run, "02"), revised)
        first_context = json.loads(
            (run / "attempts" / "01" / "attempt-context.json").read_text()
        )
        second_context = json.loads(
            (run / "attempts" / "02" / "attempt-context.json").read_text()
        )
        self.assertEqual(
            (
                first_context["catalog_revision"],
                first_context["plan_revision"],
                second_context["catalog_revision"],
                second_context["plan_revision"],
                second_context["parent_attempt"],
            ),
            (1, 1, 1, 2, "01"),
        )

    def test_source_reingest_requires_a_new_catalog_and_plan(self) -> None:
        run, assets, attempt, context = self._deterministic_attempt(
            "source-reingest"
        )
        review = self._semantic_review(
            attempt,
            context,
            verdict="fail",
            repair_route="source_reingest",
            route_findings=[
                self._route_finding(
                    "missing-key-visual",
                    "source_reingest",
                    code="key_visual_missing",
                    block_id="method",
                )
            ],
        )
        self._record_valid_v2_review(run, attempt, review)
        request = self._reopen_request(
            run, attempt, "source_reingest", ["missing-key-visual"]
        )
        reopened = self._api("reopen_curation")(run, request)
        self.assertEqual(
            (reopened["state"], reopened["next_action"]),
            ("curating", "curate_source"),
        )

        inspection = core.inspect_source(run)
        replacement = core.crop_source(
            run,
            self._crop_request(
                inspection,
                page=1,
                role="method",
                claim="A tighter complete crop replaces the missing key visual.",
                bbox=[0.0, 0.0, 0.9, 1.0],
            ),
        )
        selection = {
            "run_format_version": 2,
            "assets": [
                {
                    "asset_id": replacement["asset_id"],
                    "roles": ["method"],
                    "max_reuse": 1,
                    "importance": "essential",
                },
                {
                    "asset_id": assets["result"]["asset_id"],
                    "roles": ["result"],
                    "max_reuse": 1,
                    "importance": "essential",
                },
            ],
            "source_story": {
                "central_method": {
                    "status": "covered",
                    "asset_ids": [replacement["asset_id"]],
                    "evidence_ids": ["ev-001"],
                    "rationale": "The replacement crop covers the complete central method.",
                },
                "primary_result": {
                    "status": "covered",
                    "asset_ids": [assets["result"]["asset_id"]],
                    "evidence_ids": ["ev-002"],
                    "rationale": "The reviewed result crop remains the primary evidence.",
                },
            },
        }
        source_context = core.create_source_review_context(run, selection)
        core.record_source_review(
            run,
            source_context["context_path"],
            self._passing_source_review(source_context),
        )
        state_after_curation = json.loads(
            (run / "run.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            (
                state_after_curation["active_curation_revision"],
                state_after_curation["active_plan_revision"],
                state_after_curation["attempt_count"],
            ),
            (2, 1, 1),
        )
        revised_assets = {"method": replacement, "result": assets["result"]}
        core.save_plan_revision(
            run, self._poster_plan(revised_assets, title="Reingested source")
        )
        second = core.begin_attempt(run)
        self.assertEqual(second, "02")
        first_catalog = core.load_attempt_visual_catalog(run, "01")
        second_catalog = core.load_attempt_visual_catalog(run, "02")
        self.assertNotIn(
            replacement["asset_id"],
            {item["asset_id"] for item in first_catalog["assets"]},
        )
        self.assertIn(
            replacement["asset_id"],
            {item["asset_id"] for item in second_catalog["assets"]},
        )
        second_context = json.loads(
            (run / "attempts" / second / "attempt-context.json").read_text()
        )
        self.assertEqual(
            (
                second_context["catalog_revision"],
                second_context["plan_revision"],
                second_context["parent_attempt"],
            ),
            (2, 2, "01"),
        )

    def test_plan_and_attempt_transactions_recover_every_crash_boundary(self) -> None:
        plan_boundaries = (
            "after_plan_staging_write",
            "after_plan_promotion",
            "after_plan_pointer_write",
            "after_plan_event_write",
        )
        for boundary in plan_boundaries:
            with self.subTest(family="plan", boundary=boundary):
                run, assets = self._reviewed_run(f"plan-crash-{boundary}")
                plan = self._poster_plan(assets)
                with self.assertRaises(core.SimulatedCrash):
                    core.save_plan_revision(run, plan, fail_at=boundary)

                status = core.resume_run(run, skill_root=self.skill)

                self.assertEqual(
                    (status["state"], status["next_action"]),
                    ("planned", "author"),
                )
                self.assertEqual(core.load_active_plan(run), plan)
                self.assertFalse(any((run / "plans").glob(".plan-staging-*")))
                events = [
                    json.loads(line)
                    for line in (run / "events.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertEqual(
                    sum(
                        event.get("event") == "plan_revision_committed"
                        for event in events
                    ),
                    1,
                )

        attempt_boundaries = (
            "after_attempt_staging_write",
            "after_attempt_promotion",
            "after_attempt_pointer_write",
            "after_attempt_event_write",
        )
        for boundary in attempt_boundaries:
            with self.subTest(family="attempt", boundary=boundary):
                run, assets = self._reviewed_run(f"attempt-crash-{boundary}")
                core.save_plan_revision(run, self._poster_plan(assets))
                with self.assertRaises(core.SimulatedCrash):
                    core.begin_attempt(run, fail_at=boundary)

                status = core.resume_run(run, skill_root=self.skill)

                self.assertEqual(
                    (
                        status["state"],
                        status["active_attempt"],
                        status["attempt_count"],
                        status["next_action"],
                    ),
                    ("authoring", "01", 1, "author"),
                )
                self.assertFalse(
                    any((run / "attempts").glob(".attempt-staging-*"))
                )
                events = [
                    json.loads(line)
                    for line in (run / "events.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertEqual(
                    sum(
                        event.get("event") == "attempt_started"
                        and event.get("attempt_id") == "01"
                        for event in events
                    ),
                    1,
                )

    def test_review_and_reopen_transactions_recover_without_duplicates(self) -> None:
        run, _assets, attempt, context = self._deterministic_attempt(
            "semantic-review-crash"
        )
        review = self._semantic_review(
            attempt,
            context,
            verdict="fail",
            repair_route="content_replan",
            route_findings=[
                self._route_finding("content", "content_replan")
            ],
        )
        with self.assertRaises(core.SimulatedCrash):
            core.record_semantic_review(
                run, attempt, review, fail_after_write=True
            )
        recovered = core.resume_run(run, skill_root=self.skill)
        self.assertEqual(
            (recovered["state"], recovered["next_action"]),
            ("failed", "reopen_curation"),
        )
        recovered_state = json.loads(
            (run / "run.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            (
                recovered_state["failure_origin"],
                recovered_state["repair_route"],
                recovered_state["semantic_review_sha256"],
            ),
            (
                "semantic_review",
                "content_replan",
                core.sha256_file(
                    run
                    / "attempts"
                    / attempt
                    / "qa"
                    / "semantic-review.json"
                ),
            ),
        )

        boundaries = (
            "after_supersession_append",
            "after_reopen_pointer_write",
            "after_reopen_event_write",
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                run, _assets, attempt, context = self._deterministic_attempt(
                    f"reopen-crash-{boundary}"
                )
                review = self._semantic_review(
                    attempt,
                    context,
                    verdict="fail",
                    repair_route="content_replan",
                    route_findings=[
                        self._route_finding("content", "content_replan")
                    ],
                )
                self._record_valid_v2_review(run, attempt, review)
                request = self._reopen_request(
                    run, attempt, "content_replan", ["content"]
                )
                with self.assertRaises(core.SimulatedCrash):
                    core.reopen_curation(run, request, fail_at=boundary)

                status = core.resume_run(run, skill_root=self.skill)

                self.assertEqual(
                    (status["state"], status["next_action"]),
                    ("curated", "plan"),
                )
                self.assertEqual(len(self._ledger_entries(run)), 1)
                events = [
                    json.loads(line)
                    for line in (run / "events.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertEqual(
                    sum(
                        event.get("event") == "curation_reopened"
                        for event in events
                    ),
                    1,
                )

    def test_resume_and_finalize_reject_persisted_v2_review_state_mismatch(
        self,
    ) -> None:
        run, _assets, attempt, context = self._deterministic_attempt(
            "persisted-pass-review-state-mismatch"
        )
        passing = self._semantic_review(
            attempt,
            context,
            verdict="pass",
            repair_route=None,
            route_findings=[],
        )
        self._record_valid_v2_review(run, attempt, passing)
        conflicting_failure = self._semantic_review(
            attempt,
            context,
            verdict="fail",
            repair_route="content_replan",
            route_findings=[
                self._route_finding("content", "content_replan")
            ],
        )
        core.atomic_write_json(
            run / "attempts" / attempt / "qa" / "semantic-review.json",
            conflicting_failure,
        )

        with self.assertRaises(core.IntegrityError):
            core.resume_run(run, skill_root=self.skill)
        with self.assertRaises(core.IntegrityError):
            core.finalize_attempt(run, attempt)

        run, _assets, attempt, context = self._deterministic_attempt(
            "persisted-fail-review-state-mismatch"
        )
        content_failure = self._semantic_review(
            attempt,
            context,
            verdict="fail",
            repair_route="content_replan",
            route_findings=[
                self._route_finding("content", "content_replan")
            ],
        )
        self._record_valid_v2_review(run, attempt, content_failure)
        conflicting_route = self._semantic_review(
            attempt,
            context,
            verdict="fail",
            repair_route="source_reingest",
            route_findings=[
                self._route_finding("content", "content_replan")
            ],
        )
        core.atomic_write_json(
            run / "attempts" / attempt / "qa" / "semantic-review.json",
            conflicting_route,
        )

        with self.assertRaises(core.IntegrityError):
            core.resume_run(run, skill_root=self.skill)

    def test_reopen_request_binds_persisted_review_route_findings_and_hash(
        self,
    ) -> None:
        for mutation, error_type in (
            ("hash", core.IntegrityError),
            ("route", core.ContractError),
            ("findings", core.ContractError),
        ):
            with self.subTest(mutation=mutation):
                run, _assets, attempt, context = self._deterministic_attempt(
                    f"reopen-binding-{mutation}"
                )
                review = self._semantic_review(
                    attempt,
                    context,
                    verdict="fail",
                    repair_route="content_replan",
                    route_findings=[
                        self._route_finding("content", "content_replan")
                    ],
                )
                self._record_valid_v2_review(run, attempt, review)
                request = self._reopen_request(
                    run, attempt, "content_replan", ["content"]
                )
                if mutation == "hash":
                    request["semantic_review_sha256"] = "0" * 64
                elif mutation == "route":
                    request["repair_route"] = "source_reingest"
                else:
                    request["finding_ids"] = ["different-finding"]
                state_before = (run / "run.json").read_bytes()
                ledger_before = (
                    run / "provenance" / "supersessions.jsonl"
                ).read_bytes()
                events_before = (run / "events.jsonl").read_bytes()

                with self.assertRaises(error_type):
                    core.reopen_curation(run, request)

                self.assertEqual((run / "run.json").read_bytes(), state_before)
                self.assertEqual(
                    (run / "provenance" / "supersessions.jsonl").read_bytes(),
                    ledger_before,
                )
                self.assertEqual(
                    (run / "events.jsonl").read_bytes(), events_before
                )

    def test_resume_discards_incomplete_staging_and_rejects_conflicts(self) -> None:
        run, assets = self._reviewed_run("discard-plan-stage")
        plan = self._poster_plan(assets)
        with self.assertRaises(core.SimulatedCrash):
            core.save_plan_revision(
                run, plan, fail_at="after_plan_staging_write"
            )
        stage = next((run / "plans").glob(".plan-staging-*"))
        (stage / "COMMIT.json").unlink()

        status = core.resume_run(run, skill_root=self.skill)

        self.assertEqual(
            (status["state"], status["next_action"]), ("curated", "plan")
        )
        self.assertFalse(stage.exists())

        run, assets = self._reviewed_run("conflicting-plan-stage")
        with self.assertRaises(core.SimulatedCrash):
            core.save_plan_revision(
                run,
                self._poster_plan(assets),
                fail_at="after_plan_staging_write",
            )
        stage = next((run / "plans").glob(".plan-staging-*"))
        (stage / "unexpected.bin").write_bytes(b"conflict")
        with self.assertRaises(core.IntegrityError):
            core.resume_run(run, skill_root=self.skill)

        run, assets = self._reviewed_run("multiple-plan-orphans")
        with self.assertRaises(core.SimulatedCrash):
            core.save_plan_revision(
                run, self._poster_plan(assets), fail_at="after_plan_promotion"
            )
        import shutil

        shutil.copytree(run / "plans" / "001", run / "plans" / "002")
        with self.assertRaises(core.IntegrityError):
            core.resume_run(run, skill_root=self.skill)

    def test_supersession_prefix_rejects_truncation_rewrite_and_parent_mismatch(self) -> None:
        for mutation in ("truncate", "rewrite", "parent_mismatch"):
            with self.subTest(mutation=mutation):
                run, assets, attempt, context = self._deterministic_attempt(
                    f"ledger-{mutation}"
                )
                review = self._semantic_review(
                    attempt,
                    context,
                    verdict="fail",
                    repair_route="content_replan",
                    route_findings=[
                        self._route_finding("content", "content_replan")
                    ],
                )
                self._record_valid_v2_review(run, attempt, review)
                request = self._reopen_request(
                    run, attempt, "content_replan", ["content"]
                )
                core.reopen_curation(run, request)
                core.save_plan_revision(
                    run, self._poster_plan(assets, title="Ledger-bound replan")
                )
                core.begin_attempt(run)
                ledger = run / "provenance" / "supersessions.jsonl"
                data = ledger.read_bytes()
                if mutation == "truncate":
                    ledger.write_bytes(data[:-1])
                else:
                    entry = self._ledger_entries(run)[0]
                    if mutation == "rewrite":
                        entry["reason"] = "Rewritten ordinary retry reason."
                    else:
                        entry["previous_entry_sha256"] = "0" * 64
                    unsigned = dict(entry)
                    unsigned.pop("entry_sha256")
                    entry["entry_sha256"] = self._canonical_hash(unsigned)
                    ledger.write_text(
                        json.dumps(
                            entry,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n",
                        encoding="utf-8",
                    )

                with self.assertRaises(core.IntegrityError):
                    core.load_attempt_plan(run, "02")
                with self.assertRaises(core.IntegrityError):
                    core.resume_run(run, skill_root=self.skill)

    def test_resume_v2_emits_the_complete_stable_action_vocabulary(self) -> None:
        observed: set[str] = set()

        prepare_run = self.root / "runs" / "resume-prepare"
        core.initialize_run(
            prepare_run,
            self.skill,
            release_version="0.1.0",
            archive_sha256="a" * 64,
            run_format_version=2,
        )
        observed.add(
            core.resume_run(prepare_run, skill_root=self.skill)["next_action"]
        )

        inspect_run = self.root / "runs" / "resume-inspect"
        core.initialize_run(
            inspect_run,
            self.skill,
            release_version="0.1.0",
            archive_sha256="a" * 64,
            run_format_version=2,
        )
        source = self.root / "resume-inspect.pdf"
        source.write_bytes(b"%PDF-1.4\nresume inspect\n")
        core.prepare_source(
            inspect_run,
            source,
            tool_paths=self._fake_poppler("poppler-resume-inspect"),
        )
        observed.add(
            core.resume_run(inspect_run, skill_root=self.skill)["next_action"]
        )

        run, assets, selection = self._unreviewed_run("resume-main")
        observed.add(core.resume_run(run, skill_root=self.skill)["next_action"])
        source_context = core.create_source_review_context(run, selection)
        observed.add(core.resume_run(run, skill_root=self.skill)["next_action"])
        core.record_source_review(
            run,
            source_context["context_path"],
            self._passing_source_review(source_context),
        )
        observed.add(core.resume_run(run, skill_root=self.skill)["next_action"])
        core.save_plan_revision(run, self._poster_plan(assets))
        observed.add(core.resume_run(run, skill_root=self.skill)["next_action"])
        attempt = core.begin_attempt(run)
        core.write_source_map(
            run,
            attempt,
            [
                {
                    "id": "claim-method",
                    "text": "Central method.",
                    "source_ids": ["ev-001"],
                }
            ],
        )
        attempt_root = run / "attempts" / attempt
        core.atomic_write_bytes(
            attempt_root / "artifact" / "poster.html", b"<main>Poster</main>\n"
        )
        observed.add(core.resume_run(run, skill_root=self.skill)["next_action"])
        core.atomic_write_json(
            attempt_root / "qa" / "dom-audit.json", {"passed": True}
        )
        observed.add(core.resume_run(run, skill_root=self.skill)["next_action"])
        core.atomic_write_bytes(
            attempt_root / "qa" / "previews" / "poster.png", b"preview"
        )
        core.record_deterministic_result(
            run,
            attempt,
            passed=True,
            checks=[{"id": "poster_contract", "passed": True}],
            artifact_paths=["artifact/poster.html"],
            preview_paths={"poster": "qa/previews/poster.png"},
        )
        observed.add(core.resume_run(run, skill_root=self.skill)["next_action"])
        review_context = core.create_review_context(
            run,
            attempt,
            rubric={"format_version": 1, "dimensions": ["fidelity", "legibility"]},
        )
        passed = self._semantic_review(
            attempt,
            review_context,
            verdict="pass",
            repair_route=None,
            route_findings=[],
        )
        self._record_valid_v2_review(run, attempt, passed)
        observed.add(core.resume_run(run, skill_root=self.skill)["next_action"])
        core.finalize_attempt(run, attempt)
        observed.add(core.resume_run(run, skill_root=self.skill)["next_action"])

        runtime_run, _runtime_assets, _runtime_attempt = self._started_attempt(
            "resume-runtime"
        )
        core.mark_side_state(
            runtime_run, "failed", reason="export runtime unavailable"
        )
        observed.add(
            core.resume_run(runtime_run, skill_root=self.skill)["next_action"]
        )

        reopen_run, _assets, reopen_attempt, reopen_context = (
            self._deterministic_attempt("resume-reopen")
        )
        failed = self._semantic_review(
            reopen_attempt,
            reopen_context,
            verdict="fail",
            repair_route="content_replan",
            route_findings=[
                self._route_finding("content", "content_replan")
            ],
        )
        self._record_valid_v2_review(reopen_run, reopen_attempt, failed)
        observed.add(
            core.resume_run(reopen_run, skill_root=self.skill)["next_action"]
        )

        blocked_run = self.root / "runs" / "resume-blocked"
        core.initialize_run(
            blocked_run,
            self.skill,
            release_version="0.1.0",
            archive_sha256="a" * 64,
            run_format_version=2,
        )
        core.mark_side_state(blocked_run, "blocked", reason="missing Poppler")
        observed.add(
            core.resume_run(blocked_run, skill_root=self.skill)["next_action"]
        )

        self.assertEqual(
            observed,
            {
                "prepare_source",
                "inspect_source",
                "curate_source",
                "source_review",
                "plan",
                "author",
                "retry_current_attempt",
                "dom_audit",
                "validate",
                "semantic_review",
                "reopen_curation",
                "finalize",
                "complete",
                "resolve_blocker",
            },
        )


if __name__ == "__main__":
    unittest.main()
