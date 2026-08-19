from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "agent_skills"
SKILLS_README = SKILLS_ROOT / "README.md"
SKILLS_README_ZH = SKILLS_ROOT / "README.zh-CN.md"
ROOT_README = REPO_ROOT / "README.md"
ROOT_README_ZH = REPO_ROOT / "README.zh-CN.md"
ROOT_README_KO = REPO_ROOT / "README.ko.md"
POSTER_ROOT = SKILLS_ROOT / "autodesign-poster"
APPROVED_SKILLS = (
    "autodesign-poster",
    "autodesign-ppt",
    "autodesign-webpage",
    "autodesign-video",
)


def _frontmatter(skill_file: Path) -> dict[str, str]:
    text = skill_file.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(?P<body>.*?)\n---\n", text, flags=re.DOTALL)
    if match is None:
        return {}
    result: dict[str, str] = {}
    for line in match.group("body").splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        result[key.strip()] = value.strip().strip("\"'")
    return result


def _transitive_local_markdown_documents(entrypoint: Path) -> dict[Path, str]:
    pending = [entrypoint.resolve()]
    documents: dict[Path, str] = {}
    while pending:
        path = pending.pop()
        if path in documents:
            continue
        text = path.read_text(encoding="utf-8")
        documents[path] = text
        for target in re.findall(r"\[[^\]]+\]\(([^)]+\.md(?:#[^)]*)?)\)", text):
            relative = target.split("#", 1)[0]
            linked = (path.parent / relative).resolve()
            linked.relative_to(POSTER_ROOT.resolve())
            pending.append(linked)
    return documents


class PortableAgentSkillPackageTests(unittest.TestCase):
    def test_poster_skill_teaches_the_agent_first_workflow_without_legacy_shortcuts(self) -> None:
        skill = (POSTER_ROOT / "SKILL.md").read_text(encoding="utf-8")
        lower = skill.lower()
        normalized = " ".join(lower.split())
        self.assertIn("## workflow", lower)
        workflow = lower.split("## workflow", 1)[1]
        ordered_commands = (
            "doctor",
            "init",
            "inspect-source",
            "crop-source",
            "list-source-assets",
            "source-review-context",
            "record-source-review",
            "plan",
            "begin-attempt",
            "dom-audit",
            "validate",
            "review-context",
            "record-review",
            "reopen-curation",
            "finalize",
        )
        positions = []
        for command in ordered_commands:
            match = re.search(rf"`{re.escape(command)}(?:`|\s)", workflow)
            self.assertIsNotNone(match, command)
            assert match is not None
            positions.append(match.start())
        self.assertEqual(positions, sorted(positions))

        for marker in (
            "pdf and complete page renders are the primary semantic surface",
            "pdfimages is discovery-only and never authorizes evidence",
            "no mandatory image-count quota",
            "fresh source review must pass before `plan`",
            "scripts never edit the poster",
            "may escalate a repair route but never downgrade it",
            "`diagnose-v1` is read-only",
            "does not replace the full autodesign harness",
        ):
            self.assertIn(marker, normalized)
        self.assertRegex(lower, r"fresh (?:vision-capable )?(?:agent|subagent)")
        self.assertIn("python3", skill)
        self.assertIn("python", skill)
        self.assertIn("py -3", skill)
        for reference in (
            "references/agent-first-source.md",
            "references/output-contract.md",
            "references/review-rubric.md",
        ):
            self.assertIn(reference, skill)
        self.assertNotIn("```json", lower)

        documents = _transitive_local_markdown_documents(POSTER_ROOT / "SKILL.md")
        self.assertNotIn(
            (POSTER_ROOT / "references" / "source-grounding.md").resolve(),
            documents,
        )
        for required in (
            POSTER_ROOT / "references" / "agent-first-source.md",
            POSTER_ROOT / "references" / "output-contract.md",
            POSTER_ROOT / "references" / "review-rubric.md",
        ):
            self.assertIn(required.resolve(), documents)
        for document in documents.values():
            self.assertNotIn("--asset", document)
            self.assertNotIn("bind-visuals", document)
            self.assertNotIn("Explicit attached images begin eligible", document)
            self.assertNotIn("reviewer sidecar binds the visual", document)
            self.assertNotRegex(document, r"(?i)attempt\s*0?1\b")

    def test_news_links_directly_to_agent_skills_readme(self) -> None:
        root_documentation = ROOT_README.read_text(encoding="utf-8")
        root_documentation_zh = ROOT_README_ZH.read_text(encoding="utf-8")
        root_documentation_ko = ROOT_README_KO.read_text(encoding="utf-8")
        launch_rows = [
            line
            for line in root_documentation.splitlines()
            if "**2026-08-17**" in line
        ]

        self.assertEqual(len(launch_rows), 1)
        self.assertIn("./agent_skills/README.md", launch_rows[0])
        self.assertNotIn("/pull/", launch_rows[0])
        for documentation, target in (
            (root_documentation, "./agent_skills/README.md#agent-skills-v0-2-0"),
            (root_documentation_zh, "./agent_skills/README.zh-CN.md#agent-skills-v0-2-0"),
            (root_documentation_ko, "./agent_skills/README.md#agent-skills-v0-2-0"),
        ):
            release_rows = [
                line for line in documentation.splitlines() if "**2026-08-19**" in line
            ]
            self.assertEqual(len(release_rows), 1)
            self.assertIn(target, release_rows[0])
            self.assertNotIn("/pull/", release_rows[0])
        self.assertIn(
            "| **2026-08-15** | [Added official DeepSeek Harness support for "
            "coding agents](https://github.com/Yaxin9Luo/AutoDesign/pull/2) |",
            root_documentation,
        )
        self.assertIn(
            "| **2026-08-14** | [Initial public release]"
            "(https://github.com/Yaxin9Luo/AutoDesign/commit/"
            "55586f66fa4a126997f0d252e070701c4ae68920) |",
            root_documentation,
        )

    def test_launch_guide_is_installable_and_honest(self) -> None:
        skills_documentation = SKILLS_README.read_text(encoding="utf-8")
        skills_documentation_zh = SKILLS_README_ZH.read_text(encoding="utf-8")

        for name in APPROVED_SKILLS:
            with self.subTest(skill=name):
                self.assertIn(name, skills_documentation)
        for heading in (
            "## Quick install",
            "## Install by Coding Agent",
            "## First run",
            "## Requirements",
            "## Skills vs. the full AutoDesign Harness",
            "## Roadmap and contributing",
            "## Maintainer verification",
        ):
            self.assertIn(heading, skills_documentation)
        for marker in (
            "## Agent Skills v0.2.0 · 2026-08-19",
            "gh release download agent-skills-v0.2.0",
            'DESTINATION="${DESTINATION:-$HOME/.agents/skills}"',
            'DESTINATION="$HOME/.claude/skills"',
            'DESTINATION="$HOME/.dsh/skills"',
            '--archive "./${skill}-0.2.0.zip"',
            "Set-Location autodesign-skills-v0.2.0",
            "package_agent_skills.py install",
            "white primary canvas",
            "70–80%",
            "future target",
            "does not replace",
            "Contributing",
            "https://developers.openai.com/codex/skills",
            "https://code.claude.com/docs/en/skills",
            "https://github.com/deepseek-ai/deepseek-harness/blob/master/"
            "docs/subsystems/skills.md",
        ):
            self.assertIn(marker, skills_documentation)
        self.assertNotIn(
            "gh skill install Yaxin9Luo/AutoDesign agent_skills/",
            skills_documentation,
        )
        for marker in (
            "## Agent Skills v0.2.0 · 2026-08-19",
            "gh release download agent-skills-v0.2.0",
            '--archive "./${skill}-0.2.0.zip"',
            "Set-Location autodesign-skills-v0.2.0",
            "白色主画布",
        ):
            self.assertIn(marker, skills_documentation_zh)

    def test_public_docs_cover_agent_discovery_interpreters_and_prerequisites(self) -> None:
        skills_documentation = SKILLS_README.read_text(encoding="utf-8")
        root_documentation = ROOT_README.read_text(encoding="utf-8")

        self.assertIn("Agent Skills", root_documentation)
        self.assertIn("./agent_skills/README.md", root_documentation)
        for destination in (
            "~/.agents/skills",
            "~/.codex/skills",
            "~/.dsh/skills",
            "~/.claude/skills",
        ):
            self.assertIn(destination, skills_documentation)
        for interpreter in ("python3", "python", "py -3"):
            self.assertIn(interpreter, skills_documentation)
        for prerequisite in (
            "Poppler",
            "LibreOffice",
            "Node.js 22+",
            "ffmpeg",
            "ffprobe",
            "Python 3.10–3.12",
        ):
            self.assertIn(prerequisite, skills_documentation)

    def test_exact_approved_skill_packages_exist(self) -> None:
        package_names = sorted(
            path.name
            for path in SKILLS_ROOT.iterdir()
            if path.is_dir() and not path.name.startswith("_")
        )
        self.assertEqual(package_names, sorted(APPROVED_SKILLS))

    def test_each_package_has_portable_skill_metadata_and_license(self) -> None:
        for name in APPROVED_SKILLS:
            with self.subTest(skill=name):
                root = SKILLS_ROOT / name
                skill_file = root / "SKILL.md"
                license_file = root / "LICENSE"
                metadata_file = root / "agents" / "openai.yaml"

                self.assertTrue(skill_file.is_file())
                self.assertTrue(license_file.is_file())
                self.assertTrue(metadata_file.is_file())
                self.assertEqual(
                    _frontmatter(skill_file),
                    {
                        "name": name,
                        "description": _frontmatter(skill_file).get("description", ""),
                    },
                )
                self.assertTrue(_frontmatter(skill_file)["description"])

                metadata = metadata_file.read_text(encoding="utf-8")
                self.assertRegex(metadata, r'(?m)^  display_name: "[^"\n]+"$')
                short = re.search(
                    r'(?m)^  short_description: "(?P<value>[^"\n]+)"$', metadata
                )
                self.assertIsNotNone(short)
                assert short is not None
                self.assertGreaterEqual(len(short.group("value")), 25)
                self.assertLessEqual(len(short.group("value")), 64)
                self.assertRegex(
                    metadata,
                    rf'(?m)^  default_prompt: "[^"\n]*\${re.escape(name)}[^"\n]*"$',
                )

    def test_packages_are_independent_and_write_state_outside_install_root(self) -> None:
        forbidden = (
            "import autodesign",
            "from autodesign",
            "import design_anything",
            "from design_anything",
            "scripts.web_server",
            "skills/runtime",
            "localhost:8000",
            "127.0.0.1:8000",
        )
        for name in APPROVED_SKILLS:
            root = SKILLS_ROOT / name
            with self.subTest(skill=name):
                self.assertFalse(any(root.rglob("skill.json")))
                skill_text = (root / "SKILL.md").read_text(encoding="utf-8")
                self.assertIn("user-selected output directory", skill_text)
                self.assertIn("Never write", skill_text)
                for path in root.rglob("*"):
                    if not path.is_file():
                        continue
                    text = path.read_text(encoding="utf-8", errors="ignore").lower()
                    for marker in forbidden:
                        self.assertNotIn(marker, text, f"{path}: {marker}")

    def test_portable_png_is_vendored_only_with_the_poster_package(self) -> None:
        self.assertTrue((SKILLS_ROOT / "autodesign-poster" / "scripts" / "portable_png.py").is_file())
        for name in ("autodesign-ppt", "autodesign-webpage", "autodesign-video"):
            self.assertFalse((SKILLS_ROOT / name / "scripts" / "portable_png.py").exists())

    def test_validator_accepts_repository_packages(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "validate_agent_skills.py"),
                "--root",
                str(SKILLS_ROOT),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        for name in APPROVED_SKILLS:
            self.assertIn(name, completed.stdout)

    def test_validator_rejects_forbidden_reference_and_unsafe_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "agent_skills"
            shutil.copytree(SKILLS_ROOT, root)
            target = root / APPROVED_SKILLS[0]
            (target / "unsafe.py").write_text("import autodesign\n", encoding="utf-8")
            (target / ".env").write_text("TOKEN=not-a-real-secret\n", encoding="utf-8")
            (target / "__pycache__").mkdir()

            completed = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "validate_agent_skills.py"),
                    "--root",
                    str(root),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            output = completed.stdout + completed.stderr
            self.assertIn("forbidden product dependency", output)
            self.assertIn("secret-like path", output)
            self.assertIn("cache or generated output", output)

    def test_validator_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "agent_skills"
            shutil.copytree(SKILLS_ROOT, root)
            target = root / APPROVED_SKILLS[0] / "linked-license"
            target.symlink_to(root / APPROVED_SKILLS[0] / "LICENSE")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "validate_agent_skills.py"),
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

    def test_validator_scans_secret_markers_in_binary_named_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "agent_skills"
            shutil.copytree(SKILLS_ROOT, root)
            target = root / APPROVED_SKILLS[0] / "deploy.key"
            target.write_bytes(
                b"opaque-prefix\x00-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key\n"
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "validate_agent_skills.py"),
                    "--root",
                    str(root),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("secret-like content", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
