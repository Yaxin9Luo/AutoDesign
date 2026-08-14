"""Adapter that lets AutoDesign drive OpenCode as an external poster author.

ExternalDesignerAuthor writes the full contract to designer_author_prompt.md and
sends the same text on stdin. OpenCode's non-interactive CLI expects a message
argument instead, so this small wrapper bridges the two conventions.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run OpenCode against a designer-author attempt directory staged by AutoDesign.",
    )
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--model", default="")
    parser.add_argument("--prompt-file", default="designer_author_prompt.md")
    parser.add_argument("--target-file", action="append", default=[])
    parser.add_argument("--done-file", default="designer_author_done.json")
    parser.add_argument("--task", default="AutoDesign external designer-author task")
    parser.add_argument(
        "--dangerously-skip-permissions",
        action="store_true",
        default=False,
        help="Pass OpenCode's non-interactive auto-approval flag.",
    )
    args = parser.parse_args(argv)

    attempt_dir = Path.cwd()
    stdin_prompt = sys.stdin.read()
    prompt_path = attempt_dir / args.prompt_file
    if not prompt_path.exists() and stdin_prompt.strip():
        prompt_path.write_text(stdin_prompt, encoding="utf-8")

    target_files = list(args.target_file or []) or ["poster.html"]
    required_outputs = [*target_files, args.done_file]
    message = (
        f"Read {args.prompt_file} in the current directory and follow it exactly. "
        f"Task: {args.task}. Work only in this directory. Write "
        f"{', '.join(required_outputs)} when complete, then exit."
    )
    cmd = [
        args.opencode_bin,
        "run",
        "--dir",
        str(attempt_dir),
    ]
    if args.model:
        cmd.extend(["--model", args.model])
    if args.dangerously_skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    cmd.append(message)

    try:
        proc = subprocess.run(cmd)
    except OSError as exc:
        print(f"opencode designer-author wrapper failed to start: {exc}", file=sys.stderr)
        return 127
    return int(proc.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
