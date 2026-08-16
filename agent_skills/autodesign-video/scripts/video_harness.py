#!/usr/bin/env python3
"""Standalone, source-grounded HyperFrames conference-video harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _portable as core  # noqa: E402
import setup_video  # noqa: E402


FORMAT_VERSION = 1
RELEASE_VERSION = "0.1.0"
WIDTH = 1920
HEIGHT = 1080
FPS = 30
DEFAULT_SCENE_COUNT = 12
DEFAULT_DURATION_S = 360
MIN_SCENES = 10
MAX_SCENES = 14
MIN_DURATION_S = 300
MAX_DURATION_S = 600
SPEECH_END_MARGIN_S = 0.5
MAX_TTS_SPEED = 1.30
FRESH_MTIME_TOLERANCE_NS = 2_000_000_000
_ALLOWED_VOICES = {
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky",
    "am_adam", "am_michael", "bf_emma", "bf_isabella", "bm_george", "bm_lewis",
}
_LOCAL_REFERENCE_ATTRS = {"src", "href", "poster"}
_REMOTE_PREFIXES = ("http://", "https://", "//", "ftp://")
_NETWORK_SCRIPT = re.compile(
    r"requestAnimationFrame|\bfetch\s*\(|XMLHttpRequest|WebSocket|EventSource|"
    r"sendBeacon|\bimport\s*\(|document\.write",
    re.IGNORECASE,
)
_WORD = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)?")

REVIEW_RUBRIC: dict[str, Any] = {
    "format_version": FORMAT_VERSION,
    "artifact_type": "conference_video",
    "scale": {"min": 1, "max": 5},
    "dimensions": [
        "research_story_and_source_fidelity",
        "scene_composition_and_visual_hierarchy",
        "figure_legibility_and_evidence_use",
        "motion_continuity_and_seekability",
        "narration_pacing_and_audio_quality",
        "subtitle_readability_and_optional_playback",
        "conference_readiness_and_low_ai_aesthetic",
    ],
    "hard_blockers": [
        "invented_or_unbound_claim",
        "remote_or_untrusted_asset",
        "unreadable_source_figure",
        "non_seekable_or_frame_clock_dependent_motion",
        "missing_or_forced_subtitles",
        "inaudible_or_truncated_narration",
        "invalid_media_contract",
    ],
}


class VideoContractError(RuntimeError):
    """A video plan, project, or delivery violates the portable contract."""


class StageError(RuntimeError):
    """A delivery stage failed with an explicit repair-routing class."""

    def __init__(self, stage: str, message: str, *, failure_class: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.failure_class = failure_class


def sha256_file(path: Path | str) -> str:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise VideoContractError(f"expected a regular non-symlink file: {source}")
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VideoContractError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise VideoContractError(f"{field} must be finite")
    return result


def _integer(value: object, *, field: str) -> int:
    number = _number(value, field=field)
    if not number.is_integer():
        raise VideoContractError(f"{field} must be an integer")
    return int(number)


def normalize_plan(value: Mapping[str, Any], *, smoke: bool = False) -> dict[str, Any]:
    """Validate and normalize a deterministic video plan without repairing it."""

    if not isinstance(value, Mapping):
        raise VideoContractError("video plan must be an object")
    scenes_value = value.get("scenes")
    if not isinstance(scenes_value, list) or not scenes_value:
        raise VideoContractError("video plan requires a non-empty scenes list")
    scene_count = _integer(value.get("scene_count", len(scenes_value)), field="scene_count")
    if scene_count != len(scenes_value):
        raise VideoContractError("scene_count must match the scenes list")
    duration_default = sum(
        _number(scene.get("duration_s"), field="scene.duration_s")
        for scene in scenes_value
        if isinstance(scene, Mapping)
    )
    duration_s = _number(value.get("duration_s", duration_default or DEFAULT_DURATION_S), field="duration_s")
    if not smoke and not MIN_SCENES <= scene_count <= MAX_SCENES:
        raise VideoContractError(f"scene_count must be between {MIN_SCENES} and {MAX_SCENES}")
    if not smoke and not MIN_DURATION_S <= duration_s <= MAX_DURATION_S:
        raise VideoContractError(
            f"duration_s must be between {MIN_DURATION_S} and {MAX_DURATION_S}"
        )
    if smoke and (scene_count < 1 or duration_s <= 0):
        raise VideoContractError("smoke plans still require positive scenes and duration")
    width = _integer(value.get("width", WIDTH), field="width")
    height = _integer(value.get("height", HEIGHT), field="height")
    fps = _integer(value.get("fps", FPS), field="fps")
    if (width, height, fps) != (WIDTH, HEIGHT, FPS):
        raise VideoContractError("video canvas must be exactly 1920x1080 at 30 fps")
    if value.get("artifact_type", "video") != "video":
        raise VideoContractError("artifact_type must be video")
    if _integer(value.get("format_version", FORMAT_VERSION), field="format_version") != FORMAT_VERSION:
        raise VideoContractError("unsupported video plan format_version")
    language = str(value.get("language", "en")).strip().lower()
    if language not in {"en", "en-us", "english"}:
        raise VideoContractError("conference narration and subtitles must be English")
    voice_id = str(value.get("voice_id", "af_heart")).strip()
    if voice_id not in _ALLOWED_VOICES:
        raise VideoContractError(f"unsupported bundled Kokoro voice: {voice_id}")
    max_attempts = _integer(value.get("max_attempts", 4), field="max_attempts")
    if not 1 <= max_attempts <= 8:
        raise VideoContractError("max_attempts must be between 1 and 8")

    normalized_scenes: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = 0.0
    for index, scene_value in enumerate(scenes_value, start=1):
        if not isinstance(scene_value, Mapping):
            raise VideoContractError(f"scene {index} must be an object")
        scene_id = str(scene_value.get("scene_id", "")).strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,63}", scene_id):
            raise VideoContractError(f"scene {index} has an invalid scene_id")
        if scene_id in seen:
            raise VideoContractError(f"duplicate scene_id: {scene_id}")
        seen.add(scene_id)
        start_s = _number(scene_value.get("start_s"), field=f"{scene_id}.start_s")
        scene_duration = _number(
            scene_value.get("duration_s"), field=f"{scene_id}.duration_s"
        )
        if scene_duration <= 0:
            raise VideoContractError(f"{scene_id}.duration_s must be positive")
        if abs(start_s - cursor) > 1e-6:
            raise VideoContractError("scene timing must be contiguous from zero")
        narration = " ".join(str(scene_value.get("narration", "")).split())
        if len(_WORD.findall(narration)) < 3:
            raise VideoContractError(f"{scene_id} requires substantive English narration")
        title = " ".join(str(scene_value.get("title", "")).split())
        role = str(scene_value.get("role", "")).strip()
        if not title or not role:
            raise VideoContractError(f"{scene_id} requires title and role")
        source_ids_value = scene_value.get("source_ids")
        visual_ids_value = scene_value.get("visual_ids", [])
        if (
            not isinstance(source_ids_value, list)
            or not source_ids_value
            or any(not isinstance(item, str) or not item for item in source_ids_value)
        ):
            raise VideoContractError(f"{scene_id} requires source_ids")
        if not isinstance(visual_ids_value, list) or any(
            not isinstance(item, str) or not item for item in visual_ids_value
        ):
            raise VideoContractError(f"{scene_id}.visual_ids must be a list of ids")
        normalized_scenes.append(
            {
                "scene_id": scene_id,
                "title": title,
                "role": role,
                "start_s": start_s,
                "duration_s": scene_duration,
                "narration": narration,
                "source_ids": list(dict.fromkeys(source_ids_value)),
                "visual_ids": list(dict.fromkeys(visual_ids_value)),
            }
        )
        cursor += scene_duration
    if abs(cursor - duration_s) > 1e-6:
        raise VideoContractError("scene durations must sum exactly to duration_s")
    return {
        "format_version": FORMAT_VERSION,
        "artifact_type": "video",
        "width": WIDTH,
        "height": HEIGHT,
        "fps": FPS,
        "scene_count": scene_count,
        "duration_s": int(duration_s) if duration_s.is_integer() else duration_s,
        "voice_id": voice_id,
        "language": "en",
        "scenes": normalized_scenes,
        "max_attempts": max_attempts,
    }


class _ProjectParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.roots: list[dict[str, str]] = []
        self.scenes: list[dict[str, str]] = []
        self.audio: list[dict[str, str]] = []
        self.images: list[dict[str, str]] = []
        self.references: list[tuple[str, str, str]] = []
        self.subtitle_toggles: list[dict[str, str]] = []
        self.elements_by_id: dict[str, dict[str, str]] = {}
        self.forbidden_tags: list[str] = []
        self.srcsets: list[str] = []
        self.scripts: list[str] = []
        self._script_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        if values.get("id"):
            self.elements_by_id[values["id"]] = values
        if "data-composition-id" in values:
            self.roots.append(values)
        if tag.lower() == "section":
            self.scenes.append(values)
        if tag.lower() == "audio":
            self.audio.append(values)
        if tag.lower() == "img":
            self.images.append(values)
        if "data-subtitle-toggle" in values:
            self.subtitle_toggles.append(values)
        if tag.lower() in {"base", "embed", "form", "iframe", "object"}:
            self.forbidden_tags.append(tag.lower())
        if values.get("srcset"):
            self.srcsets.append(values["srcset"])
        for name in _LOCAL_REFERENCE_ATTRS:
            if values.get(name):
                self.references.append((tag.lower(), name, values[name].strip()))
        if tag.lower() == "script":
            self._script_depth += 1
            if values.get("src"):
                self.scripts.append(f"src={values['src']}")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._script_depth:
            self._script_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._script_depth:
            self.scripts.append(data)


def _issue(code: str, message: str, **context: object) -> dict[str, object]:
    return {"code": code, "message": message, **context}


def _safe_project_file(project: Path, reference: str) -> Path:
    relative = Path(reference.split("#", 1)[0].split("?", 1)[0])
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise VideoContractError(f"unsafe local asset path: {reference}")
    candidate = project / relative
    cursor = project
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise VideoContractError(f"local asset traverses a symlink: {reference}")
    if not candidate.is_file() or candidate.stat().st_nlink != 1:
        raise VideoContractError(f"local asset is missing or hard-linked: {reference}")
    try:
        candidate.resolve().relative_to(project.resolve())
    except ValueError as error:
        raise VideoContractError(f"local asset escapes project: {reference}") from error
    return candidate


def _classes(attrs: Mapping[str, str]) -> set[str]:
    return {item for item in attrs.get("class", "").split() if item}


def validate_project(
    project_dir: Path | str,
    plan_value: Mapping[str, Any],
    *,
    evidence_ids: set[str] | None = None,
    visual_catalog: Mapping[str, Mapping[str, str]] | None = None,
    smoke: bool = False,
) -> dict[str, Any]:
    """Run structural validation only; narration media may not exist yet."""

    issues: list[dict[str, object]] = []
    try:
        plan = normalize_plan(plan_value, smoke=smoke)
    except VideoContractError as error:
        return {"passed": False, "issues": [_issue("plan_contract", str(error))]}
    project = Path(project_dir).absolute()
    if project.is_symlink() or not project.is_dir():
        return {"passed": False, "issues": [_issue("unsafe_project", "project must be a regular directory")]}
    index = project / "index.html"
    config = project / "hyperframes.json"
    if index.is_symlink() or not index.is_file():
        issues.append(_issue("index_missing", "index.html is missing or symlinked"))
    if config.is_symlink() or not config.is_file():
        issues.append(_issue("hyperframes_config_missing", "hyperframes.json is missing or symlinked"))
    if issues:
        return {"passed": False, "issues": issues}
    try:
        config_value = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        issues.append(_issue("hyperframes_config_invalid", "hyperframes.json is not valid JSON"))
    else:
        if not isinstance(config_value, dict) or config_value.get("entry") != "index.html":
            issues.append(_issue("hyperframes_config_invalid", "entry must be index.html"))
    try:
        text = index.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return {"passed": False, "issues": [_issue("index_unreadable", str(error))]}
    parser = _ProjectParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as error:
        issues.append(_issue("html_parse", f"HTML parsing failed: {error}"))

    if len(parser.roots) != 1:
        issues.append(
            _issue("composition_root_count", "exactly one data-composition-id root is required", observed=len(parser.roots))
        )
    else:
        root = parser.roots[0]
        try:
            root_start = float(root.get("data-start", "nan"))
            root_duration = float(root.get("data-duration", "nan"))
        except ValueError:
            root_start = root_duration = math.nan
        if (
            root.get("data-width") != str(WIDTH)
            or root.get("data-height") != str(HEIGHT)
            or not math.isfinite(root_start)
            or abs(root_start) > 1e-6
            or not math.isfinite(root_duration)
            or abs(root_duration - float(plan["duration_s"])) > 1e-6
        ):
            issues.append(_issue("composition_contract", "composition dimensions or duration differ from plan"))
        if "data-no-timeline" not in root and "window.__timelines" not in text:
            issues.append(_issue("deterministic_timeline_missing", "composition is not explicitly static or seekable"))

    if len(parser.scenes) != plan["scene_count"]:
        issues.append(_issue("scene_count", "HTML scene count differs from plan", observed=len(parser.scenes)))
    expected_scenes = {scene["scene_id"]: scene for scene in plan["scenes"]}
    for attrs in parser.scenes:
        scene_id = attrs.get("id", "")
        expected = expected_scenes.get(scene_id)
        if "clip" not in _classes(attrs):
            issues.append(_issue("literal_clip_required", 'every scene needs literal class="clip"', scene_id=scene_id))
        if expected is None:
            issues.append(_issue("unknown_scene", "HTML scene is not in the plan", scene_id=scene_id))
            continue
        try:
            start = float(attrs.get("data-start", "nan"))
            duration = float(attrs.get("data-duration", "nan"))
        except ValueError:
            start, duration = math.nan, math.nan
        if (
            not math.isfinite(start)
            or not math.isfinite(duration)
            or abs(start - float(expected["start_s"])) > 1e-6
            or abs(duration - float(expected["duration_s"])) > 1e-6
        ):
            issues.append(_issue("scene_timing", "scene timing differs from plan", scene_id=scene_id))
        html_sources = {item for item in attrs.get("data-source-ids", "").split() if item}
        expected_sources = set(expected["source_ids"])
        if html_sources != expected_sources:
            issues.append(_issue("scene_source_binding", "scene source ids differ from plan", scene_id=scene_id))
        if evidence_ids is not None and not html_sources.issubset(evidence_ids):
            issues.append(_issue("unknown_evidence", "scene cites unknown evidence", scene_id=scene_id))
    if set(expected_scenes) != {attrs.get("id", "") for attrs in parser.scenes}:
        issues.append(_issue("scene_identity", "HTML does not contain every planned scene exactly once"))

    narration_audio = [
        attrs for attrs in parser.audio
        if attrs.get("src", "").split("?", 1)[0] == "assets/narration.wav"
    ]
    if len(narration_audio) != 1 or (
        narration_audio and "clip" not in _classes(narration_audio[0])
    ):
        issues.append(_issue("narration_audio_missing", "one literal clip narration audio element is required"))
    else:
        try:
            audio_start = float(narration_audio[0].get("data-start", "nan"))
            audio_duration = float(narration_audio[0].get("data-duration", "nan"))
        except ValueError:
            audio_start = audio_duration = math.nan
        if (
            not math.isfinite(audio_start)
            or abs(audio_start) > 1e-6
            or not math.isfinite(audio_duration)
            or abs(audio_duration - float(plan["duration_s"])) > 1e-6
        ):
            issues.append(_issue("narration_audio_timing", "narration audio timing differs from plan"))
    if not parser.subtitle_toggles:
        issues.append(_issue("subtitle_toggle_missing", "subtitles must have an explicit UI toggle"))
    elif not any(item.get("aria-pressed") in {"true", "false"} for item in parser.subtitle_toggles):
        issues.append(_issue("subtitle_toggle_invalid", "subtitle toggle must expose aria-pressed state"))
    elif not any(
        item.get("aria-pressed") == "false"
        and item.get("aria-controls") in parser.elements_by_id
        and "hidden" in parser.elements_by_id[item["aria-controls"]]
        for item in parser.subtitle_toggles
    ):
        issues.append(
            _issue(
                "subtitle_default_state",
                "subtitles must default off and control a hidden local overlay",
            )
        )

    scripts = "\n".join(parser.scripts)
    if _NETWORK_SCRIPT.search(scripts):
        issues.append(_issue("non_seekable_or_network_script", "frame-clock or network script is forbidden"))
    if re.search(r"<script\b[^>]+\bsrc\s*=", text, re.IGNORECASE):
        issues.append(_issue("non_seekable_or_network_script", "external script sources are forbidden"))
    if parser.forbidden_tags:
        issues.append(
            _issue(
                "unsafe_embedded_content",
                "iframes, forms, objects, embeds, and base URL overrides are forbidden",
                tags=sorted(set(parser.forbidden_tags)),
            )
        )
    if parser.srcsets:
        issues.append(_issue("unsafe_local_asset", "srcset is forbidden; stage one explicit local source image"))
    if re.search(r"@import|url\(\s*['\"]?\s*(?:https?:|//|data:|blob:)", text, re.IGNORECASE):
        issues.append(_issue("remote_asset", "remote or inline CSS assets are forbidden"))

    narration_reference = "assets/narration.wav"
    for tag, attr, reference in parser.references:
        lowered = reference.lower()
        if lowered.startswith(_REMOTE_PREFIXES):
            issues.append(_issue("remote_asset", "remote assets are forbidden", reference=reference))
            continue
        if lowered.startswith("data:"):
            issues.append(_issue("data_url", "data URLs are forbidden", reference=reference))
            continue
        if lowered.startswith(("javascript:", "blob:")):
            issues.append(_issue("unsafe_local_asset", "executable or blob URL is forbidden", reference=reference))
            continue
        if reference.startswith("#"):
            continue
        if tag == "a" and attr == "href":
            issues.append(_issue("unsafe_local_asset", "video project links must be local fragment navigation", reference=reference))
            continue
        if reference.split("?", 1)[0] == narration_reference and not (project / narration_reference).exists():
            continue
        try:
            _safe_project_file(project, reference)
        except VideoContractError as error:
            issues.append(_issue("unsafe_local_asset", str(error), reference=reference))

    catalog = dict(visual_catalog or {})
    for image in parser.images:
        visual_id = image.get("data-source-id", "")
        if not visual_id:
            issues.append(_issue("image_source_binding", "source image has no data-source-id"))
            continue
        expected_visual = catalog.get(visual_id)
        if expected_visual is None:
            if catalog:
                issues.append(_issue("unknown_source_visual", "image cites unknown source visual", visual_id=visual_id))
            continue
        expected_hash = expected_visual.get("sha256", "")
        try:
            asset = _safe_project_file(project, image.get("src", ""))
            source = Path(expected_visual.get("path", "")).absolute()
            if source.is_symlink() or not source.is_file() or source.stat().st_nlink != 1:
                raise VideoContractError("catalog source visual is missing, symlinked, or hard-linked")
            if sha256_file(asset) != expected_hash or sha256_file(source) != expected_hash:
                issues.append(_issue("source_visual_hash_mismatch", "local image does not match the bound source visual", visual_id=visual_id))
        except VideoContractError as error:
            issues.append(_issue("unsafe_local_asset", str(error), visual_id=visual_id))
    return {
        "passed": not issues,
        "issues": issues,
        "scene_count": len(parser.scenes),
        "timeline_duration_s": plan["duration_s"],
        "subtitle_toggle": bool(parser.subtitle_toggles),
        "composition_root_count": len(parser.roots),
        "index_sha256": sha256_file(index),
    }


def _runtime_env(runtime: Mapping[str, str]) -> dict[str, str]:
    safe_names = {
        "COMSPEC", "HOMEDRIVE", "HOMEPATH", "LANG", "LC_ALL", "PATH",
        "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USERPROFILE", "WINDIR",
    }
    env = {key: value for key, value in os.environ.items() if key in safe_names}
    env.update(
        {
            "HOME": str(runtime["home_dir"]),
            "HYPERFRAMES_PYTHON": str(runtime["python"]),
            "HYPERFRAMES_NO_TELEMETRY": "1",
            "HYPERFRAMES_NO_UPDATE_CHECK": "1",
            "HYPERFRAMES_SKIP_SKILLS": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    command_log = runtime.get("command_log")
    if command_log:
        env["AUTODESIGN_VIDEO_TEST_LOG"] = str(command_log)
    return env


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            process.terminate()
    else:
        process.terminate()
    try:
        process.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
        else:
            process.kill()


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    stage: str,
    failure_class: str,
) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            list(command), cwd=cwd, env=dict(env), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
    except OSError as error:
        raise StageError(stage, f"could not start {Path(command[0]).name}: {error}", failure_class="runtime") from error
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _terminate(process)
        stdout, stderr = process.communicate()
        raise StageError(stage, f"{Path(command[0]).name} timed out after {timeout:g}s", failure_class="runtime") from error
    result = subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)
    if result.returncode != 0:
        detail = ((stderr or "") + "\n" + (stdout or "")).strip()
        raise StageError(
            stage,
            f"{Path(command[0]).name} exited {result.returncode}: {detail[-2000:]}",
            failure_class=failure_class,
        )
    return result


def _write_json(path: Path, value: object) -> None:
    core.atomic_write_json(path, value)


def _write_text(path: Path, text: str) -> None:
    core.atomic_write_bytes(path, text.encode("utf-8"))


def _probe_audio(path: Path, runtime: Mapping[str, str], env: Mapping[str, str]) -> float:
    result = _run(
        [
            str(runtime["ffprobe"]), "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        cwd=path.parent,
        env=env,
        timeout=30,
        stage="narration",
        failure_class="runtime",
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError as error:
        raise StageError("narration", "ffprobe returned an invalid WAV duration", failure_class="runtime") from error
    if not math.isfinite(duration) or duration <= 0:
        raise StageError("narration", "narration WAV duration must be positive", failure_class="runtime")
    return duration


def _timestamp(seconds: float, *, vtt: bool = False) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    separator = "." if vtt else ","
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def _synthesize_narration(
    project: Path,
    plan: Mapping[str, Any],
    runtime: Mapping[str, str],
    env: Mapping[str, str],
) -> tuple[list[dict[str, Any]], Path]:
    scene_dir = project / "narration" / "scenes"
    scene_dir.mkdir(parents=True, exist_ok=True)
    segments: list[dict[str, Any]] = []
    for index, scene in enumerate(plan["scenes"], start=1):
        scene_id = scene["scene_id"]
        text_path = scene_dir / f"{index:02d}-{scene_id}.txt"
        wav_path = scene_dir / f"{index:02d}-{scene_id}.wav"
        _write_text(text_path, scene["narration"] + "\n")
        measured = math.inf
        speed = 1.0
        for refit in range(4):
            wav_path.unlink(missing_ok=True)
            started = time.time_ns()
            _run(
                [
                    str(runtime["hyperframes"]), "tts", str(text_path.relative_to(project)),
                    "--output", str(wav_path.relative_to(project)), "--voice", str(plan["voice_id"]),
                    "--lang", "en-us", "--speed", f"{speed:.2f}", "--json",
                ],
                cwd=project,
                env=env,
                timeout=900,
                stage="narration",
                failure_class="runtime",
            )
            if (
                not wav_path.is_file()
                or wav_path.is_symlink()
                or wav_path.stat().st_size <= 0
                or wav_path.stat().st_mtime_ns + FRESH_MTIME_TOLERANCE_NS < started
            ):
                raise StageError("narration", f"{scene_id} TTS did not produce fresh audio", failure_class="runtime")
            measured = _probe_audio(wav_path, runtime, env)
            available = float(scene["duration_s"]) - SPEECH_END_MARGIN_S
            if measured <= available + 0.05:
                break
            requested = math.ceil((speed * measured / available) * 1.02 * 100) / 100
            if refit == 3 or requested > MAX_TTS_SPEED:
                raise StageError(
                    "narration",
                    f"narration_timing_unfit scene={scene_id} measured={measured:.3f}s available={available:.3f}s max_speed={MAX_TTS_SPEED:.2f}",
                    failure_class="authoring",
                )
            speed = min(MAX_TTS_SPEED, max(speed + 0.01, requested))
        segments.append(
            {
                "scene_id": scene_id,
                "start_s": scene["start_s"],
                "scene_duration_s": scene["duration_s"],
                "speech_duration_s": round(measured, 6),
                "speech_end_s": round(float(scene["start_s"]) + measured, 6),
                "speed": speed,
                "text_path": text_path.relative_to(project).as_posix(),
                "wav_path": wav_path.relative_to(project).as_posix(),
                "wav_sha256": sha256_file(wav_path),
            }
        )

    narration = project / "assets" / "narration.wav"
    narration.parent.mkdir(parents=True, exist_ok=True)
    narration.unlink(missing_ok=True)
    temporary = narration.with_name(f".{narration.name}.{uuid.uuid4().hex}.wav")
    command = [
        str(runtime["ffmpeg"]), "-v", "error", "-f", "lavfi", "-t", str(plan["duration_s"]),
        "-i", "anullsrc=r=24000:cl=mono",
    ]
    filters: list[str] = []
    inputs = ["[0:a]"]
    for index, segment in enumerate(segments, start=1):
        command.extend(["-i", segment["wav_path"]])
        delay_ms = int(round(float(segment["start_s"]) * 1000))
        label = f"speech{index}"
        filters.append(f"[{index}:a]adelay={delay_ms}:all=1[{label}]")
        inputs.append(f"[{label}]")
    filters.append(
        "".join(inputs)
        + f"amix=inputs={len(inputs)}:duration=first:dropout_transition=0,"
        + f"atrim=duration={plan['duration_s']}[narration]"
    )
    command.extend(
        [
            "-filter_complex", ";".join(filters), "-map", "[narration]", "-ar", "24000",
            "-ac", "1", "-c:a", "pcm_s16le", "-y", str(temporary),
        ]
    )
    _run(command, cwd=project, env=env, timeout=300, stage="narration", failure_class="runtime")
    if not temporary.is_file() or temporary.stat().st_size <= 0:
        temporary.unlink(missing_ok=True)
        raise StageError("narration", "ffmpeg did not create the timed narration mix", failure_class="runtime")
    mixed_duration = _probe_audio(temporary, runtime, env)
    if abs(mixed_duration - float(plan["duration_s"])) > 0.10:
        temporary.unlink(missing_ok=True)
        raise StageError(
            "narration",
            f"timed narration mix duration {mixed_duration:.3f}s differs from plan {plan['duration_s']}s",
            failure_class="runtime",
        )
    temporary.replace(narration)
    return segments, narration


def _write_subtitles(project: Path, plan: Mapping[str, Any], segments: Sequence[Mapping[str, Any]]) -> dict[str, Path]:
    narration_dir = project / "narration"
    narration_dir.mkdir(parents=True, exist_ok=True)
    transcript = narration_dir / "transcript.en.txt"
    srt = narration_dir / "subtitles.en.srt"
    vtt = narration_dir / "subtitles.en.vtt"
    timing = narration_dir / "timing.json"
    metadata = narration_dir / "voice-and-subtitles.json"
    transcript_lines: list[str] = []
    srt_lines: list[str] = []
    vtt_lines = ["WEBVTT", ""]
    for index, (scene, segment) in enumerate(zip(plan["scenes"], segments), start=1):
        start = float(scene["start_s"])
        end = float(segment["speech_end_s"])
        narration = scene["narration"]
        transcript_lines.extend([f"[{scene['scene_id']}] {scene['title']}", narration, ""])
        srt_lines.extend([str(index), f"{_timestamp(start)} --> {_timestamp(end)}", narration, ""])
        vtt_lines.extend([scene["scene_id"], f"{_timestamp(start, vtt=True)} --> {_timestamp(end, vtt=True)}", narration, ""])
    _write_text(transcript, "\n".join(transcript_lines).rstrip() + "\n")
    _write_text(srt, "\n".join(srt_lines).rstrip() + "\n")
    _write_text(vtt, "\n".join(vtt_lines).rstrip() + "\n")
    _write_json(timing, {"format_version": FORMAT_VERSION, "duration_s": plan["duration_s"], "scenes": list(segments)})
    _write_json(
        metadata,
        {
            "format_version": FORMAT_VERSION,
            "language": "en",
            "iso_639_2_language": "eng",
            "voice_id": plan["voice_id"],
            "engine": "hyperframes@0.7.86/kokoro-onnx",
            "subtitles_selectable": True,
            "subtitles_forced": False,
            "html_toggle_required": True,
            "html_subtitles_default_on": False,
            "transcript_sha256": sha256_file(transcript),
            "srt_sha256": sha256_file(srt),
            "vtt_sha256": sha256_file(vtt),
        },
    )
    return {"transcript": transcript, "srt": srt, "vtt": vtt, "timing": timing, "metadata": metadata}


def _fraction(value: object) -> float:
    text = str(value or "")
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            return float(numerator) / float(denominator)
        return float(text)
    except (ValueError, ZeroDivisionError):
        return math.nan


def validate_media_probe(
    payload: Mapping[str, Any],
    *,
    expected_duration_s: float,
    smoke: bool = False,
) -> dict[str, Any]:
    issues: list[dict[str, object]] = []
    streams = payload.get("streams")
    if not isinstance(streams, list):
        streams = []
    video = next((item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "audio"), None)
    subtitle = next((item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "subtitle"), None)
    if video is None or video.get("codec_name") != "h264":
        issues.append(_issue("video_codec", "video must use H.264"))
    if video is None or (video.get("width"), video.get("height")) != (WIDTH, HEIGHT):
        issues.append(_issue("video_dimensions", "video must be exactly 1920x1080"))
    observed_fps = _fraction(video.get("avg_frame_rate")) if isinstance(video, Mapping) else math.nan
    if not math.isfinite(observed_fps) or abs(observed_fps - FPS) > 1e-6:
        issues.append(_issue("video_frame_rate", "video must be exactly 30 fps"))
    if video is None or video.get("pix_fmt") not in {"yuv420p", "yuvj420p"}:
        issues.append(_issue("video_pixel_format", "video must use a broadly playable 4:2:0 format"))
    if audio is None or audio.get("codec_name") != "aac":
        issues.append(_issue("audio_codec", "video must contain AAC narration audio"))
    if subtitle is None or subtitle.get("codec_name") not in {"mov_text", "tx3g"}:
        issues.append(_issue("subtitle_codec", "video must contain a selectable MP4 subtitle track"))
    tags = subtitle.get("tags", {}) if isinstance(subtitle, Mapping) else {}
    if not isinstance(tags, Mapping) or str(tags.get("language", "")).lower() not in {"eng", "en"}:
        issues.append(_issue("subtitle_language", "subtitle track language must be English"))
    disposition = subtitle.get("disposition", {}) if isinstance(subtitle, Mapping) else {}
    forced_value = disposition.get("forced", 0) if isinstance(disposition, Mapping) else 0
    try:
        forced = int(forced_value)
    except (TypeError, ValueError):
        forced = -1
    if forced != 0:
        issues.append(_issue("subtitle_forced", "subtitle track must not be forced"))
    format_value = payload.get("format")
    try:
        duration = float(format_value.get("duration")) if isinstance(format_value, Mapping) else math.nan
    except (TypeError, ValueError):
        duration = math.nan
    tolerance = max(0.20 if smoke else 0.30, float(expected_duration_s) / FPS + 0.05)
    if not math.isfinite(duration) or abs(duration - float(expected_duration_s)) > tolerance:
        issues.append(_issue("media_duration", "media duration differs from the deterministic plan", observed=duration))
    return {
        "passed": not issues,
        "issues": issues,
        "video_codec": video.get("codec_name") if isinstance(video, Mapping) else None,
        "audio_codec": audio.get("codec_name") if isinstance(audio, Mapping) else None,
        "width": video.get("width") if isinstance(video, Mapping) else None,
        "height": video.get("height") if isinstance(video, Mapping) else None,
        "fps": observed_fps if math.isfinite(observed_fps) else None,
        "duration_s": duration,
        "subtitle_codec": subtitle.get("codec_name") if isinstance(subtitle, Mapping) else None,
        "subtitle_language": tags.get("language") if isinstance(tags, Mapping) else None,
        "subtitle_forced": bool(forced),
    }


def _failure_report(
    project: Path,
    stages: list[dict[str, Any]],
    error: StageError,
) -> dict[str, Any]:
    report = {
        "format_version": FORMAT_VERSION,
        "passed": False,
        "failed_stage": error.stage,
        "failure_class": error.failure_class,
        "authoring_retryable": error.failure_class == "authoring",
        "error": str(error),
        "stages": stages,
    }
    _write_json(project / "delivery-report.json", report)
    return report


def _capture_frames(
    project: Path,
    mp4: Path,
    duration_s: float,
    runtime: Mapping[str, str],
    env: Mapping[str, str],
) -> tuple[Path, dict[str, str]]:
    frames = project / "frames"
    if frames.exists():
        shutil.rmtree(frames)
    frames.mkdir()
    frame_hashes: dict[str, str] = {}
    for index in range(6):
        timestamp = max(0.0, min(duration_s - 1 / FPS, duration_s * (index + 0.5) / 6))
        output = frames / f"frame-{index + 1:02d}.png"
        _run(
            [
                str(runtime["ffmpeg"]), "-v", "error", "-ss", f"{timestamp:.6f}", "-i", str(mp4),
                "-frames:v", "1", "-vf", "scale=640:360", "-y", str(output),
            ],
            cwd=project,
            env=env,
            timeout=60,
            stage="frames",
            failure_class="runtime",
        )
        if not output.is_file() or output.stat().st_size <= 0:
            raise StageError("frames", f"representative frame {index + 1} is missing", failure_class="runtime")
        frame_hashes[f"frame_{index + 1:02d}"] = sha256_file(output)
    contact = project / "contact-sheet.png"
    _run(
        [
            str(runtime["ffmpeg"]), "-v", "error", "-i", str(mp4),
            "-vf", f"fps=6/{duration_s:g},scale=640:360,tile=3x2:padding=8:margin=8",
            "-frames:v", "1", "-y", str(contact),
        ],
        cwd=project,
        env=env,
        timeout=120,
        stage="frames",
        failure_class="runtime",
    )
    if not contact.is_file() or contact.stat().st_size <= 0:
        raise StageError("frames", "contact sheet was not produced", failure_class="runtime")
    return contact, frame_hashes


def deliver_project(
    project_dir: Path | str,
    plan_value: Mapping[str, Any],
    runtime: Mapping[str, str],
    *,
    evidence_ids: set[str] | None = None,
    visual_catalog: Mapping[str, Mapping[str, str]] | None = None,
    smoke: bool = False,
) -> dict[str, Any]:
    """Produce a final MP4 only through the audited HyperFrames delivery order."""

    project = Path(project_dir).absolute()
    stages: list[dict[str, Any]] = []
    try:
        if runtime.get("status") != "ready" or runtime.get("hyperframes_version") != setup_video.HYPERFRAMES_VERSION:
            raise StageError("runtime", "exact HyperFrames 0.7.86 runtime is not ready", failure_class="runtime")
        for name in ("hyperframes", "ffmpeg", "ffprobe", "python", "home_dir"):
            if not runtime.get(name):
                raise StageError("runtime", f"runtime is missing {name}", failure_class="runtime")
        env = _runtime_env(runtime)
        version = _run(
            [str(runtime["hyperframes"]), "--version"], cwd=project, env=env,
            timeout=20, stage="runtime", failure_class="runtime",
        ).stdout.strip()
        if version != setup_video.HYPERFRAMES_VERSION:
            raise StageError("runtime", f"HyperFrames version is {version}, expected 0.7.86", failure_class="runtime")
        plan = normalize_plan(plan_value, smoke=smoke)

        structural = validate_project(
            project, plan, evidence_ids=evidence_ids, visual_catalog=visual_catalog, smoke=smoke
        )
        stages.append({"id": "structural", **structural})
        if not structural["passed"]:
            codes = ", ".join(item["code"] for item in structural["issues"])
            raise StageError("structural", f"structural validation failed: {codes}", failure_class="authoring")

        segments, narration = _synthesize_narration(project, plan, runtime, env)
        narration_hash = sha256_file(narration)
        stages.append(
            {
                "id": "narration",
                "passed": True,
                "scene_count": len(segments),
                "narration_sha256": narration_hash,
                "engine": "hyperframes@0.7.86/kokoro-onnx",
            }
        )

        subtitle_paths = _write_subtitles(project, plan, segments)
        stages.append(
            {
                "id": "subtitles",
                "passed": True,
                "language": "eng",
                "selectable": True,
                "forced": False,
                "hashes": {name: sha256_file(path) for name, path in subtitle_paths.items()},
            }
        )

        lint = _run(
            [str(runtime["hyperframes"]), "lint"], cwd=project, env=env,
            timeout=120, stage="full_lint", failure_class="authoring",
        )
        if not narration.is_file() or sha256_file(narration) != narration_hash:
            raise StageError("full_lint", "narration changed during full lint", failure_class="runtime")
        stages.append(
            {
                "id": "full_lint",
                "passed": True,
                "narration_sha256": narration_hash,
                "output": ((lint.stdout or "") + (lint.stderr or ""))[-2000:],
            }
        )

        renders = project / "renders"
        renders.mkdir(parents=True, exist_ok=True)
        raw_mp4 = renders / f"hyperframes-{time.time_ns()}-{uuid.uuid4().hex}.mp4"
        started = time.time_ns()
        render = _run(
            [
                str(runtime["hyperframes"]), "render", "--fps", "30", "--resolution", "landscape",
                "--strict", "--no-best-effort", "--output", str(raw_mp4.relative_to(project)), ".",
            ],
            cwd=project,
            env=env,
            timeout=1800,
            stage="render",
            failure_class="authoring",
        )
        if (
            not raw_mp4.is_file()
            or raw_mp4.stat().st_size <= 0
            or raw_mp4.stat().st_mtime_ns + FRESH_MTIME_TOLERANCE_NS < started
        ):
            raw_mp4.unlink(missing_ok=True)
            raise StageError("render", "HyperFrames did not produce a fresh non-empty MP4", failure_class="authoring")
        raw_hash = sha256_file(raw_mp4)
        muxed = renders / f"captioned-{uuid.uuid4().hex}.mp4"
        _run(
            [
                str(runtime["ffmpeg"]), "-v", "error", "-i", str(raw_mp4), "-i", str(subtitle_paths["srt"]),
                "-map", "0:v:0", "-map", "0:a:0", "-map", "1:0", "-c:v", "copy", "-c:a", "copy",
                "-c:s", "mov_text", "-metadata:s:s:0", "language=eng", "-disposition:s:0", "0", "-y", str(muxed),
            ],
            cwd=project,
            env=env,
            timeout=300,
            stage="render",
            failure_class="runtime",
        )
        if not muxed.is_file() or muxed.stat().st_size <= 0:
            raise StageError("render", "subtitle mux did not produce an MP4", failure_class="runtime")
        stages.append(
            {
                "id": "render",
                "passed": True,
                "renderer": "hyperframes@0.7.86",
                "hyperframes_mp4_sha256": raw_hash,
                "subtitle_mux_only": True,
                "output": ((render.stdout or "") + (render.stderr or ""))[-2000:],
            }
        )

        probe_result = _run(
            [
                str(runtime["ffprobe"]), "-v", "error", "-count_frames", "-show_streams",
                "-show_format", "-of", "json", str(muxed),
            ],
            cwd=project,
            env=env,
            timeout=60,
            stage="ffprobe",
            failure_class="runtime",
        )
        try:
            probe_payload = json.loads(probe_result.stdout)
        except json.JSONDecodeError as error:
            raise StageError("ffprobe", "ffprobe returned invalid JSON", failure_class="runtime") from error
        probe = validate_media_probe(probe_payload, expected_duration_s=float(plan["duration_s"]), smoke=smoke)
        if not probe["passed"]:
            codes = ", ".join(item["code"] for item in probe["issues"])
            raise StageError("ffprobe", f"media contract failed: {codes}", failure_class="authoring")
        _write_json(project / "media_probe.json", probe_payload)
        final_mp4 = project / "conference-video.mp4"
        final_mp4.unlink(missing_ok=True)
        muxed.replace(final_mp4)
        raw_mp4.unlink(missing_ok=True)
        stages.append({"id": "ffprobe", **probe})

        contact, frame_hashes = _capture_frames(
            project, final_mp4, float(plan["duration_s"]), runtime, env
        )
        stages.append(
            {
                "id": "frames",
                "passed": True,
                "contact_sheet_sha256": sha256_file(contact),
                "frame_hashes": frame_hashes,
            }
        )

        source_map = {
            "format_version": FORMAT_VERSION,
            "plan_sha256": hashlib.sha256(
                (json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            ).hexdigest(),
            "scenes": [
                {
                    "scene_id": scene["scene_id"],
                    "source_ids": scene["source_ids"],
                    "visual_ids": scene["visual_ids"],
                }
                for scene in plan["scenes"]
            ],
        }
        _write_json(project / "video-source-map.json", source_map)
        report: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "passed": True,
            "renderer": "hyperframes@0.7.86",
            "plain_ffmpeg_delivery": False,
            "stages": stages,
            "mp4_path": str(final_mp4),
            "mp4_sha256": sha256_file(final_mp4),
            "contact_sheet": str(contact),
            "contact_sheet_sha256": sha256_file(contact),
            "media_probe": probe,
            "media_probe_sha256": sha256_file(project / "media_probe.json"),
            "source_map_sha256": sha256_file(project / "video-source-map.json"),
            "semantic_review_required": True,
        }
        _write_json(project / "delivery-report.json", report)
        report["delivery_report_sha256"] = sha256_file(project / "delivery-report.json")
        return report
    except StageError as error:
        stale_outputs = [project / "conference-video.mp4"]
        if (project / "renders").is_dir():
            stale_outputs.extend((project / "renders").glob("*.mp4"))
        for stale in stale_outputs:
            stale.unlink(missing_ok=True)
        return _failure_report(project, stages, error)
    except (OSError, VideoContractError) as error:
        return _failure_report(project, stages, StageError("runtime", str(error), failure_class="runtime"))


def _copy_tree_regular(source: Path, destination: Path) -> list[str]:
    if source.is_symlink() or not source.is_dir():
        raise VideoContractError("delivered project must be a regular directory")
    if destination.exists() and any(destination.iterdir()):
        raise VideoContractError("attempt artifact directory must be empty")
    paths: list[str] = []
    for current, directories, files in os.walk(source, followlinks=False):
        current_path = Path(current)
        for name in directories:
            if (current_path / name).is_symlink():
                raise VideoContractError("delivered project contains a symlinked directory")
        for name in sorted(files):
            path = current_path / name
            if path.is_symlink() or path.stat().st_nlink != 1:
                raise VideoContractError("delivered project contains a symlink or hard link")
            relative = path.relative_to(source)
            target = destination / relative
            core.atomic_write_bytes(target, path.read_bytes())
            paths.append(f"artifact/{relative.as_posix()}")
    return sorted(paths)


def record_attempt_delivery(
    run_dir: Path | str,
    attempt_id: str,
    project_dir: Path | str,
    report: Mapping[str, Any],
    *,
    claims: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Persist a passing delivery into the portable hash-bound review lifecycle."""

    if report.get("passed") is not True or report.get("renderer") != "hyperframes@0.7.86":
        raise VideoContractError("only a passing HyperFrames 0.7.86 delivery can be recorded")
    run = Path(run_dir).absolute()
    attempt = core.safe_path(run / "attempts", attempt_id, must_exist=True)
    core.safe_path(attempt, "qa/runtime-failure.json").unlink(missing_ok=True)
    artifact = core.safe_path(attempt, "artifact", must_exist=True)
    paths = _copy_tree_regular(Path(project_dir).absolute(), artifact)
    core.write_source_map(run, attempt_id, claims)
    preview_dir = core.safe_path(attempt, "qa/previews", must_exist=True)
    previews: dict[str, str] = {}
    contact = artifact / "contact-sheet.png"
    contact_preview = preview_dir / "contact-sheet.png"
    core.atomic_write_bytes(contact_preview, contact.read_bytes())
    previews["contact_sheet"] = "qa/previews/contact-sheet.png"
    for frame in sorted((artifact / "frames").glob("frame-*.png")):
        target = preview_dir / frame.name
        core.atomic_write_bytes(target, frame.read_bytes())
        previews[frame.stem.replace("-", "_")] = f"qa/previews/{frame.name}"
    checks = [
        {
            "id": stage.get("id", "unknown"),
            "passed": stage.get("passed") is True,
            "details": {key: value for key, value in stage.items() if key not in {"id", "passed"}},
        }
        for stage in report.get("stages", [])
        if isinstance(stage, Mapping)
    ]
    return core.record_deterministic_result(
        run,
        attempt_id,
        passed=True,
        checks=checks,
        artifact_paths=paths,
        preview_paths=previews,
    )


def record_delivery_failure(
    run_dir: Path | str,
    attempt_id: str,
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist failure routing without charging runtime failures as new attempts."""
    if report.get("passed") is not False:
        raise VideoContractError("delivery failure routing requires a failed report")
    failure_class = report.get("failure_class")
    if failure_class not in {"authoring", "runtime"}:
        raise VideoContractError("delivery failure has no valid routing class")
    run = Path(run_dir).absolute()
    state = _read_json(run / "run.json")
    if state.get("state") != "authoring" or state.get("active_attempt") != attempt_id:
        raise VideoContractError("delivery failure does not match the active authoring attempt")
    attempt = core.safe_path(run / "attempts", attempt_id, must_exist=True)
    marker = core.safe_path(attempt, "qa/runtime-failure.json")
    if failure_class == "authoring":
        marker.unlink(missing_ok=True)
        core.mark_side_state(
            run,
            "failed",
            reason=f"{report.get('failed_stage')}: {report.get('error')}",
        )
        return {"next_action": "repair_authoring_in_next_attempt"}
    payload = core.redact_secrets(
        {
            "format_version": FORMAT_VERSION,
            "attempt_id": attempt_id,
            "failure_class": "runtime",
            "failed_stage": str(report.get("failed_stage", "runtime")),
            "error": str(report.get("error", "runtime delivery failure")),
        }
    )
    core.atomic_write_json(marker, payload)
    return {
        "next_action": "repair_runtime_and_resume_same_attempt",
        "runtime_failure": payload,
    }


def resume_video_run(run_dir: Path | str) -> dict[str, Any]:
    """Resume core state while preserving a durable runtime-repair route."""
    run = Path(run_dir).absolute()
    result = core.resume_run(run, skill_root=SKILL_ROOT)
    attempt_id = result.get("active_attempt")
    if result.get("state") != "authoring" or not isinstance(attempt_id, str):
        return result
    attempt = core.safe_path(run / "attempts", attempt_id, must_exist=True)
    marker = core.safe_path(attempt, "qa/runtime-failure.json")
    if not marker.exists():
        return result
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_nlink != 1:
        raise VideoContractError("runtime failure marker is not a regular non-linked file")
    failure = _read_json(marker)
    if (
        failure.get("format_version") != FORMAT_VERSION
        or failure.get("attempt_id") != attempt_id
        or failure.get("failure_class") != "runtime"
        or not isinstance(failure.get("failed_stage"), str)
        or not isinstance(failure.get("error"), str)
    ):
        raise VideoContractError("runtime failure marker is invalid")
    return {
        **result,
        "next_action": "repair_runtime_and_resume_same_attempt",
        "runtime_failure": failure,
    }


def begin_video_attempt(run_dir: Path | str) -> str:
    """Begin one bounded video authoring attempt from the persisted plan."""

    run = Path(run_dir).absolute()
    plan = normalize_plan(_read_json(run / "plan.json"))
    state = _read_json(run / "run.json")
    if state.get("state") == "authoring" and isinstance(state.get("active_attempt"), str):
        return str(state["active_attempt"])
    if int(state.get("attempt_count", 0)) >= int(plan["max_attempts"]):
        raise VideoContractError(
            f"video attempt budget exhausted ({plan['max_attempts']}); do not create an unreviewed fallback"
        )
    return core.begin_attempt(run)


def synthetic_smoke_plan() -> dict[str, Any]:
    scenes = []
    duration = 6.0
    for index in range(3):
        scenes.append(
            {
                "scene_id": f"scene_{index + 1:02d}",
                "title": ("Question", "Method", "Evidence")[index],
                "role": ("opening", "method", "results")[index],
                "start_s": index * 2.0,
                "duration_s": 2.0,
                "narration": ("What is the question?", "We test the method.", "The evidence is clear.")[index],
                "source_ids": ["ev-smoke"],
                "visual_ids": [],
            }
        )
    return normalize_plan(
        {
            "format_version": FORMAT_VERSION,
            "artifact_type": "video",
            "width": WIDTH,
            "height": HEIGHT,
            "fps": FPS,
            "scene_count": len(scenes),
            "duration_s": duration,
            "voice_id": "af_heart",
            "language": "en",
            "scenes": scenes,
            "max_attempts": 1,
        },
        smoke=True,
    )


def write_synthetic_smoke_project(project_dir: Path | str, plan: Mapping[str, Any]) -> Path:
    project = Path(project_dir).absolute()
    if project.exists() or project.is_symlink():
        raise VideoContractError(f"synthetic smoke project already exists: {project}")
    (project / "assets").mkdir(parents=True)
    sections = []
    colors = ("#132238", "#193d3b", "#3d2638")
    for index, scene in enumerate(plan["scenes"]):
        sections.append(
            f'<section id="{scene["scene_id"]}" class="clip" data-hf-clip="true" '
            f'data-start="{scene["start_s"]}" data-duration="{scene["duration_s"]}" '
            f'data-track-index="1" data-source-ids="ev-smoke" '
            f'style="background:{colors[index]}"><p>AutoDesign video lifecycle smoke</p>'
            f'<h1>{scene["title"]}</h1></section>'
        )
    html = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><style>
html,body{{margin:0;width:1920px;height:1080px;overflow:hidden;background:#101820;color:#fff;font-family:Arial,sans-serif}}
[data-composition-id]{{position:relative;width:1920px;height:1080px}}.clip{{position:absolute;inset:0;display:grid;place-content:center;padding:120px}}
h1{{font-size:120px;margin:20px 0}}p{{font-size:34px}}.subtitle-overlay[hidden]{{display:none}}
</style></head><body><main data-composition-id="smoke" data-start="0" data-duration="{plan["duration_s"]}" data-width="1920" data-height="1080" data-no-timeline>
{''.join(sections)}<audio id="narration" class="clip" src="assets/narration.wav" data-start="0" data-duration="{plan["duration_s"]}" data-track-index="2" data-media-start="0"></audio>
<button type="button" data-subtitle-toggle aria-pressed="false" aria-controls="subtitles">CC</button><div id="subtitles" class="subtitle-overlay" hidden></div></main>
<script>document.querySelector('[data-subtitle-toggle]').addEventListener('click',event=>{{const button=event.currentTarget;const target=document.getElementById('subtitles');const shown=button.getAttribute('aria-pressed')==='true';button.setAttribute('aria-pressed',String(!shown));target.hidden=shown;}});</script></body></html>'''
    _write_text(project / "index.html", html)
    _write_json(project / "hyperframes.json", {"version": 1, "entry": "index.html"})
    return project


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise VideoContractError(f"JSON must be an object: {path}")
    return value


def _source_contract(run: Path) -> tuple[set[str], dict[str, dict[str, str]]]:
    evidence = {str(item["id"]) for item in core.load_evidence(run)}
    value = _read_json(run / "evidence" / "source_visuals.json")
    catalog: dict[str, dict[str, str]] = {}
    for item in value.get("visuals", []):
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            continue
        relative = str(item.get("path", ""))
        catalog[item["id"]] = {
            "path": str(run / "evidence" / relative),
            "sha256": str(item.get("sha256", "")),
        }
    return evidence, catalog


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "setup"):
        command = commands.add_parser(name)
        command.add_argument("--cache-root", type=Path)
    init = commands.add_parser("init")
    init.add_argument("run", type=Path)
    init.add_argument("--release-version", default=RELEASE_VERSION)
    init.add_argument("--archive-sha256")
    evidence = commands.add_parser("evidence")
    evidence.add_argument("run", type=Path)
    evidence.add_argument("source", type=Path)
    evidence.add_argument("--asset", action="append", default=[], type=Path)
    evidence.add_argument("--reference", action="append", default=[], type=Path)
    visuals = commands.add_parser("bind-visuals")
    visuals.add_argument("run", type=Path)
    visuals.add_argument("review", type=Path)
    plan = commands.add_parser("plan")
    plan.add_argument("run", type=Path)
    plan.add_argument("plan_json", type=Path)
    attempt = commands.add_parser("begin-attempt")
    attempt.add_argument("run", type=Path)
    validate = commands.add_parser("validate")
    validate.add_argument("project", type=Path)
    validate.add_argument("plan_json", type=Path)
    validate.add_argument("--run", type=Path, required=True)
    deliver = commands.add_parser("deliver")
    deliver.add_argument("project", type=Path)
    deliver.add_argument("plan_json", type=Path)
    deliver.add_argument("--cache-root", type=Path)
    deliver.add_argument("--run", type=Path, required=True)
    deliver.add_argument("--attempt", required=True)
    deliver.add_argument("--claims", type=Path, required=True)
    context = commands.add_parser("review-context")
    context.add_argument("run", type=Path)
    context.add_argument("attempt")
    review = commands.add_parser("record-review")
    review.add_argument("run", type=Path)
    review.add_argument("attempt")
    review.add_argument("review_json", type=Path)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("run", type=Path)
    finalize.add_argument("attempt")
    resume = commands.add_parser("resume")
    resume.add_argument("run", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            result = setup_video.doctor_video_runtime(cache_root=args.cache_root)
        elif args.command == "setup":
            result = setup_video.ensure_video_runtime(cache_root=args.cache_root).as_dict()
        elif args.command == "init":
            result = core.initialize_run(
                args.run, SKILL_ROOT, release_version=args.release_version, archive_sha256=args.archive_sha256
            )
        elif args.command == "evidence":
            result = core.prepare_source(
                args.run, args.source, extra_assets=args.asset, reference_images=args.reference
            )
        elif args.command == "bind-visuals":
            result = core.bind_host_vlm_visuals(args.run, _read_json(args.review))
        elif args.command == "plan":
            result = core.save_plan(args.run, normalize_plan(_read_json(args.plan_json)))
        elif args.command == "begin-attempt":
            result = {"attempt_id": begin_video_attempt(args.run)}
        elif args.command == "validate":
            evidence_ids, catalog = _source_contract(args.run)
            result = validate_project(
                args.project, _read_json(args.plan_json), evidence_ids=evidence_ids, visual_catalog=catalog
            )
        elif args.command == "deliver":
            claims_value = json.loads(args.claims.read_text(encoding="utf-8"))
            if not isinstance(claims_value, list):
                raise VideoContractError("claims JSON must be a list")
            runtime = setup_video.require_video_runtime(cache_root=args.cache_root).as_dict()
            evidence_ids, catalog = _source_contract(args.run)
            result = deliver_project(
                args.project, _read_json(args.plan_json), runtime,
                evidence_ids=evidence_ids, visual_catalog=catalog,
            )
            if result.get("passed") is True:
                result["deterministic_result"] = record_attempt_delivery(
                    args.run, args.attempt, args.project, result, claims=claims_value
                )
            else:
                result.update(record_delivery_failure(args.run, args.attempt, result))
        elif args.command == "review-context":
            result = core.create_review_context(args.run, args.attempt, rubric=REVIEW_RUBRIC)
        elif args.command == "record-review":
            result = core.record_semantic_review(
                args.run, args.attempt, _read_json(args.review_json)
            )
        elif args.command == "finalize":
            result = core.finalize_attempt(args.run, args.attempt)
        else:
            result = resume_video_run(args.run)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if isinstance(result, Mapping) and result.get("passed") is False:
            return 2
        if isinstance(result, Mapping) and result.get("ready") is False:
            return 2
        return 0
    except (
        OSError, json.JSONDecodeError, VideoContractError, setup_video.VideoRuntimeError,
        core.PortableError,
    ) as error:
        print(json.dumps({"status": "error", "error": str(error)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
