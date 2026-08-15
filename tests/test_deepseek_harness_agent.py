from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "autodesign" / "agents" / "deepseek_harness_agent.py"


class DeepSeekHarnessAgentTests(unittest.TestCase):
    def _write_fake_dsh(
        self,
        root: Path,
        *,
        compatible: bool = True,
        run_returncode: int = 0,
    ) -> Path:
        fake_dsh = root / "fake_dsh.py"
        fake_dsh.write_text(
            "\n".join(
                [
                    f"#!{sys.executable}",
                    "import json, os, pathlib, sys",
                    "args = sys.argv[1:]",
                    "if args == ['--version']:",
                    f"    print({'0.1.0-rc.6' if compatible else '0.0.1'!r})",
                    "    raise SystemExit(0)",
                    "if args == ['--profile', 'headless', '--help']:",
                    (
                        "    print('Usage: dsh --profile headless [options] [task...]\\n"
                        "Answer one task, print the final assistant message, and exit.')"
                        if compatible
                        else "    print('Usage: dsh [options] [command]')"
                    ),
                    "    raise SystemExit(0)",
                    "patch_text = ''",
                    "if '--patch' in args:",
                    "    patch_path = pathlib.Path(args[args.index('--patch') + 1])",
                    "    patch_text = patch_path.read_text(encoding='utf-8')",
                    "observed = {",
                    "    'args': args,",
                    "    'cwd': os.getcwd(),",
                    "    'patch_text': patch_text,",
                    "}",
                    "pathlib.Path('observed.json').write_text(json.dumps(observed), encoding='utf-8')",
                    f"raise SystemExit({run_returncode})",
                ]
            ),
            encoding="utf-8",
        )
        fake_dsh.chmod(0o755)
        return fake_dsh

    def test_invokes_released_headless_profile_with_staged_prompt_and_model_patch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            prompt = root / "designer_author_prompt.md"
            prompt.write_text("SECRET SOURCE PROMPT: create poster.html", encoding="utf-8")
            fake_dsh = self._write_fake_dsh(root)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(WRAPPER),
                    "--dsh-bin",
                    str(fake_dsh),
                    "--model",
                    "deepseek-v4-pro",
                    "--prompt-file",
                    prompt.name,
                    "--target-file",
                    "poster.html",
                    "--done-file",
                    "designer_author_done.json",
                    "--task",
                    "AutoDesign poster authoring",
                ],
                cwd=root,
                env={**os.environ, "DEEPSEEK_API_KEY": "not-a-real-test-key"},
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            observed = json.loads((root / "observed.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(observed["cwd"]).resolve(), root.resolve())
            self.assertEqual(observed["args"][:2], ["--profile", "headless"])
            self.assertIn("--patch", observed["args"])
            task = observed["args"][-1]
            self.assertIn("designer_author_prompt.md", task)
            self.assertIn("poster.html", task)
            self.assertIn("designer_author_done.json", task)
            self.assertNotIn("SECRET SOURCE PROMPT", task)
            self.assertNotIn("not-a-real-test-key", json.dumps(observed))
            patch = json.loads(observed["patch_text"])
            self.assertEqual(patch, [{
                "id": "agent-default-model",
                "config": {
                    "provider": "deepseek-official",
                    "model": "deepseek-v4-pro",
                },
            }])

    def test_blank_model_does_not_create_or_pass_a_patch(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            (root / "prompt.md").write_text("Create output.", encoding="utf-8")
            fake_dsh = self._write_fake_dsh(root)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(WRAPPER),
                    "--dsh-bin",
                    str(fake_dsh),
                    "--prompt-file",
                    "prompt.md",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            observed = json.loads((root / "observed.json").read_text(encoding="utf-8"))
            self.assertNotIn("--patch", observed["args"])
            self.assertEqual(observed["patch_text"], "")
            self.assertFalse((root / ".autodesign-dsh-model.patch.yml").exists())

    def test_rejects_old_preview_cli_with_actionable_upgrade_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            (root / "prompt.md").write_text("Create output.", encoding="utf-8")
            fake_dsh = self._write_fake_dsh(root, compatible=False)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(WRAPPER),
                    "--dsh-bin",
                    str(fake_dsh),
                    "--prompt-file",
                    "prompt.md",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("incompatible", completed.stderr.lower())
            self.assertIn("npm install -g @deepseek-ai/dsh@latest", completed.stderr)
            self.assertFalse((root / "observed.json").exists())

    def test_propagates_headless_process_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            (root / "prompt.md").write_text("Create output.", encoding="utf-8")
            fake_dsh = self._write_fake_dsh(root, run_returncode=23)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(WRAPPER),
                    "--dsh-bin",
                    str(fake_dsh),
                    "--prompt-file",
                    "prompt.md",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 23)


if __name__ == "__main__":
    unittest.main()
