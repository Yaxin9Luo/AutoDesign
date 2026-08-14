"""External GUI submitter for OpenResearch Auto Research sessions.

This stage does not reproduce papers and does not know the OpenResearch UI.
It only stages a request and asks a separate GUI-capable harness to create or
open an OpenResearch Auto Research session, send the prepared prompt, and
write a done marker.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..util.io import atomic_write_json


AGENT_PROMPT_FILE = "openresearch_agent_prompt.md"
REQUEST_FILE = "openresearch_gui_submit_request.json"
SUBMITTER_PROMPT_FILE = "openresearch_gui_submit_prompt.md"
DONE_FILE = "openresearch_gui_submit_done.json"
PROCESS_FILE = "openresearch_gui_submit_process.json"
STDOUT_FILE = "openresearch_gui_submit.stdout.log"
STDERR_FILE = "openresearch_gui_submit.stderr.log"


GuiSubmitterStatus = Literal["submitted", "not_configured", "disabled", "error"]


@dataclass
class OpenResearchGuiSubmitResult:
    status: GuiSubmitterStatus
    reason: str | None = None
    error: str | None = None
    project_url: str | None = None
    session_url: str | None = None
    observed_text: str | None = None
    screenshot_path: str | None = None
    returncode: int | None = None
    elapsed_s: float | None = None
    prompt_file: str | None = None
    request_file: str | None = None
    process_file: str | None = None
    stdout_file: str | None = None
    stderr_file: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "submitted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "error": self.error,
            "project_url": self.project_url,
            "session_url": self.session_url,
            "observed_text": self.observed_text,
            "screenshot_path": self.screenshot_path,
            "returncode": self.returncode,
            "elapsed_s": self.elapsed_s,
            "prompt_file": self.prompt_file,
            "request_file": self.request_file,
            "process_file": self.process_file,
            "stdout_file": self.stdout_file,
            "stderr_file": self.stderr_file,
            "details": self.details,
            "ok": self.ok,
        }


def submit_openresearch_gui(
    *,
    settings: Any,
    job_dir: Path,
    project_url: str,
    agent_prompt: str,
    project_id: str | None,
    org_id: str | None,
    paper_id: str | None,
    repo_full_name: str | None,
    source_run_id: str,
    artifact_id: str,
    paper_url: str | None = None,
) -> OpenResearchGuiSubmitResult:
    job_dir = job_dir.resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = job_dir / AGENT_PROMPT_FILE
    request_path = job_dir / REQUEST_FILE
    submitter_prompt_path = job_dir / SUBMITTER_PROMPT_FILE
    done_path = job_dir / DONE_FILE
    process_path = job_dir / PROCESS_FILE
    stdout_path = job_dir / STDOUT_FILE
    stderr_path = job_dir / STDERR_FILE

    prompt_path.write_text(agent_prompt, encoding="utf-8")
    request_payload = {
        "kind": "openresearch_gui_submit_request",
        "version": 1,
        "openresearch_url": project_url,
        "project_url": project_url if "/projects/" in project_url else None,
        "project_id": project_id,
        "org_id": org_id,
        "paper_id": paper_id,
        "paper_url": paper_url,
        "repo_full_name": repo_full_name,
        "source_run_id": source_run_id,
        "artifact_id": artifact_id,
        "agent_prompt_file": prompt_path.name,
        "done_file": done_path.name,
        "submitter_contract": {
            "open_openresearch_url": True,
            "create_project_if_needed": True,
            "create_or_use_auto_research_session": True,
            "send_agent_prompt_file_contents": prompt_path.name,
            "do_not_reproduce_locally": True,
            "write_done_json": done_path.name,
        },
    }
    atomic_write_json(request_path, request_payload)
    submitter_prompt = _build_submitter_prompt(request_payload)
    submitter_prompt_path.write_text(submitter_prompt, encoding="utf-8")

    base_kwargs = {
        "prompt_file": str(prompt_path),
        "request_file": str(request_path),
        "process_file": str(process_path),
        "stdout_file": str(stdout_path),
        "stderr_file": str(stderr_path),
    }
    mode = str(getattr(settings, "openresearch_submitter_mode", "off") or "off").strip().lower()
    command = str(getattr(settings, "openresearch_submitter_cmd", "") or "").strip()
    if mode == "off":
        return OpenResearchGuiSubmitResult(status="disabled", reason="submitter_disabled", **base_kwargs)
    if mode != "custom":
        return OpenResearchGuiSubmitResult(status="error", error=f"unsupported submitter mode: {mode}", **base_kwargs)
    if not command:
        return OpenResearchGuiSubmitResult(status="not_configured", reason="missing_submitter_cmd", **base_kwargs)

    timeout_s = max(1, int(getattr(settings, "openresearch_submitter_timeout_s", 300) or 300))
    started = time.monotonic()
    argv = shlex.split(command)
    env = os.environ.copy()
    env.update(
        {
            "AUTODESIGN_OPENRESEARCH_URL": project_url,
            "AUTODESIGN_OPENRESEARCH_PROJECT_URL": project_url,
            "AUTODESIGN_OPENRESEARCH_AGENT_PROMPT_FILE": str(prompt_path),
            "AUTODESIGN_OPENRESEARCH_SUBMIT_REQUEST": str(request_path),
            "AUTODESIGN_OPENRESEARCH_DONE_FILE": str(done_path),
            "DESIGN_ANYTHING_OPENRESEARCH_URL": project_url,
            "DESIGN_ANYTHING_OPENRESEARCH_PROJECT_URL": project_url,
            "DESIGN_ANYTHING_OPENRESEARCH_AGENT_PROMPT_FILE": str(prompt_path),
            "DESIGN_ANYTHING_OPENRESEARCH_SUBMIT_REQUEST": str(request_path),
            "DESIGN_ANYTHING_OPENRESEARCH_DONE_FILE": str(done_path),
        }
    )
    process_payload: dict[str, Any] = {
        "argv": argv,
        "cwd": str(job_dir),
        "timeout_s": timeout_s,
        "prompt_file": str(prompt_path),
        "request_file": str(request_path),
        "done_file": str(done_path),
        "started_at_monotonic": started,
    }
    try:
        proc = subprocess.run(
            argv,
            input=submitter_prompt,
            text=True,
            cwd=str(job_dir),
            env=env,
            timeout=timeout_s,
            capture_output=True,
            check=False,
        )
        elapsed_s = time.monotonic() - started
        stdout_path.write_text(proc.stdout or "", encoding="utf-8")
        stderr_path.write_text(proc.stderr or "", encoding="utf-8")
        process_payload.update(
            {
                "returncode": proc.returncode,
                "elapsed_s": elapsed_s,
                "stdout_file": str(stdout_path),
                "stderr_file": str(stderr_path),
            }
        )
        atomic_write_json(process_path, process_payload)
    except subprocess.TimeoutExpired as exc:
        elapsed_s = time.monotonic() - started
        stdout_path.write_text(_decode_text(exc.stdout), encoding="utf-8")
        stderr_path.write_text(_decode_text(exc.stderr), encoding="utf-8")
        process_payload.update(
            {
                "returncode": None,
                "elapsed_s": elapsed_s,
                "timed_out": True,
                "stdout_file": str(stdout_path),
                "stderr_file": str(stderr_path),
            }
        )
        atomic_write_json(process_path, process_payload)
        return OpenResearchGuiSubmitResult(
            status="error",
            error=f"submitter timed out after {timeout_s}s",
            elapsed_s=elapsed_s,
            **base_kwargs,
        )
    except OSError as exc:
        elapsed_s = time.monotonic() - started
        process_payload.update({"returncode": None, "elapsed_s": elapsed_s, "error": f"{type(exc).__name__}: {exc}"})
        atomic_write_json(process_path, process_payload)
        return OpenResearchGuiSubmitResult(
            status="error",
            error=f"failed to start submitter: {type(exc).__name__}: {exc}",
            elapsed_s=elapsed_s,
            **base_kwargs,
        )

    done_payload = _read_done(done_path)
    if done_payload:
        status = str(done_payload.get("status") or "submitted").strip().lower()
        if status == "submitted":
            return OpenResearchGuiSubmitResult(
                status="submitted",
                project_url=_string_or_none(done_payload.get("project_url")),
                session_url=_string_or_none(done_payload.get("session_url")) or project_url,
                observed_text=_string_or_none(done_payload.get("observed_text")),
                screenshot_path=_string_or_none(done_payload.get("screenshot_path")),
                returncode=process_payload.get("returncode"),
                elapsed_s=process_payload.get("elapsed_s"),
                details=done_payload,
                **base_kwargs,
            )
        return OpenResearchGuiSubmitResult(
            status="error",
            error=_string_or_none(done_payload.get("error")) or f"submitter done status was {status}",
            returncode=process_payload.get("returncode"),
            elapsed_s=process_payload.get("elapsed_s"),
            details=done_payload,
            **base_kwargs,
        )

    if process_payload.get("returncode") != 0:
        return OpenResearchGuiSubmitResult(
            status="error",
            error=f"submitter exited with code {process_payload.get('returncode')}",
            returncode=process_payload.get("returncode"),
            elapsed_s=process_payload.get("elapsed_s"),
            **base_kwargs,
        )
    return OpenResearchGuiSubmitResult(
        status="error",
        error=f"submitter finished without writing {DONE_FILE}",
        returncode=process_payload.get("returncode"),
        elapsed_s=process_payload.get("elapsed_s"),
        **base_kwargs,
    )


def _build_submitter_prompt(request_payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "You are a GUI submitter for OpenResearch Auto Research.",
            "Do not reproduce the paper yourself and do not modify local code.",
            "Use the user's logged-in Chrome/OpenResearch browser session when available.",
            "Do not call OpenResearch HTTP APIs, do not use the orx CLI, and do not run experiments locally.",
            "The user has authorized submitting this OpenResearch Auto Research task.",
            "Open the OpenResearch URL. If it is not already a project page, create a project for the supplied paper.",
            "Create or use an Auto Research session,",
            "paste the contents of openresearch_agent_prompt.md into the session message box, and send it.",
            "After the message is sent, write openresearch_gui_submit_done.json in the working directory.",
            "",
            "Done JSON schema:",
            '{"status":"submitted","project_url":"<project URL if visible>","session_url":"<current URL>","observed_text":"<short UI confirmation>","screenshot_path":null}',
            "",
            "Request:",
            json.dumps(request_payload, indent=2, ensure_ascii=False),
        ]
    )


def _read_done(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _decode_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
