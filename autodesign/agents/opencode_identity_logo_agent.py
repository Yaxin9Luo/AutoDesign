"""Adapter that lets AutoDesign drive OpenCode for identity-logo discovery."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run OpenCode against an identity-logo agent directory staged by AutoDesign.",
    )
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--model", default="")
    parser.add_argument(
        "--dangerously-skip-permissions",
        action="store_true",
        default=False,
        help="Pass OpenCode's non-interactive auto-approval flag.",
    )
    args = parser.parse_args(argv)

    attempt_dir = Path.cwd()
    stdin_prompt = sys.stdin.read()
    prompt_path = attempt_dir / "identity_logo_prompt.md"
    if not prompt_path.exists() and stdin_prompt.strip():
        prompt_path.write_text(stdin_prompt, encoding="utf-8")

    message = (
        "Read identity_logo_prompt.md in the current directory and follow it "
        "exactly. Work only in this directory. Write identity_logo_candidates.json "
        "when complete, then exit."
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
        print(f"opencode identity-logo wrapper failed to start: {exc}", file=sys.stderr)
        return 127
    return int(proc.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
