#!/usr/bin/env python3
"""Standalone paper-to-poster harness for the AutoDesign Poster Skill."""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import json
import math
import re
import shlex
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET
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
import poster_dom_audit  # noqa: E402
import setup_browser  # noqa: E402


FORMAT_VERSION = 1
RELEASE_VERSION = "0.1.0"
DEFAULT_PRESET = "cvpr-landscape"
DEFAULT_MAX_ATTEMPTS = 4
SUPPORTED_IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
SUPPORTED_SIDECAR_SUFFIXES = SUPPORTED_IMAGE_SUFFIXES | {
    ".otf",
    ".ttf",
    ".woff",
    ".woff2",
}
SUPPORTED_FONT_SUFFIXES = SUPPORTED_SIDECAR_SUFFIXES - SUPPORTED_IMAGE_SUFFIXES
POSTER_SOURCE_ROLES = (
    "method",
    "overview",
    "method-overview",
    "result",
    "primary-result",
    "comparison",
    "context",
    "supporting",
)
POSTER_FINDING_MINIMUM_ROUTE = {
    "dom_overflow": "layout_repair",
    "dom_clipping": "layout_repair",
    "dom_overlap": "layout_repair",
    "dom_blank_band": "layout_repair",
    "typography": "layout_repair",
    "visual_balance": "layout_repair",
    "narrative_hierarchy": "content_replan",
    "claim_selection": "content_replan",
    "section_allocation": "content_replan",
    "evidence_area_mismatch": "content_replan",
    "key_visual_missing": "source_reingest",
    "wrong_visual": "source_reingest",
    "incomplete_crop": "source_reingest",
    "fragmentary_crop": "source_reingest",
    "unreadable_source_visual": "source_reingest",
    "caption_claim_mismatch": "source_reingest",
    **{code: "layout_repair" for code in poster_dom_audit.STABLE_FINDING_CODES},
}
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
    "thesis",
    "preset",
    "canvas",
    "print",
    "narrative",
    "visual_allocations",
    "no_visual_fallback",
    "style_reference_ids",
    "max_attempts",
}
_SOURCE_FLOW_RELATIONSHIPS = {"primary", "supporting"}
_ARC_GROUPS = {
    "problem": {"context", "introduction", "motivation", "problem"},
    "method": {"approach", "architecture", "method", "system"},
    "evidence": {"analysis", "evidence", "evaluation", "results"},
    "takeaway": {"conclusion", "discussion", "limitations", "takeaway"},
}
_UNSAFE_TAGS = {
    "base",
    "button",
    "canvas",
    "dialog",
    "embed",
    "form",
    "iframe",
    "input",
    "link",
    "object",
    "option",
    "script",
    "select",
    "textarea",
}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_URL_ATTRS = {"action", "formaction", "href", "poster", "src"}
_CSS_URL_RE = re.compile(r"(?is)url\(\s*(['\"]?)(.*?)\1\s*\)")
_CSS_CONTENT_RE = re.compile(r"(?is)(?:^|[;{])\s*content\s*:\s*([^;}]+)")
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
_COMPUTED_TYPOGRAPHY_SCRIPT = r"""
() => {
  const root = document.querySelector('main.paper-poster[data-autodesign-artifact="poster"]');
  const violations = [];
  const measurements = {title: [], identity: [], "section heading": [], body: []};
  if (!root) return {passed: false, violations: ["poster root is missing"], measurements};
  for (const element of [root, ...root.querySelectorAll("*")]) {
    for (const pseudo of ["::before", "::after", "::marker"]) {
      const content = (getComputedStyle(element, pseudo).content || "").trim();
      if (!["", "none", "normal", '""', "''"].includes(content)) {
        violations.push(`generated content is forbidden: ${element.tagName.toLowerCase()}${pseudo}`);
      }
    }
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let textIndex = 0;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!(node.nodeValue || "").trim()) continue;
    const owner = node.parentElement;
    if (!owner || owner.closest("style, script, template, noscript")) continue;
    const identity = owner.closest("[data-identity]")?.getAttribute("data-identity");
    let label = "body";
    let minimum = 24;
    if (identity === "title") {
      label = "title";
      minimum = 56;
    } else if (identity === "authors" || identity === "institutions") {
      label = "identity";
      minimum = 28;
    } else if (owner.closest("h2, h3")) {
      label = "section heading";
      minimum = 36;
    }
    const style = getComputedStyle(owner);
    const range = document.createRange();
    range.selectNodeContents(node);
    const rect = range.getBoundingClientRect();
    const size = Number.parseFloat(style.fontSize);
    let visiblyStyled = true;
    for (let element = owner; element; element = element.parentElement) {
      const ancestorStyle = getComputedStyle(element);
      if (
        ancestorStyle.display === "none" ||
        ancestorStyle.visibility === "hidden" ||
        Number.parseFloat(ancestorStyle.opacity || "1") <= 0
      ) {
        visiblyStyled = false;
        break;
      }
      if (element === root) break;
    }
    const visible = visiblyStyled && rect.width > 0 && rect.height > 0;
    measurements[label].push({
      text_index: textIndex,
      font_size_px: Number.isFinite(size) ? size : null,
      visible,
    });
    if (!visible) {
      violations.push(`${label}[${textIndex}]: not visibly rendered`);
    } else if (!Number.isFinite(size) || size + 0.01 < minimum) {
      violations.push(
        `${label}[${textIndex}]: ${Number.isFinite(size) ? size : "invalid"}px < ${minimum}px`,
      );
    }
    textIndex += 1;
  }
  if (!textIndex) violations.push("poster contains no rendered text");
  return {passed: violations.length === 0, violations, measurements};
}
"""


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
        normalized: dict[str, str] = {}
        for key, value in attrs:
            name = str(key).lower()
            if name in normalized:
                self.parse_errors.append(f"duplicate attribute on {tag.lower()}: {name}")
                continue
            normalized[name] = str(value or "")
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


def _read_canonical_json_object(path: Path | str) -> dict[str, Any]:
    """Read a CLI JSON input once and require the shared stored form."""

    source = Path(path)
    if (
        source.is_symlink()
        or not source.is_file()
        or source.stat().st_nlink != 1
    ):
        raise PosterContractError(f"expected a regular JSON file: {source}")
    try:
        data = source.read_bytes()
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PosterContractError(f"invalid JSON object: {source}") from error
    if not isinstance(value, dict):
        raise PosterContractError(f"JSON contract must be an object: {source}")
    if data != core._stored_json_bytes(value):
        raise PosterContractError(
            f"JSON contract must use canonical shared serialization: {source}"
        )
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


def _claim_ids(value: Any, name: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise PosterContractError(f"{name} must be a non-empty list of claim IDs")
    normalized = [item.strip() for item in value]
    if len(set(normalized)) != len(normalized):
        raise PosterContractError(f"{name} claim IDs must be unique")
    return normalized


def _narrative_roles(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) < 4:
        raise PosterContractError("poster plan requires at least four narrative sections")
    sections: list[dict[str, Any]] = []
    roles: set[str] = set()
    claim_owners: dict[str, str] = {}
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != {
            "role",
            "purpose",
            "claim_ids",
        }:
            raise PosterContractError(
                "each narrative section requires exactly role, purpose, and claim_ids"
            )
        raw_role = raw.get("role")
        raw_purpose = raw.get("purpose")
        if not isinstance(raw_role, str) or not isinstance(raw_purpose, str):
            raise PosterContractError(
                "narrative role and purpose must be non-empty strings"
            )
        role = raw_role.strip().lower()
        purpose = raw_purpose.strip()
        if not role or not purpose:
            raise PosterContractError(
                "narrative role and purpose must be non-empty strings"
            )
        if role in roles:
            raise PosterContractError("narrative section roles must be unique")
        roles.add(role)
        claims = _claim_ids(raw.get("claim_ids"), f"narrative.{role}.claim_ids")
        for claim_id in claims:
            if claim_id in claim_owners:
                raise PosterContractError(
                    "each claim ID must belong to exactly one narrative section"
                )
            claim_owners[claim_id] = role
        sections.append({"role": role, "purpose": purpose, "claim_ids": claims})
    missing = [name for name, aliases in _ARC_GROUPS.items() if not roles.intersection(aliases)]
    if missing:
        raise PosterContractError(f"narrative arc is missing: {', '.join(missing)}")
    return sections


def _normalize_no_visual_fallback(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"reason", "strategy"}:
        raise PosterContractError(
            "no_visual_fallback requires exactly reason and strategy"
        )
    raw_reason = value.get("reason")
    raw_strategy = value.get("strategy")
    if (
        not isinstance(raw_reason, str)
        or not raw_reason.strip()
        or not isinstance(raw_strategy, str)
        or not raw_strategy.strip()
    ):
        raise PosterContractError(
            "no_visual_fallback reason and strategy must be non-empty strings"
        )
    reason = raw_reason.strip()
    strategy = raw_strategy.strip()
    if len(reason) < 16 or len(strategy) < 16:
        raise PosterContractError(
            "no_visual_fallback reason and strategy must be explicit"
        )
    return {"reason": reason, "strategy": strategy}


def normalize_plan(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize and validate a fixed-canvas poster plan."""

    value = dict(payload)
    unknown = sorted(set(value) - _PLAN_KEYS)
    if unknown:
        raise PosterContractError(f"poster plan has unknown fields: {', '.join(unknown)}")
    if value.get("format_version") != FORMAT_VERSION or value.get("artifact_type") != "poster":
        raise PosterContractError("poster plan requires format_version=1 and artifact_type=poster")
    raw_thesis = value.get("thesis")
    if not isinstance(raw_thesis, str) or not raw_thesis.strip():
        raise PosterContractError("poster plan requires a non-empty thesis")
    thesis = raw_thesis.strip()
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
    narrative = _narrative_roles(value.get("narrative"))
    claim_owners = {
        claim_id: section["role"]
        for section in narrative
        for claim_id in section["claim_ids"]
    }
    allocations = value.get("visual_allocations", [])
    if not isinstance(allocations, list):
        raise PosterContractError("visual_allocations must be a list")
    normalized_allocations: list[dict[str, Any]] = []
    section_area: dict[str, float] = {}
    for item in allocations:
        if not isinstance(item, Mapping) or set(item) != {
            "visual_id",
            "role",
            "claim_ids",
            "source_flow_relationship",
            "intended_area",
        }:
            raise PosterContractError(
                "visual_allocations require exactly visual_id, role, claim_ids, "
                "source_flow_relationship, and intended_area"
            )
        raw_visual_id = item.get("visual_id")
        raw_role = item.get("role")
        if not isinstance(raw_visual_id, str) or not isinstance(raw_role, str):
            raise PosterContractError(
                "visual allocation visual_id and role must be non-empty strings"
            )
        visual_id = raw_visual_id.strip()
        role = raw_role.strip()
        if not visual_id or not role:
            raise PosterContractError(
                "visual allocation visual_id and role must be non-empty strings"
            )
        claims = _claim_ids(item.get("claim_ids"), f"visual_allocations.{visual_id}.claim_ids")
        relationship = item.get("source_flow_relationship")
        if (
            not isinstance(relationship, str)
            or relationship not in _SOURCE_FLOW_RELATIONSHIPS
        ):
            raise PosterContractError(
                "source_flow_relationship must be primary or supporting"
            )
        intended = item.get("intended_area")
        if not isinstance(intended, Mapping) or set(intended) != {
            "section_role",
            "relative_area",
        }:
            raise PosterContractError(
                "intended_area requires exactly section_role and relative_area"
            )
        raw_section_role = intended.get("section_role")
        if not isinstance(raw_section_role, str) or not raw_section_role.strip():
            raise PosterContractError(
                "intended_area.section_role must be a non-empty string"
            )
        section_role = raw_section_role.strip().lower()
        if section_role not in {section["role"] for section in narrative}:
            raise PosterContractError(
                f"intended_area references an unknown narrative section: {section_role}"
            )
        for claim_id in claims:
            if claim_owners.get(claim_id) != section_role:
                raise PosterContractError(
                    f"allocation claim {claim_id} is not owned by narrative section {section_role}"
                )
        relative_area = float(
            _require_number(
                intended.get("relative_area"),
                f"visual_allocations.{visual_id}.intended_area.relative_area",
            )
        )
        if relative_area > 1:
            raise PosterContractError("intended_area.relative_area must be at most 1")
        if relative_area <= 0:
            raise PosterContractError(
                "intended_area.relative_area must remain positive after normalization"
            )
        section_area[section_role] = round(
            section_area.get(section_role, 0.0) + relative_area, 4
        )
        if section_area[section_role] > 1:
            raise PosterContractError(
                f"intended area for narrative section {section_role} exceeds 1"
            )
        normalized_allocations.append(
            {
                "visual_id": visual_id,
                "role": role,
                "claim_ids": claims,
                "source_flow_relationship": relationship,
                "intended_area": {
                    "section_role": section_role,
                    "relative_area": relative_area,
                },
            }
        )
    references = value.get("style_reference_ids", [])
    if not isinstance(references, list) or any(not isinstance(item, str) or not item for item in references):
        raise PosterContractError("style_reference_ids must be a list of non-empty strings")
    if len(set(references)) != len(references):
        raise PosterContractError("style_reference_ids must be unique")
    return {
        "format_version": FORMAT_VERSION,
        "artifact_type": "poster",
        "thesis": thesis,
        "preset": preset,
        "canvas": canvas,
        "print": print_size,
        "narrative": narrative,
        "visual_allocations": normalized_allocations,
        "no_visual_fallback": _normalize_no_visual_fallback(
            value.get("no_visual_fallback")
        ),
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
    if extra_assets:
        raise PosterContractError(
            "--asset cannot provide v2 paper evidence; inspect the source PDF and "
            "derive reviewed crops with crop-source"
        )
    run_path = Path(run_dir)
    if (run_path / "run.json").exists() and core.inspect_run_format(run_path) != core.AGENT_FIRST_RUN_FORMAT_VERSION:
        raise PosterContractError(
            "v2 init cannot modify a legacy run; use diagnose-v1 for read-only inspection"
        )
    core.initialize_run(
        run_dir,
        SKILL_ROOT,
        release_version=release_version,
        archive_sha256=archive_sha256,
        run_format_version=core.AGENT_FIRST_RUN_FORMAT_VERSION,
    )
    manifest = core.prepare_source(
        run_dir,
        source_path,
        extra_assets=extra_assets,
        reference_images=reference_images,
    )
    return {
        "run_path": ".",
        "source": manifest,
        "resume": core.resume_run(run_dir, skill_root=SKILL_ROOT),
    }


def _require_v2_run(run_dir: Path | str) -> None:
    if core.inspect_run_format(run_dir) != core.AGENT_FIRST_RUN_FORMAT_VERSION:
        raise PosterContractError(
            "this command requires a Poster v2 run; use diagnose-v1 for legacy runs"
        )


def _load_active_catalog(run_dir: Path | str) -> dict[str, Any]:
    _require_v2_run(run_dir)
    return core.load_active_visual_catalog(run_dir)


def _catalog_assets(catalog: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    assets = catalog.get("assets")
    if not isinstance(assets, list):
        raise core.IntegrityError("reviewed catalog requires an assets list")
    by_id: dict[str, dict[str, Any]] = {}
    for raw in assets:
        if not isinstance(raw, Mapping):
            raise core.IntegrityError("reviewed catalog asset is invalid")
        asset = dict(raw)
        asset_id = asset.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id or asset_id in by_id:
            raise core.IntegrityError("reviewed catalog asset identity is invalid")
        if asset.get("trust") != "reviewed" or asset.get("eligible") is not True:
            raise core.IntegrityError("catalog contains an unreviewed eligible asset")
        roles = asset.get("roles")
        if (
            not isinstance(roles, list)
            or not roles
            or any(role not in POSTER_SOURCE_ROLES for role in roles)
        ):
            raise PosterContractError(f"catalog asset has invalid Poster roles: {asset_id}")
        by_id[asset_id] = asset
    return by_id


def _validate_reviewed_plan(
    plan: Mapping[str, Any], catalog: Mapping[str, Any]
) -> dict[str, Any]:
    by_id = _catalog_assets(catalog)
    allocations = [dict(item) for item in plan["visual_allocations"]]
    counts: dict[str, int] = {}
    for allocation in allocations:
        asset_id = allocation["visual_id"]
        role = allocation["role"]
        asset = by_id.get(asset_id)
        if asset is None:
            raise PosterContractError(f"plan references an unreviewed asset: {asset_id}")
        if role not in POSTER_SOURCE_ROLES or role not in asset["roles"]:
            raise PosterContractError(
                f"plan role is not permitted by the reviewed catalog: {asset_id}/{role}"
            )
        suffix = Path(str(asset.get("path") or "")).suffix.lower()
        if suffix not in SUPPORTED_IMAGE_SUFFIXES:
            raise PosterContractError(
                f"unsupported poster visual format: {suffix or '<none>'}"
            )
        counts[asset_id] = counts.get(asset_id, 0) + 1
        max_reuse = asset.get("max_reuse")
        if (
            not isinstance(max_reuse, int)
            or isinstance(max_reuse, bool)
            or counts[asset_id] > max_reuse
        ):
            raise PosterContractError(f"plan exceeds reviewed reuse limit: {asset_id}")

    story = catalog.get("source_story")
    if not isinstance(story, Mapping):
        raise core.IntegrityError("reviewed catalog source_story is invalid")
    allocated_ids = set(counts)
    statuses: dict[str, str] = {}
    for category in ("central_method", "primary_result"):
        item = story.get(category)
        if not isinstance(item, Mapping):
            raise core.IntegrityError(f"reviewed source_story is missing {category}")
        status = item.get("status")
        statuses[category] = str(status)
        if status == "covered":
            asset_ids = item.get("asset_ids")
            if (
                not isinstance(asset_ids, list)
                or not asset_ids
                or not allocated_ids.intersection(str(value) for value in asset_ids)
            ):
                raise PosterContractError(
                    f"poster plan must retain reviewed source evidence for {category}"
                )
        elif status == "not_applicable":
            if not str(item.get("rationale") or "").strip():
                raise core.IntegrityError(
                    f"reviewed not_applicable {category} requires rationale"
                )
        else:
            raise core.IntegrityError(
                f"reviewed source_story has invalid {category} status"
            )
    if not allocations:
        if set(statuses.values()) != {"not_applicable"}:
            raise PosterContractError(
                "a zero-visual plan requires reviewed not_applicable decisions for "
                "both central_method and primary_result"
            )
        if plan.get("no_visual_fallback") is None:
            raise PosterContractError(
                "a zero-visual plan requires an explicit native no_visual_fallback"
            )
    elif plan.get("no_visual_fallback") is not None:
        raise PosterContractError(
            "no_visual_fallback is allowed only for a reviewed zero-visual plan"
        )
    return {
        "reviewed_asset_count": len(by_id),
        "allocated_asset_count": len(allocated_ids),
        "source_story": statuses,
    }


def save_poster_plan(run_dir: Path | str, payload: Mapping[str, Any]) -> dict[str, Any]:
    plan = normalize_plan(payload)
    catalog = _load_active_catalog(run_dir)
    _validate_reviewed_plan(plan, catalog)
    source_visuals = _read_json_object(Path(run_dir) / "evidence" / "source_visuals.json")
    by_id = {
        str(item.get("id")): item
        for item in source_visuals.get("visuals", [])
        if isinstance(item, dict)
    }
    for reference_id in plan["style_reference_ids"]:
        item = by_id.get(reference_id)
        if item is None or item.get("eligibility") != "style_only":
            raise PosterContractError(f"style reference is not style-only: {reference_id}")
    allocated = {item["visual_id"] for item in plan["visual_allocations"]}
    overlap = allocated.intersection(plan["style_reference_ids"])
    if overlap:
        raise PosterContractError(f"style-only references cannot be content assets: {', '.join(sorted(overlap))}")
    return core.save_plan_revision(run_dir, plan)


def _load_plan(run_dir: Path | str) -> dict[str, Any]:
    return normalize_plan(core.load_active_plan(run_dir))


def _safe_catalog_path(run: Path, relative: str) -> Path:
    try:
        return core.safe_path(run, relative, must_exist=True)
    except core.PathSafetyError as error:
        raise PosterContractError(str(error)) from error


def begin_poster_attempt(run_dir: Path | str) -> dict[str, Any]:
    run = Path(run_dir).absolute()
    plan = _load_plan(run)
    state = _read_run(run)
    runtime_retry = (
        state.get("state") == "failed"
        and state.get("failure_origin") != "semantic_review"
        and isinstance(state.get("active_attempt"), str)
    )
    if (
        state.get("state") != "authoring"
        and not runtime_retry
        and int(state.get("attempt_count", 0)) >= plan["max_attempts"]
    ):
        raise PosterContractError("poster repair attempt budget is exhausted")
    attempt_id = core.begin_attempt(run)
    attempt = run / "attempts" / attempt_id
    artifact = attempt / "artifact"
    plan = normalize_plan(core.load_attempt_plan(run, attempt_id))
    catalog = core.load_attempt_visual_catalog(run, attempt_id)
    _validate_reviewed_plan(plan, catalog)
    by_id = _catalog_assets(catalog)
    attempt_context = _read_json_object(attempt / "attempt-context.json")
    authorized = {
        item["asset_id"]: item["sha256"]
        for item in attempt_context.get("authorized_assets", [])
        if isinstance(item, Mapping)
    }
    allocated_ids = {item["visual_id"] for item in plan["visual_allocations"]}
    if set(authorized) != allocated_ids:
        raise core.IntegrityError("attempt authorization differs from the plan snapshot")
    staged: list[dict[str, Any]] = []
    for allocation in plan["visual_allocations"]:
        visual = by_id[allocation["visual_id"]]
        source = _safe_catalog_path(run, str(visual.get("path") or ""))
        if source.stat().st_nlink != 1:
            raise core.PathSafetyError(f"source asset must not be hardlinked: {source}")
        suffix = source.suffix.lower()
        if suffix not in SUPPORTED_IMAGE_SUFFIXES:
            raise PosterContractError(f"unsupported poster visual format: {suffix or '<none>'}")
        if core.sha256_file(source) != visual.get("sha256") or visual.get("sha256") != authorized[allocation["visual_id"]]:
            raise core.IntegrityError(f"reviewed source asset hash mismatch: {allocation['visual_id']}")
        receipt_path = _safe_catalog_path(run, str(visual.get("receipt_path") or ""))
        if (
            receipt_path.stat().st_nlink != 1
            or core.sha256_file(receipt_path) != visual.get("receipt_file_sha256")
        ):
            raise core.IntegrityError(f"reviewed crop receipt mismatch: {allocation['visual_id']}")
        receipt = _read_json_object(receipt_path)
        if receipt.get("receipt_sha256") != visual.get("receipt_sha256"):
            raise core.IntegrityError(f"reviewed crop receipt binding mismatch: {allocation['visual_id']}")
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
                "source_path": str(visual["path"]),
                "receipt_path": str(visual["receipt_path"]),
                "receipt_sha256": str(visual["receipt_sha256"]),
                "page_path": str(receipt["page_path"]),
                "page_sha256": str(receipt["page_sha256"]),
                "max_reuse": int(visual["max_reuse"]),
            }
        )
    source_visuals = _read_json_object(run / "evidence" / "source_visuals.json")
    style_by_id = {
        str(item.get("id")): item
        for item in source_visuals.get("visuals", [])
        if isinstance(item, dict)
    }
    style_references: list[dict[str, str]] = []
    for reference_id in plan["style_reference_ids"]:
        visual = style_by_id.get(reference_id)
        if visual is None or visual.get("eligibility") != "style_only":
            raise PosterContractError(f"style reference is not style-only: {reference_id}")
        style_references.append(
            {
                "visual_id": reference_id,
                "run_relative_path": f"evidence/{visual['path']}",
                "transfer": "style_only",
            }
        )
    source_manifest = _read_json_object(run / "evidence" / "source_manifest.json")
    source_map_input = attempt / "source-map-input.json"
    context = {
        "format_version": FORMAT_VERSION,
        "attempt_id": attempt_id,
        "poster_path": "artifact/poster.html",
        "run_format_version": core.AGENT_FIRST_RUN_FORMAT_VERSION,
        "source_path": source_manifest["input_path"],
        "source_manifest_path": "evidence/source_manifest.json",
        "source_manifest_sha256": attempt_context["source_manifest_sha256"],
        "catalog_revision": attempt_context["catalog_revision"],
        "catalog_sha256": attempt_context["catalog_sha256"],
        "plan_revision": attempt_context["plan_revision"],
        "plan_sha256": attempt_context["plan_sha256"],
        "plan": plan,
        "reviewed_coverage": _validate_reviewed_plan(plan, catalog),
        "staged_content_visuals": staged,
        "style_references": style_references,
        "source_flow_guidance": (
            "Keep each reviewed source image, its caption, and its explanatory "
            "text in one .source-flow-unit with a visible gutter; native diagrams "
            "or tables may explain but must not replace essential source evidence."
        ),
        "next_command": shlex.join(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "validate",
                "--run-dir",
                str(run),
                "--attempt",
                attempt_id,
                "--source-map",
                str(source_map_input),
            ]
        ),
    }
    core.atomic_write_json(attempt / "authoring-context.json", context)
    return {
        "attempt_id": attempt_id,
        "attempt_path": f"attempts/{attempt_id}",
        "poster_path": f"attempts/{attempt_id}/artifact/poster.html",
        "authoring_context": f"attempts/{attempt_id}/authoring-context.json",
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


def _validate_svg_sidecar(path: Path, relative: str) -> list[str]:
    """Reject executable or externally dependent SVG sidecars."""

    if path.stat().st_size > 8 * 1024 * 1024:
        return [f"SVG dependency exceeds the 8 MiB limit: {relative}"]
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeError:
        return [f"SVG dependency is not UTF-8: {relative}"]
    if re.search(r"(?is)<!doctype|<!entity", raw):
        return [f"SVG dependency contains a document type or entity: {relative}"]
    errors: list[str] = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return [*errors, f"SVG dependency is malformed: {relative}"]
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        errors.append(f"SVG dependency has a non-SVG root: {relative}")
    forbidden_tags = {"foreignobject", "iframe", "object", "script"}
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag in forbidden_tags:
            errors.append(f"SVG dependency contains forbidden {tag}: {relative}")
        for raw_name, value in element.attrib.items():
            name = raw_name.rsplit("}", 1)[-1].lower()
            lowered = value.strip().lower()
            if name.startswith("on"):
                errors.append(f"SVG dependency contains an event handler: {relative}")
            if name in {"href", "src"} and value.strip() and not value.strip().startswith("#"):
                errors.append(f"SVG dependency contains an external reference: {relative}")
            if "javascript:" in lowered or re.search(r"(?i)url\(\s*(?!['\"]?#)", value):
                errors.append(f"SVG dependency contains an unsafe URL: {relative}")
        css_fragments = []
        if tag == "style" and element.text:
            css_fragments.append(element.text)
        if element.attrib.get("style"):
            css_fragments.append(element.attrib["style"])
        for css_fragment in css_fragments:
            if re.search(r"(?i)@import|expression\s*\(|javascript:", css_fragment):
                errors.append(f"SVG dependency contains unsafe CSS: {relative}")
            for match in _CSS_URL_RE.finditer(css_fragment):
                target = match.group(2).strip()
                if target and not target.startswith("#"):
                    errors.append(f"SVG dependency contains unsafe CSS: {relative}")
    return list(dict.fromkeys(errors))


def lint_poster_html(
    html_path: Path | str,
    *,
    artifact_root: Path | str,
    plan: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]],
    evidence_ids: Sequence[str] = (),
    visual_catalog: Sequence[Mapping[str, Any]] = (),
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
        if (
            element.tag == "meta"
            and element.attrs.get("http-equiv", "").strip().lower() == "refresh"
        ):
            unsafe.append("meta refresh navigation is forbidden")
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
    for match in _CSS_CONTENT_RE.finditer(css):
        value = re.sub(r"(?i)\s*!important\s*$", "", match.group(1)).strip()
        if value.lower() not in {"none", "normal", '""', "''"} and not re.fullmatch(
            r'(?:"\s*"|\'\s*\')', value
        ):
            unsafe.append("visible CSS generated content is forbidden")
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
    allocations = [dict(item) for item in normalized_plan["visual_allocations"]]
    allocated_ids = [str(item["visual_id"]) for item in allocations]
    visuals_by_id = {
        str(item.get("id") or ""): dict(item)
        for item in visual_catalog
        if isinstance(item, Mapping) and item.get("id")
    }
    used_visual_ids: list[str] = []
    for element in parser.elements:
        claim_id = element.attrs.get("data-claim-id")
        if claim_id:
            claimed_elements.setdefault(claim_id, []).append(element)
        if element.tag in {"section", "article"} and not _split_ids(element.attrs.get("data-source-ids", "")):
            binding_errors.append(f"{element.tag} is missing data-source-ids")
        if element.tag == "img":
            visual_id = element.attrs.get("data-source-id", "").strip()
            if not visual_id:
                binding_errors.append("source image is missing data-source-id")
                continue
            used_visual_ids.append(visual_id)
            if visual_id not in allocated_ids:
                binding_errors.append(f"source image is not allocated: {visual_id}")
                continue
            visual = visuals_by_id.get(visual_id)
            if visual is None or visual.get("eligibility") != "eligible":
                binding_errors.append(f"source image is absent or ineligible: {visual_id}")
                continue
            catalog_path = str(visual.get("path") or "")
            suffix = Path(catalog_path).suffix.lower()
            expected_src = f"assets/{visual_id}{suffix}"
            actual_src = unquote(urlsplit(element.attrs.get("src", "")).path)
            if actual_src != expected_src:
                binding_errors.append(
                    f"source image path does not match its staged allocation: {visual_id}"
                )
                continue
            try:
                staged = core.safe_path(artifact, expected_src, must_exist=True)
                staged_hash = core.sha256_file(staged)
            except core.PortableError:
                binding_errors.append(f"source image staged path is unsafe: {visual_id}")
                continue
            if staged_hash != visual.get("sha256"):
                binding_errors.append(f"source image hash does not match its catalog: {visual_id}")
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
        expected_text = _normalize_space(str(claim.get("text") or "")).casefold()
        actual_text = element.visible_text().casefold()
        if actual_text != expected_text:
            binding_errors.append(
                f"visible claim text must exactly match its source map: {claim_id}"
            )
    missing_claims = sorted(set(by_claim) - set(claimed_elements))
    if missing_claims:
        binding_errors.append(f"claims missing from poster: {', '.join(missing_claims)}")
    for element in parser.elements:
        source_ids = _split_ids(element.attrs.get("data-source-ids", ""))
        unknown_ids = sorted(set(source_ids) - known_evidence_ids)
        if unknown_ids:
            binding_errors.append(f"unknown evidence IDs in HTML: {', '.join(unknown_ids)}")
    mapped_numbers: set[str] = set()
    for claim in claim_values:
        mapped_numbers.update(core._numbers(str(claim.get("text") or "")))
    visible_body_numbers: set[str] = set()
    for element in parser.elements:
        if element.tag in {"article", "section"}:
            visible_body_numbers.update(core._numbers(element.visible_text()))
    unsupported_numbers = sorted(visible_body_numbers - mapped_numbers)
    if unsupported_numbers:
        binding_errors.append(
            f"unsupported visible numbers outside the source map: {', '.join(unsupported_numbers)}"
        )
    missing_visuals = sorted(set(allocated_ids) - set(used_visual_ids))
    if missing_visuals:
        binding_errors.append(f"allocated source images are missing: {', '.join(missing_visuals)}")
    repeated_visuals = sorted(
        visual_id for visual_id in set(used_visual_ids) if used_visual_ids.count(visual_id) > allocated_ids.count(visual_id)
    )
    if repeated_visuals:
        binding_errors.append(f"source images exceed planned reuse: {', '.join(repeated_visuals)}")
    checks.append(_check("source_bindings", not binding_errors, "; ".join(dict.fromkeys(binding_errors)) or "visible claims and source IDs are bound"))

    local_errors: list[str] = []
    referenced_files: set[str] = set()

    def record_local_url(url: str, *, reference_kind: str) -> None:
        error = _validate_local_url(url, html_path=html, artifact_root=artifact)
        if error:
            local_errors.append(error)
            return
        value = unquote(url.strip())
        if not value or value.startswith("#"):
            return
        relative = Path(urlsplit(value).path.replace("\\", "/"))
        resolved = (html.parent / relative).resolve(strict=True)
        resolved_relative = resolved.relative_to(artifact.resolve(strict=True)).as_posix()
        suffix = resolved.suffix.lower()
        if suffix in SUPPORTED_IMAGE_SUFFIXES and reference_kind != "img":
            local_errors.append(
                f"CSS image and non-img image references are forbidden; use an allocated img: {resolved_relative}"
            )
        elif suffix in SUPPORTED_FONT_SUFFIXES and reference_kind != "css":
            local_errors.append(f"font dependencies must be referenced from CSS: {resolved_relative}")
        referenced_files.add(resolved_relative)

    for element in parser.elements:
        for name, value in element.attrs.items():
            values: list[str] = []
            if name in _URL_ATTRS:
                values = [value]
            elif name == "srcset":
                values = [part.strip().split()[0] for part in value.split(",") if part.strip()]
                if values:
                    local_errors.append("poster images must not use srcset; use one allocated img src")
            for url in values:
                record_local_url(
                    url,
                    reference_kind="img" if element.tag == "img" and name == "src" else "html",
                )
    for match in _CSS_URL_RE.finditer(css):
        record_local_url(match.group(2), reference_kind="css")
    for element in parser.elements:
        for match in _CSS_URL_RE.finditer(element.attrs.get("style", "")):
            record_local_url(match.group(2), reference_kind="css")
    actual_files: set[str] = set()
    for path in artifact.rglob("*"):
        if path.is_symlink():
            local_errors.append(f"symlinked artifact file: {path.name}")
        elif path.is_file() and path != html:
            relative = path.relative_to(artifact).as_posix()
            actual_files.add(relative)
            suffix = path.suffix.lower()
            if suffix not in SUPPORTED_SIDECAR_SUFFIXES:
                local_errors.append(f"unsupported artifact dependency: {relative}")
            elif suffix == ".svg":
                local_errors.extend(_validate_svg_sidecar(path, relative))
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


def _clear_generated_attempt_outputs(attempt: Path) -> None:
    """Remove only harness-owned outputs before a fresh deterministic pass."""

    for relative in (
        "artifact/poster.pdf",
        "artifact/preview.png",
        "qa/poster-output.json",
        "qa/deterministic.json",
        "qa/previews/audit.json",
        "qa/previews/poster.png",
        "qa/previews/poster-print.png",
    ):
        target = core.safe_path(attempt, relative)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    artifact = core.safe_path(attempt, "artifact", must_exist=True)
    for pattern in (".poster.pdf.tmp-*", ".preview.png.tmp-*"):
        for stale in artifact.glob(pattern):
            target = core.safe_path(attempt, f"artifact/{stale.name}", must_exist=True)
            if not target.is_file():
                raise core.PathSafetyError(f"stale render output is not a regular file: {target}")
            target.unlink()
    core.safe_path(attempt, "qa/previews").mkdir(parents=True, exist_ok=True)


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
            page.emulate_media(media="screen", reduced_motion="reduce")
            page.evaluate("document.fonts ? document.fonts.ready : Promise.resolve()")
            if blocked:
                raise PosterContractError("network or outside-artifact request was blocked during PDF export")
            screen_typography = page.evaluate(_COMPUTED_TYPOGRAPHY_SCRIPT)
            page.emulate_media(media="print", reduced_motion="reduce")
            page.evaluate("document.fonts ? document.fonts.ready : Promise.resolve()")
            print_typography = page.evaluate(_COMPUTED_TYPOGRAPHY_SCRIPT)
            pdf_bytes = page.pdf(
                width=f"{args.width_mm:.4f}mm",
                height=f"{args.height_mm:.4f}mm",
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                print_background=True,
                prefer_css_page_size=True,
            )
            if blocked:
                raise PosterContractError("network or outside-artifact request was blocked during PDF export")
            core.atomic_write_bytes(output, pdf_bytes)
        finally:
            context.close()
            browser.close()
    print(
        json.dumps(
            {
                "status": "ok",
                "blocked_requests": blocked,
                "computed_typography": {
                    "passed": screen_typography.get("passed") is True
                    and print_typography.get("passed") is True,
                    "violations": [
                        *[f"screen: {item}" for item in screen_typography.get("violations", [])],
                        *[f"print: {item}" for item in print_typography.get("violations", [])],
                    ],
                    "measurements": {
                        "screen": screen_typography.get("measurements", {}),
                        "print": print_typography.get("measurements", {}),
                    },
                },
            },
            sort_keys=True,
        )
    )
    return 0


def _export_pdf(
    runtime: setup_browser.BrowserRuntime,
    *,
    html: Path,
    artifact_root: Path,
    pdf: Path,
    width_mm: float,
    height_mm: float,
) -> dict[str, Any]:
    bootstrap = (
        "import sys;"
        f"sys.path.insert(0,{str(SCRIPT_DIR)!r});"
        "import poster_harness;"
        "raise SystemExit(poster_harness._browser_export_main(sys.argv[1:]))"
    )
    command = [
        str(runtime.python_executable),
        "-I",
        "-B",
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
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise PosterContractError("poster PDF export returned invalid diagnostics") from error
    typography = payload.get("computed_typography") if isinstance(payload, dict) else None
    if (
        not isinstance(typography, dict)
        or not isinstance(typography.get("passed"), bool)
        or not isinstance(typography.get("violations"), list)
    ):
        raise PosterContractError("poster PDF export omitted computed typography diagnostics")
    return payload


def _rasterize_pdf_preview(
    pdf: Path,
    output: Path,
    *,
    width_px: int,
    height_px: int,
) -> None:
    """Render the reviewed preview from the actual one-page PDF bytes."""

    executable = shutil.which("pdftoppm")
    if not executable:
        raise PosterContractError("pdftoppm is required to review the exported poster PDF")
    output = _safe_worker_output(output, output.parents[2])
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = output.with_name(f".{output.stem}.tmp-{uuid.uuid4().hex}")
    temporary = Path(f"{prefix}.png")
    try:
        completed = subprocess.run(
            [
                executable,
                "-png",
                "-f",
                "1",
                "-l",
                "1",
                "-singlefile",
                "-scale-to-x",
                str(width_px),
                "-scale-to-y",
                str(height_px),
                str(pdf),
                str(prefix),
            ],
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0 or not temporary.is_file():
            detail = core.redact_secrets((completed.stderr or completed.stdout).strip())
            raise PosterContractError(f"poster PDF preview rendering failed: {detail}")
        core.atomic_write_bytes(output, temporary.read_bytes())
    finally:
        temporary.unlink(missing_ok=True)


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
    pdf = artifact / "poster.pdf"
    export_report = _export_pdf(
        runtime,
        html=html,
        artifact_root=artifact,
        pdf=pdf,
        width_mm=plan["print"]["width_mm"],
        height_mm=plan["print"]["height_mm"],
    )
    typography = export_report["computed_typography"]
    typography_check = _check(
        "computed_typography",
        typography["passed"] is True,
        "rendered poster typography meets every minimum"
        if typography["passed"] is True
        else "; ".join(str(item) for item in typography["violations"]),
        measurements=typography.get("measurements", {}),
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
    print_preview = preview_dir / "poster-print.png"
    _rasterize_pdf_preview(
        pdf,
        print_preview,
        width_px=int(canvas["width_px"]),
        height_px=int(canvas["height_px"]),
    )
    core.atomic_write_bytes(artifact / "preview.png", print_preview.read_bytes())
    checks = [typography_check, pdf_check]
    result = {
        "format_version": FORMAT_VERSION,
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
        "preview_paths": {
            "poster_pdf": "qa/previews/poster-print.png",
        },
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


def _dom_audit_checks(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    findings = report.get("findings")
    if not isinstance(findings, list):
        raise PosterContractError("Poster DOM audit omitted its deterministic findings")
    checks: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            raise PosterContractError("Poster DOM audit finding must be an object")
        code = str(finding.get("code") or "")
        minimum_route = POSTER_FINDING_MINIMUM_ROUTE.get(code)
        if code not in poster_dom_audit.STABLE_FINDING_CODES or minimum_route is None:
            raise PosterContractError(f"unknown Poster DOM finding code: {code}")
        if finding.get("suggested_repair_route") != "layout_repair":
            raise PosterContractError(
                f"Poster DOM finding {code} must suggest layout_repair"
            )
        geometry = finding.get("geometry")
        if not isinstance(geometry, Mapping):
            raise PosterContractError(f"Poster DOM finding {code} has invalid geometry")
        checks.append(
            _check(
                code,
                False,
                str(finding.get("message") or "Rendered Poster DOM defect detected."),
                code=code,
                block_id=str(finding.get("block_id") or "paper-poster-root"),
                severity=str(finding.get("severity") or "P1"),
                geometry=dict(geometry),
                minimum_route=minimum_route,
            )
        )
    if checks:
        return checks
    passed = report.get("passed") is True and report.get("artifact_unchanged") is True
    return [
        _check(
            "poster_dom_audit",
            passed,
            "read-only screen and print DOM audit passed"
            if passed
            else "read-only Poster DOM audit reported blocked browser diagnostics",
            report="qa/dom-audit.json",
        )
    ]


def validate_poster_attempt(
    run_dir: Path | str,
    attempt_id: str,
    *,
    source_map_path: Path | str,
    cache_root: Path | None = None,
    allow_browser_install: bool = True,
) -> dict[str, Any]:
    run = Path(run_dir).absolute()
    _require_v2_run(run)
    core.resume_run(run, skill_root=SKILL_ROOT)
    state = _read_run(run)
    if state.get("state") != "authoring" or state.get("active_attempt") != attempt_id:
        raise PosterContractError("validation must target the active authoring attempt")
    plan = normalize_plan(core.load_attempt_plan(run, attempt_id))
    attempt = core.safe_path(run / "attempts", attempt_id, must_exist=True)
    artifact = attempt / "artifact"
    source_map_input = _read_json_object(source_map_path)
    if set(source_map_input) != {"claims"} or not isinstance(source_map_input["claims"], list):
        raise PosterContractError("source-map input must contain only a claims list")
    claim_ids: list[str] = []
    for raw_claim in source_map_input["claims"]:
        if not isinstance(raw_claim, Mapping):
            raise PosterContractError("source-map claims must be objects")
        claim_id = raw_claim.get("id")
        if not isinstance(claim_id, str) or not claim_id.strip():
            raise PosterContractError(
                "source-map claim IDs must be non-empty strings"
            )
        claim_ids.append(claim_id)
    if len(set(claim_ids)) != len(claim_ids):
        raise PosterContractError("source-map claim IDs must be unique")
    planned_claim_ids = {
        claim_id
        for section in plan["narrative"]
        for claim_id in section["claim_ids"]
    }
    if set(claim_ids) != planned_claim_ids:
        raise PosterContractError(
            "source-map claim IDs do not match the attempt plan"
        )
    _clear_generated_attempt_outputs(attempt)
    core.write_source_map(run, attempt_id, source_map_input["claims"])
    reviewed_catalog = core.load_attempt_visual_catalog(run, attempt_id)
    _validate_reviewed_plan(plan, reviewed_catalog)
    visual_catalog = [
        {
            "id": asset["asset_id"],
            "path": asset["path"],
            "sha256": asset["sha256"],
            "eligibility": "eligible",
        }
        for asset in _catalog_assets(reviewed_catalog).values()
    ]
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
        visual_catalog=visual_catalog,
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
            for frame_id, relative in rendered.get("preview_paths", {}).items():
                if (attempt / relative).is_file():
                    preview_paths[str(frame_id)] = str(relative)
            try:
                dom_report = run_poster_dom_audit(
                    run,
                    attempt_id,
                    cache_root=cache_root,
                    allow_browser_install=allow_browser_install,
                )
            except (
                OSError,
                subprocess.SubprocessError,
                core.PortableError,
                setup_browser.BrowserRuntimeError,
            ) as error:
                checks.append(
                    _check(
                        "poster_dom_audit",
                        False,
                        str(core.redact_secrets(str(error))),
                    )
                )
            else:
                checks.extend(_dom_audit_checks(dom_report))
                dom_screen = attempt / "qa" / "previews" / "dom-screen.png"
                if dom_screen.is_file():
                    preview_paths["poster_screen"] = "qa/previews/dom-screen.png"
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
    plan = normalize_plan(core.load_attempt_plan(run_dir, attempt_id))
    catalog = core.load_attempt_visual_catalog(run_dir, attempt_id)
    _validate_reviewed_plan(plan, catalog)
    return core.create_review_context(run_dir, attempt_id, rubric=REVIEW_RUBRIC)


def _validate_poster_route_review(review: Mapping[str, Any]) -> None:
    route = review.get("repair_route")
    if route is not None and route not in core.REPAIR_ROUTE_ORDER:
        raise PosterContractError(f"invalid Poster repair route: {route}")
    findings = review.get("route_findings")
    if not isinstance(findings, list):
        raise PosterContractError("poster review route_findings must be a list")
    for raw in findings:
        if not isinstance(raw, Mapping):
            raise PosterContractError("poster review route finding must be an object")
        code = raw.get("code")
        minimum = POSTER_FINDING_MINIMUM_ROUTE.get(str(code))
        if minimum is None:
            raise PosterContractError(f"unknown Poster finding code: {code}")
        if raw.get("minimum_route") != minimum:
            raise PosterContractError(
                f"Poster finding {code} requires minimum route {minimum}"
            )
        if route is not None and core.REPAIR_ROUTE_ORDER[route] < core.REPAIR_ROUTE_ORDER[minimum]:
            raise PosterContractError(
                f"Poster repair route {route} downgrades finding {code}"
            )


def _load_attempt_semantic_review(
    run_dir: Path | str, attempt_id: str
) -> dict[str, Any] | None:
    path = Path(run_dir) / "attempts" / attempt_id / "qa" / "semantic-review.json"
    if not path.exists():
        return None
    review = _read_json_object(path)
    _validate_poster_route_review(review)
    return review


def record_poster_review(
    run_dir: Path | str,
    attempt_id: str,
    review_path: Path | str | Mapping[str, Any],
) -> dict[str, Any]:
    plan = normalize_plan(core.load_attempt_plan(run_dir, attempt_id))
    catalog = core.load_attempt_visual_catalog(run_dir, attempt_id)
    _validate_reviewed_plan(plan, catalog)
    review = (
        dict(review_path)
        if isinstance(review_path, Mapping)
        else _read_json_object(review_path)
    )
    _validate_poster_route_review(review)
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


def reopen_poster_curation(
    run_dir: Path | str, request: Mapping[str, Any]
) -> dict[str, Any]:
    attempt_id = request.get("attempt_id")
    if not isinstance(attempt_id, str):
        raise PosterContractError("reopen request requires an attempt_id")
    core.load_attempt_plan(run_dir, attempt_id)
    core.load_attempt_visual_catalog(run_dir, attempt_id)
    if _load_attempt_semantic_review(run_dir, attempt_id) is None:
        raise PosterContractError("reopen curation requires a persisted semantic review")
    return core.reopen_curation(run_dir, request)


def finalize_poster_attempt(run_dir: Path | str, attempt_id: str) -> dict[str, Any]:
    plan = normalize_plan(core.load_attempt_plan(run_dir, attempt_id))
    catalog = core.load_attempt_visual_catalog(run_dir, attempt_id)
    _validate_reviewed_plan(plan, catalog)
    review = _load_attempt_semantic_review(run_dir, attempt_id)
    if review is None:
        raise PosterContractError("finalization requires a persisted semantic review")
    return core.finalize_attempt(run_dir, attempt_id)


def _poster_authoring_complete(run_dir: Path | str, attempt_id: str) -> bool:
    poster = Path(run_dir) / "attempts" / attempt_id / "artifact" / "poster.html"
    if not poster.exists():
        return False
    if poster.is_symlink() or not poster.is_file() or poster.stat().st_nlink != 1:
        raise core.PathSafetyError(f"authored poster must be a regular file: {poster}")
    return poster.stat().st_size > 0


def resume_poster_run(run_dir: Path | str) -> dict[str, Any]:
    _require_v2_run(run_dir)
    persisted_state = _read_run(run_dir)
    persisted_attempt = persisted_state.get("active_attempt")
    if isinstance(persisted_attempt, str):
        _load_attempt_semantic_review(run_dir, persisted_attempt)
    state = core.resume_run(run_dir, skill_root=SKILL_ROOT)
    attempt_id = state.get("active_attempt")
    if isinstance(attempt_id, str):
        plan = normalize_plan(core.load_attempt_plan(run_dir, attempt_id))
        catalog = core.load_attempt_visual_catalog(run_dir, attempt_id)
        _validate_reviewed_plan(plan, catalog)
        _load_attempt_semantic_review(run_dir, attempt_id)
        if state.get("state") == "authoring" and not _poster_authoring_complete(
            run_dir, attempt_id
        ):
            state = {**state, "next_action": "author"}
    return state


def inspect_poster_source(run_dir: Path | str) -> dict[str, Any]:
    _require_v2_run(run_dir)
    return core.inspect_source(run_dir)


def crop_poster_source(
    run_dir: Path | str, request: Mapping[str, Any]
) -> dict[str, Any]:
    role = request.get("role")
    if role not in POSTER_SOURCE_ROLES:
        raise PosterContractError(f"unsupported Poster source role: {role}")
    return core.crop_source(run_dir, request)


def create_poster_source_review_context(
    run_dir: Path | str, selection: Mapping[str, Any]
) -> dict[str, Any]:
    assets = selection.get("assets")
    if not isinstance(assets, list):
        raise PosterContractError("source review selection requires an assets list")
    for item in assets:
        roles = item.get("roles") if isinstance(item, Mapping) else None
        if (
            not isinstance(roles, list)
            or not roles
            or any(role not in POSTER_SOURCE_ROLES for role in roles)
        ):
            raise PosterContractError("source review selection has invalid Poster roles")
    return core.create_source_review_context(run_dir, selection)


def record_poster_source_review(
    run_dir: Path | str,
    context_path: Path | str,
    review: Mapping[str, Any],
) -> dict[str, Any]:
    return core.record_source_review(run_dir, context_path, review)


def run_poster_dom_audit(
    run_dir: Path | str,
    attempt_id: str,
    *,
    cache_root: Path | None = None,
    allow_browser_install: bool = True,
) -> dict[str, Any]:
    return poster_dom_audit.run_poster_dom_audit(
        run_dir,
        attempt_id,
        cache_root=cache_root,
        allow_browser_install=allow_browser_install,
    )


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


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        _print_json({"status": "error", "error": message})
        raise SystemExit(1)


def _build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description=__doc__)
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
    inspect = subparsers.add_parser("inspect-source", help="inspect immutable PDF pages and untrusted hints")
    inspect.add_argument("--run-dir", type=Path, required=True)
    crop = subparsers.add_parser("crop-source", help="derive an immutable crop from the source PDF")
    crop.add_argument("--run-dir", type=Path, required=True)
    crop.add_argument("--request", type=Path, required=True)
    assets = subparsers.add_parser("list-source-assets", help="list unreviewed crops and extraction hints")
    assets.add_argument("--run-dir", type=Path, required=True)
    source_context = subparsers.add_parser("source-review-context", help="create a hash-bound source-review context")
    source_context.add_argument("--run-dir", type=Path, required=True)
    source_context.add_argument("--selection", type=Path, required=True)
    source_record = subparsers.add_parser("record-source-review", help="record a fresh source review")
    source_record.add_argument("--run-dir", type=Path, required=True)
    source_record.add_argument("--context", required=True)
    source_record.add_argument("--review", type=Path, required=True)
    plan = subparsers.add_parser("plan", help="save the evidence-grounded poster plan")
    plan.add_argument("--run-dir", type=Path, required=True)
    plan.add_argument("--plan", type=Path, required=True)
    begin = subparsers.add_parser("begin-attempt", help="start a bounded authoring attempt")
    begin.add_argument("--run-dir", type=Path, required=True)
    dom = subparsers.add_parser("dom-audit", help="run the strictly read-only Poster DOM audit")
    dom.add_argument("--run-dir", type=Path, required=True)
    dom.add_argument("--attempt", required=True)
    dom.add_argument("--cache-root", type=Path)
    dom.add_argument("--offline-browser", action="store_true")
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
    reopen = subparsers.add_parser("reopen-curation", help="authorize reviewed replan or source reingest")
    reopen.add_argument("--run-dir", type=Path, required=True)
    reopen.add_argument("--request", type=Path, required=True)
    finalize = subparsers.add_parser("finalize", help="promote one reviewed attempt atomically")
    finalize.add_argument("--run-dir", type=Path, required=True)
    finalize.add_argument("--attempt", required=True)
    resume = subparsers.add_parser("resume", help="verify hashes and report the next safe action")
    resume.add_argument("--run-dir", type=Path, required=True)
    diagnose = subparsers.add_parser("diagnose-v1", help="read legacy run metadata without mutation")
    diagnose.add_argument("--run-dir", type=Path, required=True)
    return parser


def _command_exit_code(result: Mapping[str, Any]) -> int:
    pending: list[Mapping[str, Any]] = [result]
    while pending:
        value = pending.pop()
        if value.get("status") in {"blocked", "failed"}:
            return 2
        if value.get("passed") is False or value.get("ready") is False:
            return 2
        if value.get("verdict") == "fail" or value.get("state") in {"blocked", "failed"}:
            return 2
        pending.extend(item for item in value.values() if isinstance(item, Mapping))
    return 0


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
            _require_v2_run(args.run_dir)
            result = {"results": core.lexical_retrieve(
                core.load_evidence(args.run_dir), args.query, limit=args.limit
            )}
        elif args.command == "inspect-source":
            result = inspect_poster_source(args.run_dir)
        elif args.command == "crop-source":
            result = crop_poster_source(
                args.run_dir, _read_canonical_json_object(args.request)
            )
        elif args.command == "list-source-assets":
            _require_v2_run(args.run_dir)
            result = core.list_source_assets(args.run_dir)
        elif args.command == "source-review-context":
            result = create_poster_source_review_context(
                args.run_dir, _read_canonical_json_object(args.selection)
            )
        elif args.command == "record-source-review":
            result = record_poster_source_review(
                args.run_dir,
                args.context,
                _read_canonical_json_object(args.review),
            )
        elif args.command == "plan":
            result = save_poster_plan(
                args.run_dir, _read_canonical_json_object(args.plan)
            )
        elif args.command == "begin-attempt":
            result = begin_poster_attempt(args.run_dir)
        elif args.command == "dom-audit":
            result = run_poster_dom_audit(
                args.run_dir,
                args.attempt,
                cache_root=args.cache_root,
                allow_browser_install=not args.offline_browser,
            )
        elif args.command == "validate":
            result = validate_poster_attempt(
                args.run_dir,
                args.attempt,
                source_map_path=args.source_map,
                cache_root=args.cache_root,
                allow_browser_install=not args.offline_browser,
            )
        elif args.command == "review-context":
            result = create_poster_review_context(args.run_dir, args.attempt)
        elif args.command == "record-review":
            result = record_poster_review(
                args.run_dir,
                args.attempt,
                _read_canonical_json_object(args.review),
            )
        elif args.command == "reopen-curation":
            result = reopen_poster_curation(
                args.run_dir, _read_canonical_json_object(args.request)
            )
        elif args.command == "finalize":
            result = finalize_poster_attempt(args.run_dir, args.attempt)
        elif args.command == "resume":
            result = resume_poster_run(args.run_dir)
        elif args.command == "diagnose-v1":
            result = core.diagnose_v1_run(args.run_dir)
        else:  # pragma: no cover - argparse owns command dispatch
            parser.error(f"unknown command: {args.command}")
        _print_json(result)
        return _command_exit_code(result)
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError, core.PortableError, setup_browser.BrowserRuntimeError) as error:
        _print_json(
            {
                "status": "error",
                "error": str(core.redact_secrets(str(error))),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
