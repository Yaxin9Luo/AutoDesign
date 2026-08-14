"""Run and classify external coding-harness paper-poster matrices."""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import (
    REPO_ROOT,
    Settings,
    codex_binary_candidates,
    code_editor_command_for_harness,
    designer_author_command_for_harness,
    resolve_harness_binary,
)
from .util.io import atomic_write_json, sha256_file


CODING_HARNESSES: tuple[str, ...] = (
    "codex",
    "claude",
    "opencode",
    "kimi",
    "mimo",
    "pi",
    "zcode",
)

_MODEL_SELECTION_MODE: dict[str, str] = {
    "custom": "explicit_command",
    "codex": "cli_flag",
    "claude": "cli_flag",
    "opencode": "cli_flag",
    "kimi": "cli_flag",
    "mimo": "cli_flag",
    "pi": "cli_flag",
    "zcode": "locked_config",
}

_HARNESS_BINARY: dict[str, tuple[str, Path | None]] = {
    "codex": ("codex", None),
    "claude": ("claude", None),
    "opencode": ("opencode", None),
    "kimi": ("kimi", None),
    "mimo": ("mimo", None),
    "pi": ("pi", None),
    "zcode": ("zcode", None),
}

_PI_DESIGNER_AUTHOR_BINARY_ENV_KEYS: tuple[str, ...] = (
    "AUTODESIGN_DESIGNER_AUTHOR_PI_BIN",
    "DESIGN_ANYTHING_DESIGNER_AUTHOR_PI_BIN",
    "DESIGN_ANYTHING_PLANNER_AUTHOR_PI_BIN",
)

_PI_CODE_EDITOR_BINARY_ENV_KEYS: tuple[str, ...] = (
    "AUTODESIGN_CODE_EDITOR_PI_BIN",
    "DESIGN_ANYTHING_CODE_EDITOR_PI_BIN",
)

_TERMINAL_ROW_STATUSES = {"completed", "cancelled", "error"}
_STRICT_SUCCESS_OUTCOMES = {"full_gate_pass"}
_USABLE_OUTCOMES = {
    "full_gate_pass",
    "soft_accept",
    "best_candidate_fallback",
    "best_available_with_warnings",
}
_HARD_FAILURE_OUTCOMES = {
    "best_available_hard_diagnostics",
    "command_no_output",
    "command_timeout",
    "poster_written_gate_failed",
    "process_error",
    "stale_fallback_manifest",
}


@dataclass(frozen=True)
class HarnessMatrixCellSpec:
    harness: str
    model: str | None = None


def build_coding_harness_capabilities(settings: Settings | None = None) -> dict[str, dict[str, Any]]:
    """Describe local coding-harness support without running any harness."""

    capabilities: dict[str, dict[str, Any]] = {}
    for harness in CODING_HARNESSES:
        binary_name, fallback_path = _HARNESS_BINARY[harness]
        if harness == "codex":
            codex_candidates = codex_binary_candidates()
            binary_path = str(codex_candidates[0]["binary"]) if codex_candidates else ""
            source = str(codex_candidates[0]["source"]) if codex_candidates else "missing"
        else:
            configured_binary = _configured_harness_binary(harness)
            path_binary = shutil.which(binary_name)
            binary_path = configured_binary or path_binary
            source = (
                "configured"
                if configured_binary
                else "path"
                if path_binary
                else "missing"
            )
        if not binary_path and fallback_path is not None and fallback_path.exists():
            binary_path = str(fallback_path)
            source = "fallback_path"
        designer_model = getattr(settings, "designer_author_model", None) if settings else None
        code_model = getattr(settings, "code_editor_model", None) if settings else None
        capabilities[harness] = {
            "id": harness,
            "binary": binary_path or binary_name,
            "binary_source": source,
            "available": bool(binary_path),
            "model_selection_mode": _MODEL_SELECTION_MODE[harness],
            "supports_hard_model_arg": _MODEL_SELECTION_MODE[harness] == "cli_flag",
            "notes": _harness_notes(harness),
            "surfaces": {
                "designer_author": {
                    "model": designer_model or "",
                    "cmd": (
                        designer_author_command_for_harness(harness, designer_model)
                        if binary_path
                        else ""
                    ),
                },
                "code_editor": {
                    "model": code_model or "",
                    "cmd": (
                        code_editor_command_for_harness(harness, code_model)
                        if binary_path
                        else ""
                    ),
                },
            },
        }
    return capabilities


def _configured_harness_binary(harness: str) -> str:
    if harness != "pi":
        return ""
    for env_key in (*_PI_DESIGNER_AUTHOR_BINARY_ENV_KEYS, *_PI_CODE_EDITOR_BINARY_ENV_KEYS):
        value = str(os.environ.get(env_key) or "").strip()
        if value:
            return value
    return ""


def run_harness_matrix(
    *,
    paper_path: str | Path,
    prompt: str,
    template: str = "cvpr-landscape",
    harnesses: list[HarnessMatrixCellSpec] | None = None,
    attempts: int = 12,
    timeout_s: int = 3600,
    identity_logo_agent: str = "off",
    identity_logo_agent_harness: str = "codex",
    identity_logo_agent_timeout_s: int = 240,
    concurrency: str = "by_harness",
    reuse_ingest_run: str | None = None,
    env_overrides: dict[str, str] | None = None,
    out_dir: str | Path | None = None,
    matrix_id: str | None = None,
    cancel_event: threading.Event | None = None,
    on_update: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run one paper-poster generation cell per harness and persist a ledger."""
    del identity_logo_agent, identity_logo_agent_harness, identity_logo_agent_timeout_s

    paper = Path(paper_path).expanduser().resolve()
    if not paper.exists() or not paper.is_file():
        raise FileNotFoundError(f"paper not found: {paper}")
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise ValueError("prompt is required")
    specs = harnesses or [HarnessMatrixCellSpec(h) for h in CODING_HARNESSES]
    normalized_specs = [_normalize_cell_spec(spec) for spec in specs]
    _assert_unique_harnesses(normalized_specs)
    if concurrency != "by_harness":
        raise ValueError("only concurrency='by_harness' is supported")

    root = Path(out_dir).expanduser().resolve() if out_dir is not None else REPO_ROOT / "out"
    matrix_root = root / "harness_matrix"
    matrix_id = matrix_id or _new_matrix_id()
    matrix_dir = matrix_root / matrix_id
    matrix_dir.mkdir(parents=True, exist_ok=True)
    lock = threading.RLock()
    cancelled = cancel_event or threading.Event()

    matrix: dict[str, Any] = {
        "matrix_id": matrix_id,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "status": "running",
        "paper_id": paper.parent.name,
        "paper_path": str(paper),
        "prompt_chars": len(clean_prompt),
        "template": template,
        "attempts": attempts,
        "timeout_s": timeout_s,
        "concurrency": concurrency,
        "reuse_ingest_run": reuse_ingest_run or "",
        "matrix_dir": str(matrix_dir),
        "report_path": str(matrix_dir / "report.md"),
        "strict_success": False,
        "hard_failure_count": 0,
        "summary": {},
        "rows": [
            _initial_row(
                matrix_id=matrix_id,
                matrix_dir=matrix_dir,
                paper=paper,
                template=template,
                attempts=attempts,
                timeout_s=timeout_s,
                spec=spec,
            )
            for spec in normalized_specs
        ],
    }
    _persist_matrix(matrix, matrix_dir, lock, on_update)

    started = time.monotonic()
    row_by_harness = {str(row["harness"]): row for row in matrix["rows"]}
    max_workers = max(1, len(normalized_specs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _run_cell,
                row_by_harness[spec.harness],
                paper,
                clean_prompt,
                template,
                attempts,
                timeout_s,
                reuse_ingest_run,
                env_overrides or {},
                matrix_dir,
                cancelled,
                matrix,
                lock,
                on_update,
            ): spec
            for spec in normalized_specs
        }
        for future in concurrent.futures.as_completed(future_map):
            spec = future_map[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                with lock:
                    row = row_by_harness[spec.harness]
                    row.update({
                        "status": "error",
                        "outcome_class": "process_error",
                        "primary_blocker": f"matrix runner error: {exc}",
                        "finished_at": _now_iso(),
                    })
                    _persist_matrix(matrix, matrix_dir, lock, on_update)

    with lock:
        _refresh_matrix_summary(matrix)
        if cancelled.is_set() and any(row["status"] == "cancelled" for row in matrix["rows"]):
            matrix["status"] = "cancelled"
        elif all(row["status"] in _TERMINAL_ROW_STATUSES for row in matrix["rows"]):
            matrix["status"] = "completed_with_failures" if matrix.get("hard_failure_count") else "completed"
        else:
            matrix["status"] = "error"
        matrix["wall_seconds"] = round(time.monotonic() - started, 2)
        matrix["updated_at"] = _now_iso()
        _write_report(matrix, matrix_dir)
        _persist_matrix(matrix, matrix_dir, lock, on_update)
    return matrix


def load_harness_matrix(matrix_dir: str | Path) -> dict[str, Any]:
    path = Path(matrix_dir) / "matrix.json"
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def classify_run_dir(run_dir: str | Path, *, returncode: int | None = None) -> dict[str, Any]:
    """Classify a completed AutoDesign run directory for matrix reporting."""

    root = Path(run_dir)
    final_html = root / "final" / "poster.html"
    final_preview = root / "final" / "preview.png"
    run_events = _read_run_events(root / "run_events.jsonl")
    terminal_status = _latest_event_value(run_events, "run.done", "terminal_status") or ""
    fallback_type = ""
    outcome_class = ""
    primary_blocker = ""
    hard_issue_ids: list[str] = []
    fallback_manifest = ""
    quality_status = ""
    source_reason = ""

    best_available_path = root / "final" / "designer_author_best_available_artifact_fallback.json"
    best_candidate_path = root / "final" / "designer_author_best_candidate_fallback.json"
    best_available = _read_optional_json(best_available_path)
    best_candidate = _read_optional_json(best_candidate_path)
    direct_manifest = _read_optional_json(root / "final" / "designer_author_direct_manifest.json")
    soft_accept = any(event.get("event") == "designer_author.direct_final_soft_accept" for event in run_events)

    if best_available:
        fallback_manifest = str(best_available_path)
        quality_status = _fallback_quality_status(best_available)
        source_reason = str(best_available.get("source_reason") or "")
        fallback_type = "best_available_artifact_fallback"
        hard_issue_ids = _string_list(best_available.get("remaining_hard_issue_ids"))
        if not _fallback_manifest_matches_final_html(best_available, final_html):
            outcome_class = "stale_fallback_manifest"
            primary_blocker = "fallback manifest is stale or unbound from final/poster.html"
            hard_issue_ids = ["stale_fallback_manifest"]
        elif quality_status in {"ready", "ready_with_warnings"} and not hard_issue_ids:
            outcome_class = "best_available_with_warnings"
            primary_blocker = ""
        else:
            # Backward-compatible classification for historical run directories.
            outcome_class = "best_available_hard_diagnostics"
            primary_blocker = "finalized with hard diagnostics"
    elif best_candidate:
        fallback_manifest = str(best_candidate_path)
        quality_status = _fallback_quality_status(best_candidate)
        source_reason = str(best_candidate.get("source_reason") or "")
        fallback_type = "best_candidate_fallback"
        hard_issue_ids = _string_list(best_candidate.get("remaining_hard_issue_ids"))
        if not _fallback_manifest_matches_final_html(best_candidate, final_html):
            outcome_class = "stale_fallback_manifest"
            primary_blocker = "fallback manifest is stale or unbound from final/poster.html"
            hard_issue_ids = ["stale_fallback_manifest"]
        else:
            outcome_class = "best_candidate_fallback"
            primary_blocker = "finalized best usable candidate"
    elif final_html.exists() and soft_accept:
        outcome_class = "soft_accept"
        primary_blocker = ""
    elif final_html.exists() and terminal_status == "pass":
        outcome_class = "full_gate_pass"
        primary_blocker = ""
    elif final_html.exists() and direct_manifest:
        outcome_class = "poster_written_gate_failed"
        primary_blocker = f"terminal_status={terminal_status or 'unknown'}"
    elif final_html.exists():
        outcome_class = "poster_written_gate_failed"
        primary_blocker = f"terminal_status={terminal_status or 'unknown'}"
    else:
        last_log = _last_attempt_log(root)
        timeout = bool(last_log.get("timeout")) if last_log else False
        reason = str(last_log.get("reason") or "") if last_log else ""
        poster_sha = str(last_log.get("poster_sha256") or "") if last_log else ""
        if timeout:
            outcome_class = "command_timeout"
            primary_blocker = "last designer-author process timed out"
        elif reason == "process_exit" and not poster_sha:
            outcome_class = "command_no_output"
            primary_blocker = "process exited without poster.html"
        elif returncode not in (None, 0):
            outcome_class = "process_error"
            primary_blocker = f"cli returncode={returncode}"
        else:
            outcome_class = "command_no_output"
            primary_blocker = "final/poster.html missing"
        hard_issue_ids = _hard_issue_ids_from_latest_feedback(root)

    attempts_seen = len(list((root / "designer_author").glob("attempt_*"))) if (root / "designer_author").exists() else 0
    last_log = _last_attempt_log(root)
    return {
        "terminal_status": terminal_status,
        "outcome_class": outcome_class,
        "fallback_type": fallback_type,
        "fallback_manifest": fallback_manifest,
        "quality_status": quality_status,
        "source_reason": source_reason,
        "primary_blocker": primary_blocker,
        "hard_issue_ids": hard_issue_ids,
        "attempts_seen": attempts_seen,
        "last_process_reason": str(last_log.get("reason") or "") if last_log else "",
        "last_process_timeout": bool(last_log.get("timeout")) if last_log else False,
        "last_process_returncode": last_log.get("returncode") if last_log else None,
        "final_html": str(final_html) if final_html.exists() else "",
        "preview_png": str(final_preview) if final_preview.exists() else "",
        "final_html_url": _run_file_url(root, final_html) if final_html.exists() else "",
        "preview_url": _run_file_url(root, final_preview) if final_preview.exists() else "",
        "run_telemetry_summary": _read_optional_json(root / "run_telemetry_summary.json"),
    }


def _fallback_manifest_matches_final_html(
    manifest: dict[str, Any],
    final_html: Path,
) -> bool:
    expected_hash = str(manifest.get("html_sha256") or "").strip().lower()
    return bool(
        final_html.is_file()
        and re.fullmatch(r"[0-9a-f]{64}", expected_hash)
        and sha256_file(final_html).lower() == expected_hash
    )


def _run_cell(
    row: dict[str, Any],
    paper: Path,
    prompt: str,
    template: str,
    attempts: int,
    timeout_s: int,
    reuse_ingest_run: str | None,
    env_overrides: dict[str, str],
    matrix_dir: Path,
    cancel_event: threading.Event,
    matrix: dict[str, Any],
    lock: threading.RLock,
    on_update: Callable[[dict[str, Any]], None] | None,
) -> None:
    harness = str(row["harness"])
    requested_model = str(row.get("requested_model") or "")
    cell_dir = matrix_dir / "cells" / harness
    cell_dir.mkdir(parents=True, exist_ok=True)
    cmd = _build_cli_command(
        paper=paper,
        prompt=prompt,
        template=template,
        harness=harness,
        model=requested_model or None,
        attempts=attempts,
        timeout_s=timeout_s,
        reuse_ingest_run=reuse_ingest_run,
    )
    stdout_path = cell_dir / "stdout.log"
    stderr_path = cell_dir / "stderr.log"
    stdout_tail: deque[str] = deque(maxlen=80)
    stderr_tail: deque[str] = deque(maxlen=80)
    start = time.monotonic()
    env = os.environ.copy()
    env.update({k: v for k, v in env_overrides.items() if v})

    with lock:
        row.update({
            "status": "running",
            "started_at": _now_iso(),
            "cmd_source": "matrix_runner",
            "resolved_cmd_summary": _command_summary(cmd, prompt_chars=len(prompt)),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        })
        _persist_matrix(matrix, matrix_dir, lock, on_update)

    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            process_group_id = os.getpgid(proc.pid)
        except Exception:  # noqa: BLE001
            process_group_id = proc.pid
        with lock:
            row.update({
                "process_id": proc.pid,
                "process_group_id": process_group_id,
            })
            _persist_matrix(matrix, matrix_dir, lock, on_update)
        stdout_thread = threading.Thread(
            target=_read_pipe,
            args=(proc.stdout, stdout_path, stdout_tail),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_read_pipe,
            args=(proc.stderr, stderr_path, stderr_tail),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        last_progress_at = 0.0
        while proc.poll() is None:
            if cancel_event.is_set():
                _terminate_process_group(proc)
                break
            now = time.monotonic()
            if now - last_progress_at >= 10.0:
                last_progress_at = now
                _refresh_row_live_progress(
                    row,
                    matrix,
                    matrix_dir,
                    lock,
                    on_update,
                    stdout_tail,
                    stderr_tail,
                )
            time.sleep(1.0)
        returncode = proc.wait(timeout=15)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
    except Exception as exc:  # noqa: BLE001
        if proc is not None and proc.poll() is None:
            _terminate_process_group(proc)
        with lock:
            row.update({
                "status": "error",
                "finished_at": _now_iso(),
                "wall_seconds": round(time.monotonic() - start, 2),
                "returncode": None,
                "outcome_class": "process_error",
                "primary_blocker": str(exc),
                "stdout_tail": "".join(stdout_tail)[-4000:],
                "stderr_tail": "".join(stderr_tail)[-4000:],
            })
            _persist_matrix(matrix, matrix_dir, lock, on_update)
        return

    stdout_text = _read_text(stdout_path)
    stderr_text = _read_text(stderr_path)
    run_id = _extract_run_id(stdout_text, stderr_text)
    run_dir = _extract_run_dir(stdout_text)
    if not run_dir and run_id:
        run_dir = str(REPO_ROOT / "out" / "runs" / run_id)
    classification: dict[str, Any] = {}
    if run_dir and Path(run_dir).exists():
        classification = classify_run_dir(run_dir, returncode=returncode)
    elif cancel_event.is_set():
        classification = {
            "outcome_class": "process_error",
            "primary_blocker": "matrix run cancelled before run directory was discovered",
        }
    else:
        classification = {
            "outcome_class": "process_error" if returncode else "command_no_output",
            "primary_blocker": "run directory was not discovered",
        }

    with lock:
        row.update(classification)
        row.update({
            "status": "cancelled" if cancel_event.is_set() else "completed",
            "finished_at": _now_iso(),
            "wall_seconds": round(time.monotonic() - start, 2),
            "returncode": returncode,
            "run_id": run_id or "",
            "run_dir": run_dir or "",
            "stdout_tail": stdout_text[-4000:],
            "stderr_tail": stderr_text[-4000:],
        })
        _persist_matrix(matrix, matrix_dir, lock, on_update)


def _build_cli_command(
    *,
    paper: Path,
    prompt: str,
    template: str,
    harness: str,
    model: str | None,
    attempts: int,
    timeout_s: int,
    reuse_ingest_run: str | None,
) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "autodesign.cli",
        "run",
        prompt,
        "--from-file",
        str(paper),
        "--template",
        template,
        "--planner-author",
        "external",
        "--planner-author-harness",
        harness,
        "--planner-author-timeout",
        str(timeout_s),
        "--planner-author-max-attempts",
        str(attempts),
    ]
    if model:
        cmd.extend(["--planner-author-model", model])
    if reuse_ingest_run:
        cmd.extend(["--reuse-ingest-run", reuse_ingest_run])
    return cmd


def _initial_row(
    *,
    matrix_id: str,
    matrix_dir: Path,
    paper: Path,
    template: str,
    attempts: int,
    timeout_s: int,
    spec: HarnessMatrixCellSpec,
) -> dict[str, Any]:
    requested_model = (spec.model or "").strip()
    return {
        "matrix_id": matrix_id,
        "paper_id": paper.parent.name,
        "paper_path": str(paper),
        "template": template,
        "harness": spec.harness,
        "requested_model": requested_model,
        "effective_model_note": (
            "model is passed as a CLI flag"
            if _MODEL_SELECTION_MODE[spec.harness] == "cli_flag"
            else "model is selected under a config lock and restored after the invocation"
        ),
        "model_selection_mode": _MODEL_SELECTION_MODE[spec.harness],
        "attempt_budget": attempts,
        "timeout_s": timeout_s,
        "status": "pending",
        "outcome_class": "",
        "fallback_type": "",
        "fallback_manifest": "",
        "quality_status": "",
        "source_reason": "",
        "terminal_status": "",
        "primary_blocker": "",
        "run_id": "",
        "run_dir": "",
        "attempts_seen": 0,
        "wall_seconds": None,
        "returncode": None,
        "process_id": None,
        "process_group_id": None,
        "final_html": "",
        "preview_png": "",
        "final_html_url": "",
        "preview_url": "",
        "report_path": str(matrix_dir / "report.md"),
        "last_process_reason": "",
        "hard_issue_ids": [],
        "stdout_tail": "",
        "stderr_tail": "",
    }


def _normalize_cell_spec(spec: HarnessMatrixCellSpec) -> HarnessMatrixCellSpec:
    harness = str(spec.harness or "").strip().lower().replace("_", "-")
    aliases = {
        "claude-code": "claude",
        "cloud-code": "claude",
        "open-code": "opencode",
        "kimi-code": "kimi",
        "mimo-code": "mimo",
        "pi-coding-agent": "pi",
        "z-code": "zcode",
        "zai-code": "zcode",
        "z-ai-code": "zcode",
    }
    harness = aliases.get(harness, harness)
    if harness not in CODING_HARNESSES:
        raise ValueError(f"unsupported harness: {spec.harness}")
    return HarnessMatrixCellSpec(harness=harness, model=(spec.model or "").strip() or None)


def _assert_unique_harnesses(specs: list[HarnessMatrixCellSpec]) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for spec in specs:
        if spec.harness in seen:
            duplicates.append(spec.harness)
        seen.add(spec.harness)
    if duplicates:
        names = ", ".join(sorted(set(duplicates)))
        raise ValueError(f"duplicate harness in matrix request: {names}")


def _harness_notes(harness: str) -> str:
    if harness == "zcode":
        return "ZCode headless CLI does not expose a stable --model flag; the wrapper selects its config under a lock and restores it afterward."
    if harness == "custom":
        return "Custom commands own their own model/provider handling."
    return "Requested model is passed through the harness command line."


def _command_summary(cmd: list[str], *, prompt_chars: int) -> str:
    display = list(cmd)
    if len(display) > 4 and display[3] == "run":
        display[4] = f"<prompt:{prompt_chars} chars>"
    return shlex.join(display)


def _read_pipe(pipe: Any, path: Path, tail: deque[str]) -> None:
    if pipe is None:
        return
    with path.open("w", encoding="utf-8") as fh:
        for line in iter(pipe.readline, ""):
            fh.write(line)
            fh.flush()
            tail.append(line)
    try:
        pipe.close()
    except Exception:  # noqa: BLE001
        pass


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:  # noqa: BLE001
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:  # noqa: BLE001
            proc.kill()


def _persist_matrix(
    matrix: dict[str, Any],
    matrix_dir: Path,
    lock: threading.RLock,
    on_update: Callable[[dict[str, Any]], None] | None,
) -> None:
    with lock:
        _refresh_matrix_summary(matrix)
        matrix["updated_at"] = _now_iso()
        atomic_write_json(matrix_dir / "matrix.json", matrix)
        if on_update is not None:
            on_update(matrix)


def _refresh_row_live_progress(
    row: dict[str, Any],
    matrix: dict[str, Any],
    matrix_dir: Path,
    lock: threading.RLock,
    on_update: Callable[[dict[str, Any]], None] | None,
    stdout_tail: deque[str],
    stderr_tail: deque[str],
) -> None:
    stdout_text = "".join(stdout_tail)
    stderr_text = "".join(stderr_tail)
    run_id = str(row.get("run_id") or "").strip() or _extract_run_id(stdout_text, stderr_text)
    run_dir = str(row.get("run_dir") or "").strip() or _extract_run_dir(stdout_text) or _extract_run_dir_from_json_events(stderr_text)
    if not run_dir and run_id:
        run_dir = str(REPO_ROOT / "out" / "runs" / run_id)
    updates: dict[str, Any] = {
        "stdout_tail": stdout_text[-4000:],
        "stderr_tail": stderr_text[-4000:],
    }
    if run_id:
        updates["run_id"] = run_id
    if run_dir and Path(run_dir).exists():
        root = Path(run_dir)
        updates["run_dir"] = str(root)
        updates["attempts_seen"] = len(list((root / "designer_author").glob("attempt_*"))) if (root / "designer_author").exists() else 0
        run_events = _read_run_events(root / "run_events.jsonl")
        terminal_status = _latest_event_value(run_events, "run.done", "terminal_status") or ""
        if terminal_status:
            updates["terminal_status"] = terminal_status
        last_log = _last_attempt_log(root)
        if last_log:
            updates["last_process_reason"] = str(last_log.get("reason") or "")
            updates["last_process_timeout"] = bool(last_log.get("timeout"))
            updates["last_process_returncode"] = last_log.get("returncode")
    with lock:
        row.update(updates)
        _persist_matrix(matrix, matrix_dir, lock, on_update)


def _refresh_matrix_summary(matrix: dict[str, Any]) -> None:
    rows = [row for row in matrix.get("rows", []) if isinstance(row, dict)]
    total = len(rows)
    outcome_counts: dict[str, int] = {}
    hard_failure_count = 0
    usable_count = 0
    strict_success_count = 0
    terminal_count = 0
    for row in rows:
        outcome = str(row.get("outcome_class") or "").strip()
        if outcome:
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        if str(row.get("status") or "") in _TERMINAL_ROW_STATUSES:
            terminal_count += 1
        if outcome in _STRICT_SUCCESS_OUTCOMES:
            strict_success_count += 1
        if outcome in _USABLE_OUTCOMES:
            usable_count += 1
        if outcome in _HARD_FAILURE_OUTCOMES or str(row.get("status") or "") == "error":
            hard_failure_count += 1
    matrix["strict_success"] = total > 0 and strict_success_count == total
    matrix["hard_failure_count"] = hard_failure_count
    matrix["summary"] = {
        "total_cells": total,
        "terminal_cells": terminal_count,
        "strict_success_count": strict_success_count,
        "usable_count": usable_count,
        "hard_failure_count": hard_failure_count,
        "outcome_counts": outcome_counts,
    }


def _fallback_quality_status(manifest: Any) -> str:
    if not isinstance(manifest, dict):
        return ""
    direct = str(manifest.get("quality_status") or "").strip()
    if direct:
        return direct
    acceptance = manifest.get("acceptance")
    if isinstance(acceptance, dict):
        return str(acceptance.get("quality_status") or "").strip()
    return ""


def _write_report(matrix: dict[str, Any], matrix_dir: Path) -> None:
    summary = matrix.get("summary") if isinstance(matrix.get("summary"), dict) else {}
    lines = [
        f"# Harness Matrix {matrix.get('matrix_id')}",
        "",
        f"- Paper: `{matrix.get('paper_id')}`",
        f"- Template: `{matrix.get('template')}`",
        f"- Status: `{matrix.get('status')}`",
        f"- Strict success: `{bool(matrix.get('strict_success'))}`",
        f"- Hard failures: `{summary.get('hard_failure_count', matrix.get('hard_failure_count', 0))}`",
        "- Note: row `Status=completed` means the cell reached a terminal lifecycle state; "
        "strict poster success still requires `Outcome=full_gate_pass`.",
        f"- Attempts: `{matrix.get('attempts')}`",
        f"- Timeout: `{matrix.get('timeout_s')}s`",
        "",
        "| Harness | Status | Outcome | Terminal | Attempts | Wall s | Quality | Source reason | Blocker |",
        "|---|---|---|---|---:|---:|---|---|---|",
    ]
    for row in matrix.get("rows", []):
        lines.append(
            "| {harness} | {status} | {outcome} | {terminal} | {attempts} | {wall} | {quality} | {reason} | {blocker} |".format(
                harness=_md_cell(row.get("harness")),
                status=_md_cell(row.get("status")),
                outcome=_md_cell(row.get("outcome_class")),
                terminal=_md_cell(row.get("terminal_status")),
                attempts=row.get("attempts_seen") or 0,
                wall=row.get("wall_seconds") or "",
                quality=_md_cell(row.get("quality_status")),
                reason=_md_cell(row.get("source_reason")),
                blocker=_md_cell(row.get("primary_blocker")),
            )
        )
    lines.extend(["", "## Artifacts", ""])
    for row in matrix.get("rows", []):
        lines.append(f"### {row.get('harness')}")
        for label, key in (
            ("Run dir", "run_dir"),
            ("Final HTML", "final_html"),
            ("Preview", "preview_png"),
            ("Fallback manifest", "fallback_manifest"),
            ("Stdout", "stdout_path"),
            ("Stderr", "stderr_path"),
        ):
            value = str(row.get(key) or "").strip()
            if value:
                lines.append(f"- {label}: `{value}`")
        hard_ids = row.get("hard_issue_ids") or []
        if hard_ids:
            lines.append(f"- Hard issues: `{', '.join(str(x) for x in hard_ids)}`")
        lines.append("")
    (matrix_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def _md_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")[:180]


def _read_run_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            events.append(data)
    return events


def _latest_event_value(events: list[dict[str, Any]], event_name: str, key: str) -> Any:
    for event in reversed(events):
        if event.get("event") == event_name:
            return event.get(key)
    return None


def _last_attempt_log(run_dir: Path) -> dict[str, Any]:
    attempt_root = run_dir / "designer_author"
    logs = sorted(attempt_root.glob("attempt_*/designer_author_log.json"))
    if not logs:
        return {}
    data = _read_optional_json(logs[-1])
    return data if isinstance(data, dict) else {}


def _hard_issue_ids_from_latest_feedback(run_dir: Path) -> list[str]:
    feedbacks = sorted((run_dir / "designer_author").glob("attempt_*/validation_feedback.json"))
    if not feedbacks:
        return []
    feedback = _read_optional_json(feedbacks[-1])
    if not isinstance(feedback, dict):
        return []
    ids: list[str] = []
    for source in (feedback.get("summary"), feedback.get("payload"), feedback):
        if isinstance(source, dict):
            issue_id = source.get("issue_id")
            if issue_id:
                ids.append(str(issue_id))
            for key in ("hard_issue_ids", "remaining_hard_issue_ids"):
                ids.extend(_string_list(source.get(key)))
            issues = source.get("issues")
            if isinstance(issues, list):
                for issue in issues:
                    if not isinstance(issue, dict):
                        continue
                    severity = str(issue.get("severity") or "").lower()
                    if severity in {"hard", "required", "blocking", "error"} or issue.get("blocks_soft_accept") is True:
                        if issue.get("issue_id"):
                            ids.append(str(issue["issue_id"]))
    return sorted(set(ids))


def _read_optional_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _extract_run_dir(stdout_text: str) -> str:
    matches = re.findall(r"Run dir:\s+(.+)", stdout_text)
    return matches[-1].strip() if matches else ""


def _extract_run_id(stdout_text: str, stderr_text: str) -> str:
    run_dir = _extract_run_dir(stdout_text)
    if run_dir:
        return Path(run_dir).name
    for text in (stderr_text, stdout_text):
        for line in text.splitlines():
            if '"run_id"' not in line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            run_id = str(data.get("run_id") or "").strip()
            if run_id:
                return run_id
    return ""


def _extract_run_dir_from_json_events(text: str) -> str:
    for line in text.splitlines():
        if '"attempt_dir"' not in line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        attempt_dir = str(data.get("attempt_dir") or "").strip()
        if not attempt_dir:
            continue
        path = Path(attempt_dir)
        if path.name == "designer_author":
            return str(path.parent)
    return ""


def _run_file_url(run_dir: Path, file_path: Path) -> str:
    if not file_path.exists():
        return ""
    run_id = run_dir.name
    try:
        rel = file_path.relative_to(run_dir)
    except ValueError:
        return ""
    return f"/api/files/runs/{run_id}/{rel.as_posix()}"


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _new_matrix_id() -> str:
    return "matrix-" + datetime.now().strftime("%Y%m%d-%H%M%S")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
