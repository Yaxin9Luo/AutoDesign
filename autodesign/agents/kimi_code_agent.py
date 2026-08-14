"""Adapter that lets AutoDesign drive Kimi Code CLI against staged files."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Kimi Code CLI against a coding-agent directory staged by AutoDesign.",
    )
    parser.add_argument("--kimi-bin", default="kimi")
    parser.add_argument("--model", default="")
    parser.add_argument("--yolo", action="store_true", default=False)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--target-file", action="append", default=[])
    parser.add_argument("--done-file", default="")
    parser.add_argument("--task", default="AutoDesign coding-agent task")
    args = parser.parse_args(argv)

    attempt_dir = Path.cwd()
    stdin_prompt = sys.stdin.read()
    prompt_path = attempt_dir / args.prompt_file
    if not prompt_path.exists() and stdin_prompt.strip():
        prompt_path.write_text(stdin_prompt, encoding="utf-8")

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
    cmd = [args.kimi_bin]
    if args.yolo:
        print("kimi code-agent wrapper: ignoring --yolo because Kimi CLI cannot combine -y with -p/--prompt.", file=sys.stderr)
    if args.model:
        cmd.extend(["-m", args.model])
    cmd.extend(["-p", message])

    try:
        proc = subprocess.run(cmd)
    except OSError as exc:
        print(f"kimi code-agent wrapper failed to start: {exc}", file=sys.stderr)
        return 127
    return int(proc.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
