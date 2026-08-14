"""Diagnostics for iterative paper project page improvement.

The generator can make a paper page look plausible in one pass, but
reference-quality project pages need a repeatable review loop. This module
turns generated HTML pages into a compact evidence packet for reviewer agents:
panel structure, material quality, text-risk markers, layout blockers, and the
next system patch brief.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import Counter
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

try:  # Pillow is a project dependency, but keep diagnostics soft-failing.
    from PIL import Image
except Exception:  # pragma: no cover - defensive fallback for partial envs
    Image = None  # type: ignore[assignment]


DEFAULT_REFERENCE_URLS = [
    "https://hongcanguo.github.io/Cola-DLM/",
    "https://cambrian-mllm.github.io/cambrian-p/",
    "https://cambrian-mllm.github.io/cambrian-s/",
    "https://vision-x-nyu.github.io/test-set-training/",
]

INTERNAL_COPY_TERMS = (
    "source-backed",
    "ingested",
    "fabricated",
    "reconstructed",
    "rendered_layers",
    "unavailable",
    "placeholder",
)

PANEL_ROLE_KEYWORDS = {
    "hero": ("hero", "first_viewport", "paper_title", "authors"),
    "resources": ("resource", "resources", "links", "github", "hugging", "arxiv", "code"),
    "abstract": ("abstract", "overview"),
    "framework": ("framework", "method", "architecture", "pipeline", "model", "system"),
    "findings": ("finding", "findings", "takeaway", "result"),
    "demo": ("demo", "demos", "sample", "samples", "gallery", "qualitative", "example"),
    "benchmark": ("benchmark", "benchmarks", "ablation", "leaderboard", "evaluation", "experiment"),
    "analysis": ("analysis", "discussion", "reflection", "limitation"),
    "citation": ("citation", "bibtex", "cite", "footer", "license"),
}

TEXT_ROLE_KEYWORDS = {
    "resources": ("resources", "links", "github", "hugging", "arxiv", "code", "model weights"),
    "abstract": ("abstract", "overview"),
    "framework": ("framework", "method", "architecture", "pipeline", "model", "system"),
    "findings": ("finding", "findings", "takeaway", "result"),
    "demo": ("demo", "demos", "sample", "samples", "gallery", "qualitative", "example"),
    "benchmark": ("benchmark", "benchmarks", "ablation", "leaderboard", "evaluation", "experiment"),
    "analysis": ("analysis", "discussion", "reflection", "limitation"),
    "citation": ("citation", "bibtex", "@article", "@inproceedings", "license"),
}


class _PaperPageParser(HTMLParser):
    def __init__(self, *, base_dir: Path):
        super().__init__(convert_charrefs=True)
        self.base_dir = base_dir
        self.sections: list[dict[str, Any]] = []
        self.images: list[dict[str, Any]] = []
        self.links: list[dict[str, Any]] = []
        self.tables: list[dict[str, Any]] = []
        self._section_stack: list[dict[str, Any]] = []
        self._link_stack: list[dict[str, Any]] = []
        self._table_stack: list[dict[str, Any]] = []
        self._row_stack: list[list[str]] = []
        self._cell_stack: list[list[str]] = []
        self._figure_stack: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key.lower(): value or "" for key, value in attrs_list}
        tag = tag.lower()
        if tag in {"section", "footer"}:
            section = {
                "index": len(self.sections),
                "section_id": attrs.get("id")
                or attrs.get("data-frame-id")
                or attrs.get("data-layer-id")
                or f"section_{len(self.sections) + 1:02d}",
                "variant": attrs.get("data-section-variant")
                or attrs.get("data-role")
                or attrs.get("role")
                or "",
                "class": attrs.get("class", ""),
                "text_chunks": [],
                "images": [],
                "links": [],
                "tables": [],
            }
            self.sections.append(section)
            self._section_stack.append(section)
        elif tag == "figure":
            self._figure_stack.append(attrs)
        elif tag == "img":
            figure_attrs = self._figure_stack[-1] if self._figure_stack else {}
            image = {
                "src": attrs.get("src", ""),
                "alt": attrs.get("alt", ""),
                "layer_id": attrs.get("data-layer-id")
                or figure_attrs.get("data-layer-id")
                or attrs.get("id")
                or "",
                "source_id": attrs.get("data-source-id")
                or attrs.get("data-paper-source-id")
                or figure_attrs.get("data-source-id")
                or figure_attrs.get("data-paper-source-id")
                or "",
                "source_page": attrs.get("data-source-page") or figure_attrs.get("data-source-page") or "",
                "source_bbox": attrs.get("data-source-bbox") or figure_attrs.get("data-source-bbox") or "",
                "visual_role": attrs.get("data-visual-role") or figure_attrs.get("data-visual-role") or "",
                "material_score": attrs.get("data-material-score") or figure_attrs.get("data-material-score") or "",
                "edge_white_ratio": attrs.get("data-edge-white-ratio") or figure_attrs.get("data-edge-white-ratio") or "",
                "material_warnings": attrs.get("data-material-warnings") or figure_attrs.get("data-material-warnings") or "",
                "generated_kind": attrs.get("data-generated-kind") or figure_attrs.get("data-generated-kind") or "",
                "evidence_role": attrs.get("data-evidence-role") or figure_attrs.get("data-evidence-role") or "",
                "section_index": self._section_stack[-1]["index"] if self._section_stack else None,
            }
            self.images.append(image)
            if self._section_stack:
                self._section_stack[-1]["images"].append(image)
        elif tag == "a":
            self._link_stack.append({"href": attrs.get("href", ""), "text_chunks": []})
        elif tag == "table":
            figure_attrs = self._figure_stack[-1] if self._figure_stack else {}
            table = {
                "rows": [],
                "col_count": attrs.get("data-col-count") or figure_attrs.get("data-col-count") or "",
                "table_mode": attrs.get("data-table-mode") or figure_attrs.get("data-table-mode") or "",
                "overflow_mode": attrs.get("data-overflow-mode") or figure_attrs.get("data-overflow-mode") or "",
                "source_id": attrs.get("data-source-id") or figure_attrs.get("data-source-id") or "",
                "source_page": attrs.get("data-source-page") or figure_attrs.get("data-source-page") or "",
                "source_bbox": attrs.get("data-source-bbox") or figure_attrs.get("data-source-bbox") or "",
                "section_index": self._section_stack[-1]["index"] if self._section_stack else None,
            }
            self.tables.append(table)
            if self._section_stack:
                self._section_stack[-1]["tables"].append(table)
            self._table_stack.append(table)
        elif tag == "tr" and self._table_stack:
            self._row_stack.append([])
        elif tag in {"td", "th"} and self._table_stack:
            self._cell_stack.append([])

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data or "").strip()
        if not text:
            return
        if self._section_stack:
            self._section_stack[-1]["text_chunks"].append(text)
        if self._link_stack:
            self._link_stack[-1]["text_chunks"].append(text)
        if self._cell_stack:
            self._cell_stack[-1].append(text)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"section", "footer"} and self._section_stack:
            self._section_stack.pop()
        elif tag == "figure" and self._figure_stack:
            self._figure_stack.pop()
        elif tag == "a" and self._link_stack:
            link = self._link_stack.pop()
            entry = {
                "href": link.get("href", ""),
                "text": " ".join(link.get("text_chunks") or []).strip(),
                "section_index": self._section_stack[-1]["index"] if self._section_stack else None,
            }
            self.links.append(entry)
            if self._section_stack:
                self._section_stack[-1]["links"].append(entry)
        elif tag in {"td", "th"} and self._cell_stack and self._row_stack:
            self._row_stack[-1].append(" ".join(self._cell_stack.pop()).strip())
        elif tag == "tr" and self._row_stack and self._table_stack:
            row = self._row_stack.pop()
            if any(cell for cell in row):
                self._table_stack[-1]["rows"].append(row)
        elif tag == "table" and self._table_stack:
            self._table_stack.pop()


def build_paper_page_iteration_review(
    pages: list[Path | str],
    *,
    references: list[str] | None = None,
    reference_manifest: Path | str | None = None,
    label: str = "paper_page_iteration",
) -> dict[str, Any]:
    """Build an agent-friendly review packet for a generated page batch."""

    page_reports = [diagnose_paper_page(Path(page)) for page in pages]
    reference_summary = _load_reference_summary(reference_manifest, references or DEFAULT_REFERENCE_URLS)
    issue_rollup = _issue_rollup(page_reports)
    report: dict[str, Any] = {
        "kind": "paper_page_iteration_review",
        "version": 1,
        "label": label,
        "references": reference_summary,
        "pages": page_reports,
        "issue_rollup": issue_rollup,
        "sub_agents": _sub_agent_briefs(page_reports, issue_rollup),
        "recommended_tools": _recommended_tools(issue_rollup),
        "system_patch_brief": _system_patch_brief(issue_rollup),
    }
    return report


def diagnose_paper_page(path: Path) -> dict[str, Any]:
    html = path.read_text(encoding="utf-8", errors="replace")
    parser = _PaperPageParser(base_dir=path.parent)
    parser.feed(html)
    title = _title_from_html(html)
    image_metrics = [_image_metrics(image, path.parent) for image in parser.images]
    duplicate_hashes = {
        value
        for value, count in Counter(
            metric.get("sha1") for metric in image_metrics if metric.get("sha1")
        ).items()
        if count > 1
    }
    for metric in image_metrics:
        if metric.get("sha1") in duplicate_hashes:
            metric.setdefault("warnings", []).append("duplicate_exact_image")

    sections = [
        _summarize_section(section, image_metrics)
        for section in parser.sections
    ]
    issues = _page_issues(
        path=path,
        html=html,
        sections=sections,
        links=parser.links,
        tables=parser.tables,
        image_metrics=image_metrics,
    )
    return {
        "path": str(path),
        "title": title,
        "structure": {
            "sections": len(sections),
            "links": len(parser.links),
            "valid_links": len([link for link in parser.links if _valid_href(link.get("href", ""))]),
            "images": len(parser.images),
            "unique_image_hashes": len({
                metric.get("sha1") for metric in image_metrics if metric.get("sha1")
            }),
            "tables": len(parser.tables),
            "words": len(_words(_visible_text(html))),
        },
        "sections": sections,
        "image_metrics": image_metrics,
        "issues": issues,
    }


def write_iteration_review(report: dict[str, Any], out_dir: Path | str) -> dict[str, str]:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    review_json = out_path / "iteration_review.json"
    review_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["iteration_review"] = str(review_json)

    brief = out_path / "iteration_brief.md"
    brief.write_text(_review_markdown(report), encoding="utf-8")
    paths["iteration_brief"] = str(brief)

    patch_brief = out_path / "system_patch_brief.md"
    patch_brief.write_text(_patch_brief_markdown(report), encoding="utf-8")
    paths["system_patch_brief"] = str(patch_brief)

    subagent_dir = out_path / "subagent_briefs"
    subagent_dir.mkdir(exist_ok=True)
    for agent in report.get("sub_agents") or []:
        agent_id = str(agent.get("id") or "agent")
        target = subagent_dir / f"{agent_id}.md"
        target.write_text(_agent_brief_markdown(agent), encoding="utf-8")
    paths["subagent_briefs"] = str(subagent_dir)
    return paths


def _summarize_section(section: dict[str, Any], image_metrics: list[dict[str, Any]]) -> dict[str, Any]:
    text = " ".join(section.get("text_chunks") or [])
    role = _classify_panel(section, text)
    table_cols = [_table_col_count(table) for table in section.get("tables") or []]
    section_image_metrics = [
        metric for metric in image_metrics
        if metric.get("section_index") == section.get("index")
    ]
    warnings: list[str] = []
    if role in {"demo", "findings", "benchmark", "framework"} and not section.get("images") and not section.get("tables"):
        warnings.append("evidence_panel_without_visual_anchor")
    if role == "demo" and len(section.get("images") or []) < 3:
        warnings.append("sample_gallery_underbuilt")
    if any(
        _table_col_count(table) > 6 and _table_overflow_mode(table) not in {"local_scroll", "local-scroll"}
        for table in section.get("tables") or []
    ):
        warnings.append("wide_table_needs_scroll_or_summary")
    if any(term in text.lower() for term in INTERNAL_COPY_TERMS):
        warnings.append("internal_harness_language")
    if section_image_metrics and all("high_edge_whitespace" in metric.get("warnings", []) for metric in section_image_metrics):
        warnings.append("all_images_have_large_white_edges")
    return {
        "section_id": section.get("section_id"),
        "role": role,
        "variant": section.get("variant"),
        "word_count": len(_words(text)),
        "image_count": len(section.get("images") or []),
        "table_count": len(section.get("tables") or []),
        "link_count": len(section.get("links") or []),
        "max_table_cols": max(table_cols or [0]),
        "warnings": warnings,
        "text_excerpt": _shorten(text, 220),
    }


def _page_issues(
    *,
    path: Path,
    html: str,
    sections: list[dict[str, Any]],
    links: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    image_metrics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    roles = Counter(str(section.get("role") or "") for section in sections)
    valid_links = [link for link in links if _valid_href(link.get("href", ""))]
    all_text = _visible_text(html).lower()
    title = _title_from_html(html).lower()

    if len(sections) < 7:
        issues.append(_issue(
            "P1",
            "too_few_project_panels",
            "Page has too few major panels for a reference-style paper project page.",
            "Add a panel plan before content fill: hero, resources, abstract, method, demos/findings, benchmarks/analysis, citation.",
            str(path),
        ))
    for required_role in ("resources", "abstract", "framework", "benchmark", "citation"):
        if roles[required_role] == 0:
            issues.append(_issue(
                "P1",
                f"missing_{required_role}_panel",
                f"Page lacks a recognizable {required_role} panel.",
                f"Add or relabel a {required_role} panel in the page outline.",
                str(path),
            ))
    if roles["demo"] == 0:
        issues.append(_issue(
            "P2",
            "missing_demo_or_sample_panel",
            "No demo/sample/qualitative gallery panel was detected.",
            "Add a real sample gallery when the paper has qualitative examples; otherwise merge into findings.",
            str(path),
        ))
    if len(valid_links) < 2:
        issues.append(_issue(
            "P1",
            "sparse_resource_links",
            "Project page exposes too few verified resource links.",
            "Run resource recall and search; include real arXiv/PDF/code/model/blog/demo links only when verified.",
            str(path),
        ))

    internal_terms = [term for term in INTERNAL_COPY_TERMS if term in all_text or term in title]
    if internal_terms:
        issues.append(_issue(
            "P1",
            "internal_harness_language",
            "Final page copy contains internal pipeline/reviewer terms.",
            "Rewrite copy as public project-page prose; keep provenance in diagnostics, not user-facing text.",
            str(path),
            terms=internal_terms,
        ))
    if "independent paper project page" in title:
        issues.append(_issue(
            "P1",
            "generator_self_description_in_title",
            "HTML title still describes the generator task instead of the paper.",
            "Use the paper title and short project tagline only.",
            str(path),
        ))

    wide_tables: list[dict[str, Any]] = []
    wide_summary_candidates: list[dict[str, Any]] = []
    for table in tables:
        cols = _table_col_count(table)
        table_mode = str(table.get("table_mode") or "").strip()
        overflow_mode = _table_overflow_mode(table)
        record = {
            "section_index": table.get("section_index"),
            "cols": cols,
            "table_mode": table_mode,
            "overflow_mode": overflow_mode,
        }
        if cols > 6 and overflow_mode not in {"local_scroll", "local-scroll"}:
            wide_tables.append(record)
        elif cols > 10 and table_mode != "summary_plus_full_scroll":
            wide_summary_candidates.append(record)
    if wide_tables:
        issues.append(_issue(
            "P0",
            "wide_table_layout_risk",
            "A wide native table can force document-level horizontal overflow.",
            "Wrap wide tables in local overflow, split into headline table plus full table, or convert key rows to a compact comparison table.",
            str(path),
            tables=wide_tables[:6],
        ))
    if wide_summary_candidates:
        issues.append(_issue(
            "P1",
            "wide_table_summary_recommended",
            "A very wide table uses local scroll but lacks an explicit summary/full-table contract.",
            "Promote headline rows or metrics to cards and keep the full table in a local scroll region.",
            str(path),
            tables=wide_summary_candidates[:6],
        ))

    duplicate_count = len([
        metric for metric in image_metrics
        if "duplicate_exact_image" in metric.get("warnings", [])
    ])
    if duplicate_count:
        issues.append(_issue(
            "P1",
            "duplicate_source_visuals",
            "The same source image appears multiple times.",
            "Deduplicate repeated figures and assign each selected visual a panel role.",
            str(path),
            duplicate_images=duplicate_count,
        ))
    weak_provenance = [
        metric for metric in image_metrics
        if metric.get("source_kind") in {"data_uri", "local"} and not metric.get("source_id")
        and str(metric.get("evidence_role") or "").lower() != "non_evidence"
        and not metric.get("generated_kind")
    ]
    if weak_provenance:
        issues.append(_issue(
            "P0",
            "weak_material_provenance",
            "Rendered images lack a sidecar source id/page/bbox/caption trail.",
            "Persist source_id, page, bbox, caption, role, crop score, and image hash for every paper visual.",
            str(path),
            images=min(len(weak_provenance), 12),
        ))
    high_whitespace = [
        metric for metric in image_metrics
        if "high_edge_whitespace" in metric.get("warnings", [])
    ]
    if high_whitespace:
        issues.append(_issue(
            "P1",
            "source_crop_whitespace",
            "Several paper visuals include large white margins.",
            "Add crop scoring and trim paper figures before layout, while preserving native captions outside the image.",
            str(path),
            images=len(high_whitespace),
        ))
    for section in sections:
        for warning in section.get("warnings") or []:
            if warning == "sample_gallery_underbuilt":
                issues.append(_issue(
                    "P1",
                    "sample_gallery_underbuilt",
                    "A demo/sample panel is present but lacks enough gallery items.",
                    "Use a dedicated sample selector or merge weak sample panels into findings.",
                    f"{path}#{section.get('section_id')}",
                ))
                break
    return issues


def _image_metrics(image: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    src = str(image.get("src") or "")
    result: dict[str, Any] = {
        "src_excerpt": _shorten(src, 96),
        "alt": image.get("alt", ""),
        "layer_id": image.get("layer_id", ""),
        "source_id": image.get("source_id", ""),
        "source_page": image.get("source_page", ""),
        "source_bbox": image.get("source_bbox", ""),
        "visual_role": image.get("visual_role", ""),
        "material_score": image.get("material_score", ""),
        "edge_white_ratio_dom": image.get("edge_white_ratio", ""),
        "material_warnings_dom": image.get("material_warnings", ""),
        "generated_kind": image.get("generated_kind", ""),
        "evidence_role": image.get("evidence_role", ""),
        "section_index": image.get("section_index"),
        "source_kind": "remote" if src.startswith(("http://", "https://")) else "local",
        "warnings": [],
    }
    payload = _load_image_payload(src, base_dir)
    if not payload:
        result["warnings"].append("image_payload_unavailable")
        return result
    data, source_kind = payload
    result["source_kind"] = source_kind
    result["bytes"] = len(data)
    result["sha1"] = hashlib.sha1(data).hexdigest()[:16]
    if Image is None:
        return result
    try:
        with Image.open(BytesIO(data)) as img:
            rgb = img.convert("RGB")
            result["width"] = rgb.width
            result["height"] = rgb.height
            if min(rgb.size) < 180:
                result["warnings"].append("low_resolution_visual")
            whitespace_ratio, edge_white_ratio = _white_ratios(rgb)
            result["white_ratio"] = round(whitespace_ratio, 4)
            result["edge_white_ratio"] = round(edge_white_ratio, 4)
            if edge_white_ratio >= 0.82:
                result["warnings"].append("high_edge_whitespace")
            if whitespace_ratio >= 0.82:
                result["warnings"].append("mostly_white_visual")
            aspect = rgb.width / max(1, rgb.height)
            result["aspect_ratio"] = round(aspect, 3)
            if aspect < 0.22 or aspect > 6.0:
                result["warnings"].append("extreme_aspect_ratio")
    except Exception as exc:  # noqa: BLE001 - diagnostics should keep going
        result["warnings"].append(f"image_decode_failed:{type(exc).__name__}")
    return result


def _load_image_payload(src: str, base_dir: Path) -> tuple[bytes, str] | None:
    if not src:
        return None
    if src.startswith("data:image/"):
        match = re.match(r"data:image/[^;]+;base64,(.+)", src, re.DOTALL)
        if not match:
            return None
        try:
            return base64.b64decode(match.group(1)), "data_uri"
        except Exception:
            return None
    if src.startswith(("http://", "https://")):
        return None
    parsed = urlparse(src)
    raw_path = unquote(parsed.path or src)
    candidate = (base_dir / raw_path).resolve()
    try:
        if candidate.exists() and candidate.is_file():
            return candidate.read_bytes(), "local"
    except OSError:
        return None
    return None


def _white_ratios(img: Any) -> tuple[float, float]:
    thumb = img.copy()
    thumb.thumbnail((320, 320))
    pixels = list(thumb.getdata())
    if not pixels:
        return 0.0, 0.0
    white = sum(1 for pixel in pixels if min(pixel) >= 245)
    edge = max(2, int(min(thumb.size) * 0.08))
    edge_pixels = []
    width, height = thumb.size
    for y in range(height):
        for x in range(width):
            if x < edge or y < edge or x >= width - edge or y >= height - edge:
                edge_pixels.append(thumb.getpixel((x, y)))
    edge_white = sum(1 for pixel in edge_pixels if min(pixel) >= 245)
    return white / len(pixels), edge_white / max(1, len(edge_pixels))


def _issue(
    severity: str,
    issue_id: str,
    message: str,
    recommendation: str,
    target: str,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "severity": severity,
        "id": issue_id,
        "message": message,
        "recommendation": recommendation,
        "target": target,
    }
    payload.update(extra)
    return payload


def _issue_rollup(page_reports: list[dict[str, Any]]) -> dict[str, Any]:
    issues = [
        issue
        for page in page_reports
        for issue in page.get("issues") or []
    ]
    by_id = Counter(str(issue.get("id") or "") for issue in issues)
    by_severity = Counter(str(issue.get("severity") or "") for issue in issues)
    return {
        "total": len(issues),
        "by_id": dict(by_id),
        "by_severity": dict(by_severity),
        "top_issue_ids": [issue_id for issue_id, _ in by_id.most_common(12)],
    }


def _sub_agent_briefs(page_reports: list[dict[str, Any]], issue_rollup: dict[str, Any]) -> list[dict[str, Any]]:
    input_files = [page.get("path") for page in page_reports]
    return [
        {
            "id": "aesthetic_reviewer",
            "focus": "Panel-level art direction, visual identity, visual hierarchy, and reference similarity.",
            "input_files": input_files,
            "questions": [
                "Which panels still look like PDF content pasted into a page?",
                "Where is whitespace intentional versus accidental?",
                "Which paper should use a different art direction profile?",
            ],
            "output_schema": "JSON: {page, panel, score_1_5, issue, reference_gap, repair_direction}",
        },
        {
            "id": "text_evidence_reviewer",
            "focus": "Claim accuracy, table-cell trust, resource recall, and public-facing copy quality.",
            "input_files": input_files,
            "questions": [
                "Which claims/numbers need PDF span or table provenance?",
                "Which copy exposes internal harness language?",
                "Which resources are missing or overclaimed?",
            ],
            "output_schema": "JSON: {page, claim_or_cell, risk, source_needed, rewrite}",
        },
        {
            "id": "layout_reviewer",
            "focus": "Panel rhythm, desktop overflow, grid alignment, table width, and demo-gallery layout.",
            "input_files": input_files,
            "questions": [
                "Which panels are too short, too long, or visually unbalanced?",
                "Which tables/code/figures can force document-level desktop overflow?",
                "Where should a panel be split, merged, or rewritten?",
            ],
            "output_schema": "JSON: {page, panel, blocker, viewport, repair}",
        },
        {
            "id": "material_reviewer",
            "focus": "Paper figure/table/sample quality, crop, duplication, provenance, and reconstruction decisions.",
            "input_files": input_files,
            "questions": [
                "Which images need crop/reject/reconstruct/native table/native code treatment?",
                "Which sample/demo assets are too weak for a standalone panel?",
                "Which figure roles are missing from the page?",
            ],
            "output_schema": "JSON: {page, asset, action, reason, target_panel}",
        },
        {
            "id": "iteration_director",
            "focus": "Aggregate reviewer reports into the next harness patch and tool/agent additions.",
            "input_files": input_files,
            "issue_rollup": issue_rollup,
            "questions": [
                "Which P0 should be implemented in the next pipeline iteration?",
                "Which agent/tool should be added now versus later?",
                "What metric proves the next iteration improved?",
            ],
            "output_schema": "JSON: {priority, patch, agent_or_tool, success_metric, dependency}",
        },
    ]


def _recommended_tools(issue_rollup: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = issue_rollup.get("by_id") or {}

    def has(issue_id: str) -> bool:
        return bool(by_id.get(issue_id))

    return [
        {
            "name": "ReferenceVisualJudge",
            "status": "add_now",
            "purpose": "Compare section screenshots against reference pages and score panel identity, hierarchy, evidence density, and polish.",
            "evidence_boundary": "Uses screenshots and DOM stats only; does not edit scientific content.",
        },
        {
            "name": "PanelPlanner",
            "status": "add_now",
            "purpose": "Plan Hero, Resources, Abstract, Method, Demo, Benchmark, Analysis, Citation panels before content fill.",
            "evidence_boundary": "Chooses section intent and template; source facts still come from PDF/resource manifests.",
        },
        {
            "name": "MaterialCritic",
            "status": "add_now" if has("weak_material_provenance") or has("source_crop_whitespace") else "monitor",
            "purpose": "Score every source visual for crop quality, duplication, role, sample suitability, and reconstruct/native fallback.",
            "evidence_boundary": "Outputs keep/crop/reject/reconstruct decisions without changing paper claims.",
        },
        {
            "name": "FigureCropOptimizer",
            "status": "add_now" if has("source_crop_whitespace") or has("weak_material_provenance") else "monitor",
            "purpose": "Persist source sidecars, trim whitespace, dedupe hashes, and preserve high-DPI paper figures.",
            "evidence_boundary": "Only improves extraction and display of real paper visuals.",
        },
        {
            "name": "NativeTableValidator",
            "status": "add_now" if has("wide_table_layout_risk") else "monitor",
            "purpose": "Validate table extraction with PDF word coordinates and split wide tables into summary/full views.",
            "evidence_boundary": "Falls back to source screenshot or warning when cell provenance is uncertain.",
        },
        {
            "name": "ResourceRecallAuditor",
            "status": "add_now" if has("sparse_resource_links") or has("missing_resources_panel") else "monitor",
            "purpose": "Compare PDF URLs, arXiv metadata, GitHub/Hugging Face search, and final manifest coverage.",
            "evidence_boundary": "Never fabricates unavailable links; records confidence and HTTP/title evidence.",
        },
        {
            "name": "GenerativeVisualTool",
            "status": "pilot_later",
            "purpose": "Generate non-evidence backgrounds, section icons, link icons, and decorative frames to improve art direction.",
            "evidence_boundary": "Generated visuals must be labeled decorative/non-evidence and may not carry paper numbers, claims, benchmark plots, or scientific diagrams.",
        },
    ]


def _system_patch_brief(issue_rollup: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = issue_rollup.get("by_id") or {}
    patches: list[dict[str, Any]] = [
        {
            "priority": "P0",
            "patch": "Make paper-page batches produce iteration_review.json, sub-agent briefs, and a system_patch_brief after every generation run.",
            "success_metric": "Every batch has five reviewer briefs and top issues are referenced by the next patch.",
        },
        {
            "priority": "P0",
            "patch": "Add a panel contract and template selector before page fill.",
            "success_metric": "Pages have 7-10 named panels and no evidence panel is text-only unless explicitly analytical.",
        },
    ]
    if by_id.get("weak_material_provenance") or by_id.get("source_crop_whitespace"):
        patches.append({
            "priority": "P0",
            "patch": "Persist paper visual provenance sidecars and score crop/whitespace/duplicate quality before layout.",
            "success_metric": "Every displayed source visual has source_id/page/bbox/caption/role/hash/crop_score; high edge-whitespace images are trimmed or rejected.",
        })
    if by_id.get("wide_table_layout_risk"):
        patches.append({
            "priority": "P0",
            "patch": "Introduce table width gates and summary/full-table split for wide benchmark tables.",
            "success_metric": "390px viewport has no document-level overflow; cols > 6 render in local scroll or summary layout.",
        })
    if by_id.get("internal_harness_language"):
        patches.append({
            "priority": "P1",
            "patch": "Add a public project-page copy rewrite pass after factuality verification.",
            "success_metric": "No final HTML title/body contains internal harness terms like source-backed, ingested, fabricated, or reconstructed.",
        })
    if by_id.get("sparse_resource_links") or by_id.get("missing_resources_panel"):
        patches.append({
            "priority": "P1",
            "patch": "Add resource recall audit before rendering resource chips.",
            "success_metric": "All real paper/project URLs found in PDF text and public metadata appear in the resource manifest with confidence.",
        })
    patches.append({
        "priority": "P1",
        "patch": "Pilot generative visuals only for decorative backgrounds/icons/frames after evidence panels are stable.",
        "success_metric": "Generated assets are tagged decorative and never replace paper figures, benchmark numbers, or scientific diagrams.",
    })
    return patches


def _load_reference_summary(reference_manifest: Path | str | None, references: list[str]) -> dict[str, Any]:
    summary: dict[str, Any] = {"urls": references, "manifest": None, "structure_targets": {}}
    if not reference_manifest:
        return summary
    path = Path(reference_manifest)
    if not path.exists():
        summary["manifest_error"] = f"missing: {path}"
        return summary
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        summary["manifest_error"] = f"invalid json: {exc}"
        return summary
    refs = data.get("references") if isinstance(data, dict) else []
    structures = [ref.get("structure") for ref in refs if isinstance(ref, dict) and isinstance(ref.get("structure"), dict)]
    if structures:
        summary["structure_targets"] = {
            "median_sections": _median([s.get("sections", 0) for s in structures]),
            "median_images": _median([s.get("images", 0) for s in structures]),
            "median_tables": _median([s.get("tables", 0) for s in structures]),
            "median_links": _median([s.get("links", 0) for s in structures]),
        }
    summary["manifest"] = str(path)
    return summary


def _classify_panel(section: dict[str, Any], text: str) -> str:
    meta_blob = " ".join([
        str(section.get("section_id") or ""),
        str(section.get("variant") or ""),
        str(section.get("class") or ""),
    ]).lower()
    for role, keys in PANEL_ROLE_KEYWORDS.items():
        if any(key in meta_blob for key in keys):
            return role
    text_blob = text[:500].lower()
    for role, keys in TEXT_ROLE_KEYWORDS.items():
        if any(key in text_blob for key in keys):
            return role
    return "content"


def _valid_href(href: str) -> bool:
    value = str(href or "").strip().lower()
    return bool(value) and value not in {"#", "todo", "tbd"} and not value.startswith("javascript:")


def _table_col_count(table: dict[str, Any]) -> int:
    declared = _safe_int(table.get("col_count"), 0)
    return max(declared, _max_cols(table.get("rows") or []))


def _table_overflow_mode(table: dict[str, Any]) -> str:
    mode = str(table.get("overflow_mode") or "").strip().lower().replace("-", "_")
    if mode:
        return mode
    table_mode = str(table.get("table_mode") or "").strip().lower()
    if table_mode in {"local_scroll", "summary_plus_full_scroll"}:
        return "local_scroll"
    return "standard"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _max_cols(rows: list[list[str]]) -> int:
    return max((len(row) for row in rows), default=0)


def _words(text: str) -> list[str]:
    return re.findall(r"\b[\w.-]+\b", text or "")


def _visible_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _title_from_html(html: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def _shorten(text: str, limit: int) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    return clean if len(clean) <= limit else clean[: max(0, limit - 1)].rstrip() + "…"


def _median(values: list[Any]) -> float:
    nums = sorted(float(value or 0) for value in values)
    if not nums:
        return 0.0
    mid = len(nums) // 2
    if len(nums) % 2:
        return nums[mid]
    return (nums[mid - 1] + nums[mid]) / 2.0


def _review_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Paper Page Iteration Review: {report.get('label')}",
        "",
        "## Issue Rollup",
    ]
    rollup = report.get("issue_rollup") or {}
    lines.append(f"- Total issues: {rollup.get('total', 0)}")
    lines.append(f"- By severity: `{json.dumps(rollup.get('by_severity') or {}, ensure_ascii=False)}`")
    lines.append(f"- Top issue ids: `{', '.join(rollup.get('top_issue_ids') or [])}`")
    lines.extend(["", "## Pages"])
    for page in report.get("pages") or []:
        structure = page.get("structure") or {}
        lines.append(f"- `{page.get('path')}`: {structure.get('sections', 0)} panels, "
                     f"{structure.get('images', 0)} images, {structure.get('tables', 0)} tables, "
                     f"{structure.get('valid_links', 0)} valid links")
        for issue in (page.get("issues") or [])[:6]:
            lines.append(f"  - {issue.get('severity')} `{issue.get('id')}`: {issue.get('message')}")
    lines.extend(["", "## Next Patch Brief"])
    for patch in report.get("system_patch_brief") or []:
        lines.append(f"- {patch.get('priority')} {patch.get('patch')} Success: {patch.get('success_metric')}")
    return "\n".join(lines) + "\n"


def _patch_brief_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# System Patch Brief",
        "",
        "Use this as the input to the next paper-page harness iteration.",
        "",
    ]
    for patch in report.get("system_patch_brief") or []:
        lines.append(f"## {patch.get('priority')}")
        lines.append(str(patch.get("patch") or ""))
        lines.append("")
        lines.append(f"Success metric: {patch.get('success_metric')}")
        lines.append("")
    lines.append("## Tool Boundary")
    for tool in report.get("recommended_tools") or []:
        lines.append(f"- `{tool.get('name')}` ({tool.get('status')}): {tool.get('purpose')} Boundary: {tool.get('evidence_boundary')}")
    return "\n".join(lines) + "\n"


def _agent_brief_markdown(agent: dict[str, Any]) -> str:
    lines = [
        f"# {agent.get('id')}",
        "",
        str(agent.get("focus") or ""),
        "",
        "## Inputs",
    ]
    for item in agent.get("input_files") or []:
        lines.append(f"- `{item}`")
    lines.extend(["", "## Questions"])
    for question in agent.get("questions") or []:
        lines.append(f"- {question}")
    lines.extend(["", "## Output Schema", "", f"`{agent.get('output_schema')}`", ""])
    return "\n".join(lines)
