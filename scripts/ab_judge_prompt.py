#!/usr/bin/env python3
"""A/B the VLM judge prompt change (lenient OLD vs sharpened NEW) on a sample of real
AutoDesign posters, full 7-dim scoring with the real critic. Confirms the sharpened
judge (a) spreads/lowers the distribution and (b) keeps the human-gold anchors high.

Two sequential phases (NEW then OLD) so the global prompt swap is race-free within a
phase. Run: uv run python scripts/ab_judge_prompt.py
"""
from __future__ import annotations
import statistics, sys, tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import autodesign.evaluator.tools as T  # noqa: E402
from scripts.score_quality_distribution import score_one  # noqa: E402
from scripts.visualize_numeric_grounding import _discover_poster_dirs  # noqa: E402

GOLD = {"arxiv2026_longcat_next", "sam2", "vit"}
SAMPLE_FRAGS = [
    "arxiv2026_longcat_next", "sam2", "ai_ml_existing_20/vit",   # gold (must stay high)
    "2017-attention-is-all-you-need", "nips2023_color_equivariant_cnn",
    "2005-radiotherapy-plus-concomitant", "2008-global-trends-in-emerging",
    "gwtc-1-a-gravitational", "icml2024_cot_transformers", "neurips2024_demo_motion",
]

# The lenient OLD prompt (pre-sharpening), restored verbatim for the A/B baseline.
_OLD_SYSTEM = """You are an academic-poster benchmark judge scoring ONE rubric dimension.
Judge only the visible rendered poster image against the source-paper brief, as a
conference attendee would. Do not assume access to HTML, DOM, prompts, or hidden
metadata. Do not favor any product, template, or house style. Penalize style only
when it harms readability, hierarchy, professionalism, or scientific communication.
Return JSON only with concise visible reasoning, not private chain-of-thought."""
_OLD_USER = """Score the dimension `{dimension}` for profile `{profile}`.

Dimension means: {dimension_summary}

Source-paper brief:
```json
{paper_brief}
```
{deterministic_grounding}
Return a JSON object with:
- dimension
- score_0_10: number in [0,10]
- rationale: 1-3 sentences grounded in visible poster evidence
- visible_evidence: 2-5 bullets describing what in the image drove the score
- judge_confidence: number in [0,1]"""


def main() -> int:
    found = {n: (n, p, t) for n, p, t in _discover_poster_dirs(_REPO / "DesignAnything_Poster")}
    picks = []
    for frag in SAMPLE_FRAGS:
        hit = next((v for k, v in found.items() if frag in k), None)
        if hit:
            picks.append(hit)
    work = Path(tempfile.mkdtemp(prefix="abjudge_"))

    def score_all(label):
        out = {}
        def run(item):
            name, preview, paper = item
            sub = work / f"{label}__{name.replace('/','_')}"; sub.mkdir(parents=True, exist_ok=True)
            return name, score_one(preview, paper, sub)["overall"]
        with ThreadPoolExecutor(max_workers=6) as ex:
            for fut in as_completed([ex.submit(run, it) for it in picks]):
                n, s = fut.result(); out[n] = s
        return out

    # NEW (current sharpened globals)
    print("scoring with NEW (sharpened) prompt ...", flush=True)
    new = score_all("new")
    # OLD (swap globals, race-free between phases)
    T._VLM_JUDGE_SYSTEM, T._VLM_JUDGE_USER = _OLD_SYSTEM, _OLD_USER
    print("scoring with OLD (lenient) prompt ...", flush=True)
    old = score_all("old")

    print(f"\n{'poster':40s} {'OLD':>6} {'NEW':>6} {'Δ':>6}  gold", flush=True)
    for n, _, _ in picks:
        o, nw = old.get(n), new.get(n)
        d = round(nw - o, 1) if (o is not None and nw is not None) else None
        g = "GOLD" if any(x in n for x in GOLD) else ""
        print(f"  {n.split('/')[-1][:38]:40s} {o:>6} {nw:>6} {str(d):>6}  {g}", flush=True)
    ov = [s for s in old.values() if s is not None]; nv = [s for s in new.values() if s is not None]
    print(f"\n  OLD mean={round(statistics.mean(ov),1)} std={round(statistics.pstdev(ov),1)} range={min(ov)}-{max(ov)}", flush=True)
    print(f"  NEW mean={round(statistics.mean(nv),1)} std={round(statistics.pstdev(nv),1)} range={min(nv)}-{max(nv)}", flush=True)
    gold_new = [new[n] for n in new if any(x in n for x in GOLD) and new[n] is not None]
    print(f"  GOLD under NEW: {gold_new} (should stay high, ~75+)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
