#!/usr/bin/env python3
"""Install the exact-pinned editable PPTX runtime into a versioned user cache."""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG
from typing import Any, Iterator, Mapping, Sequence


sys.dont_write_bytecode = True

RUNTIME_FORMAT_VERSION = 2
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
_THREAD_GUARDS_LOCK = threading.Lock()
_THREAD_GUARDS: dict[str, tuple[threading.Lock, int]] = {}


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


def _package_lock_path() -> Path:
    path = Path(__file__).resolve().with_name("requirements-ppt.lock")
    if path.is_symlink() or not path.is_file():
        raise PptRuntimeError("PPT dependency hash lock is missing or unsafe")
    status = path.lstat()
    if not S_ISREG(status.st_mode) or status.st_nlink > 1:
        raise PptRuntimeError("PPT dependency hash lock is missing or unsafe")
    return path


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
    lock_hash = hashlib.sha256(_package_lock_path().read_bytes()).hexdigest()
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
def _metadata_runtime_lock(path: Path, deadline: float) -> Iterator[None]:
    token = uuid.uuid4().hex
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


def _thread_guard_for(path: Path) -> threading.Lock:
    key = str(path.absolute())
    with _THREAD_GUARDS_LOCK:
        entry = _THREAD_GUARDS.get(key)
        if entry is None:
            guard = threading.Lock()
            _THREAD_GUARDS[key] = (guard, 1)
            return guard
        guard, references = entry
        _THREAD_GUARDS[key] = (guard, references + 1)
        return guard


def _release_thread_guard(path: Path, guard: threading.Lock) -> None:
    key = str(path.absolute())
    with _THREAD_GUARDS_LOCK:
        entry = _THREAD_GUARDS.get(key)
        if entry is None or entry[0] is not guard:
            return
        if entry[1] <= 1:
            _THREAD_GUARDS.pop(key, None)
        else:
            _THREAD_GUARDS[key] = (guard, entry[1] - 1)


def _open_guard_file(path: Path) -> int:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PptRuntimeError(f"Unsafe PPT runtime advisory lock: {path}")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise PptRuntimeError(
            f"Could not open PPT runtime advisory lock: {path}: {error}"
        ) from error
    try:
        opened = os.fstat(descriptor)
        published = path.lstat()
        if (
            not S_ISREG(opened.st_mode)
            or not S_ISREG(published.st_mode)
            or opened.st_nlink > 1
            or (opened.st_dev, opened.st_ino)
            != (published.st_dev, published.st_ino)
        ):
            raise PptRuntimeError(f"Unsafe PPT runtime advisory lock: {path}")
        if opened.st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _try_advisory_lock(descriptor: int) -> bool:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


def _unlock_advisory_lock(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextlib.contextmanager
def _advisory_runtime_guard(path: Path, deadline: float) -> Iterator[None]:
    thread_guard = _thread_guard_for(path)
    remaining = max(0.0, deadline - time.monotonic())
    if not thread_guard.acquire(timeout=remaining):
        _release_thread_guard(path, thread_guard)
        raise PptRuntimeError("Timed out waiting for PPT runtime installation lock")
    descriptor: int | None = None
    acquired = False
    try:
        descriptor = _open_guard_file(path)
        while not acquired:
            try:
                acquired = _try_advisory_lock(descriptor)
            except OSError as error:
                raise PptRuntimeError(
                    f"Could not acquire PPT runtime advisory lock: {error}"
                ) from error
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise PptRuntimeError(
                    "Timed out waiting for PPT runtime installation lock"
                )
            time.sleep(0.05)
        yield
    finally:
        if descriptor is not None:
            if acquired:
                with contextlib.suppress(OSError):
                    _unlock_advisory_lock(descriptor)
            os.close(descriptor)
        thread_guard.release()
        _release_thread_guard(path, thread_guard)


@contextlib.contextmanager
def _runtime_lock(path: Path, timeout_seconds: float) -> Iterator[None]:
    deadline = time.monotonic() + timeout_seconds
    path.parent.mkdir(parents=True, exist_ok=True)
    guard = path.with_name(f"{path.name}.guard")
    with _advisory_runtime_guard(guard, deadline):
        with _metadata_runtime_lock(path, deadline):
            yield


def _runtime_tree_sha256(cache_dir: Path | str) -> str:
    cache = Path(cache_dir).absolute()
    for current, directories, filenames in os.walk(cache, followlinks=False):
        current_path = Path(current)
        if current_path.name == "__pycache__" or "__pycache__" in directories:
            raise PptRuntimeError("PPT runtime contains a forbidden __pycache__ directory")
        if any(name.lower().endswith((".pyc", ".pyo")) for name in filenames):
            raise PptRuntimeError("PPT runtime contains forbidden executable bytecode")
    site = (
        cache / "venv" / "Lib" / "site-packages"
        if os.name == "nt"
        else cache
        / "venv"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    if site.is_symlink() or not site.is_dir():
        raise PptRuntimeError("PPT runtime site-packages directory is missing or unsafe")
    digest = hashlib.sha256()
    file_count = 0
    for current, directories, filenames in os.walk(site, followlinks=False):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in sorted(directories):
            child = current_path / name
            if child.is_symlink():
                raise PptRuntimeError("PPT runtime content tree contains a symlink")
            kept_directories.append(name)
        directories[:] = kept_directories
        for name in sorted(filenames):
            path = current_path / name
            status = path.lstat()
            if path.is_symlink() or not S_ISREG(status.st_mode) or status.st_nlink > 1:
                raise PptRuntimeError("PPT runtime content tree contains an unsafe file")
            relative = path.relative_to(site).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            file_count += 1
    if file_count == 0:
        raise PptRuntimeError("PPT runtime content tree is empty")
    return digest.hexdigest()


def _state_payload(
    spec: PptRuntimeSpec, runtime_tree_sha256: str
) -> dict[str, Any]:
    return {
        "format_version": RUNTIME_FORMAT_VERSION,
        "cache_key": spec.cache_key,
        "python_major_minor": spec.python_major_minor,
        "system": spec.system,
        "machine": spec.machine,
        "package_lock_sha256": spec.package_lock_sha256,
        "packages": dict(PINNED_PACKAGES),
        "runtime_tree_sha256": runtime_tree_sha256,
        "python_relative": _venv_python(Path(".")).as_posix().removeprefix("./"),
    }


def _probe(runtime: PptRuntime, spec: PptRuntimeSpec) -> None:
    script = (
        "import importlib.metadata,json,pptx; "
        "names=" + repr(tuple(PINNED_PACKAGES)) + "; "
        "print(json.dumps({n:importlib.metadata.version(n) for n in names},sort_keys=True))"
    )
    result = subprocess.run(
        [str(runtime.python_executable), "-B", "-I", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
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
    digest = state.get("runtime_tree_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise PptRuntimeError("PPT runtime state has no valid content tree hash")
    expected = _state_payload(
        spec or runtime_spec(cache_root=cache.parent), digest
    )
    if state != expected:
        raise PptRuntimeError("PPT runtime state does not match the pinned contract")
    if _runtime_tree_sha256(cache) != digest:
        raise PptRuntimeError("PPT runtime content tree hash changed or was tampered")
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
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
    )
    if result.returncode != 0:
        raise PptRuntimeError("Could not create the isolated PPT runtime")
    python = _venv_python(staging)
    result = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-compile",
            "--only-binary=:all:",
            "--require-hashes",
            "--no-deps",
            "--requirement",
            str(_package_lock_path()),
        ],
        capture_output=True,
        text=True,
        timeout=_INSTALL_TIMEOUT_SECONDS,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no package-manager output").strip()[-1200:]
        detail = re.sub(r"(?i)(authorization|cookie|set-cookie)\s*:\s*[^\r\n]+", r"\1: [REDACTED]", detail)
        raise PptRuntimeError(f"Exact-pinned PPT dependency installation failed: {detail}")
    for path in sorted((staging / "venv").rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        if path.is_file() and path.suffix.lower() in {".pyc", ".pyo"}:
            path.unlink()
        elif path.is_dir() and path.name == "__pycache__":
            shutil.rmtree(path)
    _atomic_json(
        staging / _STATE_FILE,
        _state_payload(spec, _runtime_tree_sha256(staging)),
    )


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
