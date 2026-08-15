"""Adapter that drives the released DeepSeek Harness against staged files."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


_MODEL_PATCH_FILE = ".autodesign-dsh-model.patch.yml"
_UPGRADE_COMMAND = "npm install -g @deepseek-ai/dsh@latest"


def _probe_released_headless(dsh_bin: str) -> tuple[bool, str, str]:
    try:
        version_proc = subprocess.run(
            [dsh_bin, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        help_proc = subprocess.run(
            [dsh_bin, "--profile", "headless", "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError as exc:
        return False, "", f"failed to start: {exc}"
    except subprocess.TimeoutExpired:
        return False, "", "capability probe timed out"

    version = ((version_proc.stdout or "") + (version_proc.stderr or "")).strip()
    help_text = ((help_proc.stdout or "") + (help_proc.stderr or "")).strip()
    compatible = (
        version_proc.returncode == 0
        and help_proc.returncode == 0
        and "usage: dsh --profile headless" in help_text.lower()
        and "task" in help_text.lower()
    )
    return compatible, version.splitlines()[0] if version else "", help_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run DeepSeek Harness against a coding-agent directory staged by AutoDesign.",
    )
    parser.add_argument("--dsh-bin", default="dsh")
    parser.add_argument("--model", default="")
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--target-file", action="append", default=[])
    parser.add_argument("--done-file", default="")
    parser.add_argument("--task", default="AutoDesign coding-agent task")
    args = parser.parse_args(argv)

    compatible, version, probe_detail = _probe_released_headless(args.dsh_bin)
    if not compatible:
        detected = f" (detected {version})" if version else ""
        detail = probe_detail.splitlines()[0] if probe_detail else "headless profile unavailable"
        print(
            "DeepSeek Harness CLI is missing or incompatible with the released "
            f"headless profile{detected}: {detail}. Upgrade with: {_UPGRADE_COMMAND}",
            file=sys.stderr,
        )
        return 2

    attempt_dir = Path.cwd()
    prompt_path = attempt_dir / args.prompt_file
    if not prompt_path.exists():
        stdin_prompt = sys.stdin.read()
        if stdin_prompt.strip():
            prompt_path.write_text(stdin_prompt, encoding="utf-8")
    if not prompt_path.is_file():
        print(f"DeepSeek Harness prompt file is missing: {args.prompt_file}", file=sys.stderr)
        return 2

    required_outputs = [name for name in args.target_file if name]
    if args.done_file:
        required_outputs.append(args.done_file)
    output_sentence = ""
    if required_outputs:
        output_sentence = " Required outputs: " + ", ".join(required_outputs) + "."

    message = (
        f"Read {args.prompt_file} in the current directory and follow it exactly. "
        f"Task: {args.task}. Work only in this directory.{output_sentence} "
        "Exit when complete."
    )
    cmd = [args.dsh_bin, "--profile", "headless"]
    model = args.model.strip()
    if model:
        patch_path = attempt_dir / _MODEL_PATCH_FILE
        patch_path.write_text(
            json.dumps(
                [{
                    "id": "agent-default-model",
                    "config": {
                        "provider": "deepseek-official",
                        "model": model,
                    },
                }],
                ensure_ascii=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        cmd.extend(["--patch", str(patch_path)])
    cmd.append(message)

    try:
        proc = subprocess.run(cmd)
    except OSError as exc:
        print(f"DeepSeek Harness adapter failed to start: {exc}", file=sys.stderr)
        return 127
    return int(proc.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
