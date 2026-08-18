from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from agent_skills._shared import portable_core as core
from tests import test_autodesign_poster_skill as poster_skill_fixtures


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
POSTER_SOURCE_GUIDE = (
    Path(__file__).resolve().parents[1]
    / "agent_skills"
    / "autodesign-poster"
    / "references"
    / "agent-first-source.md"
)
POSTER_REVIEW_GUIDE = POSTER_SOURCE_GUIDE.with_name("review-rubric.md")


def _json_example_after_heading(document: str, heading: str) -> tuple[dict[str, object], str]:
    section = document.split(heading, 1)[1]
    fenced = section.split("```json\n", 1)[1].split("```", 1)[0]
    value = json.loads(fenced)
    if not isinstance(value, dict):
        raise AssertionError(f"{heading} must contain one JSON object")
    return value, fenced


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

    def test_documented_source_and_plan_examples_match_closed_v2_schemas(self) -> None:
        document = POSTER_SOURCE_GUIDE.read_text(encoding="utf-8")
        crop, crop_bytes = _json_example_after_heading(
            document, "## Exact crop request"
        )
        selection, selection_bytes = _json_example_after_heading(
            document, "## Exact source selection"
        )
        review, review_bytes = _json_example_after_heading(
            document, "## Exact source review"
        )
        plan, plan_bytes = _json_example_after_heading(
            document, "## Exact immutable plan"
        )
        harness = poster_skill_fixtures._load_harness()
        for index, (value, encoded) in enumerate((
            (crop, crop_bytes),
            (selection, selection_bytes),
            (review, review_bytes),
            (plan, plan_bytes),
        )):
            self.assertEqual(
                encoded,
                harness.core._stored_json_bytes(value).decode("utf-8"),
            )
            contract_path = self.root / f"documented-source-contract-{index}.json"
            contract_path.write_text(encoded, encoding="utf-8")
            self.assertEqual(harness._read_canonical_json_object(contract_path), value)

        self.assertEqual(
            set(crop),
            {
                "run_format_version",
                "source_sha256",
                "page_manifest_sha256",
                "page",
                "page_sha256",
                "bbox_normalized",
                "role",
                "claim",
                "max_reuse",
            },
        )
        self.assertEqual(set(selection), {"run_format_version", "assets", "source_story"})
        self.assertEqual(
            {key for item in selection["assets"] for key in item},
            {"asset_id", "roles", "max_reuse", "importance"},
        )
        self.assertEqual(
            set(review),
            {
                "run_format_version",
                "source_review_context_sha256",
                "reviewer_kind",
                "dimension_scores",
                "asset_findings",
                "coverage_findings",
                "blockers",
                "localized_repairs",
                "verdict",
                "complete",
            },
        )
        self.assertEqual(harness.normalize_plan(plan), plan)
        reuse_by_asset = {
            item["asset_id"]: item["max_reuse"] for item in selection["assets"]
        }
        for asset_id, maximum in reuse_by_asset.items():
            self.assertLessEqual(
                sum(
                    allocation["visual_id"] == asset_id
                    for allocation in plan["visual_allocations"]
                ),
                maximum,
            )

    def test_documented_repair_table_matches_poster_policy_order_exactly(self) -> None:
        harness = poster_skill_fixtures._load_harness()
        document = POSTER_REVIEW_GUIDE.read_text(encoding="utf-8")
        section = document.split("## Exact repair-route table", 1)[1].split("\n## ", 1)[0]
        rows: list[tuple[str, str]] = []
        for line in section.splitlines():
            fields = [field.strip().strip("`") for field in line.strip().split("|")]
            if len(fields) == 4 and fields[1] in harness.POSTER_FINDING_MINIMUM_ROUTE:
                rows.append((fields[1], fields[2]))
        self.assertEqual(rows, list(harness.POSTER_FINDING_MINIMUM_ROUTE.items()))
        self.assertIn(
            "`layout_repair < content_replan < source_reingest`",
            document,
        )

    def test_documented_artifact_review_and_reopen_examples_are_canonical(self) -> None:
        document = POSTER_REVIEW_GUIDE.read_text(encoding="utf-8")
        review, review_bytes = _json_example_after_heading(
            document, "## Exact artifact-review schema"
        )
        reopen, reopen_bytes = _json_example_after_heading(
            document, "## Exact reopen request"
        )
        harness = poster_skill_fixtures._load_harness()
        for index, (value, encoded) in enumerate(
            ((review, review_bytes), (reopen, reopen_bytes))
        ):
            self.assertEqual(
                encoded,
                harness.core._stored_json_bytes(value).decode("utf-8"),
            )
            contract_path = self.root / f"documented-review-contract-{index}.json"
            contract_path.write_text(encoded, encoding="utf-8")
            self.assertEqual(harness._read_canonical_json_object(contract_path), value)
        self.assertEqual(
            set(review),
            {
                "format_version",
                "attempt_id",
                "review_context_sha256",
                "artifact_hashes",
                "preview_hashes",
                "reviewed_frame_ids",
                "source_manifest_sha256",
                "rubric_sha256",
                "source_map_sha256",
                "reviewer_mode",
                "dimension_scores",
                "blockers",
                "localized_repairs",
                "repair_route",
                "route_findings",
                "verdict",
                "complete",
            },
        )
        self.assertEqual(
            set(reopen),
            {
                "run_format_version",
                "attempt_id",
                "semantic_review_sha256",
                "repair_route",
                "reason",
                "finding_ids",
                "expected_curation_revision",
                "expected_plan_revision",
            },
        )

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
    Path(sys.argv[-1]).write_text(
        "Central method. Grounded poster source reports 85% accuracy and uses two-stage routing."
        "\\fPrimary result. Accuracy reaches 85%. The grounded poster retains accuracy.\\n",
        encoding="utf-8",
    )
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

    def _two_attempt_event_fixture(self, name: str) -> Path:
        run, assets, first, context = self._deterministic_attempt(name)
        failure = self._semantic_review(
            first,
            context,
            verdict="fail",
            repair_route="content_replan",
            route_findings=[
                self._route_finding("content", "content_replan")
            ],
        )
        self._record_valid_v2_review(run, first, failure)
        core.reopen_curation(
            run,
            self._reopen_request(
                run, first, "content_replan", ["content"]
            ),
        )
        core.save_plan_revision(
            run,
            self._poster_plan(assets, title="Replanned event fixture"),
        )
        second = core.begin_attempt(run)
        self.assertEqual(second, "02")
        core.write_source_map(
            run,
            second,
            [
                {
                    "id": "claim-method",
                    "text": "Central method.",
                    "source_ids": ["ev-001"],
                }
            ],
        )
        second_root = run / "attempts" / second
        core.atomic_write_bytes(
            second_root / "artifact" / "poster.html",
            b"<main>Central method.</main>\n",
        )
        core.atomic_write_bytes(
            second_root / "qa" / "previews" / "poster.png",
            b"preview",
        )
        core.record_deterministic_result(
            run,
            second,
            passed=True,
            checks=[{"id": "poster_contract", "passed": True}],
            artifact_paths=["artifact/poster.html"],
            preview_paths={"poster": "qa/previews/poster.png"},
        )
        return run

    def _two_attempt_source_reingest_fixture(self, name: str) -> Path:
        run, assets, first, context = self._deterministic_attempt(name)
        failure = self._semantic_review(
            first,
            context,
            verdict="fail",
            repair_route="source_reingest",
            route_findings=[
                self._route_finding("source", "source_reingest")
            ],
        )
        self._record_valid_v2_review(run, first, failure)
        core.reopen_curation(
            run,
            self._reopen_request(
                run, first, "source_reingest", ["source"]
            ),
        )
        inspection = core.inspect_source(run)
        replacement = core.crop_source(
            run,
            self._crop_request(
                inspection,
                page=1,
                role="method",
                claim="A new immutable source binding is selected.",
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
                    "rationale": "The new crop covers the central method.",
                },
                "primary_result": {
                    "status": "covered",
                    "asset_ids": [assets["result"]["asset_id"]],
                    "evidence_ids": ["ev-002"],
                    "rationale": "The reviewed result remains primary evidence.",
                },
            },
        }
        source_context = core.create_source_review_context(run, selection)
        core.record_source_review(
            run,
            source_context["context_path"],
            self._passing_source_review(source_context),
        )
        revised_assets = {"method": replacement, "result": assets["result"]}
        core.save_plan_revision(
            run,
            self._poster_plan(revised_assets, title="Reingested ancestry fixture"),
        )
        second = core.begin_attempt(run)
        self.assertEqual(second, "02")
        core.write_source_map(
            run,
            second,
            [
                {
                    "id": "claim-method",
                    "text": "Central method.",
                    "source_ids": ["ev-001"],
                }
            ],
        )
        second_root = run / "attempts" / second
        core.atomic_write_bytes(
            second_root / "artifact" / "poster.html",
            b"<main>Central method.</main>\n",
        )
        core.atomic_write_bytes(
            second_root / "qa" / "previews" / "poster.png",
            b"preview",
        )
        core.record_deterministic_result(
            run,
            second,
            passed=True,
            checks=[{"id": "poster_contract", "passed": True}],
            artifact_paths=["artifact/poster.html"],
            preview_paths={"poster": "qa/previews/poster.png"},
        )
        return run

    def _run_events(self, run: Path) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in (run / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]

    def _write_run_events(
        self, run: Path, events: list[dict[str, object]]
    ) -> None:
        data = b"".join(
            (
                json.dumps(
                    event,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            for event in events
        )
        core.atomic_write_bytes(run / "events.jsonl", data)

    def _bound_event(
        self, run: Path, family: str
    ) -> dict[str, object]:
        if family == "plan01":
            manifest = json.loads(
                (run / "plans" / "001" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            return {
                "event": "plan_revision_committed",
                "operation_id": manifest["operation_id"],
                "revision": 1,
            }
        if family in {"attempt01", "active_attempt"}:
            attempt_id = "01" if family == "attempt01" else "02"
            context = json.loads(
                (
                    run
                    / "attempts"
                    / attempt_id
                    / "attempt-context.json"
                ).read_text(encoding="utf-8")
            )
            return {
                "event": "attempt_started",
                "operation_id": self._canonical_hash(
                    {"operation": "begin_attempt", "context": context}
                ),
                "attempt_id": attempt_id,
                "parent_attempt": context["parent_attempt"],
                "catalog_revision": context["catalog_revision"],
                "plan_revision": context["plan_revision"],
            }
        if family == "reopen":
            entry = self._ledger_entries(run)[0]
            return {
                "event": "curation_reopened",
                "operation_id": entry["operation_id"],
                "attempt_id": entry["attempt_id"],
                "repair_route": entry["repair_route"],
            }
        self.fail(f"unknown event family: {family}")

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

    def test_content_replan_rejects_parent_identical_plan(self) -> None:
        run, _assets, attempt, context = self._deterministic_attempt(
            "content-replan-noop"
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
        core.reopen_curation(
            run,
            self._reopen_request(
                run, attempt, "content_replan", ["content"]
            ),
        )
        parent_plan = core.load_attempt_plan(run, attempt)

        with self.assertRaises(core.StateError):
            core.save_plan_revision(run, parent_plan)

        state = json.loads((run / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (
                state["state"],
                state["active_plan_revision"],
                state["attempt_count"],
                (run / "plans" / "002").exists(),
            ),
            ("curated", 1, 1, False),
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

    def test_source_reingest_rejects_catalog_without_new_asset_binding(
        self,
    ) -> None:
        run, _assets, attempt, context = self._deterministic_attempt(
            "source-reingest-noop"
        )
        review = self._semantic_review(
            attempt,
            context,
            verdict="fail",
            repair_route="source_reingest",
            route_findings=[
                self._route_finding("source", "source_reingest")
            ],
        )
        self._record_valid_v2_review(run, attempt, review)
        core.reopen_curation(
            run,
            self._reopen_request(
                run, attempt, "source_reingest", ["source"]
            ),
        )
        parent_catalog = core.load_attempt_visual_catalog(run, attempt)
        selection = {
            "run_format_version": 2,
            "assets": [
                {
                    "asset_id": item["asset_id"],
                    "roles": item["roles"],
                    "max_reuse": item["max_reuse"],
                    "importance": item["importance"],
                }
                for item in parent_catalog["assets"]
            ],
            "source_story": parent_catalog["source_story"],
        }
        source_context = core.create_source_review_context(run, selection)

        with self.assertRaises(core.StateError):
            core.record_source_review(
                run,
                source_context["context_path"],
                self._passing_source_review(source_context),
            )

        state = json.loads((run / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (
                state["state"],
                state["active_curation_revision"],
                state["attempt_count"],
                (run / "curations" / "002").exists(),
            ),
            ("curating", 1, 1, False),
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

    def test_resume_restores_missing_historical_and_active_commit_events(
        self,
    ) -> None:
        for family in ("plan01", "attempt01", "reopen", "active_attempt"):
            with self.subTest(family=family):
                run = self._two_attempt_event_fixture(
                    f"missing-bound-event-{family}"
                )
                expected = self._bound_event(run, family)
                events = self._run_events(run)
                self.assertEqual(events.count(expected), 1)
                events.remove(expected)
                self._write_run_events(run, events)

                status = core.resume_run(run, skill_root=self.skill)

                self.assertEqual(status["next_action"], "semantic_review")
                self.assertEqual(self._run_events(run).count(expected), 1)

    def test_source_reingest_resume_restores_prerequisite_historical_events_in_order(
        self,
    ) -> None:
        for family in ("plan01", "attempt01", "reopen"):
            with self.subTest(family=family):
                run = self._two_attempt_source_reingest_fixture(
                    f"source-reingest-event-order-{family}"
                )
                expected = self._bound_event(run, family)
                events = self._run_events(run)
                self.assertEqual(events.count(expected), 1)
                events.remove(expected)
                self._write_run_events(run, events)

                failure = None
                try:
                    status = core.resume_run(run, skill_root=self.skill)
                except core.IntegrityError as error:
                    failure = error
                if failure is not None:
                    self.assertEqual(self._run_events(run).count(expected), 0)
                    raise failure

                self.assertEqual(status["next_action"], "semantic_review")
                self.assertEqual(self._run_events(run).count(expected), 1)

    def test_resume_rejects_duplicate_historical_and_active_commit_events(
        self,
    ) -> None:
        for family in ("plan01", "attempt01", "reopen", "active_attempt"):
            with self.subTest(family=family):
                run = self._two_attempt_event_fixture(
                    f"duplicate-bound-event-{family}"
                )
                expected = self._bound_event(run, family)
                events = self._run_events(run)
                self.assertEqual(events.count(expected), 1)
                events.append(expected)
                self._write_run_events(run, events)

                with self.assertRaises(core.IntegrityError):
                    core.resume_run(run, skill_root=self.skill)

    def test_resume_rejects_conflicting_bound_commit_events(self) -> None:
        for family in ("plan01", "attempt01", "reopen"):
            with self.subTest(family=family):
                run = self._two_attempt_event_fixture(
                    f"conflicting-bound-event-{family}"
                )
                expected = self._bound_event(run, family)
                conflict = dict(expected)
                if family == "plan01":
                    conflict["revision"] = 99
                elif family == "attempt01":
                    conflict["plan_revision"] = 99
                else:
                    conflict["repair_route"] = "source_reingest"
                events = self._run_events(run)
                events.append(conflict)
                self._write_run_events(run, events)

                with self.assertRaises(core.IntegrityError):
                    core.resume_run(run, skill_root=self.skill)

    def test_direct_attempt_loaders_require_complete_plan_ancestry(
        self,
    ) -> None:
        import shutil

        run = self._two_attempt_event_fixture("loader-plan-ancestry")
        shutil.rmtree(run / "plans" / "001")

        for loader in (
            core.load_attempt_plan,
            core.load_attempt_visual_catalog,
        ):
            with self.subTest(loader=loader.__name__):
                with self.assertRaises(core.IntegrityError):
                    loader(run, "02")
        with self.assertRaises(core.IntegrityError):
            core.resume_run(run, skill_root=self.skill)

    def test_direct_attempt_loaders_require_complete_catalog_ancestry(
        self,
    ) -> None:
        import shutil

        run = self._two_attempt_source_reingest_fixture(
            "loader-catalog-ancestry"
        )
        shutil.rmtree(run / "curations" / "001")

        for loader in (
            core.load_attempt_plan,
            core.load_attempt_visual_catalog,
        ):
            with self.subTest(loader=loader.__name__):
                with self.assertRaises(core.IntegrityError):
                    loader(run, "02")
        with self.assertRaises(core.IntegrityError):
            core.resume_run(run, skill_root=self.skill)

    def test_direct_attempt_loaders_require_complete_committed_event_lineage(
        self,
    ) -> None:
        for family in ("plan01", "attempt01", "reopen", "active_attempt"):
            with self.subTest(family=family):
                run = self._two_attempt_event_fixture(
                    f"loader-event-lineage-{family}"
                )
                expected = self._bound_event(run, family)
                events = self._run_events(run)
                events.remove(expected)
                self._write_run_events(run, events)
                before = (run / "events.jsonl").read_bytes()

                for loader in (
                    core.load_attempt_plan,
                    core.load_attempt_visual_catalog,
                ):
                    with self.subTest(loader=loader.__name__):
                        with self.assertRaises(core.IntegrityError):
                            loader(run, "02")
                self.assertEqual((run / "events.jsonl").read_bytes(), before)

    def test_fail_review_repairs_split_state_write_on_retry_and_resume(
        self,
    ) -> None:
        for recovery in ("api_retry", "resume"):
            with self.subTest(recovery=recovery):
                run, _assets, attempt, context = self._deterministic_attempt(
                    f"split-fail-review-{recovery}"
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
                core.mark_side_state(
                    run, "failed", reason="semantic review failed"
                )

                if recovery == "api_retry":
                    self.assertEqual(
                        core.record_semantic_review(run, attempt, review),
                        review,
                    )
                    status = core.resume_run(run, skill_root=self.skill)
                else:
                    status = core.resume_run(run, skill_root=self.skill)

                review_path = (
                    run
                    / "attempts"
                    / attempt
                    / "qa"
                    / "semantic-review.json"
                )
                state = json.loads(
                    (run / "run.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    (
                        state["state"],
                        state.get("failure_origin"),
                        state.get("repair_route"),
                        state.get("semantic_review_sha256"),
                        status["next_action"],
                    ),
                    (
                        "failed",
                        "semantic_review",
                        "content_replan",
                        core.sha256_file(review_path),
                        "reopen_curation",
                    ),
                )
                events = [
                    json.loads(line)
                    for line in (run / "events.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertEqual(
                    sum(event.get("event") == "side_state" for event in events),
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

    def test_pass_review_persists_exact_review_hash(self) -> None:
        run, _assets, attempt, context = self._deterministic_attempt(
            "pass-review-hash"
        )
        review = self._semantic_review(
            attempt,
            context,
            verdict="pass",
            repair_route=None,
            route_findings=[],
        )
        self._record_valid_v2_review(run, attempt, review)

        review_path = (
            run / "attempts" / attempt / "qa" / "semantic-review.json"
        )
        state = json.loads((run / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (state["state"], state.get("semantic_review_sha256")),
            ("semantic_passed", core.sha256_file(review_path)),
        )

    def test_resume_and_finalize_reject_changed_valid_pass_review_bytes(
        self,
    ) -> None:
        for consumer in ("resume", "finalize"):
            with self.subTest(consumer=consumer):
                run, _assets, attempt, context = self._deterministic_attempt(
                    f"changed-pass-review-{consumer}"
                )
                review = self._semantic_review(
                    attempt,
                    context,
                    verdict="pass",
                    repair_route=None,
                    route_findings=[],
                )
                self._record_valid_v2_review(run, attempt, review)
                changed = json.loads(json.dumps(review))
                changed["dimension_scores"]["fidelity"] = 5
                core.atomic_write_json(
                    run
                    / "attempts"
                    / attempt
                    / "qa"
                    / "semantic-review.json",
                    changed,
                )

                with self.assertRaises(core.IntegrityError):
                    if consumer == "resume":
                        core.resume_run(run, skill_root=self.skill)
                    else:
                        core.finalize_attempt(run, attempt)

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

    def test_poster_wrapper_completes_source_reingest_lifecycle_without_mutating_attempt_one(self) -> None:
        harness = poster_skill_fixtures._load_harness()
        run = self.root / "runs" / "poster-wrapper-lifecycle"
        method, result, _supporting = (
            poster_skill_fixtures.AutoDesignPosterSkillTests._initialize_reviewed_visual_run(
                self, harness, run
            )
        )

        def poster_plan(method_asset: dict[str, object]) -> dict[str, object]:
            plan = poster_skill_fixtures._plan()
            plan["no_visual_fallback"] = None
            plan["visual_allocations"] = [
                poster_skill_fixtures._visual_allocation(
                    method_asset["asset_id"], "method"
                ),
                poster_skill_fixtures._visual_allocation(
                    result["asset_id"], "result"
                ),
            ]
            return plan

        def authored_html(method_id: str) -> str:
            html = poster_skill_fixtures._poster_html(image=True).replace("vis-001", method_id)
            marker = 'alt="Source method diagram">'
            return html.replace(
                marker,
                marker
                + f'<img src="assets/{result["asset_id"]}.png" '
                f'data-source-id="{result["asset_id"]}" alt="Source primary result">',
            )

        def fake_render(*, attempt_root: Path, **_kwargs: object) -> dict[str, object]:
            previews = attempt_root / "qa" / "previews"
            previews.mkdir(parents=True, exist_ok=True)
            (previews / "poster.png").write_bytes(b"screen-preview")
            (previews / "poster-print.png").write_bytes(b"print-preview")
            (attempt_root / "artifact" / "preview.png").write_bytes(b"print-preview")
            (attempt_root / "artifact" / "poster.pdf").write_bytes(b"%PDF-fixture")
            return {
                "passed": True,
                "checks": [
                    {"id": "browser_geometry", "passed": True, "detail": "passed"},
                    {"id": "computed_typography", "passed": True, "detail": "passed"},
                    {"id": "single_page_pdf", "passed": True, "detail": "passed"},
                ],
                "preview_paths": {
                    "poster_screen": "qa/previews/poster.png",
                    "poster_pdf": "qa/previews/poster-print.png",
                },
            }

        def fake_dom(
            run_dir: Path, attempt_id: str, **_kwargs: object
        ) -> dict[str, object]:
            previews = Path(run_dir) / "attempts" / attempt_id / "qa" / "previews"
            (previews / "dom-screen.png").write_bytes(b"dom-screen-preview")
            (previews / "dom-print.png").write_bytes(b"dom-print-preview")
            return {
                "passed": True,
                "artifact_unchanged": True,
                "findings": [],
                "browser_diagnostics": {
                    "blocked_requests": [],
                    "blocked_popups": [],
                    "blocked_workers": [],
                    "console_errors": [],
                    "request_errors": [],
                    "page_errors": [],
                },
            }

        def semantic_review(
            attempt_id: str,
            context: dict[str, object],
            *,
            verdict: str,
            route: str | None,
            findings: list[dict[str, object]],
        ) -> dict[str, object]:
            return {
                "format_version": 1,
                "attempt_id": attempt_id,
                "review_context_sha256": context["context_sha256"],
                "artifact_hashes": context["artifact_hashes"],
                "preview_hashes": context["preview_hashes"],
                "reviewed_frame_ids": sorted(context["preview_hashes"]),
                "source_manifest_sha256": context["source_manifest_sha256"],
                "source_map_sha256": context["source_map_sha256"],
                "rubric_sha256": context["rubric_sha256"],
                "reviewer_mode": "fresh_subagent",
                "dimension_scores": {
                    name: 4 for name in harness.REVIEW_RUBRIC["dimensions"]
                },
                "blockers": [],
                "localized_repairs": [],
                "repair_route": route,
                "route_findings": findings,
                "verdict": verdict,
                "complete": True,
            }

        harness.save_poster_plan(run, poster_plan(method))
        first = harness.begin_poster_attempt(run)
        self.assertEqual(first["attempt_id"], "01")
        (run / first["poster_path"]).write_text(
            authored_html(str(method["asset_id"])), encoding="utf-8"
        )
        source_map = self.root / "poster-wrapper-source-map.json"
        source_map.write_text(
            json.dumps({"claims": poster_skill_fixtures._claims()}),
            encoding="utf-8",
        )
        with mock.patch.object(
            harness, "_render_poster_outputs", side_effect=fake_render
        ), mock.patch.object(
            harness.poster_dom_audit, "run_poster_dom_audit", side_effect=fake_dom
        ):
            deterministic = harness.validate_poster_attempt(
                run,
                "01",
                source_map_path=source_map,
                allow_browser_install=False,
            )
        self.assertTrue(deterministic["passed"], deterministic)
        first_review_context = harness.create_poster_review_context(run, "01")
        source_finding = {
            "finding_id": "method-fragment",
            "code": "fragmentary_crop",
            "minimum_route": "source_reingest",
            "block_id": "method",
            "message": "The method crop is fragmentary.",
        }
        failed = semantic_review(
            "01",
            first_review_context,
            verdict="fail",
            route="source_reingest",
            findings=[source_finding],
        )
        harness.record_poster_review(run, "01", failed)
        attempt_one = run / "attempts" / "01"
        attempt_one_bytes = {
            path.relative_to(attempt_one).as_posix(): path.read_bytes()
            for path in attempt_one.rglob("*")
            if path.is_file()
        }
        harness.reopen_poster_curation(
            run,
            self._reopen_request(
                run, "01", "source_reingest", ["method-fragment"]
            ),
        )

        inspection = harness.inspect_poster_source(run)
        replacement = harness.crop_poster_source(
            run,
            self._crop_request(
                inspection,
                page=1,
                role="method",
                claim="The replacement crop contains the complete central method.",
                bbox=[0.0, 0.0, 0.9, 1.0],
            ),
        )
        evidence_id = str(harness.core.load_evidence(run)[0]["id"])
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
                    "asset_id": result["asset_id"],
                    "roles": ["result"],
                    "max_reuse": 2,
                    "importance": "essential",
                },
            ],
            "source_story": {
                "central_method": {
                    "status": "covered",
                    "asset_ids": [replacement["asset_id"]],
                    "evidence_ids": [evidence_id],
                    "rationale": "The replacement crop shows the complete method.",
                },
                "primary_result": {
                    "status": "covered",
                    "asset_ids": [result["asset_id"]],
                    "evidence_ids": [evidence_id],
                    "rationale": "The original complete result remains primary evidence.",
                },
            },
        }
        source_context = harness.create_poster_source_review_context(run, selection)
        harness.record_poster_source_review(
            run,
            source_context["context_path"],
            self._passing_source_review(source_context),
        )
        harness.save_poster_plan(run, poster_plan(replacement))
        second = harness.begin_poster_attempt(run)
        self.assertEqual(second["attempt_id"], "02")
        (run / second["poster_path"]).write_text(
            authored_html(str(replacement["asset_id"])), encoding="utf-8"
        )
        with mock.patch.object(
            harness, "_render_poster_outputs", side_effect=fake_render
        ), mock.patch.object(
            harness.poster_dom_audit, "run_poster_dom_audit", side_effect=fake_dom
        ):
            deterministic = harness.validate_poster_attempt(
                run,
                "02",
                source_map_path=source_map,
                allow_browser_install=False,
            )
        self.assertTrue(deterministic["passed"], deterministic)
        second_context = harness.create_poster_review_context(run, "02")
        harness.record_poster_review(
            run,
            "02",
            semantic_review(
                "02", second_context, verdict="pass", route=None, findings=[]
            ),
        )
        manifest = harness.finalize_poster_attempt(run, "02")
        self.assertEqual(manifest["attempt_id"], "02")
        self.assertEqual(
            {
                path.relative_to(attempt_one).as_posix(): path.read_bytes()
                for path in attempt_one.rglob("*")
                if path.is_file()
            },
            attempt_one_bytes,
        )
        final_html = (run / "final" / "poster.html").read_text(encoding="utf-8")
        self.assertIn(str(replacement["asset_id"]), final_html)
        self.assertNotIn(str(method["asset_id"]), final_html)
        self.assertTrue(
            (run / "final" / "assets" / f"{replacement['asset_id']}.png").is_file()
        )
        self.assertFalse(
            (run / "final" / "assets" / f"{method['asset_id']}.png").exists()
        )
        self.assertEqual(harness.resume_poster_run(run)["next_action"], "complete")

    def test_poster_dom_audit_wrapper_delegates_to_the_shared_read_only_engine(self) -> None:
        harness = poster_skill_fixtures._load_harness()
        expected = {"passed": True, "findings": []}
        with mock.patch.object(
            harness.poster_dom_audit,
            "run_poster_dom_audit",
            return_value=expected,
        ) as shared:
            observed = harness.run_poster_dom_audit(
                self.root / "run",
                "01",
                cache_root=self.root / "browser-cache",
                allow_browser_install=False,
            )

        self.assertIs(observed, expected)
        shared.assert_called_once_with(
            self.root / "run",
            "01",
            cache_root=self.root / "browser-cache",
            allow_browser_install=False,
        )

    def test_render_cleanup_preserves_dom_audit_outputs_only(self) -> None:
        harness = poster_skill_fixtures._load_harness()
        attempt = self.root / "attempt"
        (attempt / "artifact").mkdir(parents=True)
        previews = attempt / "qa" / "previews"
        previews.mkdir(parents=True)
        preserved = {
            attempt / "qa" / "dom-audit.json": b"{}\n",
            previews / "dom-screen.png": b"screen",
            previews / "dom-print.png": b"print",
        }
        removed = {
            attempt / "artifact" / "poster.pdf": b"pdf",
            attempt / "artifact" / "preview.png": b"preview",
            attempt / "qa" / "poster-output.json": b"{}\n",
            attempt / "qa" / "deterministic.json": b"{}\n",
            previews / "audit.json": b"{}\n",
            previews / "poster.png": b"screen",
            previews / "poster-print.png": b"print",
        }
        for path, payload in {**preserved, **removed}.items():
            path.write_bytes(payload)

        harness._clear_generated_attempt_outputs(attempt)

        self.assertEqual(
            {path.relative_to(attempt).as_posix(): path.read_bytes() for path in preserved},
            {path.relative_to(attempt).as_posix(): payload for path, payload in preserved.items()},
        )
        self.assertTrue(all(not path.exists() for path in removed))

    def test_validate_runs_one_shared_dom_engine_after_render_and_routes_findings(self) -> None:
        harness = poster_skill_fixtures._load_harness()
        run = self.root / "dom-validate-run"
        source = self.root / "dom-validate-source.txt"
        source.write_text(
            "Grounded poster source reports 85% accuracy and uses two-stage routing. "
            "The grounded poster retains accuracy.",
            encoding="utf-8",
        )
        fixture = poster_skill_fixtures.AutoDesignPosterSkillTests(methodName="runTest")
        fixture.root = self.root
        fixture._initialize_no_visual_run(harness, run, source)
        attempt = harness.begin_poster_attempt(run)["attempt_id"]
        attempt_root = run / "attempts" / attempt
        (attempt_root / "artifact" / "poster.html").write_text(
            poster_skill_fixtures._poster_html(), encoding="utf-8"
        )
        source_map = self.root / "dom-validate-source-map.json"
        source_map.write_text(
            json.dumps({"claims": poster_skill_fixtures._claims()}),
            encoding="utf-8",
        )
        events: list[str] = []

        def fake_render(**_kwargs: object) -> dict[str, object]:
            events.append("render")
            previews = attempt_root / "qa" / "previews"
            previews.mkdir(parents=True, exist_ok=True)
            (previews / "poster-print.png").write_bytes(b"print-preview")
            (attempt_root / "artifact" / "poster.pdf").write_bytes(b"%PDF-fixture")
            (attempt_root / "artifact" / "preview.png").write_bytes(b"print-preview")
            return {
                "passed": True,
                "checks": [
                    {"id": "computed_typography", "passed": True, "detail": "passed"},
                    {"id": "single_page_pdf", "passed": True, "detail": "passed"},
                ],
                "preview_paths": {"poster_pdf": "qa/previews/poster-print.png"},
            }

        def fake_dom(*_args: object, **_kwargs: object) -> dict[str, object]:
            events.append("dom")
            previews = attempt_root / "qa" / "previews"
            (previews / "dom-screen.png").write_bytes(b"dom-screen")
            (previews / "dom-print.png").write_bytes(b"dom-print")
            return {
                "passed": False,
                "artifact_unchanged": True,
                "findings": [
                    {
                        "code": "poster-dom-text-clipping",
                        "block_id": "method-copy",
                        "severity": "P0",
                        "geometry": {"clipped_height_px": 18.0},
                        "message": "Editable text is clipped by a rendered ancestor.",
                        "suggested_repair_route": "layout_repair",
                    }
                ],
                "browser_diagnostics": {
                    "blocked_requests": [],
                    "blocked_popups": [],
                    "blocked_workers": [],
                    "console_errors": [],
                    "request_errors": [],
                    "page_errors": [],
                },
                "screenshots": {
                    "screen": {"path": "qa/previews/dom-screen.png", "sha256": "a" * 64},
                    "print": {"path": "qa/previews/dom-print.png", "sha256": "b" * 64},
                },
            }

        with mock.patch.object(
            harness, "_render_poster_outputs", side_effect=fake_render
        ), mock.patch.object(
            harness.poster_dom_audit, "run_poster_dom_audit", side_effect=fake_dom
        ) as shared:
            deterministic = harness.validate_poster_attempt(
                run,
                attempt,
                source_map_path=source_map,
                cache_root=self.root / "browser-cache",
                allow_browser_install=False,
            )

        self.assertEqual(events, ["render", "dom"])
        shared.assert_called_once_with(
            run,
            attempt,
            cache_root=self.root / "browser-cache",
            allow_browser_install=False,
        )
        self.assertFalse(deterministic["passed"], deterministic)
        finding = next(
            check
            for check in deterministic["checks"]
            if check["id"] == "poster-dom-text-clipping"
        )
        self.assertEqual(finding["minimum_route"], "layout_repair")
        self.assertEqual(finding["block_id"], "method-copy")
        self.assertEqual(
            deterministic["previews"]["poster_screen"]["path"],
            "qa/previews/dom-screen.png",
        )

    def test_all_dom_codes_have_the_layout_repair_minimum_route(self) -> None:
        harness = poster_skill_fixtures._load_harness()
        self.assertEqual(
            {
                code: harness.POSTER_FINDING_MINIMUM_ROUTE.get(code)
                for code in harness.poster_dom_audit.STABLE_FINDING_CODES
            },
            {
                code: "layout_repair"
                for code in harness.poster_dom_audit.STABLE_FINDING_CODES
            },
        )
        self.assertNotIn(
            "audit_local_html(",
            __import__("inspect").getsource(harness._render_poster_outputs),
        )


if __name__ == "__main__":
    unittest.main()
