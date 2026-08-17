from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
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
        with self.assertRaises(core.StateError):
            core.crop_source(v1, {})

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


if __name__ == "__main__":
    unittest.main()
