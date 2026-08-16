#!/usr/bin/env python3
"""Install the exact-pinned editable PPTX runtime into a versioned user cache."""

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
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


RUNTIME_FORMAT_VERSION = 1
PINNED_PACKAGES = {
    "python-pptx": "1.0.2",
    "lxml": "6.1.0",
    "Pillow": "12.3.0",
    "XlsxWriter": "3.2.9",
    "typing-extensions": "4.15.0",
}
_INSTALL_TIMEOUT_SECONDS = 900
_LOCK_TIMEOUT_SECONDS = 960
_STATE_FILE = "runtime.json"


class PptRuntimeError(RuntimeError):
    """The pinned editable-PPT runtime is unavailable or corrupt."""


@dataclass(frozen=True)
class PptRuntimeSpec:
    cache_key: str
    cache_dir: Path
    python_major_minor: str
    system: str
    machine: str
    package_lock_sha256: str


@dataclass(frozen=True)
class PptRuntime:
    cache_dir: Path
    python_executable: Path
    state_path: Path


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "unknown"


def _package_lock_text() -> str:
    return "\n".join(f"{name}=={version}" for name, version in sorted(PINNED_PACKAGES.items())) + "\n"


def _default_cache_root() -> Path:
    override = os.environ.get("AUTODESIGN_SKILL_PPT_CACHE", "").strip()
    if override:
        return Path(override).expanduser().absolute()
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return (base / "autodesign-skills" / "ppt").absolute()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def runtime_spec(*, cache_root: Path | str | None = None) -> PptRuntimeSpec:
    root = Path(cache_root).expanduser().absolute() if cache_root is not None else _default_cache_root()
    package = Path(__file__).resolve().parent.parent
    if root.is_symlink() or _is_within(root.resolve(strict=False), package):
        raise PptRuntimeError("PPT runtime cache must be outside the installed Skill")
    system = _slug(platform.system())
    machine = _slug(platform.machine())
    python_minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    lock_hash = hashlib.sha256(_package_lock_text().encode("utf-8")).hexdigest()
    key = (
        f"runtime-v{RUNTIME_FORMAT_VERSION}-{system}-{machine}-py{python_minor}-"
        f"python-pptx-{PINNED_PACKAGES['python-pptx']}-{lock_hash[:12]}"
    )
    return PptRuntimeSpec(key, root / key, python_minor, system, machine, lock_hash)


def _venv_python(root: Path) -> Path:
    return root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(value), handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return True
        return result.returncode == 0 and str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextlib.contextmanager
def _runtime_lock(path: Path, timeout_seconds: float):
    token = uuid.uuid4().hex
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            path.mkdir(parents=False)
            _atomic_json(
                path / "owner.json",
                {"pid": os.getpid(), "token": token, "created_epoch": time.time()},
            )
            break
        except FileExistsError:
            owner: dict[str, Any] = {}
            try:
                owner = json.loads((path / "owner.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
            pid = owner.get("pid")
            if isinstance(pid, int) and not _process_alive(pid):
                quarantine = path.with_name(f"{path.name}.dead-{uuid.uuid4().hex}")
                try:
                    os.replace(path, quarantine)
                except OSError:
                    pass
                else:
                    shutil.rmtree(quarantine, ignore_errors=True)
                    continue
            if time.monotonic() >= deadline:
                raise PptRuntimeError("Timed out waiting for another PPT runtime installation")
            time.sleep(0.1)
    try:
        yield
    finally:
        try:
            owner = json.loads((path / "owner.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            owner = {}
        if owner.get("pid") == os.getpid() and owner.get("token") == token:
            shutil.rmtree(path, ignore_errors=True)


def _state_payload(spec: PptRuntimeSpec) -> dict[str, Any]:
    return {
        "format_version": RUNTIME_FORMAT_VERSION,
        "cache_key": spec.cache_key,
        "python_major_minor": spec.python_major_minor,
        "system": spec.system,
        "machine": spec.machine,
        "package_lock_sha256": spec.package_lock_sha256,
        "packages": dict(PINNED_PACKAGES),
        "python_relative": _venv_python(Path(".")).as_posix().removeprefix("./"),
    }


def _probe(runtime: PptRuntime, spec: PptRuntimeSpec) -> None:
    script = (
        "import importlib.metadata,json,pptx; "
        "names=" + repr(tuple(PINNED_PACKAGES)) + "; "
        "print(json.dumps({n:importlib.metadata.version(n) for n in names},sort_keys=True))"
    )
    result = subprocess.run(
        [str(runtime.python_executable), "-I", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise PptRuntimeError("Pinned PPT runtime import probe failed")
    try:
        versions = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise PptRuntimeError("Pinned PPT runtime import probe was unreadable") from error
    if versions != PINNED_PACKAGES:
        raise PptRuntimeError("Pinned PPT runtime package versions do not match")


def inspect_runtime(cache_dir: Path | str, spec: PptRuntimeSpec | None = None) -> PptRuntime:
    cache = Path(cache_dir).absolute()
    if cache.is_symlink() or not cache.is_dir():
        raise PptRuntimeError("PPT runtime cache is missing or unsafe")
    state_path = cache / _STATE_FILE
    if state_path.is_symlink() or not state_path.is_file():
        raise PptRuntimeError("PPT runtime state is missing")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PptRuntimeError("PPT runtime state is unreadable") from error
    expected = _state_payload(spec or runtime_spec(cache_root=cache.parent))
    if state != expected:
        raise PptRuntimeError("PPT runtime state does not match the pinned contract")
    python = cache / state["python_relative"]
    if not python.exists() or not os.access(python, os.X_OK):
        raise PptRuntimeError("PPT runtime Python executable is missing")
    runtime = PptRuntime(cache, python, state_path)
    _probe(runtime, spec or runtime_spec(cache_root=cache.parent))
    return runtime


def _install(staging: Path, spec: PptRuntimeSpec) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(staging / "venv")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise PptRuntimeError("Could not create the isolated PPT runtime")
    python = _venv_python(staging)
    requirements = [f"{name}=={version}" for name, version in PINNED_PACKAGES.items()]
    result = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--only-binary=:all:",
            *requirements,
        ],
        capture_output=True,
        text=True,
        timeout=_INSTALL_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no package-manager output").strip()[-1200:]
        detail = re.sub(r"(?i)(authorization|cookie|set-cookie)\s*:\s*[^\r\n]+", r"\1: [REDACTED]", detail)
        raise PptRuntimeError(f"Exact-pinned PPT dependency installation failed: {detail}")
    _atomic_json(staging / _STATE_FILE, _state_payload(spec))


def ensure_ppt_runtime(
    *, cache_root: Path | str | None = None, allow_install: bool = True
) -> PptRuntime:
    spec = runtime_spec(cache_root=cache_root)
    try:
        return inspect_runtime(spec.cache_dir, spec)
    except PptRuntimeError:
        if not allow_install:
            raise PptRuntimeError(
                "Pinned PPT runtime is not installed; run setup_ppt.py once while online"
            )
    spec.cache_dir.parent.mkdir(parents=True, exist_ok=True)
    lock = spec.cache_dir.with_name(f"{spec.cache_dir.name}.lock")
    with _runtime_lock(lock, _LOCK_TIMEOUT_SECONDS):
        try:
            return inspect_runtime(spec.cache_dir, spec)
        except PptRuntimeError:
            if spec.cache_dir.exists():
                quarantine = spec.cache_dir.with_name(f"{spec.cache_dir.name}.corrupt-{uuid.uuid4().hex}")
                os.replace(spec.cache_dir, quarantine)
                shutil.rmtree(quarantine, ignore_errors=True)
        for stale in spec.cache_dir.parent.glob(f".{spec.cache_dir.name}.installing-*"):
            if stale.is_dir() and not stale.is_symlink():
                shutil.rmtree(stale, ignore_errors=True)
        staging = spec.cache_dir.parent / f".{spec.cache_dir.name}.installing-{uuid.uuid4().hex}"
        staging.mkdir()
        try:
            _install(staging, spec)
            runtime = inspect_runtime(staging, spec)
            os.replace(staging, spec.cache_dir)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return inspect_runtime(spec.cache_dir, spec)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        runtime = ensure_ppt_runtime(cache_root=args.cache_root, allow_install=not args.offline)
    except PptRuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "ready", "cache_dir": str(runtime.cache_dir)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
