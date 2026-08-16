#!/usr/bin/env python3
"""Standalone, source-grounded HyperFrames conference-video harness."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
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
_VISIBLE_NUMBER = re.compile(r"(?<![A-Za-z0-9_])[-+]?(?:\d+(?:\.\d+)?|\.\d+)%?(?![A-Za-z0-9_])")
_CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE | re.DOTALL)
_VISUAL_ROLE_MAP = {
    "opening": "overview",
    "problem": "context",
    "analysis": "comparison",
    "results": "result",
    "limitations": "supporting",
    "implications": "supporting",
    "closing": "overview",
}

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

    def __init__(
        self,
        stage: str,
        message: str,
        *,
        failure_class: str,
        runtime_diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.failure_class = failure_class
        self.runtime_diagnostics = dict(runtime_diagnostics or {})


def sha256_file(path: Path | str) -> str:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise VideoContractError(f"expected a regular non-symlink file: {source}")
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def load_canonical_delivery_plan(
    run_dir: Path | str,
    supplied_plan_path: Path | str,
) -> tuple[dict[str, Any], str]:
    run = Path(run_dir).absolute()
    canonical_path = core.safe_path(run, "plan.json", must_exist=True)
    supplied = Path(supplied_plan_path).absolute()
    if supplied.is_symlink() or not supplied.is_file() or supplied.stat().st_nlink != 1:
        raise VideoContractError("supplied delivery plan must be a regular non-linked file")
    canonical_digest = sha256_file(canonical_path)
    if sha256_file(supplied) != canonical_digest:
        raise VideoContractError("delivery plan must be byte-identical to the canonical run plan")
    canonical = normalize_plan(_read_json(canonical_path))
    if normalize_plan(_read_json(supplied)) != canonical:
        raise VideoContractError("delivery plan differs from the canonical run plan")
    return canonical, canonical_digest


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
        title_claim_id = str(scene_value.get("title_claim_id", "")).strip()
        narration_claim_id = str(scene_value.get("narration_claim_id", "")).strip()
        visible_claim_ids = scene_value.get("visible_claim_ids", [])
        for claim_field, claim_id in (
            ("title_claim_id", title_claim_id),
            ("narration_claim_id", narration_claim_id),
        ):
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{1,127}", claim_id):
                raise VideoContractError(f"{scene_id}.{claim_field} is required and invalid")
        if (
            not isinstance(visible_claim_ids, list)
            or not visible_claim_ids
            or any(not isinstance(item, str) or not item.strip() for item in visible_claim_ids)
        ):
            raise VideoContractError(f"{scene_id}.visible_claim_ids requires claim ids")
        normalized_visible_claim_ids = list(dict.fromkeys(item.strip() for item in visible_claim_ids))
        if title_claim_id not in normalized_visible_claim_ids:
            raise VideoContractError(f"{scene_id}.visible_claim_ids must include title_claim_id")
        visual_role = str(scene_value.get("visual_role", _VISUAL_ROLE_MAP.get(role, role))).strip()
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
                "visual_role": visual_role,
                "title_claim_id": title_claim_id,
                "narration_claim_id": narration_claim_id,
                "visible_claim_ids": normalized_visible_claim_ids,
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
        self.duplicate_attributes: list[dict[str, str]] = []
        self.inline_event_handlers: list[dict[str, str]] = []
        self.meta_refreshes: list[dict[str, str]] = []
        self.scene_text: dict[str, list[str]] = {}
        self.claim_text: dict[str, list[str]] = {}
        self._script_depth = 0
        self._scene_stack: list[str] = []
        self._claim_stack: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = [name.lower() for name, _value in attrs]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            self.duplicate_attributes.append({"tag": tag.lower(), "attributes": " ".join(duplicates)})
        values = {name.lower(): value or "" for name, value in attrs}
        for name in names:
            if name.startswith("on"):
                self.inline_event_handlers.append({"tag": tag.lower(), "attribute": name})
        if values.get("id"):
            self.elements_by_id[values["id"]] = values
        if "data-composition-id" in values:
            self.roots.append(values)
        if tag.lower() == "section":
            self.scenes.append(values)
            scene_id = values.get("id", "")
            self._scene_stack.append(scene_id)
            self.scene_text.setdefault(scene_id, [])
        if tag.lower() == "audio":
            self.audio.append(values)
        if tag.lower() == "img":
            image = dict(values)
            if self._scene_stack:
                image["_scene_id"] = self._scene_stack[-1]
            self.images.append(image)
        if "data-subtitle-toggle" in values:
            self.subtitle_toggles.append(values)
        if tag.lower() in {"base", "embed", "form", "iframe", "object"}:
            self.forbidden_tags.append(tag.lower())
        if tag.lower() == "meta" and values.get("http-equiv", "").strip().lower() == "refresh":
            self.meta_refreshes.append(values)
        claim_id = values.get("data-claim-id", "").strip()
        if claim_id:
            self._claim_stack.append((tag.lower(), claim_id))
            self.claim_text.setdefault(claim_id, [])
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
        if tag.lower() == "section" and self._scene_stack:
            self._scene_stack.pop()
        if self._claim_stack and self._claim_stack[-1][0] == tag.lower():
            self._claim_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._script_depth:
            self.scripts.append(data)
            return
        if self._scene_stack:
            self.scene_text.setdefault(self._scene_stack[-1], []).append(data)
        if self._claim_stack:
            self.claim_text.setdefault(self._claim_stack[-1][1], []).append(data)


def _issue(code: str, message: str, **context: object) -> dict[str, object]:
    return {"code": code, "message": message, **context}


def _safe_project_file(project: Path, reference: str) -> Path:
    raw = html.unescape(reference).split("#", 1)[0].split("?", 1)[0].strip()
    decoded = urllib.parse.unquote(raw)
    if (
        not decoded
        or decoded.startswith(("/", "~"))
        or "\\" in decoded
        or "\x00" in decoded
        or re.match(r"^[A-Za-z]:", decoded)
    ):
        raise VideoContractError(f"unsafe local asset path: {reference}")
    relative = Path(decoded)
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


def _claim_catalog(
    claims: Sequence[Mapping[str, Any]] | None,
    *,
    evidence_ids: set[str] | None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, object]]]:
    issues: list[dict[str, object]] = []
    if not claims:
        return {}, [_issue("claims_empty", "video titles, narration, and visible facts require non-empty evidence claims")]
    catalog: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(claims, start=1):
        if not isinstance(value, Mapping):
            issues.append(_issue("claim_invalid", "claim must be an object", claim_index=index))
            continue
        claim_id = str(value.get("id", "")).strip()
        text = " ".join(str(value.get("text", "")).split())
        source_ids = value.get("source_ids")
        if (
            not claim_id
            or not text
            or not isinstance(source_ids, list)
            or not source_ids
            or any(not isinstance(item, str) or not item for item in source_ids)
        ):
            issues.append(_issue("claim_invalid", "claim requires id, exact text, and source_ids", claim_id=claim_id))
            continue
        if claim_id in catalog:
            issues.append(_issue("claim_duplicate", "claim ids must be unique", claim_id=claim_id))
            continue
        normalized = dict(value)
        normalized.update({"id": claim_id, "text": text, "source_ids": list(dict.fromkeys(source_ids))})
        catalog[claim_id] = normalized
        if evidence_ids is not None and not set(normalized["source_ids"]).issubset(evidence_ids):
            issues.append(_issue("claim_unknown_evidence", "claim cites unknown evidence", claim_id=claim_id))
    return catalog, issues


def _normalized_visible_text(value: str) -> str:
    return " ".join(html.unescape(value).split())


def validate_project(
    project_dir: Path | str,
    plan_value: Mapping[str, Any],
    *,
    run_dir: Path | str | None = None,
    evidence_ids: set[str] | None = None,
    visual_catalog: Mapping[str, Mapping[str, Any]] | None = None,
    claims: Sequence[Mapping[str, Any]] | None = None,
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
    if parser.duplicate_attributes:
        issues.append(
            _issue(
                "duplicate_attribute",
                "duplicate HTML attributes are forbidden because browser parsing is ambiguous",
                elements=parser.duplicate_attributes,
            )
        )
    if parser.inline_event_handlers:
        issues.append(
            _issue(
                "inline_event_handler",
                "inline on* event handlers are forbidden; use one audited local script",
                elements=parser.inline_event_handlers,
            )
        )
    if parser.meta_refreshes:
        issues.append(_issue("meta_refresh", "meta refresh navigation is forbidden"))

    claim_catalog, claim_issues = _claim_catalog(claims, evidence_ids=evidence_ids)
    issues.extend(claim_issues)
    if run_dir is not None and claim_catalog:
        try:
            grounding = core.validate_grounding(list(claim_catalog.values()), core.load_evidence(run_dir))
            for error in grounding.get("errors", []):
                issues.append(
                    _issue(
                        "claim_grounding",
                        "claim does not pass shared evidence grounding",
                        grounding_code=str(error.get("code", "invalid")),
                        claim_id=str(error.get("claim_id", "")),
                    )
                )
        except core.PortableError as error:
            issues.append(_issue("claim_grounding", str(error)))

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
        title_claim_id = attrs.get("data-title-claim-id", "")
        narration_claim_id = attrs.get("data-narration-claim-id", "")
        visible_claim_ids = [item for item in attrs.get("data-claim-ids", "").split() if item]
        if title_claim_id != expected["title_claim_id"]:
            issues.append(_issue("title_claim_binding", "scene title claim id differs from plan", scene_id=scene_id))
        if narration_claim_id != expected["narration_claim_id"]:
            issues.append(_issue("narration_claim_binding", "scene narration claim id differs from plan", scene_id=scene_id))
        if visible_claim_ids != expected["visible_claim_ids"]:
            issues.append(_issue("visible_claim_binding", "scene visible claim ids differ from plan", scene_id=scene_id))
        title_claim = claim_catalog.get(expected["title_claim_id"])
        narration_claim = claim_catalog.get(expected["narration_claim_id"])
        if title_claim is None or title_claim.get("text") != expected["title"]:
            issues.append(_issue("title_claim_mismatch", "planned title must exactly equal its evidence claim", scene_id=scene_id))
        if narration_claim is None or narration_claim.get("text") != expected["narration"]:
            issues.append(_issue("narration_claim_mismatch", "planned narration must exactly equal its evidence claim", scene_id=scene_id))
        if " ".join(attrs.get("data-narration", "").split()) != expected["narration"]:
            issues.append(_issue("narration_html_mismatch", "HTML narration must exactly equal the planned narration", scene_id=scene_id))
        if title_claim is not None and set(title_claim.get("source_ids", [])) != expected_sources:
            issues.append(_issue("title_claim_source_binding", "title claim sources differ from scene sources", scene_id=scene_id))
        if narration_claim is not None and set(narration_claim.get("source_ids", [])) != expected_sources:
            issues.append(_issue("narration_claim_source_binding", "narration claim sources differ from scene sources", scene_id=scene_id))
        rendered_title = _normalized_visible_text("".join(parser.claim_text.get(expected["title_claim_id"], [])))
        if rendered_title != expected["title"]:
            issues.append(_issue("title_html_mismatch", "visible scene title must exactly equal the bound claim", scene_id=scene_id))
        supported_numbers: set[str] = set()
        for claim_id in expected["visible_claim_ids"]:
            claim = claim_catalog.get(claim_id)
            if claim is not None:
                supported_numbers.update(_VISIBLE_NUMBER.findall(str(claim.get("text", ""))))
        visible_numbers = set(
            _VISIBLE_NUMBER.findall(_normalized_visible_text("".join(parser.scene_text.get(scene_id, []))))
        )
        unsupported_numbers = sorted(visible_numbers - supported_numbers)
        if unsupported_numbers:
            issues.append(
                _issue(
                    "unbound_visible_number",
                    "visible numeric facts must appear in an explicitly bound evidence claim",
                    scene_id=scene_id,
                    numbers=unsupported_numbers,
                )
            )
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
    if re.search(r"\bnew\s+Image\s*\(", scripts, re.IGNORECASE):
        issues.append(_issue("dynamic_image", "dynamic Image construction is forbidden; stage source-bound visuals in HTML"))
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
    if re.search(r"@import", text, re.IGNORECASE):
        issues.append(_issue("remote_asset", "remote or inline CSS assets are forbidden"))
    for _quote, css_reference in _CSS_URL.findall(text):
        reference = html.unescape(css_reference).strip()
        if reference.startswith("#"):
            continue
        lowered = urllib.parse.unquote(reference).lower()
        if lowered.startswith(_REMOTE_PREFIXES) or lowered.startswith(("data:", "blob:")):
            issues.append(_issue("remote_asset", "remote or inline CSS assets are forbidden", reference=reference))
            continue
        try:
            _safe_project_file(project, reference)
        except VideoContractError as error:
            issues.append(_issue("unsafe_css_asset", str(error), reference=reference))

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
    actual_visuals_by_scene: dict[str, set[str]] = {}
    for image in parser.images:
        visual_id = image.get("data-source-id", "")
        if visual_id:
            actual_visuals_by_scene.setdefault(image.get("_scene_id", ""), set()).add(visual_id)
    for scene_id, scene in expected_scenes.items():
        expected_visuals = set(scene["visual_ids"])
        actual_visuals = actual_visuals_by_scene.pop(scene_id, set())
        if actual_visuals != expected_visuals:
            issues.append(
                _issue(
                    "scene_visual_binding",
                    "scene image source ids must exactly equal the canonical visual_ids",
                    scene_id=scene_id,
                    missing=sorted(expected_visuals - actual_visuals),
                    unplanned=sorted(actual_visuals - expected_visuals),
                )
            )
    for scene_id, actual_visuals in sorted(actual_visuals_by_scene.items()):
        issues.append(
            _issue(
                "scene_visual_binding",
                "source-bound image is outside a canonical scene",
                scene_id=scene_id,
                missing=[],
                unplanned=sorted(actual_visuals),
            )
        )
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
    if run_dir is not None:
        allocations = [
            {"visual_id": visual_id, "role": scene["visual_role"]}
            for scene in plan["scenes"]
            for visual_id in scene["visual_ids"]
        ]
        try:
            visual_plan = core.validate_visual_plan(run_dir, allocations)
            for error in visual_plan.get("errors", []):
                issues.append(
                    _issue(
                        str(error.get("code", "visual_plan")),
                        "source visual allocation is invalid",
                        **{key: value for key, value in dict(error).items() if key != "code"},
                    )
                )
        except core.PortableError as error:
            issues.append(_issue("visual_plan_invalid", str(error)))
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


def _vtt_cues(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    cues: list[str] = []
    index = 1 if lines and lines[0].strip() == "WEBVTT" else 0
    while index < len(lines):
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index >= len(lines):
            break
        if "-->" not in lines[index]:
            index += 1
        if index >= len(lines) or "-->" not in lines[index]:
            raise StageError("browser_preflight", "generated VTT has an invalid cue", failure_class="runtime")
        index += 1
        text_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            text_lines.append(lines[index].strip())
            index += 1
        cue = " ".join(text_lines).strip()
        if not cue:
            raise StageError("browser_preflight", "generated VTT contains an empty cue", failure_class="runtime")
        cues.append(cue)
    return cues


def _browser_preflight(
    project: Path,
    vtt: Path,
    runtime: Mapping[str, str],
    env: Mapping[str, str],
) -> dict[str, Any]:
    script = r"""
const path = require('path');
const {pathToFileURL, fileURLToPath} = require('url');
const puppeteer = require(path.join(process.argv[1], 'node_modules', 'puppeteer-core'));
const browserPath = process.argv[2];
const projectRoot = path.resolve(process.argv[3]);
const indexPath = path.join(projectRoot, 'index.html');
const inside = value => value === projectRoot || value.startsWith(projectRoot + path.sep);
(async () => {
  const browser = await puppeteer.launch({
    executablePath: browserPath,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });
    const blocked = [];
    const pageErrors = [];
  try {
    const page = await browser.newPage();
    await page.setViewport({width: 1920, height: 1080, deviceScaleFactor: 1});
    await page.setRequestInterception(true);
    page.on('request', request => {
      try {
        const url = request.url();
        if (!url.startsWith('file:')) {
          blocked.push(url); request.abort(); return;
        }
        const local = path.resolve(fileURLToPath(url));
        if (!inside(local)) {
          blocked.push(url); request.abort(); return;
        }
        request.continue();
      } catch (error) {
        blocked.push(request.url()); request.abort();
      }
    });
    page.on('pageerror', error => pageErrors.push(String(error && error.message || error)));
    page.on('popup', popup => {
      blocked.push(`popup:${popup.url()}`);
      popup.close().catch(() => {});
    });
    await page.goto(pathToFileURL(indexPath).href, {waitUntil: 'load', timeout: 30000});
    const state = async () => page.$eval('[data-subtitle-toggle]', button => {
      const overlay = document.getElementById(button.getAttribute('aria-controls'));
      if (!overlay) throw new Error('subtitle overlay is missing');
      const style = getComputedStyle(overlay);
      const bounds = overlay.getBoundingClientRect();
      return {
        semantic: {aria_pressed: button.getAttribute('aria-pressed'), overlay_hidden: overlay.hidden},
        computed: {
          display: style.display,
          visibility: style.visibility,
          width: bounds.width,
          height: bounds.height,
          visible: style.display !== 'none' && !['hidden', 'collapse'].includes(style.visibility)
            && bounds.width > 0 && bounds.height > 0,
        },
      };
    });
    const initialState = await state();
    const subtitle = await page.$eval('[data-subtitle-toggle]', button => {
      const overlay = document.getElementById(button.getAttribute('aria-controls'));
      return {
        source: overlay.getAttribute('data-subtitle-source'),
        texts: Array.from(overlay.querySelectorAll('[data-subtitle-cue]')).map(item => item.textContent.replace(/\s+/g, ' ').trim()),
      };
    });
    await page.click('[data-subtitle-toggle]');
    const afterFirstState = await state();
    await page.click('[data-subtitle-toggle]');
    const afterSecondState = await state();
    const controls = await page.$$('button:not([disabled]), input[type="button"]:not([disabled]), input[type="submit"]:not([disabled]), [role="button"]:not([aria-disabled="true"]), a[href^="#"]');
    let controlsExercised = 0;
    for (const control of controls) {
      const isSubtitle = await control.evaluate(element => element.hasAttribute('data-subtitle-toggle'));
      if (!isSubtitle) {
        await control.click();
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      controlsExercised += 1;
    }
    process.stdout.write(JSON.stringify({
      passed: blocked.length === 0 && pageErrors.length === 0,
      initial: initialState.semantic,
      after_first_click: afterFirstState.semantic,
      after_second_click: afterSecondState.semantic,
      computed_states: {
        initial: initialState.computed,
        after_first_click: afterFirstState.computed,
        after_second_click: afterSecondState.computed,
      },
      control_count: controls.length,
      controls_exercised: controlsExercised,
      blocked_requests: blocked,
      page_errors: pageErrors,
      subtitle_source: subtitle.source,
      overlay_texts: subtitle.texts,
    }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error && error.stack || String(error)); process.exit(2); });
"""
    result = _run(
        [
            str(runtime["node"]), "-e", script, str(runtime["node_root"]),
            str(runtime["browser"]), str(project), str(vtt),
        ],
        cwd=project,
        env=env,
        timeout=60,
        stage="browser_preflight",
        failure_class="runtime",
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise StageError("browser_preflight", "offline browser preflight returned invalid JSON", failure_class="runtime") from error
    if not isinstance(payload, dict):
        raise StageError("browser_preflight", "offline browser preflight returned a non-object", failure_class="runtime")
    expected_states = {
        "initial": {"aria_pressed": "false", "overlay_hidden": True},
        "after_first_click": {"aria_pressed": "true", "overlay_hidden": False},
        "after_second_click": {"aria_pressed": "false", "overlay_hidden": True},
    }
    for key, expected in expected_states.items():
        if payload.get(key) != expected:
            raise StageError("browser_preflight", f"subtitle toggle state failed at {key}", failure_class="authoring")
    computed_states = payload.get("computed_states")
    if not isinstance(computed_states, Mapping):
        raise StageError("browser_preflight", "computed subtitle states are missing", failure_class="runtime")
    for key, expected_visible in (
        ("initial", False),
        ("after_first_click", True),
        ("after_second_click", False),
    ):
        computed = computed_states.get(key)
        if not isinstance(computed, Mapping):
            raise StageError("browser_preflight", f"computed subtitle state is missing at {key}", failure_class="runtime")
        visible = computed.get("visible")
        width = computed.get("width")
        height = computed.get("height")
        if (
            not isinstance(visible, bool)
            or isinstance(width, bool)
            or not isinstance(width, (int, float))
            or isinstance(height, bool)
            or not isinstance(height, (int, float))
        ):
            raise StageError("browser_preflight", f"computed subtitle state is invalid at {key}", failure_class="runtime")
        if visible is not expected_visible:
            raise StageError(
                "browser_preflight",
                f"computed subtitle visibility failed at {key}",
                failure_class="authoring",
            )
        if expected_visible and (
            computed.get("display") == "none"
            or computed.get("visibility") in {"hidden", "collapse"}
            or float(width) <= 0
            or float(height) <= 0
        ):
            raise StageError(
                "browser_preflight",
                f"computed subtitle bounds failed at {key}",
                failure_class="authoring",
            )
    control_count = payload.get("control_count")
    controls_exercised = payload.get("controls_exercised")
    if (
        isinstance(control_count, bool)
        or not isinstance(control_count, int)
        or isinstance(controls_exercised, bool)
        or not isinstance(controls_exercised, int)
    ):
        raise StageError("browser_preflight", "interactive control counts are missing", failure_class="runtime")
    if control_count < 1 or controls_exercised != control_count:
        raise StageError(
            "browser_preflight",
            "every enabled interactive control must be exercised offline",
            failure_class="authoring",
        )
    if payload.get("blocked_requests") or payload.get("page_errors"):
        raise StageError("browser_preflight", "offline browser observed a network request or page error", failure_class="authoring")
    cues = _vtt_cues(vtt)
    overlay_texts = payload.get("overlay_texts")
    if overlay_texts != cues:
        raise StageError("browser_preflight", "subtitle overlay cues differ from generated local VTT", failure_class="authoring")
    if payload.get("subtitle_source") != "narration/subtitles.en.vtt":
        raise StageError("browser_preflight", "subtitle overlay does not bind the generated local VTT", failure_class="authoring")
    payload.update(
        {
            "passed": True,
            "cue_count": len(cues),
            "overlay_matches_all_cues": overlay_texts == cues,
            "subtitle_source_sha256": sha256_file(vtt),
        }
    )
    return payload


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
    if error.runtime_diagnostics:
        report["runtime_diagnostics"] = error.runtime_diagnostics
    _write_json(project / "delivery-report.json", report)
    return report


def _runtime_rechecked_error(error: StageError, runtime: Mapping[str, str]) -> StageError:
    cache_dir = runtime.get("cache_dir")
    try:
        diagnostics = setup_video.doctor_video_runtime(
            cache_root=Path(str(cache_dir)).absolute().parent if cache_dir else None
        )
    except (OSError, setup_video.VideoRuntimeError) as doctor_error:
        diagnostics = {"ready": False, "status": "corrupt", "issues": [str(doctor_error)]}
    if diagnostics.get("ready") is True:
        return error
    return StageError(
        error.stage,
        f"{error}; exact runtime/browser doctor failed",
        failure_class="runtime",
        runtime_diagnostics={key: value for key, value in diagnostics.items() if key != "runtime"},
    )


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
    run_dir: Path | str | None = None,
    evidence_ids: set[str] | None = None,
    visual_catalog: Mapping[str, Mapping[str, Any]] | None = None,
    claims: Sequence[Mapping[str, Any]] | None = None,
    canonical_plan_sha256: str | None = None,
    smoke: bool = False,
) -> dict[str, Any]:
    """Produce a final MP4 only through the audited HyperFrames delivery order."""

    project = Path(project_dir).absolute()
    stages: list[dict[str, Any]] = []
    try:
        if runtime.get("status") != "ready" or runtime.get("hyperframes_version") != setup_video.HYPERFRAMES_VERSION:
            raise StageError("runtime", "exact HyperFrames 0.7.86 runtime is not ready", failure_class="runtime")
        for name in ("hyperframes", "ffmpeg", "ffprobe", "python", "home_dir", "node", "node_root", "browser"):
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
            project,
            plan,
            run_dir=run_dir,
            evidence_ids=evidence_ids,
            visual_catalog=visual_catalog,
            claims=claims,
            smoke=smoke,
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

        browser = _browser_preflight(project, subtitle_paths["vtt"], runtime, env)
        stages.append({"id": "browser_preflight", **browser})

        try:
            lint = _run(
                [str(runtime["hyperframes"]), "lint"], cwd=project, env=env,
                timeout=120, stage="full_lint", failure_class="authoring",
            )
        except StageError as error:
            raise _runtime_rechecked_error(error, runtime) from error
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
        try:
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
        except StageError as error:
            raise _runtime_rechecked_error(error, runtime) from error
        if (
            not raw_mp4.is_file()
            or raw_mp4.stat().st_size <= 0
            or raw_mp4.stat().st_mtime_ns + FRESH_MTIME_TOLERANCE_NS < started
        ):
            raw_mp4.unlink(missing_ok=True)
            error = StageError(
                "render",
                "HyperFrames did not produce a fresh non-empty MP4",
                failure_class="authoring",
            )
            raise _runtime_rechecked_error(error, runtime)
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
            "plan_sha256": canonical_plan_sha256 or _canonical_hash(plan),
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
            "canonical_plan_sha256": canonical_plan_sha256,
            "claims_sha256": _canonical_hash(list(claims or [])),
        }
        report["publish_allowlist"] = _expected_publish_paths(project, plan)
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


def _expected_publish_paths(project: Path, plan: Mapping[str, Any]) -> list[str]:
    index = project / "index.html"
    parser = _ProjectParser()
    parser.feed(index.read_text(encoding="utf-8"))
    parser.close()
    expected = {
        "index.html",
        "hyperframes.json",
        "conference-video.mp4",
        "contact-sheet.png",
        "media_probe.json",
        "delivery-report.json",
        "video-source-map.json",
        "narration/transcript.en.txt",
        "narration/subtitles.en.srt",
        "narration/subtitles.en.vtt",
        "narration/timing.json",
        "narration/voice-and-subtitles.json",
    }
    for _tag, _attribute, reference in parser.references:
        if reference.startswith("#"):
            continue
        lowered = reference.lower()
        if lowered.startswith(_REMOTE_PREFIXES) or lowered.startswith(("data:", "blob:", "javascript:")):
            continue
        path = _safe_project_file(project, reference)
        expected.add(path.relative_to(project).as_posix())
    for _quote, reference in _CSS_URL.findall(index.read_text(encoding="utf-8")):
        if reference.strip().startswith("#"):
            continue
        path = _safe_project_file(project, reference.strip())
        expected.add(path.relative_to(project).as_posix())
    for index_number, scene in enumerate(plan["scenes"], start=1):
        prefix = f"narration/scenes/{index_number:02d}-{scene['scene_id']}"
        expected.update({f"{prefix}.txt", f"{prefix}.wav"})
    expected.update(f"frames/frame-{index_number:02d}.png" for index_number in range(1, 7))
    return sorted(expected)


def _actual_project_files(source: Path) -> list[str]:
    paths: list[str] = []
    for current, directories, files in os.walk(source, followlinks=False):
        current_path = Path(current)
        for name in directories:
            directory = current_path / name
            if directory.is_symlink():
                raise VideoContractError("publish allowlist rejects a symlinked directory")
        for name in files:
            path = current_path / name
            if path.is_symlink() or path.stat().st_nlink != 1:
                raise VideoContractError("publish allowlist rejects a symlink or hard link")
            relative = path.relative_to(source)
            if any(part.startswith(".") for part in relative.parts):
                raise VideoContractError(f"publish allowlist rejects hidden file: {relative.as_posix()}")
            paths.append(relative.as_posix())
    return sorted(paths)


def _remove_tree_no_follow(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        return
    if not path.is_dir():
        path.unlink()
        return
    for current, directories, files in os.walk(path, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            (current_path / name).unlink(missing_ok=True)
        for name in directories:
            child = current_path / name
            if child.is_symlink():
                child.unlink(missing_ok=True)
            else:
                child.rmdir()
    path.rmdir()


def _copy_tree_allowlist(source: Path, destination: Path, allowlist: Sequence[str]) -> list[str]:
    if source.is_symlink() or not source.is_dir():
        raise VideoContractError("delivered project must be a regular directory")
    expected = sorted(dict.fromkeys(str(path) for path in allowlist))
    actual = _actual_project_files(source)
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        unknown = sorted(set(actual) - set(expected))
        raise VideoContractError(
            f"publish allowlist mismatch; missing={missing}; unknown={unknown}"
        )
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise VideoContractError("attempt artifact destination must be a regular directory")
    if destination.exists() and any(destination.iterdir()):
        if _actual_project_files(destination) != expected or any(
            sha256_file(_safe_project_file(destination, relative_text))
            != sha256_file(_safe_project_file(source, relative_text))
            for relative_text in expected
        ):
            raise VideoContractError("attempt artifact directory contains a partial or stale delivery")
        return [f"artifact/{relative_text}" for relative_text in expected]

    staging = destination.parent / f".artifact.stage-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    try:
        for relative_text in expected:
            path = _safe_project_file(source, relative_text)
            core.atomic_write_bytes(staging / relative_text, path.read_bytes())
        if _actual_project_files(staging) != expected:
            raise VideoContractError("staged artifact set differs from the publish allowlist")
        os.replace(staging, destination)
    except Exception:
        _remove_tree_no_follow(staging)
        raise
    return [f"artifact/{relative_text}" for relative_text in expected]


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
    canonical_plan_path = core.safe_path(run, "plan.json", must_exist=True)
    canonical_plan_sha256 = sha256_file(canonical_plan_path)
    if report.get("canonical_plan_sha256") != canonical_plan_sha256:
        raise VideoContractError("delivery report is not bound to the canonical run plan")
    if report.get("claims_sha256") != _canonical_hash(list(claims)):
        raise VideoContractError("delivery report claim binding differs from recorded claims")
    attempt = core.safe_path(run / "attempts", attempt_id, must_exist=True)
    runtime_failure_marker = core.safe_path(attempt, "qa/runtime-failure.json")
    artifact = core.safe_path(attempt, "artifact", must_exist=True)
    project = Path(project_dir).absolute()
    persisted_report = _read_json(project / "delivery-report.json")
    expected_report = {key: value for key, value in report.items() if key != "delivery_report_sha256"}
    if persisted_report != expected_report:
        raise VideoContractError("persisted delivery report differs from the passing in-memory report")
    if report.get("delivery_report_sha256") != sha256_file(project / "delivery-report.json"):
        raise VideoContractError("delivery report hash binding is stale")
    plan = normalize_plan(_read_json(canonical_plan_path))
    expected = _expected_publish_paths(project, plan)
    if report.get("publish_allowlist") != expected:
        raise VideoContractError("delivery report publish allowlist differs from the canonical project contract")
    actual = _actual_project_files(project)
    if actual != expected:
        raise VideoContractError(
            f"publish allowlist mismatch; missing={sorted(set(expected) - set(actual))}; "
            f"unknown={sorted(set(actual) - set(expected))}"
        )
    core.write_source_map(run, attempt_id, claims)
    paths = _copy_tree_allowlist(project, artifact, expected)
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
    result = core.record_deterministic_result(
        run,
        attempt_id,
        passed=True,
        checks=checks,
        artifact_paths=paths,
        preview_paths=previews,
    )
    runtime_failure_marker.unlink(missing_ok=True)
    return result


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
    marker_payload: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "attempt_id": attempt_id,
        "failure_class": "runtime",
        "failed_stage": str(report.get("failed_stage", "runtime")),
        "error": str(report.get("error", "runtime delivery failure")),
    }
    if isinstance(report.get("runtime_diagnostics"), Mapping):
        marker_payload["runtime_diagnostics"] = dict(report["runtime_diagnostics"])
    payload = core.redact_secrets(marker_payload)
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
                "title_claim_id": f"claim-scene-{index + 1:02d}-title",
                "narration_claim_id": f"claim-scene-{index + 1:02d}-narration",
                "visible_claim_ids": [f"claim-scene-{index + 1:02d}-title"],
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


def synthetic_smoke_claims(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for scene in plan["scenes"]:
        claims.extend(
            [
                {"id": scene["title_claim_id"], "text": scene["title"], "source_ids": scene["source_ids"]},
                {"id": scene["narration_claim_id"], "text": scene["narration"], "source_ids": scene["source_ids"]},
            ]
        )
    return claims


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
            f'data-title-claim-id="{scene["title_claim_id"]}" '
            f'data-narration-claim-id="{scene["narration_claim_id"]}" '
            f'data-claim-ids="{scene["title_claim_id"]}" '
            f'data-narration="{html.escape(scene["narration"], quote=True)}" '
            f'style="background:{colors[index]}"><p>AutoDesign video lifecycle smoke</p>'
            f'<h1 data-claim-id="{scene["title_claim_id"]}">{html.escape(scene["title"])}</h1></section>'
        )
    html_text = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><style>
html,body{{margin:0;width:1920px;height:1080px;overflow:hidden;background:#101820;color:#fff;font-family:Arial,sans-serif}}
[data-composition-id]{{position:relative;width:1920px;height:1080px}}.clip{{position:absolute;inset:0;display:grid;place-content:center;padding:120px}}
h1{{font-size:120px;margin:20px 0}}p{{font-size:34px}}.subtitle-overlay[hidden]{{display:none}}
[data-subtitle-toggle]{{position:absolute;right:48px;bottom:40px;z-index:100;padding:16px 20px}}
#smoke-details{{position:absolute;left:48px;bottom:40px;z-index:100;padding:16px 20px}}
.subtitle-overlay{{position:absolute;left:20%;right:20%;bottom:100px;z-index:99;background:#000c;padding:20px}}
</style></head><body><main data-composition-id="smoke" data-start="0" data-duration="{plan["duration_s"]}" data-width="1920" data-height="1080" data-no-timeline>
{''.join(sections)}<audio id="narration" class="clip" src="assets/narration.wav" data-start="0" data-duration="{plan["duration_s"]}" data-track-index="2" data-media-start="0"></audio>
<button type="button" data-subtitle-toggle aria-pressed="false" aria-controls="subtitles">CC</button><button type="button" id="smoke-details">Details</button><div id="subtitles" class="subtitle-overlay" data-subtitle-source="narration/subtitles.en.vtt" hidden>{''.join(f'<span data-subtitle-cue>{html.escape(scene["narration"])}</span>' for scene in plan["scenes"])}</div></main>
<script>document.querySelector('[data-subtitle-toggle]').addEventListener('click',event=>{{const button=event.currentTarget;const target=document.getElementById('subtitles');const shown=button.getAttribute('aria-pressed')==='true';button.setAttribute('aria-pressed',String(!shown));target.hidden=shown;}});document.getElementById('smoke-details').addEventListener('click',event=>event.currentTarget.setAttribute('data-exercised','true'));</script></body></html>'''
    _write_text(project / "index.html", html_text)
    _write_json(project / "hyperframes.json", {"version": 1, "entry": "index.html"})
    return project


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise VideoContractError(f"JSON must be an object: {path}")
    return value


def _source_contract(run: Path) -> tuple[set[str], dict[str, dict[str, Any]]]:
    evidence = {str(item["id"]) for item in core.load_evidence(run)}
    value = _read_json(run / "evidence" / "source_visuals.json")
    catalog: dict[str, dict[str, Any]] = {}
    for item in value.get("visuals", []):
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            continue
        relative = str(item.get("path", ""))
        catalog[item["id"]] = dict(item)
        catalog[item["id"]]["path"] = str(run / "evidence" / relative)
    return evidence, catalog


def create_video_review_context(run_dir: Path | str, attempt_id: str) -> dict[str, Any]:
    """Return hash-bound previews plus readable evidence material for the host VLM."""
    run = Path(run_dir).absolute()
    context = core.create_review_context(run, attempt_id, rubric=REVIEW_RUBRIC)
    attempt = core.safe_path(run / "attempts", attempt_id, must_exist=True)
    materials: dict[str, dict[str, str]] = {}
    for key, path in (
        ("evidence_jsonl", run / "evidence" / "evidence.jsonl"),
        ("source_text", run / "evidence" / "source.txt"),
        ("source_map", attempt / "provenance" / "source-map.json"),
    ):
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise VideoContractError(f"review material is missing or linked: {path}")
        materials[key] = {"path": str(path), "sha256": sha256_file(path)}
    return {**context, "review_materials": materials}


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
    validate.add_argument("--claims", type=Path, required=True)
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
            claims_value = json.loads(args.claims.read_text(encoding="utf-8"))
            if not isinstance(claims_value, list):
                raise VideoContractError("claims JSON must be a list")
            evidence_ids, catalog = _source_contract(args.run)
            canonical_plan, _canonical_plan_sha256 = load_canonical_delivery_plan(
                args.run, args.plan_json
            )
            result = validate_project(
                args.project,
                canonical_plan,
                run_dir=args.run,
                evidence_ids=evidence_ids,
                visual_catalog=catalog,
                claims=claims_value,
            )
        elif args.command == "deliver":
            claims_value = json.loads(args.claims.read_text(encoding="utf-8"))
            if not isinstance(claims_value, list):
                raise VideoContractError("claims JSON must be a list")
            runtime = setup_video.require_video_runtime(cache_root=args.cache_root).as_dict()
            evidence_ids, catalog = _source_contract(args.run)
            canonical_plan, canonical_plan_sha256 = load_canonical_delivery_plan(
                args.run, args.plan_json
            )
            result = deliver_project(
                args.project,
                canonical_plan,
                runtime,
                run_dir=args.run,
                evidence_ids=evidence_ids,
                visual_catalog=catalog,
                claims=claims_value,
                canonical_plan_sha256=canonical_plan_sha256,
            )
            if result.get("passed") is True:
                result["deterministic_result"] = record_attempt_delivery(
                    args.run, args.attempt, args.project, result, claims=claims_value
                )
            else:
                result.update(record_delivery_failure(args.run, args.attempt, result))
        elif args.command == "review-context":
            result = create_video_review_context(args.run, args.attempt)
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
