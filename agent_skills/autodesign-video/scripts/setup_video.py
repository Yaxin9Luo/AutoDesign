#!/usr/bin/env python3
"""Install HyperFrames and Kokoro support into a versioned user cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence


sys.dont_write_bytecode = True


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import setup_browser  # noqa: E402


HYPERFRAMES_VERSION = "0.7.86"
KOKORO_ONNX_VERSION = "0.5.0"
SOUNDFILE_VERSION = "0.14.0"
PDF_INGEST_TOOLS = ("pdftotext", "pdfinfo", "pdftoppm", "pdfimages")
PYTHON_LOCK_PATH = SKILL_ROOT / "assets" / "video-runtime" / "requirements-kokoro.lock"
PYTHON_LOCK_SHA256 = "f4ecd858f55479aa689578d66cae8e9e7d9568827b6292a016aba54a35b197b3"
MIN_TTS_PYTHON = (3, 10)
MAX_TTS_PYTHON = (3, 12)
KOKORO_MODEL_SHA256 = (
    "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"
)
KOKORO_VOICES_SHA256 = (
    "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"
)
KOKORO_MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.onnx"
)
KOKORO_VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/voices-v1.0.bin"
)
RUNTIME_FORMAT_VERSION = 1
_INSTALL_TIMEOUT_SECONDS = 1800
_SAFE_ENV_NAMES = {
    "COMSPEC", "HOME", "HOMEDRIVE", "HOMEPATH", "LANG", "LC_ALL", "PATH",
    "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USERPROFILE", "WINDIR",
}
_NETWORK_ENV_NAMES = {
    "ALL_PROXY", "CURL_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
    "NODE_EXTRA_CA_CERTS", "NPM_CONFIG_CAFILE", "PIP_CERT", "PIP_INDEX_URL",
    "PIP_TRUSTED_HOST", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "all_proxy",
    "https_proxy", "http_proxy", "no_proxy",
}
_STATE_KEYS = {
    "browser_relative",
    "browser_sha256",
    "browser_ensured",
    "cache_key",
    "ffmpeg_binary",
    "ffprobe_binary",
    "format_version",
    "home_relative",
    "hyperframes_relative",
    "hyperframes_version",
    "kokoro_model_relative",
    "kokoro_model_sha256",
    "kokoro_onnx_version",
    "kokoro_voices_relative",
    "kokoro_voices_sha256",
    "machine",
    "node_binary",
    "node_major",
    "package_lock_sha256",
    "package_sha256",
    "python_lock_sha256",
    "python_major_minor",
    "python_relative",
    "soundfile_version",
    "system",
    "tts_smoke_relative",
    "tts_smoke_sha256",
}
_SUPPORTED_TARGETS = {
    ("darwin", "arm64"),
    ("darwin", "x86_64"),
    ("linux", "aarch64"),
    ("linux", "x86_64"),
    ("windows", "amd64"),
    ("windows", "x86_64"),
}


class VideoRuntimeError(RuntimeError):
    """The exact standalone video runtime is unavailable or corrupt."""


@dataclass(frozen=True)
class VideoRuntimeSpec:
    cache_root: Path
    cache_key: str
    cache_dir: Path
    package_json: Path
    package_lock: Path
    python_lock: Path
    package_sha256: str
    package_lock_sha256: str
    python_lock_sha256: str
    system: str
    machine: str
    python_major_minor: str
    python_binary: Path | None
    node_binary: Path | None
    npm_binary: Path | None
    node_major: int | None
    ffmpeg_binary: Path | None
    ffprobe_binary: Path | None


@dataclass(frozen=True)
class VideoRuntime:
    cache_dir: Path
    home_dir: Path
    hyperframes_executable: Path
    python_executable: Path
    node_binary: Path
    browser_executable: Path
    ffmpeg_binary: Path
    ffprobe_binary: Path
    state_path: Path
    hyperframes_version: str = HYPERFRAMES_VERSION

    def as_dict(self) -> dict[str, str]:
        return {
            "status": "ready",
            "cache_dir": str(self.cache_dir),
            "home_dir": str(self.home_dir),
            "hyperframes": str(self.hyperframes_executable),
            "python": str(self.python_executable),
            "node": str(self.node_binary),
            "node_root": str(self.hyperframes_executable.parents[2]),
            "browser": str(self.browser_executable),
            "ffmpeg": str(self.ffmpeg_binary),
            "ffprobe": str(self.ffprobe_binary),
            "hyperframes_version": self.hyperframes_version,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-.") or "unknown"


def _node_version(binary: Path | None) -> tuple[str, int | None]:
    if binary is None:
        return "", None
    try:
        completed = subprocess.run(
            [str(binary), "--version"], text=True, capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", None
    text = ((completed.stdout or "") + (completed.stderr or "")).strip()
    match = re.search(r"v?(\d+)", text)
    return text.splitlines()[0] if text else "", int(match.group(1)) if match else None


def _python_version(binary: Path | None) -> tuple[str, tuple[int, int] | None]:
    if binary is None:
        return "", None
    try:
        completed = subprocess.run(
            [str(binary), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", None
    text = (completed.stdout or "").strip()
    match = re.fullmatch(r"(\d+)\.(\d+)", text)
    return text, (int(match.group(1)), int(match.group(2))) if match else None


def _select_tts_python() -> tuple[Path | None, str]:
    configured = os.environ.get("AUTODESIGN_VIDEO_PYTHON", "").strip()
    candidates: list[str] = [configured] if configured else [
        sys.executable,
        "python3.12",
        "python3.11",
        "python3.10",
    ]
    if not configured and shutil.which("uv"):
        for version in ("3.12", "3.11", "3.10"):
            try:
                found = subprocess.run(
                    [str(shutil.which("uv")), "python", "find", version],
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if found.returncode == 0 and found.stdout.strip():
                candidates.append(found.stdout.strip().splitlines()[0])
    seen: set[str] = set()
    for candidate in candidates:
        resolved = shutil.which(candidate) if not Path(candidate).is_absolute() else candidate
        if not resolved:
            continue
        binary = Path(resolved).resolve()
        if str(binary) in seen:
            continue
        seen.add(str(binary))
        version_text, version = _python_version(binary)
        if version is not None and MIN_TTS_PYTHON <= version <= MAX_TTS_PYTHON:
            return binary, version_text
    return None, "missing"


def default_cache_root() -> Path:
    override = os.environ.get("AUTODESIGN_SKILL_VIDEO_CACHE", "").strip()
    if override:
        return Path(override).expanduser().absolute()
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return (base / "autodesign-skills" / "video").absolute()


def _venv_python_relative() -> Path:
    if os.name == "nt":
        return Path("p") / "Scripts" / "python.exe"
    return Path("p") / "bin" / "python"


def runtime_spec(*, cache_root: Path | None = None) -> VideoRuntimeSpec:
    root = (cache_root or default_cache_root()).expanduser().absolute()
    if root.is_symlink():
        raise VideoRuntimeError(f"Video cache root must not be a symlink: {root}")
    if _is_within(root.resolve(strict=False), SKILL_ROOT.resolve()):
        raise VideoRuntimeError("Video cache must be outside the installed Skill")
    package_root = SKILL_ROOT / "assets" / "video-runtime"
    package_json = (package_root / "package.json").resolve(strict=True)
    package_lock = (package_root / "package-lock.json").resolve(strict=True)
    python_lock = PYTHON_LOCK_PATH.resolve(strict=True)
    observed_python_lock_sha256 = _sha256(python_lock)
    if observed_python_lock_sha256 != PYTHON_LOCK_SHA256:
        raise VideoRuntimeError("Python lock checksum does not match this Skill release")
    node = shutil.which("node")
    npm = shutil.which("npm")
    _, node_major = _node_version(Path(node) if node else None)
    system = _slug(platform.system())
    machine = _slug(platform.machine())
    if (system, machine) not in _SUPPORTED_TARGETS:
        raise VideoRuntimeError(
            f"unsupported video runtime platform: {system}/{machine}"
        )
    python_binary, python = _select_tts_python()
    identity = (
        f"v{RUNTIME_FORMAT_VERSION}|{system}|{machine}|py{python}|"
        f"node{node_major or 'missing'}|hyperframes{HYPERFRAMES_VERSION}|"
        f"python-lock={observed_python_lock_sha256}"
    )
    key = (
        f"v{RUNTIME_FORMAT_VERSION}-hf0786-"
        f"{observed_python_lock_sha256[:12]}-{hashlib.sha256(identity.encode()).hexdigest()[:4]}"
    )
    cache_dir = root / key
    if system == "darwin" and python != "missing":
        espeak_data_path = (
            cache_dir
            / "p"
            / "lib"
            / f"python{python}"
            / "site-packages"
            / "espeakng_loader"
            / "espeak-ng-data"
        )
        if len(str(espeak_data_path)) >= 160:
            raise VideoRuntimeError(
                "Video cache path is too long for the macOS eSpeak runtime; "
                "set AUTODESIGN_SKILL_VIDEO_CACHE to a shorter user-cache path"
            )
    return VideoRuntimeSpec(
        cache_root=root,
        cache_key=key,
        cache_dir=cache_dir,
        package_json=package_json,
        package_lock=package_lock,
        python_lock=python_lock,
        package_sha256=_sha256(package_json),
        package_lock_sha256=_sha256(package_lock),
        python_lock_sha256=observed_python_lock_sha256,
        system=system,
        machine=machine,
        python_major_minor=python,
        python_binary=python_binary,
        node_binary=Path(node).resolve() if node else None,
        npm_binary=Path(npm).resolve() if npm else None,
        node_major=node_major,
        ffmpeg_binary=Path(shutil.which("ffmpeg")).resolve() if shutil.which("ffmpeg") else None,
        ffprobe_binary=Path(shutil.which("ffprobe")).resolve() if shutil.which("ffprobe") else None,
    )


def runtime_environment(
    runtime: VideoRuntime,
    *,
    base: Mapping[str, str] | None = None,
    allow_network_configuration: bool = False,
) -> dict[str, str]:
    source = dict(os.environ if base is None else base)
    allowed = set(_SAFE_ENV_NAMES)
    if allow_network_configuration:
        allowed.update(_NETWORK_ENV_NAMES)
    env = {key: value for key, value in source.items() if key in allowed}
    env.update(
        {
            "HOME": str(runtime.home_dir),
            "HYPERFRAMES_PYTHON": str(runtime.python_executable),
            "HYPERFRAMES_NO_TELEMETRY": "1",
            "HYPERFRAMES_NO_UPDATE_CHECK": "1",
            "HYPERFRAMES_SKIP_SKILLS": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


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


def _safe_runtime_file(
    root: Path,
    relative: str,
    *,
    allow_executable_symlink: bool = False,
    allowed_external_target: Path | None = None,
) -> Path:
    candidate_relative = Path(relative)
    if candidate_relative.is_absolute() or ".." in candidate_relative.parts:
        raise VideoRuntimeError(f"Unsafe runtime state path: {relative}")
    candidate = root / candidate_relative
    cursor = root
    for part in candidate_relative.parts[:-1]:
        cursor /= part
        if cursor.is_symlink():
            raise VideoRuntimeError(f"Runtime path contains a symlinked parent: {relative}")
    if not candidate.is_file():
        raise VideoRuntimeError(f"Runtime file is missing: {relative}")
    if candidate.is_symlink() and not allow_executable_symlink:
        raise VideoRuntimeError(f"Runtime file must not be a symlink: {relative}")
    if not allow_executable_symlink and candidate.stat().st_nlink != 1:
        raise VideoRuntimeError(f"Runtime file must not be a hard link: {relative}")
    resolved = candidate.resolve()
    if not _is_within(resolved, root.resolve()):
        if allowed_external_target is None or resolved != allowed_external_target.resolve():
            raise VideoRuntimeError(f"Runtime file escapes cache: {relative}")
    return candidate


def _runtime_from_state(spec: VideoRuntimeSpec, state: Mapping[str, object]) -> VideoRuntime:
    hyperframes = _safe_runtime_file(
        spec.cache_dir, str(state["hyperframes_relative"]), allow_executable_symlink=True
    )
    python = _safe_runtime_file(
        spec.cache_dir,
        str(state["python_relative"]),
        allow_executable_symlink=True,
        allowed_external_target=spec.python_binary,
    )
    browser = _safe_runtime_file(spec.cache_dir, str(state["browser_relative"]))
    home = spec.cache_dir / str(state["home_relative"])
    if home.is_symlink() or not home.is_dir() or not _is_within(home.resolve(), spec.cache_dir.resolve()):
        raise VideoRuntimeError("Runtime HOME is missing, symlinked, or outside the cache")
    node = Path(str(state["node_binary"]))
    ffmpeg = Path(str(state["ffmpeg_binary"]))
    ffprobe = Path(str(state["ffprobe_binary"]))
    for label, path in (("node", node), ("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        if not path.is_file():
            raise VideoRuntimeError(f"Configured {label} executable is missing: {path}")
    return VideoRuntime(
        cache_dir=spec.cache_dir,
        home_dir=home,
        hyperframes_executable=hyperframes,
        python_executable=python,
        node_binary=node,
        browser_executable=browser,
        ffmpeg_binary=ffmpeg,
        ffprobe_binary=ffprobe,
        state_path=spec.cache_dir / "runtime-state.json",
    )


def _python_site_packages(venv: Path) -> list[Path]:
    if os.name == "nt":
        candidates = [venv / "Lib" / "site-packages"]
    else:
        candidates = sorted((venv / "lib").glob("python*/site-packages"))
    return [path for path in candidates if path.is_dir() and not path.is_symlink()]


def _make_python_packages_read_only(venv: Path) -> None:
    roots = _python_site_packages(venv)
    if len(roots) != 1:
        raise VideoRuntimeError("Python runtime must contain exactly one regular site-packages directory")
    root = roots[0]
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            if path.is_symlink() or path.stat().st_nlink != 1:
                raise VideoRuntimeError("Python package cache contains a linked file")
            executable = bool(path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
            path.chmod(0o555 if executable else 0o444)
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                raise VideoRuntimeError("Python package cache contains a symlinked directory")
            path.chmod(0o555)
    root.chmod(0o555)


def _verify_python_packages_read_only(venv: Path) -> None:
    roots = _python_site_packages(venv)
    if len(roots) != 1:
        raise VideoRuntimeError("Python runtime must contain exactly one regular site-packages directory")
    root = roots[0]
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            if path.is_symlink() or path.stat().st_mode & 0o222:
                raise VideoRuntimeError(f"Python package directory is writable or linked: {path}")
        for name in files:
            path = current_path / name
            if path.is_symlink() or path.stat().st_nlink != 1 or path.stat().st_mode & 0o222:
                raise VideoRuntimeError(f"Python package file is writable or linked: {path}")


def _discover_hyperframes_browser(runtime: VideoRuntime) -> Path:
    chrome_root = runtime.home_dir / ".cache" / "hyperframes" / "chrome"
    if chrome_root.is_symlink() or not chrome_root.is_dir():
        raise VideoRuntimeError("HyperFrames browser cache is missing or linked")
    names = {"chrome", "chrome.exe", "chrome-headless-shell", "chrome-headless-shell.exe"}
    candidates = sorted(
        path for path in chrome_root.rglob("*")
        if path.name in names and path.is_file() and not path.is_symlink()
    )
    executable = [
        path for path in candidates
        if os.name == "nt" or bool(path.stat().st_mode & stat.S_IXUSR)
    ]
    if len(executable) != 1:
        raise VideoRuntimeError(
            f"HyperFrames browser cache must contain exactly one launchable browser, found {len(executable)}"
        )
    browser = executable[0]
    if browser.stat().st_nlink != 1 or not _is_within(browser.resolve(), runtime.cache_dir.resolve()):
        raise VideoRuntimeError("HyperFrames browser executable is linked or outside the cache")
    return browser


def _probe_hyperframes_browser(runtime: VideoRuntime) -> dict[str, object]:
    puppeteer = runtime.hyperframes_executable.parents[2] / "node_modules" / "puppeteer-core"
    if puppeteer.is_symlink() or not (puppeteer / "package.json").is_file():
        raise VideoRuntimeError("HyperFrames Puppeteer runtime is missing or linked")
    script = r"""
const puppeteer = require(process.argv[1]);
(async () => {
  const browser = await puppeteer.launch({
    executablePath: process.argv[2],
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });
  try {
    const page = await browser.newPage();
    await page.setContent('<!doctype html><title>AutoDesign browser probe</title><main id="ok">ready</main>');
    const value = await page.$eval('#ok', element => element.textContent);
    if (value !== 'ready') throw new Error('browser DOM probe failed');
    process.stdout.write(JSON.stringify({passed: true, browser_version: await browser.version()}));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error && error.stack || String(error)); process.exit(2); });
"""
    result = _run_checked(
        [str(runtime.node_binary), "-e", script, str(puppeteer), str(runtime.browser_executable)],
        cwd=runtime.cache_dir,
        env=runtime_environment(runtime),
        timeout=60,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise VideoRuntimeError("HyperFrames browser launch probe returned invalid JSON") from error
    if not isinstance(payload, dict) or payload.get("passed") is not True:
        raise VideoRuntimeError("HyperFrames browser launch probe did not pass")
    return payload


def verify_kokoro_assets(
    runtime: VideoRuntime,
    *,
    sha256_reader: Callable[[Path], str] = _sha256,
) -> dict[str, str]:
    root = runtime.home_dir / ".cache" / "hyperframes" / "tts"
    model = _safe_runtime_file(
        runtime.cache_dir, str((root / "models" / "kokoro-v1.0.onnx").relative_to(runtime.cache_dir))
    )
    voices = _safe_runtime_file(
        runtime.cache_dir, str((root / "voices" / "voices-v1.0.bin").relative_to(runtime.cache_dir))
    )
    observed = {"model": sha256_reader(model), "voices": sha256_reader(voices)}
    if observed["model"] != KOKORO_MODEL_SHA256:
        raise VideoRuntimeError("Kokoro model checksum mismatch")
    if observed["voices"] != KOKORO_VOICES_SHA256:
        raise VideoRuntimeError("Kokoro voices checksum mismatch")
    return observed


def _command_version(command: Sequence[str], *, env: Mapping[str, str] | None = None) -> str:
    completed = subprocess.run(
        list(command), text=True, capture_output=True, timeout=20, check=False, env=dict(env or os.environ)
    )
    if completed.returncode != 0:
        raise VideoRuntimeError(
            f"Runtime command failed ({completed.returncode}): {Path(command[0]).name}"
        )
    return ((completed.stdout or "") + (completed.stderr or "")).strip().splitlines()[0]


def inspect_video_runtime(
    spec: VideoRuntimeSpec,
    *,
    sha256_reader: Callable[[Path], str] = _sha256,
    verify_commands: bool = True,
) -> dict[str, object]:
    cache = spec.cache_dir
    if cache.is_symlink():
        return {
            "ready": False,
            "status": "corrupt",
            "cache_dir": str(cache),
            "issues": ["runtime cache must not be a symlink"],
        }
    if not cache.exists():
        return {"ready": False, "status": "missing", "cache_dir": str(cache), "issues": ["runtime cache is absent"]}
    if not cache.is_dir():
        return {"ready": False, "status": "corrupt", "cache_dir": str(cache), "issues": ["runtime cache is not a regular directory"]}
    state_path = cache / "runtime-state.json"
    if not state_path.exists():
        return {"ready": False, "status": "partial", "cache_dir": str(cache), "issues": ["runtime-state.json is missing"]}
    if state_path.is_symlink() or not state_path.is_file() or state_path.stat().st_nlink != 1:
        return {
            "ready": False,
            "status": "corrupt",
            "cache_dir": str(cache),
            "issues": ["runtime-state.json must be a regular non-linked file"],
        }
    issues: list[str] = []
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or set(state) != _STATE_KEYS:
            raise VideoRuntimeError("runtime state has an unknown or incomplete schema")
        expected = {
            "format_version": RUNTIME_FORMAT_VERSION,
            "cache_key": spec.cache_key,
            "system": spec.system,
            "machine": spec.machine,
            "python_major_minor": spec.python_major_minor,
            "node_major": spec.node_major,
            "node_binary": str(spec.node_binary),
            "ffmpeg_binary": str(spec.ffmpeg_binary),
            "ffprobe_binary": str(spec.ffprobe_binary),
            "hyperframes_version": HYPERFRAMES_VERSION,
            "package_sha256": spec.package_sha256,
            "package_lock_sha256": spec.package_lock_sha256,
            "python_lock_sha256": spec.python_lock_sha256,
            "kokoro_onnx_version": KOKORO_ONNX_VERSION,
            "soundfile_version": SOUNDFILE_VERSION,
            "kokoro_model_sha256": KOKORO_MODEL_SHA256,
            "kokoro_voices_sha256": KOKORO_VOICES_SHA256,
            "browser_ensured": True,
        }
        for key, value in expected.items():
            if state.get(key) != value:
                raise VideoRuntimeError(f"runtime state mismatch: {key}")
        runtime = _runtime_from_state(spec, state)
        if sha256_reader(runtime.browser_executable) != state["browser_sha256"]:
            raise VideoRuntimeError("HyperFrames browser checksum mismatch")
        _verify_python_packages_read_only(runtime.python_executable.parents[1])
        observed = verify_kokoro_assets(runtime, sha256_reader=sha256_reader)
        smoke = _safe_runtime_file(cache, str(state["tts_smoke_relative"]))
        if sha256_reader(smoke) != state["tts_smoke_sha256"]:
            raise VideoRuntimeError("TTS smoke checksum mismatch")
        if verify_commands:
            env = runtime_environment(runtime)
            version = _command_version([str(runtime.hyperframes_executable), "--version"], env=env)
            if version != HYPERFRAMES_VERSION:
                raise VideoRuntimeError(f"HyperFrames version mismatch: {version}")
            for module, expected_version in (
                ("kokoro_onnx", KOKORO_ONNX_VERSION),
                ("soundfile", SOUNDFILE_VERSION),
            ):
                actual = _command_version(
                    [str(runtime.python_executable), "-c", f"import importlib.metadata as m; print(m.version('{module.replace('_', '-')}'))"],
                    env=env,
                )
                if actual != expected_version:
                    raise VideoRuntimeError(f"{module} version mismatch: {actual}")
            _probe_hyperframes_browser(runtime)
        return {
            "ready": True,
            "status": "ready",
            "cache_dir": str(cache),
            "issues": [],
            "runtime": runtime,
            "kokoro_assets": observed,
        }
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError, VideoRuntimeError) as error:
        issues.append(str(error))
        return {"ready": False, "status": "corrupt", "cache_dir": str(cache), "issues": issues}


def doctor_video_runtime(*, cache_root: Path | None = None) -> dict[str, object]:
    try:
        spec = runtime_spec(cache_root=cache_root)
    except (OSError, VideoRuntimeError) as error:
        return {"ready": False, "status": "corrupt", "issues": [str(error)]}
    runtime_missing: list[str] = []
    if spec.node_major is None or spec.node_major < 22:
        runtime_missing.append("node>=22")
    if spec.npm_binary is None:
        runtime_missing.append("npm")
    if spec.ffmpeg_binary is None:
        runtime_missing.append("ffmpeg")
    if spec.ffprobe_binary is None:
        runtime_missing.append("ffprobe")
    if spec.python_binary is None:
        runtime_missing.append("python3.10-3.12 for Kokoro")
    pdf_missing = [name for name in PDF_INGEST_TOOLS if shutil.which(name) is None]
    missing = [*runtime_missing, *pdf_missing]
    if missing:
        issues: list[str] = []
        if runtime_missing:
            issues.append("missing video runtime prerequisites: " + ", ".join(runtime_missing))
        if pdf_missing:
            issues.append(
                "missing PDF ingest prerequisites (Poppler): " + ", ".join(pdf_missing)
            )
        return {
            "ready": False,
            "status": "missing",
            "cache_dir": str(spec.cache_dir),
            "missing": missing,
            "issues": issues,
        }
    return inspect_video_runtime(spec)


def require_video_runtime(*, cache_root: Path | None = None) -> VideoRuntime:
    spec = runtime_spec(cache_root=cache_root)
    report = inspect_video_runtime(spec)
    if report.get("ready") is not True or not isinstance(report.get("runtime"), VideoRuntime):
        raise VideoRuntimeError("; ".join(str(item) for item in report.get("issues", [])))
    return report["runtime"]


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            process.terminate()
    else:
        process.terminate()
    try:
        process.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                process.kill()
        else:
            process.kill()


def _run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        list(command), cwd=cwd, env=dict(env), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
        raise VideoRuntimeError(f"Runtime setup timed out: {Path(command[0]).name}") from error
    result = subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)
    if result.returncode != 0:
        detail = ((stderr or "") + "\n" + (stdout or "")).strip()
        raise VideoRuntimeError(
            f"Runtime setup command failed ({result.returncode}): {Path(command[0]).name}"
            + (f": {detail[-4000:]}" if detail else "")
        )
    return result


def _download_verified(
    url: str,
    destination: Path,
    expected_sha256: str,
    *,
    network_environment: Mapping[str, str],
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise VideoRuntimeError(f"Refusing to overwrite runtime asset: {destination}")
    temporary = destination.with_name(f".{destination.name}.partial-{uuid.uuid4().hex}")
    proxies = {
        scheme: network_environment[name]
        for scheme, names in {
            "http": ("http_proxy", "HTTP_PROXY"),
            "https": ("https_proxy", "HTTPS_PROXY"),
        }.items()
        for name in names
        if network_environment.get(name)
    }
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    digest = hashlib.sha256()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "AutoDesign-video-skill/0.1"})
        with opener.open(request, timeout=120) as response, temporary.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        observed = digest.hexdigest()
        if observed != expected_sha256:
            raise VideoRuntimeError(
                f"Downloaded runtime asset checksum mismatch: {destination.name}"
            )
        os.replace(temporary, destination)
    except (OSError, urllib.error.URLError) as error:
        raise VideoRuntimeError(
            f"Could not download verified runtime asset {destination.name}: {error}"
        ) from error
    finally:
        temporary.unlink(missing_ok=True)


def _staging_runtime(spec: VideoRuntimeSpec, staging: Path) -> VideoRuntime:
    node_root = staging / "n"
    hyperframes = node_root / "node_modules" / ".bin" / ("hyperframes.cmd" if os.name == "nt" else "hyperframes")
    python = staging / _venv_python_relative()
    return VideoRuntime(
        cache_dir=staging,
        home_dir=staging / "h",
        hyperframes_executable=hyperframes,
        python_executable=python,
        node_binary=spec.node_binary or Path("node"),
        browser_executable=staging / "browser-not-yet-installed",
        ffmpeg_binary=spec.ffmpeg_binary or Path("ffmpeg"),
        ffprobe_binary=spec.ffprobe_binary or Path("ffprobe"),
        state_path=staging / "runtime-state.json",
    )


def _write_runtime_state(spec: VideoRuntimeSpec, runtime: VideoRuntime, smoke: Path) -> None:
    tts_root = runtime.home_dir / ".cache" / "hyperframes" / "tts"
    payload: dict[str, object] = {
        "format_version": RUNTIME_FORMAT_VERSION,
        "cache_key": spec.cache_key,
        "system": spec.system,
        "machine": spec.machine,
        "python_major_minor": spec.python_major_minor,
        "node_major": spec.node_major,
        "node_binary": str(spec.node_binary),
        "ffmpeg_binary": str(spec.ffmpeg_binary),
        "ffprobe_binary": str(spec.ffprobe_binary),
        "hyperframes_version": HYPERFRAMES_VERSION,
        "hyperframes_relative": runtime.hyperframes_executable.relative_to(runtime.cache_dir).as_posix(),
        "python_relative": runtime.python_executable.relative_to(runtime.cache_dir).as_posix(),
        "home_relative": runtime.home_dir.relative_to(runtime.cache_dir).as_posix(),
        "package_sha256": spec.package_sha256,
        "package_lock_sha256": spec.package_lock_sha256,
        "python_lock_sha256": spec.python_lock_sha256,
        "kokoro_onnx_version": KOKORO_ONNX_VERSION,
        "soundfile_version": SOUNDFILE_VERSION,
        "kokoro_model_relative": (tts_root / "models" / "kokoro-v1.0.onnx").relative_to(runtime.cache_dir).as_posix(),
        "kokoro_model_sha256": KOKORO_MODEL_SHA256,
        "kokoro_voices_relative": (tts_root / "voices" / "voices-v1.0.bin").relative_to(runtime.cache_dir).as_posix(),
        "kokoro_voices_sha256": KOKORO_VOICES_SHA256,
        "tts_smoke_relative": smoke.relative_to(runtime.cache_dir).as_posix(),
        "tts_smoke_sha256": _sha256(smoke),
        "browser_relative": runtime.browser_executable.relative_to(runtime.cache_dir).as_posix(),
        "browser_sha256": _sha256(runtime.browser_executable),
        "browser_ensured": True,
    }
    _atomic_write_json(runtime.state_path, payload)


def ensure_video_runtime(
    *,
    cache_root: Path | None = None,
    lock_timeout_seconds: float = 2400,
) -> VideoRuntime:
    spec = runtime_spec(cache_root=cache_root)
    runtime_missing: list[str] = []
    if spec.node_major is None or spec.node_major < 22:
        runtime_missing.append("Node.js 22+")
    if spec.npm_binary is None:
        runtime_missing.append("npm")
    if spec.ffmpeg_binary is None:
        runtime_missing.append("ffmpeg")
    if spec.ffprobe_binary is None:
        runtime_missing.append("ffprobe")
    if spec.python_binary is None:
        runtime_missing.append("Python 3.10-3.12 for Kokoro")
    pdf_missing = [name for name in PDF_INGEST_TOOLS if shutil.which(name) is None]
    if runtime_missing or pdf_missing:
        messages: list[str] = []
        if runtime_missing:
            messages.append("missing video runtime prerequisites: " + ", ".join(runtime_missing))
        if pdf_missing:
            messages.append(
                "missing PDF ingest prerequisites (Poppler): " + ", ".join(pdf_missing)
            )
        raise VideoRuntimeError("; ".join(messages))
    spec.cache_root.mkdir(parents=True, exist_ok=True)
    lock = spec.cache_root / f".{spec.cache_key}.lock"
    with setup_browser._cache_lock(lock, lock_timeout_seconds):
        existing = inspect_video_runtime(spec)
        if existing.get("ready") is True:
            return existing["runtime"]  # type: ignore[return-value]
        if spec.cache_dir.exists() or spec.cache_dir.is_symlink():
            quarantine = spec.cache_root / f"{spec.cache_key}.quarantine-{int(time.time())}-{uuid.uuid4().hex[:8]}"
            spec.cache_dir.rename(quarantine)
        staging = spec.cache_root / f".s-{uuid.uuid4().hex[:10]}"
        try:
            node_root = staging / "n"
            node_root.mkdir(parents=True)
            shutil.copy2(spec.package_json, node_root / "package.json")
            shutil.copy2(spec.package_lock, node_root / "package-lock.json")
            runtime = _staging_runtime(spec, staging)
            runtime.home_dir.mkdir(parents=True)
            install_env = runtime_environment(runtime, allow_network_configuration=True)
            _run_checked(
                [str(spec.npm_binary), "ci", "--omit=dev", "--include=optional", "--no-audit", "--no-fund"],
                cwd=node_root,
                env=install_env,
                timeout=_INSTALL_TIMEOUT_SECONDS,
            )
            _run_checked(
                [str(spec.python_binary), "-m", "venv", str(staging / "p")],
                cwd=staging,
                env=install_env,
                timeout=300,
            )
            _run_checked(
                [
                    str(runtime.python_executable), "-m", "pip", "install",
                    "--disable-pip-version-check", "--no-input", "--require-hashes",
                    "--only-binary=:all:", "--no-deps", "-r", str(spec.python_lock),
                ],
                cwd=staging,
                env=install_env,
                timeout=_INSTALL_TIMEOUT_SECONDS,
            )
            version = _run_checked(
                [str(runtime.hyperframes_executable), "--version"],
                cwd=staging,
                env=install_env,
                timeout=30,
            ).stdout.strip()
            if version != HYPERFRAMES_VERSION:
                raise VideoRuntimeError(f"HyperFrames version mismatch after install: {version}")
            _run_checked(
                [str(runtime.hyperframes_executable), "browser", "ensure"],
                cwd=staging,
                env=install_env,
                timeout=_INSTALL_TIMEOUT_SECONDS,
            )
            runtime = replace(runtime, browser_executable=_discover_hyperframes_browser(runtime))
            _probe_hyperframes_browser(runtime)
            tts_root = runtime.home_dir / ".cache" / "hyperframes" / "tts"
            _download_verified(
                KOKORO_MODEL_URL,
                tts_root / "models" / "kokoro-v1.0.onnx",
                KOKORO_MODEL_SHA256,
                network_environment=install_env,
            )
            _download_verified(
                KOKORO_VOICES_URL,
                tts_root / "voices" / "voices-v1.0.bin",
                KOKORO_VOICES_SHA256,
                network_environment=install_env,
            )
            verify_kokoro_assets(runtime)
            smoke_dir = staging / "s"
            smoke_dir.mkdir()
            smoke_text = smoke_dir / "tts.txt"
            smoke_wav = smoke_dir / "tts.wav"
            smoke_text.write_text("AutoDesign verifies local conference narration.\n", encoding="utf-8")
            _run_checked(
                [
                    str(runtime.hyperframes_executable), "tts", str(smoke_text),
                    "--output", str(smoke_wav), "--voice", "af_heart",
                    "--lang", "en-us", "--json",
                ],
                cwd=staging,
                env=install_env,
                timeout=_INSTALL_TIMEOUT_SECONDS,
            )
            if not smoke_wav.is_file() or smoke_wav.stat().st_size <= 0:
                raise VideoRuntimeError("HyperFrames TTS smoke did not create audio")
            _run_checked(
                [
                    str(runtime.ffprobe_binary), "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(smoke_wav),
                ],
                cwd=staging,
                env=install_env,
                timeout=30,
            )
            _make_python_packages_read_only(staging / "p")
            _verify_python_packages_read_only(staging / "p")
            _write_runtime_state(spec, runtime, smoke_wav)
            if spec.cache_dir.exists():
                raise VideoRuntimeError("Video cache appeared during atomic install")
            staging.rename(spec.cache_dir)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return require_video_runtime(cache_root=spec.cache_root)


def remove_video_runtime(
    *,
    cache_root: Path | None = None,
    lock_timeout_seconds: float = 300,
) -> dict[str, str]:
    """Remove only this version's isolated runtime cache."""
    spec = runtime_spec(cache_root=cache_root)
    spec.cache_root.mkdir(parents=True, exist_ok=True)
    lock = spec.cache_root / f".{spec.cache_key}.lock"
    with setup_browser._cache_lock(lock, lock_timeout_seconds):
        if spec.cache_dir.is_symlink():
            raise VideoRuntimeError(
                f"Refusing to remove a symlinked video runtime cache: {spec.cache_dir}"
            )
        if not spec.cache_dir.exists():
            return {"status": "missing", "cache_dir": str(spec.cache_dir)}
        if not spec.cache_dir.is_dir():
            raise VideoRuntimeError(
                f"Refusing to remove a non-directory video runtime cache: {spec.cache_dir}"
            )
        removal = spec.cache_root / f".{spec.cache_key}.remove-{uuid.uuid4().hex[:10]}"
        spec.cache_dir.rename(removal)
        try:
            for current, directories, files in os.walk(removal, topdown=False, followlinks=False):
                for name in files:
                    path = Path(current) / name
                    if not path.is_symlink():
                        path.chmod(path.stat().st_mode | stat.S_IWUSR)
                for name in directories:
                    path = Path(current) / name
                    if not path.is_symlink():
                        path.chmod(path.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
            removal.chmod(removal.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
            shutil.rmtree(removal)
        except OSError as error:
            raise VideoRuntimeError(
                f"Video runtime was detached but could not be fully removed: {removal}: {error}"
            ) from error
    return {"status": "removed", "cache_dir": str(spec.cache_dir)}


def run_real_smoke(runtime: VideoRuntime, *, output_dir: Path) -> dict[str, object]:
    """Render a short real HyperFrames/Kokoro fixture and keep all evidence."""
    if output_dir.exists() or output_dir.is_symlink():
        raise VideoRuntimeError(f"Smoke output already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import video_harness

    plan = video_harness.synthetic_smoke_plan()
    claims = video_harness.synthetic_smoke_claims(plan)
    project = output_dir / "project"
    video_harness.write_synthetic_smoke_project(project, plan)
    report = video_harness.deliver_project(
        project, plan, runtime.as_dict(), claims=claims, smoke=True
    )
    if not report.get("passed"):
        raise VideoRuntimeError(f"Real HyperFrames smoke failed: {report.get('error')}")
    return {
        "passed": True,
        "hyperframes_version": HYPERFRAMES_VERSION,
        "duration_s": plan["duration_s"],
        "project": str(project),
        "mp4": report["mp4_path"],
        "ffprobe_json": str(project / "media_probe.json"),
        "contact_sheet": report["contact_sheet"],
        "report": report,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor", help="Inspect exact local video runtime state")
    doctor.add_argument("--cache-root", type=Path)
    setup = subparsers.add_parser("setup", help="Install exact HyperFrames/Kokoro runtime")
    setup.add_argument("--cache-root", type=Path)
    remove = subparsers.add_parser("remove", help="Remove this version's isolated runtime cache")
    remove.add_argument("--cache-root", type=Path)
    smoke = subparsers.add_parser("smoke", help="Run a short real TTS/render/probe smoke")
    smoke.add_argument("--cache-root", type=Path)
    smoke.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            payload = doctor_video_runtime(cache_root=args.cache_root)
            print(json.dumps({key: value for key, value in payload.items() if key != "runtime"}, indent=2, default=str))
            return 0 if payload.get("ready") else 2
        if args.command == "setup":
            runtime = ensure_video_runtime(cache_root=args.cache_root)
            print(json.dumps(runtime.as_dict(), indent=2))
            return 0
        if args.command == "remove":
            print(json.dumps(remove_video_runtime(cache_root=args.cache_root), indent=2))
            return 0
        runtime = require_video_runtime(cache_root=args.cache_root)
        print(json.dumps(run_real_smoke(runtime, output_dir=args.output), indent=2, default=str))
        return 0
    except (OSError, VideoRuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
