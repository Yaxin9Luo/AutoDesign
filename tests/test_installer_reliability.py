from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _make_bundle(root: Path, *, setup_exit: int = 0) -> Path:
    bundle_root = root / "AutoDesign"
    (bundle_root / "web" / "dist").mkdir(parents=True)
    (bundle_root / "web" / "dist" / "index.html").write_text(
        "<!doctype html>\n",
        encoding="utf-8",
    )
    for name in ("pyproject.toml", "uv.lock"):
        (bundle_root / name).write_text(f"{name}\n", encoding="utf-8")
    shutil.copy2(ROOT / "install.sh", bundle_root / "install.sh")
    _write_executable(
        bundle_root / "scripts" / "autodesign",
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = setup ]; then\n"
        f"  exit {setup_exit}\n"
        "fi\n"
        "exit 0\n",
    )
    _write_executable(
        bundle_root / "scripts" / "designanything",
        "#!/bin/sh\nexit 0\n",
    )
    _write_executable(
        bundle_root / "scripts" / "start_local_web.sh",
        "#!/bin/sh\nexit 0\n",
    )

    archive = root / "designanything-local.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(bundle_root, arcname="AutoDesign")
    return archive


class _BundleHandler(BaseHTTPRequestHandler):
    archive: bytes = b""
    requests = 0

    def do_GET(self) -> None:
        type(self).requests += 1
        if self.path != "/designanything-local.tar.gz":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(type(self).archive)))
        self.end_headers()
        if type(self).requests == 1:
            partial = type(self).archive[: max(1, len(type(self).archive) // 3)]
            self.wfile.write(partial)
            self.wfile.flush()
            self.connection.shutdown(1)
            return
        self.wfile.write(type(self).archive)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


class InstallerReliabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.install_dir = self.root / "installed"
        self.bin_dir = self.root / "bin"
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        _write_executable(self.fake_bin / "uv", "#!/bin/sh\nexit 0\n")
        self.installer = self.root / "runner" / "install.sh"
        self.installer.parent.mkdir()
        shutil.copy2(ROOT / "install.sh", self.installer)
        self.installer.chmod(0o755)

    def _env(self, archive_url: str, sha256: str, **overrides: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fake_bin}{os.pathsep}{env['PATH']}",
                "AUTODESIGN_BUNDLE_URL": archive_url,
                "AUTODESIGN_BUNDLE_SHA256": sha256,
                "AUTODESIGN_INSTALL_DIR": str(self.install_dir),
                "AUTODESIGN_BIN_DIR": str(self.bin_dir),
                "AUTODESIGN_STATE_DIR": str(self.root / "state"),
                "AUTODESIGN_SKIP_SETUP": "1",
                "AUTODESIGN_INSTALL_UV": "0",
                "AUTODESIGN_DOWNLOAD_RETRY_DELAY": "0",
            }
        )
        env.update(overrides)
        return env

    def _run(self, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.installer)],
            cwd=self.root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_partial_transfer_is_retried_before_installing_verified_bundle(self) -> None:
        archive = _make_bundle(self.root / "bundle")
        payload = archive.read_bytes()
        expected_sha256 = hashlib.sha256(payload).hexdigest()
        handler = type("PartialThenCompleteHandler", (_BundleHandler,), {})
        handler.archive = payload
        handler.requests = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        completed = self._run(
            self._env(
                f"http://127.0.0.1:{server.server_port}/designanything-local.tar.gz",
                expected_sha256,
            )
        )

        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertGreaterEqual(handler.requests, 2)
        self.assertTrue((self.install_dir / "web" / "dist" / "index.html").is_file())
        self.assertTrue((self.bin_dir / "autodesign").is_symlink())

    def test_checksum_mismatch_preserves_existing_install_and_launcher(self) -> None:
        archive = _make_bundle(self.root / "bundle")
        self.install_dir.mkdir()
        (self.install_dir / "existing.txt").write_text("keep\n", encoding="utf-8")
        self.bin_dir.mkdir()
        old_launcher = self.root / "old-autodesign"
        _write_executable(old_launcher, "#!/bin/sh\nexit 0\n")
        (self.bin_dir / "autodesign").symlink_to(old_launcher)

        completed = self._run(
            self._env(
                archive.resolve().as_uri(),
                "0" * 64,
            )
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("checksum", completed.stderr.lower())
        self.assertTrue((self.install_dir / "existing.txt").is_file())
        self.assertEqual(
            (self.install_dir / "existing.txt").read_text(encoding="utf-8"),
            "keep\n",
        )
        self.assertEqual(
            (self.bin_dir / "autodesign").resolve(),
            old_launcher.resolve(),
        )

    def test_read_only_previous_install_does_not_block_activation(self) -> None:
        archive = _make_bundle(self.root / "bundle")
        previous_dir = self.install_dir.with_name(f"{self.install_dir.name}.previous")
        locked_dir = previous_dir / "out" / "runs" / "attempt" / "runtime_skills"
        locked_dir.mkdir(parents=True)
        (locked_dir / "SKILL.md").write_text("read only\n", encoding="utf-8")
        for directory in (locked_dir, *locked_dir.parents):
            if directory == previous_dir.parent:
                break
            directory.chmod(0o555)
        self.addCleanup(self._restore_user_write_access, previous_dir)

        completed = self._run(
            self._env(
                archive.resolve().as_uri(),
                hashlib.sha256(archive.read_bytes()).hexdigest(),
            )
        )

        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertTrue((self.install_dir / "web" / "dist" / "index.html").is_file())
        self.assertFalse(previous_dir.exists())

    @staticmethod
    def _restore_user_write_access(path: Path) -> None:
        if not path.exists():
            return
        for directory, _children, _files in os.walk(path, topdown=False):
            Path(directory).chmod(0o755)

    def test_setup_failure_preserves_existing_install_and_launcher(self) -> None:
        bundle_root = self.root / "local-bundle"
        _make_bundle(bundle_root, setup_exit=42)
        source = bundle_root / "AutoDesign"
        self.install_dir.mkdir()
        (self.install_dir / "existing.txt").write_text("keep\n", encoding="utf-8")
        self.bin_dir.mkdir()
        old_launcher = self.root / "old-autodesign"
        _write_executable(old_launcher, "#!/bin/sh\nexit 0\n")
        (self.bin_dir / "autodesign").symlink_to(old_launcher)

        env = self._env(
            "https://unused.invalid/bundle.tar.gz",
            "0" * 64,
            AUTODESIGN_SKIP_SETUP="0",
        )
        completed = subprocess.run(
            [str(source / "install.sh")],
            cwd=self.root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertEqual(completed.returncode, 42)
        self.assertTrue((self.install_dir / "existing.txt").is_file())
        self.assertEqual(
            (self.install_dir / "existing.txt").read_text(encoding="utf-8"),
            "keep\n",
        )
        self.assertEqual(
            (self.bin_dir / "autodesign").resolve(),
            old_launcher.resolve(),
        )

    def test_setup_console_scripts_remain_executable_after_activation(self) -> None:
        bundle_root = self.root / "local-bundle"
        _make_bundle(bundle_root)
        source = bundle_root / "AutoDesign"
        _write_executable(
            source / "scripts" / "autodesign",
            "#!/bin/sh\n"
            "set -eu\n"
            "if [ \"${1:-}\" = setup ]; then\n"
            "  script_dir=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
            "  cd \"$script_dir/..\"\n"
            "  mkdir -p .venv/bin \"$AUTODESIGN_STATE_DIR\"\n"
            "  ln -s /bin/sh .venv/bin/python\n"
            "  printf '#!%s/.venv/bin/python\\nprintf \"runtime-ok\\\\n\"\\n' "
            "\"$PWD\" > .venv/bin/uvicorn\n"
            "  chmod +x .venv/bin/uvicorn\n"
            "  printf 'ready\\n' > \"$AUTODESIGN_STATE_DIR/setup.stamp\"\n"
            "fi\n",
        )

        env = self._env(
            "https://unused.invalid/bundle.tar.gz",
            "0" * 64,
            AUTODESIGN_SKIP_SETUP="0",
        )
        installed = subprocess.run(
            [str(source / "install.sh")],
            cwd=self.root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)

        launched = subprocess.run(
            [
                "/bin/sh",
                "-c",
                'exec "$1"',
                "sh",
                str(self.install_dir / ".venv" / "bin" / "uvicorn"),
            ],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(launched.returncode, 0, launched.stderr)
        self.assertEqual(launched.stdout, "runtime-ok\n")

    def test_local_bundle_excludes_private_env_files(self) -> None:
        bundle_root = self.root / "local-bundle"
        _make_bundle(bundle_root)
        source = bundle_root / "AutoDesign"
        for name in (".env", ".env.local", ".env.development.local"):
            (source / name).write_text("PRIVATE_KEY=do-not-copy\n", encoding="utf-8")
        (source / ".env.example").write_text("PUBLIC_EXAMPLE=\n", encoding="utf-8")

        completed = subprocess.run(
            [str(source / "install.sh")],
            cwd=self.root,
            env=self._env(
                "https://unused.invalid/bundle.tar.gz",
                "0" * 64,
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        for name in (".env", ".env.local", ".env.development.local"):
            self.assertFalse((self.install_dir / name).exists())
        self.assertTrue((self.install_dir / ".env.example").is_file())

    def test_launcher_failure_rolls_back_the_activated_install(self) -> None:
        bundle_root = self.root / "local-bundle"
        _make_bundle(bundle_root)
        source = bundle_root / "AutoDesign"
        self.install_dir.mkdir()
        (self.install_dir / "existing.txt").write_text("keep\n", encoding="utf-8")
        self.bin_dir.mkdir()
        old_launcher = self.root / "old-autodesign"
        _write_executable(old_launcher, "#!/bin/sh\nexit 0\n")
        (self.bin_dir / "autodesign").symlink_to(old_launcher)
        _write_executable(self.fake_bin / "ln", "#!/bin/sh\nexit 73\n")

        env = self._env(
            "https://unused.invalid/bundle.tar.gz",
            "0" * 64,
        )
        completed = subprocess.run(
            [str(source / "install.sh")],
            cwd=self.root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue((self.install_dir / "existing.txt").is_file())
        self.assertEqual(
            (self.bin_dir / "autodesign").resolve(),
            old_launcher.resolve(),
        )


if __name__ == "__main__":
    unittest.main()
