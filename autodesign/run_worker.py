"""Entrypoint for one supervised AutoDesign run worker."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import shutil
import stat
import sys
import threading
import traceback
from typing import Any
from urllib.parse import urlsplit


_SECRET_ENV_NAMES = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
    "OPENAI_COMPAT_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY",
    "OPENRESEARCH_TOKEN", "AUTODESIGN_HARNESS_API_KEY", "AUTODESIGN_DESIGNER_AUTHOR_API_KEY",
    "AUTODESIGN_CODE_EDITOR_API_KEY", "DESIGN_ANYTHING_HARNESS_API_KEY",
    "DESIGN_ANYTHING_DESIGNER_AUTHOR_API_KEY", "DESIGN_ANYTHING_CODE_EDITOR_API_KEY",
    "ANTHROPIC_CUSTOM_HEADERS", "DATABASE_URL", "REDIS_URL",
})
_CAPTURED_WORKER_ENV = dict(os.environ)


class VideoExportRetryError(RuntimeError):
    """A retry failure carrying its durable phase and cleanup diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        failure_phase: str | None,
        pointer_cleanup_warnings: list[str],
    ) -> None:
        super().__init__(message)
        self.failure_phase = failure_phase
        self.pointer_cleanup_warnings = tuple(pointer_cleanup_warnings)


class VideoPointerPublicationError(VideoExportRetryError):
    """A retry failed specifically at its durable final-pointer boundary."""

    phase = "final_pointer"
    failure_phase = "final_pointer"

    def __init__(
        self,
        message: str,
        *,
        pointer_cleanup_warnings: list[str],
    ) -> None:
        super().__init__(
            message,
            failure_phase="final_pointer",
            pointer_cleanup_warnings=pointer_cleanup_warnings,
        )


def _proxy_has_credentials(value: str) -> bool:
    candidate = value.strip()
    parsed = urlsplit(candidate if "://" in candidate else f"//{candidate}")
    return parsed.username is not None or parsed.password is not None


def _environment_value_is_sensitive(name: str, value: str) -> bool:
    upper = name.upper()
    if (
        name in _SECRET_ENV_NAMES
        or any(marker in upper for marker in ("PASSWORD", "SECRET", "TOKEN", "CREDENTIAL"))
        or upper.endswith(("_API_KEY", "_ACCESS_KEY", "_PRIVATE_KEY"))
        or (upper.startswith("AWS_") and "KEY" in upper)
    ):
        return True
    if upper in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}:
        return _proxy_has_credentials(value)
    return False


def _scrub_secret_environment() -> None:
    for name, value in tuple(os.environ.items()):
        if _environment_value_is_sensitive(name, value):
            os.environ.pop(name, None)


def _restore_captured_worker_environment() -> None:
    os.environ.clear()
    os.environ.update(_CAPTURED_WORKER_ENV)
    _scrub_secret_environment()


# config.py intentionally loads .env defaults on import. Import the protocol
# only inside worker_main, then scrub again before decoding or owned work.
_scrub_secret_environment()


def _SignalCancellation(requested: threading.Event):
    """Legacy test adapter; production workers construct an authoritative token."""
    from .run_control import CancellationToken

    return CancellationToken(store=None, run_id="", signal_event=requested)


def _request_runs_dir(request: Any) -> Path:
    settings = getattr(request, "settings", None)
    if settings is not None:
        return (Path(settings.out_dir) / "runs").resolve()
    return Path(request.runs_dir).resolve()


def worker_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--spawn-nonce", default="")
    args = parser.parse_args()

    try:
        from .process_supervision import ProcessLedger
        from .run_control import (
            CancellationToken,
            RunCancelled,
            RunControlStore,
            durable_replace_json,
            validate_run_id,
        )
        from .run_worker_protocol import (
            decode_request,
            format_worker_error_message,
            sensitive_values,
        )
        from .util.logging import log, worker_run_context
    finally:
        # config imports may load repository .env defaults. Restore exactly the
        # explicit environment supplied by the supervisor, then scrub it.
        _restore_captured_worker_environment()

    validate_run_id(args.run_id)
    request = decode_request(sys.stdin.buffer)
    _restore_captured_worker_environment()
    if request.run_id != args.run_id:
        raise RuntimeError("worker request run ID does not match argv identity")
    runs_dir = _request_runs_dir(request)
    run_dir = runs_dir / request.run_id
    if args.spawn_nonce:
        ledger = ProcessLedger(run_dir).read()
        registered = any(
            record.nonce == args.spawn_nonce and record.identity.pid == os.getpid()
            for record in ledger.processes
        )
        if not registered:
            raise RuntimeError("root worker was released without durable registration")

    if os.name == "posix" and os.getsid(0) != os.getpid():
        os.setsid()
    signal_event = threading.Event()
    cancellation = CancellationToken.for_run(
        RunControlStore(runs_dir),
        request.run_id,
        signal_event=signal_event,
    )

    def request_cancel(_signum: int, _frame: Any) -> None:
        signal_event.set()

    for signal_name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, signal_name, None)
        if signum is not None:
            signal.signal(signum, request_cancel)

    run_dir.mkdir(parents=True, exist_ok=True)
    secrets = sensitive_values(request)
    with worker_run_context(request.run_id, run_dir, sensitive_values=secrets):
        log("worker.started", job_kind=request.job_kind)
        try:
            result = _dispatch(request, cancellation)
            payload = {
                "job_kind": request.job_kind,
                "run_id": request.run_id,
                "ok": True,
                "result": _redact_json(result, secrets),
            }
            log("worker.finished", job_kind=request.job_kind)
            exit_code = 0
        except RunCancelled as exc:
            pointer_cleanup_warnings = []
            if request.job_kind == "video_export_retry":
                pointer_cleanup_warnings = _stable_pointer_cleanup_warnings(
                    getattr(exc, "pointer_cleanup_warnings", ())
                )
                pointer_cleanup_warnings = [
                    _redact(warning, secrets)
                    for warning in pointer_cleanup_warnings
                ]
            message = format_worker_error_message(
                _redact(str(exc), secrets),
                pointer_cleanup_warnings,
            )
            error_payload: dict[str, Any] = {
                "type": type(exc).__name__,
                "message": message,
                "traceback": _redact("".join(traceback.format_exception(exc)), secrets)[-8000:],
            }
            if request.job_kind == "video_export_retry":
                error_payload["pointer_cleanup_warnings"] = pointer_cleanup_warnings
                error_payload["phase"] = exc.phase
            payload = {
                "job_kind": request.job_kind,
                "run_id": request.run_id,
                "ok": False,
                "error": error_payload,
            }
            log("worker.cancelled", job_kind=request.job_kind, phase=exc.phase)
            exit_code = 2
        except BaseException as exc:
            pointer_cleanup_warnings = []
            if request.job_kind == "video_export_retry":
                pointer_cleanup_warnings = _stable_pointer_cleanup_warnings(
                    getattr(exc, "pointer_cleanup_warnings", ())
                )
                pointer_cleanup_warnings = [
                    _redact(warning, secrets)
                    for warning in pointer_cleanup_warnings
                ]
            message = format_worker_error_message(
                _redact(str(exc), secrets),
                pointer_cleanup_warnings,
            )
            error_payload: dict[str, Any] = {
                "type": type(exc).__name__,
                "message": message,
                "traceback": _redact("".join(traceback.format_exception(exc)), secrets)[-8000:],
            }
            if request.job_kind == "video_export_retry":
                error_payload["pointer_cleanup_warnings"] = pointer_cleanup_warnings
                failure_phase = getattr(exc, "failure_phase", None)
                if isinstance(failure_phase, str) and failure_phase:
                    error_payload["phase"] = failure_phase
            payload = {
                "job_kind": request.job_kind,
                "run_id": request.run_id,
                "ok": False,
                "error": error_payload,
            }
            log("worker.failed", job_kind=request.job_kind, error=type(exc).__name__)
            exit_code = 1
        durable_replace_json(run_dir / "worker_result.json", payload)
    return exit_code


def _dispatch(request: Any, cancellation: Any) -> dict[str, Any]:
    cancellation.raise_if_cancelled("worker.dispatch")
    if request.job_kind == "pipeline":
        return _run_pipeline(request, cancellation)
    if request.job_kind == "artifact_edit":
        return _run_artifact_edit(request, cancellation)
    if request.job_kind == "editable_video_render":
        return _run_editable_video_render(request, cancellation)
    if request.job_kind == "poster_code_edit":
        return _run_poster_code_edit(request, cancellation)
    if request.job_kind == "pptx_export":
        return _run_pptx_export(request, cancellation)
    if request.job_kind == "video_export_retry":
        return _run_video_export_retry(request, cancellation)
    if request.job_kind == "attempt_fork":
        return _run_attempt_fork(request, cancellation)
    if request.job_kind == "candidate_publish":
        return _run_candidate_publish(request, cancellation)
    raise RuntimeError(f"unsupported worker job kind: {request.job_kind}")


def _run_pipeline(request: Any, cancellation: Any) -> dict[str, Any]:
    from .runner import PipelineRunner

    runner = PipelineRunner(request.settings)
    kwargs: dict[str, Any] = {
        "attachments": [Path(value) for value in request.attachments],
        "template": request.template,
        "run_id": request.run_id,
        "resume_run": request.resume_run,
        "reference_poster": Path(request.reference_poster) if request.reference_poster else None,
        "palette_id": request.palette_id,
    }
    kwargs["supervised"] = True
    kwargs["cancellation_token"] = cancellation
    result = runner.run(request.brief, **kwargs)
    cancellation.raise_if_cancelled("worker.pipeline.after_run")
    return result.model_dump(mode="json")


def _run_artifact_edit(request: Any, cancellation: Any) -> dict[str, Any]:
    cancellation.raise_if_cancelled("worker.artifact_edit.start")
    from .artifact_edit_job import run_artifact_edit_job

    runs_dir = (Path(request.settings.out_dir) / "runs").resolve()
    _validated_direct_run_dir(
        runs_dir,
        request.parent_run_id,
        require_existing=True,
    )
    run_dir = _validated_direct_run_dir(
        runs_dir,
        request.run_id,
        require_existing=True,
    )
    input_path = Path(request.input_path).resolve()
    if not input_path.is_relative_to(run_dir) or not input_path.is_file():
        raise RuntimeError("artifact edit input is outside its child run")
    result = run_artifact_edit_job(
        run_id=request.run_id,
        parent_run_id=request.parent_run_id,
        input_path=input_path,
        settings=request.settings,
        cancellation_token=cancellation,
    )
    cancellation.raise_if_cancelled("worker.artifact_edit.after_edit")
    return result


def _run_editable_video_render(request: Any, cancellation: Any) -> dict[str, Any]:
    cancellation.raise_if_cancelled("worker.editable_video_render.start")
    from .editable_video_job import run_editable_video_job

    runs_dir = (Path(request.settings.out_dir) / "runs").resolve()
    parent_dir = _validated_direct_run_dir(
        runs_dir,
        request.parent_run_id,
        require_existing=True,
    )
    run_dir = _validated_direct_run_dir(
        runs_dir,
        request.run_id,
        require_existing=False,
    )
    result = run_editable_video_job(
        artifact=request.artifact,
        runs_dir=runs_dir,
        editor_assets_dir=Path(request.settings.out_dir) / "editor_assets",
        source_run_dir=parent_dir,
        run_id=request.run_id,
        run_dir=run_dir,
        cancellation_token=cancellation,
    )
    cancellation.raise_if_cancelled("worker.editable_video_render.after_render")
    return {"run_id": request.run_id, "mp4_path": str(result["mp4_path"])}


def _run_video_export_retry(request: Any, cancellation: Any) -> dict[str, Any]:
    cancellation.raise_if_cancelled("worker.video_export_retry.start")
    from .process_supervision import ProcessLedger
    from .run_control import RunCancelled
    from .tools.export_video import retry_video_export_project

    runs_dir = Path(request.runs_dir).resolve()
    run_dir = _validated_direct_run_dir(
        runs_dir,
        request.run_id,
        require_existing=False,
    )
    parent_dir = _validated_direct_run_dir(
        runs_dir,
        request.parent_run_id,
        require_existing=True,
    )
    source_project = Path(request.source_project).resolve()
    if not source_project.is_relative_to(parent_dir):
        raise RuntimeError("video export retry source is outside its parent run")
    run_dir.mkdir(parents=True, exist_ok=True)
    design_spec = parent_dir / "design_spec.json"
    if not design_spec.is_file():
        raise RuntimeError("video export retry parent design_spec.json is missing")
    shutil.copy2(design_spec, run_dir / "design_spec.json")
    for optional_name in ("run_brief.json", "resume_state.json"):
        optional = parent_dir / optional_name
        if optional.is_file():
            shutil.copy2(optional, run_dir / optional_name)
    staged_project = run_dir / source_project.name
    shutil.copytree(source_project, staged_project)
    result = retry_video_export_project(
        run_dir,
        staged_project,
        cancellation_token=cancellation,
        process_ledger=ProcessLedger(run_dir),
    )
    known_pointer_cleanup_warnings: list[str] = []
    if isinstance(result, dict):
        raw_warnings = result.get("pointer_cleanup_warnings")
        if type(raw_warnings) is list and all(
            type(warning) is str for warning in raw_warnings
        ):
            known_pointer_cleanup_warnings = _stable_pointer_cleanup_warnings(
                raw_warnings
            )
    try:
        cancellation.raise_if_cancelled("worker.video_export_retry.after_retry")
    except RunCancelled as exc:
        exc.pointer_cleanup_warnings = tuple(_stable_pointer_cleanup_warnings(
            known_pointer_cleanup_warnings,
            getattr(exc, "pointer_cleanup_warnings", ()),
        ))
        raise
    if not isinstance(result, dict):
        raise RuntimeError("video export retry returned an invalid result")
    pointer_cleanup_warnings = _video_retry_pointer_cleanup_warnings(result)
    if result.get("ok") is not True:
        message = str(result.get("error") or "video export retry failed")
        if result.get("phase") == "final_pointer":
            raise VideoPointerPublicationError(
                message,
                pointer_cleanup_warnings=pointer_cleanup_warnings,
            )
        failure_phase = result.get("phase")
        raise VideoExportRetryError(
            message,
            failure_phase=(
                failure_phase
                if isinstance(failure_phase, str) and failure_phase
                else None
            ),
            pointer_cleanup_warnings=pointer_cleanup_warnings,
        )
    return {
        **result,
        "run_id": request.run_id,
        "pointer_cleanup_warnings": pointer_cleanup_warnings,
    }


def _video_retry_pointer_cleanup_warnings(result: dict[str, Any]) -> list[str]:
    if "pointer_cleanup_warnings" not in result:
        return []
    raw_warnings = result["pointer_cleanup_warnings"]
    if type(raw_warnings) is not list or not all(
        type(warning) is str for warning in raw_warnings
    ):
        raise RuntimeError(
            "video export retry returned invalid pointer cleanup warnings"
        )
    return _stable_pointer_cleanup_warnings(raw_warnings)


def _stable_pointer_cleanup_warnings(*warning_groups: object) -> list[str]:
    warnings: list[str] = []
    seen: set[str] = set()
    for group in warning_groups:
        if not isinstance(group, (list, tuple)):
            continue
        for warning in group:
            if type(warning) is str and warning not in seen:
                warnings.append(warning)
                seen.add(warning)
    return warnings


def _run_attempt_fork(request: Any, cancellation: Any) -> dict[str, Any]:
    from .attempt_fork import run_attempt_fork_job

    return run_attempt_fork_job(
        run_id=request.run_id,
        parent_run_id=request.parent_run_id,
        attempt=request.attempt,
        expected_candidate_sha256=request.expected_candidate_sha256,
        conversation_id=request.conversation_id,
        settings=request.settings,
        cancellation_token=cancellation,
    )


def _run_candidate_publish(request: Any, cancellation: Any) -> dict[str, Any]:
    from .candidate_publish import run_candidate_publish_job

    return run_candidate_publish_job(
        run_id=request.run_id,
        parent_run_id=request.parent_run_id,
        conversation_id=request.conversation_id,
        settings=request.settings,
        cancellation_token=cancellation,
        source_attempt=request.source_attempt,
        expected_candidate_sha256=request.expected_candidate_sha256,
    )


def _run_poster_code_edit(request: Any, cancellation: Any) -> dict[str, Any]:
    cancellation.raise_if_cancelled("worker.poster_code_edit.start")
    from .poster_code_edit import run_poster_code_edit_sync

    requested_runs_dir = Path(request.settings.out_dir) / "runs"
    runs_dir = requested_runs_dir.resolve()
    source_poster = Path(request.source_poster).resolve()
    parent_dir = _validated_direct_run_dir(
        runs_dir,
        request.parent_run_id,
        require_existing=True,
    )
    if not source_poster.is_relative_to(parent_dir):
        raise RuntimeError("poster edit source is outside its parent run")
    run_dir = _validated_direct_run_dir(
        runs_dir,
        request.run_id,
        require_existing=False,
    )
    result_run_dir = requested_runs_dir / request.run_id
    result = run_poster_code_edit_sync(
        run_id=request.run_id,
        runs_dir=runs_dir,
        source_run_id=request.parent_run_id,
        source_run_dir=parent_dir,
        source_poster_path=source_poster,
        artifact=request.artifact,
        instruction=request.instruction,
        conversation_history=list(request.conversation_history),
        selection_context=request.selection_context,
        required_color_system=request.required_color_system,
        settings=request.settings,
        cancellation_token=cancellation,
    )
    cancellation.raise_if_cancelled("worker.poster_code_edit.after_edit")
    poster_path = result_run_dir / "final" / "poster.html"
    preview_path = result_run_dir / "final" / "preview.png"
    if not poster_path.is_file() or not preview_path.is_file():
        raise RuntimeError("poster edit did not produce a complete final artifact")
    return {
        "run_id": request.run_id,
        "run_dir": str(result_run_dir),
        "attempt_dir": str(result.get("attempt_dir") or ""),
        "poster_path": str(poster_path),
        "preview_path": str(preview_path),
        "attempts": list(result.get("attempts") or []),
        "validation_summary": dict(result.get("validation_summary") or {}),
        "selection_context_summary": dict(
            result.get("selection_context_summary") or {}
        ),
        "promoted_assets": list(result.get("promoted_assets") or []),
    }


def _run_pptx_export(request: Any, cancellation: Any) -> dict[str, Any]:
    cancellation.raise_if_cancelled("worker.pptx_export.start")
    from .pptx_export_job import run_pptx_export_job

    runs_dir = (Path(request.settings.out_dir) / "runs").resolve()
    parent_dir = _validated_direct_run_dir(
        runs_dir,
        request.parent_run_id,
        require_existing=True,
    )
    run_dir = _validated_direct_run_dir(
        runs_dir,
        request.run_id,
        require_existing=False,
    )
    source = Path(request.source_html)
    canonical_source = source.resolve()
    if not canonical_source.is_relative_to(parent_dir):
        raise RuntimeError("PPTX export source is outside its parent run")
    result = run_pptx_export_job(
        run_id=request.run_id,
        run_dir=run_dir,
        source_html=source,
        artifact=request.artifact,
        settings=request.settings,
        cancellation_token=cancellation,
        artifact_name=request.artifact_name,
    )
    cancellation.raise_if_cancelled("worker.pptx_export.after_export")
    return {"run_id": request.run_id, "pptx_path": str(result["pptx_path"])}


def _validated_direct_run_dir(
    runs_dir: Path,
    run_id: str,
    *,
    require_existing: bool,
) -> Path:
    runs_root = Path(runs_dir).resolve()
    candidate = runs_root / run_id
    if candidate.parent != runs_root or candidate.name != run_id:
        raise RuntimeError("run directory is outside the configured runs directory")
    try:
        before = candidate.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and stat.S_ISLNK(before.st_mode):
        raise RuntimeError("run directory must not be a symlink")
    if require_existing and (before is None or not stat.S_ISDIR(before.st_mode)):
        raise RuntimeError(f"source run directory is missing: {run_id}")
    try:
        canonical = candidate.resolve(strict=require_existing)
    except OSError as exc:
        raise RuntimeError(f"could not resolve run directory: {run_id}") from exc
    if canonical.parent != runs_root or canonical.name != run_id:
        raise RuntimeError("run directory canonical identity does not match its run ID")
    if before is not None:
        try:
            after = candidate.lstat()
        except FileNotFoundError as exc:
            raise RuntimeError("run directory changed during validation") from exc
        if (
            stat.S_ISLNK(after.st_mode)
            or (before.st_dev, before.st_ino, before.st_mode)
            != (after.st_dev, after.st_ino, after.st_mode)
        ):
            raise RuntimeError("run directory changed during validation")
    return canonical


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    return text


def _redact_json(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return _redact(value, secrets)
    if isinstance(value, dict):
        return {str(key): _redact_json(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [_redact_json(item, secrets) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(worker_main())
