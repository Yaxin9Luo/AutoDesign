"""Adapter that lets AutoDesign drive Pi as an external artifact author."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


_POLL_INTERVAL_S = 0.1


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        if not path.is_file():
            return None
        info = path.stat()
    except OSError:
        return None
    if info.st_size <= 0:
        return None
    return (info.st_size, info.st_mtime_ns)


def _changed_target_signatures(
    attempt_dir: Path,
    target_files: list[str],
    baseline: tuple[tuple[int, int] | None, ...],
) -> tuple[tuple[int, int], ...] | None:
    current: list[tuple[int, int]] = []
    for index, name in enumerate(target_files):
        signature = _file_signature(attempt_dir / name)
        if signature is None or signature == baseline[index]:
            return None
        current.append(signature)
    return tuple(current)


def _terminate_child(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _write_observed_done_marker(done_path: Path) -> None:
    done_path.write_text(
        json.dumps({"summary": "Pi adapter observed stable required author output."})
        + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Pi against an AutoDesign external-author attempt directory.",
    )
    parser.add_argument("--pi-bin", default="pi")
    parser.add_argument("--model", default="")
    parser.add_argument("--config-dir", default="")
    parser.add_argument("--prompt-file", default="designer_author_prompt.md")
    parser.add_argument("--target-file", action="append", default=[])
    parser.add_argument("--done-file", default="designer_author_done.json")
    parser.add_argument("--task", default="AutoDesign external authoring task")
    parser.add_argument("--approve", action="store_true", default=False)
    parser.add_argument("--output-stable-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)
    if args.output_stable_seconds < 0:
        parser.error("--output-stable-seconds must be non-negative")

    attempt_dir = Path.cwd()
    stdin_prompt = sys.stdin.read()
    prompt_path = attempt_dir / args.prompt_file
    if not prompt_path.exists() and stdin_prompt.strip():
        prompt_path.write_text(stdin_prompt, encoding="utf-8")

    target_files = list(args.target_file or []) or ["poster.html"]
    required_outputs = [*target_files, args.done_file]
    done_path = attempt_dir / args.done_file
    target_baseline = tuple(
        _file_signature(attempt_dir / output) for output in target_files
    )
    done_baseline = _file_signature(done_path)
    message = (
        f"Read {args.prompt_file} in the current directory and follow it exactly. "
        f"Task: {args.task}. Work only in this directory. Write "
        f"{', '.join(required_outputs)} when complete, then exit. "
        "Use only Pi's built-in read, bash, edit, and write tools. If the "
        "author prompt mentions AutoDesign-only tools such as ingest_document "
        "or propose_paper_poster_html, do not attempt them; directly author "
        "the required HTML files instead."
    )
    # Pi's @file syntax makes the author contract part of the initial model
    # context. Asking the model to discover and read the file was unreliable
    # for long headless authoring prompts.
    cmd = [
        args.pi_bin,
        "--print",
        "--no-session",
        f"@{args.prompt_file}",
    ]
    if args.approve:
        cmd.append("--approve")
    if args.model:
        cmd.extend(["--model", args.model])
    cmd.append(message)

    env = os.environ.copy()
    if args.config_dir:
        env["PI_CODING_AGENT_DIR"] = args.config_dir
    try:
        # Inherit the wrapper's process group so the outer AutoDesign timeout
        # can terminate Pi and any active tool subprocesses with the wrapper.
        proc = subprocess.Popen(cmd, cwd=attempt_dir, env=env)
    except OSError as exc:
        print(f"pi code-agent wrapper failed to start: {exc}", file=sys.stderr)
        return 127

    stable_signatures: tuple[tuple[int, int], ...] | None = None
    stable_since: float | None = None
    while True:
        returncode = proc.poll()
        target_signatures = _changed_target_signatures(
            attempt_dir,
            target_files,
            target_baseline,
        )
        done_signature = _file_signature(done_path)
        done_ready = done_signature is not None and done_signature != done_baseline

        if returncode is not None:
            if returncode == 0 and target_signatures is not None and not done_ready:
                _write_observed_done_marker(done_path)
            return int(returncode)

        if target_signatures is None:
            stable_signatures = None
            stable_since = None
        elif done_ready:
            _terminate_child(proc)
            return 0
        elif target_signatures != stable_signatures:
            stable_signatures = target_signatures
            stable_since = time.monotonic()
        elif (
            stable_since is not None
            and time.monotonic() - stable_since >= args.output_stable_seconds
        ):
            _write_observed_done_marker(done_path)
            _terminate_child(proc)
            return 0

        time.sleep(_POLL_INTERVAL_S)


if __name__ == "__main__":
    raise SystemExit(main())
