from __future__ import annotations

import os
from pathlib import Path
import shutil
import shlex
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LocalReleasePackagingTest(unittest.TestCase):
    def test_release_bundle_rejects_web_dist_root_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture_root = Path(tmp) / "repo"
            script = fixture_root / "scripts" / "package_local_release.sh"
            script.parent.mkdir(parents=True)
            script.write_bytes(
                (ROOT / "scripts" / "package_local_release.sh").read_bytes()
            )
            script.chmod(0o755)
            for required in (
                "install.sh",
                "scripts/autodesign",
                "scripts/designanything",
                "scripts/start_local_web.sh",
                "pyproject.toml",
                "uv.lock",
                "runtime/video/package.json",
                "runtime/video/package-lock.json",
            ):
                required_path = fixture_root / required
                required_path.parent.mkdir(parents=True, exist_ok=True)
                required_path.touch()
            dist_target = Path(tmp) / "dist-target"
            dist_target.mkdir()
            (dist_target / "index.html").write_text("built\n", encoding="utf-8")
            (fixture_root / "web").mkdir()
            (fixture_root / "web" / "dist").symlink_to(
                dist_target,
                target_is_directory=True,
            )
            env = os.environ.copy()
            env["AUTODESIGN_RELEASE_DIR"] = str(Path(tmp) / "release")
            completed = subprocess.run(
                [str(script)],
                cwd=fixture_root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("web/dist must not contain symlinks", completed.stderr)

    def test_release_bundle_rejects_web_dist_symlinks(self) -> None:
        dist_dir = ROOT / "web" / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "outside.js"
            target.write_text("outside\n", encoding="utf-8")
            symlink = dist_dir / "release-test-symlink.js"
            try:
                symlink.symlink_to(target)
                env = os.environ.copy()
                env["AUTODESIGN_RELEASE_DIR"] = str(Path(tmp) / "release")
                completed = subprocess.run(
                    [str(ROOT / "scripts" / "package_local_release.sh")],
                    cwd=ROOT,
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                )
            finally:
                symlink.unlink(missing_ok=True)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("web/dist must not contain symlinks", completed.stderr)

    def test_release_bundle_normalizes_ownership_with_gnu_tar_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release_dir = Path(tmp) / "release"
            fake_bin = Path(tmp) / "bin"
            fake_bin.mkdir()
            tar_log = Path(tmp) / "tar.log"
            real_tar = shutil.which("tar")
            self.assertIsNotNone(real_tar)
            fake_tar = fake_bin / "tar"
            fake_tar.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "if [ \"${1:-}\" = \"--version\" ]; then\n"
                "  echo 'tar (GNU tar) 1.35'\n"
                "  exit 0\n"
                "fi\n"
                "printf '%s\\n' \"$*\" >> \"$AUTODESIGN_TEST_TAR_LOG\"\n"
                "translated=()\n"
                "for arg in \"$@\"; do\n"
                "  case \"$arg\" in\n"
                "    --owner=0) translated+=(--uid 0 --uname '') ;;\n"
                "    --group=0) translated+=(--gid 0 --gname '') ;;\n"
                "    --numeric-owner) ;;\n"
                "    --uid|--gid|--uname|--gname)\n"
                "      echo 'BSD ownership flag passed to GNU tar' >&2\n"
                "      exit 64\n"
                "      ;;\n"
                "    *) translated+=(\"$arg\") ;;\n"
                "  esac\n"
                "done\n"
                f"exec {shlex.quote(real_tar or 'tar')} \"${{translated[@]}}\"\n",
                encoding="utf-8",
            )
            fake_tar.chmod(0o755)
            env = os.environ.copy()
            env["AUTODESIGN_RELEASE_DIR"] = str(release_dir)
            env["AUTODESIGN_TEST_TAR_LOG"] = str(tar_log)
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            completed = subprocess.run(
                [str(ROOT / "scripts" / "package_local_release.sh")],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
            )
            tar_calls = tar_log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any("--owner=0" in call for call in tar_calls))
            self.assertTrue(any("--group=0" in call for call in tar_calls))
            self.assertTrue(any("--numeric-owner" in call for call in tar_calls))
            with tarfile.open(
                release_dir / "designanything-local.tar.gz",
                "r:gz",
            ) as bundle:
                self.assertTrue(
                    all(
                        (member.uid, member.gid, member.uname, member.gname)
                        == (0, 0, "", "")
                        for member in bundle.getmembers()
                    )
                )

    def test_release_bundle_uses_canonical_root_and_launchers(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory(
                prefix=".autodesign-release-env-",
                dir=ROOT,
            ) as sensitive_tmp,
        ):
            release_dir = Path(tmp) / "release"
            env = os.environ.copy()
            env["AUTODESIGN_RELEASE_DIR"] = str(release_dir)
            env["GIT_INDEX_FILE"] = str(Path(tmp) / "git-index")
            for git_args in (("read-tree", "HEAD"), ("add", "-A")):
                indexed = subprocess.run(
                    ["git", *git_args],
                    cwd=ROOT,
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(indexed.returncode, 0, indexed.stderr)
            sensitive_paths = []
            for name in (".env", ".env.local", ".env.production"):
                sensitive_path = Path(sensitive_tmp) / name
                sensitive_path.write_text("release-test-sentinel\n", encoding="utf-8")
                sensitive_paths.append(sensitive_path.relative_to(ROOT).as_posix())
            indexed = subprocess.run(
                ["git", "add", "--force", "--", *sensitive_paths],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(indexed.returncode, 0, indexed.stderr)
            completed = subprocess.run(
                [str(ROOT / "scripts" / "package_local_release.sh")],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
            )

            archive = release_dir / "designanything-local.tar.gz"
            self.assertTrue(archive.is_file(), archive)
            checksum = release_dir / "designanything-local.tar.gz.sha256"
            self.assertTrue(checksum.is_file(), checksum)
            self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(checksum.stat().st_mode), 0o644)
            self.assertRegex(
                checksum.read_text(encoding="utf-8"),
                r"^[0-9a-f]{64}  designanything-local\.tar\.gz\n$",
            )
            self.assertIn(
                'DEFAULT_BUNDLE_URL="https://designanything.ai/downloads/designanything-local.tar.gz"',
                (ROOT / "install.sh").read_text(encoding="utf-8"),
            )

            extract_dir = Path(tmp) / "extracted"
            with tarfile.open(archive, "r:gz") as bundle:
                members = {member.name: member for member in bundle.getmembers()}
                self.assertEqual(
                    {name.split("/", 1)[0] for name in members},
                    {"AutoDesign"},
                )
                self.assertNotIn("AutoDesign/.env", members)
                self.assertNotIn("AutoDesign/.env.local", members)
                self.assertNotIn(
                    "AutoDesign/eval/poster_quality_sets.json",
                    members,
                )
                self.assertNotIn(
                    "AutoDesign/eval/poster_quality_labels.json",
                    members,
                )
                for sensitive_path in sensitive_paths:
                    self.assertNotIn(f"AutoDesign/{sensitive_path}", members)
                self.assertFalse(
                    any(member.pax_headers for member in members.values()),
                    "release archive must not carry host-specific extended attributes",
                )
                self.assertTrue(
                    all(
                        member.uid == 0
                        and member.gid == 0
                        and member.uname in {"", "root"}
                        and member.gname in {"", "root"}
                        for member in members.values()
                    ),
                    "release archive must not carry the local builder identity",
                )
                for launcher in (
                    "AutoDesign/scripts/autodesign",
                    "AutoDesign/scripts/designanything",
                ):
                    self.assertIn(launcher, members)
                    self.assertTrue(members[launcher].mode & stat.S_IXUSR, launcher)
                bundle.extractall(extract_dir)

            bundle_root = extract_dir / "AutoDesign"
            canonical = subprocess.run(
                [str(bundle_root / "scripts" / "autodesign"), "path"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(canonical.returncode, 0, canonical.stderr)
            self.assertEqual(canonical.stdout.strip(), str(bundle_root.resolve()))

            legacy = subprocess.run(
                [str(bundle_root / "scripts" / "designanything"), "path"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(legacy.returncode, 0, legacy.stderr)
            self.assertEqual(legacy.stdout.strip(), str(bundle_root.resolve()))
            self.assertIn("deprecated", legacy.stderr)

            legacy_setup_dir = bundle_root / ".designanything"
            legacy_setup_dir.mkdir()
            (legacy_setup_dir / "setup.stamp").write_text("legacy\n", encoding="utf-8")
            bundled_python = bundle_root / ".venv" / "bin" / "python"
            bundled_python.parent.mkdir(parents=True)
            bundled_python.write_text(
                "#!/bin/sh\n"
                f"exec {shlex.quote(sys.executable)} \"$@\"\n",
                encoding="utf-8",
            )
            bundled_python.chmod(0o755)

            fake_bin = Path(tmp) / "fake-bin"
            fake_bin.mkdir()
            uv_log = Path(tmp) / "uv.log"
            health_server = Path(tmp) / "health_server.py"
            health_server.write_text(
                f"#!{sys.executable}\n"
                "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
                "import sys\n"
                "\n"
                "class Handler(BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n"
                "        if self.path != '/api/health':\n"
                "            self.send_error(404)\n"
                "            return\n"
                "        self.send_response(200)\n"
                "        self.end_headers()\n"
                "\n"
                "    def log_message(self, _format, *_args):\n"
                "        pass\n"
                "\n"
                "server = HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler)\n"
                "server.handle_request()\n",
                encoding="utf-8",
            )
            health_server.chmod(0o755)
            fake_uv = fake_bin / "uv"
            fake_uv.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$AUTODESIGN_TEST_UV_LOG\"\n"
                "if [ \"$1\" = \"run\" ] && [ \"$2\" = \"python\" ]; then\n"
                "  shift 2\n"
                "  exec \"$AUTODESIGN_TEST_PYTHON\" \"$@\"\n"
                "fi\n"
                "if [ \"$1\" = \"run\" ] && [ \"$2\" = \"uvicorn\" ]; then\n"
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
            fake_npm = fake_bin / "npm"
            fake_npm.write_text(
                "#!/bin/sh\n"
                "mkdir -p node_modules/.bin\n"
                "printf '#!/bin/sh\\nprintf \"%%s\\\\n\" \"$*\" >> \"$AUTODESIGN_TEST_HYPERFRAMES_LOG\"\\n"
                "if [ \"${1:-}\" = \"--version\" ]; then echo 0.7.86; fi\\n"
                "exit 0\\n' > node_modules/.bin/hyperframes\n"
                "chmod +x node_modules/.bin/hyperframes\n",
                encoding="utf-8",
            )
            fake_npm.chmod(0o755)
            hyperframes_log = Path(tmp) / "hyperframes.log"
            start_env = os.environ.copy()
            start_env["PATH"] = f"{fake_bin}{os.pathsep}{start_env['PATH']}"
            start_env["AUTODESIGN_STATE_DIR"] = str(Path(tmp) / "canonical-state")
            start_env["AUTODESIGN_NO_OPEN"] = "1"
            start_env["AUTODESIGN_TEST_HEALTH_SERVER"] = str(health_server)
            start_env["AUTODESIGN_TEST_HYPERFRAMES_LOG"] = str(hyperframes_log)
            start_env["AUTODESIGN_TEST_PYTHON"] = sys.executable
            start_env["AUTODESIGN_TEST_UV_LOG"] = str(uv_log)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as first_socket:
                first_socket.bind(("127.0.0.1", 0))
                first_port = first_socket.getsockname()[1]
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as second_socket:
                second_socket.bind(("127.0.0.1", 0))
                second_port = second_socket.getsockname()[1]
            started = subprocess.run(
                [
                    str(bundle_root / "scripts" / "autodesign"),
                    "start",
                    "--port",
                    str(first_port),
                ],
                env=start_env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            uv_calls = uv_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(uv_calls), 3, uv_calls)
            self.assertEqual(uv_calls[0], "sync")
            self.assertEqual(
                uv_calls[1],
                "run python scripts/install_playwright_browsers.py",
            )
            self.assertIn("run uvicorn scripts.web_server:app", uv_calls[2])
            self.assertEqual(
                hyperframes_log.read_text(encoding="utf-8").splitlines(),
                ["browser ensure"],
            )

            launcher_home = Path(tmp) / "launcher-home"
            launcher_legacy_state = launcher_home / ".designanything"
            launcher_legacy_state.mkdir(parents=True)
            (launcher_legacy_state / "legacy.txt").write_text(
                "legacy launcher state\n",
                encoding="utf-8",
            )
            launcher_work = Path(tmp) / "launcher-work"
            launcher_work.mkdir()
            launcher_env = start_env.copy()
            launcher_env["HOME"] = str(launcher_home)
            launcher_env["AUTODESIGN_STATE_DIR"] = "relative-launcher-state"
            migrated_launcher = subprocess.run(
                [
                    str(bundle_root / "scripts" / "autodesign"),
                    "start",
                    "--skip-setup",
                    "--port",
                    str(second_port),
                ],
                cwd=launcher_work,
                env=launcher_env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                migrated_launcher.returncode,
                0,
                migrated_launcher.stderr,
            )
            launcher_state = launcher_work / "relative-launcher-state"
            self.assertEqual(
                (launcher_state / "legacy.txt").read_text(encoding="utf-8"),
                "legacy launcher state\n",
            )
            self.assertTrue(launcher_legacy_state.is_symlink())
            self.assertEqual(
                launcher_legacy_state.resolve(),
                launcher_state.resolve(),
            )

            installer_home = Path(tmp) / "installer-home"
            installer_legacy_state = installer_home / ".designanything"
            installer_legacy_state.mkdir(parents=True)
            (installer_legacy_state / "legacy.txt").write_text(
                "legacy installer state\n",
                encoding="utf-8",
            )
            installer_work = Path(tmp) / "installer-work"
            installer_work.mkdir()
            installer_env = os.environ.copy()
            installer_env["HOME"] = str(installer_home)
            installer_env["AUTODESIGN_STATE_DIR"] = "relative-installer-state"
            installer_env["AUTODESIGN_INSTALL_DIR"] = str(
                Path(tmp) / "installed-autodesign"
            )
            installer_env["AUTODESIGN_BIN_DIR"] = str(Path(tmp) / "installed-bin")
            installer_env["AUTODESIGN_SKIP_SETUP"] = "1"
            installed = subprocess.run(
                [str(bundle_root / "install.sh")],
                cwd=installer_work,
                env=installer_env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            installer_state = installer_work / "relative-installer-state"
            self.assertEqual(
                (installer_state / "legacy.txt").read_text(encoding="utf-8"),
                "legacy installer state\n",
            )
            self.assertTrue(installer_legacy_state.is_symlink())
            self.assertEqual(
                installer_legacy_state.resolve(),
                installer_state.resolve(),
            )


if __name__ == "__main__":
    unittest.main()
