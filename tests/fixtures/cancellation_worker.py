from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _mode_payload(brief: str) -> dict[str, object]:
    try:
        payload = json.loads(brief)
    except json.JSONDecodeError:
        return {"mode": brief}
    return payload if isinstance(payload, dict) else {"mode": brief}


def _pipeline_result(run_id: str, run_dir: Path, mode: str) -> dict[str, object]:
    from autodesign.schema import RunResult

    return RunResult(
        run_id=run_id,
        run_dir=str(run_dir),
        artifact_type="poster",
        terminal_status="pass",
        finalize_notes=mode,
    ).model_dump(mode="json")


def _child_loop() -> int:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.02)


def _write_marker(path: Path) -> int:
    path.write_text("launched", encoding="utf-8")
    return 0


def _stdin_marker(path: Path) -> int:
    path.write_bytes(sys.stdin.buffer.read())
    return 0


def _hard_crash_spawn(run_dir: Path, marker: Path) -> int:
    from autodesign.process_supervision import ProcessLedger, spawn_registered_process

    def crash_after_identity(_identity: object) -> None:
        os._exit(86)

    spawn_registered_process(
        ProcessLedger(run_dir),
        [sys.executable, str(Path(__file__).resolve()), "--write-marker", str(marker)],
        role="hard-crash-child",
        registration_hook=crash_after_identity,
        handshake_timeout_s=0.5,
    )
    return 87


def _post_popen_crash_spawn(run_dir: Path, pid_path: Path, marker: Path) -> int:
    import autodesign.process_supervision as supervision

    def crash_before_identity(pid: int) -> None:
        pid_path.write_text(str(pid), encoding="utf-8")
        os._exit(88)

    supervision.process_identity = crash_before_identity
    supervision.spawn_registered_process(
        supervision.ProcessLedger(run_dir),
        [sys.executable, str(Path(__file__).resolve()), "--write-marker", str(marker)],
        role="post-popen-crash-child",
        handshake_timeout_s=0.5,
    )
    return 87


def _root_post_popen_crash(runs_dir: Path, run_id: str, pid_path: Path) -> int:
    import asyncio
    from autodesign.config import Settings
    from autodesign.run_control import RunControlStore
    import autodesign.run_supervisor as supervisor_module
    from autodesign.run_worker_protocol import PipelineWorkerRequest

    store = RunControlStore(runs_dir)
    reserved = store.reserve(run_id, "poster")
    store.transition(run_id, reserved, "queued")

    def crash_before_identity(pid: int) -> None:
        pid_path.write_text(str(pid), encoding="utf-8")
        os._exit(89)

    supervisor_module.process_identity = crash_before_identity

    request = PipelineWorkerRequest(
        job_kind="pipeline", run_id=run_id, brief="success", attachments=(),
        template=None, palette_id=None, resume_run=None, reference_poster=None,
        settings=Settings(
            anthropic_api_key="", anthropic_base_url=None, gemini_api_key="",
            designer_model="designer", critic_model="critic",
            repo_root=Path(__file__).resolve().parents[2],
            out_dir=runs_dir.parent,
        ),
    )
    supervisor = supervisor_module.RunSupervisor(
        runs_dir, control_store=store,
        worker_command=(sys.executable, str(Path(__file__).resolve())),
    )
    asyncio.run(supervisor.start(request))
    return 90


def _hold_ledger_lock(run_dir: Path, marker: Path, duration_s: float) -> int:
    from autodesign.process_supervision import ProcessLedger

    with ProcessLedger(run_dir).exclusive():
        marker.write_text("locked", encoding="utf-8")
        time.sleep(duration_s)
    return 0


def _spawn_descendant(run_dir: Path) -> int:
    from autodesign.process_supervision import ProcessLedger, spawn_registered_process

    process = spawn_registered_process(
        ProcessLedger(run_dir),
        [sys.executable, str(Path(__file__).resolve()), "--child-loop"],
        role="fixture-detached-grandchild",
        start_new_session=True,
    )
    _atomic_json(run_dir / "fixture_descendant.json", {"pids": [os.getpid(), process.pid]})
    return _child_loop()


def _unregistered_detached_loop(run_dir: Path) -> int:
    pid_path = run_dir / "fixture_unregistered_detached_pid.txt"
    heartbeat = run_dir / "generated_media" / "unregistered_heartbeat.txt"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        with heartbeat.open("a", encoding="utf-8") as handle:
            handle.write("tick\n")
            handle.flush()
            os.fsync(handle.fileno())
        time.sleep(0.015)


def _spawn_unregistered_detached(run_dir: Path) -> int:
    subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--unregistered-detached-loop",
            str(run_dir),
        ],
        start_new_session=True,
        close_fds=True,
    )
    return 0


def _worker(run_id: str) -> int:
    from autodesign.process_supervision import ProcessLedger, spawn_registered_process
    from autodesign.run_worker import _scrub_secret_environment
    from autodesign.run_worker_protocol import decode_request
    from autodesign.util.logging import append_jsonl_event

    _scrub_secret_environment()
    request = decode_request(sys.stdin.buffer)
    if request.run_id != run_id:
        raise RuntimeError("run ID mismatch")
    run_dir = Path(request.settings.out_dir) / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    mode_payload = _mode_payload(getattr(request, "brief", "success"))
    mode = str(mode_payload.get("mode") or "success")
    secret = request.settings.anthropic_api_key
    sensitive_env = {
        key: value
        for key, value in os.environ.items()
        if secret and secret in value
    }
    credential_env_keys = sorted(
        key for key in os.environ
        if key.upper().endswith(("_API_KEY", "_TOKEN", "_PASSWORD", "_SECRET"))
        or key == "ANTHROPIC_CUSTOM_HEADERS"
    )
    _atomic_json(
        run_dir / "fixture_observation.json",
        {
            "argv_contains_secret": any(secret in value for value in sys.argv) if secret else False,
            "env_keys_containing_secret": sorted(sensitive_env),
            "credential_env_keys": credential_env_keys,
            "proxy_env": {
                key: value for key, value in os.environ.items()
                if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}
            },
            "pid": os.getpid(),
            "mode": mode,
        },
    )
    append_jsonl_event(
        run_dir / "worker_events.jsonl",
        {"run_id": run_id, "event": "fixture.started", "mode": mode},
    )

    def write_result(result: dict[str, object]) -> None:
        _atomic_json(
            run_dir / "worker_result.json",
            {"job_kind": request.job_kind, "run_id": run_id, "ok": True, "result": result},
        )

    if mode == "success":
        write_result(_pipeline_result(run_id, run_dir, mode))
        return 0
    if mode == "delayed_success":
        deadline = time.monotonic() + float(mode_payload.get("delay_s") or 0.4)
        while time.monotonic() < deadline:
            time.sleep(0.01)
        write_result(_pipeline_result(run_id, run_dir, mode))
        return 0
    if mode == "secret_stderr":
        secrets = (
            request.settings.anthropic_api_key,
            request.settings.anthropic_auth_token or "",
            request.settings.gemini_api_key,
            request.settings.openai_compat_api_key or "",
            request.settings.openrouter_api_key or "",
            request.settings.harness_api_key or "",
            request.settings.openresearch_token,
            *request.settings.anthropic_custom_headers.values(),
        )
        for index, value in enumerate(value for value in secrets if value):
            stream = sys.stderr if index % 2 else sys.stdout
            midpoint = max(1, len(value) // 2)
            stream.write(value[:midpoint])
            stream.flush()
            time.sleep(0.002)
            stream.write(value[midpoint:] + "\n")
            stream.flush()
        volume = int(mode_payload.get("stderr_bytes") or 2_000_000)
        sys.stderr.write("X" * volume)
        sys.stdout.write("Y" * volume)
        sys.stderr.flush()
        sys.stdout.flush()
        write_result(_pipeline_result(run_id, run_dir, mode))
        return 0
    if mode == "abrupt_exit":
        events_path = run_dir / "worker_events.jsonl"
        append_jsonl_event(
            events_path,
            {
                "run_id": run_id,
                "event": "fixture.before_exit",
                "phase": "authoring",
                "reason": "fixture_crash",
                "unsafe_payload": {
                    "secret": secret,
                    "path": str(run_dir),
                },
            },
        )
        volume = int(mode_payload.get("output_bytes") or 64_000)
        sys.stdout.write("S" * volume)
        sys.stderr.write("E" * volume)
        sys.stdout.flush()
        sys.stderr.flush()
        midpoint = max(1, len(secret) // 2)
        sys.stderr.write(secret[:midpoint])
        sys.stderr.flush()
        time.sleep(0.002)
        sys.stderr.write(secret[midpoint:] + "\n")
        sys.stderr.write(f"internal-path={run_dir}\n")
        sys.stderr.write("stderr-final-root-cause\n")
        sys.stdout.write("stdout-final-marker\n")
        sys.stderr.flush()
        sys.stdout.flush()
        exit_code = mode_payload.get("exit_code")
        return int(exit_code if exit_code is not None else 17)
    if mode == "malformed_result":
        (run_dir / "worker_result.json").write_text("{", encoding="utf-8")
        return 0
    if mode == "structured_failure":
        _atomic_json(
            run_dir / "worker_result.json",
            {
                "job_kind": request.job_kind,
                "run_id": run_id,
                "ok": False,
                "error": {
                    "type": "fixture_failure",
                    "message": "specific structured worker failure",
                },
            },
        )
        return 19
    if mode == "success_nonzero":
        write_result(_pipeline_result(run_id, run_dir, mode))
        return 23
    if mode == "mismatched_result":
        _atomic_json(
            run_dir / "worker_result.json",
            {"job_kind": "pptx_export", "run_id": run_id, "ok": True, "result": {"mode": mode}},
        )
        return 0
    if mode == "event_stream":
        events_path = run_dir / "worker_events.jsonl"
        events_path.write_text(
            "".join((
                json.dumps({"run_id": run_id, "event": "fixture.one", "event_id": "event-1", "seq": 2}) + "\n",
                json.dumps({"run_id": run_id, "event": "fixture.duplicate", "event_id": "event-1", "seq": 3}) + "\n",
                json.dumps({"run_id": run_id, "event": "fixture.old", "event_id": "event-old", "seq": 1}) + "\n",
                '{"run_id":',
            )),
            encoding="utf-8",
        )
        while True:
            time.sleep(0.02)
    if mode == "terminal_event":
        append_jsonl_event(
            run_dir / "worker_events.jsonl",
            {"run_id": run_id, "event": "run.done"},
            event_id="forbidden-worker-terminal",
        )
        while True:
            time.sleep(0.02)
    if mode == "write_loop":
        target = run_dir / "generated_media" / "heartbeat.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        while True:
            with target.open("a", encoding="utf-8") as handle:
                handle.write("tick\n")
                handle.flush()
                os.fsync(handle.fileno())
            time.sleep(0.015)
    if mode in {"spawn_child", "spawn_detached_child"}:
        ledger = ProcessLedger(run_dir)
        if mode == "spawn_child":
            command = [sys.executable, str(Path(__file__).resolve()), "--child-loop"]
            role = "fixture-child"
            detached = False
        else:
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--spawn-descendant",
                str(run_dir),
            ]
            role = "fixture-detached-child"
            detached = True
        child = spawn_registered_process(
            ledger,
            command,
            role=role,
            start_new_session=detached,
        )
        _atomic_json(run_dir / "fixture_child.json", {"pids": [child.pid]})
        while True:
            time.sleep(0.02)
    if mode == "spawn_unregistered_detached_child":
        launcher = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--spawn-unregistered-detached",
                str(run_dir),
            ],
            start_new_session=True,
            close_fds=True,
        )
        if launcher.wait(timeout=2.0) != 0:
            raise RuntimeError("unregistered descendant launcher failed")
        while True:
            time.sleep(0.02)
    if mode == "spawn_registration_barrier":
        ledger = ProcessLedger(run_dir)
        entered = run_dir / "registration-entered"
        release = run_dir / "registration-release"

        def barrier(_identity: object) -> None:
            entered.write_text("entered", encoding="utf-8")
            deadline = time.monotonic() + 5
            while not release.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not release.exists():
                raise RuntimeError("fixture registration barrier timed out")

        child = spawn_registered_process(
            ledger,
            [sys.executable, str(Path(__file__).resolve()), "--child-loop"],
            role="cross-process-race-child",
            start_new_session=True,
            registration_hook=barrier,
            handshake_timeout_s=1.0,
        )
        _atomic_json(run_dir / "fixture_child.json", {"pids": [child.pid]})
        while True:
            time.sleep(0.02)
    if mode == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(0.02)
    while True:
        time.sleep(0.02)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id")
    parser.add_argument("--spawn-nonce")
    parser.add_argument("--child-loop", action="store_true")
    parser.add_argument("--spawn-descendant")
    parser.add_argument("--unregistered-detached-loop")
    parser.add_argument("--spawn-unregistered-detached")
    parser.add_argument("--write-marker")
    parser.add_argument("--stdin-marker")
    parser.add_argument("--hard-crash-spawn", nargs=2)
    parser.add_argument("--post-popen-crash-spawn", nargs=3)
    parser.add_argument("--root-post-popen-crash", nargs=3)
    parser.add_argument("--hold-ledger-lock", nargs=3)
    args = parser.parse_args()
    if args.child_loop:
        return _child_loop()
    if args.spawn_descendant:
        return _spawn_descendant(Path(args.spawn_descendant))
    if args.unregistered_detached_loop:
        return _unregistered_detached_loop(Path(args.unregistered_detached_loop))
    if args.spawn_unregistered_detached:
        return _spawn_unregistered_detached(Path(args.spawn_unregistered_detached))
    if args.write_marker:
        return _write_marker(Path(args.write_marker))
    if args.stdin_marker:
        return _stdin_marker(Path(args.stdin_marker))
    if args.hard_crash_spawn:
        return _hard_crash_spawn(Path(args.hard_crash_spawn[0]), Path(args.hard_crash_spawn[1]))
    if args.post_popen_crash_spawn:
        return _post_popen_crash_spawn(
            Path(args.post_popen_crash_spawn[0]),
            Path(args.post_popen_crash_spawn[1]),
            Path(args.post_popen_crash_spawn[2]),
        )
    if args.root_post_popen_crash:
        return _root_post_popen_crash(
            Path(args.root_post_popen_crash[0]),
            args.root_post_popen_crash[1],
            Path(args.root_post_popen_crash[2]),
        )
    if args.hold_ledger_lock:
        return _hold_ledger_lock(
            Path(args.hold_ledger_lock[0]),
            Path(args.hold_ledger_lock[1]),
            float(args.hold_ledger_lock[2]),
        )
    if not args.run_id:
        parser.error("--run-id is required")
    return _worker(args.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
