#!/usr/bin/env python3
"""A/B test the visual_evidence grounding with the REAL critic model: judge each poster
WITH the deterministic grounding injected and WITHOUT it, and compare scores.

The point is the anchoring risk: when the CV is wrong (e.g. a false no_figures_detected
on a poster that actually has a survival curve + table), does feeding that flag drag the
VLM's score down vs. judging the bare image? A safe grounding moves scores little and
never anchors on a wrong flag.

Run:
    uv run python scripts/run_visual_evidence_ab.py
"""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from autodesign.evaluator.quality_rubric import compute_deterministic_report  # noqa: E402
from autodesign.evaluator.tools import tool_vlm_judge  # noqa: E402

PICKS = [
    ("radiotherapy", "2005-radiotherapy-plus-concomitant", "CV false-empty (has curve+table)"),
    ("dust", "1998-maps-of-dust-infrared", "CV true-empty (really no figures)"),
    ("attention", "2017-attention-is-all-you-need", "CV undercount (missed results table)"),
    ("patchrot", "nips2022_patchrot", "CV accurate (5 figs)"),
    ("maskrcnn", "2017-mask-r-cnn", "CV accurate (7 figs)"),
    ("gwtc", "gwtc-1-a-gravitational-wave", "CV accurate (5 figs)"),
]


def judge(prev: Path, grounding):
    out = tool_vlm_judge(dimension="visual_evidence_use", image=prev, paper_brief={}, grounding=grounding)
    return out.get("score_0_10"), str(out.get("rationale") or "")[:400]


def main() -> int:
    results = []
    for tag, frag, note in PICKS:
        cand = [p for p in (_REPO / "DesignAnything_Poster").rglob("preview.png") if frag in str(p)]
        if not cand:
            print(f"  SKIP {tag}: not found", flush=True)
            continue
        prev = cand[0]
        with tempfile.TemporaryDirectory() as d:
            rep = compute_deterministic_report(paper=None, candidate_artifact=prev, out_dir=Path(d))
        ve = rep["metric_bundles"]["visual_evidence"]
        with_s, with_r = judge(prev, ve)
        without_s, without_r = judge(prev, None)
        delta = (with_s - without_s) if (with_s is not None and without_s is not None) else None
        results.append({"tag": tag, "note": note, "cv_count": ve["figure_region_count"],
                        "cv_area": ve["figure_area_ratio"], "no_figures": ve["no_figures_detected"],
                        "with": with_s, "without": without_s, "delta": delta,
                        "with_rationale": with_r, "without_rationale": without_r})
        print(f"  {tag:13s} cv_figs={ve['figure_region_count']} no_fig={ve['no_figures_detected']} | "
              f"with={with_s}  without={without_s}  delta={delta}  ({note})", flush=True)
    Path("/tmp/ve_ab.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
    deltas = [r["delta"] for r in results if r["delta"] is not None]
    print(f"\n=== mean |delta| = {round(sum(abs(d) for d in deltas)/len(deltas),2) if deltas else 'n/a'} ; "
          f"radiotherapy (false-empty) delta = {next((r['delta'] for r in results if r['tag']=='radiotherapy'), 'n/a')} ===", flush=True)
    print("saved /tmp/ve_ab.json", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
