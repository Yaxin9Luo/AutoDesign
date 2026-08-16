#!/usr/bin/env python3
"""Install and run the portable Skill browser in an isolated user cache."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence


RUNTIME_SCHEMA_VERSION = 1
PLAYWRIGHT_VERSION = "1.59.0"
_EXPECTED_PYTHON_PACKAGES = {
    "greenlet": "3.5.0",
    "playwright": PLAYWRIGHT_VERSION,
    "pyee": "13.0.1",
    "typing-extensions": "4.15.0",
}
_STATE_FILE = "runtime-state.json"
_DEFAULT_LOCK_TIMEOUT_SECONDS = 300.0
_STALE_LOCK_SECONDS = 1800.0
_SAFE_ENVIRONMENT_NAMES = {
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
}
_NETWORK_ENVIRONMENT_NAMES = {
    "ALL_PROXY",
    "CURL_CA_BUNDLE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "PIP_CERT",
    "PIP_INDEX_URL",
    "PIP_TRUSTED_HOST",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "all_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
}
_EXPECTED_STATE_KEYS = {
    "browser_executable_relative",
    "browser_executable_sha256",
    "cache_key",
    "format_version",
    "machine",
    "playwright_version",
    "python_major_minor",
    "requirements_sha256",
    "system",
    "venv_python_relative",
}


class BrowserRuntimeError(RuntimeError):
    """The pinned browser runtime is absent, corrupt, or unusable."""


@dataclass(frozen=True)
class RuntimeSpec:
    cache_root: Path
    cache_key: str
    cache_dir: Path
    requirements_path: Path
    requirements_sha256: str
    worker_path: Path
    system: str
    machine: str
    python_major_minor: str


@dataclass(frozen=True)
class BrowserRuntime:
    cache_dir: Path
    python_executable: Path
    browsers_path: Path
    browser_executable: Path
    state_path: Path


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-.")
    return normalized or "unknown"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_cache_root() -> Path:
    override = os.environ.get("AUTODESIGN_SKILL_BROWSER_CACHE", "").strip()
    if override:
        return Path(override).expanduser().absolute()
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return (base / "autodesign-skills" / "browser").absolute()


def venv_python_relative_path() -> Path:
    if os.name == "nt":
        return Path("venv") / "Scripts" / "python.exe"
    return Path("venv") / "bin" / "python"


def runtime_spec(
    *,
    cache_root: Path | None = None,
    requirements_path: Path | None = None,
    worker_path: Path | None = None,
) -> RuntimeSpec:
    root = (cache_root or default_cache_root()).expanduser().absolute()
    if root.is_symlink():
        raise BrowserRuntimeError(f"Browser cache root must not be a symlink: {root}")
    package = _package_root()
    if _is_within(root.resolve(strict=False), package):
        raise BrowserRuntimeError("Browser cache must be outside the installed Skill")
    requirements = (
        requirements_path or Path(__file__).resolve().with_name("requirements-browser.lock")
    ).resolve(strict=True)
    worker = (worker_path or Path(__file__).resolve().with_name("browser_worker.py")).resolve(
        strict=True
    )
    system = _slug(platform.system())
    machine = _slug(platform.machine())
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    key = (
        f"runtime-v{RUNTIME_SCHEMA_VERSION}-{system}-{machine}-"
        f"py{python}-playwright-{PLAYWRIGHT_VERSION}"
    )
    return RuntimeSpec(
        cache_root=root,
        cache_key=key,
        cache_dir=root / key,
        requirements_path=requirements,
        requirements_sha256=_sha256(requirements),
        worker_path=worker,
        system=system,
        machine=machine,
        python_major_minor=python,
    )


def isolated_environment(
    base: Mapping[str, str] | None = None,
    *,
    browsers_path: Path,
    allow_network_configuration: bool,
) -> dict[str, str]:
    """Return a minimal environment with no host Python, venv, or secret injection."""

    source = dict(os.environ if base is None else base)
    allowed = set(_SAFE_ENVIRONMENT_NAMES)
    if allow_network_configuration:
        allowed.update(_NETWORK_ENVIRONMENT_NAMES)
    result = {name: value for name, value in source.items() if name in allowed}
    result["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_path)
    result["PYTHONNOUSERSITE"] = "1"
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    result["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    result["PIP_NO_INPUT"] = "1"
    result["PIP_NO_CACHE_DIR"] = "1"
    return result


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _contained_existing_path(root: Path, relative: str, *, allow_symlink: bool) -> Path:
    candidate_relative = Path(relative)
    if candidate_relative.is_absolute() or ".." in candidate_relative.parts:
        raise BrowserRuntimeError(f"Unsafe runtime state path: {relative}")
    candidate = root / candidate_relative
    cursor = root
    for part in candidate_relative.parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise BrowserRuntimeError(
                f"Runtime path contains a symlinked parent: {candidate_relative}"
            )
    if not candidate.is_file():
        raise BrowserRuntimeError(f"Runtime file is missing: {candidate_relative.as_posix()}")
    if not allow_symlink and candidate.is_symlink():
        raise BrowserRuntimeError(f"Runtime file must not be a symlink: {candidate_relative}")
    if not allow_symlink and not _is_within(candidate.resolve(), root.resolve()):
        raise BrowserRuntimeError(f"Runtime file escapes cache: {candidate_relative}")
    return candidate


def write_runtime_state(staging: Path, spec: RuntimeSpec, browser_executable: Path) -> None:
    root = staging.resolve(strict=True)
    browser = browser_executable.resolve(strict=True)
    if not _is_within(browser, root):
        raise BrowserRuntimeError("Chromium executable is outside the runtime cache")
    browser_relative = browser.relative_to(root).as_posix()
    python_relative = venv_python_relative_path().as_posix()
    python = staging / python_relative
    if not python.is_file() or not os.access(python, os.X_OK):
        raise BrowserRuntimeError("Pinned virtual environment Python is missing")
    if not os.access(browser, os.X_OK):
        raise BrowserRuntimeError("Chromium executable is not executable")
    payload = {
        "format_version": RUNTIME_SCHEMA_VERSION,
        "cache_key": spec.cache_key,
        "system": spec.system,
        "machine": spec.machine,
        "python_major_minor": spec.python_major_minor,
        "playwright_version": PLAYWRIGHT_VERSION,
        "requirements_sha256": spec.requirements_sha256,
        "venv_python_relative": python_relative,
        "browser_executable_relative": browser_relative,
        "browser_executable_sha256": _sha256(browser),
    }
    _atomic_write_json(staging / _STATE_FILE, payload)


def inspect_browser_runtime(
    cache_dir: Path, spec: RuntimeSpec | None
) -> BrowserRuntime:
    cache = cache_dir.absolute()
    if cache.is_symlink():
        raise BrowserRuntimeError(f"Browser runtime cache must not be a symlink: {cache}")
    if not cache.is_dir():
        raise BrowserRuntimeError(f"Browser runtime cache is missing: {cache}")
    state_path = cache / _STATE_FILE
    if state_path.is_symlink() or not state_path.is_file():
        raise BrowserRuntimeError("Browser runtime state is missing")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BrowserRuntimeError("Browser runtime state is unreadable") from error
    if not isinstance(state, dict) or set(state) != _EXPECTED_STATE_KEYS:
        raise BrowserRuntimeError("Browser runtime state has an invalid schema")
    expected = {
        "format_version": RUNTIME_SCHEMA_VERSION,
        "cache_key": spec.cache_key if spec else cache.name,
        "system": spec.system if spec else _slug(platform.system()),
        "machine": spec.machine if spec else _slug(platform.machine()),
        "python_major_minor": spec.python_major_minor
        if spec
        else f"{sys.version_info.major}.{sys.version_info.minor}",
        "playwright_version": PLAYWRIGHT_VERSION,
    }
    for name, value in expected.items():
        if state.get(name) != value:
            raise BrowserRuntimeError(f"Browser runtime state mismatch: {name}")
    if spec and state.get("requirements_sha256") != spec.requirements_sha256:
        raise BrowserRuntimeError("Browser runtime dependency lock has changed")
    if not re.fullmatch(r"[0-9a-f]{64}", str(state.get("requirements_sha256", ""))):
        raise BrowserRuntimeError("Browser runtime dependency hash is invalid")

    python = _contained_existing_path(
        cache, str(state["venv_python_relative"]), allow_symlink=True
    )
    browser = _contained_existing_path(
        cache, str(state["browser_executable_relative"]), allow_symlink=False
    )
    if not os.access(python, os.X_OK) or not os.access(browser, os.X_OK):
        raise BrowserRuntimeError("Browser runtime executable permission is invalid")
    expected_browser_hash = str(state.get("browser_executable_sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", expected_browser_hash):
        raise BrowserRuntimeError("Chromium executable hash is invalid")
    if _sha256(browser) != expected_browser_hash:
        raise BrowserRuntimeError("Chromium executable hash mismatch")
    browsers_path = cache / "browsers"
    if browsers_path.is_symlink() or not browsers_path.is_dir():
        raise BrowserRuntimeError("Chromium browser directory is missing")
    return BrowserRuntime(cache, python, browsers_path, browser, state_path)


def _default_command_runner(
    command: Sequence[str], *, env: Mapping[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        env=dict(env),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _safe_process_detail(result: subprocess.CompletedProcess[str]) -> str:
    raw = (result.stderr or result.stdout or "no process output").strip()[-2000:]
    raw = re.sub(r"(?i)(authorization|cookie|set-cookie)\s*:\s*[^\r\n]+", r"\1: [REDACTED]", raw)
    raw = re.sub(r"(?i)\b(?:https?|socks5?)://[^\s/@]+@", "[redacted-url]://", raw)
    raw = re.sub(r"(?i)\b(?:sk-|api[_-]?key[=:])[A-Za-z0-9._-]{12,}", "[REDACTED]", raw)
    return raw


def _run_checked(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: int,
    action: str,
    command_runner: CommandRunner = _default_command_runner,
) -> subprocess.CompletedProcess[str]:
    try:
        result = command_runner(list(command), env=dict(env), timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        raise BrowserRuntimeError(f"{action} failed to start: {error}") from error
    if result.returncode != 0:
        detail = _safe_process_detail(result)
        guidance = ""
        if platform.system().lower() == "linux":
            guidance = (
                " Do not run dependency installation as root from this Skill. "
                "Ask an administrator to run `playwright install-deps chromium` "
                "for the pinned runtime if Linux libraries are missing."
            )
        raise BrowserRuntimeError(f"{action} failed (exit {result.returncode}): {detail}.{guidance}")
    return result


def _probe_runtime(runtime: BrowserRuntime, spec: RuntimeSpec) -> None:
    with tempfile.TemporaryDirectory(prefix="autodesign-browser-probe-") as temporary:
        report = Path(temporary) / "probe.json"
        env = isolated_environment(
            browsers_path=runtime.browsers_path,
            allow_network_configuration=False,
        )
        _run_checked(
            [
                str(runtime.python_executable),
                "-I",
                str(spec.worker_path),
                "probe",
                "--report",
                str(report),
            ],
            env=env,
            timeout=60,
            action="fresh Chromium launch probe",
        )
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise BrowserRuntimeError("Chromium launch probe did not produce a valid report") from error
        if payload.get("passed") is not True:
            raise BrowserRuntimeError("Chromium launch probe did not pass")
        if payload.get("python_packages") != _EXPECTED_PYTHON_PACKAGES:
            raise BrowserRuntimeError("Pinned Playwright Python package versions do not match")
        reported = Path(str(payload.get("browser_executable", ""))).resolve(strict=True)
        if reported != runtime.browser_executable.resolve(strict=True):
            raise BrowserRuntimeError("Chromium launch probe used an unexpected executable")
        if payload.get("browser_executable_sha256") != _sha256(runtime.browser_executable):
            raise BrowserRuntimeError("Chromium launch probe reported an unexpected binary hash")


def _install_runtime(staging: Path, spec: RuntimeSpec) -> None:
    browsers = staging / "browsers"
    browsers.mkdir(parents=True)
    install_env = isolated_environment(
        browsers_path=browsers,
        allow_network_configuration=True,
    )
    _run_checked(
        [sys.executable, "-I", "-m", "venv", str(staging / "venv")],
        env=install_env,
        timeout=180,
        action="isolated browser virtual environment creation",
    )
    python = staging / venv_python_relative_path()
    _run_checked(
        [
            str(python),
            "-I",
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "--no-deps",
            "--only-binary=:all:",
            "--requirement",
            str(spec.requirements_path),
        ],
        env=install_env,
        timeout=600,
        action="hash-locked Playwright dependency installation",
    )
    _run_checked(
        [str(python), "-I", "-m", "playwright", "install", "chromium"],
        env=install_env,
        timeout=900,
        action="pinned Chromium installation",
    )
    probe_report = staging / ".install-probe.json"
    probe_env = isolated_environment(
        browsers_path=browsers,
        allow_network_configuration=False,
    )
    _run_checked(
        [str(python), "-I", str(spec.worker_path), "probe", "--report", str(probe_report)],
        env=probe_env,
        timeout=60,
        action="pre-promotion Chromium launch probe",
    )
    try:
        probe = json.loads(probe_report.read_text(encoding="utf-8"))
        if probe.get("passed") is not True:
            raise BrowserRuntimeError("Pre-promotion Chromium launch probe did not pass")
        if probe.get("python_packages") != _EXPECTED_PYTHON_PACKAGES:
            raise BrowserRuntimeError("Installed Playwright Python package versions do not match")
        browser = Path(str(probe["browser_executable"]))
        if probe.get("browser_executable_sha256") != _sha256(browser):
            raise BrowserRuntimeError("Pre-promotion Chromium binary hash is inconsistent")
        write_runtime_state(staging, spec, browser)
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BrowserRuntimeError("Pre-promotion Chromium probe report is invalid") from error
    finally:
        probe_report.unlink(missing_ok=True)


@contextlib.contextmanager
def _cache_lock(lock: Path, timeout_seconds: float) -> Iterator[None]:
    deadline = time.monotonic() + timeout_seconds
    lock.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    while not acquired:
        try:
            lock.mkdir(mode=0o700)
            (lock / "owner.json").write_text(
                json.dumps({"pid": os.getpid(), "created_epoch": time.time()}),
                encoding="utf-8",
            )
            acquired = True
        except FileExistsError:
            if lock.is_symlink() or not lock.is_dir():
                raise BrowserRuntimeError(f"Unsafe browser runtime lock: {lock}")
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                age = 0
            if age > _STALE_LOCK_SECONDS:
                stale = lock.with_name(f".{lock.name}.stale-{uuid.uuid4().hex}")
                try:
                    os.replace(lock, stale)
                    shutil.rmtree(stale, ignore_errors=True)
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise BrowserRuntimeError("Timed out waiting for browser runtime installation lock")
            time.sleep(0.05)
    try:
        yield
    finally:
        if acquired:
            shutil.rmtree(lock, ignore_errors=True)


def _discard_corrupt_cache(cache: Path) -> None:
    if not cache.exists() and not cache.is_symlink():
        return
    quarantine = cache.with_name(f".{cache.name}.corrupt-{uuid.uuid4().hex}")
    os.replace(cache, quarantine)
    if quarantine.is_symlink() or quarantine.is_file():
        quarantine.unlink(missing_ok=True)
    else:
        shutil.rmtree(quarantine)


def ensure_browser_runtime(
    *,
    cache_root: Path | None = None,
    requirements_path: Path | None = None,
    worker_path: Path | None = None,
    allow_install: bool = True,
    lock_timeout_seconds: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> BrowserRuntime:
    """Return a verified runtime, atomically installing it when permitted."""

    spec = runtime_spec(
        cache_root=cache_root,
        requirements_path=requirements_path,
        worker_path=worker_path,
    )
    try:
        runtime = inspect_browser_runtime(spec.cache_dir, spec)
    except BrowserRuntimeError as initial_error:
        if not allow_install:
            raise BrowserRuntimeError(
                f"Verified browser runtime is unavailable for offline reuse: {initial_error}"
            ) from initial_error
    else:
        _probe_runtime(runtime, spec)
        return runtime

    spec.cache_root.mkdir(parents=True, exist_ok=True)
    lock = spec.cache_root / f".{spec.cache_key}.lock"
    with _cache_lock(lock, lock_timeout_seconds):
        try:
            runtime = inspect_browser_runtime(spec.cache_dir, spec)
        except BrowserRuntimeError:
            _discard_corrupt_cache(spec.cache_dir)
        else:
            _probe_runtime(runtime, spec)
            return runtime

        staging = spec.cache_root / f".{spec.cache_key}.installing-{uuid.uuid4().hex}"
        staging.mkdir(mode=0o700)
        try:
            _install_runtime(staging, spec)
            inspect_browser_runtime(staging, spec)
            os.replace(staging, spec.cache_dir)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        runtime = inspect_browser_runtime(spec.cache_dir, spec)
        _probe_runtime(runtime, spec)
        return runtime


def audit_local_html(
    html_path: Path,
    *,
    workspace_root: Path,
    output_dir: Path,
    viewports: Sequence[str] = ("desktop:1440x900",),
    runtime: BrowserRuntime | None = None,
    cache_root: Path | None = None,
    allow_install: bool = True,
    command_runner: CommandRunner = _default_command_runner,
    timeout_seconds: int = 120,
) -> dict[str, object]:
    """Audit local HTML with the pinned worker and return its JSON report."""

    workspace = workspace_root.expanduser().resolve(strict=True)
    html = html_path.expanduser().resolve(strict=True)
    output = output_dir.expanduser().resolve(strict=False)
    package = _package_root()
    if not workspace.is_dir() or _is_within(workspace, package):
        raise BrowserRuntimeError("Browser audit workspace must be outside the installed Skill")
    if not html.is_file() or not _is_within(html, workspace):
        raise BrowserRuntimeError("Browser audit HTML must be inside the workspace")
    if not _is_within(output, workspace) or _is_within(output, package):
        raise BrowserRuntimeError("Browser audit output must be inside the external workspace")
    if output.is_symlink():
        raise BrowserRuntimeError("Browser audit output must not be a symlink")

    spec = runtime_spec(cache_root=cache_root)
    active = runtime
    if active is None:
        active = ensure_browser_runtime(cache_root=cache_root, allow_install=allow_install)
    else:
        active = inspect_browser_runtime(active.cache_dir, spec)
        _probe_runtime(active, spec)
    report_path = output / "audit.json"
    command = [
        str(active.python_executable),
        "-I",
        str(spec.worker_path),
        "audit",
        "--workspace-root",
        str(workspace),
        "--html",
        str(html),
        "--output-dir",
        str(output),
        "--report",
        str(report_path),
    ]
    for viewport in viewports:
        command.extend(["--viewport", viewport])
    env = isolated_environment(
        browsers_path=active.browsers_path,
        allow_network_configuration=False,
    )
    try:
        result = command_runner(command, env=env, timeout=timeout_seconds)
    except (OSError, subprocess.SubprocessError) as error:
        raise BrowserRuntimeError(f"Browser audit failed to start: {error}") from error
    if result.returncode not in (0, 2):
        raise BrowserRuntimeError(
            f"Browser audit crashed (exit {result.returncode}): {_safe_process_detail(result)}"
        )
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BrowserRuntimeError("Browser audit did not produce a valid report") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
        raise BrowserRuntimeError("Browser audit report has an invalid schema")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        runtime = ensure_browser_runtime(
            cache_root=args.cache_root,
            allow_install=not args.offline,
        )
    except BrowserRuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "ready", "cache_dir": str(runtime.cache_dir)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
