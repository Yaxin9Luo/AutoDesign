from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "agent_skills" / "autodesign-poster"
HARNESS_PATH = SKILL_ROOT / "scripts" / "poster_harness.py"


def _load_harness():
    if not HARNESS_PATH.is_file():
        raise AssertionError("standalone poster harness is missing")
    module_name = "autodesign_portable_poster_harness_test"
    spec = importlib.util.spec_from_file_location(module_name, HARNESS_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("poster harness could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _plan(*, preset: str = "cvpr-landscape", max_attempts: int = 4) -> dict[str, object]:
    if preset == "a0-landscape":
        canvas = {"width_px": 3366, "height_px": 2378}
        print_size = {"width_mm": 1189.0, "height_mm": 841.0}
    else:
        canvas = {"width_px": 3072, "height_px": 1536}
        print_size = {"width_mm": 2133.6, "height_mm": 1066.8}
    return {
        "format_version": 1,
        "artifact_type": "poster",
        "thesis": "The grounded paper supports a clear problem, method, result, and bounded takeaway.",
        "preset": preset,
        "canvas": canvas,
        "print": print_size,
        "narrative": [
            {
                "role": "problem",
                "purpose": "Frame the research problem.",
                "claim_ids": ["c-problem"],
            },
            {
                "role": "method",
                "purpose": "Explain the method.",
                "claim_ids": ["c-method"],
            },
            {
                "role": "evidence",
                "purpose": "Show measured evidence.",
                "claim_ids": ["c-evidence"],
            },
            {
                "role": "takeaway",
                "purpose": "State the bounded conclusion.",
                "claim_ids": ["c-takeaway"],
            },
        ],
        "visual_allocations": [],
        "no_visual_fallback": _no_visual_fallback(),
        "max_attempts": max_attempts,
    }


def _visual_allocation(
    visual_id: object,
    role: str,
    *,
    section_role: str | None = None,
    claim_ids: list[str] | None = None,
    relationship: str = "primary",
    relative_area: float = 0.25,
) -> dict[str, object]:
    section = section_role or (
        "method" if role in {"method", "overview", "method-overview"} else "evidence"
    )
    claims = claim_ids or [f"c-{section}"]
    return {
        "visual_id": str(visual_id),
        "role": role,
        "claim_ids": claims,
        "source_flow_relationship": relationship,
        "intended_area": {
            "section_role": section,
            "relative_area": relative_area,
        },
    }


def _no_visual_fallback() -> dict[str, str]:
    return {
        "reason": "The reviewed source catalog contains no eligible figures or tables.",
        "strategy": "Use source-bound native tables and readouts; do not invent imagery.",
    }


def _claims() -> list[dict[str, object]]:
    return [
        {
            "id": "c-problem",
            "text": "The grounded poster source reports 85% accuracy.",
            "source_ids": ["ev-001"],
        },
        {
            "id": "c-method",
            "text": "The source uses two-stage routing.",
            "source_ids": ["ev-001"],
        },
        {
            "id": "c-evidence",
            "text": "Accuracy reaches 85%.",
            "source_ids": ["ev-001"],
        },
        {
            "id": "c-takeaway",
            "text": "The grounded poster retains accuracy.",
            "source_ids": ["ev-001"],
        },
    ]


def _poster_html(*, image: bool = False) -> str:
    visual = (
        '<img src="assets/vis-001.png" data-source-id="vis-001" '
        'alt="Source method diagram">'
        if image
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
@page {{ size: 2133.6mm 1066.8mm; margin: 0; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; width: 3072px; height: 1536px; }}
body {{ font-family: Arial, Helvetica, sans-serif; color: #18202a; background: #fff; }}
.paper-poster {{ width: 3072px; height: 1536px; padding: 48px; overflow: hidden; }}
[data-role="identity-header"] {{ height: 250px; border-top: 18px solid #174a7e; }}
[data-identity="title"] {{ margin: 18px 0 4px; font-size: 56px; line-height: 1.04; }}
[data-identity="authors"], [data-identity="institutions"] {{ margin: 4px 0; font-size: 28px; }}
.body {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 24px; height: 1190px; }}
section {{ min-width: 0; padding: 24px; border-top: 6px solid #174a7e; }}
h2 {{ margin: 0 0 18px; font-size: 36px; line-height: 1.05; }}
p, li, th, td {{ font-size: 24px; line-height: 1.22; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ padding: 8px; text-align: left; border-bottom: 2px solid #ccd4dc; }}
img {{ display: block; width: 100%; max-height: 390px; object-fit: contain; }}
@media print {{
  html, body {{ width: 2133.6mm; height: 1066.8mm; }}
  .paper-poster {{ width: 2133.6mm; height: 1066.8mm; }}
}}
</style>
</head>
<body>
<main class="paper-poster" data-autodesign-artifact="poster"
      data-canvas-width="3072" data-canvas-height="1536"
      data-print-width-mm="2133.6" data-print-height-mm="1066.8">
  <header data-role="identity-header">
    <h1 data-identity="title" data-source-ids="ev-001">Grounded Poster Study</h1>
    <p data-identity="authors" data-source-ids="ev-001">A. Researcher · B. Scientist</p>
    <p data-identity="institutions" data-source-ids="ev-001">Example University</p>
  </header>
  <div class="body">
    <section data-section-role="problem" data-source-ids="ev-001">
      <h2>Problem</h2>
      <p data-claim-id="c-problem" data-source-ids="ev-001">The grounded poster source reports 85% accuracy.</p>
      <p>Research posters must preserve the paper's actual question and scope.</p>
    </section>
    <section data-section-role="method" data-source-ids="ev-001">
      <h2>Method</h2>
      <p data-claim-id="c-method" data-source-ids="ev-001">The source uses two-stage routing.</p>
      {visual}
      <ul><li>Stage one selects evidence.</li><li>Stage two composes the result.</li></ul>
    </section>
    <section data-section-role="evidence" data-source-ids="ev-001">
      <h2>Evidence</h2>
      <p data-claim-id="c-evidence" data-source-ids="ev-001">Accuracy reaches 85%.</p>
      <table><thead><tr><th>Measure</th><th>Value</th></tr></thead>
      <tbody><tr><td>Accuracy</td><td>85%</td></tr></tbody></table>
    </section>
    <section data-section-role="takeaway" data-source-ids="ev-001">
      <h2>Takeaway</h2>
      <p data-claim-id="c-takeaway" data-source-ids="ev-001">The grounded poster retains accuracy.</p>
      <p>Keep the conclusion bounded by the supplied evidence.</p>
    </section>
  </div>
</main>
</body>
</html>
"""


class AutoDesignPosterSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_fixture(self, *, image: bool = False) -> tuple[Path, Path]:
        artifact = self.root / "artifact"
        artifact.mkdir()
        html = artifact / "poster.html"
        html.write_text(_poster_html(image=image), encoding="utf-8")
        if image:
            (artifact / "assets").mkdir()
            (artifact / "assets" / "vis-001.png").write_bytes(b"fixture-image")
        return artifact, html

    def _passing_source_review(self, context: dict[str, object]) -> dict[str, object]:
        return {
            "run_format_version": 2,
            "source_review_context_sha256": context["context_sha256"],
            "reviewer_kind": "host_fresh_pass",
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

    def _curate_no_visuals(self, harness: object, run: Path) -> None:
        evidence = harness.core.load_evidence(run)
        evidence_id = str(evidence[0]["id"])
        selection = {
            "run_format_version": 2,
            "assets": [],
            "source_story": {
                key: {
                    "status": "not_applicable",
                    "asset_ids": [],
                    "evidence_ids": [evidence_id],
                    "rationale": f"The reviewed source has no distinct {key} visual region.",
                }
                for key in ("central_method", "primary_result")
            },
        }
        context = harness.create_poster_source_review_context(run, selection)
        harness.record_poster_source_review(
            run, context["context_path"], self._passing_source_review(context)
        )

    def _initialize_no_visual_run(
        self, harness: object, run: Path, source: Path, *, max_attempts: int = 4
    ) -> None:
        harness.initialize_poster_run(run, source)
        self._curate_no_visuals(harness, run)
        harness.save_poster_plan(run, _plan(max_attempts=max_attempts))

    @staticmethod
    def _png(width: int, height: int, value: int) -> bytes:
        def chunk(kind: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + kind
                + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
            )

        header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
        rows = b"".join(
            b"\0" + bytes([value, value, value, 255]) * width
            for _ in range(height)
        )
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b"")
        )

    def _fake_poppler(self, name: str) -> dict[str, Path]:
        tools_root = self.root / name
        tools_root.mkdir()
        page_one = tools_root / "page-one.png"
        page_two = tools_root / "page-two.png"
        page_one.write_bytes(self._png(20, 12, 20))
        page_two.write_bytes(self._png(20, 12, 40))
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
    shutil.copyfile({str(page_one)!r}, sys.argv[-1] + "-1.png")
    shutil.copyfile({str(page_two)!r}, sys.argv[-1] + "-2.png")
elif name == "pdfimages" and "-list" in sys.argv:
    print("page num type width height color comp bpc enc interp object ID x-ppi y-ppi size ratio")
'''
        tools: dict[str, Path] = {}
        for tool_name in ("pdftotext", "pdfinfo", "pdftoppm", "pdfimages"):
            executable = tools_root / tool_name
            executable.write_text(script, encoding="utf-8")
            executable.chmod(0o755)
            tools[tool_name] = executable
        return tools

    def _initialize_reviewed_visual_run(
        self,
        harness: object,
        run: Path,
        *,
        reference_images: list[Path] | None = None,
        include_supporting: bool = False,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object] | None]:
        source = self.root / f"{run.name}.pdf"
        source.write_bytes(b"%PDF-1.4\nsynthetic\n")
        tools = self._fake_poppler(f"poppler-{run.name}")
        original_which = harness.core.shutil.which
        with mock.patch.object(
            harness.core.shutil,
            "which",
            side_effect=lambda name: str(tools[name]) if name in tools else original_which(name),
        ):
            harness.initialize_poster_run(
                run, source, reference_images=reference_images or []
            )
        inspection = harness.inspect_poster_source(run)

        def crop(page: int, role: str, bbox: list[float]) -> dict[str, object]:
            page_info = inspection["pages"][page - 1]
            return harness.crop_poster_source(
                run,
                {
                    "run_format_version": 2,
                    "source_sha256": inspection["source"]["sha256"],
                    "page_manifest_sha256": inspection["page_manifest_sha256"],
                    "page": page,
                    "page_sha256": page_info["sha256"],
                    "bbox_normalized": bbox,
                    "role": role,
                    "claim": f"Reviewed {role} source region.",
                    "max_reuse": 2,
                },
            )

        method = crop(1, "method", [0.0, 0.0, 1.0, 1.0])
        result = crop(2, "result", [0.0, 0.0, 1.0, 1.0])
        supporting = (
            crop(1, "supporting", [0.0, 0.0, 0.5, 1.0])
            if include_supporting
            else None
        )
        evidence_id = str(harness.core.load_evidence(run)[0]["id"])
        selected = [
            {"asset_id": method["asset_id"], "roles": ["method"], "max_reuse": 2, "importance": "essential"},
            {"asset_id": result["asset_id"], "roles": ["result"], "max_reuse": 2, "importance": "essential"},
        ]
        if supporting is not None:
            selected.append(
                {"asset_id": supporting["asset_id"], "roles": ["supporting"], "max_reuse": 1, "importance": "supporting"}
            )
        selection = {
            "run_format_version": 2,
            "assets": selected,
            "source_story": {
                "central_method": {
                    "status": "covered",
                    "asset_ids": [method["asset_id"]],
                    "evidence_ids": [evidence_id],
                    "rationale": "The complete method crop shows the central method.",
                },
                "primary_result": {
                    "status": "covered",
                    "asset_ids": [result["asset_id"]],
                    "evidence_ids": [evidence_id],
                    "rationale": "The complete result crop shows the primary result.",
                },
            },
        }
        context = harness.create_poster_source_review_context(run, selection)
        harness.record_poster_source_review(
            run, context["context_path"], self._passing_source_review(context)
        )
        return method, result, supporting

    def test_cli_help_exposes_complete_portable_lifecycle(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(HARNESS_PATH), "--help"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        for command in (
            "doctor", "init", "evidence", "inspect-source", "crop-source",
            "list-source-assets", "source-review-context", "record-source-review",
            "plan", "begin-attempt", "dom-audit", "validate", "review-context",
            "record-review", "reopen-curation", "finalize", "resume", "diagnose-v1",
        ):
            self.assertIn(command, completed.stdout)
        self.assertNotIn("bind-visuals", completed.stdout)

    def test_agent_first_cli_exposes_exact_new_argument_contracts(self) -> None:
        expectations = {
            "inspect-source": {"--run-dir"},
            "crop-source": {"--run-dir", "--request"},
            "list-source-assets": {"--run-dir"},
            "source-review-context": {"--run-dir", "--selection"},
            "record-source-review": {"--run-dir", "--context", "--review"},
            "reopen-curation": {"--run-dir", "--request"},
            "dom-audit": {"--run-dir", "--attempt", "--cache-root", "--offline-browser"},
            "diagnose-v1": {"--run-dir"},
        }
        for command, flags in expectations.items():
            with self.subTest(command=command):
                completed = subprocess.run(
                    [sys.executable, str(HARNESS_PATH), command, "--help"],
                    cwd=self.root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )
                for flag in flags:
                    self.assertIn(flag, completed.stdout)

    def test_cli_rejects_noncanonical_json_with_one_json_error(self) -> None:
        payload = self.root / "noncanonical-plan.json"
        payload.write_text('{"format_version": 1}\n', encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(HARNESS_PATH),
                "plan",
                "--run-dir",
                str(self.root / "missing-run"),
                "--plan",
                str(payload),
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, "")
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "error")
        self.assertIn("canonical shared serialization", result["error"])

    def test_cli_rejects_asset_evidence_before_creating_a_v2_run(self) -> None:
        source = self.root / "paper.txt"
        source.write_text("Grounded paper.", encoding="utf-8")
        asset = self.root / "scratch.png"
        asset.write_bytes(self._png(2, 2, 1))
        run = self.root / "asset-rejected-run"
        completed = subprocess.run(
            [
                sys.executable,
                str(HARNESS_PATH),
                "init",
                "--run-dir",
                str(run),
                "--source",
                str(source),
                "--asset",
                str(asset),
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(json.loads(completed.stdout)["status"], "error")
        self.assertFalse(run.exists())

    def test_dom_audit_cli_is_explicitly_blocked_until_read_only_engine_exists(self) -> None:
        harness = _load_harness()
        run = self.root / "dom-seam-run"
        source = self.root / "paper.txt"
        source.write_text("A source without distinct visuals.", encoding="utf-8")
        self._initialize_no_visual_run(harness, run, source)
        attempt = harness.begin_poster_attempt(run)["attempt_id"]
        artifact = run / "attempts" / attempt / "artifact"
        before = harness.core.tree_hash(artifact)
        completed = subprocess.run(
            [
                sys.executable,
                str(HARNESS_PATH),
                "dom-audit",
                "--run-dir",
                str(run),
                "--attempt",
                attempt,
                "--offline-browser",
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(harness.core.tree_hash(artifact), before)

    def test_resume_requires_poster_html_not_only_staged_source_assets(self) -> None:
        harness = _load_harness()
        run = self.root / "staged-assets-only-run"
        method, result, _supporting = self._initialize_reviewed_visual_run(
            harness, run
        )
        plan = _plan()
        plan["no_visual_fallback"] = None
        plan["visual_allocations"] = [
            _visual_allocation(method["asset_id"], "method"),
            _visual_allocation(result["asset_id"], "result"),
        ]
        harness.save_poster_plan(run, plan)
        attempt = harness.begin_poster_attempt(run)
        artifact = run / "attempts" / attempt["attempt_id"] / "artifact"
        self.assertTrue(any((artifact / "assets").iterdir()))
        self.assertFalse((artifact / "poster.html").exists())

        resumed = harness.resume_poster_run(run)

        self.assertEqual(resumed["next_action"], "author")

    def test_diagnose_v1_cli_is_read_only_and_other_v2_commands_reject_it(self) -> None:
        harness = _load_harness()
        run = self.root / "legacy-run"
        source = self.root / "legacy.txt"
        source.write_text("Legacy grounded source.", encoding="utf-8")
        harness.core.initialize_run(
            run, SKILL_ROOT, release_version="0.1.0"
        )
        harness.core.prepare_source(run, source)
        before = {
            path.relative_to(run).as_posix(): path.read_bytes()
            for path in run.rglob("*")
            if path.is_file()
        }
        diagnosed = subprocess.run(
            [sys.executable, str(HARNESS_PATH), "diagnose-v1", "--run-dir", str(run)],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(diagnosed.returncode, 0, diagnosed.stdout + diagnosed.stderr)
        self.assertEqual(json.loads(diagnosed.stdout)["mode"], "read_only")
        after = {
            path.relative_to(run).as_posix(): path.read_bytes()
            for path in run.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

        rejected = subprocess.run(
            [sys.executable, str(HARNESS_PATH), "resume", "--run-dir", str(run)],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("diagnose-v1", json.loads(rejected.stdout)["error"])

    def test_v2_init_preflights_existing_v1_before_shared_initialization(self) -> None:
        harness = _load_harness()
        run = self.root / "existing-v1-run"
        source = self.root / "legacy-source.txt"
        source.write_text("Legacy source evidence.", encoding="utf-8")
        harness.core.initialize_run(run, SKILL_ROOT, release_version="0.1.0")
        harness.core.prepare_source(run, source)
        before = {
            path.relative_to(run).as_posix(): path.read_bytes()
            for path in run.rglob("*")
            if path.is_file()
        }

        replacement = self.root / "replacement.txt"
        replacement.write_text("Replacement source.", encoding="utf-8")
        with mock.patch.object(
            harness.core,
            "initialize_run",
            side_effect=AssertionError("shared initialize_run must not be called"),
        ):
            with self.assertRaisesRegex(harness.PosterContractError, "diagnose-v1"):
                harness.initialize_poster_run(run, replacement)

        after = {
            path.relative_to(run).as_posix(): path.read_bytes()
            for path in run.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_cli_help_does_not_write_bytecode_into_a_read_only_style_install(self) -> None:
        installed = self.root / "installed" / "autodesign-poster"
        shutil.copytree(SKILL_ROOT, installed)
        for generated in installed.rglob("__pycache__"):
            shutil.rmtree(generated)
        before = {
            path.relative_to(installed).as_posix(): path.read_bytes()
            for path in installed.rglob("*")
            if path.is_file()
        }
        environment = dict(os.environ)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        completed = subprocess.run(
            [sys.executable, str(installed / "scripts" / "poster_harness.py"), "--help"],
            cwd=self.root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        after = {
            path.relative_to(installed).as_posix(): path.read_bytes()
            for path in installed.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_importing_harness_does_not_write_bytecode_into_skill_package(self) -> None:
        for generated in SKILL_ROOT.rglob("__pycache__"):
            shutil.rmtree(generated)
        _load_harness()
        self.assertFalse(list(SKILL_ROOT.rglob("__pycache__")))

    def test_plan_defaults_to_fixed_cvpr_landscape_contract(self) -> None:
        harness = _load_harness()
        normalized = harness.normalize_plan(
            {
                "format_version": 1,
                "artifact_type": "poster",
                "thesis": _plan()["thesis"],
                "narrative": _plan()["narrative"],
                "visual_allocations": [],
            }
        )
        self.assertEqual(normalized["preset"], "cvpr-landscape")
        self.assertEqual(normalized["canvas"], {"width_px": 3072, "height_px": 1536})
        self.assertEqual(normalized["print"], {"width_mm": 2133.6, "height_mm": 1066.8})
        self.assertEqual(normalized["max_attempts"], 4)

    def test_plan_normalizes_closed_story_flow_and_area_contract(self) -> None:
        harness = _load_harness()

        normalized = harness.normalize_plan(_plan())

        self.assertEqual(
            normalized["thesis"],
            "The grounded paper supports a clear problem, method, result, and bounded takeaway.",
        )
        self.assertEqual(
            normalized["narrative"][1],
            {
                "role": "method",
                "purpose": "Explain the method.",
                "claim_ids": ["c-method"],
            },
        )
        self.assertEqual(normalized["visual_allocations"], [])

    def test_plan_rejects_missing_unknown_or_stale_story_bindings(self) -> None:
        harness = _load_harness()

        def fresh() -> dict[str, object]:
            return json.loads(json.dumps(_plan()))

        missing_thesis = fresh()
        missing_thesis.pop("thesis")
        cases: list[tuple[str, dict[str, object], str]] = [
            ("missing_thesis", missing_thesis, "thesis"),
        ]

        non_string_thesis = fresh()
        non_string_thesis["thesis"] = 7
        cases.append(("non_string_thesis", non_string_thesis, "thesis"))

        missing_claims = fresh()
        missing_claims["narrative"][0].pop("claim_ids")
        cases.append(("missing_narrative_claims", missing_claims, "claim_ids"))

        unknown_narrative = fresh()
        unknown_narrative["narrative"][0]["unknown"] = True
        cases.append(("unknown_narrative_field", unknown_narrative, "narrative"))

        non_string_purpose = fresh()
        non_string_purpose["narrative"][0]["purpose"] = 7
        cases.append(("non_string_narrative", non_string_purpose, "non-empty strings"))

        duplicate_owner = fresh()
        duplicate_owner["narrative"][1]["claim_ids"] = ["c-problem"]
        cases.append(("duplicate_claim_owner", duplicate_owner, "exactly one"))

        missing_allocation_fields = fresh()
        missing_allocation_fields["visual_allocations"] = [
            {"visual_id": "src-method", "role": "method"}
        ]
        cases.append(
            ("missing_allocation_fields", missing_allocation_fields, "visual_allocations")
        )

        non_string_visual_id = fresh()
        allocation = _visual_allocation("src-method", "method")
        allocation["visual_id"] = 7
        non_string_visual_id["visual_allocations"] = [allocation]
        cases.append(("non_string_visual_id", non_string_visual_id, "non-empty strings"))

        unknown_allocation = fresh()
        allocation = _visual_allocation("src-method", "method")
        allocation["unknown"] = True
        unknown_allocation["visual_allocations"] = [allocation]
        cases.append(("unknown_allocation_field", unknown_allocation, "visual_allocations"))

        stale_claim = fresh()
        stale_claim["visual_allocations"] = [
            _visual_allocation(
                "src-method", "method", claim_ids=["c-method-stale"]
            )
        ]
        cases.append(("stale_claim", stale_claim, "not owned"))

        wrong_section = fresh()
        wrong_section["visual_allocations"] = [
            _visual_allocation(
                "src-method",
                "method",
                section_role="evidence",
                claim_ids=["c-method"],
            )
        ]
        cases.append(("wrong_section_binding", wrong_section, "not owned"))

        bad_relationship = fresh()
        bad_relationship["visual_allocations"] = [
            _visual_allocation(
                "src-method", "method", relationship="floating-decoration"
            )
        ]
        cases.append(("bad_relationship", bad_relationship, "source_flow_relationship"))

        bad_area = fresh()
        bad_area["visual_allocations"] = [
            _visual_allocation("src-method", "method", relative_area=0.0)
        ]
        cases.append(("bad_area", bad_area, "relative_area"))

        unknown_area = fresh()
        allocation = _visual_allocation("src-method", "method")
        allocation["intended_area"]["pixels"] = 100
        unknown_area["visual_allocations"] = [allocation]
        cases.append(("unknown_area_field", unknown_area, "intended_area"))

        excessive_area = fresh()
        excessive_area["visual_allocations"] = [
            _visual_allocation("src-method-a", "method", relative_area=0.6),
            _visual_allocation("src-method-b", "method", relative_area=0.6),
        ]
        cases.append(("excessive_section_area", excessive_area, "exceeds"))

        for name, payload, message in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(harness.PosterContractError, message):
                    harness.normalize_plan(payload)

    def test_plan_story_fields_bind_active_plan_attempt_snapshot_and_context(self) -> None:
        harness = _load_harness()
        run = self.root / "story-binding-run"
        method, result, _supporting = self._initialize_reviewed_visual_run(
            harness, run
        )
        plan = _plan()
        plan["no_visual_fallback"] = None
        plan["visual_allocations"] = [
            _visual_allocation(method["asset_id"], "method", relative_area=0.4),
            _visual_allocation(result["asset_id"], "result", relative_area=0.4),
        ]

        normalized = harness.normalize_plan(plan)
        harness.save_poster_plan(run, plan)
        attempt = harness.begin_poster_attempt(run)
        active = harness.core.load_active_plan(run)
        snapshot = harness.core.load_attempt_plan(run, attempt["attempt_id"])
        context = json.loads((run / attempt["authoring_context"]).read_text())

        self.assertEqual(active, normalized)
        self.assertEqual(snapshot, normalized)
        self.assertEqual(context["plan"], normalized)
        self.assertEqual(
            context["plan"]["visual_allocations"][0]["intended_area"],
            {"section_role": "method", "relative_area": 0.4},
        )

    def test_authoring_context_next_command_is_exact_absolute_and_shell_safe(self) -> None:
        harness = _load_harness()
        run = self.root / "poster run's handoff"
        source = self.root / "paper source.txt"
        source.write_text("A grounded source without distinct visuals.", encoding="utf-8")
        self._initialize_no_visual_run(harness, run, source)

        attempt = harness.begin_poster_attempt(run)
        context = json.loads((run / attempt["authoring_context"]).read_text())
        source_map = run.absolute() / "attempts" / attempt["attempt_id"] / "source-map-input.json"

        self.assertEqual(
            shlex.split(context["next_command"]),
            [
                sys.executable,
                str(HARNESS_PATH.resolve()),
                "validate",
                "--run-dir",
                str(run.absolute()),
                "--attempt",
                attempt["attempt_id"],
                "--source-map",
                str(source_map),
            ],
        )

    def test_validation_rejects_source_map_claim_ids_stale_from_attempt_plan(self) -> None:
        harness = _load_harness()
        run = self.root / "stale-plan-claims-run"
        source = self.root / "stale-plan-claims.txt"
        source.write_text(
            "Grounded poster source reports 85% accuracy and uses two-stage routing.",
            encoding="utf-8",
        )
        self._initialize_no_visual_run(harness, run, source)
        attempt = harness.begin_poster_attempt(run)
        (run / attempt["poster_path"]).write_text(_poster_html(), encoding="utf-8")
        claims = _claims()
        claims[1] = {**claims[1], "id": "c-method-stale"}
        source_map = self.root / "stale-source-map.json"
        source_map.write_text(json.dumps({"claims": claims}), encoding="utf-8")

        with self.assertRaisesRegex(harness.PosterContractError, "attempt plan"):
            harness.validate_poster_attempt(
                run,
                attempt["attempt_id"],
                source_map_path=source_map,
                allow_browser_install=False,
            )

        self.assertFalse(
            (
                run
                / "attempts"
                / attempt["attempt_id"]
                / "provenance"
                / "source-map.json"
            ).exists()
        )

    def test_plan_honors_supported_user_size_and_rejects_ratio_mismatch(self) -> None:
        harness = _load_harness()
        normalized = harness.normalize_plan(_plan(preset="a0-landscape"))
        self.assertEqual(normalized["canvas"], {"width_px": 3366, "height_px": 2378})
        self.assertEqual(normalized["print"], {"width_mm": 1189.0, "height_mm": 841.0})

        broken = _plan()
        broken["preset"] = "custom"
        broken["print"] = {"width_mm": 841.0, "height_mm": 1189.0}
        with self.assertRaisesRegex(harness.PosterContractError, "aspect ratio"):
            harness.normalize_plan(broken)

    def test_plan_rejects_empty_allocations_when_eligible_visuals_exist(self) -> None:
        harness = _load_harness()
        run = self.root / "eligible-empty-run"
        self._initialize_reviewed_visual_run(harness, run)

        plan = _plan()
        plan["no_visual_fallback"] = None
        with self.assertRaisesRegex(
            harness.PosterContractError,
            "retain reviewed source evidence for central_method",
        ):
            harness.save_poster_plan(run, plan)

    def test_plan_allows_zero_visuals_only_with_explicit_native_fallback(self) -> None:
        harness = _load_harness()
        run = self.root / "no-eligible-run"
        source = self.root / "paper.txt"
        source.write_text("A paper with no extractable visual evidence.", encoding="utf-8")
        harness.initialize_poster_run(run, source)
        self._curate_no_visuals(harness, run)

        missing_fallback = _plan()
        missing_fallback.pop("no_visual_fallback")
        with self.assertRaisesRegex(harness.PosterContractError, "no_visual_fallback"):
            harness.save_poster_plan(run, missing_fallback)

        plan = _plan()
        plan["no_visual_fallback"] = _no_visual_fallback()
        saved = harness.save_poster_plan(run, plan)
        self.assertEqual(saved["plan_revision"], 1)
        self.assertEqual(
            harness.core.load_active_plan(run)["no_visual_fallback"],
            _no_visual_fallback(),
        )

    def test_reviewed_catalog_has_no_fixed_visual_count_floor_and_enforces_roles(self) -> None:
        harness = _load_harness()
        run = self.root / "limited-eligible-run"
        method, result, _supporting = self._initialize_reviewed_visual_run(
            harness, run
        )

        wrong_roles = _plan()
        wrong_roles["no_visual_fallback"] = None
        wrong_roles["visual_allocations"] = [
            _visual_allocation(method["asset_id"], "result"),
            _visual_allocation(result["asset_id"], "result"),
        ]
        with self.assertRaisesRegex(harness.PosterContractError, "not permitted"):
            harness.save_poster_plan(run, wrong_roles)

        plan = _plan()
        plan["no_visual_fallback"] = None
        plan["visual_allocations"] = [
            _visual_allocation(method["asset_id"], "method"),
            _visual_allocation(result["asset_id"], "result"),
        ]
        saved = harness.save_poster_plan(run, plan)
        self.assertEqual(saved["plan_revision"], 1)
        attempt = harness.begin_poster_attempt(run)
        context = json.loads(
            (run / attempt["authoring_context"]).read_text(encoding="utf-8")
        )
        self.assertEqual(context["reviewed_coverage"]["allocated_asset_count"], 2)

    def test_skill_teaches_portable_python_resolution_and_source_flow_preservation(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        output_contract = (SKILL_ROOT / "references" / "output-contract.md").read_text(
            encoding="utf-8"
        )
        review_rubric = (SKILL_ROOT / "references" / "review-rubric.md").read_text(
            encoding="utf-8"
        )
        for launcher in ("`python3`", "`python`", "`py -3`"):
            self.assertIn(launcher, skill)
        self.assertIn(".source-flow-unit", skill)
        self.assertIn("native readout", output_contract)
        self.assertIn("must not replace", output_contract)
        self.assertIn("source-flow", review_rubric)

    def test_static_lint_accepts_editable_grounded_poster_contract(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture(image=True)
        plan = _plan()
        plan["visual_allocations"] = [_visual_allocation("vis-001", "method")]
        result = harness.lint_poster_html(
            html,
            artifact_root=artifact,
            plan=plan,
            claims=_claims(),
            visual_catalog=[
                {
                    "id": "vis-001",
                    "path": "assets/source.png",
                    "sha256": harness.core.sha256_file(artifact / "assets" / "vis-001.png"),
                    "eligibility": "eligible",
                }
            ],
        )
        self.assertTrue(result["passed"], result)
        self.assertEqual(
            {check["id"] for check in result["checks"]},
            {
                "document_contract", "fixed_canvas", "print_page_size",
                "identity_header", "native_editability", "source_bindings",
                "local_assets", "narrative_arc", "typography_contract",
            },
        )

    def test_static_lint_accepts_grounded_identity_ids_outside_claim_map(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        html.write_text(
            html.read_text(encoding="utf-8").replace(
                'data-source-ids="ev-001">Grounded Poster Study',
                'data-source-ids="ev-identity">Grounded Poster Study',
            ).replace(
                'data-source-ids="ev-001">A. Researcher',
                'data-source-ids="ev-identity">A. Researcher',
            ).replace(
                'data-source-ids="ev-001">Example University',
                'data-source-ids="ev-identity">Example University',
            ),
            encoding="utf-8",
        )
        result = harness.lint_poster_html(
            html,
            artifact_root=artifact,
            plan=_plan(),
            claims=_claims(),
            evidence_ids=["ev-identity"],
        )
        binding = next(check for check in result["checks"] if check["id"] == "source_bindings")
        self.assertTrue(binding["passed"], binding)

    def test_static_lint_rejects_non_identity_header_content_and_logo(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        text = html.read_text(encoding="utf-8").replace(
            '<p data-identity="institutions" data-source-ids="ev-001">Example University</p>',
            '<p data-identity="institutions" data-source-ids="ev-001">Example University</p>'
            '<p data-identity="venue">CVPR 2026</p><img src="logo.png" alt="logo">',
        )
        html.write_text(text, encoding="utf-8")
        result = harness.lint_poster_html(html, artifact_root=artifact, plan=_plan(), claims=_claims())
        finding = next(check for check in result["checks"] if check["id"] == "identity_header")
        self.assertFalse(finding["passed"])
        self.assertIn("exactly title, authors, and institutions", finding["detail"])

    def test_static_lint_rejects_remote_traversal_data_and_missing_assets(self) -> None:
        harness = _load_harness()
        for source in (
            "https://example.com/figure.png",
            "data:image/png;base64,AAAA",
            "../outside.png",
            "assets/missing.png",
        ):
            with self.subTest(source=source):
                artifact, html = self._write_fixture()
                html.write_text(
                    html.read_text(encoding="utf-8").replace(
                        "<h2>Method</h2>",
                        f'<h2>Method</h2><img src="{source}" data-source-id="vis-001" alt="method">',
                    ),
                    encoding="utf-8",
                )
                result = harness.lint_poster_html(
                    html, artifact_root=artifact, plan=_plan(), claims=_claims()
                )
                local = next(check for check in result["checks"] if check["id"] == "local_assets")
                self.assertFalse(local["passed"])
                for child in sorted(artifact.iterdir(), reverse=True):
                    if child.is_dir():
                        for nested in child.rglob("*"):
                            if nested.is_file():
                                nested.unlink()
                        child.rmdir()
                    else:
                        child.unlink()
                artifact.rmdir()

    def test_static_lint_rejects_remote_inline_style_asset(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        html.write_text(
            html.read_text(encoding="utf-8").replace(
                '<section data-section-role="method"',
                '<section style="background-image:url(https://example.com/paper.png)" '
                'data-section-role="method"',
            ),
            encoding="utf-8",
        )
        result = harness.lint_poster_html(
            html, artifact_root=artifact, plan=_plan(), claims=_claims()
        )
        local = next(check for check in result["checks"] if check["id"] == "local_assets")
        self.assertFalse(local["passed"])

    def test_static_lint_rejects_unallocated_css_background_images(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        (artifact / "assets").mkdir()
        (artifact / "assets" / "fabricated.png").write_bytes(b"fabricated-image")
        html.write_text(
            html.read_text(encoding="utf-8").replace(
                "section {",
                "section { background-image:url(assets/fabricated.png);",
            ),
            encoding="utf-8",
        )
        result = harness.lint_poster_html(
            html, artifact_root=artifact, plan=_plan(), claims=_claims()
        )
        self.assertFalse(result["passed"], result)
        local = next(check for check in result["checks"] if check["id"] == "local_assets")
        self.assertIn("CSS image", local["detail"])

    def test_static_lint_rejects_unreferenced_artifact_files(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        (artifact / "unused-reference.png").write_bytes(b"must not ship")
        result = harness.lint_poster_html(
            html, artifact_root=artifact, plan=_plan(), claims=_claims()
        )
        local = next(check for check in result["checks"] if check["id"] == "local_assets")
        self.assertFalse(local["passed"])
        self.assertIn("unreferenced", local["detail"])

    def test_static_lint_rejects_unallocated_or_hash_mismatched_source_images(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture(image=True)
        result = harness.lint_poster_html(
            html,
            artifact_root=artifact,
            plan=_plan(),
            claims=_claims(),
            visual_catalog=[],
        )
        binding = next(check for check in result["checks"] if check["id"] == "source_bindings")
        self.assertFalse(binding["passed"], binding)
        self.assertIn("not allocated", binding["detail"])

        plan = _plan()
        plan["visual_allocations"] = [_visual_allocation("vis-001", "method")]
        result = harness.lint_poster_html(
            html,
            artifact_root=artifact,
            plan=plan,
            claims=_claims(),
            visual_catalog=[
                {
                    "id": "vis-001",
                    "path": "assets/source.png",
                    "sha256": "0" * 64,
                    "eligibility": "eligible",
                }
            ],
        )
        binding = next(check for check in result["checks"] if check["id"] == "source_bindings")
        self.assertFalse(binding["passed"], binding)
        self.assertIn("hash", binding["detail"])

    def test_static_lint_rejects_executable_or_unsupported_sidecar_assets(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        sidecar = artifact / "assets" / "extra.html"
        sidecar.parent.mkdir()
        sidecar.write_text("<script>fetch('https://example.com')</script>", encoding="utf-8")
        html.write_text(
            html.read_text(encoding="utf-8").replace(
                "<h2>Method</h2>", '<h2>Method</h2><a href="assets/extra.html">Details</a>',
            ),
            encoding="utf-8",
        )
        result = harness.lint_poster_html(
            html, artifact_root=artifact, plan=_plan(), claims=_claims()
        )
        local = next(check for check in result["checks"] if check["id"] == "local_assets")
        self.assertFalse(local["passed"], local)
        self.assertIn("unsupported artifact dependency", local["detail"])

    def test_svg_validator_rejects_dtd_before_parsing_the_document(self) -> None:
        harness = _load_harness()
        svg = self.root / "hostile.svg"
        svg.write_text(
            '<!DOCTYPE svg [<!ENTITY payload "x">]><svg>&payload;</svg>',
            encoding="utf-8",
        )
        with mock.patch.object(
            harness.ET,
            "fromstring",
            side_effect=AssertionError("DTD-bearing SVG must not reach the XML parser"),
        ):
            errors = harness._validate_svg_sidecar(svg, "assets/hostile.svg")
        self.assertTrue(any("document type or entity" in error for error in errors), errors)

    def test_static_lint_rejects_svg_sidecar_with_remote_css_dependency(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        sidecar = artifact / "assets" / "extra.svg"
        sidecar.parent.mkdir()
        sidecar.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"><style>'
            '@import url(https://example.com/leak.css);</style><rect width="10" height="10"/></svg>',
            encoding="utf-8",
        )
        html.write_text(
            html.read_text(encoding="utf-8").replace(
                "<h2>Method</h2>", '<h2>Method</h2><a href="assets/extra.svg">Details</a>',
            ),
            encoding="utf-8",
        )
        result = harness.lint_poster_html(
            html, artifact_root=artifact, plan=_plan(), claims=_claims()
        )
        self.assertFalse(result["passed"], result)
        local = next(check for check in result["checks"] if check["id"] == "local_assets")
        self.assertIn("SVG dependency contains unsafe CSS", local["detail"])

    def test_plan_rejects_content_assets_the_poster_cannot_render(self) -> None:
        harness = _load_harness()
        run = self.root / "unsupported-asset-run"
        source = self.root / "paper.txt"
        source.write_text("Grounded poster source reports 85% accuracy.", encoding="utf-8")
        unsupported = self.root / "raw-data.csv"
        unsupported.write_text("measure,value\naccuracy,85\n", encoding="utf-8")
        with self.assertRaisesRegex(harness.PosterContractError, "derive reviewed crops"):
            harness.initialize_poster_run(run, source, extra_assets=[unsupported])
        self.assertFalse(run.exists())

    def test_static_lint_rejects_raster_only_or_non_native_table_content(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        html.write_text(
            """<!doctype html><html><head><style>@page{size:2133.6mm 1066.8mm;margin:0}</style></head>
            <body><main class="paper-poster" data-autodesign-artifact="poster"
            data-canvas-width="3072" data-canvas-height="1536"
            data-print-width-mm="2133.6" data-print-height-mm="1066.8">
            <img src="poster.png" alt="flattened poster"></main></body></html>""",
            encoding="utf-8",
        )
        (artifact / "poster.png").write_bytes(b"flattened")
        result = harness.lint_poster_html(html, artifact_root=artifact, plan=_plan(), claims=_claims())
        native = next(check for check in result["checks"] if check["id"] == "native_editability")
        self.assertFalse(native["passed"])
        self.assertIn("native", native["detail"].lower())

    def test_static_lint_rejects_wrong_canvas_print_size_and_typography(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        text = html.read_text(encoding="utf-8")
        text = text.replace('data-canvas-width="3072"', 'data-canvas-width="1920"')
        text = text.replace("size: 2133.6mm 1066.8mm", "size: 297mm 210mm")
        text = text.replace("font-size: 24px", "font-size: 10px")
        html.write_text(text, encoding="utf-8")
        result = harness.lint_poster_html(html, artifact_root=artifact, plan=_plan(), claims=_claims())
        failed = {check["id"] for check in result["checks"] if not check["passed"]}
        self.assertTrue({"fixed_canvas", "print_page_size", "typography_contract"}.issubset(failed))

    def test_static_lint_rejects_missing_or_mismatched_claim_bindings(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        text = html.read_text(encoding="utf-8")
        text = text.replace('data-claim-id="c-method"', 'data-claim-id="unknown-claim"')
        text = text.replace("The grounded poster source reports 85% accuracy.", "An unsupported claim.", 1)
        html.write_text(text, encoding="utf-8")
        result = harness.lint_poster_html(html, artifact_root=artifact, plan=_plan(), claims=_claims())
        binding = next(check for check in result["checks"] if check["id"] == "source_bindings")
        self.assertFalse(binding["passed"])
        self.assertIn("unknown-claim", binding["detail"])

    def test_static_lint_rejects_claim_binding_wrapped_around_extra_text(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        text = html.read_text(encoding="utf-8")
        text = text.replace(
            '<section data-section-role="method" data-source-ids="ev-001">',
            '<section data-section-role="method" data-claim-id="c-method" '
            'data-source-ids="ev-001">',
        ).replace(
            '<p data-claim-id="c-method" data-source-ids="ev-001">',
            '<p data-source-ids="ev-001">',
        )
        html.write_text(text, encoding="utf-8")
        result = harness.lint_poster_html(
            html, artifact_root=artifact, plan=_plan(), claims=_claims()
        )
        binding = next(check for check in result["checks"] if check["id"] == "source_bindings")
        self.assertFalse(binding["passed"], binding)
        self.assertIn("exactly match", binding["detail"])

    def test_static_lint_rejects_scripts_handlers_and_incomplete_research_arc(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        text = html.read_text(encoding="utf-8")
        text = text.replace("<body>", '<body onload="fetch(\'https://example.com\')"><script>alert(1)</script>')
        text = text.replace('data-section-role="takeaway"', 'data-section-role="evidence"')
        html.write_text(text, encoding="utf-8")
        result = harness.lint_poster_html(html, artifact_root=artifact, plan=_plan(), claims=_claims())
        failed = {check["id"] for check in result["checks"] if not check["passed"]}
        self.assertTrue({"document_contract", "narrative_arc"}.issubset(failed))

    def test_static_lint_rejects_delayed_meta_refresh_navigation(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        html.write_text(
            html.read_text(encoding="utf-8").replace(
                '<meta charset="utf-8">',
                '<meta charset="utf-8"><meta http-equiv="refresh" '
                'content="60;url=https://example.com/leak">',
            ),
            encoding="utf-8",
        )
        result = harness.lint_poster_html(
            html, artifact_root=artifact, plan=_plan(), claims=_claims()
        )
        document = next(check for check in result["checks"] if check["id"] == "document_contract")
        self.assertFalse(document["passed"], document)
        self.assertIn("meta refresh", document["detail"])

    def test_static_lint_rejects_duplicate_attributes_before_browser_parsing(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        html.write_text(
            html.read_text(encoding="utf-8").replace(
                '<meta charset="utf-8">',
                '<meta charset="utf-8"><meta http-equiv="refresh" '
                'http-equiv="x-safe" content="60;url=https://example.com/leak">',
            ),
            encoding="utf-8",
        )
        result = harness.lint_poster_html(
            html, artifact_root=artifact, plan=_plan(), claims=_claims()
        )
        document = next(check for check in result["checks"] if check["id"] == "document_contract")
        self.assertFalse(document["passed"], document)
        self.assertIn("duplicate attribute", document["detail"])

    def test_static_lint_rejects_visible_css_generated_content(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        html.write_text(
            html.read_text(encoding="utf-8").replace(
                "</style>",
                'section[data-section-role="method"]::after {'
                'content:"Fabricated accuracy 999%";display:block;font-size:24px}'
                "</style>",
            ),
            encoding="utf-8",
        )
        result = harness.lint_poster_html(
            html, artifact_root=artifact, plan=_plan(), claims=_claims()
        )
        document = next(check for check in result["checks"] if check["id"] == "document_contract")
        self.assertFalse(document["passed"], document)
        self.assertIn("generated content", document["detail"])

    def test_static_lint_rejects_form_control_value_text(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        html.write_text(
            html.read_text(encoding="utf-8").replace(
                "<h2>Method</h2>",
                '<h2>Method</h2><input value="Fabricated accuracy 999%" '
                'style="font-size:24px">',
            ),
            encoding="utf-8",
        )
        result = harness.lint_poster_html(
            html, artifact_root=artifact, plan=_plan(), claims=_claims()
        )
        document = next(check for check in result["checks"] if check["id"] == "document_contract")
        self.assertFalse(document["passed"], document)
        self.assertIn("unsafe tag: input", document["detail"])

    def test_static_lint_rejects_unmapped_visible_numbers(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        html.write_text(
            html.read_text(encoding="utf-8").replace(
                "<h2>Method</h2>",
                "<h2>Method</h2><p>Fabricated accuracy reaches 999%.</p>",
            ),
            encoding="utf-8",
        )
        result = harness.lint_poster_html(
            html, artifact_root=artifact, plan=_plan(), claims=_claims()
        )
        binding = next(check for check in result["checks"] if check["id"] == "source_bindings")
        self.assertFalse(binding["passed"], binding)
        self.assertIn("unsupported visible numbers", binding["detail"])

    def test_pdfinfo_gate_requires_one_page_and_exact_physical_size(self) -> None:
        harness = _load_harness()
        passed = harness.parse_pdfinfo(
            "Pages:          1\nPage size:      6048 x 3024 pts (custom)\n",
            expected_width_mm=2133.6,
            expected_height_mm=1066.8,
        )
        self.assertTrue(passed["passed"], passed)

        two_pages = harness.parse_pdfinfo(
            "Pages: 2\nPage size: 6048 x 3024 pts\n",
            expected_width_mm=2133.6,
            expected_height_mm=1066.8,
        )
        wrong_size = harness.parse_pdfinfo(
            "Pages: 1\nPage size: 792 x 612 pts (letter)\n",
            expected_width_mm=2133.6,
            expected_height_mm=1066.8,
        )
        self.assertFalse(two_pages["passed"])
        self.assertFalse(wrong_size["passed"])

    def test_begin_attempt_stages_only_allocated_eligible_content_visuals(self) -> None:
        harness = _load_harness()
        run = self.root / "run"
        style_reference = self.root / "reference.png"
        style_reference.write_bytes(self._png(3, 3, 90))
        method, result, supporting = self._initialize_reviewed_visual_run(
            harness,
            run,
            reference_images=[style_reference],
            include_supporting=True,
        )
        assert supporting is not None
        plan = _plan()
        plan["no_visual_fallback"] = None
        plan["visual_allocations"] = [
            _visual_allocation(method["asset_id"], "method"),
            _visual_allocation(result["asset_id"], "result"),
        ]
        plan["style_reference_ids"] = ["vis-001"]
        harness.save_poster_plan(run, plan)
        attempt = harness.begin_poster_attempt(run)
        staged = run / "attempts" / attempt["attempt_id"] / "artifact" / "assets"
        self.assertEqual(
            {path.name for path in staged.iterdir()},
            {f"{method['asset_id']}.png", f"{result['asset_id']}.png"},
        )
        self.assertFalse((staged / f"{supporting['asset_id']}.png").exists())
        context = json.loads((run / attempt["authoring_context"]).read_text())
        self.assertEqual(context["style_references"][0]["visual_id"], "vis-001")
        self.assertTrue(
            all("receipt_sha256" in item for item in context["staged_content_visuals"])
        )
        self.assertEqual(context["catalog_revision"], 1)
        self.assertEqual(context["plan_revision"], 1)
        self.assertIn("source-flow-unit", context["source_flow_guidance"])
        self.assertIn(f"--attempt {attempt['attempt_id']}", context["next_command"])
        self.assertTrue(
            all("page_path" in item and "page_sha256" in item for item in context["staged_content_visuals"])
        )

    def test_plan_and_attempt_honor_reviewed_max_reuse_without_duplicate_files(self) -> None:
        harness = _load_harness()
        run = self.root / "reuse-run"
        method, result, _supporting = self._initialize_reviewed_visual_run(
            harness, run
        )
        plan = _plan()
        plan["no_visual_fallback"] = None
        plan["visual_allocations"] = [
            _visual_allocation(method["asset_id"], "method"),
            _visual_allocation(method["asset_id"], "method"),
            _visual_allocation(result["asset_id"], "result"),
        ]
        harness.save_poster_plan(run, plan)
        attempt = harness.begin_poster_attempt(run)
        staged = run / "attempts" / attempt["attempt_id"] / "artifact" / "assets"
        self.assertEqual(
            {path.name for path in staged.iterdir()},
            {f"{method['asset_id']}.png", f"{result['asset_id']}.png"},
        )
        context = json.loads((run / attempt["authoring_context"]).read_text())
        self.assertEqual(
            [item["visual_id"] for item in context["staged_content_visuals"]].count(
                method["asset_id"]
            ),
            2,
        )

        over_reused = dict(plan)
        over_reused["visual_allocations"] = [
            *plan["visual_allocations"],
            _visual_allocation(method["asset_id"], "method"),
        ]
        second_run = self.root / "over-reuse-run"
        second_method, second_result, _ = self._initialize_reviewed_visual_run(
            harness, second_run
        )
        over_reused["visual_allocations"] = [
            _visual_allocation(second_method["asset_id"], "method"),
            _visual_allocation(second_method["asset_id"], "method"),
            _visual_allocation(second_method["asset_id"], "method"),
            _visual_allocation(second_result["asset_id"], "result"),
        ]
        with self.assertRaisesRegex(harness.PosterContractError, "reuse limit"):
            harness.save_poster_plan(second_run, over_reused)

    def test_poster_finding_table_rejects_unknown_or_downgraded_routes(self) -> None:
        harness = _load_harness()
        harness._validate_poster_route_review(
            {
                "repair_route": "source_reingest",
                "route_findings": [
                    {
                        "code": "dom_overflow",
                        "minimum_route": "layout_repair",
                    }
                ],
            }
        )
        with self.assertRaisesRegex(harness.PosterContractError, "requires minimum route"):
            harness._validate_poster_route_review(
                {
                    "repair_route": "layout_repair",
                    "route_findings": [
                        {
                            "code": "fragmentary_crop",
                            "minimum_route": "layout_repair",
                        }
                    ],
                }
            )
        with self.assertRaisesRegex(harness.PosterContractError, "unknown Poster finding"):
            harness._validate_poster_route_review(
                {
                    "repair_route": "layout_repair",
                    "route_findings": [
                        {"code": "unregistered", "minimum_route": "layout_repair"}
                    ],
                }
            )

    def test_resume_revalidates_poster_route_before_shared_recovery(self) -> None:
        harness = _load_harness()
        run = self.root / "route-revalidation-run"
        review_path = run / "attempts" / "01" / "qa" / "semantic-review.json"
        review_path.parent.mkdir(parents=True)
        (run / "run.json").write_text(
            json.dumps({"active_attempt": "01"}), encoding="utf-8"
        )
        review_path.write_text(
            json.dumps(
                {
                    "repair_route": "layout_repair",
                    "route_findings": [
                        {
                            "code": "fragmentary_crop",
                            "minimum_route": "layout_repair",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            harness.core,
            "inspect_run_format",
            return_value=harness.core.AGENT_FIRST_RUN_FORMAT_VERSION,
        ), mock.patch.object(
            harness.core,
            "resume_run",
            side_effect=AssertionError("shared recovery must not run first"),
        ) as shared_resume:
            with self.assertRaisesRegex(
                harness.PosterContractError, "requires minimum route"
            ):
                harness.resume_poster_run(run)
        shared_resume.assert_not_called()

    def test_nested_blocked_result_uses_cli_blocked_exit_code(self) -> None:
        harness = _load_harness()
        self.assertEqual(
            harness._command_exit_code(
                {"source": {"status": "blocked"}, "resume": {"state": "blocked"}}
            ),
            2,
        )

    def test_runtime_retry_does_not_consume_or_trip_attempt_budget(self) -> None:
        harness = _load_harness()
        run = self.root / "bounded-run"
        source = self.root / "paper.txt"
        source.write_text("Grounded poster source reports 85% accuracy.", encoding="utf-8")
        self._initialize_no_visual_run(harness, run, source, max_attempts=1)
        first = harness.begin_poster_attempt(run)["attempt_id"]
        harness.core.mark_side_state(run, "failed", reason="browser runtime unavailable")
        retried = harness.begin_poster_attempt(run)["attempt_id"]
        self.assertEqual((first, retried), ("01", "01"))
        self.assertEqual(json.loads((run / "run.json").read_text())["attempt_count"], 1)

    def test_full_lifecycle_hash_binds_preview_pdf_review_and_final_delivery(self) -> None:
        harness = _load_harness()
        run = self.root / "lifecycle-run"
        source = self.root / "paper.txt"
        source.write_text(
            "Grounded poster source reports 85% accuracy and uses two-stage routing. "
            "The grounded poster retains accuracy.",
            encoding="utf-8",
        )
        self._initialize_no_visual_run(harness, run, source)
        attempt_info = harness.begin_poster_attempt(run)
        attempt_id = attempt_info["attempt_id"]
        poster_path = run / attempt_info["poster_path"]
        poster_path.write_text(_poster_html(), encoding="utf-8")
        source_map = self.root / "source-map-input.json"
        source_map.write_text(json.dumps({"claims": _claims()}), encoding="utf-8")

        def fake_render(*, attempt_root: Path, **_kwargs: object) -> dict[str, object]:
            preview = attempt_root / "qa" / "previews" / "poster.png"
            preview.write_bytes(b"png-preview")
            print_preview = attempt_root / "qa" / "previews" / "poster-print.png"
            print_preview.write_bytes(b"pdf-png-preview")
            (attempt_root / "artifact" / "preview.png").write_bytes(b"pdf-png-preview")
            (attempt_root / "artifact" / "poster.pdf").write_bytes(b"%PDF-fixture")
            return {
                "passed": True,
                "checks": [
                    {"id": "browser_geometry", "passed": True, "detail": "passed"},
                    {"id": "single_page_pdf", "passed": True, "detail": "passed"},
                ],
                "preview_paths": {
                    "poster_pdf": "qa/previews/poster-print.png",
                    "poster_screen": "qa/previews/poster.png",
                },
            }

        with mock.patch.object(harness, "_render_poster_outputs", side_effect=fake_render):
            deterministic = harness.validate_poster_attempt(
                run,
                attempt_id,
                source_map_path=source_map,
                allow_browser_install=False,
            )
        self.assertTrue(deterministic["passed"], deterministic)
        context = harness.create_poster_review_context(run, attempt_id)
        review = {
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
            "dimension_scores": {name: 4 for name in harness.REVIEW_RUBRIC["dimensions"]},
            "blockers": [],
            "localized_repairs": [],
            "repair_route": None,
            "route_findings": [],
            "verdict": "pass",
            "complete": True,
        }
        review_path = self.root / "review.json"
        review_path.write_text(json.dumps(review), encoding="utf-8")
        harness.record_poster_review(run, attempt_id, review_path)
        manifest = harness.finalize_poster_attempt(run, attempt_id)
        self.assertEqual(manifest["verification_status"], "verified")
        self.assertEqual(
            set(manifest["files"]),
            {"poster.html", "poster.pdf", "preview.png", "provenance/source-map.json"},
        )
        self.assertEqual(
            harness.resume_poster_run(run)["next_action"],
            "complete",
        )

    def test_validation_recovers_after_hard_interrupt_left_generated_outputs(self) -> None:
        harness = _load_harness()
        run = self.root / "interrupted-render-run"
        source = self.root / "paper.txt"
        source.write_text(
            "Grounded poster source reports 85% accuracy and uses two-stage routing. "
            "The grounded poster retains accuracy.",
            encoding="utf-8",
        )
        self._initialize_no_visual_run(harness, run, source)
        attempt_info = harness.begin_poster_attempt(run)
        attempt_id = attempt_info["attempt_id"]
        (run / attempt_info["poster_path"]).write_text(_poster_html(), encoding="utf-8")
        source_map = self.root / "interrupted-source-map.json"
        source_map.write_text(json.dumps({"claims": _claims()}), encoding="utf-8")

        def interrupted_render(*, attempt_root: Path, **_kwargs: object) -> dict[str, object]:
            preview = attempt_root / "qa" / "previews" / "poster.png"
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview.write_bytes(b"stale-qa-preview")
            (attempt_root / "artifact" / "preview.png").write_bytes(b"stale-preview")
            (attempt_root / "artifact" / "poster.pdf").write_bytes(b"stale-pdf")
            raise KeyboardInterrupt("simulated hard interruption")

        with mock.patch.object(harness, "_render_poster_outputs", side_effect=interrupted_render):
            with self.assertRaisesRegex(KeyboardInterrupt, "hard interruption"):
                harness.validate_poster_attempt(
                    run,
                    attempt_id,
                    source_map_path=source_map,
                    allow_browser_install=False,
                )

        stale_atomic_output = run / "attempts" / attempt_id / "artifact" / ".poster.pdf.tmp-deadbeef"
        stale_atomic_output.write_bytes(b"partial-pdf")

        def fresh_render(*, attempt_root: Path, **_kwargs: object) -> dict[str, object]:
            preview = attempt_root / "qa" / "previews" / "poster.png"
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview.write_bytes(b"fresh-qa-preview")
            print_preview = attempt_root / "qa" / "previews" / "poster-print.png"
            print_preview.write_bytes(b"fresh-print-preview")
            (attempt_root / "artifact" / "preview.png").write_bytes(b"fresh-print-preview")
            (attempt_root / "artifact" / "poster.pdf").write_bytes(b"fresh-pdf")
            return {
                "passed": True,
                "checks": [
                    {"id": "browser_geometry", "passed": True, "detail": "passed"},
                    {"id": "computed_typography", "passed": True, "detail": "passed"},
                    {"id": "single_page_pdf", "passed": True, "detail": "passed"},
                ],
                "preview_paths": {
                    "poster_pdf": "qa/previews/poster-print.png",
                    "poster_screen": "qa/previews/poster.png",
                },
            }

        with mock.patch.object(harness, "_render_poster_outputs", side_effect=fresh_render):
            deterministic = harness.validate_poster_attempt(
                run,
                attempt_id,
                source_map_path=source_map,
                allow_browser_install=False,
            )
        self.assertTrue(deterministic["passed"], deterministic)
        attempt = run / "attempts" / attempt_id
        self.assertEqual(
            (attempt / "artifact" / "preview.png").read_bytes(), b"fresh-print-preview"
        )
        self.assertEqual((attempt / "artifact" / "poster.pdf").read_bytes(), b"fresh-pdf")
        self.assertFalse(stale_atomic_output.exists())

    def test_interrupted_output_cleanup_rejects_symlinked_parent_without_deleting_outside(self) -> None:
        harness = _load_harness()
        attempt = self.root / "attempt"
        (attempt / "artifact").mkdir(parents=True)
        outside = self.root / "outside"
        (outside / "previews").mkdir(parents=True)
        marker = outside / "previews" / "KEEP.txt"
        marker.write_text("keep", encoding="utf-8")
        (attempt / "qa").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(harness.core.PathSafetyError, "symlink"):
            harness._clear_generated_attempt_outputs(attempt)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    @unittest.skipUnless(
        __import__("os").environ.get("AUTODESIGN_SKILL_REAL_BROWSER") == "1",
        "set AUTODESIGN_SKILL_REAL_BROWSER=1 for pinned Chromium/PDF integration",
    )
    def test_real_browser_renders_preview_and_exact_one_page_pdf(self) -> None:
        harness = _load_harness()
        for generated in SKILL_ROOT.rglob("__pycache__"):
            shutil.rmtree(generated)
        run = self.root / "real-browser-run"
        source = self.root / "paper.txt"
        source.write_text(
            "Grounded poster source reports 85% accuracy and uses two-stage routing. "
            "The grounded poster retains accuracy.",
            encoding="utf-8",
        )
        self._initialize_no_visual_run(harness, run, source)
        attempt = harness.begin_poster_attempt(run)
        (run / attempt["poster_path"]).write_text(_poster_html(), encoding="utf-8")
        source_map = self.root / "real-source-map.json"
        source_map.write_text(json.dumps({"claims": _claims()}), encoding="utf-8")
        result = harness.validate_poster_attempt(
            run,
            attempt["attempt_id"],
            source_map_path=source_map,
            cache_root=Path(__import__("os").environ["AUTODESIGN_SKILL_BROWSER_CACHE"]),
            allow_browser_install=False,
        )
        self.assertTrue(result["passed"], result)
        attempt_root = run / "attempts" / attempt["attempt_id"]
        self.assertGreater((attempt_root / "artifact" / "poster.pdf").stat().st_size, 1000)
        self.assertGreater((attempt_root / "artifact" / "preview.png").stat().st_size, 1000)
        pdf_check = next(check for check in result["checks"] if check["id"] == "single_page_pdf")
        self.assertEqual(pdf_check["pages"], 1)
        self.assertFalse(list(SKILL_ROOT.rglob("__pycache__")))

    @unittest.skipUnless(
        __import__("os").environ.get("AUTODESIGN_SKILL_REAL_BROWSER") == "1",
        "set AUTODESIGN_SKILL_REAL_BROWSER=1 for pinned Chromium/PDF integration",
    )
    def test_real_browser_rejects_late_computed_typography_override(self) -> None:
        harness = _load_harness()
        run = self.root / "computed-typography-run"
        source = self.root / "paper.txt"
        source.write_text(
            "Grounded poster source reports 85% accuracy and uses two-stage routing. "
            "The grounded poster retains accuracy.",
            encoding="utf-8",
        )
        self._initialize_no_visual_run(harness, run, source)
        attempt = harness.begin_poster_attempt(run)
        html = _poster_html().replace(
            "</style>",
            """
[data-identity="title"], [data-identity="authors"],
[data-identity="institutions"], h2, p, li, th, td {
  font-size: 8px !important;
}
</style>""",
        )
        (run / attempt["poster_path"]).write_text(html, encoding="utf-8")
        source_map = self.root / "computed-typography-source-map.json"
        source_map.write_text(json.dumps({"claims": _claims()}), encoding="utf-8")
        result = harness.validate_poster_attempt(
            run,
            attempt["attempt_id"],
            source_map_path=source_map,
            cache_root=Path(__import__("os").environ["AUTODESIGN_SKILL_BROWSER_CACHE"]),
            allow_browser_install=False,
        )
        self.assertFalse(result["passed"], result)
        typography = next(
            check for check in result["checks"] if check["id"] == "computed_typography"
        )
        self.assertFalse(typography["passed"], typography)

    @unittest.skipUnless(
        __import__("os").environ.get("AUTODESIGN_SKILL_REAL_BROWSER") == "1",
        "set AUTODESIGN_SKILL_REAL_BROWSER=1 for pinned Chromium/PDF integration",
    )
    def test_real_browser_rejects_nested_text_typography_override(self) -> None:
        harness = _load_harness()
        run = self.root / "nested-typography-run"
        source = self.root / "paper.txt"
        source.write_text(
            "Grounded poster source reports 85% accuracy and uses two-stage routing. ",
            encoding="utf-8",
        )
        self._initialize_no_visual_run(harness, run, source)
        attempt = harness.begin_poster_attempt(run)
        html = _poster_html().replace(
            "Grounded Poster Study",
            '<span style="font-size:8px!important">Grounded Poster Study</span>',
            1,
        )
        (run / attempt["poster_path"]).write_text(html, encoding="utf-8")
        source_map = self.root / "nested-typography-source-map.json"
        source_map.write_text(json.dumps({"claims": _claims()}), encoding="utf-8")
        result = harness.validate_poster_attempt(
            run,
            attempt["attempt_id"],
            source_map_path=source_map,
            cache_root=Path(os.environ["AUTODESIGN_SKILL_BROWSER_CACHE"]),
            allow_browser_install=False,
        )
        self.assertFalse(result["passed"], result)
        typography = next(
            check for check in result["checks"] if check["id"] == "computed_typography"
        )
        self.assertFalse(typography["passed"], typography)

    @unittest.skipUnless(
        __import__("os").environ.get("AUTODESIGN_SKILL_REAL_BROWSER") == "1",
        "set AUTODESIGN_SKILL_REAL_BROWSER=1 for pinned Chromium/PDF integration",
    )
    def test_real_browser_rejects_small_text_in_nonsemantic_body_element(self) -> None:
        harness = _load_harness()
        run = self.root / "div-typography-run"
        source = self.root / "paper.txt"
        source.write_text(
            "Grounded poster source reports 85% accuracy and uses two-stage routing. ",
            encoding="utf-8",
        )
        self._initialize_no_visual_run(harness, run, source)
        attempt = harness.begin_poster_attempt(run)
        html = _poster_html().replace(
            '<p data-claim-id="c-problem" data-source-ids="ev-001">'
            "The grounded poster source reports 85% accuracy.</p>",
            '<div data-claim-id="c-problem" data-source-ids="ev-001" '
            'style="font-size:8px!important">'
            "The grounded poster source reports 85% accuracy.</div>",
            1,
        )
        (run / attempt["poster_path"]).write_text(html, encoding="utf-8")
        source_map = self.root / "div-typography-source-map.json"
        source_map.write_text(json.dumps({"claims": _claims()}), encoding="utf-8")
        result = harness.validate_poster_attempt(
            run,
            attempt["attempt_id"],
            source_map_path=source_map,
            cache_root=Path(os.environ["AUTODESIGN_SKILL_BROWSER_CACHE"]),
            allow_browser_install=False,
        )
        self.assertFalse(result["passed"], result)
        typography = next(
            check for check in result["checks"] if check["id"] == "computed_typography"
        )
        self.assertFalse(typography["passed"], typography)

    @unittest.skipUnless(
        __import__("os").environ.get("AUTODESIGN_SKILL_REAL_BROWSER") == "1",
        "set AUTODESIGN_SKILL_REAL_BROWSER=1 for pinned Chromium/PDF integration",
    )
    def test_real_browser_rejects_obfuscated_css_generated_text(self) -> None:
        harness = _load_harness()
        for index, declaration in enumerate(
            (
                'content/**/:"Fabricated accuracy 999%"',
                'c\\6f ntent:"Fabricated accuracy 999%"',
            ),
            start=1,
        ):
            with self.subTest(declaration=declaration):
                run = self.root / f"generated-content-run-{index}"
                source = self.root / f"paper-{index}.txt"
                source.write_text(
                    "Grounded poster source reports 85% accuracy and uses two-stage routing. ",
                    encoding="utf-8",
                )
                self._initialize_no_visual_run(harness, run, source)
                attempt = harness.begin_poster_attempt(run)
                html = _poster_html().replace(
                    "</style>",
                    'section[data-section-role="method"]::after {'
                    f"{declaration};display:block;font-size:24px" + "}</style>",
                )
                (run / attempt["poster_path"]).write_text(html, encoding="utf-8")
                source_map = self.root / f"generated-content-source-map-{index}.json"
                source_map.write_text(json.dumps({"claims": _claims()}), encoding="utf-8")
                result = harness.validate_poster_attempt(
                    run,
                    attempt["attempt_id"],
                    source_map_path=source_map,
                    cache_root=Path(os.environ["AUTODESIGN_SKILL_BROWSER_CACHE"]),
                    allow_browser_install=False,
                )
                self.assertFalse(result["passed"], result)
                typography = next(
                    check
                    for check in result["checks"]
                    if check["id"] == "computed_typography"
                )
                self.assertFalse(typography["passed"], typography)
                self.assertIn("generated content", typography["detail"])

    @unittest.skipUnless(
        __import__("os").environ.get("AUTODESIGN_SKILL_REAL_BROWSER") == "1",
        "set AUTODESIGN_SKILL_REAL_BROWSER=1 for pinned Chromium/PDF integration",
    )
    def test_real_browser_rejects_obfuscated_list_marker_text(self) -> None:
        harness = _load_harness()
        run = self.root / "generated-marker-run"
        source = self.root / "paper.txt"
        source.write_text(
            "Grounded poster source reports 85% accuracy and uses two-stage routing. ",
            encoding="utf-8",
        )
        self._initialize_no_visual_run(harness, run, source)
        attempt = harness.begin_poster_attempt(run)
        html = _poster_html().replace(
            "</style>",
            'li::marker { c\\6f ntent:"Fabricated accuracy 999% "; }</style>',
        )
        (run / attempt["poster_path"]).write_text(html, encoding="utf-8")
        source_map = self.root / "generated-marker-source-map.json"
        source_map.write_text(json.dumps({"claims": _claims()}), encoding="utf-8")
        result = harness.validate_poster_attempt(
            run,
            attempt["attempt_id"],
            source_map_path=source_map,
            cache_root=Path(os.environ["AUTODESIGN_SKILL_BROWSER_CACHE"]),
            allow_browser_install=False,
        )
        self.assertFalse(result["passed"], result)
        typography = next(
            check for check in result["checks"] if check["id"] == "computed_typography"
        )
        self.assertFalse(typography["passed"], typography)
        self.assertIn("generated content", typography["detail"])

    @unittest.skipUnless(
        __import__("os").environ.get("AUTODESIGN_SKILL_REAL_BROWSER") == "1",
        "set AUTODESIGN_SKILL_REAL_BROWSER=1 for pinned Chromium/PDF integration",
    )
    def test_real_browser_rejects_screen_only_typography_override(self) -> None:
        harness = _load_harness()
        run = self.root / "screen-typography-run"
        source = self.root / "paper.txt"
        source.write_text(
            "Grounded poster source reports 85% accuracy and uses two-stage routing. ",
            encoding="utf-8",
        )
        self._initialize_no_visual_run(harness, run, source)
        attempt = harness.begin_poster_attempt(run)
        html = _poster_html().replace(
            "</style>",
            """
@media screen {
  [data-identity="title"], [data-identity="authors"],
  [data-identity="institutions"], h2, p, li, th, td {
    font-size: 8px !important;
  }
}
</style>""",
        )
        (run / attempt["poster_path"]).write_text(html, encoding="utf-8")
        source_map = self.root / "screen-typography-source-map.json"
        source_map.write_text(json.dumps({"claims": _claims()}), encoding="utf-8")
        result = harness.validate_poster_attempt(
            run,
            attempt["attempt_id"],
            source_map_path=source_map,
            cache_root=Path(os.environ["AUTODESIGN_SKILL_BROWSER_CACHE"]),
            allow_browser_install=False,
        )
        self.assertFalse(result["passed"], result)
        typography = next(
            check for check in result["checks"] if check["id"] == "computed_typography"
        )
        self.assertFalse(typography["passed"], typography)

    @unittest.skipUnless(
        __import__("os").environ.get("AUTODESIGN_SKILL_REAL_BROWSER") == "1",
        "set AUTODESIGN_SKILL_REAL_BROWSER=1 for pinned Chromium/PDF integration",
    )
    def test_review_context_binds_pdf_raster_when_print_layout_differs(self) -> None:
        harness = _load_harness()
        run = self.root / "print-preview-run"
        source = self.root / "paper.txt"
        source.write_text(
            "Grounded poster source reports 85% accuracy and uses two-stage routing. ",
            encoding="utf-8",
        )
        self._initialize_no_visual_run(harness, run, source)
        attempt = harness.begin_poster_attempt(run)
        html = _poster_html().replace(
            "@media print {",
            "@media print { .paper-poster { transform: scale(.5); transform-origin: top left; }",
        )
        (run / attempt["poster_path"]).write_text(html, encoding="utf-8")
        source_map = self.root / "print-preview-source-map.json"
        source_map.write_text(json.dumps({"claims": _claims()}), encoding="utf-8")
        result = harness.validate_poster_attempt(
            run,
            attempt["attempt_id"],
            source_map_path=source_map,
            cache_root=Path(os.environ["AUTODESIGN_SKILL_BROWSER_CACHE"]),
            allow_browser_install=False,
        )
        self.assertTrue(result["passed"], result)
        context = harness.create_poster_review_context(run, attempt["attempt_id"])
        self.assertEqual(set(context["preview_hashes"]), {"poster_pdf", "poster_screen"})
        attempt_root = run / "attempts" / attempt["attempt_id"]
        print_preview = attempt_root / "qa" / "previews" / "poster-print.png"
        screen_preview = attempt_root / "qa" / "previews" / "poster.png"
        self.assertNotEqual(print_preview.read_bytes(), screen_preview.read_bytes())
        self.assertEqual(
            (attempt_root / "artifact" / "preview.png").read_bytes(),
            print_preview.read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()
