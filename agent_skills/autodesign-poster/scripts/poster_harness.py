#!/usr/bin/env python3
"""Standalone paper-to-poster harness for the AutoDesign Poster Skill."""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _portable as core  # noqa: E402
import setup_browser  # noqa: E402


FORMAT_VERSION = 1
RELEASE_VERSION = "0.1.0"
DEFAULT_PRESET = "cvpr-landscape"
DEFAULT_MAX_ATTEMPTS = 4
SUPPORTED_IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
PRESETS: dict[str, dict[str, dict[str, float | int]]] = {
    "cvpr-landscape": {
        "canvas": {"width_px": 3072, "height_px": 1536},
        "print": {"width_mm": 2133.6, "height_mm": 1066.8},
    },
    "a0-landscape": {
        "canvas": {"width_px": 3366, "height_px": 2378},
        "print": {"width_mm": 1189.0, "height_mm": 841.0},
    },
    "a0-portrait": {
        "canvas": {"width_px": 2378, "height_px": 3366},
        "print": {"width_mm": 841.0, "height_mm": 1189.0},
    },
    "36x48-landscape": {
        "canvas": {"width_px": 3200, "height_px": 2400},
        "print": {"width_mm": 1219.2, "height_mm": 914.4},
    },
    "36x48-portrait": {
        "canvas": {"width_px": 2400, "height_px": 3200},
        "print": {"width_mm": 914.4, "height_mm": 1219.2},
    },
}
REVIEW_RUBRIC: dict[str, Any] = {
    "format_version": FORMAT_VERSION,
    "artifact_type": "poster",
    "dimensions": [
        "poster_impact",
        "information_architecture",
        "evidence_use",
        "human_effort_saved",
        "typography_craft",
        "originality_anti_template",
        "editability_export",
    ],
    "score_scale": {"minimum": 1, "maximum": 5, "pass_average": 3.75},
    "pass_requires_zero_blockers": True,
}
_PLAN_KEYS = {
    "format_version",
    "artifact_type",
    "preset",
    "canvas",
    "print",
    "narrative",
    "visual_allocations",
    "style_reference_ids",
    "max_attempts",
}
_ARC_GROUPS = {
    "problem": {"context", "introduction", "motivation", "problem"},
    "method": {"approach", "architecture", "method", "system"},
    "evidence": {"analysis", "evidence", "evaluation", "results"},
    "takeaway": {"conclusion", "discussion", "limitations", "takeaway"},
}
_UNSAFE_TAGS = {"base", "canvas", "embed", "iframe", "link", "object", "script"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_URL_ATTRS = {"action", "formaction", "href", "poster", "src"}
_CSS_URL_RE = re.compile(r"(?is)url\(\s*(['\"]?)(.*?)\1\s*\)")
_PAGE_SIZE_RE = re.compile(
    r"(?is)@page\s*\{[^{}]*?\bsize\s*:\s*"
    r"(?P<width>[0-9]+(?:\.[0-9]+)?)mm\s+"
    r"(?P<height>[0-9]+(?:\.[0-9]+)?)mm\s*;?[^{}]*\}"
)
_FONT_RULE_RE = re.compile(r"(?s)(?P<selectors>[^{}]+)\{(?P<body>[^{}]*)\}")
_FONT_SIZE_RE = re.compile(r"(?i)\bfont-size\s*:\s*([0-9]+(?:\.[0-9]+)?)px\b")
_PDF_PAGES_RE = re.compile(r"(?im)^Pages:\s*(\d+)\s*$")
_PDF_SIZE_RE = re.compile(
    r"(?im)^Page size:\s*([0-9]+(?:\.[0-9]+)?)\s+x\s+"
    r"([0-9]+(?:\.[0-9]+)?)\s+pts\b"
)


class PosterContractError(core.ContractError):
    """The poster-specific artifact contract is invalid."""


@dataclass
class _Element:
    tag: str
    attrs: dict[str, str]
    parent: int | None
    text: list[str] = field(default_factory=list)

    def visible_text(self) -> str:
        return _normalize_space(" ".join(self.text))


class _PosterParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[_Element] = []
        self.stack: list[int] = []
        self.declarations: list[str] = []
        self.parse_errors: list[str] = []

    def handle_decl(self, decl: str) -> None:
        self.declarations.append(decl.strip().lower())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = {str(key).lower(): str(value or "") for key, value in attrs}
        parent = self.stack[-1] if self.stack else None
        self.elements.append(_Element(tag.lower(), normalized, parent))
        index = len(self.elements) - 1
        if tag.lower() not in _VOID_TAGS:
            self.stack.append(index)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack and self.elements[self.stack[-1]].tag == tag.lower():
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        for position in range(len(self.stack) - 1, -1, -1):
            index = self.stack[position]
            if self.elements[index].tag == normalized:
                del self.stack[position:]
                return
        self.parse_errors.append(f"unmatched closing tag: {tag}")

    def handle_data(self, data: str) -> None:
        if not data.strip():
            return
        for index in self.stack:
            self.elements[index].text.append(data)

    def is_descendant(self, child_index: int, ancestor_index: int) -> bool:
        cursor = self.elements[child_index].parent
        while cursor is not None:
            if cursor == ancestor_index:
                return True
            cursor = self.elements[cursor].parent
        return False


def _normalize_space(value: str) -> str:
    return " ".join(value.split())


def _read_json_object(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise PosterContractError(f"expected a regular JSON file: {source}")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PosterContractError(f"invalid JSON object: {source}") from error
    if not isinstance(value, dict):
        raise PosterContractError(f"JSON contract must be an object: {source}")
    return value


def _read_run(run_dir: Path | str) -> dict[str, Any]:
    return _read_json_object(Path(run_dir) / "run.json")


def _require_number(value: Any, name: str, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PosterContractError(f"{name} must be a finite number")
    if value <= 0:
        raise PosterContractError(f"{name} must be positive")
    if integer:
        if int(value) != value:
            raise PosterContractError(f"{name} must be an integer")
        return int(value)
    return round(float(value), 4)


def _narrative_roles(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) < 4:
        raise PosterContractError("poster plan requires at least four narrative sections")
    sections: list[dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != {"role", "purpose"}:
            raise PosterContractError("each narrative section requires only role and purpose")
        role = str(raw.get("role") or "").strip().lower()
        purpose = str(raw.get("purpose") or "").strip()
        if not role or not purpose:
            raise PosterContractError("narrative role and purpose must be non-empty")
        sections.append({"role": role, "purpose": purpose})
    present = {section["role"] for section in sections}
    missing = [name for name, aliases in _ARC_GROUPS.items() if not present.intersection(aliases)]
    if missing:
        raise PosterContractError(f"narrative arc is missing: {', '.join(missing)}")
    return sections


def normalize_plan(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize and validate a fixed-canvas poster plan."""

    value = dict(payload)
    unknown = sorted(set(value) - _PLAN_KEYS)
    if unknown:
        raise PosterContractError(f"poster plan has unknown fields: {', '.join(unknown)}")
    if value.get("format_version") != FORMAT_VERSION or value.get("artifact_type") != "poster":
        raise PosterContractError("poster plan requires format_version=1 and artifact_type=poster")
    preset = str(value.get("preset") or DEFAULT_PRESET).strip().lower()
    if preset != "custom" and preset not in PRESETS:
        raise PosterContractError(f"unsupported poster preset: {preset}")
    if preset == "custom":
        if not isinstance(value.get("canvas"), Mapping) or not isinstance(value.get("print"), Mapping):
            raise PosterContractError("custom poster size requires canvas and print objects")
        canvas_source = dict(value["canvas"])
        print_source = dict(value["print"])
    else:
        canvas_source = dict(value.get("canvas") or PRESETS[preset]["canvas"])
        print_source = dict(value.get("print") or PRESETS[preset]["print"])
        if canvas_source != PRESETS[preset]["canvas"] or print_source != PRESETS[preset]["print"]:
            raise PosterContractError("named poster preset dimensions must match the preset exactly")
    if set(canvas_source) != {"width_px", "height_px"} or set(print_source) != {
        "width_mm", "height_mm"
    }:
        raise PosterContractError("canvas and print size schemas are incomplete")
    canvas = {
        "width_px": _require_number(canvas_source["width_px"], "canvas.width_px", integer=True),
        "height_px": _require_number(canvas_source["height_px"], "canvas.height_px", integer=True),
    }
    print_size = {
        "width_mm": _require_number(print_source["width_mm"], "print.width_mm"),
        "height_mm": _require_number(print_source["height_mm"], "print.height_mm"),
    }
    canvas_ratio = canvas["width_px"] / canvas["height_px"]
    print_ratio = print_size["width_mm"] / print_size["height_mm"]
    if not math.isclose(canvas_ratio, print_ratio, rel_tol=0.012, abs_tol=0.012):
        raise PosterContractError("canvas and physical print aspect ratios do not match")
    max_attempts = value.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 8:
        raise PosterContractError("max_attempts must be an integer from 1 through 8")
    allocations = value.get("visual_allocations", [])
    if not isinstance(allocations, list) or any(
        not isinstance(item, Mapping) or set(item) != {"visual_id", "role"}
        or not str(item.get("visual_id") or "").strip()
        or not str(item.get("role") or "").strip()
        for item in allocations
    ):
        raise PosterContractError("visual_allocations require visual_id and role")
    references = value.get("style_reference_ids", [])
    if not isinstance(references, list) or any(not isinstance(item, str) or not item for item in references):
        raise PosterContractError("style_reference_ids must be a list of non-empty strings")
    if len(set(references)) != len(references):
        raise PosterContractError("style_reference_ids must be unique")
    return {
        "format_version": FORMAT_VERSION,
        "artifact_type": "poster",
        "preset": preset,
        "canvas": canvas,
        "print": print_size,
        "narrative": _narrative_roles(value.get("narrative")),
        "visual_allocations": [
            {"visual_id": str(item["visual_id"]), "role": str(item["role"])}
            for item in allocations
        ],
        "style_reference_ids": list(references),
        "max_attempts": max_attempts,
    }


def initialize_poster_run(
    run_dir: Path | str,
    source_path: Path | str,
    *,
    extra_assets: Sequence[Path | str] = (),
    reference_images: Sequence[Path | str] = (),
    release_version: str = RELEASE_VERSION,
    archive_sha256: str | None = None,
) -> dict[str, Any]:
    core.initialize_run(
        run_dir,
        SKILL_ROOT,
        release_version=release_version,
        archive_sha256=archive_sha256,
    )
    manifest = core.prepare_source(
        run_dir,
        source_path,
        extra_assets=extra_assets,
        reference_images=reference_images,
    )
    return {
        "run_dir": str(Path(run_dir).absolute()),
        "source": manifest,
        "resume": core.resume_run(run_dir, skill_root=SKILL_ROOT),
    }


def save_poster_plan(run_dir: Path | str, payload: Mapping[str, Any]) -> dict[str, Any]:
    plan = normalize_plan(payload)
    catalog = _read_json_object(Path(run_dir) / "evidence" / "source_visuals.json")
    by_id = {str(item.get("id")): item for item in catalog.get("visuals", []) if isinstance(item, dict)}
    for allocation in plan["visual_allocations"]:
        item = by_id.get(allocation["visual_id"])
        suffix = Path(str(item.get("path") or "")).suffix.lower() if item else ""
        if item is not None and suffix not in SUPPORTED_IMAGE_SUFFIXES:
            raise PosterContractError(
                f"unsupported poster visual format: {suffix or '<none>'}"
            )
    try:
        visual_result = core.validate_visual_plan(run_dir, plan["visual_allocations"])
    except core.ContractError as error:
        raise PosterContractError(str(error)) from error
    if not visual_result["valid"]:
        raise PosterContractError("visual plan contains an unknown or over-reused visual")
    for reference_id in plan["style_reference_ids"]:
        item = by_id.get(reference_id)
        if item is None or item.get("eligibility") != "style_only":
            raise PosterContractError(f"style reference is not style-only: {reference_id}")
    allocated = {item["visual_id"] for item in plan["visual_allocations"]}
    overlap = allocated.intersection(plan["style_reference_ids"])
    if overlap:
        raise PosterContractError(f"style-only references cannot be content assets: {', '.join(sorted(overlap))}")
    return core.save_plan(run_dir, plan)


def _load_plan(run_dir: Path | str) -> dict[str, Any]:
    return normalize_plan(_read_json_object(Path(run_dir) / "plan.json"))


def _safe_catalog_path(run: Path, relative: str) -> Path:
    try:
        return core.safe_path(run / "evidence", relative, must_exist=True)
    except core.PathSafetyError as error:
        raise PosterContractError(str(error)) from error


def begin_poster_attempt(run_dir: Path | str) -> dict[str, Any]:
    run = Path(run_dir).absolute()
    plan = _load_plan(run)
    state = _read_run(run)
    if state.get("state") != "authoring" and int(state.get("attempt_count", 0)) >= plan["max_attempts"]:
        raise PosterContractError("poster repair attempt budget is exhausted")
    attempt_id = core.begin_attempt(run)
    attempt = run / "attempts" / attempt_id
    artifact = attempt / "artifact"
    catalog = _read_json_object(run / "evidence" / "source_visuals.json")
    by_id = {str(item.get("id")): item for item in catalog.get("visuals", []) if isinstance(item, dict)}
    staged: list[dict[str, str]] = []
    for allocation in plan["visual_allocations"]:
        visual = by_id.get(allocation["visual_id"])
        if visual is None or visual.get("eligibility") != "eligible":
            raise PosterContractError(f"allocated visual is no longer eligible: {allocation['visual_id']}")
        source = _safe_catalog_path(run, str(visual.get("path") or ""))
        suffix = source.suffix.lower()
        if suffix not in SUPPORTED_IMAGE_SUFFIXES:
            raise PosterContractError(f"unsupported poster visual format: {suffix or '<none>'}")
        relative = f"assets/{allocation['visual_id']}{suffix}"
        target = core.safe_path(artifact, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and core.sha256_file(target) != visual.get("sha256"):
            raise core.IntegrityError(f"staged visual drifted: {allocation['visual_id']}")
        if not target.exists():
            core.atomic_write_bytes(target, source.read_bytes())
        staged.append(
            {
                "visual_id": allocation["visual_id"],
                "role": allocation["role"],
                "artifact_path": relative,
                "sha256": core.sha256_file(target),
            }
        )
    style_references: list[dict[str, str]] = []
    for reference_id in plan["style_reference_ids"]:
        visual = by_id[reference_id]
        style_references.append(
            {
                "visual_id": reference_id,
                "run_relative_path": f"evidence/{visual['path']}",
                "transfer": "style_only",
            }
        )
    context = {
        "format_version": FORMAT_VERSION,
        "attempt_id": attempt_id,
        "poster_path": "artifact/poster.html",
        "plan": plan,
        "staged_content_visuals": staged,
        "style_references": style_references,
    }
    core.atomic_write_json(attempt / "authoring-context.json", context)
    return {
        "attempt_id": attempt_id,
        "attempt_dir": str(attempt),
        "poster_path": str(artifact / "poster.html"),
        "authoring_context": str(attempt / "authoring-context.json"),
        "staged_content_visuals": staged,
        "style_references": style_references,
    }


def _split_ids(value: str) -> list[str]:
    return [part for part in re.split(r"[\s,]+", value.strip()) if part]


def _check(check_id: str, passed: bool, detail: str, **data: Any) -> dict[str, Any]:
    return {"id": check_id, "passed": bool(passed), "detail": detail, **data}


def _is_ancestor(parser: _PosterParser, ancestor: int, child: int) -> bool:
    return ancestor == child or parser.is_descendant(child, ancestor)


def _font_sizes(css: str, selector_needles: Sequence[str]) -> list[float]:
    values: list[float] = []
    lowered_needles = [needle.lower() for needle in selector_needles]
    for match in _FONT_RULE_RE.finditer(css):
        selectors = match.group("selectors").lower()
        if not any(needle in selectors for needle in lowered_needles):
            continue
        values.extend(float(item) for item in _FONT_SIZE_RE.findall(match.group("body")))
    return values


def _validate_local_url(raw: str, *, html_path: Path, artifact_root: Path) -> str | None:
    value = unquote(raw.strip())
    if not value or value.startswith("#"):
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or value.startswith(("/", "\\")):
        return f"non-local URL: {raw[:120]}"
    relative = Path(parsed.path.replace("\\", "/"))
    if any(part in {"", ".", ".."} for part in relative.parts):
        return f"unsafe local path: {raw[:120]}"
    candidate = html_path.parent / relative
    cursor = html_path.parent
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return f"symlinked asset path: {raw[:120]}"
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(artifact_root.resolve(strict=True))
    except (OSError, ValueError):
        return f"missing or escaping local asset: {raw[:120]}"
    if not resolved.is_file():
        return f"local asset is not a file: {raw[:120]}"
    return None


def lint_poster_html(
    html_path: Path | str,
    *,
    artifact_root: Path | str,
    plan: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]],
    evidence_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Run deterministic static gates before browser execution."""

    html = Path(html_path).absolute()
    artifact = Path(artifact_root).absolute()
    if html.is_symlink() or not html.is_file() or artifact.is_symlink() or not artifact.is_dir():
        raise PosterContractError("poster HTML and artifact root must be regular local paths")
    try:
        html.resolve(strict=True).relative_to(artifact.resolve(strict=True))
    except ValueError as error:
        raise PosterContractError("poster HTML must be inside the attempt artifact directory") from error
    normalized_plan = normalize_plan(plan)
    text = html.read_text(encoding="utf-8")
    if len(text.encode("utf-8")) > 8 * 1024 * 1024:
        raise PosterContractError("poster HTML exceeds the 8 MiB contract limit")
    parser = _PosterParser()
    parser.feed(text)
    parser.close()
    checks: list[dict[str, Any]] = []

    unsafe: list[str] = list(parser.parse_errors)
    if parser.stack:
        unsafe.append("unclosed HTML elements")
    if not any(decl == "doctype html" for decl in parser.declarations):
        unsafe.append("missing <!doctype html>")
    for element in parser.elements:
        if element.tag in _UNSAFE_TAGS:
            unsafe.append(f"unsafe tag: {element.tag}")
        for name, value in element.attrs.items():
            if name.startswith("on"):
                unsafe.append(f"event handler attribute: {name}")
            if "javascript:" in value.lower():
                unsafe.append(f"script URL in {name}")
        inline_style = element.attrs.get("style", "")
        if re.search(r"(?i)@import|expression\s*\(|javascript:", inline_style):
            unsafe.append("unsafe inline style")
    css = "\n".join(element.visible_text() for element in parser.elements if element.tag == "style")
    if re.search(r"(?i)@import|expression\s*\(|javascript:", css):
        unsafe.append("unsafe CSS import or expression")
    checks.append(_check("document_contract", not unsafe, "; ".join(unsafe) or "safe standalone HTML"))

    roots = [
        element for element in parser.elements
        if element.tag == "main"
        and "paper-poster" in _split_ids(element.attrs.get("class", ""))
        and element.attrs.get("data-autodesign-artifact") == "poster"
    ]
    canvas_errors: list[str] = []
    if len(roots) != 1:
        canvas_errors.append("expected exactly one main.paper-poster root")
    else:
        root = roots[0]
        expected_canvas = normalized_plan["canvas"]
        if root.attrs.get("data-canvas-width") != str(expected_canvas["width_px"]):
            canvas_errors.append("root data-canvas-width does not match the plan")
        if root.attrs.get("data-canvas-height") != str(expected_canvas["height_px"]):
            canvas_errors.append("root data-canvas-height does not match the plan")
    checks.append(_check("fixed_canvas", not canvas_errors, "; ".join(canvas_errors) or "fixed canvas declared"))

    print_errors: list[str] = []
    page_match = _PAGE_SIZE_RE.search(css)
    expected_print = normalized_plan["print"]
    if page_match is None:
        print_errors.append("missing explicit @page size in millimeters")
    else:
        page_width = float(page_match.group("width"))
        page_height = float(page_match.group("height"))
        if not math.isclose(page_width, expected_print["width_mm"], abs_tol=0.05):
            print_errors.append("@page width does not match the plan")
        if not math.isclose(page_height, expected_print["height_mm"], abs_tol=0.05):
            print_errors.append("@page height does not match the plan")
        page_rule = page_match.group(0)
        if not re.search(r"(?i)\bmargin\s*:\s*0(?:mm|cm|in|px|pt)?\s*;?", page_rule):
            print_errors.append("@page must use zero margin")
    if roots:
        root = roots[0]
        for attr, field in (
            ("data-print-width-mm", "width_mm"),
            ("data-print-height-mm", "height_mm"),
        ):
            try:
                declared = float(root.attrs.get(attr, ""))
            except ValueError:
                declared = -1
            if not math.isclose(declared, expected_print[field], abs_tol=0.05):
                print_errors.append(f"root {attr} does not match the plan")
    checks.append(_check("print_page_size", not print_errors, "; ".join(print_errors) or "physical print page declared"))

    identity_errors: list[str] = []
    headers = [
        index for index, element in enumerate(parser.elements)
        if element.tag == "header" and element.attrs.get("data-role") == "identity-header"
    ]
    if len(headers) != 1:
        identity_errors.append("expected exactly one identity header")
    else:
        header = headers[0]
        fields: dict[str, list[int]] = {name: [] for name in ("title", "authors", "institutions")}
        extra_fields: list[str] = []
        unassigned_text: list[str] = []
        for index, element in enumerate(parser.elements):
            if not _is_ancestor(parser, header, index):
                continue
            identity = element.attrs.get("data-identity")
            if identity in fields:
                fields[identity].append(index)
            elif identity:
                extra_fields.append(identity)
            if element.tag in {"a", "figure", "img", "svg", "table"}:
                identity_errors.append(f"identity header contains forbidden {element.tag}")
        for name, indices in fields.items():
            if len(indices) != 1 or not parser.elements[indices[0]].visible_text():
                identity_errors.append(f"identity field {name} must appear exactly once")
            elif not _split_ids(parser.elements[indices[0]].attrs.get("data-source-ids", "")):
                identity_errors.append(f"identity field {name} requires source IDs")
        if extra_fields:
            identity_errors.append(f"non-identity fields present: {', '.join(extra_fields)}")
        for index, element in enumerate(parser.elements):
            if not _is_ancestor(parser, header, index) or not element.text:
                continue
            if any(_is_ancestor(parser, field_index, index) for values in fields.values() for field_index in values):
                continue
            if element.parent == header and element.visible_text():
                unassigned_text.append(element.visible_text())
        if unassigned_text:
            identity_errors.append("identity header contains text outside the three identity fields")
    identity_detail = "; ".join(identity_errors) or "identity header contains exactly title, authors, and institutions"
    if identity_errors:
        identity_detail = "identity header must contain exactly title, authors, and institutions; " + identity_detail
    checks.append(_check("identity_header", not identity_errors, identity_detail))

    native_errors: list[str] = []
    native_text = [
        element for element in parser.elements
        if element.tag in {"h1", "h2", "h3", "li", "p", "td", "th"} and element.visible_text()
    ]
    table_indices = [
        index for index, element in enumerate(parser.elements) if element.tag == "table"
    ]
    if len(native_text) < 8 or sum(len(element.visible_text()) for element in native_text) < 240:
        native_errors.append("poster needs substantial native editable text")
    if not table_indices or not all(
        any(
            parser.is_descendant(child_index, table_index)
            and child.tag in {"td", "th"}
            and child.visible_text()
            for child_index, child in enumerate(parser.elements)
        )
        for table_index in table_indices
    ):
        native_errors.append("poster needs at least one native editable HTML table")
    checks.append(_check("native_editability", not native_errors, "; ".join(native_errors) or "native text and table structure present"))

    binding_errors: list[str] = []
    claim_values = [dict(claim) for claim in claims]
    by_claim = {str(claim.get("id") or ""): claim for claim in claim_values}
    if len(by_claim) != len(claim_values) or "" in by_claim:
        binding_errors.append("source map claim IDs must be unique and non-empty")
    claimed_elements: dict[str, list[_Element]] = {}
    known_evidence_ids = {str(item) for item in evidence_ids if str(item)} | {
        str(source_id)
        for claim in claim_values
        for source_id in claim.get("source_ids", [])
        if isinstance(claim.get("source_ids"), list)
    }
    for element in parser.elements:
        claim_id = element.attrs.get("data-claim-id")
        if claim_id:
            claimed_elements.setdefault(claim_id, []).append(element)
        if element.tag in {"section", "article"} and not _split_ids(element.attrs.get("data-source-ids", "")):
            binding_errors.append(f"{element.tag} is missing data-source-ids")
        if element.tag == "img" and not element.attrs.get("data-source-id"):
            binding_errors.append("source image is missing data-source-id")
    for claim_id, elements in claimed_elements.items():
        claim = by_claim.get(claim_id)
        if claim is None:
            binding_errors.append(f"unknown claim binding: {claim_id}")
            continue
        if len(elements) != 1:
            binding_errors.append(f"claim binding must appear exactly once: {claim_id}")
            continue
        element = elements[0]
        expected_ids = {str(item) for item in claim.get("source_ids", [])}
        actual_ids = set(_split_ids(element.attrs.get("data-source-ids", "")))
        if actual_ids != expected_ids:
            binding_errors.append(f"claim source IDs do not match: {claim_id}")
        if _normalize_space(str(claim.get("text") or "")).lower() not in element.visible_text().lower():
            binding_errors.append(f"visible claim text does not match source map: {claim_id}")
    missing_claims = sorted(set(by_claim) - set(claimed_elements))
    if missing_claims:
        binding_errors.append(f"claims missing from poster: {', '.join(missing_claims)}")
    for element in parser.elements:
        source_ids = _split_ids(element.attrs.get("data-source-ids", ""))
        unknown_ids = sorted(set(source_ids) - known_evidence_ids)
        if unknown_ids:
            binding_errors.append(f"unknown evidence IDs in HTML: {', '.join(unknown_ids)}")
    checks.append(_check("source_bindings", not binding_errors, "; ".join(dict.fromkeys(binding_errors)) or "visible claims and source IDs are bound"))

    local_errors: list[str] = []
    referenced_files: set[str] = set()

    def record_local_url(url: str) -> None:
        error = _validate_local_url(url, html_path=html, artifact_root=artifact)
        if error:
            local_errors.append(error)
            return
        value = unquote(url.strip())
        if not value or value.startswith("#"):
            return
        relative = Path(urlsplit(value).path.replace("\\", "/"))
        resolved = (html.parent / relative).resolve(strict=True)
        referenced_files.add(resolved.relative_to(artifact.resolve(strict=True)).as_posix())

    for element in parser.elements:
        for name, value in element.attrs.items():
            values: list[str] = []
            if name in _URL_ATTRS:
                values = [value]
            elif name == "srcset":
                values = [part.strip().split()[0] for part in value.split(",") if part.strip()]
            for url in values:
                record_local_url(url)
    for match in _CSS_URL_RE.finditer(css):
        record_local_url(match.group(2))
    for element in parser.elements:
        for match in _CSS_URL_RE.finditer(element.attrs.get("style", "")):
            record_local_url(match.group(2))
    actual_files: set[str] = set()
    for path in artifact.rglob("*"):
        if path.is_symlink():
            local_errors.append(f"symlinked artifact file: {path.name}")
        elif path.is_file() and path != html:
            actual_files.add(path.relative_to(artifact).as_posix())
    unreferenced = sorted(actual_files - referenced_files)
    if unreferenced:
        local_errors.append(f"unreferenced artifact files: {', '.join(unreferenced)}")
    checks.append(_check("local_assets", not local_errors, "; ".join(dict.fromkeys(local_errors)) or "all dependencies are local and complete"))

    section_roles = {
        element.attrs.get("data-section-role", "").lower()
        for element in parser.elements
        if element.tag in {"article", "section"}
    }
    missing_arc = [name for name, aliases in _ARC_GROUPS.items() if not section_roles.intersection(aliases)]
    section_heading_errors: list[str] = []
    for section_index, section in enumerate(parser.elements):
        if section.tag not in {"article", "section"}:
            continue
        if not any(
            parser.is_descendant(index, section_index) and element.tag in {"h2", "h3"}
            and element.visible_text()
            for index, element in enumerate(parser.elements)
        ):
            section_heading_errors.append(section.attrs.get("data-section-role", "unnamed"))
    arc_errors = ([f"missing research arc roles: {', '.join(missing_arc)}"] if missing_arc else [])
    if section_heading_errors:
        arc_errors.append(f"sections missing headings: {', '.join(section_heading_errors)}")
    checks.append(_check("narrative_arc", not arc_errors, "; ".join(arc_errors) or "problem, method, evidence, and takeaway arc present"))

    typography_errors: list[str] = []
    typography_contract = (
        ("title", ('[data-identity="title"]', "[data-identity='title']"), 56.0),
        ("identity", ('[data-identity="authors"]', "[data-identity='authors']", '[data-identity="institutions"]', "[data-identity='institutions']"), 28.0),
        ("section heading", ("h2",), 36.0),
        ("body/table", ("p,", "li,", "th,", "td", "p ", "li ", "th ", "td "), 24.0),
    )
    for label, selectors, minimum in typography_contract:
        sizes = _font_sizes(css, selectors)
        if not sizes or max(sizes) < minimum:
            typography_errors.append(f"{label} font size must be explicit and at least {minimum:g}px")
    checks.append(_check("typography_contract", not typography_errors, "; ".join(typography_errors) or "conference-poster typography thresholds met"))
    return {"format_version": FORMAT_VERSION, "passed": all(check["passed"] for check in checks), "checks": checks}


def parse_pdfinfo(
    text: str,
    *,
    expected_width_mm: float,
    expected_height_mm: float,
) -> dict[str, Any]:
    pages_match = _PDF_PAGES_RE.search(text)
    size_match = _PDF_SIZE_RE.search(text)
    pages = int(pages_match.group(1)) if pages_match else 0
    width_pts = float(size_match.group(1)) if size_match else 0.0
    height_pts = float(size_match.group(2)) if size_match else 0.0
    expected_width_pts = expected_width_mm / 25.4 * 72.0
    expected_height_pts = expected_height_mm / 25.4 * 72.0
    size_matches = (
        width_pts > 0
        and height_pts > 0
        and math.isclose(width_pts, expected_width_pts, abs_tol=1.5)
        and math.isclose(height_pts, expected_height_pts, abs_tol=1.5)
    )
    return {
        "id": "single_page_pdf",
        "passed": pages == 1 and size_matches,
        "detail": "exact one-page physical poster PDF" if pages == 1 and size_matches else "PDF must contain exactly one page at the planned physical size",
        "pages": pages,
        "width_pts": width_pts,
        "height_pts": height_pts,
        "expected_width_pts": round(expected_width_pts, 3),
        "expected_height_pts": round(expected_height_pts, 3),
    }


def _safe_worker_output(path: Path, root: Path) -> Path:
    if path.is_symlink() or root.is_symlink():
        raise PosterContractError("render output paths must not be symlinks")
    root_resolved = root.resolve(strict=True)
    try:
        path.parent.resolve(strict=True).relative_to(root_resolved)
    except ValueError as error:
        raise PosterContractError("render output escapes the attempt") from error
    return path


def _browser_export_main(argv: Sequence[str]) -> int:
    """Pinned-runtime worker entry point; not a user-facing command."""

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--width-mm", type=float, required=True)
    parser.add_argument("--height-mm", type=float, required=True)
    args = parser.parse_args(argv)
    artifact = args.artifact_root.resolve(strict=True)
    html = args.html.resolve(strict=True)
    html.relative_to(artifact)
    output = _safe_worker_output(args.pdf.absolute(), artifact)
    from playwright.sync_api import sync_playwright
    import browser_worker

    blocked: list[str] = []

    def route_request(route: Any) -> None:
        request_url = route.request.url
        parsed = urlsplit(request_url)
        if parsed.scheme in {"about", "blob", "data"}:
            route.continue_()
            return
        if parsed.scheme == "file" and not parsed.netloc:
            try:
                candidate = Path(url2pathname(unquote(parsed.path))).resolve(strict=True)
                candidate.relative_to(artifact)
            except (OSError, ValueError):
                blocked.append("file:///[outside-artifact]")
                route.abort("blockedbyclient")
            else:
                route.continue_()
            return
        blocked.append(f"{parsed.scheme or '[none]'}://{parsed.hostname or '[unknown]'}")
        route.abort("blockedbyclient")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=list(browser_worker._BROWSER_NETWORK_REDUCTION_ARGS),
        )
        context = browser.new_context()
        context.route("**/*", route_request)
        page = context.new_page()
        try:
            page.goto(html.as_uri(), wait_until="load", timeout=30_000)
            page.emulate_media(media="print", reduced_motion="reduce")
            if blocked:
                raise PosterContractError("network or outside-artifact request was blocked during PDF export")
            pdf_bytes = page.pdf(
                width=f"{args.width_mm:.4f}mm",
                height=f"{args.height_mm:.4f}mm",
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                print_background=True,
                prefer_css_page_size=True,
            )
            core.atomic_write_bytes(output, pdf_bytes)
        finally:
            context.close()
            browser.close()
    print(json.dumps({"status": "ok", "blocked_requests": blocked}, sort_keys=True))
    return 0


def _export_pdf(
    runtime: setup_browser.BrowserRuntime,
    *,
    html: Path,
    artifact_root: Path,
    pdf: Path,
    width_mm: float,
    height_mm: float,
) -> None:
    bootstrap = (
        "import sys;"
        f"sys.path.insert(0,{str(SCRIPT_DIR)!r});"
        "import poster_harness;"
        "raise SystemExit(poster_harness._browser_export_main(sys.argv[1:]))"
    )
    command = [
        str(runtime.python_executable),
        "-I",
        "-c",
        bootstrap,
        "--html",
        str(html),
        "--artifact-root",
        str(artifact_root),
        "--pdf",
        str(pdf),
        "--width-mm",
        str(width_mm),
        "--height-mm",
        str(height_mm),
    ]
    env = setup_browser.isolated_environment(
        browsers_path=runtime.browsers_path,
        allow_network_configuration=False,
    )
    completed = subprocess.run(
        command,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        detail = core.redact_secrets((completed.stderr or completed.stdout).strip())
        raise PosterContractError(f"poster PDF export failed: {detail}")


def _render_poster_outputs(
    *,
    attempt_root: Path,
    plan: Mapping[str, Any],
    cache_root: Path | None,
    allow_browser_install: bool,
) -> dict[str, Any]:
    artifact = attempt_root / "artifact"
    html = artifact / "poster.html"
    preview_dir = attempt_root / "qa" / "previews"
    runtime = setup_browser.ensure_browser_runtime(
        cache_root=cache_root,
        allow_install=allow_browser_install,
    )
    canvas = plan["canvas"]
    browser_report = setup_browser.audit_local_html(
        html,
        workspace_root=attempt_root,
        output_dir=preview_dir,
        viewports=(f"poster:{canvas['width_px']}x{canvas['height_px']}",),
        runtime=runtime,
        cache_root=cache_root,
        allow_install=False,
        timeout_seconds=180,
    )
    preview = preview_dir / "poster.png"
    browser_check = _check(
        "browser_geometry",
        browser_report.get("passed") is True and preview.is_file(),
        "pinned Chromium geometry and dependency audit passed"
        if browser_report.get("passed") is True and preview.is_file()
        else "pinned Chromium found overflow, clipping, blank render, missing assets, or runtime errors",
        report="qa/previews/audit.json",
    )
    if preview.is_file():
        core.atomic_write_bytes(artifact / "preview.png", preview.read_bytes())
    pdf = artifact / "poster.pdf"
    _export_pdf(
        runtime,
        html=html,
        artifact_root=artifact,
        pdf=pdf,
        width_mm=plan["print"]["width_mm"],
        height_mm=plan["print"]["height_mm"],
    )
    pdfinfo = shutil.which("pdfinfo")
    if not pdfinfo:
        raise PosterContractError("pdfinfo is required to verify one-page poster delivery")
    completed = subprocess.run(
        [pdfinfo, str(pdf)],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise PosterContractError(f"pdfinfo failed: {core.redact_secrets(completed.stderr.strip())}")
    pdf_check = parse_pdfinfo(
        completed.stdout,
        expected_width_mm=plan["print"]["width_mm"],
        expected_height_mm=plan["print"]["height_mm"],
    )
    checks = [browser_check, pdf_check]
    result = {
        "format_version": FORMAT_VERSION,
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
        "preview_path": "qa/previews/poster.png",
        "browser_report": browser_report,
    }
    core.atomic_write_json(attempt_root / "qa" / "poster-output.json", result)
    return result


def _attempt_artifact_paths(attempt_root: Path) -> list[str]:
    paths: list[str] = []
    artifact = attempt_root / "artifact"
    for path in sorted(artifact.rglob("*")):
        if path.is_symlink():
            raise core.PathSafetyError(f"artifact contains a symlink: {path}")
        if path.is_file():
            paths.append(f"artifact/{path.relative_to(artifact).as_posix()}")
    return paths


def validate_poster_attempt(
    run_dir: Path | str,
    attempt_id: str,
    *,
    source_map_path: Path | str,
    cache_root: Path | None = None,
    allow_browser_install: bool = True,
) -> dict[str, Any]:
    run = Path(run_dir).absolute()
    state = _read_run(run)
    if state.get("state") != "authoring" or state.get("active_attempt") != attempt_id:
        raise PosterContractError("validation must target the active authoring attempt")
    plan = _load_plan(run)
    attempt = core.safe_path(run / "attempts", attempt_id, must_exist=True)
    artifact = attempt / "artifact"
    source_map_input = _read_json_object(source_map_path)
    if set(source_map_input) != {"claims"} or not isinstance(source_map_input["claims"], list):
        raise PosterContractError("source-map input must contain only a claims list")
    core.write_source_map(run, attempt_id, source_map_input["claims"])
    static = lint_poster_html(
        artifact / "poster.html",
        artifact_root=artifact,
        plan=plan,
        claims=source_map_input["claims"],
        evidence_ids=[
            str(item.get("id"))
            for item in core.load_evidence(run)
            if isinstance(item, Mapping) and item.get("id")
        ],
    )
    checks = list(static["checks"])
    preview_paths: dict[str, str] = {}
    if static["passed"]:
        try:
            rendered = _render_poster_outputs(
                attempt_root=attempt,
                plan=plan,
                cache_root=cache_root,
                allow_browser_install=allow_browser_install,
            )
        except (OSError, subprocess.SubprocessError, core.PortableError, setup_browser.BrowserRuntimeError) as error:
            checks.append(_check("browser_and_pdf_delivery", False, str(core.redact_secrets(str(error)))))
        else:
            checks.extend(rendered["checks"])
            if (attempt / rendered["preview_path"]).is_file():
                preview_paths["poster_full"] = rendered["preview_path"]
    passed = bool(checks) and all(check["passed"] for check in checks)
    required = {"artifact/poster.html", "artifact/poster.pdf", "artifact/preview.png"}
    artifact_paths = _attempt_artifact_paths(attempt)
    missing = sorted(required - set(artifact_paths))
    if missing:
        checks.append(_check("delivery_files", False, f"missing delivery files: {', '.join(missing)}"))
        passed = False
    report = core.record_deterministic_result(
        run,
        attempt_id,
        passed=passed,
        checks=checks,
        artifact_paths=artifact_paths,
        preview_paths=preview_paths,
    )
    return report


def create_poster_review_context(run_dir: Path | str, attempt_id: str) -> dict[str, Any]:
    return core.create_review_context(run_dir, attempt_id, rubric=REVIEW_RUBRIC)


def record_poster_review(
    run_dir: Path | str, attempt_id: str, review_path: Path | str
) -> dict[str, Any]:
    review = _read_json_object(review_path)
    scores = review.get("dimension_scores")
    if isinstance(scores, Mapping) and scores:
        average = sum(float(value) for value in scores.values()) / len(scores)
    else:
        average = 0.0
    if review.get("verdict") == "pass":
        if review.get("blockers"):
            raise PosterContractError("a passing poster review cannot contain blockers")
        if average < REVIEW_RUBRIC["score_scale"]["pass_average"]:
            raise PosterContractError("a passing poster review does not meet the rubric average")
    return core.record_semantic_review(run_dir, attempt_id, review)


def finalize_poster_attempt(run_dir: Path | str, attempt_id: str) -> dict[str, Any]:
    return core.finalize_attempt(run_dir, attempt_id)


def resume_poster_run(run_dir: Path | str) -> dict[str, Any]:
    return core.resume_run(run_dir, skill_root=SKILL_ROOT)


def _doctor(*, cache_root: Path | None, install_browser: bool) -> dict[str, Any]:
    poppler = {name: shutil.which(name) for name in ("pdfimages", "pdfinfo", "pdftoppm", "pdftotext")}
    browser: dict[str, Any]
    try:
        runtime = setup_browser.ensure_browser_runtime(
            cache_root=cache_root,
            allow_install=install_browser,
        )
    except setup_browser.BrowserRuntimeError as error:
        browser = {"ready": False, "detail": str(core.redact_secrets(str(error)))}
    else:
        browser = {"ready": True, "cache_dir": str(runtime.cache_dir)}
    return {
        "format_version": FORMAT_VERSION,
        "ready": all(poppler.values()) and browser["ready"],
        "python": sys.version.split()[0],
        "poppler": poppler,
        "browser": browser,
    }


def _print_json(value: Any) -> None:
    print(json.dumps(core.redact_secrets(value), ensure_ascii=False, indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor", help="check Poppler and pinned Chromium")
    doctor.add_argument("--cache-root", type=Path)
    doctor.add_argument("--install-browser", action="store_true")
    initialize = subparsers.add_parser("init", help="initialize a run and prepare source evidence")
    initialize.add_argument("--run-dir", type=Path, required=True)
    initialize.add_argument("--source", type=Path, required=True)
    initialize.add_argument("--asset", action="append", type=Path, default=[])
    initialize.add_argument("--reference", action="append", type=Path, default=[])
    initialize.add_argument("--release-version", default=RELEASE_VERSION)
    initialize.add_argument("--archive-sha256")
    evidence = subparsers.add_parser("evidence", help="retrieve grounded paper evidence")
    evidence.add_argument("--run-dir", type=Path, required=True)
    evidence.add_argument("--query", required=True)
    evidence.add_argument("--limit", type=int, default=8)
    bind = subparsers.add_parser("bind-visuals", help="bind PDF visuals after fresh host-VLM review")
    bind.add_argument("--run-dir", type=Path, required=True)
    bind.add_argument("--review", type=Path, required=True)
    plan = subparsers.add_parser("plan", help="save the evidence-grounded poster plan")
    plan.add_argument("--run-dir", type=Path, required=True)
    plan.add_argument("--plan", type=Path, required=True)
    begin = subparsers.add_parser("begin-attempt", help="start a bounded authoring attempt")
    begin.add_argument("--run-dir", type=Path, required=True)
    validate = subparsers.add_parser("validate", help="run static, browser, preview, and PDF gates")
    validate.add_argument("--run-dir", type=Path, required=True)
    validate.add_argument("--attempt", required=True)
    validate.add_argument("--source-map", type=Path, required=True)
    validate.add_argument("--cache-root", type=Path)
    validate.add_argument("--offline-browser", action="store_true")
    context = subparsers.add_parser("review-context", help="create hash-bound fresh-review context")
    context.add_argument("--run-dir", type=Path, required=True)
    context.add_argument("--attempt", required=True)
    record = subparsers.add_parser("record-review", help="validate and persist fresh visual review")
    record.add_argument("--run-dir", type=Path, required=True)
    record.add_argument("--attempt", required=True)
    record.add_argument("--review", type=Path, required=True)
    finalize = subparsers.add_parser("finalize", help="promote one reviewed attempt atomically")
    finalize.add_argument("--run-dir", type=Path, required=True)
    finalize.add_argument("--attempt", required=True)
    resume = subparsers.add_parser("resume", help="verify hashes and report the next safe action")
    resume.add_argument("--run-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if argv and argv[0] == "__browser-export":
        return _browser_export_main(argv[1:])
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = _doctor(cache_root=args.cache_root, install_browser=args.install_browser)
            _print_json(result)
            return 0 if result["ready"] else 2
        if args.command == "init":
            result = initialize_poster_run(
                args.run_dir,
                args.source,
                extra_assets=args.asset,
                reference_images=args.reference,
                release_version=args.release_version,
                archive_sha256=args.archive_sha256,
            )
        elif args.command == "evidence":
            result = core.lexical_retrieve(
                core.load_evidence(args.run_dir), args.query, limit=args.limit
            )
        elif args.command == "bind-visuals":
            result = core.bind_host_vlm_visuals(args.run_dir, _read_json_object(args.review))
        elif args.command == "plan":
            result = save_poster_plan(args.run_dir, _read_json_object(args.plan))
        elif args.command == "begin-attempt":
            result = begin_poster_attempt(args.run_dir)
        elif args.command == "validate":
            result = validate_poster_attempt(
                args.run_dir,
                args.attempt,
                source_map_path=args.source_map,
                cache_root=args.cache_root,
                allow_browser_install=not args.offline_browser,
            )
            _print_json(result)
            return 0 if result.get("passed") is True else 2
        elif args.command == "review-context":
            result = create_poster_review_context(args.run_dir, args.attempt)
        elif args.command == "record-review":
            result = record_poster_review(args.run_dir, args.attempt, args.review)
        elif args.command == "finalize":
            result = finalize_poster_attempt(args.run_dir, args.attempt)
        elif args.command == "resume":
            result = resume_poster_run(args.run_dir)
        else:  # pragma: no cover - argparse owns command dispatch
            parser.error(f"unknown command: {args.command}")
        _print_json(result)
        return 0
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError, core.PortableError, setup_browser.BrowserRuntimeError) as error:
        print(f"ERROR: {core.redact_secrets(str(error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
