#!/usr/bin/env python3
"""Run the professional_aesthetics VLM judge over poster image folders."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from autodesign.evaluator.tools import tool_vlm_judge  # noqa: E402
from autodesign.util.io import atomic_write_json  # noqa: E402


def _safe_name(value: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
    return out or "candidate"


def _iter_cases(root: Path) -> list[tuple[str, Path]]:
    cases: list[tuple[str, Path]] = []
    for case_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        image = case_dir / "poster.png"
        if not image.exists():
            image = case_dir / "preview.png"
        if image.exists():
            cases.append((case_dir.name, image))
    return cases


def _paper_for(case: str, paper_root: Path | None) -> Path | None:
    if paper_root is None:
        return None
    paper = paper_root / case / "paper.pdf"
    return paper if paper.exists() else None


def _aesthetics_from_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "score_0_10": result.get("score_0_10"),
        "rationale": result.get("rationale") or "",
        "evidence": result.get("visible_evidence") or result.get("evidence") or [],
        "defects_found": result.get("defects_found") or [],
        "judge_confidence": result.get("judge_confidence"),
        "model": result.get("model"),
        "status": result.get("status") or "ok",
    }


def _write_candidate_report(
    *,
    out_dir: Path,
    root_name: str,
    case: str,
    image: Path,
    paper: Path | None,
    result: dict[str, Any],
) -> None:
    candidate_name = f"{root_name}_{case}" if root_name else case
    cdir = out_dir / "candidates" / _safe_name(candidate_name)
    det_dir = cdir / "deterministic"
    judge_dir = cdir / "vlm"
    det_dir.mkdir(parents=True, exist_ok=True)
    judge_dir.mkdir(parents=True, exist_ok=True)

    aes = _aesthetics_from_result(result)
    atomic_write_json(det_dir / "deterministic_report.json", {
        "preview_image": str(image.resolve()),
        "metric_bundles": {},
        "findings": [],
    })
    judge_path = judge_dir / "professional_aesthetics.json"
    atomic_write_json(judge_path, result)
    dim = {
        "id": "professional_aesthetics",
        "weight": 10.0,
        "owner": "subjective",
        "score_0_10": aes.get("score_0_10"),
        "normalized": (float(aes["score_0_10"]) / 10.0) if isinstance(aes.get("score_0_10"), (int, float)) else None,
        "source": "judge",
        "status": aes.get("status") or "ok",
        "rationale": aes.get("rationale") or "",
        "visible_evidence": aes.get("evidence") or [],
        "metrics": {
            "defects_found": aes.get("defects_found") or [],
            "judge_confidence": aes.get("judge_confidence"),
            "model": aes.get("model"),
        },
    }
    atomic_write_json(cdir / "poster_quality_report.json", {
        "version": "professional-aesthetics-vlm-0.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "aesthetics_vlm",
        "candidate_name": candidate_name,
        "artifact": str(image.resolve()),
        "paper": str(paper.resolve()) if paper else None,
        "overall_score_0_100": None,
        "gate_triggered": False,
        "verdict": "aesthetics_only",
        "dimensions": [dim],
        "findings": [],
        "judge_report_path": str(judge_path.resolve()),
        "deterministic_report_path": str((det_dir / "deterministic_report.json").resolve()),
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poster-root", action="append", type=Path, required=True, help="Directory with one subdirectory per case.")
    parser.add_argument("--paper-root", type=Path, default=_REPO / "eval/EvaData/ai_ml_existing_20")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--case", action="append", default=None, help="Optional case name filter; repeatable.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    wanted = set(args.case or [])
    completed = 0
    failures: list[dict[str, str]] = []
    for root in [p.expanduser().resolve() for p in args.poster_root]:
        root_name = root.parent.name if root.name == "ai_ml_existing_20" else root.name
        for case, image in _iter_cases(root):
            if wanted and case not in wanted:
                continue
            candidate_name = f"{root_name}_{case}" if root_name else case
            report_path = args.out_dir / "candidates" / _safe_name(candidate_name) / "poster_quality_report.json"
            if report_path.exists() and not args.force:
                completed += 1
                continue
            if args.limit is not None and completed >= args.limit:
                break
            paper = _paper_for(case, args.paper_root.expanduser().resolve() if args.paper_root else None)
            print(f"[{completed + 1}] judging {candidate_name} -> {image}", flush=True)
            try:
                result = tool_vlm_judge(
                    dimension="professional_aesthetics",
                    image=image,
                    paper_brief={},
                    model=args.model,
                    dry_run=False,
                )
                _write_candidate_report(
                    out_dir=args.out_dir,
                    root_name=root_name,
                    case=case,
                    image=image,
                    paper=paper,
                    result=result,
                )
                completed += 1
            except Exception as exc:  # noqa: BLE001 - keep batch going and report failures.
                failures.append({"candidate": candidate_name, "error": str(exc)})
                print(f"ERROR {candidate_name}: {exc}", file=sys.stderr, flush=True)
    atomic_write_json(args.out_dir / "professional_aesthetics_batch.json", {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "completed": completed,
        "failures": failures,
    })
    print(f"Completed {completed}; failures {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
