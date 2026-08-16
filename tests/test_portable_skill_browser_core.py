from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_skills._shared import browser_worker
from agent_skills._shared import setup_browser


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS = (
    "autodesign-poster",
    "autodesign-ppt",
    "autodesign-webpage",
    "autodesign-video",
)


class PortableSkillBrowserSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cache_root = self.root / "browser-cache"
        self.requirements = REPO_ROOT / "agent_skills" / "_shared" / "requirements-browser.lock"
        self.worker = REPO_ROOT / "agent_skills" / "_shared" / "browser_worker.py"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _fake_install(self, staging: Path, spec: setup_browser.RuntimeSpec) -> None:
        python = staging / setup_browser.venv_python_relative_path()
        browser = staging / "browsers" / "chromium-fixture" / "chrome"
        python.parent.mkdir(parents=True, exist_ok=True)
        browser.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"fake-python")
        browser.write_bytes(b"fake-chromium")
        python.chmod(python.stat().st_mode | stat.S_IXUSR)
        browser.chmod(browser.stat().st_mode | stat.S_IXUSR)
        setup_browser.write_runtime_state(staging, spec, browser)

    def test_requirements_lock_is_exact_hash_pinned_playwright_closure(self) -> None:
        text = self.requirements.read_text(encoding="utf-8")
        normalized = " ".join(
            line.strip().removesuffix("\\").strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        for requirement in (
            "playwright==1.59.0",
            "greenlet==3.5.0",
            "pyee==13.0.1",
            "typing-extensions==4.15.0",
        ):
            self.assertIn(requirement, normalized)
        self.assertEqual(normalized.count("=="), 4)
        self.assertGreaterEqual(normalized.count("--hash=sha256:"), 4)
        self.assertNotIn("http://", normalized)
        self.assertNotIn("https://", normalized)

    def test_cache_key_binds_runtime_platform_python_and_playwright(self) -> None:
        spec = setup_browser.runtime_spec(
            cache_root=self.cache_root,
            requirements_path=self.requirements,
            worker_path=self.worker,
        )
        self.assertIn(f"runtime-v{setup_browser.RUNTIME_SCHEMA_VERSION}", spec.cache_key)
        self.assertIn(platform.system().lower(), spec.cache_key)
        self.assertIn(platform.machine().lower(), spec.cache_key)
        self.assertIn(f"py{sys.version_info.major}.{sys.version_info.minor}", spec.cache_key)
        self.assertIn("playwright-1.59.0", spec.cache_key)
        self.assertEqual(spec.cache_dir, self.cache_root / spec.cache_key)

    def test_isolated_environment_strips_python_and_conda_injection(self) -> None:
        base = {
            "PATH": "/usr/bin",
            "HOME": str(self.root),
            "PYTHONPATH": "/tmp/inject",
            "PYTHONHOME": "/tmp/home",
            "VIRTUAL_ENV": "/tmp/venv",
            "CONDA_PREFIX": "/tmp/conda",
            "CONDA_DEFAULT_ENV": "base",
            "HTTP_PROXY": "http://proxy.invalid:3128",
            "OPENAI_API_KEY": "must-not-reach-browser",
        }
        install_env = setup_browser.isolated_environment(
            base,
            browsers_path=self.root / "browsers",
            allow_network_configuration=True,
        )
        browser_env = setup_browser.isolated_environment(
            base,
            browsers_path=self.root / "browsers",
            allow_network_configuration=False,
        )
        for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV"):
            self.assertNotIn(name, install_env)
            self.assertNotIn(name, browser_env)
        self.assertEqual(install_env["HTTP_PROXY"], base["HTTP_PROXY"])
        self.assertNotIn("HTTP_PROXY", browser_env)
        self.assertNotIn("OPENAI_API_KEY", install_env)
        self.assertNotIn("OPENAI_API_KEY", browser_env)
        self.assertEqual(
            browser_env["PLAYWRIGHT_BROWSERS_PATH"], str(self.root / "browsers")
        )

    def test_first_install_is_atomic_then_verified_offline_reuse_skips_install(self) -> None:
        probes: list[Path] = []
        with mock.patch.object(setup_browser, "_install_runtime", side_effect=self._fake_install) as install:
            with mock.patch.object(
                setup_browser,
                "_probe_runtime",
                side_effect=lambda runtime, _spec: probes.append(runtime.cache_dir),
            ):
                first = setup_browser.ensure_browser_runtime(
                    cache_root=self.cache_root,
                    requirements_path=self.requirements,
                    worker_path=self.worker,
                )
                second = setup_browser.ensure_browser_runtime(
                    cache_root=self.cache_root,
                    requirements_path=self.requirements,
                    worker_path=self.worker,
                    allow_install=False,
                )
        self.assertEqual(first, second)
        self.assertEqual(install.call_count, 1)
        self.assertEqual(probes, [first.cache_dir, first.cache_dir])
        self.assertTrue((first.cache_dir / "runtime-state.json").is_file())
        self.assertFalse(any(self.cache_root.glob(".*.installing-*")))

    def test_partial_or_corrupt_cache_is_rejected_offline_and_repaired_online(self) -> None:
        with mock.patch.object(setup_browser, "_install_runtime", side_effect=self._fake_install):
            with mock.patch.object(setup_browser, "_probe_runtime"):
                runtime = setup_browser.ensure_browser_runtime(
                    cache_root=self.cache_root,
                    requirements_path=self.requirements,
                    worker_path=self.worker,
                )
        runtime.browser_executable.write_bytes(b"tampered")
        with mock.patch.object(setup_browser, "_probe_runtime"):
            with self.assertRaises(setup_browser.BrowserRuntimeError):
                setup_browser.ensure_browser_runtime(
                    cache_root=self.cache_root,
                    requirements_path=self.requirements,
                    worker_path=self.worker,
                    allow_install=False,
                )

        with mock.patch.object(setup_browser, "_install_runtime", side_effect=self._fake_install) as install:
            with mock.patch.object(setup_browser, "_probe_runtime"):
                repaired = setup_browser.ensure_browser_runtime(
                    cache_root=self.cache_root,
                    requirements_path=self.requirements,
                    worker_path=self.worker,
                )
        self.assertEqual(install.call_count, 1)
        self.assertEqual(repaired.browser_executable.read_bytes(), b"fake-chromium")
        self.assertFalse(any(self.cache_root.glob(".*.corrupt-*")))

    def test_valid_cache_with_launch_failure_is_not_reinstalled(self) -> None:
        with mock.patch.object(setup_browser, "_install_runtime", side_effect=self._fake_install):
            with mock.patch.object(setup_browser, "_probe_runtime"):
                runtime = setup_browser.ensure_browser_runtime(
                    cache_root=self.cache_root,
                    requirements_path=self.requirements,
                    worker_path=self.worker,
                )
        state_before = (runtime.cache_dir / "runtime-state.json").read_bytes()
        with mock.patch.object(setup_browser, "_install_runtime") as install:
            with mock.patch.object(
                setup_browser,
                "_probe_runtime",
                side_effect=setup_browser.BrowserRuntimeError("Linux libraries are missing"),
            ):
                with self.assertRaisesRegex(setup_browser.BrowserRuntimeError, "Linux libraries"):
                    setup_browser.ensure_browser_runtime(
                        cache_root=self.cache_root,
                        requirements_path=self.requirements,
                        worker_path=self.worker,
                    )
        install.assert_not_called()
        self.assertEqual((runtime.cache_dir / "runtime-state.json").read_bytes(), state_before)

    def test_failed_install_never_promotes_partial_cache(self) -> None:
        def fail_install(staging: Path, _spec: setup_browser.RuntimeSpec) -> None:
            (staging / "partial").mkdir(parents=True)
            raise setup_browser.BrowserRuntimeError("fixture install failed")

        with mock.patch.object(setup_browser, "_install_runtime", side_effect=fail_install):
            with self.assertRaisesRegex(setup_browser.BrowserRuntimeError, "fixture install failed"):
                setup_browser.ensure_browser_runtime(
                    cache_root=self.cache_root,
                    requirements_path=self.requirements,
                    worker_path=self.worker,
                )
        self.assertFalse(any(path.is_dir() for path in self.cache_root.iterdir()))

    def test_concurrent_callers_share_one_completed_install(self) -> None:
        install_started = threading.Event()
        release_install = threading.Event()
        results: list[setup_browser.BrowserRuntime] = []
        errors: list[BaseException] = []

        def slow_install(staging: Path, spec: setup_browser.RuntimeSpec) -> None:
            install_started.set()
            release_install.wait(timeout=5)
            self._fake_install(staging, spec)

        def call() -> None:
            try:
                results.append(
                    setup_browser.ensure_browser_runtime(
                        cache_root=self.cache_root,
                        requirements_path=self.requirements,
                        worker_path=self.worker,
                        lock_timeout_seconds=5,
                    )
                )
            except BaseException as error:  # pragma: no cover - assertion captures details
                errors.append(error)

        with mock.patch.object(setup_browser, "_install_runtime", side_effect=slow_install) as install:
            with mock.patch.object(setup_browser, "_probe_runtime"):
                first = threading.Thread(target=call)
                second = threading.Thread(target=call)
                first.start()
                self.assertTrue(install_started.wait(timeout=2))
                second.start()
                time.sleep(0.1)
                release_install.set()
                first.join(timeout=5)
                second.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(install.call_count, 1)

    def test_runtime_state_rejects_symlinked_cache_or_browser_escape(self) -> None:
        self.cache_root.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        linked = self.cache_root / "linked-runtime"
        linked.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(setup_browser.BrowserRuntimeError):
            setup_browser.inspect_browser_runtime(linked, None)

    def test_runtime_state_rejects_symlinked_venv_parent_escape(self) -> None:
        spec = setup_browser.runtime_spec(
            cache_root=self.cache_root,
            requirements_path=self.requirements,
            worker_path=self.worker,
        )
        self._fake_install(spec.cache_dir, spec)
        outside = self.root / "outside-venv"
        (outside / "venv").parent.mkdir(parents=True)
        os.replace(spec.cache_dir / "venv", outside / "venv")
        (spec.cache_dir / "venv").symlink_to(outside / "venv", target_is_directory=True)
        with self.assertRaises(setup_browser.BrowserRuntimeError):
            setup_browser.inspect_browser_runtime(spec.cache_dir, spec)

    def test_audit_wrapper_uses_isolated_runtime_python_and_external_output(self) -> None:
        workspace = self.root / "workspace"
        output = workspace / "qa"
        workspace.mkdir()
        html = workspace / "index.html"
        html.write_text("<h1>Fixture</h1>", encoding="utf-8")
        spec = setup_browser.runtime_spec(
            cache_root=self.cache_root,
            requirements_path=self.requirements,
            worker_path=self.worker,
        )
        staging = self.cache_root / spec.cache_key
        self._fake_install(staging, spec)
        runtime = setup_browser.inspect_browser_runtime(staging, spec)
        captured: dict[str, object] = {}

        def fake_run(command: list[str], *, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
            captured.update(command=command, env=env, timeout=timeout)
            output.mkdir(parents=True, exist_ok=True)
            (output / "audit.json").write_text(
                json.dumps({"passed": True, "checks": {"fixture": True}}), encoding="utf-8"
            )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch.object(setup_browser, "_probe_runtime"):
            report = setup_browser.audit_local_html(
                html,
                workspace_root=workspace,
                output_dir=output,
                runtime=runtime,
                command_runner=fake_run,
            )
        command = captured["command"]
        self.assertEqual(command[0], str(runtime.python_executable))
        self.assertEqual(command[1:3], ["-I", str(self.worker)])
        self.assertTrue(report["passed"])
        self.assertTrue((output / "audit.json").is_file())
        self.assertFalse(any(REPO_ROOT.glob("audit.json")))

    def test_audit_wrapper_rejects_output_outside_workspace_before_launch(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        html = workspace / "index.html"
        html.write_text("<h1>Fixture</h1>", encoding="utf-8")
        with self.assertRaisesRegex(setup_browser.BrowserRuntimeError, "output"):
            setup_browser.audit_local_html(
                html,
                workspace_root=workspace,
                output_dir=self.root / "outside",
                allow_install=False,
            )

    def test_sync_vendors_browser_runtime_with_exact_bytes(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/sync_agent_skill_core.py", "--check"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        mapping = {
            "scripts/browser_worker.py": "browser_worker.py",
            "scripts/setup_browser.py": "setup_browser.py",
            "scripts/requirements-browser.lock": "requirements-browser.lock",
        }
        for skill in SKILLS:
            for target_name, source_name in mapping.items():
                with self.subTest(skill=skill, target=target_name):
                    self.assertEqual(
                        (REPO_ROOT / "agent_skills" / skill / target_name).read_bytes(),
                        (REPO_ROOT / "agent_skills" / "_shared" / source_name).read_bytes(),
                    )


class PortableSkillBrowserWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_request_policy_allows_only_local_workspace_and_safe_inline_schemes(self) -> None:
        local = self.workspace / "assets" / "figure.png"
        local.parent.mkdir()
        local.write_bytes(b"png")
        allowed = browser_worker.classify_request(local.as_uri(), self.workspace)
        self.assertTrue(allowed.allowed)
        self.assertFalse(allowed.missing)

        for url in ("about:blank", "data:text/plain,ok", "blob:null/fixture"):
            with self.subTest(url=url):
                self.assertTrue(browser_worker.classify_request(url, self.workspace).allowed)

        missing = browser_worker.classify_request(
            (self.workspace / "assets" / "missing.png").as_uri(), self.workspace
        )
        outside = self.root / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        escaped = browser_worker.classify_request(outside.as_uri(), self.workspace)
        remote = browser_worker.classify_request(
            "https://example.com/private/path?token=super-secret", self.workspace
        )
        socket = browser_worker.classify_request("wss://example.com/socket?key=secret", self.workspace)
        self.assertFalse(missing.allowed)
        self.assertTrue(missing.missing)
        self.assertFalse(escaped.allowed)
        self.assertFalse(remote.allowed)
        self.assertFalse(socket.allowed)
        self.assertNotIn("super-secret", remote.sanitized_url)
        self.assertNotIn("private/path", remote.sanitized_url)
        self.assertNotIn("secret", socket.sanitized_url)

    def test_policy_rejects_encoded_file_traversal_and_non_workspace_host(self) -> None:
        encoded = f"file://{self.workspace.as_posix()}/assets/%2e%2e/%2e%2e/secret.txt"
        hosted = "file://attacker.example/tmp/fixture.html"
        self.assertFalse(browser_worker.classify_request(encoded, self.workspace).allowed)
        self.assertFalse(browser_worker.classify_request(hosted, self.workspace).allowed)

    def test_result_fails_closed_on_security_missing_blank_and_horizontal_overflow(self) -> None:
        base = {
            "blocked_requests": [],
            "missing_local_assets": [],
            "console_errors": [],
            "page_errors": [],
            "request_errors": [],
            "blank_render": False,
            "geometry": {
                "viewport_width": 1440,
                "viewport_height": 900,
                "scroll_width": 1440,
                "scroll_height": 1800,
                "horizontal_overflow": 0,
                "vertical_overflow": 900,
                "out_of_canvas": [],
            },
            "screenshot": "preview.png",
        }
        passed = browser_worker.finalize_observation(base)
        self.assertTrue(passed["passed"])
        self.assertEqual(passed["geometry"]["vertical_overflow"], 900)
        cases = (
            ("blocked_requests", [{"url": "https://example.com"}]),
            ("missing_local_assets", [{"url": "file:///[workspace]/missing.png"}]),
            ("blank_render", True),
        )
        for key, value in cases:
            with self.subTest(key=key):
                observation = json.loads(json.dumps(base))
                observation[key] = value
                self.assertFalse(browser_worker.finalize_observation(observation)["passed"])
        overflow = json.loads(json.dumps(base))
        overflow["geometry"]["horizontal_overflow"] = 1
        self.assertFalse(browser_worker.finalize_observation(overflow)["passed"])

    def test_workspace_and_output_paths_reject_escape_symlinks_and_installed_package_writes(self) -> None:
        html = self.workspace / "index.html"
        html.write_text("<h1>Fixture</h1>", encoding="utf-8")
        outside = self.root / "outside"
        outside.mkdir()
        link = self.workspace / "linked-output"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(browser_worker.BrowserAuditError):
            browser_worker.resolve_audit_paths(html, self.workspace, link)
        with self.assertRaises(browser_worker.BrowserAuditError):
            browser_worker.resolve_audit_paths(html, self.workspace, outside)

        package_root = Path(browser_worker.__file__).resolve().parent.parent
        package_html = package_root / "fixture-do-not-create.html"
        with self.assertRaises(browser_worker.BrowserAuditError):
            browser_worker.resolve_audit_paths(package_html, package_root, package_root / "qa")

    def test_probe_report_rejects_installed_package_write(self) -> None:
        package_root = Path(browser_worker.__file__).resolve().parent.parent
        with self.assertRaises(browser_worker.BrowserAuditError):
            browser_worker._safe_probe_report_path(package_root / "probe.json")


if __name__ == "__main__":
    unittest.main()
