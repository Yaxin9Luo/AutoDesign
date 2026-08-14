"""HyperFramesComposer — single-turn LLM agent that writes index.html.

Called automatically by `export_video` after scaffolding the HyperFrames
project directory. Produces a standards-compliant `index.html` driven by
`prompts/hyperframes_composer.md` as its system prompt.

Shape: one LLM call, no tools, no loop.  The model receives:
  - system: prompts/hyperframes_composer.md
  - user:   composer_context (figure list + DESIGN.md text)

Output is written to `<proj_dir>/index.html` only after it passes the local
HTML delivery checks. API errors, short output, placeholders, and networked
HTML are explicit failures; they never create an apparently usable source.

Model: `COMPOSER_MODEL` env var, falling back to `settings.composer_model`.
`SKIP_VIDEO_COMPOSER=1` disables the stage so power users can author
index.html themselves.
"""

from __future__ import annotations

from html.parser import HTMLParser
import time
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from ..config import Settings
from ..llm_backend import LLMBackend, TurnResponse, make_backend
from ..run_control import CancellationToken
from ..schema import VIDEO_MAX_DURATION_S, VIDEO_MIN_DURATION_S
from ..util.logging import log
from ..util.english_text import is_substantially_english

# Minimum acceptable index.html — we check this before trusting the output.
_MIN_CHARS = 1000

_PLACEHOLDER_MARKERS = (
    "index.html placeholder",
    "index.html not yet generated",
    "composer output was unavailable",
)
_NETWORK_REFERENCE_RE = re.compile(
    r"https?://|"
    r"\b(?:src|href|poster)\s*=\s*['\"]\s*//|"
    r"\burl\(\s*['\"]?\s*//|"
    r"@import\s+['\"]\s*//|"
    r"data:",
    re.IGNORECASE,
)
_NETWORK_API_RE = re.compile(
    r"\b(?:fetch|WebSocket|EventSource|XMLHttpRequest)\s*\(",
    re.IGNORECASE,
)
_CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^)'\"]+)['\"]?\s*\)", re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:url\(\s*)?['\"]?([^)'\"\s;]+)",
    re.IGNORECASE,
)
_JS_IMPORT_RE = re.compile(
    r"(?:\bimport\s+(?:[^'\"]+?\s+from\s+)?|\bimport\s*\(|\brequire\s*\()"
    r"['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_DYNAMIC_ASSET_ASSIGNMENT_RE = re.compile(
    r"(?:\.\s*(?:src|href|poster)\s*=|"
    r"setAttribute\s*\(\s*['\"](?:src|href|poster|data|xlink:href)['\"])",
    re.IGNORECASE,
)
_SCRIPT_ASSET_TOKEN_RE = re.compile(
    r"\b(?:src|srcset|href|poster|xlink:href)\b|"
    r"(?:\.\s*data\s*=|\[\s*['\"]data['\"]\s*\]\s*=|"
    r"setAttribute(?:NS)?\s*\([^)]*['\"]data['\"])",
    re.IGNORECASE,
)
_URI_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_REQUEST_ANIMATION_FRAME_RE = re.compile(r"\brequestAnimationFrame\s*\(")


HYPERFRAMES_AUTHOR_PROTOCOL: dict[str, Any] = {
    "manifest_project_path": "project",
    "composition_root": {
        "exact_count": 1,
        "required_attributes": [
            "data-composition-id",
            "data-start=0",
            "data-duration",
            "data-width=1920",
            "data-height=1080",
        ],
        "timeline_mode": "data-no-timeline or a registered window.__timelines entry",
    },
    "scene": {
        "element": "section",
        "required_class": "clip",
        "required_attributes": [
            "id",
            "data-start",
            "data-duration",
            "data-track-index",
            "data-narration",
        ],
    },
    "narration_audio": {
        "required_class": "clip",
        "src": "assets/narration.wav",
        "required_attributes": [
            "id",
            "data-start=0",
            "data-duration",
            "data-track-index",
            "data-media-start=0",
        ],
    },
    "forbidden": ["requestAnimationFrame", "remote assets", "network APIs"],
}


def _has_registered_timeline(script_content: str, composition_id: str) -> bool:
    """Return whether the source registers a timeline for this composition."""
    if not composition_id:
        return False
    key = re.escape(composition_id)
    assignment = r"(?:\?\?|\|\|)?="
    bracket_entry = re.compile(
        rf"\bwindow\s*\.\s*__timelines\s*\[\s*['\"]{key}['\"]\s*\]\s*{assignment}"
    )
    if bracket_entry.search(script_content):
        return True
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", composition_id):
        dot_entry = re.compile(
            rf"\bwindow\s*\.\s*__timelines\s*\.\s*{key}\s*{assignment}"
        )
        return bool(dot_entry.search(script_content))
    return False


@dataclass(frozen=True)
class ComposerResult:
    """Outcome of one composer pass.

    `index_html` is the full file content that was written.
    `skipped` is True when the stage was bypassed or failed gracefully.
    `skip_reason` explains why (empty string when not skipped).
    """
    index_html: str
    proj_dir: Path
    model: str
    skipped: bool = False
    skip_reason: str = ""
    wall_time_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


class HyperFramesComposer:
    """Single-turn agent that writes index.html for a HyperFrames project.

    Usage::

        composer = HyperFramesComposer(settings, system_prompt)
        result = composer.compose(composer_context, proj_dir)
        # result.index_html is written to proj_dir/index.html
    """

    def __init__(self, settings: Settings, system_prompt: str):
        self.settings = settings
        self.system_prompt = system_prompt
        self.backend: LLMBackend = make_backend(
            settings, settings.composer_model, role="composer",
        )

    def compose(
        self,
        composer_context: str,
        proj_dir: Path,
        delivery_contract: Any | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> ComposerResult:
        """Call the LLM, write index.html, return a ComposerResult.

        A failed or invalid composer pass leaves no `index.html`. This prevents
        stale or placeholder source from being promoted as a video delivery.
        """
        _raise_if_cancelled(cancellation_token, "hyperframes_composer.start")
        model = self.backend.model
        log("hyperframes.compose.request",
            model=model,
            backend=self.backend.name,
            context_chars=len(composer_context),
            proj_dir=proj_dir.name)

        wall_start = time.monotonic()
        messages = [{"role": "user", "content": composer_context}]
        # A 10-14 scene conference video can require substantial HTML.
        # Give the composer enough room without introducing another loop.
        max_tokens = 32768
        index_path = proj_dir / "index.html"
        _raise_if_cancelled(cancellation_token, "hyperframes_composer.before_clear_output")
        index_path.unlink(missing_ok=True)

        try:
            request_kwargs: dict[str, Any] = {
                "system": self.system_prompt,
                "messages": messages,
                "tools": [],
                "thinking_budget": 0,
                "max_tokens": max_tokens,
            }
            if (
                cancellation_token is not None
                and getattr(cancellation_token, "can_cancel", True)
            ):
                request_kwargs["cancellation_token"] = cancellation_token
            resp: TurnResponse = self.backend.create_turn(**request_kwargs)
        except Exception as e:
            wall_s = round(time.monotonic() - wall_start, 2)
            log("hyperframes.compose.error",
                error=f"{type(e).__name__}: {e}",
                wall_s=wall_s,
                fallback="none")
            return ComposerResult(
                index_html="",
                proj_dir=proj_dir,
                model=model,
                skipped=True,
                skip_reason=f"api_error: {type(e).__name__}",
                wall_time_s=wall_s,
            )

        _raise_if_cancelled(cancellation_token, "hyperframes_composer.after_model")
        wall_s = round(time.monotonic() - wall_start, 2)
        raw = (resp.text or "").strip()

        # Strip markdown fences the model might have wrapped around the HTML
        # despite the system prompt saying not to.
        html_content = _strip_markdown_fences(raw)

        html_errors = validate_authored_video_html(
            html_content,
            delivery_contract,
            project_dir=proj_dir,
        )
        if len(html_content) < _MIN_CHARS:
            html_errors.insert(0, f"authored HTML is shorter than {_MIN_CHARS} characters")
        if html_errors:
            log("hyperframes.compose.degraded",
                reason="; ".join(html_errors),
                output_chars=len(html_content),
                wall_s=wall_s,
                fallback="none")
            return ComposerResult(
                index_html="",
                proj_dir=proj_dir,
                model=model,
                skipped=True,
                skip_reason="invalid_authored_html: " + "; ".join(html_errors),
                wall_time_s=wall_s,
                input_tokens=resp.usage.get("input", 0),
                output_tokens=resp.usage.get("output", 0),
            )

        _raise_if_cancelled(cancellation_token, "hyperframes_composer.before_write")
        _write_html(proj_dir, html_content)
        _raise_if_cancelled(cancellation_token, "hyperframes_composer.after_write")
        log("hyperframes.compose.done",
            model=model,
            html_chars=len(html_content),
            wall_s=wall_s,
            input_tokens=resp.usage.get("input", 0),
            output_tokens=resp.usage.get("output", 0))

        return ComposerResult(
            index_html=html_content,
            proj_dir=proj_dir,
            model=model,
            skipped=False,
            wall_time_s=wall_s,
            input_tokens=resp.usage.get("input", 0),
            output_tokens=resp.usage.get("output", 0),
        )


def _raise_if_cancelled(
    cancellation_token: CancellationToken | None,
    phase: str,
) -> None:
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled(phase)


def load_composer_system_prompt(settings: Settings) -> str | None:
    """Read `prompts/hyperframes_composer.md`.

    Returns None (and logs a warning) when the file is missing so the
    caller can skip the stage gracefully rather than crashing.
    """
    path: Path = settings.prompts_dir / "hyperframes_composer.md"
    if not path.exists():
        log("hyperframes.compose.missing_prompt", path=str(path))
        return None
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_html(proj_dir: Path, content: str) -> None:
    dest = proj_dir / "index.html"
    dest.write_text(content, encoding="utf-8")


class _AuthoredVideoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root_attrs: dict[str, str] | None = None
        self.composition_attrs: list[dict[str, str]] = []
        self.scene_attrs: list[dict[str, str]] = []
        self.noncanonical_scene_attrs: list[dict[str, str]] = []
        self.audio_attrs: list[dict[str, str]] = []
        self.asset_refs: list[str] = []
        self.script_chunks: list[str] = []
        self._in_script = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = {name.lower(): value or "" for name, value in attrs}
        if tag.lower() == "script":
            self._in_script = True
        for name in ("src", "href", "poster", "data-composition-src"):
            if normalized.get(name):
                self.asset_refs.append(normalized[name])
        if tag.lower() == "object" and normalized.get("data"):
            self.asset_refs.append(normalized["data"])
        if normalized.get("xlink:href"):
            self.asset_refs.append(normalized["xlink:href"])
        if normalized.get("srcset"):
            self.asset_refs.extend(_srcset_refs(normalized["srcset"]))
        if normalized.get("style"):
            self.asset_refs.extend(_CSS_URL_RE.findall(normalized["style"]))
        composition_id = normalized.get("data-composition-id", "").strip()
        if composition_id:
            self.composition_attrs.append(normalized)
            if self.root_attrs is None:
                self.root_attrs = normalized
        if tag.lower() == "section":
            if "clip" in normalized.get("class", "").split():
                self.scene_attrs.append(normalized)
            elif normalized.get("data-hf-clip"):
                self.noncanonical_scene_attrs.append(normalized)
        if tag.lower() == "audio" and normalized.get("src") == "assets/narration.wav":
            self.audio_attrs.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self.script_chunks.append(data)


def _float_attr(attrs: dict[str, str], name: str) -> float | None:
    try:
        return float(attrs[name])
    except (KeyError, TypeError, ValueError):
        return None


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _srcset_refs(value: str) -> list[str]:
    return [
        candidate.strip().split()[0]
        for candidate in value.split(",")
        if candidate.strip()
    ]


def _static_asset_refs(content: str, *, suffix: str) -> list[str]:
    if suffix.lower() == ".css":
        return [*_CSS_URL_RE.findall(content), *_CSS_IMPORT_RE.findall(content)]
    if suffix.lower() in {".js", ".mjs"}:
        return _JS_IMPORT_RE.findall(content)
    parser = _AuthoredVideoParser()
    parser.feed(content)
    parser.close()
    return [*parser.asset_refs, *_CSS_URL_RE.findall(content)]


def _has_dynamic_asset_assignment(content: str) -> bool:
    if _DYNAMIC_ASSET_ASSIGNMENT_RE.search(content):
        return True
    parser = _AuthoredVideoParser()
    parser.feed(content)
    parser.close()
    return any(_SCRIPT_ASSET_TOKEN_RE.search(chunk) for chunk in parser.script_chunks)


def authored_video_local_asset_paths(
    html_content: str,
    project_dir: Path,
    *,
    allow_generated_narration: bool = False,
) -> dict[str, Path]:
    """Resolve every local authored-HTML dependency inside the video project."""
    if _has_dynamic_asset_assignment(html_content):
        raise ValueError("dynamic asset assignment is forbidden in authored video HTML")
    project_root = project_dir.resolve()
    resolved_assets: dict[str, Path] = {}
    pending = [
        (project_root, raw_ref)
        for raw_ref in _static_asset_refs(html_content, suffix=".html")
    ]
    inspected_dependencies: set[Path] = set()
    while pending:
        base_dir, raw_ref = pending.pop()
        ref = raw_ref.strip()
        if not ref or ref.startswith("#"):
            continue
        path_text = ref.split("?", 1)[0].split("#", 1)[0]
        candidate = Path(path_text)
        if (
            _URI_SCHEME_RE.match(ref)
            or ref.startswith("//")
            or candidate.is_absolute()
            or (".." in candidate.parts and base_dir == project_root)
        ):
            raise ValueError(f"local asset reference escapes the video project: {ref}")
        resolved = (base_dir / candidate).resolve()
        generated_narration = (
            allow_generated_narration
            and resolved == project_root / "assets" / "narration.wav"
        )
        if not resolved.is_relative_to(project_root) or (
            not generated_narration and not resolved.is_file()
        ):
            raise ValueError(f"local asset is missing or outside the video project: {ref}")
        if generated_narration:
            continue
        resolved_assets[resolved.relative_to(project_root).as_posix()] = resolved
        if resolved in inspected_dependencies or resolved.suffix.lower() not in {
            ".css", ".html", ".htm", ".js", ".mjs",
        }:
            continue
        inspected_dependencies.add(resolved)
        content = resolved.read_text(encoding="utf-8")
        if resolved.suffix.lower() in {".html", ".htm"} and _has_dynamic_asset_assignment(content):
            raise ValueError(f"dynamic asset assignment is forbidden in {resolved.name}")
        if resolved.suffix.lower() in {".js", ".mjs"} and (
            _NETWORK_REFERENCE_RE.search(content)
            or _NETWORK_API_RE.search(content)
            or _has_dynamic_asset_assignment(content)
        ):
            raise ValueError(f"network or dynamic asset access is forbidden in {resolved.name}")
        pending.extend(
            (resolved.parent, nested_ref)
            for nested_ref in _static_asset_refs(content, suffix=resolved.suffix)
        )
    return resolved_assets


def validate_authored_video_html(
    html_content: str,
    delivery_contract: Any | None = None,
    *,
    project_dir: Path | None = None,
) -> list[str]:
    """Return hard delivery errors for authored HyperFrames HTML."""
    lowered = html_content.lower()
    for marker in _PLACEHOLDER_MARKERS:
        if marker in lowered:
            return ["placeholder HTML is not a video source"]

    errors: list[str] = []
    if not lowered.lstrip().startswith("<!doctype html"):
        errors.append("authored HTML must start with <!doctype html>")
    if _NETWORK_REFERENCE_RE.search(html_content):
        errors.append("external or data URL assets are forbidden")
    if _NETWORK_API_RE.search(html_content):
        errors.append("network APIs are forbidden in authored HTML")
    if _has_dynamic_asset_assignment(html_content):
        errors.append("dynamic asset assignment is forbidden in authored video HTML")
    parser = _AuthoredVideoParser()
    try:
        parser.feed(html_content)
        parser.close()
    except Exception as exc:
        errors.append(f"authored HTML could not be parsed structurally: {exc}")

    for raw_ref in [*parser.asset_refs, *_CSS_URL_RE.findall(html_content)]:
        ref = raw_ref.strip()
        if not ref or ref.startswith("#"):
            continue
        path_text = ref.split("?", 1)[0].split("#", 1)[0]
        candidate = Path(path_text)
        if (
            _URI_SCHEME_RE.match(ref)
            or ref.startswith("//")
            or candidate.is_absolute()
            or ".." in candidate.parts
        ):
            errors.append(f"local asset reference escapes the video project: {ref}")
            continue
        if project_dir is not None:
            project_root = project_dir.resolve()
            resolved = (project_root / candidate).resolve()
            generated_narration = candidate.as_posix() == "assets/narration.wav"
            if (
                not resolved.is_relative_to(project_root)
                or (not generated_narration and not resolved.is_file())
            ):
                errors.append(f"local asset is missing or outside the video project: {ref}")

    local_asset_paths: dict[str, Path] = {}
    if project_dir is not None:
        try:
            local_asset_paths = authored_video_local_asset_paths(
                html_content,
                project_dir,
                allow_generated_narration=True,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            message = str(exc)
            if message and message not in errors:
                errors.append(message)

    script_chunks = list(parser.script_chunks)
    for path in local_asset_paths.values():
        if path.suffix.lower() in {".js", ".mjs"}:
            try:
                script_chunks.append(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError) as exc:
                errors.append(f"local script could not be read: {path.name}: {exc}")
    script_content = "\n".join(script_chunks)

    root_attrs = parser.root_attrs or {}
    composition_ids = [
        attrs["data-composition-id"].strip()
        for attrs in parser.composition_attrs
        if attrs.get("data-composition-id", "").strip()
    ]
    if len(composition_ids) != 1:
        errors.append(
            "HyperFrames authored HTML must contain exactly one "
            "data-composition-id composition root"
        )
    if len(composition_ids) != len(set(composition_ids)):
        errors.append(
            "HyperFrames data-composition-id values must be unique within a "
            "composition file"
        )
    if not root_attrs:
        errors.append("HyperFrames root data-composition-id is required")
    else:
        root_duration = _float_attr(root_attrs, "data-duration")
        if (
            root_duration is None
            or not VIDEO_MIN_DURATION_S <= root_duration <= VIDEO_MAX_DURATION_S
        ):
            errors.append("HyperFrames root data-duration must be within 300-600 seconds")
    if root_attrs.get("data-width") != "1920":
        errors.append("HyperFrames root data-width must be 1920")
    if root_attrs.get("data-height") != "1080":
        errors.append("HyperFrames root data-height must be 1080")
    if _float_attr(root_attrs, "data-start") != 0:
        errors.append("HyperFrames root data-start must be 0")
    if (
        root_attrs
        and "data-no-timeline" not in root_attrs
        and not _has_registered_timeline(
            script_content,
            root_attrs.get("data-composition-id", "").strip(),
        )
    ):
        errors.append(
            "HyperFrames root must set data-no-timeline or register a "
            "window.__timelines entry for its data-composition-id"
        )
    if _REQUEST_ANIMATION_FRAME_RE.search(script_content):
        errors.append(
            "requestAnimationFrame is forbidden in HyperFrames compositions; "
            "use a registered deterministic timeline instead"
        )

    scene_attrs = parser.scene_attrs
    for attrs in parser.noncanonical_scene_attrs:
        scene_id = attrs.get("id") or attrs.get("data-scene-id") or "<unnamed>"
        errors.append(
            "HyperFrames scene "
            f"{scene_id} uses data-hf-clip without the required literal "
            'class="clip"; add class="clip" to the <section>.'
        )
    if not 10 <= len(scene_attrs) <= 14:
        errors.append("authored HyperFrames HTML must contain 10-14 clip scenes")
    for index, attrs in enumerate(scene_attrs, start=1):
        narration = _normalized_text(attrs.get("data-narration"))
        if not is_substantially_english(narration):
            errors.append(f"scene {index} must include English data-narration metadata")
    if not parser.audio_attrs:
        errors.append("authored HTML must reference local assets/narration.wav audio")
    else:
        audio = parser.audio_attrs[0]
        if not audio.get("id"):
            errors.append("HyperFrames narration audio id is required")
        if _float_attr(audio, "data-start") != 0:
            errors.append("HyperFrames narration audio data-start must be 0")
        if _float_attr(audio, "data-duration") is None:
            errors.append("HyperFrames narration audio data-duration is required")
        if not audio.get("data-track-index"):
            errors.append("HyperFrames narration audio data-track-index is required")
        if _float_attr(audio, "data-media-start") != 0:
            errors.append("HyperFrames narration audio data-media-start must be 0")

    if delivery_contract is not None:
        expected_scenes = list(getattr(delivery_contract, "scenes", []) or [])
        target_duration = float(getattr(delivery_contract, "target_duration_s", 0) or 0)
        root_duration = _float_attr(root_attrs, "data-duration")
        if root_duration is None or abs(root_duration - target_duration) > 1e-6:
            errors.append(
                "HyperFrames root data-duration must exactly match VideoDeliveryContract"
            )
        actual_ids = [attrs.get("id", "") for attrs in scene_attrs]
        expected_ids = [str(getattr(scene, "scene_id", "")) for scene in expected_scenes]
        if actual_ids != expected_ids:
            errors.append("authored scene ids/order must exactly match VideoDeliveryContract")
        for index, (attrs, expected) in enumerate(
            zip(scene_attrs, expected_scenes), start=1
        ):
            start_s = _float_attr(attrs, "data-start")
            duration_s = _float_attr(attrs, "data-duration")
            expected_start = float(getattr(expected, "start_s", 0))
            expected_duration = float(getattr(expected, "duration_s", 0))
            if start_s is None or abs(start_s - expected_start) > 1e-6:
                errors.append(
                    f"scene {index} data-start must exactly match VideoDeliveryContract"
                )
            if duration_s is None or abs(duration_s - expected_duration) > 1e-6:
                errors.append(
                    f"scene {index} data-duration must exactly match VideoDeliveryContract"
                )
            if _normalized_text(attrs.get("data-narration")) != _normalized_text(
                getattr(expected, "narration_text", "")
            ):
                errors.append(
                    f"scene {index} data-narration must exactly match VideoDeliveryContract"
                )
        if parser.audio_attrs:
            audio_duration = _float_attr(parser.audio_attrs[0], "data-duration")
            if audio_duration is None or abs(audio_duration - target_duration) > 1e-6:
                errors.append(
                    "HyperFrames narration audio data-duration must exactly match "
                    "VideoDeliveryContract"
                )
    return errors


def _strip_markdown_fences(text: str) -> str:
    """Remove leading/trailing markdown code fences the model may have added.

    Handles both:
      ```html\\n...\\n```
      ```\\n...\\n```
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # Remove opening fence + optional language tag
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1:]
        # Remove closing fence
        if stripped.endswith("```"):
            stripped = stripped[:-3].rstrip()
    return stripped.strip()
