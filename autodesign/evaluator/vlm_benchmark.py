"""VLM-backed poster benchmark judges.

This module is separate from the generation-time critic. Benchmark inputs are
only the source paper plus rendered poster artifacts (PNG/PDF). The generated
HTML/DOM, DesignSpec, and repair-tool context are intentionally excluded from
the scoring path.
"""

from __future__ import annotations

import base64
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz
from PIL import Image

from autodesign.config import Settings, load_settings
from autodesign.llm_backend import LLMBackend, make_backend
from autodesign.util.io import atomic_write_json

from .adapter import snapshot_artifact
from .tools import DEFAULT_BENCHMARK_JUDGE_MODEL


POSTER_BENCHMARK_VERSION = "0.1.0"
POSTER_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}
_MAX_PAPER_CHARS = 90_000
_MAX_IMAGE_EDGE = 1800


@dataclass(frozen=True)
class PosterCandidate:
    name: str
    artifact: Path


def run_poster_benchmark(
    *,
    paper: Path,
    candidates: list[PosterCandidate],
    out_dir: Path,
    profile: str | None = None,
    model: str | None = None,
    pairwise: bool = True,
    pairwise_limit: int | None = None,
    dry_run: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run VLM poster benchmark judges and write an audit-friendly report."""

    if not candidates:
        raise ValueError("at least one poster candidate is required")
    paper = paper.expanduser().resolve()
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    _validate_inputs(paper, candidates)

    paper_text = _extract_paper_text(paper)
    paper_digest = _build_paper_digest(paper_text)
    backend: LLMBackend | None = None
    judge_model = model or DEFAULT_BENCHMARK_JUDGE_MODEL
    if not dry_run:
        settings = settings or load_settings()
        backend = make_backend(settings, judge_model, role="critic")

    brief_dir = out_dir / "paper_brief"
    paper_brief = _run_paper_brief_judge(
        paper_digest=paper_digest,
        out_dir=brief_dir,
        backend=backend,
        model=judge_model,
        dry_run=dry_run,
    )

    candidate_reports: list[dict[str, Any]] = []
    prepared: list[dict[str, Any]] = []
    for candidate in candidates:
        item = _prepare_candidate(candidate, out_dir)
        prepared.append(item)
        report = _run_single_poster_judge(
            candidate=item,
            paper_brief=paper_brief,
            profile=profile,
            backend=backend,
            model=judge_model,
            dry_run=dry_run,
        )
        candidate_reports.append(report)

    pairwise_reports: list[dict[str, Any]] = []
    if pairwise and len(prepared) >= 2:
        pairs = _candidate_pairs(prepared)
        if pairwise_limit is not None:
            pairs = pairs[:max(0, pairwise_limit)]
        for left, right in pairs:
            pairwise_reports.append(_run_pairwise_judge(
                left=left,
                right=right,
                paper_brief=paper_brief,
                profile=profile,
                backend=backend,
                model=judge_model,
                out_dir=out_dir / "pairwise",
                dry_run=dry_run,
            ))

    report = {
        "version": POSTER_BENCHMARK_VERSION,
        "mode": "dry_run" if dry_run else "vlm",
        "model": judge_model,
        "profile": profile,
        "paper": str(paper),
        "input_contract": {
            "candidate_formats": sorted(POSTER_EXTS),
            "html_allowed": False,
            "generated_dom_allowed": False,
        },
        "paper_brief": paper_brief,
        "candidates": candidate_reports,
        "pairwise": pairwise_reports,
        "outputs": {
            "paper_brief": str(brief_dir),
            "candidates_dir": str(out_dir / "candidates"),
            "pairwise_dir": str(out_dir / "pairwise"),
        },
    }
    _write_summary_outputs(report, out_dir)
    return report


def _validate_inputs(paper: Path, candidates: list[PosterCandidate]) -> None:
    if not paper.exists():
        raise FileNotFoundError(f"paper does not exist: {paper}")
    seen_names: set[str] = set()
    seen_safe_names: set[str] = set()
    for candidate in candidates:
        if candidate.name in seen_names:
            raise ValueError(f"duplicate candidate name: {candidate.name}")
        seen_names.add(candidate.name)
        safe_name = _safe_name(candidate.name)
        if safe_name in seen_safe_names:
            raise ValueError(f"candidate names collide after path-safe normalization: {candidate.name}")
        seen_safe_names.add(safe_name)
        artifact = candidate.artifact.expanduser()
        suffix = artifact.suffix.lower()
        if suffix not in POSTER_EXTS:
            raise ValueError(
                f"benchmark candidate must be PNG/JPG/WebP/PDF, got {artifact}"
            )
        if not artifact.exists():
            raise FileNotFoundError(f"candidate does not exist: {artifact}")


def _extract_paper_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        with fitz.open(path) as doc:
            return "\n".join(page.get_text("text") for page in doc)
    return path.read_text(encoding="utf-8", errors="replace")


def _build_paper_digest(text: str) -> str:
    cleaned = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(cleaned) <= _MAX_PAPER_CHARS:
        return cleaned
    head = cleaned[:60_000]
    tail = cleaned[-25_000:]
    return (
        head
        + "\n\n[... paper text truncated for judge prompt ...]\n\n"
        + tail
    )


def _prepare_candidate(candidate: PosterCandidate, out_dir: Path) -> dict[str, Any]:
    safe = _safe_name(candidate.name)
    candidate_dir = out_dir / "candidates" / safe
    snapshot_dir = candidate_dir / "snapshot"
    snapshot = snapshot_artifact(candidate.artifact, snapshot_dir, artifact_type="poster")
    if not snapshot.preview_image:
        raise RuntimeError(f"candidate could not be rendered to preview: {candidate.artifact}")
    judge_image = candidate_dir / "judge_input.jpg"
    _write_judge_image(Path(snapshot.preview_image), judge_image)
    payload = {
        "name": candidate.name,
        "safe_name": safe,
        "artifact": str(candidate.artifact.expanduser().resolve()),
        "artifact_kind": snapshot.artifact_kind,
        "preview_image": snapshot.preview_image,
        "judge_image": str(judge_image),
        "snapshot": snapshot.to_dict(),
        "out_dir": str(candidate_dir),
    }
    atomic_write_json(candidate_dir / "candidate_manifest.json", payload)
    return payload


def _write_judge_image(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as image:
        image = image.convert("RGB")
        image.thumbnail((_MAX_IMAGE_EDGE, _MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
        image.save(dest, format="JPEG", quality=86, optimize=True)


def _run_paper_brief_judge(
    *,
    paper_digest: str,
    out_dir: Path,
    backend: LLMBackend | None,
    model: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    system = PAPER_BRIEF_SYSTEM_PROMPT
    prompt = PAPER_BRIEF_USER_PROMPT.format(paper_text=paper_digest)
    (out_dir / "system_prompt.md").write_text(system, encoding="utf-8")
    (out_dir / "user_prompt.md").write_text(prompt, encoding="utf-8")
    if dry_run:
        report = {
            "status": "dry_run",
            "model": model,
            "paper_title": None,
            "must_cover_points": [],
            "important_figures_tables": [],
            "raw_response_path": None,
        }
        atomic_write_json(out_dir / "paper_brief_report.json", report)
        (out_dir / "paper_brief_report.md").write_text(_paper_brief_markdown(report), encoding="utf-8")
        return report
    assert backend is not None
    response = backend.create_turn(
        system=system,
        messages=[{"role": "user", "content": prompt}],
        tools=[],
        thinking_budget=0,
        max_tokens=6000,
    )
    (out_dir / "raw_response.txt").write_text(response.text, encoding="utf-8")
    report = _parse_json_response(response.text)
    report.setdefault("status", "ok")
    report.update({
        "model": model,
        "usage": response.usage,
        "raw_response_path": str(out_dir / "raw_response.txt"),
    })
    atomic_write_json(out_dir / "paper_brief_report.json", report)
    (out_dir / "paper_brief_report.md").write_text(_paper_brief_markdown(report), encoding="utf-8")
    return report


def _run_single_poster_judge(
    *,
    candidate: dict[str, Any],
    paper_brief: dict[str, Any],
    profile: str | None,
    backend: LLMBackend | None,
    model: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    out_dir = Path(candidate["out_dir"])
    system = POSTER_QUALITY_SYSTEM_PROMPT
    prompt = POSTER_QUALITY_USER_PROMPT.format(
        candidate_label=candidate["name"],
        profile=profile or "unspecified",
        paper_brief=json.dumps(paper_brief, ensure_ascii=False, indent=2),
    )
    (out_dir / "judge_system_prompt.md").write_text(system, encoding="utf-8")
    (out_dir / "judge_user_prompt.md").write_text(prompt, encoding="utf-8")
    if dry_run:
        report = _dry_run_single_report(candidate, model)
    else:
        assert backend is not None
        image_b64 = _image_b64(Path(candidate["judge_image"]))
        message = backend.vision_user_message(
            image_b64=image_b64,
            media_type="image/jpeg",
            text=prompt,
        )
        response = backend.create_turn(
            system=system,
            messages=[message],
            tools=[],
            thinking_budget=0,
            max_tokens=7000,
        )
        (out_dir / "judge_raw_response.txt").write_text(response.text, encoding="utf-8")
        report = _parse_json_response(response.text)
        report.setdefault("status", "ok")
        report.update({
            "model": model,
            "usage": response.usage,
            "raw_response_path": str(out_dir / "judge_raw_response.txt"),
        })
    report.setdefault("candidate", candidate["name"])
    report.setdefault("artifact", candidate["artifact"])
    report.setdefault("judge_image", candidate["judge_image"])
    atomic_write_json(out_dir / "judge_report.json", report)
    (out_dir / "judge_report.md").write_text(_single_report_markdown(report), encoding="utf-8")
    return report


def _run_pairwise_judge(
    *,
    left: dict[str, Any],
    right: dict[str, Any],
    paper_brief: dict[str, Any],
    profile: str | None,
    backend: LLMBackend | None,
    model: str | None,
    out_dir: Path,
    dry_run: bool,
) -> dict[str, Any]:
    safe_pair = f"{left['safe_name']}__vs__{right['safe_name']}"
    pair_dir = out_dir / safe_pair
    pair_dir.mkdir(parents=True, exist_ok=True)
    system = PAIRWISE_SYSTEM_PROMPT
    prompt_a = PAIRWISE_USER_PROMPT_A.format(
        profile=profile or "unspecified",
        paper_brief=json.dumps(paper_brief, ensure_ascii=False, indent=2),
    )
    prompt_b = PAIRWISE_USER_PROMPT_B
    (pair_dir / "judge_system_prompt.md").write_text(system, encoding="utf-8")
    (pair_dir / "candidate_a_prompt.md").write_text(prompt_a, encoding="utf-8")
    (pair_dir / "candidate_b_prompt.md").write_text(prompt_b, encoding="utf-8")
    mapping = {
        "A": {"name": left["name"], "artifact": left["artifact"], "judge_image": left["judge_image"]},
        "B": {"name": right["name"], "artifact": right["artifact"], "judge_image": right["judge_image"]},
    }
    atomic_write_json(pair_dir / "candidate_mapping.json", mapping)
    if dry_run:
        report = {
            "status": "dry_run",
            "model": model,
            "winner": "tie",
            "visible_reasoning": "Dry run: pairwise judge was not called.",
            "candidate_mapping_path": str(pair_dir / "candidate_mapping.json"),
        }
    else:
        assert backend is not None
        msg_a = backend.vision_user_message(
            image_b64=_image_b64(Path(left["judge_image"])),
            media_type="image/jpeg",
            text=prompt_a,
        )
        msg_b = backend.vision_user_message(
            image_b64=_image_b64(Path(right["judge_image"])),
            media_type="image/jpeg",
            text=prompt_b,
        )
        response = backend.create_turn(
            system=system,
            messages=[msg_a, msg_b],
            tools=[],
            thinking_budget=0,
            max_tokens=7000,
        )
        (pair_dir / "judge_raw_response.txt").write_text(response.text, encoding="utf-8")
        report = _parse_json_response(response.text)
        report.setdefault("status", "ok")
        report.update({
            "model": model,
            "usage": response.usage,
            "raw_response_path": str(pair_dir / "judge_raw_response.txt"),
            "candidate_mapping_path": str(pair_dir / "candidate_mapping.json"),
        })
    report.setdefault("pair_id", safe_pair)
    report.setdefault("candidate_a", left["name"])
    report.setdefault("candidate_b", right["name"])
    atomic_write_json(pair_dir / "pairwise_report.json", report)
    (pair_dir / "pairwise_report.md").write_text(_pairwise_markdown(report, mapping), encoding="utf-8")
    return report


def _candidate_pairs(items: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for i, left in enumerate(items):
        for right in items[i + 1:]:
            pairs.append((left, right))
    return pairs


def _parse_json_response(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fence:
        stripped = fence.group(1)
    else:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            stripped = stripped[start:end + 1]
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return {
            "status": "parse_error",
            "error": f"JSONDecodeError: {exc}",
            "raw_text_excerpt": text[:2000],
        }
    if not isinstance(data, dict):
        return {"status": "parse_error", "error": "judge JSON was not an object", "raw": data}
    return data


def _write_summary_outputs(report: dict[str, Any], out_dir: Path) -> None:
    atomic_write_json(out_dir / "benchmark_report.json", report)
    rows: list[dict[str, Any]] = []
    for item in report.get("candidates") or []:
        rows.append({
            "candidate": item.get("candidate"),
            "overall_score_0_100": item.get("overall_score_0_100"),
            "status": item.get("status"),
            "confidence": item.get("judge_confidence"),
            "summary": item.get("executive_summary"),
        })
    if rows:
        with (out_dir / "candidate_scores.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    pair_rows: list[dict[str, Any]] = []
    for item in report.get("pairwise") or []:
        pair_rows.append({
            "pair_id": item.get("pair_id"),
            "candidate_a": item.get("candidate_a"),
            "candidate_b": item.get("candidate_b"),
            "winner": item.get("winner"),
            "confidence": item.get("judge_confidence"),
            "visible_reasoning": item.get("visible_reasoning"),
        })
    if pair_rows:
        with (out_dir / "pairwise_results.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(pair_rows[0].keys()))
            writer.writeheader()
            writer.writerows(pair_rows)
    (out_dir / "benchmark_report.md").write_text(_benchmark_markdown(report), encoding="utf-8")


def _image_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return safe.strip("._") or "candidate"


def _dry_run_single_report(candidate: dict[str, Any], model: str | None) -> dict[str, Any]:
    return {
        "status": "dry_run",
        "model": model,
        "candidate": candidate["name"],
        "overall_score_0_100": None,
        "executive_summary": "Dry run: VLM poster judge was not called.",
        "dimension_scores": {},
        "strengths": [],
        "weaknesses": [],
        "critical_errors": [],
        "visible_reasoning": "Prompts and candidate images were staged for review.",
        "judge_confidence": None,
    }


def _paper_brief_markdown(report: dict[str, Any]) -> str:
    lines = ["# Paper Brief Judge", "", f"- Status: `{report.get('status')}`", f"- Model: `{report.get('model')}`", ""]
    for key in ("paper_title", "one_sentence_thesis"):
        if report.get(key):
            lines.append(f"- {key}: {report.get(key)}")
    for key in ("core_contributions", "key_results", "important_figures_tables", "must_cover_points"):
        values = report.get(key)
        if isinstance(values, list) and values:
            lines.extend(["", f"## {key}"])
            for value in values:
                lines.append(f"- {value}")
    lines.append("")
    return "\n".join(lines)


def _single_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Poster VLM Judge Report",
        "",
        f"- Candidate: `{report.get('candidate')}`",
        f"- Status: `{report.get('status')}`",
        f"- Model: `{report.get('model')}`",
        f"- Overall score: `{report.get('overall_score_0_100')}`",
        f"- Confidence: `{report.get('judge_confidence')}`",
        "",
        "## Executive Summary",
        "",
        str(report.get("executive_summary") or ""),
        "",
        "## Visible Reasoning",
        "",
        str(report.get("visible_reasoning") or ""),
    ]
    dims = report.get("dimension_scores")
    if isinstance(dims, dict) and dims:
        lines.extend(["", "## Dimension Scores", ""])
        for name, payload in dims.items():
            if isinstance(payload, dict):
                score = payload.get("score_0_10", payload.get("score"))
                lines.append(f"- `{name}`: `{score}` — {payload.get('rationale') or ''}")
            else:
                lines.append(f"- `{name}`: `{payload}`")
    for key in ("strengths", "weaknesses", "critical_errors", "actionable_improvements"):
        values = report.get(key)
        if isinstance(values, list) and values:
            lines.extend(["", f"## {key.replace('_', ' ').title()}", ""])
            for value in values:
                lines.append(f"- {value}")
    lines.append("")
    return "\n".join(lines)


def _pairwise_markdown(report: dict[str, Any], mapping: dict[str, Any]) -> str:
    lines = [
        "# Pairwise Poster Judge Report",
        "",
        f"- Candidate A: `{mapping['A']['name']}`",
        f"- Candidate B: `{mapping['B']['name']}`",
        f"- Winner: `{report.get('winner')}`",
        f"- Confidence: `{report.get('judge_confidence')}`",
        "",
        "## Visible Reasoning",
        "",
        str(report.get("visible_reasoning") or ""),
    ]
    values = report.get("dimension_deltas")
    if isinstance(values, dict) and values:
        lines.extend(["", "## Dimension Deltas", ""])
        for key, value in values.items():
            lines.append(f"- `{key}`: {value}")
    lines.append("")
    return "\n".join(lines)


def _benchmark_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Poster Benchmark Report",
        "",
        f"- Version: `{report.get('version')}`",
        f"- Mode: `{report.get('mode')}`",
        f"- Model: `{report.get('model')}`",
        f"- Paper: `{report.get('paper')}`",
        "",
        "## Candidate Scores",
        "",
        "| Candidate | Score | Confidence |",
        "|---|---:|---:|",
    ]
    for item in report.get("candidates") or []:
        lines.append(
            f"| `{item.get('candidate')}` | {item.get('overall_score_0_100')} | {item.get('judge_confidence')} |"
        )
    if report.get("pairwise"):
        lines.extend(["", "## Pairwise Results", "", "| A | B | Winner |", "|---|---|---|"])
        for item in report.get("pairwise") or []:
            lines.append(f"| `{item.get('candidate_a')}` | `{item.get('candidate_b')}` | `{item.get('winner')}` |")
    lines.append("")
    return "\n".join(lines)


PAPER_BRIEF_SYSTEM_PROMPT = """You are an expert scientific-poster benchmark judge.
Your job is to read a paper and create a compact, source-grounded evaluation
brief for later visual poster judges. Do not evaluate any poster here.
Return JSON only."""

PAPER_BRIEF_USER_PROMPT = """Extract a benchmark-ready paper brief from the paper text.

Return a JSON object with these keys:
- paper_title
- one_sentence_thesis
- core_contributions: 3-8 bullets
- method_summary: 2-5 bullets
- key_results: 3-8 bullets, including numbers only if present in the paper
- important_figures_tables: 3-10 bullets naming what visual evidence matters
- limitations_or_caveats: 0-6 bullets
- must_cover_points: 5-12 bullets a high-quality poster should convey
- terminology_to_preserve: 5-15 terms
- evaluation_risks: 3-8 things a poster judge should watch for

Paper text:
```text
{paper_text}
```"""


POSTER_QUALITY_SYSTEM_PROMPT = """You are an expert academic-poster benchmark judge.
Evaluate the rendered poster image against the source-paper brief. You are not
part of the generation pipeline and you must not favor any product, template,
or known house style.

Use the visible poster image as the only artifact. Do not assume access to HTML,
DOM, editable layers, prompts, or hidden generation metadata. Judge what a human
conference attendee would see.

Important benchmark principles:
- Reward accurate paper coverage, scientific synthesis, readable information
  density, clear hierarchy, meaningful use of figures/tables, and professional
  academic-poster craft that looks intentionally authored by a human conference
  presenter, not merely clean generic design.
- Penalize hallucinated or unsupported claims, missing core paper arc, unreadable
  text, visible overlap/cropping, huge unused panel interiors, weak figure
  reading notes, screenshot walls with little synthesis, and rigid one-claim
  boxed-card layouts when they reduce scientific communication.
- For professional_aesthetics, penalize sparse report-page or slide-like layouts,
  generic AI-template polish, and palettes with too many colors or unrelated
  accent colors. A restrained, disciplined academic palette is better than a
  colorful but arbitrary design.
- Do not penalize a poster merely for using a different aesthetic than
  AutoDesign. Only penalize style when it harms readability, hierarchy,
  professionalism, or scientific communication.
- Write concise visible reasoning. Do not expose private chain-of-thought.

Return JSON only."""

POSTER_QUALITY_USER_PROMPT = """Evaluate candidate `{candidate_label}` for profile `{profile}`.

Source-paper brief:
```json
{paper_brief}
```

Return a JSON object with:
- candidate
- overall_score_0_100: integer or float
- executive_summary: 2-5 sentences
- visible_reasoning: concise explanation of the main judgment, grounded in
  visible poster evidence and the paper brief
- dimension_scores: object with exactly these keys. Each value is an object
  with score_0_10, rationale, visible_evidence, and improvement_if_low:
  - source_faithfulness
  - paper_coverage
  - information_density_and_synthesis
  - visual_evidence_use
  - basic_layout_integrity
  - layout_readability
  - professional_aesthetics
  - poster_specific_craft
- strengths: 3-8 bullets
- weaknesses: 3-8 bullets
- critical_errors: bullets for severe factual/layout/readability issues, or []
- actionable_improvements: 3-8 bullets
- judge_confidence: number in [0,1]

Score with this approximate weighting:
- source_faithfulness: 5
- paper_coverage: 5
- information_density_and_synthesis: 35
- visual_evidence_use: 10
- basic_layout_integrity: 20
- layout_readability: 15
- professional_aesthetics: 10
Use poster_specific_craft as an additional qualitative diagnostic, not a hidden
house-style preference."""


PAIRWISE_SYSTEM_PROMPT = """You are an expert academic-poster benchmark judge doing
a blind pairwise comparison. You will see Candidate A and Candidate B as rendered
poster images for the same paper. Do not infer which system made which poster.
Choose the poster that better communicates the paper to a conference audience.
Return JSON only with concise visible reasoning, not private chain-of-thought."""

PAIRWISE_USER_PROMPT_A = """Paper brief for both candidates, profile `{profile}`:
```json
{paper_brief}
```

This image is Candidate A. Inspect it for paper coverage, visual evidence,
information density, readability, hierarchy, and professional craft."""

PAIRWISE_USER_PROMPT_B = """This image is Candidate B.

Now compare Candidate A and Candidate B. Return JSON with:
- winner: "A", "B", or "tie"
- judge_confidence: number in [0,1]
- visible_reasoning: concise comparative rationale
- candidate_a_score_0_100
- candidate_b_score_0_100
- dimension_deltas: object describing which candidate wins each dimension:
  source_faithfulness, paper_coverage, information_density_and_synthesis,
  visual_evidence_use, basic_layout_integrity, layout_readability,
  professional_aesthetics
- decisive_evidence: 3-8 bullets
- caveats: 0-5 bullets
"""
