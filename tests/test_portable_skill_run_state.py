from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
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


def _seed_sync_fixture(root: Path) -> None:
    shared = root / "_shared"
    shared.mkdir(parents=True)
    sources = {
        "portable_core.py": b"canonical-core",
        "source-grounding.md": b"canonical-grounding",
        "browser_worker.py": b"canonical-browser-worker",
        "setup_browser.py": b"canonical-browser-setup",
        "requirements-browser.lock": b"canonical-browser-lock",
        "portable_png.py": b"canonical-png",
    }
    for name, data in sources.items():
        (shared / name).write_bytes(data)
    for skill in SKILLS:
        package = root / skill
        (package / "scripts").mkdir(parents=True)
        (package / "references").mkdir()
        (package / "scripts" / "_portable.py").write_bytes(sources["portable_core.py"])
        (package / "scripts" / "browser_worker.py").write_bytes(sources["browser_worker.py"])
        (package / "scripts" / "setup_browser.py").write_bytes(sources["setup_browser.py"])
        (package / "scripts" / "requirements-browser.lock").write_bytes(
            sources["requirements-browser.lock"]
        )
        (package / "references" / "source-grounding.md").write_bytes(
            sources["source-grounding.md"]
        )
    (root / "autodesign-poster" / "scripts" / "portable_png.py").write_bytes(
        sources["portable_png.py"]
    )


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
        source = self.root / f"default-source-{self.run.name}.txt"
        source.write_text(
            "Grounded poster source reports 85% accuracy.\n", encoding="utf-8"
        )
        core.prepare_source(self.run, source)
        core.save_plan(self.run, {"artifact_type": "poster", "visual_allocations": []})
        attempt = core.begin_attempt(self.run)
        core.write_source_map(
            self.run,
            attempt,
            [
                {
                    "id": "default-claim",
                    "text": "The source reports 85% accuracy.",
                    "source_ids": ["ev-001"],
                }
            ],
        )
        return attempt

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
            "source_map_sha256": context["source_map_sha256"],
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

    def _fake_poppler(
        self,
        name: str,
        *,
        text: str = "Sparse routing reaches 85% accuracy.\n",
        page_count: int = 1,
        image_rows: tuple[tuple[int, int], ...] = ((1, 0),),
        extracted_image_count: int | None = None,
        fail_extract: bool = False,
    ) -> dict[str, Path]:
        bin_dir = self.root / name
        bin_dir.mkdir()
        script = f'''#!/usr/bin/env python3
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
if name == "pdfinfo":
    print("Pages: {page_count}")
elif name == "pdftotext":
    Path(sys.argv[-1]).write_text({text!r}, encoding="utf-8")
elif name == "pdftoppm":
    for page in range(1, {page_count} + 1):
        Path(sys.argv[-1] + f"-{{page}}.png").write_bytes(f"page-{{page}}".encode())
elif name == "pdfimages" and "-list" in sys.argv:
    print("page num type width height color comp bpc enc interp object ID x-ppi y-ppi size ratio")
    for page, number in {image_rows!r}:
        print(f"{{page}} {{number}} image 10 10 rgb 3 8 image no {{number}} 0 72 72 1B 1%")
elif name == "pdfimages":
    for index in range({len(image_rows) if extracted_image_count is None else extracted_image_count}):
        Path(sys.argv[-1] + f"-{{index:03d}}.png").write_bytes(f"image-{{index}}".encode())
    if {fail_extract!r}:
        raise SystemExit(1)
'''
        tools: dict[str, Path] = {}
        for tool_name in ("pdftotext", "pdfinfo", "pdftoppm", "pdfimages"):
            executable = bin_dir / tool_name
            executable.write_text(script, encoding="utf-8")
            executable.chmod(0o755)
            tools[tool_name] = executable
        return tools

    def _prepare_pdf(
        self,
        *,
        run: Path | None = None,
        source_bytes: bytes = b"%PDF-1.4\nfixture\n",
        tools: dict[str, Path] | None = None,
    ) -> dict[str, object]:
        target_run = run or self.run
        source = self.root / f"paper-{target_run.name}.pdf"
        source.write_bytes(source_bytes)
        return core.prepare_source(
            target_run,
            source,
            tool_paths=tools or self._fake_poppler(f"tools-{target_run.name}"),
        )

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

    def test_run_operations_reject_symlinked_internal_directories_without_external_writes(self) -> None:
        for relative in ("input", "evidence/assets"):
            with self.subTest(relative=relative):
                self.run = self.root / "workspace" / relative.replace("/", "-")
                self._initialize()
                target = self.run / relative
                shutil.rmtree(target)
                outside = self.root / f"outside-{relative.replace('/', '-')}"
                outside.mkdir()
                target.symlink_to(outside, target_is_directory=True)
                source = self.root / f"source-{relative.replace('/', '-')}.txt"
                source.write_text("Grounded source.\n", encoding="utf-8")
                asset = self.root / f"asset-{relative.replace('/', '-')}.png"
                asset.write_bytes(b"asset")
                with self.assertRaises(core.PathSafetyError):
                    core.prepare_source(self.run, source, extra_assets=[asset])
                self.assertEqual(list(outside.iterdir()), [])

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

    def test_secret_redaction_removes_complete_authentication_header_values(self) -> None:
        redacted = core.redact_secrets(
            "Cookie: sid=abc; theme=dark\n"
            "Set-Cookie: session=secret; HttpOnly; Path=/\n"
            "Authorization: Basic Zm9vOmJhcg==\n"
            "X-Visible: keep"
        )
        self.assertEqual(
            redacted,
            "Cookie: [REDACTED]\n"
            "Set-Cookie: [REDACTED]\n"
            "Authorization: [REDACTED]\n"
            "X-Visible: keep",
        )

    def test_secret_redaction_removes_prefixed_trace_header_values(self) -> None:
        redacted = core.redact_secrets(
            "curl trace: > Authorization: Basic Zm9vOmJhcg==\n"
            "debug: < Cookie: sid=abc; theme=dark\n"
            "proxy: < Set-Cookie: session=secret; HttpOnly\n"
            "visible: keep"
        )
        self.assertEqual(
            redacted,
            "curl trace: > Authorization: [REDACTED]\n"
            "debug: < Cookie: [REDACTED]\n"
            "proxy: < Set-Cookie: [REDACTED]\n"
            "visible: keep",
        )

    def test_secret_redaction_covers_prefixed_variable_names_and_crlf_headers(self) -> None:
        redacted = core.redact_secrets(
            "OPENAI_API_KEY=one\n"
            "ANTHROPIC_AUTH_TOKEN: two\n"
            "AWS_SECRET_ACCESS_KEY=three\n"
            "private_key=four\n"
            "session_cookie=five\n"
            "trace: > Authorization: Basic Zm9vOmJhcg==\r\n"
            "trace: < Cookie: sid=secret; theme=dark\r\n"
            "trace: < Set-Cookie: session=secret; HttpOnly\r\n"
        )
        self.assertEqual(
            redacted,
            "OPENAI_API_KEY=[REDACTED]\n"
            "ANTHROPIC_AUTH_TOKEN=[REDACTED]\n"
            "AWS_SECRET_ACCESS_KEY=[REDACTED]\n"
            "private_key=[REDACTED]\n"
            "session_cookie=[REDACTED]\n"
            "trace: > Authorization: [REDACTED]\r\n"
            "trace: < Cookie: [REDACTED]\r\n"
            "trace: < Set-Cookie: [REDACTED]\r\n",
        )

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

    def test_runtime_snapshot_captures_assets_and_resume_rejects_asset_drift(self) -> None:
        runtime = self.skill / "assets" / "video-runtime"
        runtime.mkdir(parents=True)
        assets = {
            "package.json": b'{"name":"video-runtime"}\n',
            "package-lock.json": b'{"lockfileVersion":3}\n',
            "requirements-kokoro.in": b"kokoro>=0.9\n",
            "requirements-kokoro.lock": b"kokoro==0.9.4\n",
        }
        for name, data in assets.items():
            (runtime / name).write_bytes(data)
        before = core.tree_hash(self.skill)

        self._initialize()

        manifest = json.loads(
            (self.run / "skill_snapshot" / "manifest.json").read_text(encoding="utf-8")
        )
        paths = {entry["path"] for entry in manifest["files"]}
        for name, data in assets.items():
            relative = f"assets/video-runtime/{name}"
            self.assertIn(relative, paths)
            self.assertEqual(
                (self.run / "skill_snapshot" / "files" / relative).read_bytes(),
                data,
            )
        self.assertEqual(core.tree_hash(self.skill), before)

        (runtime / "package.json").write_bytes(b'{"name":"drifted"}\n')
        with self.assertRaises(core.IntegrityError):
            core.resume_run(self.run, skill_root=self.skill)

    def test_runtime_snapshot_ignores_generated_assets_and_rejects_asset_symlinks(self) -> None:
        runtime = self.skill / "assets" / "video-runtime"
        runtime.mkdir(parents=True)
        (runtime / "package.json").write_bytes(b'{"name":"video-runtime"}\n')
        generated = (
            runtime / "node_modules" / "dependency" / "package.json",
            runtime / "dist" / "bundle.js",
            runtime / "__pycache__" / "helper.cpython-313.pyc",
            runtime / ".DS_Store",
        )
        for path in generated:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"generated")

        self._initialize()

        manifest = json.loads(
            (self.run / "skill_snapshot" / "manifest.json").read_text(encoding="utf-8")
        )
        paths = {entry["path"] for entry in manifest["files"]}
        self.assertIn("assets/video-runtime/package.json", paths)
        self.assertTrue(
            all(path.relative_to(self.skill).as_posix() not in paths for path in generated)
        )
        for path in generated:
            path.write_bytes(b"changed generated output")
        self.assertEqual(
            core.resume_run(self.run, skill_root=self.skill)["next_action"],
            "prepare_source",
        )

        outside = self.root / "outside-package.json"
        outside.write_bytes(b"outside")
        (runtime / "linked-package.json").symlink_to(outside)
        with self.assertRaises(core.PathSafetyError):
            core.resume_run(self.run, skill_root=self.skill)

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

    def test_snapshot_verification_rejects_unlisted_extra_files(self) -> None:
        self._initialize()
        extra = self.run / "skill_snapshot" / "files" / "scripts" / "unlisted.py"
        extra.write_text("UNLISTED = True\n", encoding="utf-8")
        with self.assertRaises(core.IntegrityError):
            core.verify_skill_snapshot(self.run)

    def test_runtime_snapshot_excludes_generated_python_caches(self) -> None:
        cache = self.skill / "scripts" / "__pycache__"
        cache.mkdir()
        (cache / "tool.cpython-313.pyc").write_bytes(b"platform-specific")
        (self.skill / "scripts" / "leftover.pyc").write_bytes(b"cache")
        (self.skill / "scripts" / "leftover.pyo").write_bytes(b"optimized-cache")
        (self.skill / "scripts" / ".pytest_cache").mkdir()
        (self.skill / "scripts" / ".pytest_cache" / "state").write_text(
            "generated", encoding="utf-8"
        )

        self._initialize()
        manifest = json.loads(
            (self.run / "skill_snapshot" / "manifest.json").read_text(encoding="utf-8")
        )
        paths = {entry["path"] for entry in manifest["files"]}
        self.assertEqual(
            paths,
            {"SKILL.md", "scripts/tool.py", "references/grounding.md"},
        )
        (cache / "tool.cpython-313.pyc").write_bytes(b"changed-after-import")
        self.assertEqual(
            core.resume_run(self.run, skill_root=self.skill)["next_action"],
            "prepare_source",
        )

    def test_existing_run_rejects_release_version_and_archive_drift(self) -> None:
        self._initialize()
        with self.assertRaises(core.IntegrityError):
            core.initialize_run(
                self.run,
                self.skill,
                release_version="0.2.0",
                archive_sha256="a" * 64,
            )
        with self.assertRaises(core.IntegrityError):
            core.initialize_run(
                self.run,
                self.skill,
                release_version="0.1.0",
                archive_sha256="b" * 64,
            )

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

    def test_markdown_evidence_preserves_preamble_headings_title_and_authors(self) -> None:
        self._initialize()
        source = self.root / "complete-paper.md"
        source.write_text(
            "Conference preamble\n"
            "# Sparse Routing\n"
            "Alice Example, Bob Example\n"
            "## Method\n"
            "We route tokens sparsely.\n",
            encoding="utf-8",
        )

        core.prepare_source(self.run, source)
        evidence = core.load_evidence(self.run)

        self.assertEqual(
            [item["text"] for item in evidence],
            [
                "Conference preamble",
                "# Sparse Routing\nAlice Example, Bob Example",
                "## Method\nWe route tokens sparsely.",
            ],
        )
        self.assertEqual(
            [
                (item["anchor"]["line_start"], item["anchor"]["line_end"])
                for item in evidence
            ],
            [(1, 1), (2, 3), (4, 5)],
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
            core.resume_run(self.run, skill_root=self.skill)

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
    print("1 0 image 10 10")
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

    def test_pdf_commands_read_immutable_copied_input_after_external_source_mutates(self) -> None:
        self._initialize()
        source = self.root / "mutable-paper.pdf"
        source.write_bytes(b"SOURCE-A")
        log_path = self.root / "immutable-input.log"
        bin_dir = self.root / "mutating-poppler"
        bin_dir.mkdir()
        script = f'''#!/usr/bin/env python3
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
if name in ("pdfinfo",) or (name == "pdfimages" and "-list" in sys.argv):
    source_arg = Path(sys.argv[-1])
else:
    source_arg = Path(sys.argv[-2])
with Path({str(log_path)!r}).open("a", encoding="utf-8") as handle:
    handle.write(name + "|" + str(source_arg) + "\\n")
if name == "pdfinfo":
    Path({str(source)!r}).write_bytes(b"SOURCE-B-MUTATED")
    print("Pages: 1")
elif name == "pdftotext":
    Path(sys.argv[-1]).write_text(source_arg.read_bytes().decode(), encoding="utf-8")
elif name == "pdftoppm":
    Path(sys.argv[-1] + "-1.png").write_bytes(b"page")
elif name == "pdfimages" and "-list" in sys.argv:
    print("page num type width height")
elif name == "pdfimages":
    pass
'''
        tools: dict[str, Path] = {}
        for name in ("pdftotext", "pdfinfo", "pdftoppm", "pdfimages"):
            executable = bin_dir / name
            executable.write_text(script, encoding="utf-8")
            executable.chmod(0o755)
            tools[name] = executable

        manifest = core.prepare_source(self.run, source, tool_paths=tools)

        copied = self.run / "input" / "source.pdf"
        self.assertEqual(source.read_bytes(), b"SOURCE-B-MUTATED")
        self.assertEqual(copied.read_bytes(), b"SOURCE-A")
        self.assertEqual(manifest["source_sha256"], hashlib.sha256(b"SOURCE-A").hexdigest())
        self.assertEqual(manifest["source_size"], len(b"SOURCE-A"))
        self.assertEqual(core.load_evidence(self.run)[0]["text"], "SOURCE-A")
        self.assertEqual(
            {line.split("|", 1)[1] for line in log_path.read_text().splitlines()},
            {str(copied)},
        )

    def test_pdf_retry_removes_stale_pages_and_extracted_images(self) -> None:
        self._initialize()
        source = self.root / "retry.pdf"
        source.write_bytes(b"%PDF-1.4\nretry\n")
        blocked = core.prepare_source(
            self.run,
            source,
            tool_paths=self._fake_poppler(
                "poppler-a",
                text="Source A.\n",
                page_count=2,
                image_rows=((1, 0), (2, 1)),
                fail_extract=True,
            ),
        )
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue((self.run / "evidence" / "pages" / "page-2.png").is_file())
        self.assertTrue((self.run / "evidence" / "assets" / "pdf-image-001.png").is_file())

        ready = core.prepare_source(
            self.run,
            source,
            tool_paths=self._fake_poppler(
                "poppler-b", text="Source B.\n", page_count=1, image_rows=((1, 7),)
            ),
        )
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(
            sorted(path.name for path in (self.run / "evidence" / "pages").iterdir()),
            ["page-1.png"],
        )
        self.assertEqual(
            sorted(
                path.name
                for path in (self.run / "evidence" / "assets").glob("pdf-image*")
            ),
            ["pdf-image-000.png"],
        )

    def test_ready_source_cannot_be_silently_replaced(self) -> None:
        self._initialize()
        first = self.root / "first.txt"
        second = self.root / "second.txt"
        first.write_text("Source A.\n", encoding="utf-8")
        second.write_text("Source B.\n", encoding="utf-8")
        core.prepare_source(self.run, first)
        before = (self.run / "evidence" / "source_manifest.json").read_bytes()
        with self.assertRaises(core.StateError):
            core.prepare_source(self.run, second)
        self.assertEqual(
            (self.run / "evidence" / "source_manifest.json").read_bytes(), before
        )

    def test_ready_source_cannot_be_replaced_after_unrelated_blocked_side_state(self) -> None:
        self._initialize()
        first = self.root / "ready-first.txt"
        second = self.root / "ready-second.txt"
        first.write_text("Source A.\n", encoding="utf-8")
        second.write_text("Source B.\n", encoding="utf-8")
        core.prepare_source(self.run, first)
        before = (self.run / "evidence" / "source_manifest.json").read_bytes()
        core.mark_side_state(self.run, "blocked", reason="unrelated downstream blocker")

        with self.assertRaises(core.StateError):
            core.prepare_source(self.run, second)
        self.assertEqual(
            (self.run / "evidence" / "source_manifest.json").read_bytes(), before
        )

    def test_pdf_visuals_record_pdfimages_page_and_object_number(self) -> None:
        self._initialize()
        manifest = self._prepare_pdf(
            tools=self._fake_poppler(
                "poppler-mapping",
                text="Caption one.\fCaption two.\fCaption three.\n",
                page_count=3,
                image_rows=((1, 4), (3, 9)),
            )
        )
        self.assertEqual(manifest["status"], "ready")
        visuals = json.loads(
            (self.run / "evidence" / "source_visuals.json").read_text(encoding="utf-8")
        )["visuals"]
        self.assertEqual(
            [(item["page"], item["pdf_image_num"]) for item in visuals],
            [(1, 4), (3, 9)],
        )

    def test_pdf_visual_mapping_mismatch_blocks_instead_of_registering_unknown_page(self) -> None:
        self._initialize()
        manifest = self._prepare_pdf(
            tools=self._fake_poppler(
                "poppler-mismatch",
                image_rows=(),
                extracted_image_count=1,
            )
        )
        self.assertEqual(manifest["status"], "blocked")
        self.assertIn("pdfimages_mapping", manifest["failed_commands"])
        visuals = json.loads(
            (self.run / "evidence" / "source_visuals.json").read_text(encoding="utf-8")
        )["visuals"]
        self.assertEqual(visuals, [])

    def test_pdf_rendered_pages_are_exact_set_and_hash_bound(self) -> None:
        mutations = {
            "extra": lambda pages: (pages / "page-3.png").write_bytes(b"extra"),
            "missing": lambda pages: (pages / "page-2.png").unlink(),
            "tampered": lambda pages: (pages / "page-1.png").write_bytes(b"tampered"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.run = self.root / "workspace" / f"pdf-pages-{name}"
                self._initialize()
                manifest = self._prepare_pdf(
                    tools=self._fake_poppler(
                        f"poppler-pages-{name}",
                        page_count=2,
                        image_rows=((1, 0),),
                    )
                )
                self.assertEqual(
                    set(manifest["rendered_pages"]),
                    {"evidence/pages/page-1.png", "evidence/pages/page-2.png"},
                )
                mutate(self.run / "evidence" / "pages")
                with self.assertRaises(core.IntegrityError):
                    core.resume_run(self.run, skill_root=self.skill)

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
        source_manifest_path = self.run / "evidence" / "source_manifest.json"
        source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
        source_manifest["source_visuals_sha256"] = core.sha256_file(visuals_path)
        source_manifest["visual_count"] = len(visuals["visuals"])
        core.atomic_write_json(source_manifest_path, source_manifest)
        run_state_path = self.run / "run.json"
        run_state = json.loads(run_state_path.read_text(encoding="utf-8"))
        run_state["source_manifest_sha256"] = core.sha256_file(source_manifest_path)
        core.atomic_write_json(run_state_path, run_state)
        with self.assertRaises(core.ContractError):
            core.validate_visual_plan(
                self.run, [{"visual_id": "vis-002", "role": "method"}]
            )

        core.bind_host_vlm_visuals(
            self.run,
            {
                "reviewer_mode": "fresh_host_vlm",
                "source_manifest_sha256": core.sha256_file(
                    self.run / "evidence" / "source_manifest.json"
                ),
                "source_visuals_sha256": core.sha256_file(visuals_path),
                "matches": [
                    {
                        "visual_id": "vis-002",
                        "visual_sha256": pdf_candidate["sha256"],
                        "caption_evidence_id": "ev-001",
                        "caption_evidence_sha256": core.load_evidence(self.run)[0]["sha256"],
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

    def test_host_vlm_visual_review_is_bound_to_source_visual_and_caption_hashes(self) -> None:
        self._initialize()
        self._prepare_pdf(
            tools=self._fake_poppler(
                "poppler-bound-a", text="Figure A caption.\n", image_rows=((1, 2),)
            )
        )
        visual_contract = json.loads(
            (self.run / "evidence" / "source_visuals.json").read_text(encoding="utf-8")
        )
        visual = visual_contract["visuals"][0]
        evidence = core.load_evidence(self.run)[0]
        review = {
            "reviewer_mode": "fresh_host_vlm",
            "source_manifest_sha256": core.sha256_file(
                self.run / "evidence" / "source_manifest.json"
            ),
            "source_visuals_sha256": core.sha256_file(
                self.run / "evidence" / "source_visuals.json"
            ),
            "matches": [
                {
                    "visual_id": visual["id"],
                    "visual_sha256": visual["sha256"],
                    "caption_evidence_id": evidence["id"],
                    "caption_evidence_sha256": evidence["sha256"],
                    "confidence": 0.95,
                    "allowed_content_roles": ["method"],
                }
            ],
        }

        other_run = self.root / "workspace" / "other-run"
        core.initialize_run(
            other_run,
            self.skill,
            release_version="0.1.0",
            archive_sha256="a" * 64,
        )
        self._prepare_pdf(
            run=other_run,
            source_bytes=b"%PDF-1.4\nsource-b\n",
            tools=self._fake_poppler(
                "poppler-bound-b", text="Figure B caption.\n", image_rows=((1, 2),)
            ),
        )
        with self.assertRaises(core.ContractError):
            core.bind_host_vlm_visuals(other_run, review)

    def test_host_vlm_visual_review_requires_finite_real_confidence_in_range(self) -> None:
        self._initialize()
        self._prepare_pdf(
            tools=self._fake_poppler(
                "poppler-confidence", text="Figure caption.\n", image_rows=((1, 2),)
            )
        )
        visual = json.loads(
            (self.run / "evidence" / "source_visuals.json").read_text(encoding="utf-8")
        )["visuals"][0]
        evidence = core.load_evidence(self.run)[0]
        base_review = {
            "reviewer_mode": "fresh_host_vlm",
            "source_manifest_sha256": core.sha256_file(
                self.run / "evidence" / "source_manifest.json"
            ),
            "source_visuals_sha256": core.sha256_file(
                self.run / "evidence" / "source_visuals.json"
            ),
            "matches": [
                {
                    "visual_id": visual["id"],
                    "visual_sha256": visual["sha256"],
                    "caption_evidence_id": evidence["id"],
                    "caption_evidence_sha256": evidence["sha256"],
                    "confidence": 0.9,
                    "allowed_content_roles": ["method"],
                }
            ],
        }
        for confidence in (True, float("nan"), float("inf"), 1.01):
            with self.subTest(confidence=confidence):
                review = json.loads(json.dumps(base_review))
                review["matches"][0]["confidence"] = confidence
                with self.assertRaises(core.ContractError):
                    core.bind_host_vlm_visuals(self.run, review)

    def test_host_vlm_binding_is_rejected_after_planning(self) -> None:
        self._initialize()
        self._prepare_pdf(
            tools=self._fake_poppler(
                "poppler-late-vlm", text="Figure caption.\n", image_rows=((1, 2),)
            )
        )
        visual = json.loads(
            (self.run / "evidence" / "source_visuals.json").read_text(encoding="utf-8")
        )["visuals"][0]
        evidence = core.load_evidence(self.run)[0]
        review = {
            "reviewer_mode": "fresh_host_vlm",
            "source_manifest_sha256": core.sha256_file(
                self.run / "evidence" / "source_manifest.json"
            ),
            "source_visuals_sha256": core.sha256_file(
                self.run / "evidence" / "source_visuals.json"
            ),
            "matches": [
                {
                    "visual_id": visual["id"],
                    "visual_sha256": visual["sha256"],
                    "caption_evidence_id": evidence["id"],
                    "caption_evidence_sha256": evidence["sha256"],
                    "confidence": 0.9,
                    "allowed_content_roles": ["method"],
                }
            ],
        }
        core.save_plan(self.run, {"artifact_type": "poster"})

        with self.assertRaises(core.StateError):
            core.bind_host_vlm_visuals(self.run, review)

    def test_repeated_host_vlm_batches_preserve_hash_bound_authorization_history(self) -> None:
        self._initialize()
        self._prepare_pdf(
            tools=self._fake_poppler(
                "poppler-vlm-history", text="Figure caption.\n", image_rows=((1, 2),)
            )
        )

        def review_for_current_source(confidence: float) -> dict[str, object]:
            visual = json.loads(
                (self.run / "evidence" / "source_visuals.json").read_text(
                    encoding="utf-8"
                )
            )["visuals"][0]
            evidence = core.load_evidence(self.run)[0]
            return {
                "reviewer_mode": "fresh_host_vlm",
                "source_manifest_sha256": core.sha256_file(
                    self.run / "evidence" / "source_manifest.json"
                ),
                "source_visuals_sha256": core.sha256_file(
                    self.run / "evidence" / "source_visuals.json"
                ),
                "matches": [
                    {
                        "visual_id": visual["id"],
                        "visual_sha256": visual["sha256"],
                        "caption_evidence_id": evidence["id"],
                        "caption_evidence_sha256": evidence["sha256"],
                        "confidence": confidence,
                        "allowed_content_roles": ["method"],
                    }
                ],
            }

        first = review_for_current_source(0.9)
        core.bind_host_vlm_visuals(self.run, first)
        second = review_for_current_source(0.95)
        core.bind_host_vlm_visuals(self.run, second)

        sidecar_path = self.run / "evidence" / "host-vlm-visual-review.json"
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        self.assertEqual(sidecar, {"format_version": 1, "batches": [first, second]})
        sidecar_sha256 = core.sha256_file(sidecar_path)
        visuals = json.loads(
            (self.run / "evidence" / "source_visuals.json").read_text(encoding="utf-8")
        )
        manifest = json.loads(
            (self.run / "evidence" / "source_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(visuals["host_vlm_review_sha256"], sidecar_sha256)
        self.assertEqual(manifest["host_vlm_review_sha256"], sidecar_sha256)

        original = sidecar_path.read_bytes()
        sidecar["batches"].pop(0)
        core.atomic_write_json(sidecar_path, sidecar)
        with self.assertRaises(core.IntegrityError):
            core.resume_run(self.run, skill_root=self.skill)
        core.atomic_write_bytes(sidecar_path, original)
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        sidecar["batches"][0]["matches"][0]["confidence"] = 0.99
        core.atomic_write_json(sidecar_path, sidecar)
        with self.assertRaises(core.IntegrityError):
            core.resume_run(self.run, skill_root=self.skill)

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
                    "derived_formula": {"expression": "85 - 80 = 5", "inputs": ["85%", "80%"], "result": "5%"},
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
                        "inputs": ["85%", "80%"],
                        "result": "6%",
                    },
                }
            ],
            evidence,
        )
        self.assertFalse(bad_formula["valid"])
        self.assertIn("invalid_derived_formula", {error["code"] for error in bad_formula["errors"]})

    def test_numeric_grounding_preserves_percent_units_and_normalizes_number_formats(self) -> None:
        text = "Accuracy is 85%, with 1,234.5 examples and 1.2e3 evaluation cases."
        evidence = [
            {
                "id": "ev-001",
                "kind": "text",
                "text": text,
                "safe_to_quote": True,
                "anchor": {"line_start": 1, "line_end": 1},
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        ]
        valid = core.validate_grounding(
            [
                {"id": "percent", "text": "Accuracy is 85%.", "source_ids": ["ev-001"]},
                {"id": "comma", "text": "There are 1234.5 examples.", "source_ids": ["ev-001"]},
                {"id": "scientific", "text": "There are 1,200 evaluation cases.", "source_ids": ["ev-001"]},
            ],
            evidence,
        )
        self.assertTrue(valid["valid"], valid)
        unitless = core.validate_grounding(
            [{"id": "unitless", "text": "Accuracy is 85.", "source_ids": ["ev-001"]}],
            evidence,
        )
        self.assertFalse(unitless["valid"])
        self.assertIn("unsupported_numeric", {item["code"] for item in unitless["errors"]})

        percent_formula = core.validate_grounding(
            [
                {
                    "id": "formula",
                    "text": "Accuracy improves by 5%.",
                    "source_ids": ["ev-002"],
                    "derived_formula": {
                        "expression": "85 - 80 = 5",
                        "inputs": ["85%", "80%"],
                        "result": "5%",
                    },
                }
            ],
            [
                {
                    "id": "ev-002",
                    "kind": "text",
                    "text": "Accuracy improves from 80% to 85%.",
                    "safe_to_quote": True,
                    "anchor": {"line_start": 1, "line_end": 1},
                    "sha256": hashlib.sha256(
                        b"Accuracy improves from 80% to 85%."
                    ).hexdigest(),
                }
            ],
        )
        self.assertTrue(percent_formula["valid"], percent_formula)

    def test_derived_formula_rejects_mixed_percent_and_unitless_values(self) -> None:
        text = "Accuracy is 85% across 80 evaluation cases."
        evidence = [
            {
                "id": "ev-001",
                "kind": "text",
                "text": text,
                "safe_to_quote": True,
                "anchor": {"line_start": 1, "line_end": 1},
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        ]
        result = core.validate_grounding(
            [
                {
                    "id": "mixed-units",
                    "text": "The mixed difference is 5%.",
                    "source_ids": ["ev-001"],
                    "derived_formula": {
                        "expression": "85 - 80 = 5",
                        "inputs": ["85%", "80"],
                        "result": "5%",
                    },
                }
            ],
            evidence,
        )
        self.assertFalse(result["valid"])
        self.assertIn(
            "invalid_derived_formula", {error["code"] for error in result["errors"]}
        )

    def test_unicode_cjk_claims_require_deterministic_lexical_overlap(self) -> None:
        evidence_text = "稀疏路由提高模型准确率。"
        evidence = [
            {
                "id": "ev-001",
                "kind": "text",
                "text": evidence_text,
                "safe_to_quote": True,
                "anchor": {"line_start": 1, "line_end": 1},
                "sha256": hashlib.sha256(evidence_text.encode()).hexdigest(),
            }
        ]
        related = core.validate_grounding(
            [{"id": "related", "text": "稀疏路由提升准确率。", "source_ids": ["ev-001"]}],
            evidence,
        )
        unrelated = core.validate_grounding(
            [{"id": "unrelated", "text": "视频编码降低延迟。", "source_ids": ["ev-001"]}],
            evidence,
        )

        self.assertTrue(related["valid"], related)
        self.assertFalse(unrelated["valid"])
        self.assertIn(
            "insufficient_lexical_overlap",
            {error["code"] for error in unrelated["errors"]},
        )

    def test_numeric_grounding_supports_leading_decimals_and_unicode_minus(self) -> None:
        evidence_text = "The signed change is −.5 and the threshold is .25."
        evidence = [
            {
                "id": "ev-001",
                "kind": "text",
                "text": evidence_text,
                "safe_to_quote": True,
                "anchor": {"line_start": 1, "line_end": 1},
                "sha256": hashlib.sha256(evidence_text.encode()).hexdigest(),
            }
        ]
        result = core.validate_grounding(
            [
                {"id": "negative", "text": "The change is -0.5.", "source_ids": ["ev-001"]},
                {"id": "leading", "text": "The threshold is 0.25.", "source_ids": ["ev-001"]},
                {
                    "id": "formula",
                    "text": "The combined change is −0.75.",
                    "source_ids": ["ev-001"],
                    "derived_formula": {
                        "expression": "−.5 - .25 = −.75",
                        "inputs": ["−.5", ".25"],
                        "result": "−.75",
                    },
                },
            ],
            evidence,
        )
        self.assertTrue(result["valid"], result)

    def test_derived_formula_rejects_hidden_numeric_operands(self) -> None:
        evidence_text = "Accuracy improves from 80% to 85%."
        evidence = [
            {
                "id": "ev-001",
                "kind": "text",
                "text": evidence_text,
                "safe_to_quote": True,
                "anchor": {"line_start": 1, "line_end": 1},
                "sha256": hashlib.sha256(evidence_text.encode()).hexdigest(),
            }
        ]
        result = core.validate_grounding(
            [
                {
                    "id": "hidden-constant",
                    "text": "The adjusted improvement is 6%.",
                    "source_ids": ["ev-001"],
                    "derived_formula": {
                        "expression": "85 - 80 + 1 = 6",
                        "inputs": ["85%", "80%"],
                        "result": "6%",
                    },
                }
            ],
            evidence,
        )
        self.assertFalse(result["valid"])
        self.assertIn(
            "invalid_derived_formula", {error["code"] for error in result["errors"]}
        )

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

    def test_deterministic_result_requires_a_preexisting_source_map(self) -> None:
        self._initialize()
        source = self.root / "map-before-qa.txt"
        source.write_text("Grounded accuracy is 85%.\n", encoding="utf-8")
        core.prepare_source(self.run, source)
        core.save_plan(self.run, {"artifact_type": "poster"})
        attempt = core.begin_attempt(self.run)
        attempt_root = self.run / "attempts" / attempt
        core.atomic_write_bytes(attempt_root / "artifact" / "poster.html", b"artifact")
        core.atomic_write_bytes(attempt_root / "qa" / "previews" / "poster.png", b"preview")

        with self.assertRaises(core.StateError):
            core.record_deterministic_result(
                self.run,
                attempt,
                passed=True,
                checks=[],
                artifact_paths=["artifact/poster.html"],
                preview_paths={"poster": "qa/previews/poster.png"},
            )

    def test_first_source_map_cannot_be_written_after_authoring(self) -> None:
        self._initialize()
        source = self.root / "late-map.txt"
        source.write_text("Grounded accuracy is 85%.\n", encoding="utf-8")
        core.prepare_source(self.run, source)
        core.save_plan(self.run, {"artifact_type": "poster"})
        attempt = core.begin_attempt(self.run)
        core.transition_state(self.run, "deterministic_passed")

        with self.assertRaises(core.StateError):
            core.write_source_map(
                self.run,
                attempt,
                [{"id": "late", "text": "Accuracy is 85%.", "source_ids": ["ev-001"]}],
            )

    def test_review_context_and_semantic_review_echo_source_map_hash(self) -> None:
        attempt, _report = self._deterministic_attempt()
        source_map = self.run / "attempts" / attempt / "provenance" / "source-map.json"
        context = core.create_review_context(
            self.run, attempt, rubric={"format_version": 1, "dimensions": ["fidelity"]}
        )
        self.assertEqual(context.get("source_map_sha256"), core.sha256_file(source_map))
        review = {
            "format_version": 1,
            "attempt_id": attempt,
            "review_context_sha256": context["context_sha256"],
            "artifact_hashes": context["artifact_hashes"],
            "preview_hashes": context["preview_hashes"],
            "reviewed_frame_ids": sorted(context["preview_hashes"]),
            "source_manifest_sha256": context["source_manifest_sha256"],
            "source_map_sha256": context.get("source_map_sha256"),
            "rubric_sha256": context["rubric_sha256"],
            "reviewer_mode": "fresh_subagent",
            "dimension_scores": {"fidelity": 4},
            "blockers": [],
            "localized_repairs": [],
            "verdict": "pass",
            "complete": True,
        }
        review["source_map_sha256"] = "0" * 64
        with self.assertRaises(core.ContractError):
            core.record_semantic_review(self.run, attempt, review)

    def test_finalize_revalidates_persisted_source_map_schema_and_grounding(self) -> None:
        for mutation in ("unknown_field", "regrounded_tampered_claim"):
            with self.subTest(mutation=mutation):
                self.run = self.root / "workspace" / f"source-map-{mutation}"
                attempt, _review = self._semantic_attempt()
                source_map_path = (
                    self.run
                    / "attempts"
                    / attempt
                    / "provenance"
                    / "source-map.json"
                )
                source_map = json.loads(source_map_path.read_text(encoding="utf-8"))
                if mutation == "unknown_field":
                    source_map["unexpected"] = "accepted-before-fix"
                else:
                    source_map["claims"] = [
                        {
                            "id": "tampered",
                            "text": "The source reports 99% accuracy.",
                            "source_ids": ["ev-001"],
                        }
                    ]
                    source_map["grounding"] = {
                        "format_version": 1,
                        "valid": True,
                        "errors": [],
                    }
                core.atomic_write_json(source_map_path, source_map)

                with self.assertRaises(core.ContractError):
                    core.finalize_attempt(self.run, attempt)

    def test_source_maps_are_attempt_scoped_and_final_promotes_only_selected_attempt(self) -> None:
        self._initialize()
        source = self.root / "claims.txt"
        source.write_text("Sparse routing reaches 85% accuracy.\n", encoding="utf-8")
        core.prepare_source(self.run, source)
        core.save_plan(self.run, {"artifact_type": "poster"})
        first = core.begin_attempt(self.run)
        first_map = core.write_source_map(
            self.run,
            first,
            [{"id": "c1", "text": "Accuracy reaches 85%.", "source_ids": ["ev-001"]}],
        )
        core.mark_side_state(self.run, "failed", reason="repair requested")
        second = core.begin_attempt(self.run)
        second_map = core.write_source_map(
            self.run,
            second,
            [{"id": "c2", "text": "Sparse routing reaches 85%.", "source_ids": ["ev-001"]}],
        )
        self.assertNotEqual(first_map["claims"], second_map["claims"])
        self.assertTrue(
            (self.run / "attempts" / first / "provenance" / "source-map.json").is_file()
        )
        self.assertTrue(
            (self.run / "attempts" / second / "provenance" / "source-map.json").is_file()
        )

        attempt_root = self.run / "attempts" / second
        core.atomic_write_bytes(attempt_root / "artifact" / "poster.html", b"artifact")
        core.atomic_write_bytes(attempt_root / "qa" / "previews" / "poster.png", b"preview")
        core.record_deterministic_result(
            self.run,
            second,
            passed=True,
            checks=[],
            artifact_paths=["artifact/poster.html"],
            preview_paths={"poster": "qa/previews/poster.png"},
        )
        context = core.create_review_context(
            self.run, second, rubric={"format_version": 1, "dimensions": ["fidelity"]}
        )
        core.record_semantic_review(
            self.run,
            second,
            {
                "format_version": 1,
                "attempt_id": second,
                "review_context_sha256": context["context_sha256"],
                "artifact_hashes": context["artifact_hashes"],
                "preview_hashes": context["preview_hashes"],
                "reviewed_frame_ids": sorted(context["preview_hashes"]),
                "source_manifest_sha256": context["source_manifest_sha256"],
                "source_map_sha256": context["source_map_sha256"],
                "rubric_sha256": context["rubric_sha256"],
                "reviewer_mode": "fresh_subagent",
                "dimension_scores": {"fidelity": 4},
                "blockers": [],
                "localized_repairs": [],
                "verdict": "pass",
                "complete": True,
            },
        )
        core.finalize_attempt(self.run, second)
        promoted = json.loads(
            (self.run / "final" / "provenance" / "source-map.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(promoted["attempt_id"], second)
        self.assertEqual(promoted["claims"], second_map["claims"])

    def test_source_map_and_finalization_reverify_full_source_contract(self) -> None:
        self._initialize()
        source = self.root / "tamper.txt"
        source.write_text("Grounded accuracy is 85%.\n", encoding="utf-8")
        core.prepare_source(self.run, source)
        core.save_plan(self.run, {"artifact_type": "poster"})
        attempt = core.begin_attempt(self.run)
        evidence_path = self.run / "evidence" / "evidence.jsonl"
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["text"] = "Tampered accuracy is 99%."
        evidence["sha256"] = hashlib.sha256(evidence["text"].encode()).hexdigest()
        evidence_path.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
        with self.assertRaises(core.IntegrityError):
            core.write_source_map(
                self.run,
                attempt,
                [{"id": "c1", "text": "Accuracy is 99%.", "source_ids": ["ev-001"]}],
            )

        # Use a separate intact run to reach semantic_passed, then tamper evidence.
        self.run = self.root / "workspace" / "tamper-finalize"
        self._initialize()
        core.prepare_source(self.run, source)
        core.save_plan(self.run, {"artifact_type": "poster"})
        attempt = core.begin_attempt(self.run)
        core.write_source_map(
            self.run,
            attempt,
            [{"id": "c1", "text": "Accuracy is 85%.", "source_ids": ["ev-001"]}],
        )
        attempt_root = self.run / "attempts" / attempt
        core.atomic_write_bytes(attempt_root / "artifact" / "poster.html", b"artifact")
        core.atomic_write_bytes(attempt_root / "qa" / "previews" / "poster.png", b"preview")
        core.record_deterministic_result(
            self.run,
            attempt,
            passed=True,
            checks=[],
            artifact_paths=["artifact/poster.html"],
            preview_paths={"poster": "qa/previews/poster.png"},
        )
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
            "source_map_sha256": context["source_map_sha256"],
            "rubric_sha256": context["rubric_sha256"],
            "reviewer_mode": "fresh_subagent",
            "dimension_scores": {"fidelity": 4},
            "blockers": [],
            "localized_repairs": [],
            "verdict": "pass",
            "complete": True,
        }
        core.record_semantic_review(self.run, attempt, review)
        evidence_path = self.run / "evidence" / "evidence.jsonl"
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["text"] = "Tampered but internally hashed."
        evidence["sha256"] = hashlib.sha256(evidence["text"].encode()).hexdigest()
        evidence_path.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
        with self.assertRaises(core.IntegrityError):
            core.finalize_attempt(self.run, attempt)

    def test_state_machine_and_idempotent_resume_recover_artifact_and_qa_writes(self) -> None:
        attempt = self._begin_attempt()
        self.assertEqual(attempt, "01")
        self.assertEqual(core.begin_attempt(self.run), "01")
        status = core.resume_run(self.run, skill_root=self.skill)
        self.assertEqual(status["next_action"], "author")

        attempt_root = self.run / "attempts" / attempt
        core.atomic_write_bytes(attempt_root / "artifact" / "poster.html", b"artifact")
        self.assertEqual(
            core.resume_run(self.run, skill_root=self.skill)["next_action"], "validate"
        )

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
        recovered = core.resume_run(self.run, skill_root=self.skill)
        self.assertEqual(recovered["state"], "deterministic_passed")
        self.assertEqual(recovered["next_action"], "semantic_review")

    def test_resume_recovers_failed_deterministic_qa_write_to_repair(self) -> None:
        attempt = self._begin_attempt()
        attempt_root = self.run / "attempts" / attempt
        core.atomic_write_bytes(attempt_root / "artifact" / "poster.html", b"artifact")
        with self.assertRaises(core.SimulatedCrash):
            core.record_deterministic_result(
                self.run,
                attempt,
                passed=False,
                checks=[{"id": "geometry", "passed": False}],
                artifact_paths=["artifact/poster.html"],
                preview_paths={},
                fail_after_write=True,
            )
        status = core.resume_run(self.run, skill_root=self.skill)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["next_action"], "repair")

    def test_invalid_state_transition_is_rejected(self) -> None:
        self._initialize()
        with self.assertRaises(core.StateError):
            core.begin_attempt(self.run)
        with self.assertRaises(core.StateError):
            core.transition_state(self.run, "finalized")

    def test_planning_requires_a_ready_source(self) -> None:
        self._initialize()
        with self.assertRaises(core.StateError):
            core.save_plan(self.run, {"artifact_type": "poster"})

    def test_side_states_are_explicit_and_resume_reports_them(self) -> None:
        self._initialize()
        core.mark_side_state(self.run, "blocked", reason="missing pdftotext")
        status = core.resume_run(self.run, skill_root=self.skill)
        self.assertEqual(status["state"], "blocked")
        self.assertEqual(status["next_action"], "resolve_blocker")
        self.assertEqual(status["reason"], "missing pdftotext")

    def test_successful_source_retry_clears_blocked_state(self) -> None:
        self._initialize()
        core.mark_side_state(self.run, "blocked", reason="source tool unavailable")
        source = self.root / "paper.txt"
        source.write_text("Recovered source.\n", encoding="utf-8")
        core.prepare_source(self.run, source)
        status = core.resume_run(self.run, skill_root=self.skill)
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
            "source_map_sha256": context["source_map_sha256"],
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
        self.assertEqual(
            core.resume_run(self.run, skill_root=self.skill)["next_action"], "author"
        )

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
            "source_map_sha256": context["source_map_sha256"],
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
            "source_map_sha256": context["source_map_sha256"],
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
        status = core.resume_run(self.run, skill_root=self.skill)
        self.assertEqual(status["state"], "semantic_passed")
        self.assertEqual(status["next_action"], "finalize")

    def test_resume_and_finalize_revalidate_persisted_semantic_review_schema(self) -> None:
        mutations = (
            {"reviewed_frame_ids": []},
            {"reviewer_mode": "author"},
            {"dimension_scores": {"fidelity": 99}},
            {"unexpected": True},
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                self.run = self.root / "workspace" / f"semantic-tamper-{index}"
                attempt, _ = self._semantic_attempt()
                review_path = self.run / "attempts" / attempt / "qa" / "semantic-review.json"
                review = json.loads(review_path.read_text(encoding="utf-8"))
                review.update(mutation)
                core.atomic_write_json(review_path, review)
                with self.assertRaises(core.ContractError):
                    core.resume_run(self.run, skill_root=self.skill)
                with self.assertRaises(core.ContractError):
                    core.finalize_attempt(self.run, attempt)

    def test_finalization_is_staged_non_overwriting_and_recovers_after_rename(self) -> None:
        attempt, _ = self._semantic_attempt()
        with self.assertRaises(core.SimulatedCrash):
            core.finalize_attempt(self.run, attempt, fail_at="after_rename")
        self.assertTrue((self.run / "final" / "delivery-manifest.json").is_file())
        recovered = core.resume_run(self.run, skill_root=self.skill)
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

    def test_finalization_requires_active_attempt_source_map(self) -> None:
        attempt, _ = self._semantic_attempt()
        source_map = self.run / "attempts" / attempt / "provenance" / "source-map.json"
        source_map.unlink()
        with self.assertRaises(core.IntegrityError):
            core.finalize_attempt(self.run, attempt)

    def test_finalization_rejects_artifacts_added_after_deterministic_review(self) -> None:
        attempt, _review = self._semantic_attempt()
        core.atomic_write_bytes(
            self.run / "attempts" / attempt / "artifact" / "unreviewed.txt",
            b"late artifact",
        )
        with self.assertRaises(core.IntegrityError):
            core.finalize_attempt(self.run, attempt)

    def test_finalized_state_is_terminal_for_side_states_and_attempts(self) -> None:
        attempt, _review = self._semantic_attempt()
        core.finalize_attempt(self.run, attempt)

        with self.assertRaises(core.StateError):
            core.mark_side_state(self.run, "failed", reason="must stay finalized")
        with self.assertRaises(core.StateError):
            core.begin_attempt(self.run)

    def test_promoted_final_is_terminal_before_state_recovery(self) -> None:
        attempt, _review = self._semantic_attempt()
        with self.assertRaises(core.SimulatedCrash):
            core.finalize_attempt(self.run, attempt, fail_at="after_rename")

        with self.assertRaises(core.StateError):
            core.mark_side_state(self.run, "failed", reason="must recover final first")

    def test_finalize_retry_after_rename_revalidates_every_attempt_contract(self) -> None:
        mutations = (
            "deterministic",
            "semantic_review",
            "attempt_source_map",
            "extra_artifact",
            "final_provenance",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.run = self.root / "workspace" / f"retry-final-{mutation}"
                attempt, _review = self._semantic_attempt()
                with self.assertRaises(core.SimulatedCrash):
                    core.finalize_attempt(self.run, attempt, fail_at="after_rename")
                attempt_root = self.run / "attempts" / attempt
                if mutation == "deterministic":
                    path = attempt_root / "qa" / "deterministic.json"
                    value = json.loads(path.read_text(encoding="utf-8"))
                    value["artifact_hashes"]["artifact/poster.html"] = "0" * 64
                    core.atomic_write_json(path, value)
                elif mutation == "semantic_review":
                    path = attempt_root / "qa" / "semantic-review.json"
                    value = json.loads(path.read_text(encoding="utf-8"))
                    value["unexpected"] = True
                    core.atomic_write_json(path, value)
                elif mutation == "attempt_source_map":
                    path = attempt_root / "provenance" / "source-map.json"
                    value = json.loads(path.read_text(encoding="utf-8"))
                    value["unexpected"] = True
                    core.atomic_write_json(path, value)
                elif mutation == "extra_artifact":
                    core.atomic_write_bytes(
                        attempt_root / "artifact" / "late.txt", b"unreviewed"
                    )
                else:
                    path = self.run / "final" / "provenance" / "source-map.json"
                    value = json.loads(path.read_text(encoding="utf-8"))
                    value["unexpected"] = True
                    core.atomic_write_json(path, value)
                    delivery_path = self.run / "final" / "delivery-manifest.json"
                    delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
                    delivery["files"]["provenance/source-map.json"] = core.sha256_file(path)
                    core.atomic_write_json(delivery_path, delivery)

                with self.assertRaises(core.PortableError):
                    core.finalize_attempt(self.run, attempt)

    def test_finalized_resume_rejects_current_source_manifest_provenance_mismatch(self) -> None:
        attempt, _review = self._semantic_attempt()
        core.finalize_attempt(self.run, attempt)
        source_manifest_path = self.run / "evidence" / "source_manifest.json"
        source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
        source_manifest["post_finalize_tamper"] = True
        core.atomic_write_json(source_manifest_path, source_manifest)
        run_path = self.run / "run.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["source_manifest_sha256"] = core.sha256_file(source_manifest_path)
        core.atomic_write_json(run_path, run)

        with self.assertRaises(core.PortableError):
            core.resume_run(self.run, skill_root=self.skill)

    def test_resume_requires_installed_skill_root(self) -> None:
        self._initialize()
        with self.assertRaises(TypeError):
            core.resume_run(self.run)

    def test_unprepared_resume_requests_source_preparation(self) -> None:
        self._initialize()
        self.assertEqual(
            core.resume_run(self.run, skill_root=self.skill)["next_action"],
            "prepare_source",
        )

    def test_resume_checks_installed_skill_drift(self) -> None:
        self._initialize()
        (self.skill / "scripts" / "tool.py").write_text("VALUE = 9\n", encoding="utf-8")
        with self.assertRaises(core.IntegrityError):
            core.resume_run(self.run, skill_root=self.skill)

    def test_incomplete_final_staging_is_never_promoted(self) -> None:
        attempt, _ = self._semantic_attempt()
        with self.assertRaises(core.SimulatedCrash):
            core.finalize_attempt(self.run, attempt, fail_at="after_copy")
        self.assertFalse((self.run / "final").exists())
        status = core.resume_run(self.run, skill_root=self.skill)
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
        status = core.resume_run(self.run, skill_root=self.skill)
        self.assertEqual(status["state"], "finalized")
        self.assertEqual(status["next_action"], "complete")
        self.assertTrue((self.run / "final" / "poster.html").is_file())

    def test_delivery_verification_rejects_unlisted_files_and_directories(self) -> None:
        attempt, _ = self._semantic_attempt()
        core.finalize_attempt(self.run, attempt)
        (self.run / "final" / "unlisted.txt").write_text("extra", encoding="utf-8")
        with self.assertRaises(core.IntegrityError):
            core.resume_run(self.run, skill_root=self.skill)

        self.run = self.root / "workspace" / "stage-extra"
        attempt, _ = self._semantic_attempt()
        with self.assertRaises(core.SimulatedCrash):
            core.finalize_attempt(self.run, attempt, fail_at="after_manifest")
        (self.run / f".final.staging-{attempt}" / "empty-extra").mkdir()
        with self.assertRaises(core.IntegrityError):
            core.resume_run(self.run, skill_root=self.skill)

    def test_sync_script_keeps_all_four_vendored_copies_byte_identical(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "sync_agent_skill_core.py"),
                "--check",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        core_bytes = (REPO_ROOT / "agent_skills" / "_shared" / "portable_core.py").read_bytes()
        grounding_bytes = (REPO_ROOT / "agent_skills" / "_shared" / "source-grounding.md").read_bytes()
        png_bytes = (REPO_ROOT / "agent_skills" / "_shared" / "portable_png.py").read_bytes()
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
        self.assertEqual(
            (REPO_ROOT / "agent_skills" / "autodesign-poster" / "scripts" / "portable_png.py").read_bytes(),
            png_bytes,
        )

    def test_synced_nonposter_core_import_does_not_require_png_helper(self) -> None:
        code = """
import importlib.util
import sys
from pathlib import Path

path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("isolated_portable", path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.RELEASED_RUN_FORMAT_VERSION == 1
assert module.AGENT_FIRST_RUN_FORMAT_VERSION == 2
assert not (path.parent / "portable_png.py").exists()
assert "_autodesign_portable_png" not in sys.modules
"""
        for skill_name in ("autodesign-ppt", "autodesign-webpage", "autodesign-video"):
            with self.subTest(skill=skill_name):
                portable = (
                    REPO_ROOT / "agent_skills" / skill_name / "scripts" / "_portable.py"
                )
                completed = subprocess.run(
                    [sys.executable, "-c", code, str(portable)],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                )
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )

    def test_sync_check_reports_drift_without_mutating_target(self) -> None:
        root = self.root / "agent_skills"
        _seed_sync_fixture(root)
        target = root / "autodesign-poster" / "scripts" / "portable_png.py"
        target.write_bytes(b"drifted-png")
        before = target.read_bytes()
        completed = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "sync_agent_skill_core.py"),
                "--root",
                str(root),
                "--check",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(
            completed.stdout,
            "DRIFT: autodesign-poster/scripts/portable_png.py\n",
        )
        self.assertEqual(target.read_bytes(), before)
        for skill_name in ("autodesign-ppt", "autodesign-webpage", "autodesign-video"):
            self.assertFalse((root / skill_name / "scripts" / "portable_png.py").exists())

    def test_sync_rejects_hardlinked_canonical_sources_and_targets(self) -> None:
        for case in ("source", "target"):
            with self.subTest(case=case):
                root = self.root / f"sync-hardlink-{case}"
                _seed_sync_fixture(root)
                outside = self.root / f"outside-hardlink-{case}"
                if case == "source":
                    source = root / "_shared" / "portable_png.py"
                    outside.write_bytes(source.read_bytes())
                    source.unlink()
                    os.link(outside, source)
                else:
                    target = root / "autodesign-poster" / "scripts" / "portable_png.py"
                    outside.write_bytes(target.read_bytes())
                    target.unlink()
                    os.link(outside, target)
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(REPO_ROOT / "scripts" / "sync_agent_skill_core.py"),
                        "--root",
                        str(root),
                    ],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("hardlink", completed.stdout + completed.stderr)

    def test_sync_preflight_rejects_late_unsafe_target_without_any_mutation(self) -> None:
        root = self.root / "sync-preflight"
        _seed_sync_fixture(root)
        (root / "autodesign-poster" / "scripts" / "_portable.py").write_bytes(b"drifted")
        unsafe_target = root / "autodesign-video" / "scripts" / "_portable.py"
        outside = self.root / "outside-preflight"
        outside.write_bytes(b"outside")
        unsafe_target.unlink()
        unsafe_target.symlink_to(outside)
        before = _tree_snapshot(root)
        completed = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "sync_agent_skill_core.py"),
                "--root",
                str(root),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("symlink", completed.stdout + completed.stderr)
        self.assertEqual(_tree_snapshot(root), before)

    def test_sync_rejects_symlinked_packages_directories_and_targets(self) -> None:
        cases = ("package", "scripts", "references", "target")
        for case in cases:
            with self.subTest(case=case):
                root = self.root / f"sync-{case}"
                shared = root / "_shared"
                shared.mkdir(parents=True)
                (shared / "portable_core.py").write_bytes(b"canonical-core")
                (shared / "source-grounding.md").write_bytes(b"canonical-grounding")
                (shared / "browser_worker.py").write_bytes(b"canonical-browser-worker")
                (shared / "setup_browser.py").write_bytes(b"canonical-browser-setup")
                (shared / "requirements-browser.lock").write_bytes(b"canonical-browser-lock")
                (shared / "portable_png.py").write_bytes(b"canonical-png")
                outside = self.root / f"outside-{case}"
                outside.mkdir()
                (outside / "sentinel").write_bytes(b"unchanged")
                for skill in SKILLS:
                    package = root / skill
                    (package / "scripts").mkdir(parents=True)
                    (package / "references").mkdir()
                    (package / "scripts" / "_portable.py").write_bytes(b"canonical-core")
                    (package / "references" / "source-grounding.md").write_bytes(
                        b"canonical-grounding"
                    )
                (root / "autodesign-poster" / "scripts" / "portable_png.py").write_bytes(
                    b"canonical-png"
                )
                package = root / SKILLS[0]
                if case == "package":
                    shutil.rmtree(package)
                    package.symlink_to(outside, target_is_directory=True)
                elif case in {"scripts", "references"}:
                    shutil.rmtree(package / case)
                    (package / case).symlink_to(outside, target_is_directory=True)
                else:
                    (package / "scripts" / "portable_png.py").unlink()
                    (package / "scripts" / "portable_png.py").symlink_to(
                        outside / "sentinel"
                    )
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(REPO_ROOT / "scripts" / "sync_agent_skill_core.py"),
                        "--root",
                        str(root),
                    ],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("symlink", completed.stdout + completed.stderr)
                self.assertEqual((outside / "sentinel").read_bytes(), b"unchanged")


if __name__ == "__main__":
    unittest.main()
