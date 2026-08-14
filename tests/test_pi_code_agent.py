from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "autodesign" / "agents" / "pi_code_agent.py"


class PiCodeAgentTest(unittest.TestCase):
    def test_runs_print_mode_with_model_and_isolated_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            attempt_dir = Path(temp_dir)
            (attempt_dir / "designer_author_prompt.md").write_text(
                "Write poster.html.", encoding="utf-8",
            )
            fake_pi = attempt_dir / "fake_pi.py"
            fake_pi.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "cwd = pathlib.Path.cwd()\n"
                "(cwd / 'observed.json').write_text(json.dumps({'args': sys.argv[1:], 'pi_dir': os.getenv('PI_CODING_AGENT_DIR', '')}), encoding='utf-8')\n"
                "(cwd / 'poster.html').write_text('<!doctype html><html></html>', encoding='utf-8')\n"
                "(cwd / 'designer_author_done.json').write_text('{}', encoding='utf-8')\n",
                encoding="utf-8",
            )
            fake_pi.chmod(0o755)

            result = subprocess.run(
                [
                    sys.executable,
                    str(WRAPPER),
                    "--pi-bin",
                    str(fake_pi),
                    "--model",
                    "vendor-glm52/glm-5.2",
                    "--config-dir",
                    "/tmp/pi-config",
                    "--prompt-file",
                    "designer_author_prompt.md",
                    "--target-file",
                    "poster.html",
                    "--done-file",
                    "designer_author_done.json",
                    "--approve",
                ],
                cwd=attempt_dir,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads((attempt_dir / "observed.json").read_text(encoding="utf-8"))
            self.assertEqual(observed["pi_dir"], "/tmp/pi-config")
            for flag in ("--print", "--no-session", "--approve", "--model", "vendor-glm52/glm-5.2"):
                self.assertIn(flag, observed["args"])
            self.assertNotIn("--thinking", observed["args"])
            self.assertIn("@designer_author_prompt.md", observed["args"])
            self.assertIn("designer_author_prompt.md", observed["args"][-1])
            self.assertIn("AutoDesign-only tools", observed["args"][-1])
            self.assertIn("designer_author_done.json", observed["args"][-1])
            self.assertTrue((attempt_dir / "poster.html").exists())
            done = json.loads((attempt_dir / "designer_author_done.json").read_text(encoding="utf-8"))
            self.assertEqual(done, {})

    def test_waits_for_stable_target_before_synthesizing_done_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            attempt_dir = Path(temp_dir)
            (attempt_dir / "designer_author_prompt.md").write_text(
                "Write poster.html.", encoding="utf-8",
            )
            fake_pi = attempt_dir / "fake_pi.py"
            fake_pi.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, time\n"
                "path = pathlib.Path('poster.html')\n"
                "path.write_text('<html>draft</html>', encoding='utf-8')\n"
                "time.sleep(0.4)\n"
                "path.write_text('<html>final</html>', encoding='utf-8')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            fake_pi.chmod(0o755)

            result = subprocess.run(
                [
                    sys.executable,
                    str(WRAPPER),
                    "--pi-bin",
                    str(fake_pi),
                    "--prompt-file",
                    "designer_author_prompt.md",
                    "--target-file",
                    "poster.html",
                    "--done-file",
                    "designer_author_done.json",
                    "--output-stable-seconds",
                    "0.6",
                ],
                cwd=attempt_dir,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (attempt_dir / "poster.html").read_text(encoding="utf-8"),
                "<html>final</html>",
            )
            done = json.loads(
                (attempt_dir / "designer_author_done.json").read_text(encoding="utf-8")
            )
            self.assertIn("Pi adapter observed", done["summary"])

    @unittest.skipUnless(os.name == "posix", "process-group contract is POSIX-specific")
    def test_outer_process_group_termination_also_stops_pi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            attempt_dir = Path(temp_dir)
            (attempt_dir / "designer_author_prompt.md").write_text(
                "Wait without writing output.", encoding="utf-8",
            )
            fake_pi = attempt_dir / "fake_pi.py"
            fake_pi.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, time\n"
                "pathlib.Path('pi.pid').write_text(str(os.getpid()), encoding='utf-8')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            fake_pi.chmod(0o755)
            wrapper = subprocess.Popen(
                [
                    sys.executable,
                    str(WRAPPER),
                    "--pi-bin",
                    str(fake_pi),
                    "--prompt-file",
                    "designer_author_prompt.md",
                ],
                cwd=attempt_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            child_pid: int | None = None
            try:
                pid_path = attempt_dir / "pi.pid"
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not pid_path.exists():
                    time.sleep(0.05)
                self.assertTrue(pid_path.exists(), "fake Pi did not start")
                child_pid = int(pid_path.read_text(encoding="utf-8"))

                os.killpg(wrapper.pid, signal.SIGTERM)
                wrapper.wait(timeout=5)

                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and self._pid_exists(child_pid):
                    time.sleep(0.05)
                self.assertFalse(
                    self._pid_exists(child_pid),
                    "Pi survived termination of the wrapper process group",
                )
            finally:
                if wrapper.poll() is None:
                    os.killpg(wrapper.pid, signal.SIGKILL)
                    wrapper.wait(timeout=5)
                if child_pid is not None and self._pid_exists(child_pid):
                    os.kill(child_pid, signal.SIGKILL)

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True


if __name__ == "__main__":
    unittest.main()
