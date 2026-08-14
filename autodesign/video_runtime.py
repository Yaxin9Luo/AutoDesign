from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parents[1]
_HYPERFRAMES_VERSION = "0.7.86"


class VideoRuntimeUnavailableError(ValueError):
    issue_id = "video_runtime_unavailable"

    def __init__(self, profile: dict[str, Any]):
        self.profile = dict(profile)
        self.missing = [str(item) for item in profile.get("missing") or []]
        self.repair = str(
            profile.get("repair")
            or "Run `autodesign doctor` and `autodesign setup`."
        )
        missing_text = ", ".join(self.missing) or "video runtime diagnostics"
        super().__init__(
            "Video generation is unavailable because required local runtime "
            f"components are missing: {missing_text}. {self.repair}"
        )


def _runtime_command_version(
    binary: Path | str | None,
    *args: str,
    path_env: str | None = None,
) -> str:
    if not binary:
        return ""
    try:
        completed = subprocess.run(
            [str(binary), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "PATH": path_env} if path_env is not None else None,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    return output.splitlines()[0] if output else ""


def video_environment_profile(
    *,
    repo_root: Path | None = None,
    path_env: str | None = None,
) -> dict[str, Any]:
    root = repo_root or _REPO_ROOT
    search_path = os.environ.get("PATH", "") if path_env is None else path_env
    node_binary = shutil.which("node", path=search_path)
    ffmpeg_binary = shutil.which("ffmpeg", path=search_path)
    ffprobe_binary = shutil.which("ffprobe", path=search_path)
    managed_hyperframes = (
        root / "runtime" / "video" / "node_modules" / ".bin" / "hyperframes"
    )
    development_hyperframes = (
        root / "web" / "node_modules" / ".bin" / "hyperframes"
    )
    configured_hyperframes = str(
        os.getenv("AUTODESIGN_HYPERFRAMES_BIN") or ""
    ).strip()
    hyperframes_binary: Path | None = None
    hyperframes_source = "missing"
    for candidate, source in (
        (
            Path(configured_hyperframes).expanduser()
            if configured_hyperframes
            else None,
            "configured",
        ),
        (managed_hyperframes, "managed_runtime"),
        (development_hyperframes, "development_runtime"),
    ):
        if candidate is not None and candidate.is_file() and os.access(candidate, os.X_OK):
            hyperframes_binary = candidate
            hyperframes_source = source
            break

    node_version = _runtime_command_version(
        node_binary,
        "--version",
        path_env=search_path,
    )
    node_major_text = node_version.removeprefix("v").split(".", 1)[0]
    node_compatible = node_major_text.isdigit() and int(node_major_text) >= 22
    hyperframes_version = _runtime_command_version(
        hyperframes_binary,
        "--version",
        path_env=search_path,
    )
    missing: list[str] = []
    if not node_compatible:
        missing.append("node>=22")
    if hyperframes_version != _HYPERFRAMES_VERSION:
        missing.append(f"hyperframes=={_HYPERFRAMES_VERSION}")
    if not ffmpeg_binary:
        missing.append("ffmpeg")
    if not ffprobe_binary:
        missing.append("ffprobe")

    return {
        "ready": not missing,
        "missing": missing,
        "repair": (
            "Run `autodesign setup`; install Node.js 22+ and ffmpeg if "
            "the reported prerequisites remain missing."
        ),
        "node": {
            "available": bool(node_binary),
            "compatible": node_compatible,
            "binary": node_binary or "",
            "version": node_version,
        },
        "hyperframes": {
            "available": hyperframes_version == _HYPERFRAMES_VERSION,
            "binary": str(hyperframes_binary or ""),
            "version": hyperframes_version,
            "source": hyperframes_source,
        },
        "ffmpeg": {
            "available": bool(ffmpeg_binary),
            "binary": ffmpeg_binary or "",
        },
        "ffprobe": {
            "available": bool(ffprobe_binary),
            "binary": ffprobe_binary or "",
        },
    }


def require_video_runtime(
    artifact_type: str,
    *,
    profile: dict[str, Any] | None = None,
) -> None:
    if artifact_type != "video":
        return
    resolved = profile or video_environment_profile()
    if resolved.get("ready") is True:
        return
    raise VideoRuntimeUnavailableError(resolved)
