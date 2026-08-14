#!/usr/bin/env python3
"""Preview delivery fallback choices for historical poster runs.

This is a read-only diagnostic helper: it does not finalize the input runs or
invoke the external designer. It reuses the fallback selector/acceptance logic,
renders or copies the chosen candidate preview, and writes a small contact sheet.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from autodesign.agents.external_designer_author import (
    _best_available_artifact_fallback_acceptance,
    _best_available_artifact_fallback_candidates,
    _best_candidate_fallback_acceptance,
    _best_candidate_fallback_candidates,
    _direct_canvas,
    _fallback_hard_issue_ids,
    _fallback_remaining_issue_ids,
    _render_direct_preview,
)
from autodesign.config import load_settings
from autodesign.tools import ToolContext


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _last_feedback(run_dir: Path) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for path in sorted((run_dir / "designer_author").glob("attempt_*/validation_feedback.json")):
        data = _read_json(path)
        if isinstance(data, dict):
            latest = data
    return latest


def _ctx_for_run(run_dir: Path) -> ToolContext:
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-local-diagnostic")
    settings = load_settings()
    ctx = ToolContext(
        settings=settings,
        run_dir=run_dir,
        layers_dir=run_dir / "layers",
        run_id=run_dir.name,
    )
    canvas_plan = _read_json(run_dir / "canvas_plan.json")
    if isinstance(canvas_plan, dict):
        ctx.state["canvas_plan"] = canvas_plan
    return ctx


def _choose(ctx: ToolContext, last_feedback: dict[str, Any] | None) -> tuple[str, dict[str, Any] | None, dict[str, Any], list[dict[str, Any]]]:
    rejected: list[dict[str, Any]] = []
    for candidate in _best_candidate_fallback_candidates(ctx, last_feedback=last_feedback):
        acceptance = _best_candidate_fallback_acceptance(ctx, candidate, last_feedback)
        if acceptance.get("accepted"):
            return "best_candidate_fallback", candidate, acceptance, rejected
        rejected.append({
            "candidate_id": candidate.get("candidate_id"),
            "candidate_score": candidate.get("candidate_score"),
            "reason": acceptance.get("reason"),
            "details": acceptance.get("details") or {},
        })
    for candidate in _best_available_artifact_fallback_candidates(ctx, last_feedback=last_feedback):
        acceptance = _best_available_artifact_fallback_acceptance(ctx, candidate, last_feedback)
        if acceptance.get("accepted"):
            return "best_available_artifact_fallback", candidate, acceptance, rejected
        rejected.append({
            "candidate_id": candidate.get("candidate_id"),
            "candidate_score": candidate.get("candidate_score"),
            "reason": acceptance.get("reason"),
            "details": acceptance.get("details") or {},
        })
    return "none", None, {"accepted": False, "reason": "no_fallback_candidate"}, rejected


def _materialize_preview(ctx: ToolContext, candidate: dict[str, Any], out_dir: Path, index: int) -> str:
    preview_out = out_dir / f"{index:02d}_{ctx.run_dir.name}.png"
    html_path = Path(str(candidate.get("_measure_html_abs") or ""))
    preview_src = Path(str(candidate.get("_preview_png_abs") or ""))
    if html_path.exists():
        result = _render_direct_preview(
            html_path=html_path,
            preview_path=preview_out,
            canvas=_direct_canvas(ctx),
            ctx=ctx,
        )
        if getattr(result, "ok", False) and preview_out.exists():
            return preview_out.name
    if preview_src.exists():
        shutil.copy2(preview_src, preview_out)
        return preview_out.name
    return ""


def _render_index(rows: list[dict[str, Any]], output_dir: Path) -> None:
    cards: list[str] = []
    for row in rows:
        preview = row.get("preview") or ""
        image_html = f'<img src="{html.escape(preview)}" alt="preview">' if preview else "<div class='missing'>No preview</div>"
        rejected = row.get("rejected") or []
        rejected_bits = "".join(
            f"<li>{html.escape(str(item.get('candidate_id')))}: {html.escape(str(item.get('reason')))}</li>"
            for item in rejected[:5]
        )
        cards.append(
            "<section class='card'>"
            f"<h2>{html.escape(row['run_id'])}</h2>"
            f"<div class='mode'>{html.escape(row['mode'])}</div>"
            f"{image_html}"
            "<dl>"
            f"<dt>candidate</dt><dd>{html.escape(str(row.get('candidate_id') or ''))}</dd>"
            f"<dt>score</dt><dd>{html.escape(str(row.get('candidate_score') or ''))}</dd>"
            f"<dt>acceptance</dt><dd>{html.escape(str(row.get('acceptance_reason') or ''))}</dd>"
            f"<dt>remaining issues</dt><dd>{html.escape(', '.join(row.get('remaining_issue_ids') or []))}</dd>"
            f"<dt>hard issues</dt><dd>{html.escape(', '.join(row.get('remaining_hard_issue_ids') or []))}</dd>"
            "</dl>"
            f"<details><summary>Rejected candidates</summary><ul>{rejected_bits}</ul></details>"
            "</section>"
        )
    html_text = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Best Available Fallback Preview</title>
<style>
body { font-family: system-ui, sans-serif; margin: 24px; background: #f6f7f8; color: #172033; }
h1 { margin: 0 0 18px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(440px, 1fr)); gap: 18px; }
.card { background: white; border: 1px solid #d7dce2; border-radius: 8px; padding: 14px; }
.mode { font-size: 13px; color: #596579; margin-bottom: 10px; }
img { width: 100%; border: 1px solid #d7dce2; background: #fff; display: block; }
dl { display: grid; grid-template-columns: 130px 1fr; gap: 4px 10px; font-size: 13px; }
dt { font-weight: 700; color: #344055; }
dd { margin: 0; overflow-wrap: anywhere; }
.missing { height: 220px; display: grid; place-items: center; background: #eef1f4; color: #667085; }
</style>
</head>
<body>
<h1>Best Available Fallback Preview</h1>
<div class="grid">
""" + "\n".join(cards) + """
</div>
</body>
</html>
"""
    (output_dir / "index.html").write_text(html_text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output_dir = args.output_dir or Path("out/diagnostics") / f"best_available_fallback_preview_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for index, run_dir in enumerate(args.run_dirs, start=1):
        run_dir = run_dir.resolve()
        ctx = _ctx_for_run(run_dir)
        feedback = _last_feedback(run_dir)
        mode, candidate, acceptance, rejected = _choose(ctx, feedback)
        preview = _materialize_preview(ctx, candidate, output_dir, index) if candidate else ""
        rows.append({
            "run_id": run_dir.name,
            "mode": mode,
            "candidate_id": candidate.get("candidate_id") if candidate else "",
            "candidate_score": candidate.get("candidate_score") if candidate else "",
            "acceptance_reason": acceptance.get("reason"),
            "remaining_issue_ids": _fallback_remaining_issue_ids(candidate, feedback) if candidate else [],
            "remaining_hard_issue_ids": _fallback_hard_issue_ids(candidate, feedback) if candidate else [],
            "preview": preview,
            "rejected": rejected,
        })
    _render_index(rows, output_dir)
    print(output_dir / "index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
