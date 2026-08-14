from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]


class LocalStartupScriptsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "repo"
        scripts_dir = self.root / "scripts"
        scripts_dir.mkdir(parents=True)
        for name in ("autodesign", "start_local_web.sh"):
            target = scripts_dir / name
            shutil.copy2(ROOT / "scripts" / name, target)
            target.chmod(0o755)
        (self.root / "web" / "dist").mkdir(parents=True)
        (self.root / "web" / "dist" / "index.html").write_text(
            "<!doctype html>\n", encoding="utf-8"
        )
        (self.root / ".env").write_text("\n", encoding="utf-8")

        venv_python = self.root / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        venv_python.chmod(0o755)

        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        self.uv_log = self.root / "uv.log"
        self.health_marker = self.root / "health.marker"
        self.open_log = self.root / "open.log"
        self._write_health_server()
        self._write_fake_commands()

    def _write_health_server(self) -> None:
        server_source = self.root / "health_server.py"
        server_source.write_text(
            textwrap.dedent(
                """\
                from http.server import BaseHTTPRequestHandler, HTTPServer
                import os
                from pathlib import Path
                import sys
                import time

                marker = os.environ.get("AUTODESIGN_TEST_HEALTH_MARKER")
                delay_seconds = float(
                    os.environ.get("AUTODESIGN_TEST_HEALTH_DELAY_SECONDS", "0")
                )

                class Handler(BaseHTTPRequestHandler):
                    def do_GET(self):
                        if self.path != "/api/health":
                            self.send_error(404)
                            return
                        if marker:
                            Path(marker).write_text("healthy\\n", encoding="utf-8")
                        if delay_seconds:
                            time.sleep(delay_seconds)
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b"{}")

                    def log_message(self, _format, *_args):
                        pass

                server = HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler)
                if "--serve-forever" in sys.argv[2:]:
                    server.serve_forever()
                else:
                    server.handle_request()
                    time.sleep(0.5)
                """
            ),
            encoding="utf-8",
        )
        health_server = self.root / "health_server"
        health_server.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(server_source))} \"$@\"\n",
            encoding="utf-8",
        )
        health_server.chmod(0o755)
        self.health_server = health_server

    def _write_fake_commands(self) -> None:
        fake_uv = self.fake_bin / "uv"
        fake_uv.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$AUTODESIGN_TEST_UV_LOG\"\n"
            "if [ \"$1\" = \"run\" ] && [ \"$2\" = \"python\" ]; then\n"
            "  shift 2\n"
            "  exec \"$AUTODESIGN_TEST_PYTHON\" \"$@\"\n"
            "fi\n"
            "if [ \"$1\" = \"run\" ] && [ \"$2\" = \"uvicorn\" ]; then\n"
            "  if [ \"${AUTODESIGN_TEST_UV_MODE:-health}\" = \"fail\" ]; then\n"
            "    exit \"${AUTODESIGN_TEST_UV_EXIT_CODE:-23}\"\n"
            "  fi\n"
            "  while [ \"$#\" -gt 0 ]; do\n"
            "    if [ \"$1\" = \"--port\" ]; then\n"
            "      exec \"$AUTODESIGN_TEST_HEALTH_SERVER\" \"$2\"\n"
            "    fi\n"
            "    shift\n"
            "  done\n"
            "  exit 97\n"
            "fi\n",
            encoding="utf-8",
        )
        fake_uv.chmod(0o755)

        fake_open = self.fake_bin / "open"
        fake_open.write_text(
            "#!/bin/sh\n"
            "if [ ! -f \"$AUTODESIGN_TEST_HEALTH_MARKER\" ]; then\n"
            "  printf '%s\\n' opened-before-health > \"$AUTODESIGN_TEST_OPEN_LOG\"\n"
            "  exit 1\n"
            "fi\n"
            "printf '%s\\n' \"$1\" > \"$AUTODESIGN_TEST_OPEN_LOG\"\n",
            encoding="utf-8",
        )
        fake_open.chmod(0o755)

        fake_codex = self.fake_bin / "codex"
        fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_codex.chmod(0o755)

    def _env(self, **overrides: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.fake_bin}{os.pathsep}{env['PATH']}",
                "AUTODESIGN_NO_OPEN": "0",
                "AUTODESIGN_SKIP_SETUP": "1",
                "AUTODESIGN_STATE_DIR": str(self.root / "state"),
                "AUTODESIGN_TEST_HEALTH_MARKER": str(self.health_marker),
                "AUTODESIGN_TEST_HEALTH_SERVER": str(self.health_server),
                "AUTODESIGN_TEST_OPEN_LOG": str(self.open_log),
                "AUTODESIGN_TEST_PYTHON": sys.executable,
                "AUTODESIGN_TEST_UV_LOG": str(self.uv_log),
            }
        )
        env.update(overrides)
        return env

    def _run(self, *command: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=self.root,
            env=self._env(**overrides),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def _start_conflicting_health_listener(self, port: int) -> subprocess.Popen[str]:
        listener = subprocess.Popen(
            [str(self.health_server), str(port), "--serve-forever"],
            cwd=self.root,
            env=self._env(
                AUTODESIGN_TEST_HEALTH_DELAY_SECONDS="0.5",
                AUTODESIGN_TEST_HEALTH_MARKER="",
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.addCleanup(self._stop_process, listener)

        health_url = f"http://127.0.0.1:{port}/api/health"
        for _ in range(50):
            if listener.poll() is not None:
                self.fail("conflicting health listener exited before startup")
            try:
                with urlopen(health_url, timeout=1) as response:
                    if response.status == 200:
                        return listener
            except URLError:
                time.sleep(0.02)

        self.fail("conflicting health listener did not become ready")

    @staticmethod
    def _stop_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def test_explicit_busy_port_fails_before_starting_backend(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            completed = self._run(
                str(self.root / "scripts" / "autodesign"),
                "start",
                "--skip-setup",
                "--port",
                str(port),
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(f"Requested port {port} is already in use", completed.stderr)
        self.assertNotIn("AutoDesign is running", completed.stdout)
        self.assertFalse(self.uv_log.exists())

    def test_explicit_zero_port_fails_before_starting_backend(self) -> None:
        completed = self._run(
            str(self.root / "scripts" / "autodesign"),
            "start",
            "--skip-setup",
            "--port",
            "0",
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Requested port 0 is invalid", completed.stderr)
        self.assertNotIn("AutoDesign is running", completed.stdout)
        self.assertFalse(self.uv_log.exists())

    def test_automatic_port_uses_alternate_after_health_check(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            preferred_port = listener.getsockname()[1]
            completed = self._run(
                str(self.root / "scripts" / "autodesign"),
                "start",
                "--skip-setup",
                BACKEND_PORT=str(preferred_port),
            )

        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertIn(f"Port {preferred_port} is busy; using ", completed.stdout)
        self.assertIn("AutoDesign is running at http://127.0.0.1:", completed.stdout)
        self.assertEqual(self.health_marker.read_text(encoding="utf-8"), "healthy\n")
        opened_url = self.open_log.read_text(encoding="utf-8").strip()
        self.assertNotEqual(opened_url, "opened-before-health")
        self.assertRegex(opened_url, r"^http://127\.0\.0\.1:\d+$")

    def test_web_launcher_propagates_startup_child_failure(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]

        completed = self._run(
            str(self.root / "scripts" / "start_local_web.sh"),
            BACKEND_PORT=str(port),
            AUTODESIGN_TEST_UV_MODE="fail",
            AUTODESIGN_TEST_UV_EXIT_CODE="23",
        )

        self.assertEqual(completed.returncode, 23, completed.stderr)
        self.assertIn("Backend exited before becoming healthy", completed.stderr)
        self.assertNotIn("AutoDesign is running", completed.stdout)
        self.assertFalse(self.open_log.exists())

    def test_web_launcher_uses_configured_codex_binary_before_path(self) -> None:
        configured_codex = self.root / "configured-codex"
        configured_log = self.root / "configured-codex.log"
        configured_codex.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$AUTODESIGN_TEST_CONFIGURED_CODEX_LOG\"\n"
            "exit 0\n",
            encoding="utf-8",
        )
        configured_codex.chmod(0o755)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]

        completed = self._run(
            str(self.root / "scripts" / "start_local_web.sh"),
            BACKEND_PORT=str(port),
            AUTODESIGN_CODEX_BIN=str(configured_codex),
            AUTODESIGN_TEST_CONFIGURED_CODEX_LOG=str(configured_log),
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(configured_log.read_text(encoding="utf-8"), "login status\n")
        self.assertNotIn("Codex CLI was not found", completed.stderr)

    def test_web_launcher_rejects_conflicting_healthy_listener_after_child_failure(
        self,
    ) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        self._start_conflicting_health_listener(port)
        completed = self._run(
            str(self.root / "scripts" / "start_local_web.sh"),
            BACKEND_PORT=str(port),
            AUTODESIGN_TEST_UV_MODE="fail",
            AUTODESIGN_TEST_UV_EXIT_CODE="23",
        )

        self.assertEqual(completed.returncode, 23, completed.stderr)
        self.assertIn("Backend exited before becoming healthy", completed.stderr)
        self.assertIn("(exit 23)", completed.stderr)
        self.assertNotIn("AutoDesign is running", completed.stdout)
        self.assertFalse(self.open_log.exists())

    def test_no_env_guidance_does_not_override_designer_model(self) -> None:
        (self.root / ".env").unlink()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]

        completed = self._run(
            str(self.root / "scripts" / "start_local_web.sh"),
            BACKEND_PORT=str(port),
            AUTODESIGN_NO_OPEN="1",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("DESIGNER_MODEL=", completed.stderr)
        for role in (
            "ENHANCER",
            "CLAIM_GRAPH",
            "DECK_OUTLINE",
            "PAPER_MEMORY",
            "CRITIC",
            "COMPOSER",
            "INGEST",
        ):
            self.assertIn(f"{role}_MODEL=gpt-5.4-nano", completed.stderr)

    def test_setup_installs_locked_video_runtime_before_writing_stamp(self) -> None:
        runtime_dir = self.root / "runtime" / "video"
        runtime_dir.mkdir(parents=True)
        (runtime_dir / "package.json").write_text(
            '{"dependencies":{"hyperframes":"0.7.86"}}\n',
            encoding="utf-8",
        )
        (runtime_dir / "package-lock.json").write_text(
            '{"lockfileVersion":3,"packages":{}}\n',
            encoding="utf-8",
        )
        npm_log = self.root / "npm.log"
        setup_bin = self.root / "setup-bin"
        setup_bin.mkdir()
        _write_fake = lambda name, body: (
            (setup_bin / name).write_text(body, encoding="utf-8"),
            (setup_bin / name).chmod(0o755),
        )
        _write_fake(
            "uv",
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$AUTODESIGN_TEST_UV_LOG\"\n"
            "exit 0\n",
        )
        _write_fake(
            "node",
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = \"--version\" ]; then echo v22.14.0; fi\n"
            "exit 0\n",
        )
        _write_fake(
            "npm",
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$AUTODESIGN_TEST_NPM_LOG\"\n"
            "mkdir -p node_modules/.bin\n"
            "printf '#!/bin/sh\\nexit 0\\n' > node_modules/.bin/hyperframes\n"
            "chmod +x node_modules/.bin/hyperframes\n"
            "exit 0\n",
        )

        completed = self._run(
            str(self.root / "scripts" / "autodesign"),
            "setup",
            PATH=f"{setup_bin}{os.pathsep}{os.environ['PATH']}",
            AUTODESIGN_TEST_NPM_LOG=str(npm_log),
        )

        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertTrue(npm_log.is_file())
        self.assertEqual(
            npm_log.read_text(encoding="utf-8").splitlines(),
            ["ci --omit=dev"],
        )
        self.assertTrue((runtime_dir / "node_modules" / ".bin" / "hyperframes").is_file())
        self.assertTrue((self.root / "state" / "setup.stamp").is_file())


if __name__ == "__main__":
    unittest.main()
