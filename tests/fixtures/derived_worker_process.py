from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _ignore_term() -> None:
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)


def _writer(path: Path) -> int:
    _ignore_term()
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("tick\n")
            handle.flush()
            os.fsync(handle.fileno())
        time.sleep(0.02)


def _idle() -> int:
    _ignore_term()
    while True:
        time.sleep(0.02)


def _pptx_child(pid_path: Path) -> int:
    _ignore_term()
    grandchild = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--idle"],
        start_new_session=(os.name == "posix"),
    )
    _atomic_json(pid_path, {"pids": [os.getpid(), grandchild.pid]})
    while True:
        time.sleep(0.02)


def _worker(run_id: str) -> int:
    from autodesign.process_supervision import ProcessLedger, spawn_registered_process
    from autodesign.run_worker_protocol import decode_request

    request = decode_request(sys.stdin.buffer)
    if request.run_id != run_id:
        raise RuntimeError("run ID mismatch")
    settings = getattr(request, "settings", None)
    runs_dir = Path(settings.out_dir) / "runs" if settings is not None else Path(request.runs_dir)
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    if request.job_kind == "editable_video_render":
        sentinel = run_dir / "editable-video" / "render-heartbeat.txt"
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--writer",
                str(sentinel),
            ]
        )
        _atomic_json(run_dir / "recorded_pids.json", {"pids": [child.pid]})
        return _idle()

    if request.job_kind == "poster_code_edit":
        sentinel = run_dir / "poster-edit" / "author-heartbeat.txt"
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--writer",
                str(sentinel),
            ]
        )
        _atomic_json(run_dir / "recorded_pids.json", {"pids": [child.pid]})
        return _idle()

    if request.job_kind == "attempt_fork":
        sentinel = run_dir / "attempt-fork" / "materialize-heartbeat.txt"
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--writer",
                str(sentinel),
            ]
        )
        _atomic_json(run_dir / "recorded_pids.json", {"pids": [child.pid]})
        return _idle()

    if request.job_kind == "candidate_publish":
        sentinel = run_dir / "candidate-publish" / "validation-heartbeat.txt"
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--writer",
                str(sentinel),
            ]
        )
        _atomic_json(run_dir / "recorded_pids.json", {"pids": [child.pid]})
        return _idle()

    if request.job_kind == "pptx_export":
        source = Path(request.source_html)
        staged = run_dir / "pptx-export" / "current.html"
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, staged)
        spawn_registered_process(
            ProcessLedger(run_dir),
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--pptx-child",
                str(run_dir / "recorded_pids.json"),
            ],
            role="fixture-pptx-agent",
            start_new_session=(os.name == "posix"),
        )
        return _idle()

    if request.job_kind == "video_export_retry":
        stage_one = run_dir / "video-retry" / "stage-one-heartbeat.txt"
        stage_one.parent.mkdir(parents=True, exist_ok=True)
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--writer",
                str(stage_one),
            ]
        )
        _atomic_json(run_dir / "recorded_pids.json", {"pids": [child.pid]})
        (run_dir / "video-retry" / "stage-one-started").write_text(
            "started", encoding="utf-8"
        )
        child.wait()
        (run_dir / "video-retry" / "stage-two-started").write_text(
            "started", encoding="utf-8"
        )
        final_dir = run_dir / "final"
        final_dir.mkdir(parents=True, exist_ok=True)
        (final_dir / "video_delivery.json").write_text(
            '{"status":"published"}\n', encoding="utf-8"
        )
        return 0

    raise RuntimeError(f"unsupported fixture job kind: {request.job_kind}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id")
    parser.add_argument("--spawn-nonce")
    parser.add_argument("--writer")
    parser.add_argument("--pptx-child")
    parser.add_argument("--idle", action="store_true")
    args = parser.parse_args()
    if args.writer:
        return _writer(Path(args.writer))
    if args.pptx_child:
        return _pptx_child(Path(args.pptx_child))
    if args.idle:
        return _idle()
    if not args.run_id:
        parser.error("--run-id is required")
    return _worker(args.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
