#!/usr/bin/env python3
"""Run an AutoDesign coding-harness matrix from the command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from autodesign.harness_matrix import (
    CODING_HARNESSES,
    HarnessMatrixCellSpec,
    run_harness_matrix,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run paper-poster generation across coding harnesses.")
    parser.add_argument("--paper", "--from-file", dest="paper", required=True, help="Paper PDF path.")
    parser.add_argument("--prompt", default="", help="Inline poster prompt.")
    parser.add_argument("--prompt-file", default="", help="Read the poster prompt from a text file.")
    parser.add_argument("--template", default="cvpr-landscape", help="AutoDesign template name.")
    parser.add_argument(
        "--harness",
        action="append",
        choices=CODING_HARNESSES,
        default=[],
        help="Harness to run. Repeatable. Defaults to all named harnesses.",
    )
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="HARNESS=MODEL",
        help="Optional requested model for one harness.",
    )
    parser.add_argument("--attempts", type=int, default=12, help="Designer-author max attempts per harness.")
    parser.add_argument("--timeout", type=int, default=3600, help="Designer-author timeout seconds per attempt.")
    parser.add_argument("--reuse-ingest-run", default="", help="Optional existing run id or run dir to reuse ingest.")
    parser.add_argument("--out-dir", default="", help="AutoDesign out dir; defaults to ./out.")
    args = parser.parse_args(argv)

    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
    if not prompt.strip():
        print("error: provide --prompt or --prompt-file", file=sys.stderr)
        return 2

    models: dict[str, str] = {}
    for item in args.model:
        if "=" not in item:
            print(f"error: --model must be HARNESS=MODEL, got {item!r}", file=sys.stderr)
            return 2
        harness, model = item.split("=", 1)
        harness = harness.strip()
        if harness not in CODING_HARNESSES:
            print(f"error: unknown harness in --model: {harness}", file=sys.stderr)
            return 2
        models[harness] = model.strip()

    harnesses = args.harness or list(CODING_HARNESSES)
    matrix = run_harness_matrix(
        paper_path=args.paper,
        prompt=prompt,
        template=args.template,
        harnesses=[HarnessMatrixCellSpec(harness=h, model=models.get(h)) for h in harnesses],
        attempts=args.attempts,
        timeout_s=args.timeout,
        concurrency="by_harness",
        reuse_ingest_run=args.reuse_ingest_run or None,
        out_dir=args.out_dir or None,
    )
    print(json.dumps({
        "matrix_id": matrix["matrix_id"],
        "status": matrix["status"],
        "matrix_dir": matrix["matrix_dir"],
        "report_path": matrix["report_path"],
        "strict_success": matrix.get("strict_success", False),
        "hard_failure_count": matrix.get("hard_failure_count", 0),
    }, ensure_ascii=False, indent=2))
    return 0 if matrix.get("status") == "completed" and not matrix.get("hard_failure_count") else 1


if __name__ == "__main__":
    raise SystemExit(main())
