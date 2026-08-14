"""Adapter that lets AutoDesign drive ZCode CLI against staged files."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def _write_config(path: Path, payload: dict[str, object]) -> None:
    write_path = path.resolve(strict=True) if path.is_symlink() else path
    mode = stat.S_IMODE(write_path.stat().st_mode) if write_path.exists() else 0o600
    temp_path = write_path.with_name(
        f".{write_path.name}.autodesign-{os.getpid()}.tmp"
    )
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
                fd = -1
                temp_file.write(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
                )
        finally:
            if fd >= 0:
                os.close(fd)
        os.replace(temp_path, write_path)
    finally:
        temp_path.unlink(missing_ok=True)


def _recover_interrupted_model_selection(
    config_path: Path,
    recovery_path: Path,
) -> None:
    if not recovery_path.is_file():
        return
    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
    if not isinstance(recovery, dict) or recovery.get("version") != 1:
        raise ValueError(f"invalid ZCode model recovery journal: {recovery_path}")
    selected_model = recovery.get("selected_model")
    if not isinstance(selected_model, str) or not selected_model:
        raise ValueError(f"invalid ZCode model recovery journal: {recovery_path}")
    current = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(current, dict):
        raise ValueError(f"ZCode model config must contain a JSON object: {config_path}")
    if current.get("model") == selected_model:
        current["model"] = recovery.get("previous_model")
        _write_config(config_path, current)
    recovery_path.unlink(missing_ok=True)


def _raise_for_termination(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    group_signalled = False
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        group_signalled = True
    except (PermissionError, ProcessLookupError):
        if proc.poll() is None:
            proc.terminate()
    if not group_signalled:
        if proc.poll() is not None:
            return
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            if proc.poll() is None:
                proc.terminate()
            break
        if proc.poll() is None:
            try:
                proc.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(0.05)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            if proc.poll() is None:
                proc.kill()
    if proc.poll() is None:
        proc.wait()


@contextmanager
def _selected_model(config_path: Path, model: str) -> Iterator[None]:
    """Select one ZCode model for a subprocess and restore the prior default."""

    config_path = config_path.expanduser()
    lock_path = config_path.with_name(f".{config_path.name}.autodesign.lock")
    recovery_path = config_path.with_name(
        f".{config_path.name}.autodesign-recovery.json"
    )
    if not model and not recovery_path.is_file() and not config_path.is_file():
        yield
        return
    if not config_path.is_file():
        raise FileNotFoundError(f"ZCode model config does not exist: {config_path}")

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            _recover_interrupted_model_selection(config_path, recovery_path)
            if not model:
                yield
                return
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(
                    f"ZCode model config must contain a JSON object: {config_path}"
                )
            previous_model = payload.get("model")
            try:
                _write_config(
                    recovery_path,
                    {
                        "version": 1,
                        "previous_model": previous_model,
                        "selected_model": model,
                    },
                )
                payload["model"] = model
                _write_config(config_path, payload)
                yield
            finally:
                restore_complete = False
                try:
                    current = json.loads(config_path.read_text(encoding="utf-8"))
                    if isinstance(current, dict) and current.get("model") == model:
                        current["model"] = previous_model
                        _write_config(config_path, current)
                    restore_complete = True
                finally:
                    if restore_complete:
                        recovery_path.unlink(missing_ok=True)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run ZCode CLI against a coding-agent directory staged by AutoDesign.",
    )
    parser.add_argument("--zcode-bin", default="zcode")
    parser.add_argument("--model", default="")
    parser.add_argument("--config-path", default="~/.zcode/cli/config.json")
    parser.add_argument("--mode", default="yolo")
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--target-file", action="append", default=[])
    parser.add_argument("--done-file", default="")
    parser.add_argument("--task", default="AutoDesign coding-agent task")
    args = parser.parse_args(argv)

    attempt_dir = Path.cwd()
    prompt_path = attempt_dir / args.prompt_file
    if not prompt_path.exists():
        stdin_prompt = sys.stdin.read()
        if stdin_prompt.strip():
            prompt_path.write_text(stdin_prompt, encoding="utf-8")

    required_outputs = [name for name in args.target_file if name]
    if args.done_file:
        required_outputs.append(args.done_file)
    output_sentence = ""
    if required_outputs:
        output_sentence = " Required outputs: " + ", ".join(required_outputs) + "."
    model_sentence = (
        f" Requested model: {args.model}. The harness selected it before this task started."
        if args.model else ""
    )

    message = (
        "Headless execution contract: complete the requested file-editing task in "
        "the current directory. Do not ask for interactive input. "
        f"Read {args.prompt_file} in the current directory and follow it exactly. "
        f"Task: {args.task}. Work only in this directory.{output_sentence}"
        f"{model_sentence} You must directly create or update the required output files "
        "before exiting."
    )
    cmd = [
        args.zcode_bin,
        "--cwd",
        str(attempt_dir),
        "--mode",
        args.mode or "yolo",
        "--prompt",
        message,
    ]

    termination_signal: int | None = None
    spawn_in_progress = False
    termination_cleanup_in_progress = False

    def handle_termination(signum: int, _frame: object) -> None:
        nonlocal termination_signal
        if termination_signal is None:
            termination_signal = signum
        if spawn_in_progress or termination_cleanup_in_progress:
            return
        raise SystemExit(128 + termination_signal)

    previous_handlers = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        for signum in previous_handlers:
            signal.signal(signum, handle_termination)
        with _selected_model(Path(args.config_path), args.model):
            spawned_process: subprocess.Popen[bytes] | None = None
            try:
                spawn_in_progress = True
                spawned_process = subprocess.Popen(cmd, start_new_session=True)
                spawn_in_progress = False
                if termination_signal is not None:
                    raise SystemExit(128 + termination_signal)
                returncode = spawned_process.wait()
            except BaseException:
                if spawned_process is not None:
                    termination_cleanup_in_progress = True
                    try:
                        _terminate_process_group(spawned_process)
                    finally:
                        termination_cleanup_in_progress = False
                raise
            finally:
                spawn_in_progress = False
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"zcode code-agent wrapper failed to start: {exc}", file=sys.stderr)
        return 127
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)
    return int(returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
