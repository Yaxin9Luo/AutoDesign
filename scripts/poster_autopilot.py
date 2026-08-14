"""One-step autopilot wrapper for the paper-poster dogfood loop.

This script runs the fixed three-case harness, then calls
poster_autopilot_decision.py to decide what the next engineering patch should
target. It keeps code changes out of the script; the outer Codex agent applies
the patch after reading the decision.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from autodesign.util.io import atomic_write_json, ensure_dirs
from scripts.poster_autopilot_decision import (
    DEFAULT_CASES,
    build_autopilot_decision,
    load_gold_reference_spec,
    render_autopilot_decision,
)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_dir = Path(args.out_dir).expanduser().resolve()
    ensure_dirs(out_dir)

    if args.from_iteration_dir:
        iteration_dir = Path(args.from_iteration_dir).expanduser().resolve()
        eval_returncode = None
    else:
        iteration_dir, eval_returncode = _run_quality_loop(args, out_dir=out_dir)

    metrics_path = iteration_dir / "metrics.json"
    metrics = _read_json(metrics_path)
    if not isinstance(metrics, dict):
        raise SystemExit(f"metrics file missing after loop: {metrics_path}")

    gold_spec = load_gold_reference_spec(Path(args.gold_spec).expanduser())
    decision = build_autopilot_decision(
        metrics,
        gold_spec,
        iteration_dir=iteration_dir,
        selected_cases=tuple(args.case or DEFAULT_CASES),
    )
    atomic_write_json(iteration_dir / "autopilot_decision.json", decision)
    (iteration_dir / "autopilot_decision.md").write_text(
        render_autopilot_decision(decision),
        encoding="utf-8",
    )
    summary = {
        "out_dir": str(out_dir),
        "iteration_dir": str(iteration_dir),
        "eval_returncode": eval_returncode,
        "generate": bool(args.generate),
        "cases": args.case or list(DEFAULT_CASES),
        "decision_path": str(iteration_dir / "autopilot_decision.json"),
        "primary_target": decision.get("primary_target"),
        "acceptance": decision.get("acceptance"),
    }
    atomic_write_json(iteration_dir / "autopilot_summary.json", summary)
    print(f"autopilot iteration: {iteration_dir}")
    print(f"decision: {iteration_dir / 'autopilot_decision.md'}")
    print(f"primary target: {decision.get('primary_target')}")
    return 0 if eval_returncode in (None, 0) else int(eval_returncode)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--out-dir", default="out/poster_autopilot/human_effort_three_case")
    parser.add_argument("--set", dest="eval_set", default="human-effort-six-pack-v1")
    parser.add_argument("--label-set", default="paper-poster-evaluator-calibration-v2")
    parser.add_argument("--identity-set", default="paper-poster-identity-calibration-v1")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--harness-mode", default="dogfood", choices=["cheap", "standard", "quality", "dogfood"])
    parser.add_argument("--generate", action="store_true", help="Run API generation before evaluation.")
    parser.add_argument("--allow-large-generate", action="store_true")
    parser.add_argument("--generate-workers", type=int, default=3)
    parser.add_argument("--skip-enhancer", action="store_true")
    parser.add_argument("--no-claim-graph", action="store_true")
    parser.add_argument("--max-system-iterations", type=int, default=1)
    parser.add_argument("--attempted-owner", default="layout_storyboard")
    parser.add_argument("--patch-summary", default="Poster autopilot dense-reference contract iteration")
    parser.add_argument(
        "--gold-spec",
        default=str(
            _REPO_ROOT
            / "autodesign"
            / "evaluator"
            / "assets"
            / "poster_gold_reference_specs.json"
        ),
    )
    parser.add_argument(
        "--from-iteration-dir",
        default=None,
        help="Skip generation/eval and only produce the autopilot decision for an existing iteration.",
    )
    return parser.parse_args(argv)


def _run_quality_loop(args: argparse.Namespace, *, out_dir: Path) -> tuple[Path, int]:
    before = _iteration_dirs(out_dir)
    cmd = [
        sys.executable,
        "scripts/poster_quality_loop.py",
        "--data-dir",
        args.data_dir,
        "--set",
        args.eval_set,
        "--label-set",
        args.label_set,
        "--identity-set",
        args.identity_set,
        "--out-dir",
        str(out_dir),
        "--harness-mode",
        args.harness_mode,
        "--generate-workers",
        str(args.generate_workers),
        "--max-system-iterations",
        str(args.max_system_iterations),
        "--attempted-owner",
        args.attempted_owner,
        "--patch-summary",
        args.patch_summary,
        "--raphael-loop",
    ]
    for case in args.case or DEFAULT_CASES:
        cmd.extend(["--case", case])
    if args.generate:
        cmd.append("--generate")
    else:
        cmd.append("--no-generate")
    if args.allow_large_generate:
        cmd.append("--allow-large-generate")
    if args.skip_enhancer:
        cmd.append("--skip-enhancer")
    if args.no_claim_graph:
        cmd.append("--no-claim-graph")

    env = os.environ.copy()
    env["POSTER_HARNESS_MODE"] = args.harness_mode
    ensure_dirs(out_dir)
    log_path = out_dir / f"autopilot_quality_loop_{time.strftime('%Y%m%d-%H%M%S')}.log"
    with log_path.open("w", encoding="utf-8") as log_file:
        log_file.write("$ " + " ".join(cmd) + "\n\n")
        proc = subprocess.run(
            cmd,
            cwd=str(_REPO_ROOT),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    after = _iteration_dirs(out_dir)
    new_dirs = [path for path in after if path not in before]
    iteration_dir = new_dirs[-1] if new_dirs else (after[-1] if after else out_dir)
    return iteration_dir, int(proc.returncode)


def _iteration_dirs(out_dir: Path) -> list[Path]:
    if not out_dir.exists():
        return []
    return sorted(path for path in out_dir.glob("iteration_*") if path.is_dir())


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"could not parse {path}: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
