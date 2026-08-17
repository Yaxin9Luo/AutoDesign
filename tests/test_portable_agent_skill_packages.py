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
ROOT_README = REPO_ROOT / "README.md"
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


class PortableAgentSkillPackageTests(unittest.TestCase):
    def test_news_links_directly_to_agent_skills_readme(self) -> None:
        root_documentation = ROOT_README.read_text(encoding="utf-8")
        launch_rows = [
            line
            for line in root_documentation.splitlines()
            if "**2026-08-17**" in line
        ]

        self.assertEqual(len(launch_rows), 1)
        self.assertIn("./agent_skills/README.md", launch_rows[0])
        self.assertNotIn("/pull/", launch_rows[0])
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

        for name in APPROVED_SKILLS:
            with self.subTest(skill=name):
                self.assertIn(name, skills_documentation)
                self.assertIn(
                    f"agent_skills/{name} --agent codex --scope user",
                    skills_documentation,
                )
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
            "gh skill install",
            "--agent claude-code --scope user",
            '--dir "$HOME/.dsh/skills"',
            "package_agent_skills.py install",
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
