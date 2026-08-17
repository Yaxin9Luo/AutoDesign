from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import platform
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from dataclasses import replace
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


def _png_fixture(width: int, height: int, pixels: list[tuple[int, int, int]]) -> bytes:
    if len(pixels) != width * height:
        raise ValueError("pixel count does not match dimensions")

    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    rows = bytearray()
    for row in range(height):
        rows.append(0)
        for red, green, blue in pixels[row * width : (row + 1) * width]:
            rows.extend((red, green, blue))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows)))
        + chunk(b"IEND", b"")
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

    def test_lock_wait_covers_the_declared_first_install_bound(self) -> None:
        self.assertGreaterEqual(
            setup_browser._DEFAULT_LOCK_TIMEOUT_SECONDS,
            setup_browser._MAX_FIRST_INSTALL_SECONDS,
        )

    def test_lock_records_unique_owner_and_does_not_delete_successor(self) -> None:
        lock = self.root / "runtime.lock"
        successor = {"pid": os.getpid(), "token": "f" * 32, "created_epoch": time.time()}
        with setup_browser._cache_lock(lock, 1):
            owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
            self.assertEqual(owner["pid"], os.getpid())
            self.assertRegex(owner["token"], r"^[0-9a-f]{32}$")
            moved = self.root / "old-lock"
            os.replace(lock, moved)
            lock.mkdir()
            (lock / "owner.json").write_text(json.dumps(successor), encoding="utf-8")
        self.assertTrue(lock.is_dir())
        self.assertEqual(
            json.loads((lock / "owner.json").read_text(encoding="utf-8")), successor
        )
        guard = lock.with_name(f"{lock.name}.guard")
        self.assertTrue(guard.is_file())
        self.assertNotIn(str(guard.absolute()), setup_browser._THREAD_GUARDS)

    def test_dead_lock_owner_is_recovered_without_stale_timeout(self) -> None:
        lock = self.root / "runtime.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(
            json.dumps({"pid": 987654321, "token": "d" * 32, "created_epoch": time.time()}),
            encoding="utf-8",
        )
        acquired = False
        with mock.patch.object(setup_browser, "_process_is_alive", return_value=False):
            try:
                with setup_browser._cache_lock(lock, 0.1):
                    acquired = True
            except setup_browser.BrowserRuntimeError:
                pass
        self.assertTrue(acquired)
        self.assertFalse(lock.exists())

    def test_windows_liveness_probe_does_not_use_posix_signal_zero(self) -> None:
        with mock.patch.object(setup_browser.os, "name", "nt"):
            with mock.patch.object(
                setup_browser, "_windows_process_is_alive", return_value=True, create=True
            ) as windows_probe:
                with mock.patch.object(setup_browser.os, "kill") as posix_probe:
                    self.assertTrue(setup_browser._process_is_alive(1234))
        windows_probe.assert_called_once_with(1234)
        posix_probe.assert_not_called()

    def test_windows_liveness_probe_reads_process_exit_state_without_termination(self) -> None:
        import ctypes

        for exit_code, expected in ((259, True), (0, False)):
            with self.subTest(exit_code=exit_code):
                open_process = mock.Mock(return_value=123)

                def write_exit_code(_handle: object, pointer: object) -> int:
                    pointer._obj.value = exit_code
                    return 1

                get_exit_code = mock.Mock(side_effect=write_exit_code)
                close_handle = mock.Mock(return_value=1)
                kernel32 = mock.Mock(
                    OpenProcess=open_process,
                    GetExitCodeProcess=get_exit_code,
                    CloseHandle=close_handle,
                )
                with mock.patch.object(
                    ctypes, "WinDLL", return_value=kernel32, create=True
                ):
                    self.assertEqual(
                        setup_browser._windows_process_is_alive(1234), expected
                    )
                open_process.assert_called_once_with(0x1000, False, 1234)
                close_handle.assert_called_once_with(123)

    def test_windows_advisory_lock_locks_one_byte_at_offset_zero(self) -> None:
        guard = self.root / "windows.guard"
        guard.write_bytes(b"\0")
        descriptor = os.open(guard, os.O_RDWR)
        msvcrt = mock.Mock(LK_NBLCK=1, LK_UNLCK=2)
        try:
            with mock.patch.object(setup_browser.os, "name", "nt"):
                with mock.patch.dict(sys.modules, {"msvcrt": msvcrt}):
                    self.assertTrue(setup_browser._try_advisory_lock(descriptor))
                    setup_browser._unlock_advisory_lock(descriptor)
        finally:
            os.close(descriptor)
        self.assertEqual(
            msvcrt.locking.call_args_list,
            [mock.call(descriptor, 1, 1), mock.call(descriptor, 2, 1)],
        )

    def test_advisory_guard_rejects_symlink_and_releases_thread_map_entry(self) -> None:
        lock = self.root / "runtime.lock"
        guard = lock.with_name(f"{lock.name}.guard")
        outside = self.root / "outside.guard"
        outside.write_bytes(b"\0")
        guard.symlink_to(outside)
        with self.assertRaisesRegex(
            setup_browser.BrowserRuntimeError, "Unsafe browser runtime advisory lock"
        ):
            with setup_browser._cache_lock(lock, 0.1):
                self.fail("symlinked advisory guard was accepted")
        self.assertNotIn(str(guard.absolute()), setup_browser._THREAD_GUARDS)

    def test_live_lock_owner_is_never_stolen_based_on_age(self) -> None:
        lock = self.root / "runtime.lock"
        lock.mkdir()
        owner = {"pid": 42, "token": "a" * 32, "created_epoch": 1.0}
        (lock / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
        old = time.time() - 7200
        os.utime(lock, (old, old))
        with mock.patch.object(setup_browser, "_process_is_alive", return_value=True):
            with self.assertRaisesRegex(setup_browser.BrowserRuntimeError, "Timed out"):
                with setup_browser._cache_lock(lock, 0.05):
                    self.fail("live lock owner was stolen")
        self.assertEqual(
            json.loads((lock / "owner.json").read_text(encoding="utf-8")), owner
        )

    def test_ownerless_lock_observes_grace_before_recovery(self) -> None:
        lock = self.root / "runtime.lock"
        lock.mkdir()
        with mock.patch.object(setup_browser, "_LOCK_OWNER_GRACE_SECONDS", 0.05):
            with self.assertRaisesRegex(setup_browser.BrowserRuntimeError, "Timed out"):
                with setup_browser._cache_lock(lock, 0):
                    self.fail("fresh ownerless lock was stolen before the grace period")

            old = time.time() - 1
            os.utime(lock, (old, old))
            with setup_browser._cache_lock(lock, 0.1):
                owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
                self.assertEqual(owner["pid"], os.getpid())
        self.assertFalse(lock.exists())

    def test_lock_owner_is_published_before_other_callers_can_recover(self) -> None:
        lock = self.root / "runtime.lock"
        owner_write_paused = threading.Event()
        release_owner_write = threading.Event()
        release_holders = threading.Event()
        first_owner_write = True
        entered_while_owner_unpublished = False
        active_holders = 0
        max_holders = 0
        errors: list[BaseException] = []
        state_guard = threading.Lock()
        original_write_text = Path.write_text

        def pause_first_owner_write(
            path: Path, data: str, *args: object, **kwargs: object
        ) -> int:
            nonlocal first_owner_write
            if path == lock / "owner.json":
                with state_guard:
                    should_pause = first_owner_write
                    first_owner_write = False
                if should_pause:
                    owner_write_paused.set()
                    release_owner_write.wait(timeout=3)
            return original_write_text(path, data, *args, **kwargs)

        def holder() -> None:
            nonlocal active_holders, max_holders
            try:
                with setup_browser._cache_lock(lock, 3):
                    with state_guard:
                        active_holders += 1
                        max_holders = max(max_holders, active_holders)
                    release_holders.wait(timeout=3)
                    with state_guard:
                        active_holders -= 1
            except BaseException as error:  # pragma: no cover - assertion reports it
                errors.append(error)

        with mock.patch.object(Path, "write_text", new=pause_first_owner_write):
            with mock.patch.object(setup_browser, "_LOCK_OWNER_GRACE_SECONDS", 0.0):
                first = threading.Thread(target=holder)
                second = threading.Thread(target=holder)
                first.start()
                self.assertTrue(owner_write_paused.wait(timeout=2))
                second.start()
                time.sleep(0.15)
                with state_guard:
                    entered_while_owner_unpublished = active_holders > 0
                release_owner_write.set()
                time.sleep(0.15)
                release_holders.set()
                first.join(timeout=4)
                second.join(timeout=4)

        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(entered_while_owner_unpublished)
        self.assertEqual(max_holders, 1)

    def test_dead_owner_recovery_cannot_move_a_live_successor_lock(self) -> None:
        lock = self.root / "runtime.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(
            json.dumps({"pid": 987654321, "token": "d" * 32, "created_epoch": 1.0}),
            encoding="utf-8",
        )
        liveness_barrier = threading.Barrier(2)
        first_entered = threading.Event()
        second_entered = threading.Event()
        release_holders = threading.Event()
        active_holders = 0
        max_holders = 0
        recovery_calls = 0
        errors: list[BaseException] = []
        state_guard = threading.Lock()
        original_recover = setup_browser._quarantine_and_remove

        def dead_owner(_pid: int) -> bool:
            try:
                liveness_barrier.wait(timeout=0.2)
            except threading.BrokenBarrierError:
                pass
            return False

        def ordered_recover(path: Path, *, marker: str) -> None:
            nonlocal recovery_calls
            if marker != "orphaned":
                original_recover(path, marker=marker)
                return
            with state_guard:
                recovery_calls += 1
                call_number = recovery_calls
            if call_number == 2:
                first_entered.wait(timeout=2)
            original_recover(path, marker=marker)

        def holder() -> None:
            nonlocal active_holders, max_holders
            try:
                with setup_browser._cache_lock(lock, 3):
                    with state_guard:
                        active_holders += 1
                        max_holders = max(max_holders, active_holders)
                        if active_holders == 1:
                            first_entered.set()
                        else:
                            second_entered.set()
                    release_holders.wait(timeout=3)
                    with state_guard:
                        active_holders -= 1
            except BaseException as error:  # pragma: no cover - assertion reports it
                errors.append(error)

        with mock.patch.object(setup_browser, "_process_is_alive", side_effect=dead_owner):
            with mock.patch.object(
                setup_browser, "_quarantine_and_remove", side_effect=ordered_recover
            ):
                first = threading.Thread(target=holder)
                second = threading.Thread(target=holder)
                first.start()
                second.start()
                self.assertTrue(first_entered.wait(timeout=2))
                successor_was_moved = second_entered.wait(timeout=0.2)
                release_holders.set()
                first.join(timeout=4)
                second.join(timeout=4)

        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(successor_was_moved)
        self.assertEqual(max_holders, 1)

    def test_lock_heartbeat_refreshes_owner_timestamp(self) -> None:
        lock = self.root / "runtime.lock"
        real_utime = os.utime
        with mock.patch.object(setup_browser, "_LOCK_HEARTBEAT_SECONDS", 0.01):
            with mock.patch.object(setup_browser.os, "utime", wraps=real_utime) as utime:
                with setup_browser._cache_lock(lock, 1):
                    deadline = time.monotonic() + 1
                    while utime.call_count < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertGreaterEqual(utime.call_count, 2)
                    refreshed = {Path(call.args[0]).name for call in utime.call_args_list}
                    self.assertIn("runtime.lock", refreshed)
                    self.assertIn("owner.json", refreshed)

    def test_abandoned_install_staging_is_cleaned_under_lock(self) -> None:
        spec = setup_browser.runtime_spec(
            cache_root=self.cache_root,
            requirements_path=self.requirements,
            worker_path=self.worker,
        )
        abandoned = self.cache_root / f".{spec.cache_key}.installing-abandoned"
        abandoned.mkdir(parents=True)
        (abandoned / "partial").write_bytes(b"partial")
        with mock.patch.object(setup_browser, "_install_runtime", side_effect=self._fake_install):
            with mock.patch.object(setup_browser, "_probe_runtime"):
                setup_browser.ensure_browser_runtime(
                    cache_root=self.cache_root,
                    requirements_path=self.requirements,
                    worker_path=self.worker,
                )
        self.assertFalse(abandoned.exists())

    def test_windows_arm64_python310_is_rejected_before_install_commands(self) -> None:
        spec = setup_browser.runtime_spec(
            cache_root=self.cache_root,
            requirements_path=self.requirements,
            worker_path=self.worker,
        )
        unsupported = replace(
            spec,
            system="windows",
            machine="arm64",
            python_major_minor="3.10",
        )
        staging = self.cache_root / "unsupported"
        staging.mkdir(parents=True)
        with mock.patch.object(setup_browser, "_run_checked") as run_checked:
            with self.assertRaisesRegex(
                setup_browser.BrowserRuntimeError,
                r"Windows ARM64.*Python 3\.10.*hash-locked wheel",
            ):
                setup_browser._install_runtime(staging, unsupported)
        run_checked.assert_not_called()

    def test_isolated_environment_strips_python_and_conda_injection(self) -> None:
        base = {
            "PATH": "/usr/bin",
            "HOME": str(self.root),
            "USERPROFILE": str(self.root / "windows-home"),
            "HOMEDRIVE": "C:",
            "HOMEPATH": r"\Users\fixture",
            "PYTHONPATH": "/tmp/inject",
            "PYTHONHOME": "/tmp/home",
            "VIRTUAL_ENV": "/tmp/venv",
            "CONDA_PREFIX": "/tmp/conda",
            "CONDA_DEFAULT_ENV": "base",
            "HTTP_PROXY": "http://proxy.invalid:3128",
            "NODE_EXTRA_CA_CERTS": str(self.root / "company-ca.pem"),
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
        self.assertEqual(
            install_env["NODE_EXTRA_CA_CERTS"], base["NODE_EXTRA_CA_CERTS"]
        )
        for name in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
            self.assertEqual(install_env[name], base[name])
            self.assertEqual(browser_env[name], base[name])
        self.assertNotIn("HTTP_PROXY", browser_env)
        self.assertNotIn("NODE_EXTRA_CA_CERTS", browser_env)
        self.assertNotIn("OPENAI_API_KEY", install_env)
        self.assertNotIn("OPENAI_API_KEY", browser_env)
        self.assertEqual(
            browser_env["PLAYWRIGHT_BROWSERS_PATH"], str(self.root / "browsers")
        )

    def test_timed_out_install_command_terminates_descendant_processes(self) -> None:
        marker = self.root / "descendant-survived"
        descendant = (
            "import pathlib,time; "
            "time.sleep(0.75); "
            f"pathlib.Path({str(marker)!r}).write_text('survived', encoding='utf-8')"
        )
        parent = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable, '-I', '-c', {descendant!r}], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
            "time.sleep(10)"
        )

        with self.assertRaisesRegex(
            setup_browser.BrowserRuntimeError, "fixture command failed to start"
        ):
            setup_browser._run_checked(
                [sys.executable, "-I", "-c", parent],
                env=setup_browser.isolated_environment(
                    browsers_path=self.root / "browsers",
                    allow_network_configuration=False,
                ),
                timeout=0.15,
                action="fixture command",
            )

        time.sleep(0.9)
        self.assertFalse(marker.exists(), "timed-out command left a live descendant")

    def test_windows_tree_kill_failure_falls_back_to_direct_process_kill(self) -> None:
        process = mock.Mock(pid=4321)
        failed = subprocess.CompletedProcess(
            ["taskkill", "/PID", "4321", "/T", "/F"], 1
        )
        with mock.patch.object(setup_browser.os, "name", "nt"):
            with mock.patch.object(
                setup_browser.subprocess, "run", return_value=failed
            ) as taskkill:
                setup_browser._terminate_process_tree(process)

        self.assertEqual(taskkill.call_args.args[0][:5], ["taskkill", "/PID", "4321", "/T", "/F"])
        process.kill.assert_called_once_with()

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

    def test_sync_requires_all_canonical_browser_sources(self) -> None:
        root = self.root / "skills"
        shared = root / "_shared"
        shared.mkdir(parents=True)
        (shared / "portable_core.py").write_bytes(b"core")
        (shared / "source-grounding.md").write_bytes(b"grounding")
        for skill in SKILLS:
            (root / skill / "scripts").mkdir(parents=True)
            (root / skill / "references").mkdir()
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
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("missing canonical source", completed.stdout + completed.stderr)

    def test_sync_missing_canonical_source_fails_before_mutating_targets(self) -> None:
        root = self.root / "skills"
        shared = root / "_shared"
        shared.mkdir(parents=True)
        (shared / "portable_core.py").write_bytes(b"canonical-core")
        (shared / "source-grounding.md").write_bytes(b"canonical-grounding")
        for skill in SKILLS:
            (root / skill / "scripts").mkdir(parents=True)
            (root / skill / "references").mkdir()
            (root / skill / "scripts" / "_portable.py").write_bytes(b"drifted-core")
            (root / skill / "references" / "source-grounding.md").write_bytes(
                b"drifted-grounding"
            )
        target = root / SKILLS[0] / "scripts" / "_portable.py"

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
        self.assertIn("missing canonical source", completed.stdout + completed.stderr)
        self.assertEqual(target.read_bytes(), b"drifted-core")


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
            "direct_network_attempts": [],
            "console_errors": [],
            "page_errors": [],
            "request_errors": [],
            "media_errors": [],
            "blank_render": False,
            "dom_state_stable": True,
            "geometry": {
                "viewport_width": 1440,
                "viewport_height": 900,
                "scroll_width": 1440,
                "scroll_height": 1800,
                "horizontal_overflow": 0,
                "vertical_overflow": 900,
                "out_of_canvas": [],
                "clipped_content": [],
                "inspection_truncated": False,
            },
            "screenshot_analysis": {"painted_content": True, "near_uniform": False},
            "screenshot": "preview.png",
        }
        passed = browser_worker.finalize_observation(base)
        self.assertTrue(passed["passed"])
        self.assertEqual(passed["geometry"]["vertical_overflow"], 900)
        cases = (
            ("blocked_requests", [{"url": "https://example.com"}]),
            ("missing_local_assets", [{"url": "file:///[workspace]/missing.png"}]),
            ("direct_network_attempts", [{"api": "RTCPeerConnection"}]),
            ("console_errors", [{"text": "console failure"}]),
            ("request_errors", [{"error": "request failure"}]),
            ("media_errors", [{"selector": "img#broken"}]),
            ("blank_render", True),
            ("dom_state_stable", False),
        )
        for key, value in cases:
            with self.subTest(key=key):
                observation = json.loads(json.dumps(base))
                observation[key] = value
                self.assertFalse(browser_worker.finalize_observation(observation)["passed"])
        overflow = json.loads(json.dumps(base))
        overflow["geometry"]["horizontal_overflow"] = 1
        self.assertFalse(browser_worker.finalize_observation(overflow)["passed"])
        off_canvas = json.loads(json.dumps(base))
        off_canvas["geometry"]["out_of_canvas"] = [{"selector": "#outside"}]
        self.assertFalse(browser_worker.finalize_observation(off_canvas)["passed"])
        clipped = json.loads(json.dumps(base))
        clipped["geometry"]["clipped_content"] = [{"selector": "#clipped"}]
        self.assertFalse(browser_worker.finalize_observation(clipped)["passed"])
        truncated = json.loads(json.dumps(base))
        truncated["geometry"]["inspection_truncated"] = True
        self.assertFalse(browser_worker.finalize_observation(truncated)["passed"])

    def test_png_paint_analysis_rejects_uniform_and_accepts_visible_content(self) -> None:
        white = _png_fixture(20, 20, [(255, 255, 255)] * 400)
        uniform_blue = _png_fixture(20, 20, [(20, 80, 160)] * 400)
        painted_pixels = [(255, 255, 255)] * 400
        for index in range(80, 320):
            painted_pixels[index] = (20, 30, 40)
        painted = _png_fixture(20, 20, painted_pixels)

        self.assertFalse(browser_worker._analyze_png_paint(white)["painted_content"])
        self.assertFalse(browser_worker._analyze_png_paint(uniform_blue)["painted_content"])
        self.assertTrue(browser_worker._analyze_png_paint(painted)["painted_content"])

    def test_atomic_binary_write_replaces_hardlink_without_touching_outside_inode(self) -> None:
        output = self.workspace / "qa"
        output.mkdir()
        outside = self.root / "outside.png"
        outside.write_bytes(b"outside-sentinel")
        target = output / "desktop.png"
        os.link(outside, target)

        browser_worker._atomic_write_bytes(target, b"new-screenshot")

        self.assertEqual(outside.read_bytes(), b"outside-sentinel")
        self.assertEqual(target.read_bytes(), b"new-screenshot")
        self.assertFalse(os.path.samefile(outside, target))

    def test_atomic_binary_write_replaces_symlink_without_touching_outside_target(self) -> None:
        output = self.workspace / "qa"
        output.mkdir()
        outside = self.root / "outside.png"
        outside.write_bytes(b"outside-sentinel")
        target = output / "desktop.png"
        target.symlink_to(outside)

        browser_worker._atomic_write_bytes(target, b"new-screenshot")

        self.assertEqual(outside.read_bytes(), b"outside-sentinel")
        self.assertEqual(target.read_bytes(), b"new-screenshot")
        self.assertFalse(target.is_symlink())

    def test_diagnostic_text_sanitizes_embedded_urls_and_local_roots(self) -> None:
        home = Path.home().resolve()
        text = (
            "failed at https://example.com/private/path?token=top-secret "
            "and wss://socket.example/ws?auth=secret "
            f"and {(self.workspace / 'private' / 'asset.png').as_uri()} "
            f"workspace={self.workspace}/private/file.txt home={home}/private/token.txt"
        )
        sanitized = browser_worker._sanitize_diagnostic_text(text, self.workspace)
        self.assertIn("https://example.com", sanitized)
        self.assertIn("wss://socket.example", sanitized)
        self.assertIn("file:///[workspace]/private/asset.png", sanitized)
        self.assertIn("[workspace]/private/file.txt", sanitized)
        self.assertIn("[home]/private/token.txt", sanitized)
        for secret in ("private/path", "top-secret", "/ws", "auth=", str(home), str(self.workspace)):
            self.assertNotIn(secret, sanitized)

    def test_diagnostic_sanitizer_survives_missing_platform_home(self) -> None:
        with mock.patch.object(
            browser_worker.Path,
            "home",
            side_effect=RuntimeError("Could not determine home directory"),
        ):
            sanitized = browser_worker._sanitize_diagnostic_text(
                f"workspace={self.workspace}/private/file.txt", self.workspace
            )
        self.assertEqual(sanitized, "workspace=[workspace]/private/file.txt")

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


@unittest.skipUnless(
    os.environ.get("AUTODESIGN_SKILL_REAL_BROWSER") == "1",
    "set AUTODESIGN_SKILL_REAL_BROWSER=1 with an explicit verified browser cache",
)
class PortableSkillBrowserRealChromiumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cache = os.environ.get("AUTODESIGN_SKILL_BROWSER_CACHE", "").strip()
        if not cache:
            raise unittest.SkipTest("AUTODESIGN_SKILL_BROWSER_CACHE must be explicit")
        cls.runtime = setup_browser.ensure_browser_runtime(
            cache_root=Path(cache), allow_install=False
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _audit(self, html: Path, *, output_name: str = "qa") -> dict[str, object]:
        return setup_browser.audit_local_html(
            html,
            workspace_root=self.workspace,
            output_dir=self.workspace / output_name,
            viewports=("desktop:640x480",),
            runtime=self.runtime,
            timeout_seconds=60,
        )

    def test_context_policy_blocks_http_udp_worker_popup_and_outside_file(self) -> None:
        http_hits: list[str] = []

        class CanaryHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
                http_hits.append(self.path)
                self.send_response(204)
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        http_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CanaryHandler)
        http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        http_thread.start()

        udp_hits: list[bytes] = []
        udp_stop = threading.Event()
        udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_socket.bind(("127.0.0.1", 0))
        udp_socket.settimeout(0.05)

        def receive_udp() -> None:
            while not udp_stop.is_set():
                try:
                    payload, _address = udp_socket.recvfrom(65535)
                except (TimeoutError, socket.timeout):
                    continue
                except OSError:
                    return
                udp_hits.append(payload)

        udp_thread = threading.Thread(target=receive_udp, daemon=True)
        udp_thread.start()

        socket_hits: list[tuple[str, int]] = []
        socket_stop = threading.Event()
        socket_canary = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        socket_canary.bind(("127.0.0.1", 0))
        socket_canary.listen()
        socket_canary.settimeout(0.05)

        def receive_socket() -> None:
            while not socket_stop.is_set():
                try:
                    connection, address = socket_canary.accept()
                except (TimeoutError, socket.timeout):
                    continue
                except OSError:
                    return
                socket_hits.append(address)
                connection.close()

        socket_thread = threading.Thread(target=receive_socket, daemon=True)
        socket_thread.start()
        try:
            http_url = f"http://127.0.0.1:{http_server.server_port}/private?token=secret"
            ws_url = f"ws://127.0.0.1:{socket_canary.getsockname()[1]}/private?token=secret"
            webtransport_url = (
                f"https://127.0.0.1:{udp_socket.getsockname()[1]}/transport?token=secret"
            )
            outside = self.root / "outside.html"
            outside.write_text("<h1>OUTSIDE SENTINEL</h1>", encoding="utf-8")
            child = self.workspace / "child.html"
            child.write_text(
                f"<script>fetch({json.dumps(http_url)}).catch(() => {{}})</script>",
                encoding="utf-8",
            )
            worker_source = (
                f"fetch({json.dumps(http_url)}).catch(() => {{}});"
                f"try {{ new WebSocket({json.dumps(ws_url)}); }} catch (_error) {{}}"
                f"try {{ new WebTransport({json.dumps(webtransport_url)}); }} catch (_error) {{}}"
            )
            html = self.workspace / "index.html"
            html.write_text(
                f"""<!doctype html>
                <style>body {{ background: #fff }} main {{ background:#174a73;color:white;padding:80px }}</style>
                <main>Network boundary fixture</main>
                <script>
                fetch({json.dumps(http_url)}).catch(() => {{}});
                try {{ new WebSocket({json.dumps(ws_url)}); }} catch (_error) {{}}
                new Worker(URL.createObjectURL(new Blob([{json.dumps(worker_source)}], {{type:'text/javascript'}})));
                window.open({json.dumps(child.as_uri())}, 'child-popup');
                window.open({json.dumps(outside.as_uri())}, 'outside-popup');
                try {{
                  const peer = new RTCPeerConnection({{iceServers:[{{urls:'stun:127.0.0.1:{udp_socket.getsockname()[1]}'}}]}});
                  peer.createDataChannel('canary');
                  peer.createOffer().then((offer) => peer.setLocalDescription(offer)).catch(() => {{}});
                }} catch (_error) {{}}
                try {{ new WebTransport({json.dumps(webtransport_url)}); }} catch (_error) {{}}
                </script>""",
                encoding="utf-8",
            )

            report = self._audit(html)
            time.sleep(0.25)
        finally:
            http_server.shutdown()
            http_server.server_close()
            http_thread.join(timeout=2)
            udp_stop.set()
            udp_socket.close()
            udp_thread.join(timeout=2)
            socket_stop.set()
            socket_canary.close()
            socket_thread.join(timeout=2)

        self.assertEqual(http_hits, [])
        self.assertEqual(udp_hits, [])
        self.assertEqual(socket_hits, [])
        viewport = report["viewports"]["desktop"]
        reasons = {item["reason"] for item in viewport["blocked_requests"]}
        self.assertIn("network_blocked", reasons)
        self.assertIn("websocket_blocked", reasons)
        self.assertIn("file_outside_workspace", reasons)
        self.assertIn("direct_network_api_blocked", reasons)
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("/private?", serialized)
        self.assertNotIn("token=secret", serialized)
        self.assertFalse(report["passed"])

    def test_uniform_canvas_and_broken_media_fail_closed(self) -> None:
        uniform = self.workspace / "uniform.html"
        uniform.write_text(
            """<!doctype html><canvas id="blank" width="640" height="480"></canvas>
            <script>const c=document.querySelector('canvas').getContext('2d');c.fillStyle='#fff';c.fillRect(0,0,640,480)</script>""",
            encoding="utf-8",
        )
        uniform_report = self._audit(uniform, output_name="uniform-qa")
        uniform_view = uniform_report["viewports"]["desktop"]
        self.assertFalse(uniform_report["passed"])
        self.assertTrue(uniform_view["blank_render"])
        self.assertTrue(uniform_view["screenshot_analysis"]["near_uniform"])

        broken = self.workspace / "broken.html"
        broken.write_text(
            """<!doctype html><style>body{background:#eee}h1{color:#173c65}</style>
            <h1>Visible but broken media</h1><img id="broken" src="missing-image.png">""",
            encoding="utf-8",
        )
        broken_report = self._audit(broken, output_name="broken-qa")
        broken_view = broken_report["viewports"]["desktop"]
        self.assertFalse(broken_report["passed"])
        self.assertTrue(broken_view["media_errors"])
        self.assertFalse(broken_view["checks"]["media_complete"])

    def test_valid_lazy_local_media_is_loaded_before_readiness_gate(self) -> None:
        pixels = [(20, 70, 120)] * 200 + [(230, 170, 70)] * 200
        (self.workspace / "lazy.png").write_bytes(_png_fixture(20, 20, pixels))
        sample_rate = 8000
        audio_samples = b"\0\0" * 800
        wave = (
            struct.pack("<4sI4s", b"RIFF", 36 + len(audio_samples), b"WAVE")
            + struct.pack(
                "<4sIHHIIHH",
                b"fmt ",
                16,
                1,
                1,
                sample_rate,
                sample_rate * 2,
                2,
                16,
            )
            + struct.pack("<4sI", b"data", len(audio_samples))
            + audio_samples
        )
        encoded_audio = base64.b64encode(wave).decode("ascii")
        html = self.workspace / "lazy-media.html"
        html.write_text(
            f"""<!doctype html><style>
            body{{margin:0;background:linear-gradient(135deg,#f7f4ef,#ccd9e8);color:#172f49}}
            main{{min-height:1200px;padding:48px}}img{{width:120px;height:120px}}
            </style><main><h1>Lazy local media fixture</h1></main>
            <img loading="lazy" src="lazy.png" alt="valid lazy asset">
            <audio preload="none" src="data:audio/wav;base64,{encoded_audio}"></audio>""",
            encoding="utf-8",
        )

        report = self._audit(html)
        viewport = report["viewports"]["desktop"]

        self.assertTrue(report["passed"], viewport)
        self.assertEqual(viewport["media_errors"], [])
        self.assertTrue(viewport["checks"]["media_complete"])

    def test_diagnostic_geometry_and_clipping_gates_are_sanitized(self) -> None:
        html = self.workspace / "diagnostics.html"
        home = Path.home().resolve()
        html.write_text(
            f"""<!doctype html>
            <style>
              body {{ height:180px;overflow:hidden;background:linear-gradient(90deg,#fff,#ccd9e8); }}
              #outside {{ position:absolute;left:-80px;top:120px;color:#111 }}
              #clip {{ width:90px;height:24px;overflow:hidden;background:#fff }}
              #clip span {{ display:block;width:520px;white-space:nowrap }}
              #body-clipped {{ position:absolute;top:260px;left:20px }}
            </style>
            <h1>Diagnostics fixture</h1>
            <div id="outside">Off canvas text</div>
            <div id="clip"><span>Important clipped conference content</span></div>
            <div id="body-clipped">Content clipped by the document body</div>
            <script>
              console.error('failed https://example.com/private/path?token=top-secret file://{home}/private/token.txt');
              fetch('https://request.example/private/data?secret=request-secret').catch(() => {{}});
              setTimeout(() => {{ throw new Error('page https://page.example/private?token=page-secret file://{home}/private/page.txt') }}, 20);
            </script>""",
            encoding="utf-8",
        )
        report = self._audit(html)
        viewport = report["viewports"]["desktop"]
        self.assertFalse(report["passed"])
        self.assertTrue(viewport["console_errors"])
        self.assertTrue(viewport["page_errors"])
        self.assertTrue(viewport["request_errors"])
        self.assertTrue(viewport["geometry"]["out_of_canvas"])
        self.assertTrue(viewport["geometry"]["clipped_content"])
        self.assertTrue(
            any(
                item["selector"] == "div#body-clipped" and item["clipped_by"] == "body"
                for item in viewport["geometry"]["clipped_content"]
            )
        )
        for check in (
            "no_console_errors",
            "no_page_errors",
            "no_request_errors",
            "no_out_of_canvas",
            "no_clipped_content",
        ):
            self.assertFalse(viewport["checks"][check])
        serialized = json.dumps(report, sort_keys=True)
        for leaked in (
            "private/path",
            "top-secret",
            "private/data",
            "request-secret",
            "page-secret",
            str(home),
        ):
            self.assertNotIn(leaked, serialized)

    def test_screenshot_replaces_hardlink_atomically(self) -> None:
        html = self.workspace / "index.html"
        html.write_text(
            "<style>body{background:#eef}main{background:#163c64;color:white;padding:100px}</style><main>Painted content</main>",
            encoding="utf-8",
        )
        output = self.workspace / "qa"
        output.mkdir()
        outside = self.root / "outside.png"
        outside.write_bytes(b"outside-sentinel")
        target = output / "desktop.png"
        os.link(outside, target)

        report = self._audit(html)

        self.assertTrue(report["viewports"]["desktop"]["screenshot_analysis"]["painted_content"])
        self.assertEqual(outside.read_bytes(), b"outside-sentinel")
        self.assertFalse(os.path.samefile(outside, target))
        self.assertTrue(target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_animation_is_frozen_for_identical_geometry_and_screenshot_state(self) -> None:
        html = self.workspace / "animation.html"
        html.write_text(
            """<!doctype html><style>
            body{background:#f7f4ef}.band{height:100px;background:#173f67;color:white;width:120px}
            </style><div id="band" class="band">Stable geometry</div>
            <script>
            let wide=false;
            setInterval(()=>{wide=!wide;document.querySelector('#band').style.width=wide?'520px':'120px'},1);
            </script>""",
            encoding="utf-8",
        )

        report = self._audit(html)
        viewport = report["viewports"]["desktop"]

        self.assertTrue(viewport["dom_state_stable"])
        self.assertTrue(viewport["checks"]["dom_state_stable"])

    def test_legitimate_vertical_document_scroll_remains_valid(self) -> None:
        html = self.workspace / "vertical-scroll.html"
        html.write_text(
            """<!doctype html><style>
            body{margin:0;background:#f7f4ef;color:#172f49}
            section{box-sizing:border-box;min-height:360px;padding:72px;border-bottom:1px solid #aac}
            section:nth-child(2){background:#173f67;color:white}
            </style>
            <section><h1>Long-form research page</h1><p>Introduction and contribution.</p></section>
            <section><h2>Evidence</h2><p>Source-grounded findings remain visible.</p></section>
            <section><h2>Conclusion</h2><p>Normal document scrolling is intentional.</p></section>""",
            encoding="utf-8",
        )

        report = self._audit(html)
        viewport = report["viewports"]["desktop"]

        self.assertTrue(report["passed"])
        self.assertGreater(viewport["geometry"]["vertical_overflow"], 0)
        self.assertEqual(viewport["geometry"]["out_of_canvas"], [])
        self.assertEqual(viewport["geometry"]["clipped_content"], [])

    def test_clipping_after_five_hundred_text_nodes_is_not_silently_skipped(self) -> None:
        filler = "".join(f"<span>node {index} </span>" for index in range(500))
        html = self.workspace / "many-text-nodes.html"
        html.write_text(
            f"""<!doctype html><style>
            body{{background:#f7f4ef;color:#172f49}}
            #clip{{width:100px;height:24px;overflow:hidden;background:white}}
            #clip span{{display:block;width:700px;white-space:nowrap}}
            </style><main>{filler}</main>
            <div id="clip"><span id="late-clipped">Late critical content must be inspected</span></div>""",
            encoding="utf-8",
        )

        report = self._audit(html)
        viewport = report["viewports"]["desktop"]

        self.assertFalse(report["passed"])
        self.assertFalse(viewport["geometry"]["inspection_truncated"])
        self.assertTrue(
            any(
                item["selector"] == "span#late-clipped"
                for item in viewport["geometry"]["clipped_content"]
            )
        )


if __name__ == "__main__":
    unittest.main()
