from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from agent_skills._shared import portable_core as core
from agent_skills._shared import portable_png


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
    rows = b"".join(b"\0" + bytes([value, value, value, 255]) * width for _ in range(height))
    return PNG_SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(rows)) + _chunk(b"IEND", b"")


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, bytes | str | None]]:
    snapshot: dict[str, tuple[int, int, bytes | str | None]] = {}
    for path in sorted(root.rglob("*")):
        details = path.lstat()
        if stat.S_ISREG(details.st_mode):
            content: bytes | str | None = path.read_bytes()
        elif stat.S_ISLNK(details.st_mode):
            content = os.readlink(path)
        else:
            content = None
        snapshot[path.relative_to(root).as_posix()] = (
            stat.S_IFMT(details.st_mode),
            details.st_nlink,
            content,
        )
    return snapshot


def _events(run: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]


class PortableSourceCurationV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skill = self.root / "installed-skill"
        (self.skill / "scripts").mkdir(parents=True)
        (self.skill / "references").mkdir()
        (self.skill / "SKILL.md").write_text("# Fixture skill\n", encoding="utf-8")
        (self.skill / "scripts" / "tool.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        (self.skill / "references" / "grounding.md").write_text(
            "Ground every claim.\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _initialize(self, run: Path, *, run_format_version: int | None = None) -> dict[str, object]:
        arguments: dict[str, object] = {
            "release_version": "0.1.0",
            "archive_sha256": "a" * 64,
        }
        if run_format_version is not None:
            arguments["run_format_version"] = run_format_version
        return core.initialize_run(run, self.skill, **arguments)

    def _fake_poppler(self, name: str) -> tuple[dict[str, Path], Path]:
        tools_root = self.root / name
        tools_root.mkdir()
        first_page = tools_root / "first-page.png"
        second_page = tools_root / "second-page.png"
        extracted = tools_root / "extracted.png"
        first_page.write_bytes(_png(10, 6, 10))
        second_page.write_bytes(_png(7, 5, 20))
        extracted.write_bytes(_png(2, 2, 30))
        calls = tools_root / "calls.jsonl"
        script = f'''#!/usr/bin/env python3
import json
import shutil
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
with Path({str(calls)!r}).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({{"name": name, "args": sys.argv[1:]}}) + "\\n")
if name == "pdfinfo":
    print("Pages: 2")
elif name == "pdftotext":
    Path(sys.argv[-1]).write_text("Caption one.\\fCaption two.\\n", encoding="utf-8")
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
        return tools, calls

    def _prepare_pdf_run(self, name: str) -> tuple[Path, dict[str, object]]:
        run = self.root / "runs" / name
        self._initialize(run, run_format_version=2)
        source = self.root / f"{name}.pdf"
        source.write_bytes(f"%PDF-1.4\n{name}\n".encode())
        tools, _calls = self._fake_poppler(f"poppler-{name}")
        core.prepare_source(run, source, tool_paths=tools)
        return run, core.inspect_source(run)

    def _crop_request(
        self,
        inspection: dict[str, object],
        **updates: object,
    ) -> dict[str, object]:
        source = inspection["source"]
        pages = inspection["pages"]
        assert isinstance(source, dict) and isinstance(pages, list)
        request: dict[str, object] = {
            "run_format_version": 2,
            "source_sha256": source["sha256"],
            "page_manifest_sha256": inspection["page_manifest_sha256"],
            "page": 1,
            "page_sha256": pages[0]["sha256"],
            "bbox_normalized": [0.11, 0.20, 0.51, 0.70],
            "role": "method",
            "claim": "The central method is shown in this page region.",
            "max_reuse": 1,
        }
        request.update(updates)
        return request

    def _review_fixture(
        self,
        context: dict[str, object],
        **updates: object,
    ) -> dict[str, object]:
        review: dict[str, object] = {
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
        review.update(updates)
        return review

    def _review_source_fixture(
        self,
        name: str,
    ) -> tuple[Path, dict[str, object], dict[str, object]]:
        run, inspection = self._prepare_pdf_run(name)
        pages = inspection["pages"]
        assert isinstance(pages, list)
        method = core.crop_source(run, self._crop_request(inspection))
        result = core.crop_source(
            run,
            self._crop_request(
                inspection,
                page=2,
                page_sha256=pages[1]["sha256"],
                bbox_normalized=[0.0, 0.0, 1.0, 1.0],
                role="result",
                claim="The primary result appears on the second page.",
            ),
        )
        selection: dict[str, object] = {
            "run_format_version": 2,
            "assets": [
                {
                    "asset_id": method["asset_id"],
                    "roles": ["method-overview"],
                    "max_reuse": 1,
                    "importance": "essential",
                },
                {
                    "asset_id": result["asset_id"],
                    "roles": ["novel-result-emphasis"],
                    "max_reuse": 99,
                    "importance": "supporting",
                },
            ],
            "source_story": {
                "central_method": {
                    "status": "covered",
                    "asset_ids": [method["asset_id"]],
                    "evidence_ids": ["ev-001"],
                    "rationale": "The complete framework crop explains the central method.",
                },
                "primary_result": {
                    "status": "covered",
                    "asset_ids": [result["asset_id"]],
                    "evidence_ids": ["ev-002"],
                    "rationale": "The second-page crop contains the primary result.",
                },
            },
        }
        return run, selection, {"method": method, "result": result}

    def _canonical_digest(self, value: object) -> str:
        encoded = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def test_source_review_context_binds_the_complete_selected_source_set(self) -> None:
        run, selection, crops = self._review_source_fixture("review-context")

        first = core.create_source_review_context(run, selection)
        second = core.create_source_review_context(run, selection)

        self.assertRegex(
            first["context_path"],
            r"^source-reviews/review-[0-9a-f]{12}-001/context\.json$",
        )
        self.assertEqual(
            second["context_path"],
            first["context_path"].replace("-001/context.json", "-002/context.json"),
        )
        context_path = run / str(first["context_path"])
        context = json.loads(context_path.read_text(encoding="utf-8"))
        payload = dict(context)
        context_sha256 = payload.pop("context_sha256")
        self.assertEqual(context_sha256, self._canonical_digest(payload))
        self.assertEqual(first, context)
        self.assertEqual(context["selection"], selection)
        self.assertEqual(
            context["source_manifest"],
            {
                "path": "evidence/source_manifest.json",
                "sha256": core.sha256_file(run / "evidence" / "source_manifest.json"),
            },
        )
        self.assertEqual(
            context["page_manifest"],
            {
                "path": "evidence/page-manifest.json",
                "sha256": core.sha256_file(run / "evidence" / "page-manifest.json"),
            },
        )
        self.assertEqual(context["current_catalog_parent"], {"revision": None, "sha256": None})
        self.assertEqual(context["evidence_ids"], ["ev-001", "ev-002"])
        self.assertEqual(
            context["rubric"]["dimensions"],
            [
                "importance",
                "crop_completeness",
                "caption_claim_match",
                "label_axis_legend_readability",
                "duplicate_or_ornamental_content",
                "method_result_coverage",
                "poster_area_fit",
            ],
        )
        bindings = {item["asset_id"]: item for item in context["asset_bindings"]}
        self.assertEqual(set(bindings), {crops["method"]["asset_id"], crops["result"]["asset_id"]})
        for crop in crops.values():
            binding = bindings[crop["asset_id"]]
            receipt_path = run / crop["receipt_path"]
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(binding["asset_sha256"], crop["asset_sha256"])
            self.assertEqual(binding["receipt_sha256"], receipt["receipt_sha256"])
            self.assertEqual(binding["receipt_file_sha256"], core.sha256_file(receipt_path))
            preview_path = context_path.parent / binding["preview_path"]
            self.assertEqual(preview_path.read_bytes(), (run / crop["asset_path"]).read_bytes())
            self.assertEqual(binding["preview_sha256"], core.sha256_file(preview_path))
        self.assertEqual(
            sorted(path.relative_to(context_path.parent).as_posix() for path in context_path.parent.rglob("*") if path.is_file()),
            [
                "context.json",
                f"previews/{crops['method']['asset_id']}.png",
                f"previews/{crops['result']['asset_id']}.png",
            ],
        )

    def test_source_review_context_rejects_invalid_or_stale_selection_without_writes(self) -> None:
        run, selection, crops = self._review_source_fixture("review-context-reject")
        invalid: list[dict[str, object]] = []

        extra = json.loads(json.dumps(selection))
        extra["unexpected"] = True
        invalid.append(extra)
        duplicate = json.loads(json.dumps(selection))
        duplicate["assets"].append(duplicate["assets"][0])
        invalid.append(duplicate)
        unknown_asset = json.loads(json.dumps(selection))
        unknown_asset["assets"][0]["asset_id"] = "src-000000000000000000000000"
        invalid.append(unknown_asset)
        unknown_evidence = json.loads(json.dumps(selection))
        unknown_evidence["source_story"]["central_method"]["evidence_ids"] = ["claim-unknown"]
        invalid.append(unknown_evidence)
        invalid_role = json.loads(json.dumps(selection))
        invalid_role["assets"][0]["roles"] = ["not canonical"]
        invalid.append(invalid_role)
        invalid_reuse = json.loads(json.dumps(selection))
        invalid_reuse["assets"][0]["max_reuse"] = True
        invalid.append(invalid_reuse)
        invalid_importance = json.loads(json.dumps(selection))
        invalid_importance["assets"][0]["importance"] = "decorative"
        invalid.append(invalid_importance)
        invalid_not_applicable = json.loads(json.dumps(selection))
        invalid_not_applicable["source_story"]["primary_result"] = {
            "status": "not_applicable",
            "asset_ids": [],
            "evidence_ids": [],
            "rationale": "",
        }
        invalid.append(invalid_not_applicable)
        invalid_covered = json.loads(json.dumps(selection))
        invalid_covered["source_story"]["central_method"]["asset_ids"] = []
        invalid.append(invalid_covered)

        for index, candidate in enumerate(invalid):
            with self.subTest(index=index):
                before = _tree_snapshot(run)
                with self.assertRaises((core.ContractError, core.IntegrityError)):
                    core.create_source_review_context(run, candidate)
                self.assertEqual(_tree_snapshot(run), before)

        hint_selection = json.loads(json.dumps(selection))
        hint_selection["assets"][0]["asset_id"] = "pdfimage-0001"
        with self.assertRaises(core.ContractError):
            core.create_source_review_context(run, hint_selection)

        receipt_path = run / crops["method"]["receipt_path"]
        tampered = json.loads(receipt_path.read_text(encoding="utf-8"))
        tampered["semantic_request"]["role"] = "tampered"
        core.atomic_write_json(receipt_path, tampered)
        before = _tree_snapshot(run)
        with self.assertRaises(core.IntegrityError):
            core.create_source_review_context(run, selection)
        self.assertEqual(_tree_snapshot(run), before)

    def test_source_review_supports_source_grounded_no_visuals_without_a_quota(self) -> None:
        run = self.root / "runs" / "review-no-visuals"
        self._initialize(run, run_format_version=2)
        source = self.root / "review-no-visuals.md"
        source.write_text("# Conceptual paper\n\nNo figures are present.\n", encoding="utf-8")
        core.prepare_source(run, source)
        selection = {
            "run_format_version": 2,
            "assets": [],
            "source_story": {
                "central_method": {
                    "status": "not_applicable",
                    "asset_ids": [],
                    "evidence_ids": ["ev-001"],
                    "rationale": "The source describes a conceptual argument and contains no visual method.",
                },
                "primary_result": {
                    "status": "not_applicable",
                    "asset_ids": [],
                    "evidence_ids": ["ev-001"],
                    "rationale": "The source contains no visual quantitative result.",
                },
            },
        }

        context = core.create_source_review_context(run, selection)
        result = core.record_source_review(run, context["context_path"], self._review_fixture(context))

        self.assertEqual((result["verdict"], result["state"]), ("pass", "curated"))
        catalog = json.loads((run / "curations" / "001" / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog["assets"], [])
        self.assertNotIn("visual_count", catalog)
        self.assertNotIn("minimum_visuals", catalog)

    def test_source_review_schema_is_exact_and_failures_remain_in_curation(self) -> None:
        run, selection, crops = self._review_source_fixture("review-schema")
        context = core.create_source_review_context(run, selection)
        valid = self._review_fixture(context)
        invalid: list[dict[str, object]] = []
        extra = json.loads(json.dumps(valid))
        extra["unexpected"] = True
        invalid.append(extra)
        reviewer = json.loads(json.dumps(valid))
        reviewer["reviewer_kind"] = "author"
        invalid.append(reviewer)
        for score in (True, 4.0, float("nan"), 3, 6):
            review = json.loads(json.dumps(valid))
            review["dimension_scores"]["importance"] = score
            invalid.append(review)
        blocked_pass = json.loads(json.dumps(valid))
        blocked_pass["blockers"] = [{"code": "unreadable", "finding": "The key crop is unreadable."}]
        invalid.append(blocked_pass)
        incomplete = json.loads(json.dumps(valid))
        incomplete["complete"] = False
        invalid.append(incomplete)
        empty_fail = json.loads(json.dumps(valid))
        empty_fail["verdict"] = "fail"
        empty_fail["dimension_scores"]["crop_completeness"] = 2
        invalid.append(empty_fail)

        for index, review in enumerate(invalid):
            with self.subTest(index=index):
                before = _tree_snapshot(run)
                with self.assertRaises(core.ContractError):
                    core.record_source_review(run, context["context_path"], review)
                self.assertEqual(_tree_snapshot(run), before)

        failing_context = core.create_source_review_context(run, selection)
        failing_review = self._review_fixture(
            failing_context,
            verdict="fail",
            dimension_scores={
                **valid["dimension_scores"],
                "crop_completeness": 2,
            },
            asset_findings=[{
                "asset_id": crops["method"]["asset_id"],
                "dimension": "crop_completeness",
                "finding": "The method label is cropped.",
            }],
            localized_repairs=[{
                "target": f"asset:{crops['method']['asset_id']}",
                "instruction": "Register a wider crop from the same page.",
            }],
        )
        before_invalid_boundary = _tree_snapshot(run)
        with self.assertRaises(core.ContractError):
            core.record_source_review(
                run,
                failing_context["context_path"],
                failing_review,
                fail_at="after_review_staging_write",
            )
        self.assertEqual(_tree_snapshot(run), before_invalid_boundary)

        result = core.record_source_review(
            run, failing_context["context_path"], failing_review
        )
        self.assertEqual((result["verdict"], result["state"]), ("fail", "curating"))
        self.assertEqual(list((run / "curations").iterdir()), [])
        self.assertTrue((run / failing_context["context_path"]).parent.joinpath("review.json").is_file())
        self.assertEqual(
            sum(event.get("event") == "source_review_failed" for event in _events(run)),
            1,
        )

    def test_passing_source_review_commits_one_immutable_catalog_with_cas(self) -> None:
        run, selection, crops = self._review_source_fixture("review-pass")
        accepted = core.create_source_review_context(run, selection)
        stale = core.create_source_review_context(run, selection)
        review = self._review_fixture(accepted, reviewer_kind="host_fresh_pass")

        result = core.record_source_review(run, accepted["context_path"], review)
        replay = core.record_source_review(run, accepted["context_path"], review)

        self.assertEqual(replay, result)
        revision = run / "curations" / "001"
        self.assertEqual(
            {path.name for path in revision.iterdir()},
            {"catalog.json", "review.json", "manifest.json", "COMMIT.json"},
        )
        state = json.loads((run / "run.json").read_text(encoding="utf-8"))
        catalog = json.loads((revision / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (state["state"], state["active_curation_revision"], state["active_curation_sha256"]),
            ("curated", 1, core.sha256_file(revision / "catalog.json")),
        )
        self.assertEqual(catalog["parent"], {"revision": None, "sha256": None})
        self.assertEqual(catalog["source_story"], selection["source_story"])
        self.assertEqual({asset["asset_id"] for asset in catalog["assets"]}, {item["asset_id"] for item in crops.values()})
        selected_by_id = {item["asset_id"]: item for item in selection["assets"]}
        for asset in catalog["assets"]:
            selected = selected_by_id[asset["asset_id"]]
            receipt = json.loads((run / asset["receipt_path"]).read_text(encoding="utf-8"))
            self.assertEqual(asset["trust"], "reviewed")
            self.assertEqual(asset["receipt_sha256"], receipt["receipt_sha256"])
            self.assertEqual(asset["roles"], selected["roles"])
            self.assertEqual(asset["max_reuse"], selected["max_reuse"])
            self.assertEqual(asset["importance"], selected["importance"])
        self.assertEqual(
            sum(event.get("event") == "source_review_passed" for event in _events(run)),
            1,
        )
        with self.assertRaises((core.StateError, core.IntegrityError)):
            core.record_source_review(run, stale["context_path"], self._review_fixture(stale))

        exhausted_context = json.loads(json.dumps(accepted))
        exhausted_context["current_catalog_parent"] = {
            "revision": 999,
            "sha256": "a" * 64,
        }
        with self.assertRaises(core.StateError):
            core._curation_documents(exhausted_context, review)

        before_tampered_retry = _tree_snapshot(run)
        catalog["assets"][0]["trust"] = "tampered"
        core.atomic_write_json(revision / "catalog.json", catalog)
        after_tamper = _tree_snapshot(run)
        with self.assertRaises(core.IntegrityError):
            core.record_source_review(run, accepted["context_path"], review)
        self.assertNotEqual(after_tamper, before_tampered_retry)
        self.assertEqual(_tree_snapshot(run), after_tamper)

    def test_source_review_rejects_context_asset_ledger_and_path_tamper(self) -> None:
        run, selection, _crops = self._review_source_fixture("review-tamper")
        context = core.create_source_review_context(run, selection)
        review = self._review_fixture(context)
        context_dir = (run / context["context_path"]).parent
        outside = self.root / "outside-context.json"
        outside.write_bytes((context_dir / "context.json").read_bytes())
        with self.assertRaises(core.PathSafetyError):
            core.record_source_review(run, outside, review)

        preview = next((context_dir / "previews").iterdir())
        preview.write_bytes(_png(1, 1, 123))
        before = _tree_snapshot(run)
        with self.assertRaises(core.IntegrityError):
            core.record_source_review(run, context["context_path"], review)
        self.assertEqual(_tree_snapshot(run), before)

        run2, selection2, _ = self._review_source_fixture("review-event-tamper")
        context2 = core.create_source_review_context(run2, selection2)
        events = (run2 / "events.jsonl").read_bytes()
        (run2 / "events.jsonl").write_bytes(events.replace(b"source_crop_registered", b"source_crop_rewritten", 1))
        before2 = _tree_snapshot(run2)
        with self.assertRaises(core.IntegrityError):
            core.record_source_review(run2, context2["context_path"], self._review_fixture(context2))
        self.assertEqual(_tree_snapshot(run2), before2)

        run3, selection3, _ = self._review_source_fixture("review-ledger-tamper")
        context3 = core.create_source_review_context(run3, selection3)
        (run3 / "provenance" / "supersessions.jsonl").write_text("{}\n", encoding="utf-8")
        before3 = _tree_snapshot(run3)
        with self.assertRaises(core.IntegrityError):
            core.record_source_review(run3, context3["context_path"], self._review_fixture(context3))
        self.assertEqual(_tree_snapshot(run3), before3)

    def test_source_review_recovers_each_transaction_boundary_exactly_once(self) -> None:
        boundaries = (
            "after_review_staging_write",
            "after_curation_promotion",
            "after_curation_pointer_write",
            "after_curation_event_write",
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                run, selection, _ = self._review_source_fixture(f"review-crash-{boundary}")
                context = core.create_source_review_context(run, selection)
                review = self._review_fixture(context)
                with self.assertRaises(core.SimulatedCrash):
                    core.record_source_review(
                        run, context["context_path"], review, fail_at=boundary
                    )

                recovered = core.record_source_review(run, context["context_path"], review)

                self.assertEqual((recovered["verdict"], recovered["curation_revision"]), ("pass", 1))
                self.assertEqual(
                    {path.name for path in (run / "curations" / "001").iterdir()},
                    {"catalog.json", "review.json", "manifest.json", "COMMIT.json"},
                )
                self.assertFalse(any((run / "curations").glob(".curation-staging-*")))
                self.assertEqual(
                    sum(event.get("event") == "source_review_passed" for event in _events(run)),
                    1,
                )

        run, selection, _ = self._review_source_fixture("review-stage-extra")
        context = core.create_source_review_context(run, selection)
        review = self._review_fixture(context)
        with self.assertRaises(core.SimulatedCrash):
            core.record_source_review(
                run,
                context["context_path"],
                review,
                fail_at="after_review_staging_write",
            )
        stage = next((run / "curations").glob(".curation-staging-*"))
        (stage / "unexpected.bin").write_bytes(b"not part of the commit")
        with self.assertRaises(core.IntegrityError):
            core.record_source_review(run, context["context_path"], review)

    def test_source_review_transactions_serialize_and_v1_rejects_before_locking(self) -> None:
        run, selection, _ = self._review_source_fixture("review-concurrency")
        with ThreadPoolExecutor(max_workers=4) as executor:
            contexts = list(
                executor.map(
                    lambda _index: core.create_source_review_context(run, selection),
                    range(4),
                )
            )
        self.assertEqual(
            sorted(Path(item["context_path"]).parent.name.rsplit("-", 1)[1] for item in contexts),
            ["001", "002", "003", "004"],
        )
        context = contexts[0]
        review = self._review_fixture(context)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(
                executor.map(
                    lambda _index: core.record_source_review(
                        run, context["context_path"], review
                    ),
                    range(4),
                )
            )
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(
            sum(event.get("event") == "source_review_passed" for event in _events(run)),
            1,
        )

        v1 = self.root / "runs" / "review-v1"
        self._initialize(v1)
        parent_before = _tree_snapshot(v1.parent)
        with self.assertRaises(core.StateError):
            core.create_source_review_context(v1, selection)
        with self.assertRaises(core.StateError):
            core.record_source_review(v1, "source-reviews/review-deadbeef0000-001/context.json", {})
        self.assertEqual(_tree_snapshot(v1.parent), parent_before)

    def test_source_review_registry_and_committed_history_are_exact_before_append(self) -> None:
        run, selection, _ = self._review_source_fixture("review-registry-extra")
        context = core.create_source_review_context(run, selection)
        context_dir = (run / context["context_path"]).parent
        (context_dir / "unlisted.bin").write_bytes(b"not part of the review context")
        before = _tree_snapshot(run)

        with self.assertRaises(core.IntegrityError):
            core.create_source_review_context(run, selection)

        self.assertEqual(_tree_snapshot(run), before)

        run_record, selection_record, _ = self._review_source_fixture(
            "review-registry-extra-before-record"
        )
        unrelated = core.create_source_review_context(run_record, selection_record)
        candidate = core.create_source_review_context(run_record, selection_record)
        (run_record / unrelated["context_path"]).parent.joinpath(
            "unlisted.bin"
        ).write_bytes(b"not part of any context")
        before_record = _tree_snapshot(run_record)

        with self.assertRaises(core.IntegrityError):
            core.record_source_review(
                run_record,
                candidate["context_path"],
                self._review_fixture(candidate),
            )

        self.assertEqual(_tree_snapshot(run_record), before_record)

        run_gap, selection_gap, _ = self._review_source_fixture(
            "review-registry-sequence-gap"
        )
        removed = core.create_source_review_context(run_gap, selection_gap)
        remaining = core.create_source_review_context(run_gap, selection_gap)
        core._remove_regular_tree((run_gap / removed["context_path"]).parent)
        before_gap = _tree_snapshot(run_gap)

        with self.assertRaises(core.IntegrityError):
            core.record_source_review(
                run_gap,
                remaining["context_path"],
                self._review_fixture(remaining),
            )

        self.assertEqual(_tree_snapshot(run_gap), before_gap)

        run2, selection2, _ = self._review_source_fixture("review-history-tamper")
        accepted = core.create_source_review_context(run2, selection2)
        core.record_source_review(
            run2, accepted["context_path"], self._review_fixture(accepted)
        )
        manifest_path = run2 / "curations" / "001" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source_review_context_sha256"] = "0" * 64
        core.atomic_write_json(manifest_path, manifest)
        state_path = run2 / "run.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["state"] = "curating"
        core.atomic_write_json(state_path, state)
        before2 = _tree_snapshot(run2)

        with self.assertRaises(core.IntegrityError):
            core.create_source_review_context(run2, selection2)

        self.assertEqual(_tree_snapshot(run2), before2)

    def test_conflicting_orphan_curation_does_not_consume_an_unrelated_context(self) -> None:
        for boundary in ("after_review_staging_write", "after_curation_promotion"):
            with self.subTest(boundary=boundary):
                run, selection, _ = self._review_source_fixture(
                    f"review-orphan-conflict-{boundary}"
                )
                first = core.create_source_review_context(run, selection)
                second = core.create_source_review_context(run, selection)
                with self.assertRaises(core.SimulatedCrash):
                    core.record_source_review(
                        run,
                        first["context_path"],
                        self._review_fixture(first),
                        fail_at=boundary,
                    )
                second_review = (run / second["context_path"]).parent / "review.json"
                self.assertFalse(second_review.exists())
                before = _tree_snapshot(run)

                with self.assertRaises(core.IntegrityError):
                    core.record_source_review(
                        run,
                        second["context_path"],
                        self._review_fixture(second),
                    )

                self.assertFalse(second_review.exists())
                self.assertEqual(_tree_snapshot(run), before)

    def test_persisted_pass_review_rejects_event_conflicts_before_catalog_writes(self) -> None:
        run, selection, crops = self._review_source_fixture(
            "review-persisted-event-conflict"
        )
        passing_context = core.create_source_review_context(run, selection)
        failing_context = core.create_source_review_context(run, selection)
        passing_review = self._review_fixture(passing_context)
        core.atomic_write_json(
            (run / passing_context["context_path"]).with_name("review.json"),
            passing_review,
        )
        failing_review = self._review_fixture(
            failing_context,
            verdict="fail",
            asset_findings=[{
                "asset_id": crops["method"]["asset_id"],
                "dimension": "importance",
                "finding": "The alternative selection needs another review.",
            }],
        )
        core.record_source_review(
            run,
            failing_context["context_path"],
            failing_review,
        )
        before = _tree_snapshot(run)

        with self.assertRaises(core.IntegrityError):
            core.record_source_review(
                run,
                passing_context["context_path"],
                passing_review,
            )

        self.assertEqual(_tree_snapshot(run), before)

    def test_persisted_pass_review_rejects_an_event_without_catalog_state(self) -> None:
        run, selection, _ = self._review_source_fixture(
            "review-event-before-catalog"
        )
        context = core.create_source_review_context(run, selection)
        review = self._review_fixture(context)
        core.atomic_write_json(
            (run / context["context_path"]).with_name("review.json"),
            review,
        )
        operation_id = core._review_operation_id(context, review)
        core.append_jsonl(
            run / "events.jsonl",
            {
                "event": "source_review_passed",
                "operation_id": operation_id,
                "revision": 1,
            },
        )
        before = _tree_snapshot(run)

        with self.assertRaises(core.IntegrityError):
            core.record_source_review(run, context["context_path"], review)

        self.assertEqual(_tree_snapshot(run), before)

    def test_v2_initialization_is_explicit_and_v1_default_is_unchanged(self) -> None:
        v1 = self.root / "runs" / "v1"
        v1_state = self._initialize(v1)
        self.assertEqual(
            v1_state,
            {
                "format_version": 1,
                "state": "initialized",
                "active_attempt": None,
                "attempt_count": 0,
                "skill_snapshot_manifest_sha256": core.sha256_file(
                    v1 / "skill_snapshot" / "manifest.json"
                ),
                "source_manifest_sha256": core.sha256_file(
                    v1 / "evidence" / "source_manifest.json"
                ),
            },
        )
        self.assertNotIn("run_format_version", v1_state)
        self.assertEqual(
            sorted(
                path.relative_to(v1).as_posix()
                for path in v1.rglob("*")
                if path.is_dir()
            ),
            [
                "attempts",
                "evidence",
                "evidence/assets",
                "evidence/pages",
                "evidence/reference_images",
                "input",
                "provenance",
                "skill_snapshot",
                "skill_snapshot/files",
                "skill_snapshot/files/references",
                "skill_snapshot/files/scripts",
            ],
        )

        v2 = self.root / "runs" / "v2"
        v2_state = self._initialize(v2, run_format_version=2)
        self.assertEqual(
            {key: v2_state[key] for key in (
                "run_format_version",
                "state",
                "active_curation_revision",
                "active_curation_sha256",
                "active_plan_revision",
                "active_plan_sha256",
            )},
            {
                "run_format_version": 2,
                "state": "initialized",
                "active_curation_revision": None,
                "active_curation_sha256": None,
                "active_plan_revision": None,
                "active_plan_sha256": None,
            },
        )
        for relative in (
            "source-assets/files",
            "source-assets/receipts",
            "source-reviews",
            "curations",
            "plans",
        ):
            self.assertTrue((v2 / relative).is_dir(), relative)
        self.assertEqual((v2 / "provenance" / "supersessions.jsonl").read_bytes(), b"")

    def test_inspect_and_diagnose_are_fail_closed_and_read_only(self) -> None:
        v1 = self.root / "runs" / "v1-diagnostic"
        state = self._initialize(v1)
        before = _tree_snapshot(v1)
        self.assertEqual(core.inspect_run_format(v1), 1)
        diagnosis = core.diagnose_v1_run(v1)
        self.assertEqual(
            {key: diagnosis[key] for key in (
                "mode", "run_format_version", "run_path", "state", "source_status",
            )},
            {
                "mode": "read_only",
                "run_format_version": 1,
                "run_path": ".",
                "state": state["state"],
                "source_status": "not_prepared",
            },
        )
        for key, value in diagnosis.items():
            if key.endswith("_path") and value is not None:
                self.assertIsInstance(value, str)
                self.assertFalse(Path(value).is_absolute())
        self.assertEqual(_tree_snapshot(v1), before)

        snapshot = v1 / "skill_snapshot"
        outside_snapshot = self.root / "untrusted-old-snapshot"
        outside_snapshot.mkdir()
        (outside_snapshot / "payload.py").write_text(
            "raise RuntimeError('must never execute')\n", encoding="utf-8"
        )
        for path in sorted(snapshot.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        snapshot.rmdir()
        snapshot.symlink_to(outside_snapshot, target_is_directory=True)
        with self.assertRaises(core.StateError):
            core.inspect_source(v1)
        before_crop_rejection = _tree_snapshot(v1.parent)
        with self.assertRaises(core.StateError):
            core.crop_source(v1, {})
        self.assertEqual(_tree_snapshot(v1.parent), before_crop_rejection)

        cases = {
            "missing": None,
            "bool": {"run_format_version": True},
            "unknown": {"run_format_version": 3},
            "malformed": b"{not-json\n",
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                run = self.root / "invalid" / name
                run.mkdir(parents=True)
                if isinstance(value, bytes):
                    (run / "run.json").write_bytes(value)
                elif value is not None:
                    (run / "run.json").write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises((core.IntegrityError, core.PathSafetyError)):
                    core.inspect_run_format(run)

        symlink_run = self.root / "invalid" / "symlink"
        symlink_run.mkdir(parents=True)
        outside = self.root / "outside-run.json"
        outside.write_text('{"run_format_version": 2}\n', encoding="utf-8")
        (symlink_run / "run.json").symlink_to(outside)
        with self.assertRaises(core.PathSafetyError):
            core.inspect_run_format(symlink_run)

        pdf_run = self.root / "runs" / "v2-pdf"
        self._initialize(pdf_run, run_format_version=2)
        source = self.root / "paper.pdf"
        source.write_bytes(b"%PDF-1.4\nimmutable fixture\n")
        tools, calls_path = self._fake_poppler("fake-poppler")
        manifest = core.prepare_source(pdf_run, source, tool_paths=tools)
        copied_source = pdf_run / "input" / "source.pdf"
        self.assertEqual(manifest["status"], "ready")
        self.assertEqual(json.loads((pdf_run / "run.json").read_text())["state"], "curating")
        for call in map(json.loads, calls_path.read_text(encoding="utf-8").splitlines()):
            source_argument = call["args"][-1] if call["name"] in {"pdfinfo"} or "-list" in call["args"] else call["args"][-2]
            self.assertEqual(Path(source_argument), copied_source)

        page_manifest = json.loads(
            (pdf_run / "evidence" / "page-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(page_manifest["source_sha256"], core.sha256_file(copied_source))
        self.assertEqual(
            [(page["page"], page["path"], page["width"], page["height"]) for page in page_manifest["pages"]],
            [
                (1, "evidence/pages/page-0001.png", 10, 6),
                (2, "evidence/pages/page-0002.png", 7, 5),
            ],
        )
        self.assertTrue(all(page["renderer"] == "pdftoppm" for page in page_manifest["pages"]))
        self.assertTrue(all(page["dpi"] == 144 for page in page_manifest["pages"]))
        self.assertTrue(all(page["pdf_page_box"] == "poppler_default" for page in page_manifest["pages"]))
        self.assertTrue(all(page["effective_rotation"] == 0 for page in page_manifest["pages"]))
        hints = json.loads(
            (pdf_run / "evidence" / "pdfimages-hints.json").read_text(encoding="utf-8")
        )["hints"]
        self.assertEqual(
            [(hint["page"], hint["object_number"], hint["trust"], hint["eligible"]) for hint in hints],
            [(2, 7, "untrusted_hint", False)],
        )

        inspected = core.inspect_source(pdf_run)
        self.assertEqual(inspected["source"]["path"], "input/source.pdf")
        self.assertEqual(inspected["source"]["sha256"], core.sha256_file(copied_source))
        self.assertEqual([page["page"] for page in inspected["pages"]], [1, 2])
        self.assertEqual(inspected["extraction_hints"][0]["trust"], "untrusted")

        text_run = self.root / "runs" / "v2-text"
        self._initialize(text_run, run_format_version=2)
        text_source = self.root / "paper.md"
        text_source.write_text("# Paper\n\nGrounded source.\n", encoding="utf-8")
        core.prepare_source(text_run, text_source)
        text_inspection = core.inspect_source(text_run)
        self.assertEqual(text_inspection["source"]["source_type"], "markdown")
        self.assertEqual(text_inspection["pages"], [])
        with self.assertRaises(core.StateError):
            core.crop_source(text_run, {})

    def test_agent_can_register_crop_missing_from_pdfimages_hints(self) -> None:
        run, inspection = self._prepare_pdf_run("crop-without-hint")
        self.assertEqual([hint["page"] for hint in inspection["extraction_hints"]], [2])
        request = self._crop_request(inspection)

        result = core.crop_source(run, request)

        self.assertRegex(result["asset_id"], r"^src-[0-9a-f]{24}$")
        self.assertEqual(result["bbox_pixels"], [1, 1, 6, 5])
        self.assertEqual(result["asset_path"], f"source-assets/files/{result['asset_id']}.png")
        self.assertEqual(
            result["receipt_path"],
            f"source-assets/receipts/{result['asset_id']}.json",
        )
        crop_info = portable_png.inspect_png((run / result["asset_path"]).read_bytes())
        self.assertEqual((crop_info["width"], crop_info["height"]), (5, 4))
        assets = core.list_source_assets(run)
        self.assertEqual(len(assets["extraction_hints"]), 1)
        self.assertEqual(len(assets["derived_assets"]), 1)
        self.assertFalse(assets["extraction_hints"][0]["eligible"])
        self.assertFalse(assets["derived_assets"][0]["eligible"])

    def test_v2_pdf_retry_preserves_immutable_source_and_replaces_only_uncommitted_outputs(self) -> None:
        run = self.root / "runs" / "pdf-retry"
        self._initialize(run, run_format_version=2)
        source = self.root / "pdf-retry.pdf"
        original = b"%PDF-1.4\nretry source\n"
        source.write_bytes(original)
        blocked = core.prepare_source(
            run,
            source,
            tool_paths={
                "pdftotext": None,
                "pdfinfo": None,
                "pdftoppm": None,
                "pdfimages": None,
            },
        )
        self.assertEqual(blocked["status"], "blocked")
        copied = run / "input" / "source.pdf"
        self.assertEqual(copied.read_bytes(), original)
        (run / "evidence" / "pages" / "page-999.png").write_bytes(_png(1, 1, 1))
        (run / "evidence" / "assets" / "pdf-image-stale.png").write_bytes(_png(1, 1, 1))

        source.write_bytes(b"%PDF-1.4\ndifferent source\n")
        before_replacement_attempt = _tree_snapshot(run)
        tools, _calls = self._fake_poppler("poppler-pdf-retry")
        with self.assertRaises(core.StateError):
            core.prepare_source(run, source, tool_paths=tools)
        self.assertEqual(_tree_snapshot(run), before_replacement_attempt)

        source.write_bytes(original)
        ready = core.prepare_source(run, source, tool_paths=tools)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(copied.read_bytes(), original)
        self.assertEqual(
            sorted(path.name for path in (run / "evidence" / "pages").iterdir()),
            ["page-0001.png", "page-0002.png"],
        )
        self.assertNotIn(
            "pdf-image-stale.png",
            {path.name for path in (run / "evidence" / "assets").iterdir()},
        )
        before_ready_retry = _tree_snapshot(run)
        with self.assertRaises(core.StateError):
            core.prepare_source(run, source, tool_paths=tools)
        self.assertEqual(_tree_snapshot(run), before_ready_retry)

    def test_crop_registry_is_hash_bound_idempotent_and_append_only(self) -> None:
        run, inspection = self._prepare_pdf_run("crop-registry")
        request = self._crop_request(inspection)
        first = core.crop_source(run, request)
        event_count = len((run / "events.jsonl").read_text(encoding="utf-8").splitlines())
        second = core.crop_source(run, request)
        self.assertEqual(second, first)
        self.assertEqual(
            len((run / "events.jsonl").read_text(encoding="utf-8").splitlines()),
            event_count,
        )

        variants = (
            {"bbox_normalized": [0.12, 0.20, 0.51, 0.70]},
            {"role": "result"},
            {"claim": "The primary result is shown in this page region."},
            {"max_reuse": 2},
        )
        results = [core.crop_source(run, self._crop_request(inspection, **change)) for change in variants]
        self.assertEqual(len({first["asset_id"], *(item["asset_id"] for item in results)}), 5)
        self.assertEqual(len(list((run / "source-assets" / "files").iterdir())), 5)
        self.assertEqual(len(list((run / "source-assets" / "receipts").iterdir())), 5)

        receipt = json.loads((run / first["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["source_sha256"], request["source_sha256"])
        self.assertEqual(receipt["page_manifest_sha256"], request["page_manifest_sha256"])
        self.assertEqual(receipt["page_sha256"], request["page_sha256"])
        self.assertEqual(receipt["semantic_request"], {
            "role": request["role"],
            "claim": request["claim"],
            "max_reuse": request["max_reuse"],
        })
        receipt_hash = receipt.pop("receipt_sha256")
        canonical_receipt = (
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        self.assertEqual(receipt_hash, hashlib.sha256(canonical_receipt).hexdigest())

        for mutation in (
            {"extra": "field"},
            {"source_sha256": "0" * 64},
            {"page_manifest_sha256": "0" * 64},
            {"page_sha256": "0" * 64},
        ):
            bad = dict(request)
            bad.update(mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaises((core.ContractError, core.IntegrityError)):
                    core.crop_source(run, bad)

        receipt_path = run / first["receipt_path"]
        tampered = json.loads(receipt_path.read_text(encoding="utf-8"))
        tampered["semantic_request"]["role"] = "tampered"
        core.atomic_write_json(receipt_path, tampered)
        with self.assertRaises(core.IntegrityError):
            core.list_source_assets(run)

    def test_crop_registry_fails_closed_without_outside_mutation(self) -> None:
        run, inspection = self._prepare_pdf_run("crop-failures")
        request = self._crop_request(inspection)
        outside = self.root / "outside"
        outside.mkdir()
        before_outside = _tree_snapshot(outside)
        invalid = (
            {"page": 0},
            {"page": 3},
            {"bbox_normalized": [0.5, 0.2, 0.5, 0.7]},
            {"bbox_normalized": [-0.1, 0.2, 0.5, 0.7]},
            {"role": ""},
            {"claim": ""},
            {"max_reuse": 0},
            {"max_reuse": True},
        )
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises((core.ContractError, core.IntegrityError)):
                    core.crop_source(run, self._crop_request(inspection, **change))
                self.assertEqual(_tree_snapshot(outside), before_outside)

        request_file = self.root / "noncanonical-request.json"
        request_file.write_text(json.dumps(request, indent=4), encoding="utf-8")
        with mock.patch.object(
            core,
            "crop_source",
            side_effect=AssertionError("noncanonical file reached Mapping-level API"),
        ):
            with self.assertRaises(core.ContractError):
                core.crop_source_from_file(run, request_file)

        symlink_run, symlink_inspection = self._prepare_pdf_run("crop-symlink")
        files = symlink_run / "source-assets" / "files"
        files.rmdir()
        files.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(core.PathSafetyError):
            core.crop_source(symlink_run, self._crop_request(symlink_inspection))
        self.assertEqual(_tree_snapshot(outside), before_outside)

        hardlink_run, hardlink_inspection = self._prepare_pdf_run("crop-hardlink")
        page = hardlink_run / "evidence" / "pages" / "page-0001.png"
        outside_page = outside / "page.png"
        outside_page.write_bytes(page.read_bytes())
        page.unlink()
        os.link(outside_page, page)
        outside_with_page = _tree_snapshot(outside)
        with self.assertRaises(core.PathSafetyError):
            core.crop_source(hardlink_run, self._crop_request(hardlink_inspection))
        self.assertEqual(_tree_snapshot(outside), outside_with_page)

        for fail_at in (
            "after_staged_asset_write",
            "after_staged_receipt_write",
            "after_asset_promotion",
        ):
            with self.subTest(fail_at=fail_at):
                crash_run, crash_inspection = self._prepare_pdf_run(f"crash-{fail_at}")
                crash_request = self._crop_request(crash_inspection)
                with self.assertRaises(core.SimulatedCrash):
                    core.crop_source(crash_run, crash_request, fail_at=fail_at)
                recovered = core.crop_source(crash_run, crash_request)
                self.assertTrue((crash_run / recovered["asset_path"]).is_file())
                self.assertTrue((crash_run / recovered["receipt_path"]).is_file())
                self.assertFalse(any((crash_run / "source-assets").glob(".crop-staging-*")))
                events = [
                    json.loads(line)
                    for line in (crash_run / "events.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(
                    sum(event.get("event") == "source_crop_registered" for event in events),
                    1,
                )

        thread_run, thread_inspection = self._prepare_pdf_run("crop-threads")
        same = self._crop_request(thread_inspection)
        different = self._crop_request(thread_inspection, role="result")
        with ThreadPoolExecutor(max_workers=6) as executor:
            thread_results = list(
                executor.map(lambda item: core.crop_source(thread_run, item), [same] * 5 + [different])
            )
        self.assertEqual(len({item["asset_id"] for item in thread_results}), 2)
        self.assertEqual(len(list((thread_run / "source-assets" / "files").iterdir())), 2)
        self.assertEqual(len(list((thread_run / "source-assets" / "receipts").iterdir())), 2)

        process_run, process_inspection = self._prepare_pdf_run("crop-processes")
        process_requests = [
            self._crop_request(process_inspection),
            self._crop_request(process_inspection),
            self._crop_request(process_inspection, claim="A distinct process claim."),
        ]
        processes = []
        for process_request in process_requests:
            code = (
                "import json,sys; from agent_skills._shared import portable_core as c; "
                "print(json.dumps(c.crop_source(sys.argv[1], json.loads(sys.argv[2]))))"
            )
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", code, str(process_run), json.dumps(process_request)],
                    cwd=Path(__file__).resolve().parents[1],
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        process_results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            process_results.append(json.loads(stdout))
        self.assertEqual(len({item["asset_id"] for item in process_results}), 2)
        self.assertEqual(len(list((process_run / "source-assets" / "files").iterdir())), 2)
        self.assertEqual(len(list((process_run / "source-assets" / "receipts").iterdir())), 2)
        process_events = [
            json.loads(line)
            for line in (process_run / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            sum(event.get("event") == "source_crop_registered" for event in process_events),
            2,
        )

    def test_v2_prepare_validates_the_run_tree_and_source_cas_before_writes(self) -> None:
        source = self.root / "safety.md"
        source.write_text("# Safe source\n", encoding="utf-8")

        hardlink_run = self.root / "runs" / "prepare-hardlink"
        self._initialize(hardlink_run, run_format_version=2)
        events_path = hardlink_run / "events.jsonl"
        outside_events = self.root / "outside-events.jsonl"
        outside_events.write_bytes(events_path.read_bytes())
        events_path.unlink()
        os.link(outside_events, events_path)
        before_outside = outside_events.read_bytes()
        with self.assertRaises(core.PathSafetyError):
            core.prepare_source(hardlink_run, source)
        self.assertEqual(outside_events.read_bytes(), before_outside)
        self.assertFalse((hardlink_run / "input" / "source.md").exists())

        cas_run = self.root / "runs" / "prepare-source-cas"
        self._initialize(cas_run, run_format_version=2)
        source_manifest = cas_run / "evidence" / "source_manifest.json"
        source_manifest.write_text(
            '{"format_version":1,"status":"tampered"}\n', encoding="utf-8"
        )
        before_run = _tree_snapshot(cas_run)
        with self.assertRaises(core.IntegrityError):
            core.prepare_source(cas_run, source)
        self.assertEqual(_tree_snapshot(cas_run), before_run)

    def test_v2_prepare_recovers_every_commit_boundary_with_exactly_one_event(self) -> None:
        source = self.root / "recovery.md"
        source.write_text("# Recoverable source\n\nOne grounded claim.\n", encoding="utf-8")
        boundaries = (
            "after_source_outputs_staged",
            "after_source_manifest_promotion",
            "after_source_run_update",
            "after_source_prepared_event",
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                run = self.root / "runs" / f"prepare-crash-{boundary}"
                self._initialize(run, run_format_version=2)
                with self.assertRaises(core.SimulatedCrash):
                    core.prepare_source(run, source, fail_at=boundary)
                recovered = core.prepare_source(run, source)
                state = json.loads((run / "run.json").read_text(encoding="utf-8"))
                self.assertEqual((recovered["status"], state["state"]), ("ready", "curating"))
                self.assertEqual(
                    state["source_manifest_sha256"],
                    core.sha256_file(run / "evidence" / "source_manifest.json"),
                )
                self.assertFalse((run / ".source-prep-staging").exists())
                self.assertEqual(
                    sum(event.get("event") == "source_prepared" for event in _events(run)),
                    1,
                )
                before_rejected_retry = _tree_snapshot(run)
                with self.assertRaises(core.StateError):
                    core.prepare_source(run, source)
                self.assertEqual(_tree_snapshot(run), before_rejected_retry)

        blocked_run = self.root / "runs" / "prepare-blocker-event-crash"
        self._initialize(blocked_run, run_format_version=2)
        pdf = self.root / "recover-blocked.pdf"
        pdf.write_bytes(b"%PDF-1.4\nblocked then ready\n")
        blocked = core.prepare_source(
            blocked_run,
            pdf,
            tool_paths={name: None for name in ("pdftotext", "pdfinfo", "pdftoppm", "pdfimages")},
        )
        self.assertEqual(blocked["status"], "blocked")
        tools, _calls = self._fake_poppler("recover-blocked-poppler")
        with self.assertRaises(core.SimulatedCrash):
            core.prepare_source(
                blocked_run,
                pdf,
                tool_paths=tools,
                fail_at="after_source_blocker_resolved_event",
            )
        recovered = core.prepare_source(blocked_run, pdf, tool_paths=tools)
        self.assertEqual(recovered["status"], "ready")
        names = [event.get("event") for event in _events(blocked_run)]
        self.assertEqual(names.count("source_blocker_resolved"), 1)
        self.assertEqual(names.count("source_prepared"), 1)
        self.assertFalse((blocked_run / ".source-prep-staging").exists())

    def test_v2_initialization_recovers_crashes_without_a_partial_live_run(self) -> None:
        boundaries = (
            "after_init_outputs_staged",
            "after_init_run_write",
            "after_init_event_append",
            "after_init_promotion",
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                run = self.root / "runs" / f"init-crash-{boundary}"
                with self.assertRaises(core.SimulatedCrash):
                    core.initialize_run(
                        run,
                        self.skill,
                        release_version="0.1.0",
                        archive_sha256="a" * 64,
                        run_format_version=2,
                        fail_at=boundary,
                    )
                if boundary == "after_init_promotion":
                    self.assertTrue(run.is_dir())
                else:
                    self.assertFalse(run.exists())
                recovered = self._initialize(run, run_format_version=2)
                self.assertEqual(core.inspect_run_format(run), 2)
                self.assertEqual(recovered["format_version"], 1)
                self.assertEqual(
                    recovered["skill_snapshot_manifest_sha256"],
                    core.sha256_file(run / "skill_snapshot" / "manifest.json"),
                )
                self.assertEqual(
                    sum(event.get("event") == "run_initialized" for event in _events(run)),
                    1,
                )
                self.assertFalse(
                    (run.parent / f".{run.name}.v2-init-staging").exists()
                )

    def test_v2_initialization_serializes_threads_and_processes(self) -> None:
        thread_run = self.root / "runs" / "init-threads"
        with ThreadPoolExecutor(max_workers=6) as executor:
            states = list(
                executor.map(
                    lambda _index: self._initialize(thread_run, run_format_version=2),
                    range(6),
                )
            )
        self.assertTrue(all(state == states[0] for state in states))
        self.assertEqual(
            sum(event.get("event") == "run_initialized" for event in _events(thread_run)),
            1,
        )
        core.verify_skill_snapshot(thread_run, skill_root=self.skill)

        process_run = self.root / "runs" / "init-processes"
        code = (
            "import json,sys; from agent_skills._shared import portable_core as c; "
            "print(json.dumps(c.initialize_run(sys.argv[1], sys.argv[2], "
            "release_version='0.1.0', archive_sha256='a'*64, run_format_version=2)))"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(process_run), str(self.skill)],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _index in range(3)
        ]
        process_states = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            process_states.append(json.loads(stdout))
        self.assertTrue(all(state == process_states[0] for state in process_states))
        self.assertEqual(
            sum(event.get("event") == "run_initialized" for event in _events(process_run)),
            1,
        )
        self.assertEqual(core.inspect_run_format(process_run), 2)
        core.verify_skill_snapshot(process_run, skill_root=self.skill)

    def test_v2_initialization_staging_rejects_a_nonexact_file_set(self) -> None:
        init_run = self.root / "runs" / "init-stage-extra"
        with self.assertRaises(core.SimulatedCrash):
            core.initialize_run(
                init_run,
                self.skill,
                release_version="0.1.0",
                archive_sha256="a" * 64,
                run_format_version=2,
                fail_at="after_init_event_append",
            )
        init_stage = init_run.parent / f".{init_run.name}.v2-init-staging"
        (init_stage / "unexpected.bin").write_bytes(b"not part of the transaction")
        with self.assertRaises(core.IntegrityError):
            self._initialize(init_run, run_format_version=2)
        self.assertFalse(init_run.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_v2_initialization_staging_rejects_special_entries(self) -> None:
        run = self.root / "runs" / "init-stage-fifo"
        with self.assertRaises(core.SimulatedCrash):
            core.initialize_run(
                run,
                self.skill,
                release_version="0.1.0",
                archive_sha256="a" * 64,
                run_format_version=2,
                fail_at="after_init_event_append",
            )
        stage = run.parent / f".{run.name}.v2-init-staging"
        os.mkfifo(stage / "rogue.pipe")

        with self.assertRaises(core.PathSafetyError):
            self._initialize(run, run_format_version=2)

        self.assertFalse(run.exists())
        self.assertTrue((stage / "rogue.pipe").exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_v2_initialization_live_run_rejects_special_entries(self) -> None:
        run = self.root / "runs" / "init-live-fifo"
        self._initialize(run, run_format_version=2)
        os.mkfifo(run / "rogue.pipe")

        with self.assertRaises(core.PathSafetyError):
            self._initialize(run, run_format_version=2)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_v2_initialization_quarantines_a_promotion_boundary_mutation(self) -> None:
        run = self.root / "runs" / "init-promotion-mutation"
        stage = run.parent / f".{run.name}.v2-init-staging"
        real_replace = os.replace
        injected = False

        def replace_with_late_fifo(source: object, target: object) -> None:
            nonlocal injected
            if Path(source) == stage and Path(target) == run and not injected:
                injected = True
                os.mkfifo(stage / "late.pipe")
            real_replace(source, target)

        with mock.patch.object(core.os, "replace", side_effect=replace_with_late_fifo):
            with self.assertRaises(core.PathSafetyError):
                self._initialize(run, run_format_version=2)

        self.assertTrue(injected)
        self.assertFalse(run.exists())
        quarantines = list(
            run.parent.glob(f".{run.name}.v2-init-quarantine-*")
        )
        self.assertEqual(len(quarantines), 1)
        self.assertTrue((quarantines[0] / "late.pipe").exists())

        state = self._initialize(run, run_format_version=2)

        self.assertEqual(state["state"], "initialized")
        self.assertTrue((run / ".initialization-seal.json").is_file())
        self.assertEqual(
            sum(event.get("event") == "run_initialized" for event in _events(run)),
            1,
        )

    def test_v2_initialization_drift_does_not_quarantine_a_safe_live_run(self) -> None:
        run = self.root / "runs" / "init-installed-drift"
        self._initialize(run, run_format_version=2)
        (self.skill / "scripts" / "tool.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )

        with self.assertRaises(core.IntegrityError):
            self._initialize(run, run_format_version=2)

        self.assertTrue(run.is_dir())
        self.assertEqual(
            list(run.parent.glob(f".{run.name}.v2-init-quarantine-*")),
            [],
        )

    def test_v2_initialization_never_overwrites_a_conflicting_quarantine(self) -> None:
        run = self.root / "runs" / "init-quarantine-conflict"
        stage = run.parent / f".{run.name}.v2-init-staging"
        real_replace = os.replace
        quarantine: Path | None = None

        def replace_with_conflict(source: object, target: object) -> None:
            nonlocal quarantine
            if Path(source) == stage and Path(target) == run and quarantine is None:
                seal = json.loads(
                    (stage / ".initialization-seal.json").read_text(encoding="utf-8")
                )
                quarantine = run.parent / (
                    f".{run.name}.v2-init-quarantine-{seal['generation_id']}"
                )
                quarantine.mkdir()
                (quarantine / "owner.txt").write_text("do not overwrite\n", encoding="utf-8")
                state = json.loads((stage / "run.json").read_text(encoding="utf-8"))
                state["state"] = "curating"
                (stage / "run.json").write_bytes(core._stored_json_bytes(state))
            real_replace(source, target)

        with mock.patch.object(core.os, "replace", side_effect=replace_with_conflict):
            with self.assertRaises(core.IntegrityError):
                self._initialize(run, run_format_version=2)

        self.assertIsNotNone(quarantine)
        assert quarantine is not None
        self.assertEqual(
            (quarantine / "owner.txt").read_text(encoding="utf-8"),
            "do not overwrite\n",
        )
        self.assertFalse(run.exists())
        quarantines = list(
            run.parent.glob(f".{run.name}.v2-init-quarantine-*")
        )
        self.assertEqual(len(quarantines), 2)
        fresh_quarantine = next(path for path in quarantines if path != quarantine)
        self.assertEqual(
            json.loads(
                (fresh_quarantine / "run.json").read_text(encoding="utf-8")
            )["state"],
            "curating",
        )

        state = self._initialize(run, run_format_version=2)
        self.assertEqual(state["state"], "initialized")

    def test_v2_prepare_waits_for_initialization_promotion_validation(self) -> None:
        run = self.root / "runs" / "init-prepare-lock-order"
        stage = run.parent / f".{run.name}.v2-init-staging"
        source = self.root / "init-prepare-lock-order.md"
        source.write_text("# Serialized after initialization\n", encoding="utf-8")
        real_replace = os.replace
        real_prepare = core._prepare_source_v2_transaction
        promoted = threading.Event()
        release_initialization = threading.Event()
        prepare_called = threading.Event()
        prepare_entered = threading.Event()

        def replace_and_pause(source_path: object, target_path: object) -> None:
            real_replace(source_path, target_path)
            if Path(source_path) == stage and Path(target_path) == run:
                promoted.set()
                if not release_initialization.wait(timeout=10):
                    raise AssertionError("initialization promotion pause timed out")

        def mark_prepare_entry(*args: object, **kwargs: object) -> dict[str, object]:
            prepare_entered.set()
            return real_prepare(*args, **kwargs)

        def prepare() -> dict[str, object]:
            prepare_called.set()
            return core.prepare_source(run, source)

        initialization_state: dict[str, object] | None = None
        prepared_manifest: dict[str, object] | None = None
        initialization_error: BaseException | None = None
        prepare_error: BaseException | None = None
        with mock.patch.object(core.os, "replace", side_effect=replace_and_pause), mock.patch.object(
            core,
            "_prepare_source_v2_transaction",
            side_effect=mark_prepare_entry,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                initialization = executor.submit(
                    self._initialize, run, run_format_version=2
                )
                self.assertTrue(promoted.wait(timeout=5))
                preparation = executor.submit(prepare)
                self.assertTrue(prepare_called.wait(timeout=5))
                prepare_was_blocked = not prepare_entered.wait(timeout=1)
                release_initialization.set()
                try:
                    initialization_state = initialization.result(timeout=10)
                except BaseException as error:
                    initialization_error = error
                try:
                    prepared_manifest = preparation.result(timeout=10)
                except BaseException as error:
                    prepare_error = error

        self.assertTrue(
            prepare_was_blocked,
            "prepare_source entered its transaction before initialization validation",
        )
        if initialization_error is not None:
            raise initialization_error
        if prepare_error is not None:
            raise prepare_error
        assert initialization_state is not None
        assert prepared_manifest is not None
        self.assertEqual(initialization_state["state"], "initialized")
        self.assertEqual(prepared_manifest["status"], "ready")
        self.assertEqual(core.inspect_source(run)["next_action"], "curate_source")
        self.assertEqual(
            json.loads((run / "run.json").read_text(encoding="utf-8"))["state"],
            "curating",
        )

    def test_v2_source_staging_rejects_a_nonexact_file_set(self) -> None:
        source_run = self.root / "runs" / "source-stage-extra"
        self._initialize(source_run, run_format_version=2)
        source = self.root / "source-stage-extra.md"
        source.write_text("# Exact staged source\n", encoding="utf-8")
        with self.assertRaises(core.SimulatedCrash):
            core.prepare_source(
                source_run, source, fail_at="after_source_outputs_staged"
            )
        (source_run / ".source-prep-staging" / "unexpected.bin").write_bytes(
            b"not part of the transaction"
        )
        with self.assertRaises(core.IntegrityError):
            core.prepare_source(source_run, source)
        state = json.loads((source_run / "run.json").read_text(encoding="utf-8"))
        manifest = json.loads(
            (source_run / "evidence" / "source_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual((state["state"], manifest["status"]), ("initialized", "not_prepared"))

    def test_v2_source_stage_mkdir_crash_recovers_exactly_once(self) -> None:
        run = self.root / "runs" / "source-stage-mkdir-crash"
        self._initialize(run, run_format_version=2)
        source = self.root / "source-stage-mkdir-crash.md"
        source.write_text("# Recover an incomplete source transaction\n", encoding="utf-8")

        with self.assertRaises(core.SimulatedCrash):
            core.prepare_source(run, source, fail_at="after_source_stage_mkdir")
        stage = run / ".source-prep-staging"
        self.assertTrue(stage.is_dir())
        self.assertEqual(list(stage.iterdir()), [])

        manifest = core.prepare_source(run, source)

        self.assertEqual(manifest["status"], "ready")
        self.assertFalse(stage.exists())
        self.assertEqual(
            sum(event.get("event") == "source_prepared" for event in _events(run)),
            1,
        )

    def test_v2_source_empty_staging_is_discarded_and_reseeded(self) -> None:
        run = self.root / "runs" / "source-empty-stage"
        self._initialize(run, run_format_version=2)
        source = self.root / "source-empty-stage.md"
        source.write_text("# Recover a pre-transaction stage\n", encoding="utf-8")
        stage = run / ".source-prep-staging"
        stage.mkdir()

        manifest = core.prepare_source(run, source)

        self.assertEqual(manifest["status"], "ready")
        self.assertFalse(stage.exists())
        self.assertEqual(
            sum(event.get("event") == "source_prepared" for event in _events(run)),
            1,
        )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_v2_source_empty_stage_late_mutation_blocks_cleanup_and_commit(self) -> None:
        run = self.root / "runs" / "source-empty-stage-late-mutation"
        self._initialize(run, run_format_version=2)
        source = self.root / "source-empty-stage-late-mutation.md"
        source.write_text("# Do not commit after unsafe cleanup\n", encoding="utf-8")
        stage = run / ".source-prep-staging"
        stage.mkdir()
        real_rmdir = Path.rmdir
        injected = False

        def rmdir_with_late_fifo(path: Path) -> None:
            nonlocal injected
            if path == stage and not injected:
                injected = True
                os.mkfifo(stage / "late.pipe")
            real_rmdir(path)

        with mock.patch.object(Path, "rmdir", new=rmdir_with_late_fifo):
            with self.assertRaises(core.PathSafetyError):
                core.prepare_source(run, source)

        self.assertTrue(injected)
        self.assertTrue((stage / "late.pipe").exists())
        self.assertEqual(
            json.loads((run / "run.json").read_text(encoding="utf-8"))["state"],
            "initialized",
        )
        self.assertFalse((run / "input" / "source.md").exists())
        self.assertEqual(
            sum(event.get("event") == "source_prepared" for event in _events(run)),
            0,
        )

    def test_v2_source_partial_seed_crash_recovers_exactly_once(self) -> None:
        run = self.root / "runs" / "source-partial-seed-crash"
        self._initialize(run, run_format_version=2)
        source = self.root / "source-partial-seed-crash.md"
        source.write_text("# Recover a legitimate partial seed\n", encoding="utf-8")

        with self.assertRaises(core.SimulatedCrash):
            core.prepare_source(run, source, fail_at="after_source_seed_input")
        stage = run / ".source-prep-staging"
        self.assertTrue((stage / "input" / "source.md").is_file())
        self.assertFalse((stage / "transaction.json").exists())

        manifest = core.prepare_source(run, source)

        self.assertEqual(manifest["status"], "ready")
        self.assertFalse(stage.exists())
        self.assertEqual(
            sum(event.get("event") == "source_prepared" for event in _events(run)),
            1,
        )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_v2_source_partial_stage_late_mutation_is_quarantined(self) -> None:
        run = self.root / "runs" / "source-partial-stage-late-mutation"
        self._initialize(run, run_format_version=2)
        source = self.root / "source-partial-stage-late-mutation.md"
        source.write_text("# Quarantine a changed partial seed\n", encoding="utf-8")
        with self.assertRaises(core.SimulatedCrash):
            core.prepare_source(run, source, fail_at="after_source_seed_input")
        stage = run / ".source-prep-staging"
        real_replace = os.replace
        injected = False

        def replace_with_late_fifo(source_path: object, target: object) -> None:
            nonlocal injected
            target_path = Path(target)
            if (
                Path(source_path) == stage
                and target_path.parent == run
                and target_path.name.startswith(".source-prep-quarantine-")
                and not injected
            ):
                injected = True
                os.mkfifo(stage / "late.pipe")
            real_replace(source_path, target)

        with mock.patch.object(core.os, "replace", side_effect=replace_with_late_fifo):
            with self.assertRaises(core.PathSafetyError):
                core.prepare_source(run, source)

        self.assertTrue(injected)
        self.assertFalse(stage.exists())
        quarantines = list(run.glob(".source-prep-quarantine-*"))
        self.assertEqual(len(quarantines), 1)
        self.assertTrue((quarantines[0] / "late.pipe").exists())
        self.assertEqual(
            json.loads((run / "run.json").read_text(encoding="utf-8"))["state"],
            "initialized",
        )
        self.assertEqual(
            sum(event.get("event") == "source_prepared" for event in _events(run)),
            0,
        )

    def test_v2_source_partial_stage_is_bound_to_the_original_request(self) -> None:
        run = self.root / "runs" / "source-partial-stage-request-binding"
        self._initialize(run, run_format_version=2)
        source = self.root / "source-partial-stage-request-binding.md"
        source.write_text("# Original request\n", encoding="utf-8")
        with self.assertRaises(core.SimulatedCrash):
            core.prepare_source(run, source, fail_at="after_source_seed_input")
        source.write_text("# Changed request\n", encoding="utf-8")
        before = _tree_snapshot(run)

        with self.assertRaises(core.IntegrityError):
            core.prepare_source(run, source)

        self.assertEqual(_tree_snapshot(run), before)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_v2_source_incomplete_staging_rejects_special_entries(self) -> None:
        run = self.root / "runs" / "source-stage-fifo"
        self._initialize(run, run_format_version=2)
        source = self.root / "source-stage-fifo.md"
        source.write_text("# Reject an unsafe source transaction\n", encoding="utf-8")
        stage = run / ".source-prep-staging"
        stage.mkdir()
        os.mkfifo(stage / "rogue.pipe")
        before = _tree_snapshot(run)

        with self.assertRaises(core.PathSafetyError):
            core.prepare_source(run, source)

        self.assertEqual(_tree_snapshot(run), before)

    def test_inspect_run_format_rejects_ambiguous_or_noncanonical_contracts(self) -> None:
        accepted = (
            ({"format_version": 1}, 1),
            ({"format_version": 1, "run_format_version": 2}, 2),
        )
        for index, (contract, expected) in enumerate(accepted):
            with self.subTest(accepted=contract):
                run = self.root / "format-contracts" / f"accepted-{index}"
                run.mkdir(parents=True)
                (run / "run.json").write_text(json.dumps(contract), encoding="utf-8")
                self.assertEqual(core.inspect_run_format(run), expected)

        rejected = (
            {},
            {"format_version": 2},
            {"run_format_version": 2},
            {"format_version": 2, "run_format_version": 1},
            {"format_version": 1, "run_format_version": 1},
            {"format_version": True},
            {"format_version": 1, "run_format_version": True},
            {"format_version": 1, "run_format_version": 3},
        )
        for index, contract in enumerate(rejected):
            with self.subTest(rejected=contract):
                run = self.root / "format-contracts" / f"rejected-{index}"
                run.mkdir(parents=True)
                (run / "run.json").write_text(json.dumps(contract), encoding="utf-8")
                with self.assertRaises(core.IntegrityError):
                    core.inspect_run_format(run)

    def test_v2_pdf_rejects_structurally_incomplete_png_pages_before_publication(self) -> None:
        valid = _png(2, 2, 40)
        header = struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0)
        raw = b"\0" + bytes([40, 40, 40, 255]) * 2
        malformed = {
            "truncated-after-ihdr-prefix": valid[:24],
            "bad-crc": valid[:-1] + bytes([valid[-1] ^ 1]),
            "missing-iend": valid[:-12],
            "trailing-data": valid + b"trailing",
            "incomplete-idat-stream": (
                PNG_SIGNATURE
                + _chunk(b"IHDR", header)
                + _chunk(b"IDAT", zlib.compress(raw * 2)[:-1])
                + _chunk(b"IEND", b"")
            ),
            "dimension-data-mismatch": (
                PNG_SIGNATURE
                + _chunk(b"IHDR", header)
                + _chunk(b"IDAT", zlib.compress(raw))
                + _chunk(b"IEND", b"")
            ),
            "invalid-filter": (
                PNG_SIGNATURE
                + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
                + _chunk(b"IDAT", zlib.compress(b"\5" + bytes([1, 2, 3, 255])))
                + _chunk(b"IEND", b"")
            ),
        }
        for name, page_bytes in malformed.items():
            with self.subTest(name=name):
                run = self.root / "runs" / f"bad-png-{name}"
                self._initialize(run, run_format_version=2)
                source = self.root / f"bad-png-{name}.pdf"
                source.write_bytes(b"%PDF-1.4\ninvalid rendered page\n")
                tools, _calls = self._fake_poppler(f"bad-png-tools-{name}")
                (tools["pdftoppm"].parent / "first-page.png").write_bytes(page_bytes)
                with self.assertRaises(core.IntegrityError):
                    core.prepare_source(run, source, tool_paths=tools)
                state = json.loads((run / "run.json").read_text(encoding="utf-8"))
                manifest = json.loads(
                    (run / "evidence" / "source_manifest.json").read_text(encoding="utf-8")
                )
                self.assertEqual((state["state"], manifest["status"]), ("initialized", "not_prepared"))
                self.assertEqual(
                    state["source_manifest_sha256"],
                    core.sha256_file(run / "evidence" / "source_manifest.json"),
                )
                self.assertEqual(list((run / "evidence" / "pages").iterdir()), [])
                self.assertFalse((run / "evidence" / "page-manifest.json").exists())
                self.assertFalse((run / ".source-prep-staging").exists())

    def test_png_input_identity_and_size_are_checked_before_reading(self) -> None:
        oversized = self.root / "oversized.png"
        oversized.touch()
        os.truncate(oversized, 512 * 1024 * 1024 + 1)
        with (
            mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("oversized PNG reached the allocation path"),
            ),
            mock.patch.object(
                os,
                "open",
                side_effect=AssertionError("oversized PNG was opened before rejection"),
            ),
        ):
            with self.assertRaises(core.IntegrityError):
                core._png_dimensions(oversized)

        valid = self.root / "valid.png"
        valid.write_bytes(_png(1, 1, 90))
        hardlink = self.root / "hardlinked.png"
        os.link(valid, hardlink)
        with self.assertRaises(core.PathSafetyError):
            core._png_dimensions(hardlink)

        symlink = self.root / "symlinked.png"
        symlink.symlink_to(valid)
        with self.assertRaises(core.PathSafetyError):
            core._png_dimensions(symlink)

    def test_v2_tree_inventory_uses_fresh_metadata_on_windows(self) -> None:
        tree = self.root / "windows-like-inventory"
        tree.mkdir()
        (tree / "file.txt").write_text("single link\n", encoding="utf-8")
        real_scandir = os.scandir

        class WindowsLikeEntry:
            def __init__(self, entry: os.DirEntry[str]) -> None:
                self.name = entry.name
                self._entry = entry

            def stat(self, *, follow_symlinks: bool = True) -> object:
                details = self._entry.stat(follow_symlinks=follow_symlinks)
                return mock.Mock(
                    st_mode=details.st_mode,
                    st_dev=0,
                    st_ino=0,
                    st_nlink=0,
                )

        def windows_like_scandir(path: object) -> list[WindowsLikeEntry]:
            return [WindowsLikeEntry(entry) for entry in real_scandir(path)]

        with mock.patch.object(core.os, "scandir", side_effect=windows_like_scandir):
            files, directories = core._regular_tree_inventory(tree)

        self.assertEqual(files, {"file.txt"})
        self.assertEqual(directories, set())

    def test_v2_tree_inventory_rejects_links_and_windows_reparse_entries(self) -> None:
        hardlink_tree = self.root / "inventory-hardlink"
        hardlink_tree.mkdir()
        original = hardlink_tree / "original.txt"
        original.write_text("two links\n", encoding="utf-8")
        os.link(original, hardlink_tree / "second.txt")
        with self.assertRaises(core.PathSafetyError):
            core._regular_tree_inventory(hardlink_tree)

        symlink_tree = self.root / "inventory-symlink"
        symlink_tree.mkdir()
        target = symlink_tree / "target.txt"
        target.write_text("target\n", encoding="utf-8")
        (symlink_tree / "alias.txt").symlink_to(target)
        with self.assertRaises(core.PathSafetyError):
            core._regular_tree_inventory(symlink_tree)

        reparse_tree = self.root / "inventory-reparse"
        reparse_tree.mkdir()
        reparse = reparse_tree / "junction-like"
        reparse.mkdir()
        real_stat = os.stat

        def windows_reparse_stat(
            path: object, *, follow_symlinks: bool = True
        ) -> object:
            details = real_stat(path, follow_symlinks=follow_symlinks)
            if Path(path) == reparse:
                return mock.Mock(
                    st_mode=details.st_mode,
                    st_dev=details.st_dev,
                    st_ino=details.st_ino,
                    st_nlink=details.st_nlink,
                    st_file_attributes=0x400,
                )
            return details

        with mock.patch.object(core.os, "stat", side_effect=windows_reparse_stat):
            with self.assertRaises(core.PathSafetyError):
                core._regular_tree_inventory(reparse_tree)


if __name__ == "__main__":
    unittest.main()
