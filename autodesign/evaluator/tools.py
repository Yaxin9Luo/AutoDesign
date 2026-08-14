"""Rule-based benchmark evaluation tool palette.

Every tool wraps an existing deterministic metric or the per-dimension VLM judge
and returns a JSON-serializable dict. The same functions are importable by the
deterministic pre-pass and runnable from the benchmark scripts:

    python -m autodesign.evaluator.tools <tool> [--flags] > out.json

No metric algorithm is reimplemented here; this module only adapts existing
functions into a uniform tool interface and adds image cropping + the VLM judge.
"""

import argparse
import base64
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

from .schema import ArtifactSnapshot
from .metrics import (
    image_density_metrics,
    numeric_token_metrics,
    html_structure_metrics,
)
from .adapter import snapshot_artifact
from .ocr import run_ocr
from .poster_rubric import dimension_by_id


_IMAGE_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
DEFAULT_BENCHMARK_JUDGE_MODEL = "gemini-3.5-flash"


# --- deterministic tools -----------------------------------------------------

def tool_density(preview: Path, *, artifact_type: str = "poster") -> dict[str, Any]:
    snap = ArtifactSnapshot(
        artifact_path=str(preview),
        artifact_kind="image",
        preview_image=str(preview),
    )
    bundle, findings = image_density_metrics(snap, artifact_type=artifact_type)
    return {
        "tool": "density",
        "status": bundle.status,
        "metrics": bundle.metrics,
        "findings": [f.to_dict() for f in findings],
    }


def tool_numeric_grounding(*, artifact_text: str, paper: Path) -> dict[str, Any]:
    snap = ArtifactSnapshot(artifact_path="<text>", artifact_kind="text", text=artifact_text)
    bundle, findings = numeric_token_metrics(snap, paper)
    return {
        "tool": "numeric_grounding",
        "status": bundle.status,
        "metrics": bundle.metrics,
        "findings": [f.to_dict() for f in findings],
    }


def tool_render_audit(*, html: Path, canvas_w: int, canvas_h: int) -> dict[str, Any]:
    """Run the authored-poster gate audit when an HTML artifact is available."""
    try:
        from ..util.poster_gate_audit import audit_authored_poster_gate
    except Exception as exc:  # noqa: BLE001
        return {"tool": "render_audit", "status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
    payload = audit_authored_poster_gate(
        html,
        canvas={"w_px": int(canvas_w), "h_px": int(canvas_h)},
    )
    return {
        "tool": "render_audit",
        "status": payload.get("backend", "unknown"),
        "p0_count": payload.get("p0_count", 0),
        "p1_count": payload.get("p1_count", 0),
        "metrics": payload.get("metrics", {}),
        "findings": payload.get("findings", []),
        "warnings": payload.get("warnings", []),
    }


def tool_html_structure(*, html: Path, out_dir: Path | None = None) -> dict[str, Any]:
    work = out_dir or Path(tempfile.mkdtemp(prefix="eval_html_struct_"))
    snap = snapshot_artifact(html, work, artifact_type="poster")
    bundle, findings = html_structure_metrics(snap)
    return {
        "tool": "html_structure",
        "status": bundle.status,
        "metrics": bundle.metrics,
        "findings": [f.to_dict() for f in findings],
    }


def tool_ocr(*, image: Path, include_text: bool = False) -> dict[str, Any]:
    """Run the OCR bridge over a rendered poster image (RapidOCR; degrades if absent)."""
    result = run_ocr(image)
    if not include_text and isinstance(result, dict) and "text" in result:
        text = str(result.get("text") or "")
        result = {k: v for k, v in result.items() if k != "text"}
        result["text_excerpt"] = text[:2000]
    return {"tool": "ocr", **result}


def tool_crop(*, preview: Path, region: tuple[int, int, int, int], out: Path) -> dict[str, Any]:
    """Crop a region of the preview for closer visual inspection."""
    x0, y0, x1, y1 = region
    out.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(preview) as image:
        image = image.convert("RGB")
        w, h = image.size
        box = (max(0, x0), max(0, y0), min(w, x1), min(h, y1))
        if box[2] <= box[0] or box[3] <= box[1]:
            return {"tool": "crop", "status": "error", "error": "empty crop region", "image_size": [w, h]}
        crop = image.crop(box)
        crop.save(out, format="PNG")
    return {"tool": "crop", "status": "ok", "path": str(out), "region": list(box), "source_size": [w, h]}


# --- VLM judge tool ----------------------------------------------------------

def _format_grounding(dimension: str, grounding: dict[str, Any] | None) -> str:
    """Format deterministic detector evidence for visual quality dimensions."""
    if not grounding:
        return ""
    if "visual_evidence" in grounding or "paper_body_screenshot" in grounding:
        visual = grounding.get("visual_evidence") or {}
        body = grounding.get("paper_body_screenshot") or {}
        layout = grounding.get("basic_layout_integrity") or {}
        components = grounding.get("dimension_components") or {}
    else:
        # Backward compatibility for direct callers that pass visual_evidence only.
        visual = grounding
        body = {}
        layout = {}
        components = {}

    sections: list[str] = []
    if dimension == "visual_evidence_use" and visual.get("available"):
        flags = []
        if visual.get("no_figures_detected"):
            flags.append("no_figures_detected")
        if visual.get("possible_screenshot_wall"):
            flags.append("possible_screenshot_wall")
        if visual.get("figure_cramming"):
            flags.append(
                f"figure_cramming ({visual.get('cramming_cluster_size')} figures packed "
                "tightly into one block; confirm against the image)"
            )
        raw_count = visual.get("figure_region_count")
        raw_clause = f"; raw detector boxes={raw_count} (debug only)" if raw_count is not None else ""
        sections.append(
            "Deterministic visual-evidence grounding (image-native CV groups, ADVISORY and "
            "a lower bound because native tables or sparse line charts can be missed):\n"
            f"- evidence groups={visual.get('evidence_group_count')}; "
            f"grouped canvas area about {_pct_metric(visual.get('evidence_group_area_ratio'))}; "
            f"largest group about {_pct_metric(visual.get('largest_group_area_ratio'))}; "
            f"median group short edge={_pct_metric(visual.get('median_group_short_edge_ratio'))}; "
            f"thumbnail-sized groups={visual.get('thumbnail_group_count')}{raw_clause}; "
            f"OCR text coverage={visual.get('text_coverage')}.\n"
            f"- flags to verify: {', '.join(flags) if flags else 'none'}.\n"
            "Inspect every visible figure/table yourself. Do not hard-cap merely because "
            "CV found no figure. Do not award high scores from detector count, captions, "
            "or neat arrangement alone; evidence must be readable, locally explained, "
            "and hierarchically useful."
        )

    body_level = str(body.get("severity_level") or "none")
    if dimension in {"visual_evidence_use", "layout_readability", "professional_aesthetics"} and body_level != "none":
        sections.append(
            "Paper-body screenshot detector (source-text overlap plus OCR geometry; verify the "
            "image, but do not treat a confirmed page/body crop as a legitimate figure):\n"
            f"- severity={body_level}; reason={body.get('catastrophic_reason') or body.get('severe_reason') or body.get('moderate_reason')}; "
            f"OCR words={body.get('ocr_word_count')}; copied-token ratio={body.get('copied_token_ratio')}; "
            f"copied body canvas area={body.get('copied_body_segment_area_ratio')}.\n"
            "- Captions adjacent to legitimate plots are allowed. Dense source prose, paper "
            "columns, or several uncropped paper panels standing in for synthesis are serious defects."
        )

    if dimension in {"visual_evidence_use", "layout_readability", "professional_aesthetics"} and layout.get("available"):
        section_bounds = layout.get("section_bounds") or {}
        overlap = layout.get("overlap") or {}
        sections.append(
            "Deterministic layout grounding (ADVISORY; confirm against pixels because panel/CV "
            "inference can over-segment figures):\n"
            f"- layout score={layout.get('score_0_10')}; P1 findings={layout.get('p1_count')}; "
            f"P2 findings={layout.get('p2_count')}; median body-text height at 2048px={layout.get('median_body_text_height_ref_px')}.\n"
            f"- section overflow={section_bounds.get('content_overflow_count')}; "
            f"bottom-truncated sections={section_bounds.get('bottom_truncated_section_count')}; "
            f"text overlaps={overlap.get('text_overlap_count')}; "
            f"text/visual overlaps={overlap.get('text_visual_overlap_count')}.\n"
            "Penalize defects that are visibly real and ignore detector counts disproved by the image. "
            "For visual_evidence_use, clipped/mis-cropped figures, bottom truncation, or export-edge "
            "damage means the evidence is not fully usable even if many figures are present."
        )

    if dimension == "professional_aesthetics" and isinstance(components, dict):
        density = components.get("information_density_and_synthesis") or {}
        density_score = density.get("score_0_10") if isinstance(density, dict) else None
        if density_score is not None:
            sections.append(
                "Deterministic academic-poster density grounding (ADVISORY but important "
                "for professional craft):\n"
                f"- information_density_and_synthesis score={density_score}.\n"
                "- A clean, sparse report page, slide, or paper-digest layout should not "
                "receive high professional_aesthetics just because typography and spacing "
                "are consistent. Human-made conference posters normally look intentionally "
                "occupied, with dense but controlled sections and source visuals woven into "
                "the narrative."
            )

    return "\n\n" + "\n\n".join(sections) + "\n" if sections else ""


def _pct_metric(value: Any) -> str:
    try:
        return f"{round(float(value) * 100)}%"
    except (TypeError, ValueError):
        return "unknown"


def tool_vlm_judge(
    *,
    dimension: str,
    image: Path,
    paper_brief: dict[str, Any] | None = None,
    profile: str | None = None,
    dry_run: bool = False,
    model: str | None = None,
    grounding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Single-dimension VLM judge over the rendered poster image.

    This is the only tool that looks at pixels. It is deliberately one judge call
    scoped to one rubric dimension, with visible evidence required. ``grounding`` is an
    optional deterministic-signals dict (e.g. CV figure detection and layout damage)
    injected into the prompt to anchor the judgment; final reproducible caps are applied
    by the aggregator.
    """
    dim = dimension_by_id(dimension)
    dim_summary = dim.summary if dim else dimension
    judge_model = model or DEFAULT_BENCHMARK_JUDGE_MODEL
    if dry_run:
        return {
            "tool": "vlm_judge",
            "status": "dry_run",
            "dimension": dimension,
            "score_0_10": None,
            "rationale": "Dry run: VLM judge was not called.",
            "visible_evidence": [],
            "judge_confidence": None,
            "model": judge_model,
        }
    from ..config import load_settings
    from ..llm_backend import make_backend

    settings = load_settings()
    backend = make_backend(settings, judge_model, role="critic")
    system = _VLM_JUDGE_SYSTEM
    prompt = _VLM_JUDGE_USER.format(
        dimension=dimension,
        dimension_summary=dim_summary,
        profile=profile or "unspecified",
        paper_brief=json.dumps(paper_brief or {}, ensure_ascii=False, indent=2),
        deterministic_grounding=_format_grounding(dimension, grounding),
        defect_checklist=_DEFECT_CHECKLISTS.get(dimension, _DEFAULT_CHECKLIST),
        dimension_scoring_notes=_DIMENSION_SCORING_NOTES.get(dimension, _DEFAULT_SCORING_NOTES),
    )
    media_type = _IMAGE_MEDIA.get(image.suffix.lower(), "image/png")
    message = backend.vision_user_message(
        image_b64=base64.b64encode(image.read_bytes()).decode("ascii"),
        media_type=media_type,
        text=prompt,
    )
    response = backend.create_turn(
        system=system,
        messages=[message],
        tools=[],
        thinking_budget=0,
        max_tokens=2500,
    )
    report = _parse_json(response.text)
    report.setdefault("dimension", dimension)
    report.setdefault("status", "ok")
    report["model"] = judge_model
    return {"tool": "vlm_judge", **report}


_VLM_JUDGE_SYSTEM = """You are a STRICT reviewer for a top venue's best-poster award,
scoring ONE rubric dimension. Most posters have real flaws — your job is to FIND them,
not to be charitable. A high score is EARNED, it is not the default. Judge only the
visible rendered poster image against the source-paper brief, as a critical reviewer
who scrutinizes every section, not a passing attendee. Composition and figure-curation
defects — figure collages/cramming (several figures squished into one block), wrong or
distorted crops, starved or imbalanced columns, screenshot walls, overflow/clipping —
are FIRST-CLASS scoring criteria, not mere "style". Do not assume access to HTML, DOM,
prompts, or hidden metadata. Do not favor any product, template, or house style. Return
JSON only with concise visible reasoning, not private chain-of-thought."""

# Per-dimension defect lists the judge must actively hunt for. Injected as
# {defect_checklist}. These turn the positive one-line summary into a critical lens.
_DEFECT_CHECKLISTS = {
    "source_faithfulness":
        "- numbers, results, or claims not supported by the paper (hallucinated)\n"
        "- wrong title, authors, venue, or year\n"
        "- a figure/table that misrepresents the paper's findings",
    "paper_coverage":
        "- missing the paper's core arc or a must-cover result\n"
        "- padding that restates the title/abstract without conveying real findings\n"
        "- the key contribution buried, vague, or absent",
    "information_density_and_synthesis":
        "- thin content padded with whitespace; sections that say very little\n"
        "- one column dense while another is starved (content imbalance)\n"
        "- hollow panels; raw figure dumps instead of synthesized takeaways",
    "visual_evidence_use":
        "- figure COLLAGE / cramming: several figures squished into one block or panel "
        "(real conferences never do this — it is a serious defect)\n"
        "- blank or placeholder image frames, including a bordered figure slot that contains only a token label\n"
        "- wrong, distorted, or mis-cropped figure/table screenshots (clipped content, bad aspect)\n"
        "- figures/tables damaged by export-edge clipping, bottom truncation, or section overflow\n"
        "- figures with no nearby readout/caption; decorative-only figures; a screenshot wall",
    "basic_layout_integrity":
        "- broken size/aspect; unreadable text; export-edge damage; clipped content",
    "layout_readability":
        "- column imbalance; overflow, clipping, or overlap; crammed regions\n"
        "- inconsistent alignment; unclear or missing visual hierarchy",
    "professional_aesthetics":
        "- Scope boundary: judge ONLY academic-poster visual craft. Use density/layout "
        "grounding only when it changes visible craft: sparse, report-like, hollow, or "
        "unfinished posters are not strong human academic posters even if clean.\n"
        "- Typography system: inconsistent heading/body/caption hierarchy, random type "
        "sizes, mixed font personalities, weak line spacing, or typography that feels "
        "auto-filled rather than designed.\n"
        "- Composition and visual rhythm: weak focal point, unstable section rhythm, "
        "awkward panel proportions, mechanical numbering, template rhythm, or content "
        "blocks that look pasted together. Clean equal-weight repeated bands/boxes are "
        "only mid-tier craft when they do not create paper-specific hierarchy. A "
        "consistent boxed academic system is NOT automatically a defect; penalize it "
        "when it feels generic, weakly hierarchized, or mechanically filled. Large "
        "unused lower-page or column areas that make the poster feel unfinished are a "
        "serious craft defect, not just a density issue.\n"
        "- Color and restraint: clashing palette, cheap gradients, decorative color that "
        "competes with the science, poor contrast choices, a one-note default theme, excessive colors, too many colors, "
        "or many unrelated accent colors that make the poster look like a generic AI design "
        "instead of a disciplined academic artifact.\n"
        "- Figure/table craft: figures, tables, captions, and callouts that do not feel "
        "integrated into the poster system; distorted or visually isolated plots. Do "
        "not call a purposeful qualitative/sample grid a screenshot wall when it is "
        "central evidence for the paper and has nearby captions/readouts. Penalize a "
        "single catch-all figure section that crams most/all images into miniature paper "
        "crops instead of distributing visual evidence across the narrative. Do not apply "
        "that penalty to a dedicated results/visual-evidence panel when the images are "
        "core sample grids or qualitative comparisons with nearby readouts.\n"
        "- Academic polish: does the poster look like a mature human-made conference poster "
        "for NeurIPS/ICML/CVPR, or does it read as amateur, AI-template, sparse report page, "
        "or slide-deck filler?",
}
_DEFAULT_CHECKLIST = "- any visible composition, content, readability, or curation defect"

_DIMENSION_SCORING_NOTES = {
    "visual_evidence_use":
        "Visual evidence score anchors: 9-10 requires multiple readable locally explained "
        "evidence groups plus a strong focal anchor; 7-8 is correct and source-backed but "
        "partly small, table-heavy, or missing a decisive visual hierarchy; 5-6.5 means "
        "thumbnails, tiny labels, equal-weight evidence, or no focal hierarchy; 0-4 means "
        "damaged, unreadable, wall-like, or decorative evidence. Do not award high scores "
        "based only on count, captions, or neat arrangement; native tables or sparse line "
        "charts can be missed by CV, so do not hard-cap merely because no figure group is "
        "detected. A visibly clipped, distorted, or mis-cropped core figure/table caps "
        "visual evidence at 6; if this combines with bottom truncation, export-edge damage, "
        "or a basic layout score below 6, cap at 5 unless the visible image clearly "
        "disproves the detector.",
    "professional_aesthetics":
        "Professional aesthetics cap rules: judge human-made conference poster craft, "
        "not generic clean design. Use these instead of the generic <=5 cap when they "
        "are more specific. An unexplained screenshot wall, decorative "
        "figure collage, or unreadable pasted paper crop caps at 6. A purposeful "
        "sample grid or qualitative-comparison grid that is central to the paper and "
        "has captions/readouts does NOT trigger the collage cap; judge it by "
        "integration and clarity. A dedicated results/visual-evidence panel with core "
        "sample grids or qualitative comparisons does NOT trigger the catch-all-section "
        "cap if other parts of the poster also carry visual/table evidence. A poster "
        "with large unused lower-page/column areas "
        "that make it look unfinished caps at 5. A poster that concentrates most/all "
        "visual evidence into one cramped paper-figure screenshot/crop section caps at "
        "5.5, and if that is combined with unfinished whitespace or weak hierarchy it "
        "should score in the 4-5 range. Severe typography-system inconsistency caps at 7. "
        "Clean equal-weight repeated bands/boxes, mechanical numbering, or template rhythm "
        "is a 6-7 pattern even when spacing is neat. 8+ needs paper-specific hierarchy, "
        "focal evidence, controlled non-default palette, and scale variation. Cheap/clashing "
        "palette, excessive colors, too many colors, overly many accent colors, a one-note default theme, or decoration "
        "overpowering content caps at 7; if the color variety makes the poster look like "
        "a generic AI dashboard/slide rather than a disciplined academic poster, score "
        "in the 5-6.5 range. Visually "
        "detached/distorted figure/table treatment caps at 6. Obvious generic "
        "template-fill or AI-default visual language caps at 7 even when it is clean. "
        "A clean but sparse paper digest, report page, or slide-like layout caps at "
        "6.5-7 because it lacks conference-poster craft. A coherent, information-rich "
        "boxed academic poster can score above 8 only when it feels intentionally "
        "authored, visually disciplined, and densely occupied like a human-made conference poster. "
        "Award-level 9-10 requires exceptional composition beyond being clean and professional.",
}
_DEFAULT_SCORING_NOTES = "No dimension-specific overrides."


_VLM_JUDGE_USER = """Strictly score the dimension `{dimension}` for profile `{profile}`.

Dimension means: {dimension_summary}

Actively HUNT, section by section, for these disqualifying defects:
{defect_checklist}

Source-paper brief:
```json
{paper_brief}
```
{deterministic_grounding}
Score bands — use the FULL range, do NOT cluster at 7-9:
- 9-10: award-worthy; no notable defect.
- 7-8: solid; only minor blemishes.
- 5-6: a clear defect a reviewer would flag.
- 3-4: multiple real defects, or visibly un-academic composition.
- 1-2: badly broken or unusable for this dimension.
A SINGLE serious defect (figure collage/cramming, wrong or mis-crop, starved column,
screenshot wall, overflow/clipping) CAPS this dimension at 5 unless the
dimension-specific scoring notes below define a more precise cap.

Dimension-specific scoring notes:
{dimension_scoring_notes}

Calibration (do NOT over-penalize clean work): minor blemishes do NOT pull a
well-executed poster below 7; a clean, well-composed poster with NO serious defect
should score 8-9 even if it is not perfect. Reserve scores of 5 and below for a poster
that ACTUALLY has a serious defect from the list — do not invent serious defects to be
harsh. Be strict about real defects, generous about their absence.

Return a JSON object with:
- dimension
- defects_found: array of {{"defect": str, "where": str, "severity": "minor" or "serious"}} (empty array ONLY if truly none)
- score_0_10: number in [0,10], CONSISTENT with defects_found and the cap rules above
- rationale: 1-3 sentences naming the worst defect (or why none), grounded in visible evidence
- visible_evidence: 2-5 bullets describing what in the image drove the score
- judge_confidence: number in [0,1]"""


def _parse_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fence:
        stripped = fence.group(1)
    else:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start >= 0 and end > start:
            stripped = stripped[start:end + 1]
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return {"status": "parse_error", "error": f"JSONDecodeError: {exc}", "raw_excerpt": text[:1500]}
    return data if isinstance(data, dict) else {"status": "parse_error", "error": "not an object"}


# --- CLI dispatcher ----------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="tool", required=True)

    p = sub.add_parser("density")
    p.add_argument("--preview", required=True, type=Path)
    p.add_argument("--type", dest="artifact_type", default="poster")

    p = sub.add_parser("numeric_grounding")
    p.add_argument("--artifact-text", required=True, type=Path, help="UTF-8 text file of poster text.")
    p.add_argument("--paper", required=True, type=Path)

    p = sub.add_parser("render_audit")
    p.add_argument("--html", required=True, type=Path)
    p.add_argument("--canvas-w", required=True, type=int)
    p.add_argument("--canvas-h", required=True, type=int)

    p = sub.add_parser("html_structure")
    p.add_argument("--html", required=True, type=Path)

    p = sub.add_parser("ocr")
    p.add_argument("--image", required=True, type=Path)
    p.add_argument("--include-text", action="store_true", help="Include full recognized text in output.")

    p = sub.add_parser("crop")
    p.add_argument("--preview", required=True, type=Path)
    p.add_argument("--region", required=True, help="x0,y0,x1,y1 in source pixels.")
    p.add_argument("--out", required=True, type=Path)

    p = sub.add_parser("vlm_judge")
    p.add_argument("--dimension", required=True)
    p.add_argument("--image", required=True, type=Path)
    p.add_argument("--paper-brief", type=Path, default=None)
    p.add_argument("--report", type=Path, default=None,
                   help="deterministic_report.json; supplies grounding (e.g. CV figures for visual_evidence_use).")
    p.add_argument("--profile", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    try:
        result = _dispatch(args)
    except Exception as exc:  # noqa: BLE001 - surface tool errors as JSON for scripts
        result = {"tool": args.tool, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.tool == "density":
        return tool_density(args.preview, artifact_type=args.artifact_type)
    if args.tool == "numeric_grounding":
        text = args.artifact_text.read_text(encoding="utf-8", errors="replace")
        return tool_numeric_grounding(artifact_text=text, paper=args.paper)
    if args.tool == "render_audit":
        return tool_render_audit(html=args.html, canvas_w=args.canvas_w, canvas_h=args.canvas_h)
    if args.tool == "html_structure":
        return tool_html_structure(html=args.html)
    if args.tool == "ocr":
        return tool_ocr(image=args.image, include_text=args.include_text)
    if args.tool == "crop":
        parts = [int(float(v)) for v in str(args.region).split(",")]
        if len(parts) != 4:
            return {"tool": "crop", "status": "error", "error": "region must be x0,y0,x1,y1"}
        return tool_crop(preview=args.preview, region=(parts[0], parts[1], parts[2], parts[3]), out=args.out)
    if args.tool == "vlm_judge":
        brief = None
        if args.paper_brief and args.paper_brief.exists():
            brief = json.loads(args.paper_brief.read_text(encoding="utf-8"))
        grounding = None
        if args.report and args.report.exists():
            report = json.loads(args.report.read_text(encoding="utf-8"))
            grounding = report.get("metric_bundles", {}) or {}
        return tool_vlm_judge(
            dimension=args.dimension,
            image=args.image,
            paper_brief=brief,
            profile=args.profile,
            model=args.model,
            dry_run=args.dry_run,
            grounding=grounding,
        )
    return {"status": "error", "error": f"unknown tool: {args.tool}"}


if __name__ == "__main__":
    raise SystemExit(main())
