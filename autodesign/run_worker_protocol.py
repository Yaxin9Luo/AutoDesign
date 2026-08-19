"""Private, strict stdin protocol for supervised AutoDesign workers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime
import json
from pathlib import Path
import re
import struct
import types
from typing import Any, BinaryIO, Literal, TypeAlias, Union, get_args, get_origin, get_type_hints

from .config import Settings
from .run_control import RunControlError, validate_run_id


PROTOCOL_VERSION = 1
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
WORKER_ERROR_MESSAGE_LIMIT = 2000
_LENGTH = struct.Struct(">I")


class ProtocolError(ValueError):
    """The private worker envelope is incomplete or violates its schema."""


@dataclass(frozen=True)
class PipelineWorkerRequest:
    job_kind: Literal["pipeline"]
    run_id: str
    brief: str
    attachments: tuple[str, ...]
    template: str | None
    palette_id: str | None
    resume_run: str | None
    reference_poster: str | None
    settings: Settings
    canvas_preset_id: str | None = None


@dataclass(frozen=True)
class ArtifactEditWorkerRequest:
    job_kind: Literal["artifact_edit"]
    run_id: str
    parent_run_id: str
    input_path: str
    conversation_id: str
    settings: Settings


@dataclass(frozen=True)
class EditableVideoRenderWorkerRequest:
    job_kind: Literal["editable_video_render"]
    run_id: str
    parent_run_id: str
    artifact: dict[str, Any]
    conversation_id: str
    baseline_artifact_json: str
    settings: Settings


@dataclass(frozen=True)
class PosterCodeEditWorkerRequest:
    job_kind: Literal["poster_code_edit"]
    run_id: str
    parent_run_id: str
    source_poster: str
    artifact: dict[str, Any]
    instruction: str
    conversation_history: tuple[dict[str, Any], ...]
    selection_context: dict[str, Any] | None
    palette_id: str | None
    required_color_system: dict[str, Any]
    conversation_id: str
    baseline_artifact_json: str
    settings: Settings


@dataclass(frozen=True)
class PptxExportWorkerRequest:
    job_kind: Literal["pptx_export"]
    run_id: str
    parent_run_id: str
    source_html: str
    artifact: dict[str, Any]
    artifact_name: str
    conversation_id: str
    settings: Settings


@dataclass(frozen=True)
class VideoExportRetryWorkerRequest:
    job_kind: Literal["video_export_retry"]
    run_id: str
    parent_run_id: str
    source_project: str
    conversation_id: str
    baseline_artifact_json: str
    runs_dir: str


@dataclass(frozen=True)
class AttemptForkWorkerRequest:
    job_kind: Literal["attempt_fork"]
    run_id: str
    parent_run_id: str
    attempt: int
    expected_candidate_sha256: str
    conversation_id: str
    settings: Settings


@dataclass(frozen=True)
class CandidatePublishWorkerRequest:
    job_kind: Literal["candidate_publish"]
    run_id: str
    parent_run_id: str
    conversation_id: str
    settings: Settings
    source_attempt: int | None = None
    expected_candidate_sha256: str | None = None


RunWorkerRequest: TypeAlias = (
    PipelineWorkerRequest
    | ArtifactEditWorkerRequest
    | EditableVideoRenderWorkerRequest
    | PosterCodeEditWorkerRequest
    | PptxExportWorkerRequest
    | VideoExportRetryWorkerRequest
    | AttemptForkWorkerRequest
    | CandidatePublishWorkerRequest
)


# Deliberately literal: adding a Settings field must update the private protocol
# consciously instead of silently dropping or defaulting a request-scoped value.
_SETTINGS_FIELDS = (
    "anthropic_api_key", "anthropic_base_url", "gemini_api_key", "designer_model",
    "critic_model", "anthropic_auth_token", "anthropic_custom_headers",
    "designer_provider", "critic_provider", "enhancer_model", "enhancer_provider",
    "enhancer_thinking_budget", "enable_prompt_enhancer", "claim_graph_model",
    "claim_graph_provider", "claim_graph_max_turns", "claim_graph_thinking_budget",
    "enable_claim_graph", "deck_outline_model", "deck_outline_provider",
    "deck_outline_max_turns", "deck_outline_thinking_budget", "enable_deck_outline",
    "paper_memory_model", "paper_memory_provider", "paper_memory_max_turns",
    "paper_memory_thinking_budget", "enable_paper_memory_agent", "composer_model",
    "composer_provider", "enable_video_composer", "designer_author_mode",
    "designer_author_harness", "designer_author_cmd", "designer_author_model",
    "designer_author_timeout_s", "designer_author_max_attempts",
    "authoring_max_attempts_override", "designer_author_poster_stable_s", "harness_api_key",
    "code_editor_harness", "code_editor_cmd", "code_editor_model", "code_editor_timeout_s",
    "code_editor_max_attempts", "identity_logo_agent_mode", "identity_logo_agent_harness",
    "identity_logo_agent_cmd", "identity_logo_agent_model", "identity_logo_agent_timeout_s",
    "identity_logo_agent_max_entities", "identity_logo_agent_max_candidates",
    "openresearch_api_url", "openresearch_token", "openresearch_timeout_s",
    "openresearch_org_id", "openresearch_default_repo_full_name",
    "openresearch_submitter_mode", "openresearch_submitter_cmd",
    "openresearch_submitter_timeout_s", "poster_harness_mode", "openai_compat_api_key",
    "openai_compat_base_url", "llm_http_timeout", "allow_private_network",
    "allow_remote_image_urls", "openrouter_api_key", "image_model",
    "image_provider", "image_fallback_model", "ingest_model",
    "ingest_http_timeout", "repo_root", "fonts_dir", "prompts_dir", "skills_dir", "out_dir",
    "max_critique_iters", "max_designer_turns", "max_env_repair_attempts", "enable_skills",
    "critic_preview_max_edge", "poster_preview_max_edge", "section_number_policy",
    "critic_max_turns", "critic_max_images_per_turn", "designer_thinking_budget",
    "critic_thinking_budget", "enable_interleaved_thinking", "fonts", "default_text_font",
    "default_title_font",
)
_PATH_FIELDS = frozenset({"repo_root", "fonts_dir", "prompts_dir", "skills_dir", "out_dir"})
_SENSITIVE_SETTINGS_FIELDS = frozenset({
    "anthropic_api_key", "anthropic_auth_token", "gemini_api_key",
    "harness_api_key", "openai_compat_api_key", "openrouter_api_key", "openresearch_token",
})

_REQUEST_FIELDS: dict[str, tuple[str, ...]] = {
    "pipeline": (
        "job_kind", "run_id", "brief", "attachments", "template", "palette_id",
        "resume_run", "reference_poster", "settings", "canvas_preset_id",
    ),
    "artifact_edit": (
        "job_kind", "run_id", "parent_run_id", "input_path",
        "conversation_id", "settings",
    ),
    "editable_video_render": (
        "job_kind", "run_id", "parent_run_id", "artifact", "conversation_id",
        "baseline_artifact_json", "settings",
    ),
    "poster_code_edit": (
        "job_kind", "run_id", "parent_run_id", "source_poster", "artifact", "instruction",
        "conversation_history", "selection_context", "palette_id", "required_color_system", "conversation_id",
        "baseline_artifact_json", "settings",
    ),
    "pptx_export": (
        "job_kind", "run_id", "parent_run_id", "source_html", "artifact", "artifact_name",
        "conversation_id", "settings",
    ),
    "video_export_retry": (
        "job_kind", "run_id", "parent_run_id", "source_project", "conversation_id",
        "baseline_artifact_json", "runs_dir",
    ),
    "attempt_fork": (
        "job_kind", "run_id", "parent_run_id", "attempt",
        "expected_candidate_sha256", "conversation_id", "settings",
    ),
    "candidate_publish": (
        "job_kind", "run_id", "parent_run_id", "conversation_id", "settings",
        "source_attempt", "expected_candidate_sha256",
    ),
}

_LEGACY_CANDIDATE_PUBLISH_FIELDS = frozenset(
    set(_REQUEST_FIELDS["candidate_publish"])
    - {"source_attempt", "expected_candidate_sha256"}
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def encode_request(request: RunWorkerRequest) -> bytes:
    request_payload = asdict(request)
    if (
        request_payload.get("job_kind") == "candidate_publish"
        and request_payload.get("source_attempt") is None
        and request_payload.get("expected_candidate_sha256") is None
    ):
        request_payload.pop("source_attempt")
        request_payload.pop("expected_candidate_sha256")
    if "settings" in request_payload:
        request_payload["settings"] = settings_to_payload(request.settings)
    _validate_request_payload(request_payload)
    try:
        body = json.dumps(
            {"version": PROTOCOL_VERSION, "request": request_payload},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("worker request contains a non-JSON value") from exc
    if len(body) > MAX_PAYLOAD_BYTES:
        raise ProtocolError(f"worker payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    return _LENGTH.pack(len(body)) + body


def decode_request(stream: BinaryIO) -> RunWorkerRequest:
    header = _read_exact(stream, _LENGTH.size, "length prefix")
    (length,) = _LENGTH.unpack(header)
    if length <= 0 or length > MAX_PAYLOAD_BYTES:
        raise ProtocolError(f"invalid worker payload length: {length}")
    body = _read_exact(stream, length, "JSON payload")
    if stream.read(1):
        raise ProtocolError("worker protocol accepts exactly one envelope")
    try:
        envelope = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("worker payload is not valid UTF-8 JSON") from exc
    _require_exact_object(envelope, {"version", "request"}, "envelope")
    if type(envelope["version"]) is not int or envelope["version"] != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported worker protocol version: {envelope['version']!r}")
    payload = envelope["request"]
    _validate_request_payload(payload)
    values = dict(payload)
    if "settings" in values:
        values["settings"] = settings_from_payload(values["settings"])
    kind = values["job_kind"]
    if kind == "pipeline":
        values["attachments"] = tuple(values["attachments"])
        return PipelineWorkerRequest(**values)
    if kind == "artifact_edit":
        return ArtifactEditWorkerRequest(**values)
    if kind == "editable_video_render":
        return EditableVideoRenderWorkerRequest(**values)
    if kind == "poster_code_edit":
        values["conversation_history"] = tuple(values["conversation_history"])
        return PosterCodeEditWorkerRequest(**values)
    if kind == "pptx_export":
        return PptxExportWorkerRequest(**values)
    if kind == "video_export_retry":
        return VideoExportRetryWorkerRequest(**values)
    if kind == "attempt_fork":
        return AttemptForkWorkerRequest(**values)
    values.setdefault("source_attempt", None)
    values.setdefault("expected_candidate_sha256", None)
    return CandidatePublishWorkerRequest(**values)


def settings_to_payload(settings: Settings) -> dict[str, Any]:
    values = asdict(settings)
    if set(values) != set(_SETTINGS_FIELDS):
        missing = sorted(set(_SETTINGS_FIELDS) - set(values))
        unknown = sorted(set(values) - set(_SETTINGS_FIELDS))
        raise ProtocolError(f"Settings schema drift (missing={missing}, unknown={unknown})")
    for name in _PATH_FIELDS:
        values[name] = str(values[name])
    _validate_settings_payload(values)
    return values


def settings_from_payload(payload: Any) -> Settings:
    _validate_settings_payload(payload)
    values = dict(payload)
    for name in _PATH_FIELDS:
        values[name] = Path(values[name])
    try:
        return Settings(**values)
    except (TypeError, ValueError) as exc:
        raise ProtocolError("Settings payload could not be reconstructed") from exc


def sensitive_values(request: RunWorkerRequest) -> tuple[str, ...]:
    values: set[str] = set()
    settings = getattr(request, "settings", None)
    if settings is None:
        return ()
    for name in _SENSITIVE_SETTINGS_FIELDS:
        value = getattr(settings, name)
        if isinstance(value, str) and value:
            values.add(value)
    values.update(value for value in settings.anthropic_custom_headers.values() if value)
    return tuple(sorted(values, key=len, reverse=True))


def decode_worker_result(
    payload: Any,
    *,
    expected_run_id: str,
    expected_job_kind: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProtocolError("worker result must be a JSON object")
    expected_fields = {"job_kind", "run_id", "ok", "result"} if payload.get("ok") is True else {
        "job_kind", "run_id", "ok", "error",
    }
    _require_exact_object(payload, expected_fields, "worker result")
    if payload["job_kind"] not in _REQUEST_FIELDS:
        raise ProtocolError(f"unknown worker result job_kind: {payload['job_kind']!r}")
    if payload["job_kind"] != expected_job_kind:
        raise ProtocolError("worker result job_kind does not match request")
    _require_identifier(payload["run_id"], "worker result run_id")
    if payload["run_id"] != expected_run_id:
        raise ProtocolError("worker result run_id does not match request")
    if type(payload["ok"]) is not bool:
        raise ProtocolError("worker result ok must be a boolean")
    value_name = "result" if payload["ok"] else "error"
    _require_json_object(payload[value_name], f"worker result {value_name}")
    if payload["ok"]:
        _validate_success_result(payload["job_kind"], payload["result"], payload["run_id"])
    else:
        if (
            payload["job_kind"] == "video_export_retry"
            and set(payload["error"])
            in ({"type", "message"}, {"type", "message", "traceback"})
        ):
            payload["error"]["pointer_cleanup_warnings"] = []
        _validate_error_result(payload["job_kind"], payload["error"])
    return payload


def parse_worker_result_json(raw: str) -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ProtocolError("worker_result.json is invalid JSON") from exc


def format_worker_error_message(
    message: str,
    pointer_cleanup_warnings: list[str] | tuple[str, ...],
) -> str:
    if not pointer_cleanup_warnings:
        return (message or "Worker failed")[:WORKER_ERROR_MESSAGE_LIMIT]

    marker = "\nPointer cleanup warnings: "
    suffix = marker + " | ".join(pointer_cleanup_warnings)
    marker_index = message.rfind(marker)
    if marker_index >= 0:
        existing_tail = message[marker_index:]
        if suffix.startswith(existing_tail):
            message = message[:marker_index]
    primary = message or "Worker failed"
    if len(primary) + len(suffix) <= WORKER_ERROR_MESSAGE_LIMIT:
        return primary + suffix

    half_limit = WORKER_ERROR_MESSAGE_LIMIT // 2
    if len(primary) <= half_limit:
        primary_limit = len(primary)
        suffix_limit = WORKER_ERROR_MESSAGE_LIMIT - primary_limit
    elif len(suffix) <= half_limit:
        suffix_limit = len(suffix)
        primary_limit = WORKER_ERROR_MESSAGE_LIMIT - suffix_limit
    else:
        suffix_limit = half_limit
        primary_limit = WORKER_ERROR_MESSAGE_LIMIT - suffix_limit
    return primary[:primary_limit] + suffix[:suffix_limit]


def _validate_success_result(kind: str, result: dict[str, Any], run_id: str) -> None:
    if kind == "pipeline":
        from .schema import RunResult

        _require_exact_object(result, set(RunResult.model_fields), "pipeline worker result")
        try:
            validated = RunResult.model_validate(result)
        except (TypeError, ValueError) as exc:
            raise ProtocolError("pipeline worker result violates RunResult") from exc
        if validated.run_id != run_id:
            raise ProtocolError("worker result run_id does not match request")
        return
    required: dict[str, dict[str, type]] = {
        "editable_video_render": {"run_id": str, "mp4_path": str},
        "artifact_edit": {
            "run_id": str,
            "artifact_type": str,
            "source_path": str,
            "restored_layer_ids": list,
            "skipped": list,
            "candidate_lineage": dict,
        },
        "poster_code_edit": {
            "run_id": str, "run_dir": str, "attempt_dir": str,
            "poster_path": str, "preview_path": str, "attempts": list,
            "validation_summary": dict, "selection_context_summary": dict,
            "promoted_assets": list,
        },
        "pptx_export": {"run_id": str, "pptx_path": str},
        "video_export_retry": {
            "run_id": str, "ok": bool, "phase": str, "project_dir": str,
            "manifest_path": str, "mp4_path": str, "media_probe_path": str,
            "render_started_at": str, "pointer_cleanup_warnings": list,
        },
        "attempt_fork": {
            "run_id": str,
            "artifact_type": str,
            "source_path": str,
            "lineage": dict,
        },
        "candidate_publish": {
            "run_id": str,
            "artifact_type": str,
            "source_path": str,
            "lineage": dict,
        },
    }
    expected_fields = set(required[kind])
    if (
        kind == "video_export_retry"
        and set(result) == expected_fields - {"pointer_cleanup_warnings"}
    ):
        result["pointer_cleanup_warnings"] = []
    _require_exact_object(result, expected_fields, f"{kind} worker result")
    for field_name, expected_type in required[kind].items():
        if type(result.get(field_name)) is not expected_type:
            raise ProtocolError(
                f"{kind} worker result {field_name} must be {expected_type.__name__}"
            )
    if result["run_id"] != run_id:
        raise ProtocolError("worker result run_id does not match request")
    if kind == "artifact_edit":
        if result["artifact_type"] not in {"poster", "deck", "landing", "video"}:
            raise ProtocolError("artifact edit result type is unsupported")
        for field_name in ("restored_layer_ids", "skipped"):
            if not all(isinstance(value, str) for value in result[field_name]):
                raise ProtocolError(
                    f"artifact edit worker result {field_name} must contain strings"
                )
    if kind == "video_export_retry" and (
        result["ok"] is not True or result["phase"] != "done"
    ):
        raise ProtocolError("video export retry result must be a successful done result")
    if kind == "video_export_retry":
        if not all(
            type(warning) is str
            for warning in result["pointer_cleanup_warnings"]
        ):
            raise ProtocolError(
                "video export retry pointer_cleanup_warnings must contain strings"
            )
        render_started_at = result["render_started_at"]
        if not render_started_at or render_started_at != render_started_at.strip():
            raise ProtocolError("video export retry render_started_at must be an ISO timestamp")
        try:
            parsed_render_started_at = datetime.fromisoformat(render_started_at)
        except ValueError as exc:
            raise ProtocolError(
                "video export retry render_started_at must be an ISO timestamp"
            ) from exc
        if parsed_render_started_at.tzinfo is None:
            raise ProtocolError(
                "video export retry render_started_at must include a timezone"
            )


def _validate_error_result(kind: str, error: dict[str, Any]) -> None:
    required = {"type", "message"}
    optional = {"traceback"}
    if kind == "video_export_retry":
        required.add("pointer_cleanup_warnings")
        optional.add("phase")
    if not required.issubset(error) or not set(error).issubset(required | optional):
        raise ProtocolError(
            "worker error fields are invalid for its job kind"
        )
    for name in set(error) - {"pointer_cleanup_warnings"}:
        if not isinstance(error[name], str):
            raise ProtocolError(f"worker error {name} must be a string")
    if "pointer_cleanup_warnings" in error:
        warnings = error["pointer_cleanup_warnings"]
        if type(warnings) is not list or not all(
            type(warning) is str for warning in warnings
        ):
            raise ProtocolError(
                "worker error pointer_cleanup_warnings must be a list of strings"
            )


def _validate_request_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ProtocolError("request must be a JSON object")
    kind = payload.get("job_kind")
    if kind not in _REQUEST_FIELDS:
        raise ProtocolError(f"unknown worker job_kind: {kind!r}")
    if kind == "candidate_publish":
        actual_fields = set(payload)
        canonical_fields = set(_REQUEST_FIELDS[kind])
        if actual_fields not in {
            _LEGACY_CANDIDATE_PUBLISH_FIELDS,
            frozenset(canonical_fields),
        }:
            _require_exact_object(payload, canonical_fields, f"{kind} request")
    else:
        _require_exact_object(payload, set(_REQUEST_FIELDS[kind]), f"{kind} request")
    _require_identifier(payload["run_id"], "run_id")
    if kind != "pipeline":
        _require_identifier(payload["parent_run_id"], "parent_run_id")
        if payload["run_id"] == payload["parent_run_id"]:
            raise ProtocolError("derived run_id must differ from parent_run_id")
    if "settings" in payload:
        _validate_settings_payload(payload["settings"])
    if kind == "pipeline":
        _require_string(payload["brief"], "brief")
        if not isinstance(payload["attachments"], (list, tuple)) or not all(
            isinstance(item, str) for item in payload["attachments"]
        ):
            raise ProtocolError("attachments must contain only strings")
        for name in (
            "template",
            "palette_id",
            "canvas_preset_id",
            "resume_run",
            "reference_poster",
        ):
            _require_optional_string(payload[name], name)
    elif kind == "artifact_edit":
        _require_string(payload["input_path"], "input_path")
        _require_string(payload["conversation_id"], "conversation_id")
    elif kind == "editable_video_render":
        _require_json_object(payload["artifact"], "artifact")
        _require_string(payload["conversation_id"], "conversation_id")
        _require_string(payload["baseline_artifact_json"], "baseline_artifact_json")
    elif kind == "poster_code_edit":
        _require_string(payload["source_poster"], "source_poster")
        _require_json_object(payload["artifact"], "artifact")
        _require_string(payload["instruction"], "instruction")
        if not isinstance(payload["conversation_history"], (list, tuple)) or not all(
            isinstance(item, dict) for item in payload["conversation_history"]
        ):
            raise ProtocolError("conversation_history must contain JSON objects")
        for item in payload["conversation_history"]:
            _require_json_object(item, "conversation_history item")
        if payload["selection_context"] is not None:
            _require_json_object(payload["selection_context"], "selection_context")
        _require_optional_string(payload["palette_id"], "palette_id")
        _require_json_object(payload["required_color_system"], "required_color_system")
        _require_string(payload["conversation_id"], "conversation_id")
        _require_string(payload["baseline_artifact_json"], "baseline_artifact_json")
    elif kind == "pptx_export":
        _require_string(payload["source_html"], "source_html")
        _require_json_object(payload["artifact"], "artifact")
        _require_string(payload["artifact_name"], "artifact_name")
        _require_string(payload["conversation_id"], "conversation_id")
    elif kind == "video_export_retry":
        _require_string(payload["source_project"], "source_project")
        _require_string(payload["conversation_id"], "conversation_id")
        _require_string(payload["baseline_artifact_json"], "baseline_artifact_json")
        _require_string(payload["runs_dir"], "runs_dir")
    elif kind == "attempt_fork":
        if type(payload["attempt"]) is not int or payload["attempt"] <= 0:
            raise ProtocolError("attempt must be a positive integer")
        _require_string(
            payload["expected_candidate_sha256"],
            "expected_candidate_sha256",
        )
        if len(payload["expected_candidate_sha256"]) != 64:
            raise ProtocolError("expected_candidate_sha256 must be a SHA-256 digest")
        _require_string(payload["conversation_id"], "conversation_id")
    elif kind == "candidate_publish":
        _require_string(payload["conversation_id"], "conversation_id")
        if set(payload) == _LEGACY_CANDIDATE_PUBLISH_FIELDS:
            return
        source_attempt = payload.get("source_attempt")
        expected_sha256 = payload.get("expected_candidate_sha256")
        if source_attempt is None or expected_sha256 is None:
            raise ProtocolError(
                "source_attempt and expected_candidate_sha256 must be provided together"
            )
        if type(source_attempt) is not int or source_attempt <= 0:
            raise ProtocolError("source_attempt must be a positive integer")
        if (
            type(expected_sha256) is not str
            or _SHA256_PATTERN.fullmatch(expected_sha256) is None
        ):
            raise ProtocolError(
                "expected_candidate_sha256 must be a lowercase SHA-256 digest"
            )


def _validate_settings_payload(payload: Any) -> None:
    _require_exact_object(payload, set(_SETTINGS_FIELDS), "settings")
    declared_fields = {field.name for field in fields(Settings)}
    if declared_fields != set(_SETTINGS_FIELDS):
        raise ProtocolError("Settings dataclass and worker protocol allowlist differ")
    annotations = get_type_hints(Settings)
    for name in _SETTINGS_FIELDS:
        if name in _PATH_FIELDS:
            _require_string(payload[name], name)
        elif not _matches_annotation(payload[name], annotations[name]):
            raise ProtocolError(f"settings.{name} has the wrong primitive or Literal type")
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ProtocolError("settings contains a non-JSON value") from exc


def _read_exact(stream: BinaryIO, count: int, label: str) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise ProtocolError(f"truncated worker {label}")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _require_exact_object(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ProtocolError(f"{label} must be a JSON object")
    actual = set(value)
    if actual != expected:
        raise ProtocolError(
            f"{label} fields mismatch (missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)})"
        )


def _require_identifier(value: Any, name: str) -> None:
    _require_string(value, name)
    try:
        validate_run_id(value)
    except RunControlError as exc:
        raise ProtocolError(f"{name} is not a safe identifier") from exc


def _require_string(value: Any, name: str) -> None:
    if not isinstance(value, str):
        raise ProtocolError(f"{name} must be a string")


def _require_optional_string(value: Any, name: str) -> None:
    if value is not None:
        _require_string(value, name)


def _require_json_object(value: Any, name: str) -> None:
    if not isinstance(value, dict):
        raise ProtocolError(f"{name} must be a JSON object")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"{name} must contain only JSON values") from exc


def _matches_annotation(value: Any, annotation: Any) -> bool:
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in {types.UnionType, Union}:
        return any(_matches_annotation(value, argument) for argument in arguments)
    if origin is Literal:
        return type(value) is type(arguments[0]) and value in arguments
    if origin is dict:
        key_type, value_type = arguments
        return isinstance(value, dict) and all(
            _matches_annotation(key, key_type) and _matches_annotation(item, value_type)
            for key, item in value.items()
        )
    if annotation is Any:
        return True
    if annotation is type(None):
        return value is None
    if annotation in {bool, int, float, str}:
        return type(value) is annotation
    return isinstance(value, annotation)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ProtocolError(f"non-finite JSON number is forbidden: {value}")
