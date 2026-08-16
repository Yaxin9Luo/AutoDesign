#!/usr/bin/env python3
"""Standalone, source-grounded research-webpage quality harness."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _portable as portable  # noqa: E402
import setup_browser  # noqa: E402


FORMAT_VERSION = 1
DEFAULT_RELEASE_VERSION = "0.1.0"
REQUIRED_SECTION_ROLES = (
    "identity",
    "abstract",
    "method",
    "evidence",
    "results",
    "limitations",
    "resources",
    "citation",
)
ALLOWED_MISSING_METADATA = {
    "authors",
    "affiliations",
    "venue",
    "date",
    "paper_url",
    "code_url",
    "data_url",
    "citation",
    "license",
}
ALLOWED_INTERACTION_KINDS = {"inspect", "compare", "navigate"}
ALLOWED_STATE_ATTRIBUTES = {
    "aria-current",
    "aria-expanded",
    "aria-pressed",
    "aria-selected",
    "data-active",
    "data-state",
}
ALLOWED_VISUAL_SUFFIXES = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
ASSET_ATTRIBUTES = (
    ("img", "src"),
    ("input", "src"),
    ("source", "src"),
    ("track", "src"),
    ("video", "src"),
    ("video", "poster"),
    ("audio", "src"),
    ("script", "src"),
    ("link", "href"),
    ("image", "href"),
    ("image", "xlink:href"),
    ("use", "href"),
    ("use", "xlink:href"),
)
RUBRIC = {
    "format_version": FORMAT_VERSION,
    "artifact_type": "research_webpage",
    "pass_policy": {
        "minimum_dimension": 3,
        "minimum_mean": 4.0,
        "required_four_or_better": ["source_fidelity", "anti_slop"],
    },
    "dimensions": [
        "source_fidelity",
        "research_narrative",
        "visual_hierarchy",
        "typography",
        "evidence_use",
        "interaction_utility",
        "accessibility_responsive",
        "anti_slop",
    ],
}

_CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
_MOTION = re.compile(
    r"(?:^|[;{])\s*(animation(?:-[\w-]+)?|transition(?:-[\w-]+)?|scroll-behavior)\s*:\s*([^;}]+)",
    re.IGNORECASE,
)
_NETWORK_JS = re.compile(
    r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|WebTransport|RTCPeerConnection)\b"
    r"|\bsendBeacon\s*\(",
    re.IGNORECASE,
)
_DYNAMIC_NAVIGATION_JS = re.compile(
    r"\b(?:(?:window|document|top|parent|self)\s*\.\s*)?location"
    r"(?:\s*\.\s*href)?\s*="
    r"|\b(?:(?:window|document|top|parent|self)\s*\.\s*)?location"
    r"\s*\.\s*(?:assign|replace|reload)\s*\("
    r"|\bwindow\s*\.\s*open\s*\(",
    re.IGNORECASE,
)
_REVEAL_JS = re.compile(
    r"\bIntersectionObserver\b|\.classList\.(?:add|remove|toggle)\s*\(",
    re.IGNORECASE,
)
_HIDDEN_REVEAL_CSS = re.compile(
    r"[^{}]*(?:reveal|animate|in-view|inview|fade-in)[^{}]*\{[^{}]*"
    r"(?:opacity\s*:\s*0(?:\D|$)|visibility\s*:\s*hidden\b|display\s*:\s*none\b)",
    re.IGNORECASE | re.DOTALL,
)
_MARKETING_PATTERNS = (
    re.compile(r"\bbook a demo\b", re.IGNORECASE),
    re.compile(r"\bstart (?:your )?free trial\b", re.IGNORECASE),
    re.compile(r"\bjoin (?:the )?waitlist\b", re.IGNORECASE),
    re.compile(r"\bunlock the power\b", re.IGNORECASE),
    re.compile(r"\btransform your workflow\b", re.IGNORECASE),
)
_VISIBLE_URL = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_VISIBLE_NUMBER = re.compile(r"(?<![\w])[-+]?\d[\d,]*(?:\.\d+)?(?:\s*[%‰])?")
_VISIBLE_FORMULA = re.compile(
    r"(?:\\\(|\\\[|\$[^$\n]+\$|\\(?:frac|sum|prod|sqrt|int)\b|"
    r"[∑∏√∫≈≠≤≥±]|\b[A-Za-z][A-Za-z0-9_]*\s*(?:=|<=|>=|<|>)\s*[^,.;:]+|"
    r"\^[{]?[-+]?\d)",
    re.IGNORECASE,
)
_GENERATED_ARTIFACT_REPORTS = {
    "browser-audit.json",
    "interaction-audit.json",
    "webpage-validation.json",
}


class WebpageHarnessError(RuntimeError):
    """Base error for standalone webpage authoring."""


class WebpageContractError(WebpageHarnessError):
    """The plan, artifact, or review violates the webpage contract."""


class WebpageBlockedError(WebpageHarnessError):
    """A required local runtime is unavailable and validation cannot proceed."""


class _Node:
    def __init__(
        self,
        tag: str,
        attrs: Mapping[str, str],
        parent: "_Node | None",
        line: int,
    ) -> None:
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children: list[_Node] = []
        self.data: list[str] = []
        self.line = line

    def descendants(self) -> Iterable["_Node"]:
        for child in self.children:
            yield child
            yield from child.descendants()

    def text(self, *, omit_tags: set[str] | None = None) -> str:
        omitted = omit_tags or set()
        parts = list(self.data)
        for child in self.children:
            if child.tag not in omitted:
                parts.append(child.text(omit_tags=omitted))
        return " ".join(" ".join(parts).split())


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document", {}, None, 0)
        self.stack = [self.root]
        self.doctype = False
        self.parse_errors: list[str] = []
        self.duplicate_attributes: list[dict[str, Any]] = []

    def handle_decl(self, decl: str) -> None:
        if decl.strip().lower() == "doctype html":
            self.doctype = True

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        normalized: dict[str, str] = {}
        for key, value in attrs:
            name = str(key).lower()
            if name in normalized:
                self.duplicate_attributes.append(
                    {"tag": tag.lower(), "attribute": name, "line": self.getpos()[0]}
                )
                continue
            normalized[name] = str(value or "")
        node = _Node(tag.lower(), normalized, self.stack[-1], self.getpos()[0])
        self.stack[-1].children.append(node)
        if node.tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag.lower() and tag.lower() not in VOID_TAGS:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == lowered:
                del self.stack[index:]
                return
        self.parse_errors.append(f"unexpected closing tag </{lowered}> on line {self.getpos()[0]}")

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].data.append(data)

    def nodes(self, tag: str | None = None) -> list[_Node]:
        values = list(self.root.descendants())
        if tag is None:
            return values
        return [node for node in values if node.tag == tag]


def _json_document(path: Path | str) -> Any:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise WebpageContractError(f"expected a regular JSON file: {source}")
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WebpageContractError(f"invalid JSON file: {source}") from error


def _json_file(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    value = _json_document(source)
    if not isinstance(value, dict):
        raise WebpageContractError(f"JSON contract must be an object: {source}")
    return value


def _run_root(run_dir: Path | str) -> Path:
    run = Path(run_dir).absolute()
    if run.is_symlink() or not run.is_dir():
        raise WebpageContractError(f"run directory does not exist: {run}")
    return run


def _state(run: Path) -> dict[str, Any]:
    return _json_file(run / "run.json")


def _plan_file(run: Path) -> dict[str, Any]:
    return _json_file(run / "plan.json")


def _attempt_root(run: Path, attempt_id: str) -> Path:
    state = _state(run)
    if state.get("active_attempt") != attempt_id:
        raise WebpageContractError("attempt is not the active run attempt")
    try:
        return portable.safe_path(run / "attempts", attempt_id, must_exist=True)
    except portable.PortableError as error:
        raise WebpageContractError(str(error)) from error


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def initialize_webpage_run(
    run_dir: Path | str,
    source_path: Path | str,
    *,
    extra_assets: Sequence[Path | str] = (),
    reference_images: Sequence[Path | str] = (),
    release_version: str = DEFAULT_RELEASE_VERSION,
    archive_sha256: str | None = None,
    install_browser: bool = True,
    browser_cache: Path | None = None,
) -> dict[str, Any]:
    """Initialize portable state, prepare the source, and optionally install Chromium."""

    if install_browser:
        try:
            setup_browser.ensure_browser_runtime(cache_root=browser_cache, allow_install=True)
        except setup_browser.BrowserRuntimeError as error:
            raise WebpageBlockedError(f"browser runtime unavailable: {error}") from error
    portable.initialize_run(
        run_dir,
        SKILL_ROOT,
        release_version=release_version,
        archive_sha256=archive_sha256,
    )
    manifest = portable.prepare_source(
        run_dir,
        source_path,
        extra_assets=extra_assets,
        reference_images=reference_images,
    )
    return {"state": _state(_run_root(run_dir)), "source_manifest": manifest}


def resume_webpage_run(run_dir: Path | str) -> dict[str, Any]:
    """Resume only after verifying the currently installed Skill snapshot."""

    return portable.resume_run(run_dir, skill_root=SKILL_ROOT)


def bind_webpage_visuals(
    run_dir: Path | str, review: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind a fresh host-VLM review to PDF-extracted visual candidates."""

    run = _run_root(run_dir)
    resumed = resume_webpage_run(run)
    if resumed.get("next_action") != "plan":
        raise WebpageContractError(
            f"visual review binding requires an initialized run: {resumed.get('next_action')}"
        )
    try:
        return portable.bind_host_vlm_visuals(run, review)
    except portable.PortableError as error:
        raise WebpageContractError(str(error)) from error


def _evidence_by_id(run: Path) -> dict[str, dict[str, Any]]:
    return {str(item["id"]): item for item in portable.load_evidence(run)}


def _strings(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise WebpageContractError(f"{field} must be a {'possibly empty ' if allow_empty else ''}list")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            raise WebpageContractError(f"{field} contains an empty value")
        result.append(text)
    if len(set(result)) != len(result):
        raise WebpageContractError(f"{field} contains duplicates")
    return result


def validate_webpage_plan(run_dir: Path | str, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a complete evidence-first research-webpage plan."""

    run = _run_root(run_dir)
    evidence = _evidence_by_id(run)
    value = json.loads(json.dumps(dict(plan), ensure_ascii=False))
    required_fields = {
        "format_version",
        "artifact_type",
        "brief",
        "title_claim_id",
        "thesis_claim_id",
        "sections",
        "visual_allocations",
        "interactions",
        "resource_links",
        "missing_metadata",
        "max_attempts",
    }
    if set(value) != required_fields:
        unexpected = sorted(set(value) - required_fields)
        missing = sorted(required_fields - set(value))
        raise WebpageContractError(
            f"plan has unknown fields {unexpected} or missing fields {missing}"
        )
    if value.get("format_version") != FORMAT_VERSION:
        raise WebpageContractError("plan format_version must be 1")
    if value.get("artifact_type") != "research_webpage":
        raise WebpageContractError("plan artifact_type must be research_webpage")
    if not isinstance(value.get("brief"), str) or not value["brief"].strip():
        raise WebpageContractError("plan requires a non-empty user brief")
    for field in ("title_claim_id", "thesis_claim_id"):
        if not str(value.get(field) or "").strip():
            raise WebpageContractError(f"plan requires {field}")

    sections = value.get("sections")
    if not isinstance(sections, list) or not sections:
        raise WebpageContractError("plan requires semantic sections")
    section_ids: list[str] = []
    section_roles: list[str] = []
    planned_claim_ids: set[str] = set()
    for section in sections:
        if not isinstance(section, dict):
            raise WebpageContractError("each section must be an object")
        if set(section) != {"id", "role", "claim_ids"}:
            raise WebpageContractError("section has unknown or incomplete fields")
        section_id = str(section.get("id") or "").strip()
        role = str(section.get("role") or "").strip()
        if not section_id or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", section_id):
            raise WebpageContractError("section ids must be stable HTML identifiers")
        if not role:
            raise WebpageContractError("each section requires a role")
        claim_ids = _strings(
            section.get("claim_ids"),
            f"section {section_id} claim_ids",
            allow_empty=True,
        )
        planned_claim_ids.update(claim_ids)
        section_ids.append(section_id)
        section_roles.append(role)
    if len(set(section_ids)) != len(section_ids) or len(set(section_roles)) != len(section_roles):
        raise WebpageContractError("section ids and roles must be unique")
    missing_roles = [role for role in REQUIRED_SECTION_ROLES if role not in section_roles]
    if missing_roles:
        raise WebpageContractError(
            "plan is missing required research sections: " + ", ".join(missing_roles)
        )
    required_positions = [section_roles.index(role) for role in REQUIRED_SECTION_ROLES]
    if required_positions != sorted(required_positions):
        raise WebpageContractError("research sections must follow the evidence-first narrative order")
    identity_claims = set(
        sections[section_roles.index("identity")].get("claim_ids", [])
    )
    if {
        str(value["title_claim_id"]),
        str(value["thesis_claim_id"]),
    } - identity_claims:
        raise WebpageContractError(
            "title and thesis claims must be declared by the identity section"
        )

    allocations = value.get("visual_allocations")
    if not isinstance(allocations, list):
        raise WebpageContractError("visual_allocations must be a list")
    if any(
        not isinstance(allocation, dict)
        or set(allocation) != {"visual_id", "role"}
        for allocation in allocations
    ):
        raise WebpageContractError("visual allocation has unknown or incomplete fields")
    try:
        visual_result = portable.validate_visual_plan(run, allocations)
    except portable.PortableError as error:
        raise WebpageContractError(str(error)) from error
    if not visual_result["valid"]:
        codes = ", ".join(str(item.get("code")) for item in visual_result["errors"])
        raise WebpageContractError(f"visual allocation failed: {codes}")
    allocated_visuals = {
        str(item.get("visual_id") or "").strip()
        for item in allocations
        if isinstance(item, dict)
    }

    interactions = value.get("interactions")
    if not isinstance(interactions, list) or not interactions:
        raise WebpageContractError("plan requires a source-grounded interaction")
    interaction_ids: list[str] = []
    meaningful = 0
    for interaction in interactions:
        if not isinstance(interaction, dict):
            raise WebpageContractError("each interaction must be an object")
        if set(interaction) != {
            "id",
            "kind",
            "claim_ids",
            "visual_ids",
            "control_id",
            "target_id",
            "state_attribute",
        }:
            raise WebpageContractError("interaction has unknown or incomplete fields")
        interaction_id = str(interaction.get("id") or "").strip()
        kind = str(interaction.get("kind") or "").strip()
        if not interaction_id or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", interaction_id):
            raise WebpageContractError("interaction ids must be stable HTML identifiers")
        if kind not in ALLOWED_INTERACTION_KINDS:
            raise WebpageContractError(f"unknown interaction kind: {kind}")
        claim_ids = _strings(
            interaction.get("claim_ids"), f"interaction {interaction_id} claim_ids", allow_empty=True
        )
        visual_ids = _strings(
            interaction.get("visual_ids"), f"interaction {interaction_id} visual_ids", allow_empty=True
        )
        if not claim_ids and not visual_ids:
            raise WebpageContractError("interaction must bind claims or source visuals")
        if any(claim_id not in planned_claim_ids for claim_id in claim_ids):
            raise WebpageContractError(
                "interaction references claims outside the planned research claims"
            )
        if any(visual_id not in allocated_visuals for visual_id in visual_ids):
            raise WebpageContractError("interaction references a visual outside the approved plan")
        for field in ("control_id", "target_id"):
            if not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_-]*", str(interaction.get(field) or "")
            ):
                raise WebpageContractError(f"interaction requires a valid {field}")
        if interaction.get("state_attribute") not in ALLOWED_STATE_ATTRIBUTES:
            raise WebpageContractError("interaction requires an observable accessible state attribute")
        if kind in {"inspect", "compare"}:
            meaningful += 1
        interaction_ids.append(interaction_id)
    if len(set(interaction_ids)) != len(interaction_ids):
        raise WebpageContractError("interaction ids must be unique")
    if meaningful < 1:
        raise WebpageContractError(
            "navigation alone is ornamental; require an inspect or compare interaction"
        )

    resources = value.get("resource_links")
    if not isinstance(resources, list):
        raise WebpageContractError("resource_links must be a list")
    resource_urls: list[str] = []
    for resource in resources:
        if not isinstance(resource, dict):
            raise WebpageContractError("each resource link must be an object")
        if set(resource) != {"label", "url", "source_ids"}:
            raise WebpageContractError("resource link has unknown or incomplete fields")
        label = str(resource.get("label") or "").strip()
        url = str(resource.get("url") or "").strip()
        source_ids = _strings(resource.get("source_ids"), f"resource {label} source_ids")
        parsed = urlsplit(url)
        if not label or parsed.scheme != "https" or not parsed.netloc or parsed.fragment:
            raise WebpageContractError("resource URLs must be complete source-provided HTTPS URLs")
        cited: list[str] = []
        for source_id in source_ids:
            if source_id not in evidence:
                raise WebpageContractError(f"resource link uses unknown source evidence: {source_id}")
            cited.append(str(evidence[source_id].get("text") or ""))
        if not any(url in text for text in cited):
            raise WebpageContractError(f"resource URL is not present in its source evidence: {url}")
        resource_urls.append(url)
    if len(set(resource_urls)) != len(resource_urls):
        raise WebpageContractError("resource URLs must be unique")

    missing_metadata = _strings(
        value.get("missing_metadata"), "missing_metadata", allow_empty=True
    )
    unknown_missing = sorted(set(missing_metadata) - ALLOWED_MISSING_METADATA)
    if unknown_missing:
        raise WebpageContractError("unknown missing metadata fields: " + ", ".join(unknown_missing))
    max_attempts = value.get("max_attempts")
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 6:
        raise WebpageContractError("max_attempts must be an integer from 1 through 6")
    return value


def save_webpage_plan(run_dir: Path | str, plan: Mapping[str, Any]) -> dict[str, Any]:
    run = _run_root(run_dir)
    resumed = resume_webpage_run(run)
    if resumed.get("next_action") != "plan":
        raise WebpageContractError(f"run is not ready to plan: {resumed.get('next_action')}")
    return portable.save_plan(run, validate_webpage_plan(run, plan))


def begin_webpage_attempt(run_dir: Path | str) -> str:
    run = _run_root(run_dir)
    resumed = resume_webpage_run(run)
    if resumed.get("next_action") not in {"begin_attempt", "repair"}:
        raise WebpageContractError(f"run is not ready for an attempt: {resumed.get('next_action')}")
    plan = _plan_file(run)
    if int(resumed.get("attempt_count") or 0) >= int(plan.get("max_attempts") or 0):
        raise WebpageContractError("bounded repair budget is exhausted")
    return portable.begin_attempt(run)


def stage_visual(run_dir: Path | str, attempt_id: str, visual_id: str) -> str:
    """Copy one plan-authorized immutable source visual into the artifact closure."""

    run = _run_root(run_dir)
    resumed = resume_webpage_run(run)
    if resumed.get("next_action") not in {"author", "validate"}:
        raise WebpageContractError("visual staging requires an active authoring attempt")
    attempt = _attempt_root(run, attempt_id)
    plan = _plan_file(run)
    allocations = {
        str(item.get("visual_id") or "")
        for item in plan.get("visual_allocations", [])
        if isinstance(item, dict)
    }
    visuals = _json_file(run / "evidence" / "source_visuals.json").get("visuals")
    if not isinstance(visuals, list):
        raise WebpageContractError("source visual catalog is invalid")
    visual = next(
        (item for item in visuals if isinstance(item, dict) and item.get("id") == visual_id),
        None,
    )
    if visual is None:
        raise WebpageContractError(f"unknown visual: {visual_id}")
    if visual_id not in allocations:
        raise WebpageContractError(f"visual is not authorized by the plan: {visual_id}")
    if visual.get("eligibility") != "eligible":
        raise WebpageContractError(f"visual is not host-reviewed and eligible: {visual_id}")
    source = portable.safe_path(
        run / "evidence", str(visual.get("path") or ""), must_exist=True
    )
    if portable.sha256_file(source) != visual.get("sha256"):
        raise WebpageContractError(f"source visual hash drift: {visual_id}")
    suffix = source.suffix.lower()
    if suffix not in ALLOWED_VISUAL_SUFFIXES:
        raise WebpageContractError(f"unsupported browser visual type: {suffix}")
    relative = f"assets/{visual_id}{suffix}"
    destination = portable.safe_path(attempt / "artifact", relative)
    if destination.exists():
        if destination.is_symlink() or portable.sha256_file(destination) != visual.get("sha256"):
            raise WebpageContractError(f"refusing to overwrite a drifted staged visual: {visual_id}")
        return relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    portable.atomic_write_bytes(destination, source.read_bytes())
    return relative


def write_webpage_source_map(
    run_dir: Path | str, attempt_id: str, claims: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    run = _run_root(run_dir)
    resumed = resume_webpage_run(run)
    if resumed.get("next_action") not in {"author", "validate"}:
        raise WebpageContractError("source mapping requires an active authoring attempt")
    try:
        return portable.write_source_map(run, attempt_id, claims)
    except portable.PortableError as error:
        raise WebpageContractError(str(error)) from error


def _node_hidden(node: _Node) -> bool:
    if "hidden" in node.attrs or node.attrs.get("aria-hidden", "").lower() == "true":
        return True
    style = node.attrs.get("style", "").lower().replace("!important", "")
    if re.search(r"(?:^|;)\s*display\s*:\s*none\b", style):
        return True
    if re.search(r"(?:^|;)\s*visibility\s*:\s*(?:hidden|collapse)\b", style):
        return True
    match = re.search(r"(?:^|;)\s*opacity\s*:\s*([0-9.]+)", style)
    return bool(match and float(match.group(1)) <= 0)


def _inside_hidden(node: _Node) -> bool:
    current: _Node | None = node
    while current is not None:
        if _node_hidden(current):
            return True
        current = current.parent
    return False


def _attr_tokens(node: _Node, name: str) -> set[str]:
    return {item for item in node.attrs.get(name, "").split() if item}


def _find_by_id(nodes: Sequence[_Node]) -> tuple[dict[str, _Node], set[str]]:
    counts = Counter(node.attrs.get("id", "") for node in nodes if node.attrs.get("id"))
    by_id = {node.attrs["id"]: node for node in nodes if node.attrs.get("id")}
    return by_id, {value for value, count in counts.items() if count > 1}


def _safe_local_path(root: Path, value: str, *, base: Path | None = None) -> Path | None:
    raw = unquote(str(value or "").strip())
    if not raw or raw.startswith("#"):
        return None
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or raw.startswith("//") or not parsed.path:
        return None
    relative = Path(parsed.path)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    candidate = ((base or root) / relative).resolve(strict=False)
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _srcset_values(value: str) -> list[str]:
    return [
        candidate.strip().split(None, 1)[0]
        for candidate in value.split(",")
        if candidate.strip()
    ]


def _motion_disabled(value: str) -> bool:
    compact = re.sub(r"\s+!important\s*$", "", value.lower()).strip()
    if compact in {"none", "initial", "inherit", "unset", "revert", "auto"}:
        return True
    durations = re.findall(r"(?:^|[\s,])(\d*\.?\d+)(ms|s)(?=$|[\s,])", compact)
    return bool(durations) and all(float(amount) == 0 for amount, _unit in durations)


def _effective_reduced_motion(css: str) -> bool:
    blocks: list[str] = []
    for match in re.finditer(
        r"@media\b[^{}]*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)[^{}]*\{",
        css,
        re.IGNORECASE,
    ):
        start = match.end()
        depth = 1
        index = start
        while index < len(css) and depth:
            depth += 1 if css[index] == "{" else -1 if css[index] == "}" else 0
            index += 1
        if depth == 0:
            blocks.append(css[start : index - 1])
    if not blocks:
        return False
    fallback = "\n".join(blocks)
    active: set[str] = set()
    for name, value in _MOTION.findall(css):
        if _motion_disabled(value):
            continue
        lowered = name.lower()
        active.add(
            "animation"
            if lowered.startswith("animation")
            else "transition"
            if lowered.startswith("transition")
            else "scroll"
        )
    disabled = {
        "animation": bool(
            re.search(r"animation(?:-duration)?\s*:\s*(?:none|0(?:\.0+)?(?:ms|s))\b", fallback, re.I)
        ),
        "transition": bool(
            re.search(r"transition(?:-duration)?\s*:\s*(?:none|0(?:\.0+)?(?:ms|s))\b", fallback, re.I)
        ),
        "scroll": bool(re.search(r"scroll-behavior\s*:\s*auto\b", fallback, re.I)),
    }
    return bool(active) and all(disabled[item] for item in active)


def _control_name(control: _Node) -> str:
    return (
        control.attrs.get("aria-label", "").strip()
        or control.attrs.get("title", "").strip()
        or control.text(omit_tags={"svg", "img"}).strip()
    )


def _normalized_claim_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def _bound_claim_ancestor(node: _Node) -> _Node | None:
    current: _Node | None = node
    while current is not None and current.tag != "#document":
        if _attr_tokens(current, "data-claim-id"):
            return current
        current = current.parent
    return None


def _inside_tag(node: _Node, tags: set[str]) -> bool:
    current: _Node | None = node
    while current is not None and current.tag != "#document":
        if current.tag in tags:
            return True
        current = current.parent
    return False


def _visible_assertion_kind(node: _Node) -> str | None:
    if _inside_tag(node, {"head", "script", "style", "template"}):
        return None
    text = _normalized_claim_text(" ".join(node.data))
    if node.tag == "math":
        return "formula"
    if not text:
        return None
    if _VISIBLE_URL.search(text):
        return "url"
    if _VISIBLE_FORMULA.search(text):
        return "formula"
    if _VISIBLE_NUMBER.search(text):
        return "numeric"
    return None


def _has_nonempty_pseudo_content(css: str) -> bool:
    for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", css, re.DOTALL):
        selector, declarations = rule.groups()
        if not re.search(r"::?(?:before|after)\b", selector, re.IGNORECASE):
            continue
        for match in re.finditer(r"(?:^|;)\s*content\s*:\s*([^;}]+)", declarations, re.I):
            value = re.sub(r"\s*!important\s*$", "", match.group(1)).strip()
            if value.lower() in {"none", "normal", "''", '""'}:
                continue
            return True
    return False


def _finding(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}


def validate_webpage_html(run_dir: Path | str, attempt_id: str) -> dict[str, Any]:
    """Run deterministic source, HTML, no-JS, interaction, and asset checks."""

    run = _run_root(run_dir)
    attempt = _attempt_root(run, attempt_id)
    artifact = attempt / "artifact"
    html_path = artifact / "index.html"
    findings: list[dict[str, Any]] = []
    if html_path.is_symlink() or not html_path.is_file():
        return {
            "format_version": FORMAT_VERSION,
            "passed": False,
            "findings": [_finding("missing_index", "artifact/index.html is required")],
            "metrics": {},
        }
    try:
        html = html_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise WebpageContractError("index.html must be valid UTF-8") from error
    parser = _DocumentParser()
    parser.feed(html)
    parser.close()
    nodes = parser.nodes()
    by_id, duplicate_ids = _find_by_id(nodes)
    html_sidecars = sorted(
        path.relative_to(artifact).as_posix()
        for path in artifact.rglob("*")
        if path != html_path and path.suffix.lower() in {".htm", ".html"}
    )
    if html_sidecars:
        findings.append(
            _finding(
                "html_sidecar_forbidden",
                "index.html must be the artifact's only HTML document",
                paths=html_sidecars,
            )
        )
    if not parser.doctype:
        findings.append(_finding("missing_doctype", "use the HTML5 doctype"))
    if parser.parse_errors:
        findings.append(_finding("malformed_html", "; ".join(parser.parse_errors[:3])))
    if parser.duplicate_attributes:
        findings.append(
            _finding(
                "duplicate_attribute",
                "duplicate HTML attributes are ambiguous and forbidden",
                attributes=parser.duplicate_attributes,
            )
        )
    if duplicate_ids:
        findings.append(_finding("duplicate_ids", "HTML ids must be unique", ids=sorted(duplicate_ids)))
    inline_handlers = sorted(
        {
            attribute
            for node in nodes
            for attribute in node.attrs
            if attribute.lower().startswith("on")
        }
    )
    if inline_handlers:
        findings.append(
            _finding(
                "inline_event_handler",
                "inline event handlers are forbidden; bind local behavior from a script",
                attributes=inline_handlers,
            )
        )

    html_nodes = [node for node in nodes if node.tag == "html"]
    if len(html_nodes) != 1 or not html_nodes[0].attrs.get("lang", "").strip():
        findings.append(_finding("missing_document_language", "html[lang] is required"))
    viewport = [
        node
        for node in nodes
        if node.tag == "meta" and node.attrs.get("name", "").lower() == "viewport"
    ]
    if not viewport or "width=device-width" not in viewport[0].attrs.get("content", "").lower():
        findings.append(_finding("missing_viewport", "responsive viewport metadata is required"))
    if len([node for node in nodes if node.tag == "main"]) != 1:
        findings.append(_finding("main_landmark", "exactly one main landmark is required"))
    if not any(node.tag == "nav" and node.attrs.get("aria-label", "").strip() for node in nodes):
        findings.append(_finding("navigation_label", "research navigation requires an accessible name"))
    if not any(
        node.tag == "a" and node.attrs.get("href") == "#main" and node.text().strip()
        for node in nodes
    ):
        findings.append(_finding("missing_skip_link", "provide a visible-on-focus skip link to #main"))

    plan = validate_webpage_plan(run, _plan_file(run))
    source_map_path = attempt / "provenance" / "source-map.json"
    if source_map_path.is_symlink() or not source_map_path.is_file():
        findings.append(_finding("missing_source_map", "write the source map before validation"))
        source_claims: dict[str, dict[str, Any]] = {}
    else:
        source_map = _json_file(source_map_path)
        source_claims = {
            str(claim.get("id")): claim
            for claim in source_map.get("claims", [])
            if isinstance(claim, dict) and claim.get("id")
        }
    planned_claims = {
        claim_id
        for section in plan["sections"]
        for claim_id in section.get("claim_ids", [])
    } | {str(plan["title_claim_id"]), str(plan["thesis_claim_id"])}
    missing_claims = sorted(planned_claims - set(source_claims))
    if missing_claims:
        findings.append(_finding("plan_claim_missing_from_source_map", "planned claims are not source mapped", ids=missing_claims))
    html_claims = {
        claim_id for node in nodes for claim_id in _attr_tokens(node, "data-claim-id")
    }
    unknown_html_claims = sorted(html_claims - set(source_claims))
    if unknown_html_claims:
        findings.append(_finding("unknown_html_claim", "HTML references unknown claim ids", ids=unknown_html_claims))
    unused_claims = sorted(set(source_claims) - html_claims)
    if unused_claims:
        findings.append(_finding("unrendered_source_claim", "every mapped claim must appear in native HTML", ids=unused_claims))
    for node in nodes:
        claim_ids = sorted(_attr_tokens(node, "data-claim-id"))
        if not claim_ids:
            continue
        actual = _normalized_claim_text(
            node.text(omit_tags={"script", "style", "template", "noscript"})
        )
        if len(claim_ids) != 1:
            findings.append(
                _finding(
                    "claim_text_mismatch",
                    "each visible claim node must bind exactly one source-map claim",
                    ids=claim_ids,
                    line=node.line,
                )
            )
            continue
        claim_id = claim_ids[0]
        source_claim = source_claims.get(claim_id)
        if source_claim is None:
            continue
        expected = _normalized_claim_text(str(source_claim.get("text") or ""))
        if not actual or actual != expected:
            findings.append(
                _finding(
                    "claim_text_mismatch",
                    "visible claim text must exactly match its source-map claim",
                    claim_id=claim_id,
                    line=node.line,
                )
            )
    for node in nodes:
        assertion_kind = _visible_assertion_kind(node)
        if assertion_kind and _bound_claim_ancestor(node) is None:
            findings.append(
                _finding(
                    "ungrounded_visible_assertion",
                    "visible numeric, URL, and formula assertions must live inside an exact source claim node",
                    kind=assertion_kind,
                    line=node.line,
                )
            )

    role_nodes: dict[str, list[_Node]] = {}
    for node in nodes:
        role = node.attrs.get("data-section-role", "").strip()
        if role:
            role_nodes.setdefault(role, []).append(node)
    planned_roles = [str(section["role"]) for section in plan["sections"]]
    planned_sections = {
        str(section["role"]): section for section in plan["sections"]
    }
    for role in planned_roles:
        if len(role_nodes.get(role, [])) != 1:
            findings.append(_finding("section_role_count", f"require exactly one {role} section", role=role))
            continue
        planned_section = planned_sections[role]
        planned_id = str(planned_section["id"])
        if role_nodes[role][0].attrs.get("id") != planned_id:
            findings.append(
                _finding(
                    "section_id_mismatch",
                    f"{role} must use its planned section id",
                    expected=planned_id,
                    actual=role_nodes[role][0].attrs.get("id", ""),
                )
            )
        section_node = role_nodes[role][0]
        actual_claims = {
            claim_id
            for node in [section_node, *section_node.descendants()]
            for claim_id in _attr_tokens(node, "data-claim-id")
        }
        expected_claims = set(planned_section.get("claim_ids", []))
        if actual_claims != expected_claims:
            findings.append(
                _finding(
                    "section_claim_mismatch",
                    f"{role} must render exactly its planned source claims",
                    role=role,
                    missing=sorted(expected_claims - actual_claims),
                    unexpected=sorted(actual_claims - expected_claims),
                )
            )
    visible_roles = [
        node.attrs.get("data-section-role")
        for node in nodes
        if node.attrs.get("data-section-role")
    ]
    if visible_roles != planned_roles:
        findings.append(_finding("section_order", "render the complete research narrative in plan order"))
    for role, candidates in role_nodes.items():
        if role in planned_sections and candidates and _inside_hidden(candidates[0]):
            findings.append(_finding("hidden_core_section", f"{role} must be visible without JavaScript", role=role))

    h1_nodes = [node for node in nodes if node.tag == "h1"]
    if len(h1_nodes) != 1 or _attr_tokens(h1_nodes[0], "data-claim-id") != {
        str(plan["title_claim_id"])
    }:
        findings.append(_finding("paper_identity", "one visible h1 must bind the paper title claim"))
    identity = role_nodes.get("identity", [])
    thesis_nodes = [
        node
        for node in (list(identity[0].descendants()) if identity else [])
        if node.attrs.get("data-thesis-claim-id") == plan["thesis_claim_id"]
    ]
    if len(thesis_nodes) != 1:
        findings.append(_finding("missing_first_viewport_thesis", "identity must expose the source-backed thesis"))
    elif _attr_tokens(thesis_nodes[0], "data-claim-id") != {
        str(plan["thesis_claim_id"])
    }:
        findings.append(
            _finding(
                "thesis_claim_binding",
                "the first-viewport thesis marker must itself be the exact thesis claim node",
            )
        )

    missing_markers = {
        marker
        for node in nodes
        for marker in _attr_tokens(node, "data-missing-metadata")
    }
    expected_missing = set(plan["missing_metadata"])
    if missing_markers != expected_missing:
        findings.append(
            _finding(
                "missing_metadata_contract",
                "truthful missing-metadata notes must exactly match the plan",
                missing=sorted(expected_missing - missing_markers),
                unexpected=sorted(missing_markers - expected_missing),
            )
        )

    styles: list[tuple[str, Path]] = []
    scripts: list[str] = []
    referenced_files: set[Path] = {html_path.resolve()}
    for node in nodes:
        if node.tag == "style":
            styles.append((node.text(), artifact))
        if node.attrs.get("style"):
            styles.append((node.attrs["style"], artifact))
        if node.tag == "script" and not node.attrs.get("src"):
            scripts.append(node.text())
    for node in nodes:
        for tag, attribute in ASSET_ATTRIBUTES:
            if node.tag != tag or not node.attrs.get(attribute):
                continue
            value = node.attrs[attribute]
            parsed = urlsplit(unquote(value.strip()))
            if parsed.scheme or parsed.netloc or value.startswith("//"):
                findings.append(_finding("remote_asset", f"{tag}[{attribute}] must use a local file", value=value))
                continue
            path = _safe_local_path(artifact, value)
            if path is None or path.is_symlink() or not path.is_file():
                findings.append(_finding("missing_local_asset", f"missing local dependency: {value}"))
                continue
            referenced_files.add(path)
            if node.tag == "link" and "stylesheet" in _attr_tokens(node, "rel"):
                try:
                    styles.append((path.read_text(encoding="utf-8"), path.parent))
                except (OSError, UnicodeError):
                    findings.append(_finding("invalid_stylesheet", f"stylesheet is not UTF-8: {value}"))
            if node.tag == "script":
                try:
                    scripts.append(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError):
                    findings.append(_finding("invalid_script", f"script is not UTF-8: {value}"))
        if node.tag in {"img", "source"} and node.attrs.get("srcset"):
            for value in _srcset_values(node.attrs["srcset"]):
                path = _safe_local_path(artifact, value)
                if path is None or path.is_symlink() or not path.is_file():
                    findings.append(_finding("missing_local_asset", f"invalid srcset dependency: {value}"))
                else:
                    referenced_files.add(path)
    css = "\n".join(content for content, _base in styles)
    if _has_nonempty_pseudo_content(css):
        findings.append(
            _finding(
                "css_generated_content",
                "non-empty CSS pseudo-element content is forbidden because it bypasses source binding",
            )
        )
    for content, base in styles:
        if re.search(r"@import\b", content, re.IGNORECASE):
            findings.append(_finding("remote_asset", "CSS @import is forbidden"))
        for _quote, value in _CSS_URL.findall(content):
            raw_value = value.strip()
            if raw_value.startswith("#"):
                continue
            parsed = urlsplit(unquote(raw_value))
            if parsed.scheme or parsed.netloc or raw_value.startswith("//"):
                findings.append(
                    _finding(
                        "remote_asset",
                        "CSS URLs must use an inspectable local file",
                        value=raw_value,
                    )
                )
                continue
            path = _safe_local_path(artifact, value, base=base)
            if path is None or path.is_symlink() or not path.is_file():
                findings.append(_finding("missing_local_asset", f"invalid CSS dependency: {value}"))
            else:
                referenced_files.add(path)
    script = "\n".join(scripts)
    if _NETWORK_JS.search(script):
        findings.append(_finding("network_script", "artifact JavaScript must not use network APIs"))
    if _DYNAMIC_NAVIGATION_JS.search(script):
        findings.append(
            _finding(
                "dynamic_navigation_script",
                "artifact JavaScript must not navigate or open browsing contexts",
            )
        )
    if _REVEAL_JS.search(script) and _HIDDEN_REVEAL_CSS.search(css):
        findings.append(_finding("javascript_reveal_dependency", "core content starts hidden and depends on JavaScript reveal"))

    resource_urls = {str(item["url"]): str(item["label"]) for item in plan["resource_links"]}
    rendered_resources: set[str] = set()
    internal_count = 0
    for anchor in [node for node in nodes if node.tag == "a"]:
        href = anchor.attrs.get("href", "").strip()
        if not href:
            findings.append(_finding("empty_link", "anchors require a real href"))
            continue
        if href.startswith("#"):
            internal_count += 1
            target = href[1:]
            if not target or target not in by_id:
                findings.append(_finding("broken_internal_link", f"internal link does not resolve: {href}"))
            continue
        parsed = urlsplit(href)
        if parsed.scheme:
            if parsed.scheme != "https" or href not in resource_urls:
                findings.append(_finding("invented_resource_link", f"external link is not source-authorized: {href}"))
                continue
            expected_label = resource_urls[href]
            if anchor.attrs.get("data-resource-link") != expected_label:
                findings.append(_finding("resource_link_label", f"resource link must declare {expected_label!r}"))
            rendered_resources.add(href)
            if anchor.attrs.get("target") == "_blank" and "noopener" not in _attr_tokens(anchor, "rel"):
                findings.append(_finding("unsafe_new_tab", "target=_blank requires rel=noopener"))
            continue
        path = _safe_local_path(artifact, href)
        if path is None or path.is_symlink() or not path.is_file():
            findings.append(_finding("broken_local_link", f"local link does not resolve: {href}"))
        else:
            referenced_files.add(path)
    if rendered_resources != set(resource_urls):
        findings.append(_finding("missing_resource_link", "render every source-authorized resource link"))

    for node in [item for item in nodes if item.tag == "img"]:
        if not node.attrs.get("alt", "").strip():
            findings.append(_finding("image_missing_alt", "every image requires meaningful alt text", line=node.line))
    for node in [item for item in nodes if item.tag in {"iframe", "object", "embed", "base"}]:
        findings.append(_finding("unsafe_embed", f"{node.tag} is forbidden in a portable page"))
    navigation_markup = [
        node.tag
        for node in nodes
        if node.tag == "form"
        or (
            node.tag == "meta"
            and node.attrs.get("http-equiv", "").strip().lower() == "refresh"
        )
    ]
    if navigation_markup:
        findings.append(
            _finding(
                "dynamic_navigation_markup",
                "forms and meta refresh are forbidden in a portable page",
                tags=sorted(set(navigation_markup)),
            )
        )
    for node in nodes:
        tabindex = node.attrs.get("tabindex", "").strip()
        if tabindex and re.fullmatch(r"[+]?[1-9][0-9]*", tabindex):
            findings.append(_finding("positive_tabindex", "positive tabindex breaks document order", line=node.line))
        role = node.attrs.get("role", "").lower()
        if role in {"button", "link"} and node.tag not in {"button", "a"}:
            findings.append(_finding("interaction_not_keyboard_native", "use native button or anchor controls", line=node.line))

    interactions = {str(item["id"]): item for item in plan["interactions"]}
    grounded_interactions = 0
    for interaction_id, interaction in interactions.items():
        unmapped_interaction_claims = sorted(
            set(interaction.get("claim_ids", [])) - set(source_claims)
        )
        if unmapped_interaction_claims:
            findings.append(
                _finding(
                    "interaction_claim_unmapped",
                    f"interaction {interaction_id} references claims outside the source map",
                    ids=unmapped_interaction_claims,
                )
            )
        control = by_id.get(str(interaction["control_id"]))
        target = by_id.get(str(interaction["target_id"]))
        if control is None or target is None:
            findings.append(_finding("interaction_target_missing", f"interaction {interaction_id} is incomplete"))
            continue
        if control.tag not in {"button", "a"}:
            findings.append(_finding("interaction_not_keyboard_native", f"interaction {interaction_id} must use a native control"))
        if control.attrs.get("data-interaction-id") != interaction_id:
            findings.append(_finding("interaction_id_mismatch", f"control does not bind interaction {interaction_id}"))
        if str(interaction["target_id"]) not in _attr_tokens(control, "aria-controls"):
            findings.append(_finding("interaction_aria_controls", f"interaction {interaction_id} must expose aria-controls"))
        state_attribute = str(interaction["state_attribute"])
        if state_attribute not in control.attrs:
            findings.append(_finding("interaction_state_missing", f"interaction {interaction_id} lacks {state_attribute}"))
        if not _control_name(control):
            findings.append(_finding("control_missing_name", f"interaction {interaction_id} has no accessible name"))
        target_nodes = [target, *target.descendants()]
        target_claims = {
            item for node in target_nodes for item in _attr_tokens(node, "data-claim-id")
        }
        target_visuals = {
            node.attrs.get("data-source-id", "") for node in target_nodes if node.attrs.get("data-source-id")
        }
        if not (
            target_claims.intersection(interaction.get("claim_ids", []))
            or target_visuals.intersection(interaction.get("visual_ids", []))
        ):
            findings.append(_finding("interaction_not_source_bound", f"interaction {interaction_id} target is ornamental"))
        else:
            grounded_interactions += 1
        if _inside_hidden(target):
            findings.append(_finding("hidden_interaction_evidence", f"interaction {interaction_id} hides core evidence without JavaScript"))

    visual_catalog = _json_file(run / "evidence" / "source_visuals.json")
    visuals = {
        str(item.get("id")): item
        for item in visual_catalog.get("visuals", [])
        if isinstance(item, dict) and item.get("id")
    }
    planned_visual_counts = Counter(
        str(item["visual_id"]) for item in plan["visual_allocations"]
    )
    allocations = {str(item["visual_id"]): item for item in plan["visual_allocations"]}
    rendered_visual_counts: Counter[str] = Counter()
    rendered_instances: set[tuple[str, int]] = set()
    for node in [item for item in nodes if item.tag in {"img", "source"}]:
        visual_id = ""
        current: _Node | None = node
        while current is not None and current.tag != "#document":
            visual_id = current.attrs.get("data-source-id", "").strip()
            if visual_id:
                break
            current = current.parent
        if not visual_id:
            continue
        visual = visuals.get(visual_id)
        if visual is None or visual_id not in allocations:
            findings.append(_finding("unapproved_source_visual", f"visual is not plan-authorized: {visual_id}"))
            continue
        source_path = portable.safe_path(run / "evidence", str(visual.get("path") or ""), must_exist=True)
        candidate_values = []
        if node.attrs.get("src"):
            candidate_values.append(node.attrs["src"])
        candidate_values.extend(_srcset_values(node.attrs.get("srcset", "")))
        rendered_paths = [
            path
            for value in candidate_values
            if (path := _safe_local_path(artifact, value)) is not None and path.is_file()
        ]
        if not rendered_paths:
            continue
        if any(
            portable.sha256_file(source_path) != portable.sha256_file(rendered_path)
            for rendered_path in rendered_paths
        ):
            findings.append(_finding("source_visual_hash_mismatch", f"rendered bytes drifted from source: {visual_id}"))
        if _inside_hidden(node):
            findings.append(_finding("hidden_source_visual", f"source evidence is hidden: {visual_id}"))
        display_node = (
            node.parent if node.parent is not None and node.parent.tag == "picture" else node
        )
        instance = (visual_id, id(display_node))
        if instance not in rendered_instances:
            rendered_instances.add(instance)
            rendered_visual_counts[visual_id] += 1
    if rendered_visual_counts != planned_visual_counts:
        findings.append(
            _finding(
                "source_visual_reuse_mismatch",
                "render source visuals exactly as many times as the reviewed plan permits",
                planned=dict(sorted(planned_visual_counts.items())),
                rendered=dict(sorted(rendered_visual_counts.items())),
            )
        )

    asset_files = {
        path.resolve()
        for path in (artifact / "assets").rglob("*")
        if path.is_file() and not path.is_symlink()
    } if (artifact / "assets").is_dir() else set()
    if asset_files - referenced_files:
        findings.append(_finding("orphan_local_asset", "artifact contains unreferenced staged assets"))

    artifact_files: dict[Path, str] = {}
    unsafe_entries: list[str] = []
    hardlinked_files: list[str] = []
    for current, directories, files in os.walk(artifact, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                unsafe_entries.append(path.relative_to(artifact).as_posix())
        for name in files:
            path = current_path / name
            relative = path.relative_to(artifact).as_posix()
            if path.is_symlink() or not path.is_file():
                unsafe_entries.append(relative)
                continue
            resolved = path.resolve(strict=True)
            artifact_files[resolved] = relative
            if path.lstat().st_nlink > 1:
                hardlinked_files.append(relative)
    if unsafe_entries:
        findings.append(
            _finding(
                "unsafe_artifact_entry",
                "artifact closure must contain only ordinary local files and directories",
                paths=sorted(unsafe_entries),
            )
        )
    if hardlinked_files:
        findings.append(
            _finding(
                "hardlinked_artifact_file",
                "artifact files must own their bytes and cannot be hardlinked",
                paths=sorted(hardlinked_files),
            )
        )
    generated_reports = {
        (artifact / name).resolve(strict=True)
        for name in _GENERATED_ARTIFACT_REPORTS
        if (artifact / name).is_file() and not (artifact / name).is_symlink()
    }
    unreachable = sorted(
        artifact_files[path]
        for path in set(artifact_files) - referenced_files - generated_reports
    )
    if unreachable:
        findings.append(
            _finding(
                "unreachable_artifact_file",
                "every authored artifact file must be reachable from index.html",
                paths=unreachable,
            )
        )

    icon_nodes = [node for node in nodes if node.tag == "svg" and "data-icon" in node.attrs]
    if not 3 <= len(icon_nodes) <= 8:
        findings.append(_finding("functional_icon_count", "use 3-8 restrained functional inline SVG icons", actual=len(icon_nodes)))
    interactive_tags = {"a", "button", "summary", "input", "select", "textarea"}
    for icon in icon_nodes:
        parent = icon.parent
        while parent is not None and parent.tag not in interactive_tags:
            parent = parent.parent
        if parent is not None and not _control_name(parent):
            findings.append(_finding("icon_control_missing_name", "icon controls require an accessible name", line=icon.line))
        if parent is None and icon.attrs.get("aria-hidden", "").lower() != "true" and not icon.attrs.get("aria-label"):
            findings.append(_finding("decorative_icon_exposed", "decorative icons must be aria-hidden"))

    if not re.search(r":focus-visible\b", css, re.IGNORECASE):
        findings.append(_finding("missing_focus_visible", "define a visible :focus-visible state"))
    if not re.search(r"@media\b[^{}]*max-width", css, re.IGNORECASE):
        findings.append(_finding("missing_responsive_layout", "provide a mobile layout breakpoint"))
    active_motion = [item for item in _MOTION.findall(css) if not _motion_disabled(item[1])]
    if (active_motion or re.search(r"requestAnimationFrame\s*\(|\.animate\s*\(", script)) and not _effective_reduced_motion(css):
        findings.append(_finding("motion_without_reduced_motion", "disable motion and smooth scrolling under prefers-reduced-motion"))

    visible_text = " ".join(
        parser.root.text(omit_tags={"script", "style", "template", "noscript"}).split()
    )
    word_count = len(re.findall(r"\b[\w'-]+\b", visible_text))
    if word_count < 120:
        findings.append(_finding("insufficient_research_content", "research page needs a substantive native-text narrative", actual=word_count))
    marketing_hits = sorted(
        {pattern.pattern for pattern in _MARKETING_PATTERNS if pattern.search(visible_text)}
    )
    if marketing_hits:
        findings.append(_finding("generic_marketing_copy", "replace sales-funnel copy with research evidence", matches=marketing_hits))
    if re.search(r"(?:linear|radial|conic)-gradient\s*\(", css, re.IGNORECASE):
        findings.append(_finding("decorative_gradient", "use an editorial research surface instead of decorative gradients"))
    card_count = sum(
        1 for node in nodes if any("card" in value.lower() for value in _attr_tokens(node, "class"))
    )
    if card_count > 6:
        findings.append(_finding("repetitive_card_wall", "replace the repeated card wall with evidence-led section composition", actual=card_count))

    unique_findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for finding in findings:
        key = (str(finding["code"]), str(finding["message"]))
        if key not in seen:
            seen.add(key)
            unique_findings.append(finding)
    return {
        "format_version": FORMAT_VERSION,
        "passed": not unique_findings,
        "findings": unique_findings,
        "metrics": {
            "required_section_count": sum(
                1 for role in REQUIRED_SECTION_ROLES if len(role_nodes.get(role, [])) == 1
            ),
            "word_count": word_count,
            "claim_count": len(source_claims),
            "source_visual_count": sum(rendered_visual_counts.values()),
            "source_grounded_interaction_count": grounded_interactions,
            "internal_link_count": internal_count,
            "resource_link_count": len(rendered_resources),
            "missing_metadata_count": len(missing_markers),
            "functional_icon_count": len(icon_nodes),
        },
    }


_INTERACTION_PROBE = r'''#!/usr/bin/env python3
import argparse, json, os
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname
from playwright.sync_api import sync_playwright

FLAGS = [
  "--disable-background-networking", "--disable-component-update",
  "--disable-domain-reliability", "--disable-features=WebTransport", "--disable-quic",
  "--disable-sync", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
  "--host-resolver-rules=MAP * ~NOTFOUND", "--metrics-recording-only", "--no-pings",
  "--no-proxy-server", "--webrtc-ip-handling-policy=disable_non_proxied_udp",
]
BLOCK = r"""
(() => {
  const deny = (name) => function(){ throw new DOMException(name + ' disabled in local QA', 'SecurityError'); };
  globalThis.fetch = deny('fetch');
  if (globalThis.XMLHttpRequest) globalThis.XMLHttpRequest.prototype.open = deny('XMLHttpRequest');
  for (const name of ['WebSocket','EventSource','WebTransport','RTCPeerConnection','webkitRTCPeerConnection']) {
    if (name in globalThis) globalThis[name] = class { constructor(){ throw new DOMException(name + ' disabled in local QA', 'SecurityError'); } };
  }
  if (navigator.sendBeacon) navigator.sendBeacon = deny('sendBeacon');
  const pending = new Set();
  const pendingFrames = new Set();
  const pendingAnimations = new Set();
  const nativeSetTimeout = globalThis.setTimeout.bind(globalThis);
  const nativeClearTimeout = globalThis.clearTimeout.bind(globalThis);
  const nativeSetInterval = globalThis.setInterval.bind(globalThis);
  const nativeClearInterval = globalThis.clearInterval.bind(globalThis);
  globalThis.setTimeout = (callback, delay, ...args) => {
    let identifier;
    const wrapped = (...values) => {
      pending.delete(identifier);
      if (typeof callback === 'function') return callback(...values);
      return globalThis.eval(String(callback));
    };
    identifier = nativeSetTimeout(wrapped, delay, ...args);
    pending.add(identifier);
    return identifier;
  };
  globalThis.clearTimeout = (identifier) => { pending.delete(identifier); return nativeClearTimeout(identifier); };
  globalThis.setInterval = (callback, delay, ...args) => {
    const identifier = nativeSetInterval(callback, delay, ...args);
    pending.add(identifier);
    return identifier;
  };
  globalThis.clearInterval = (identifier) => { pending.delete(identifier); return nativeClearInterval(identifier); };
  if (globalThis.requestAnimationFrame) {
    const nativeRequestAnimationFrame = globalThis.requestAnimationFrame.bind(globalThis);
    const nativeCancelAnimationFrame = globalThis.cancelAnimationFrame.bind(globalThis);
    globalThis.requestAnimationFrame = callback => {
      let identifier;
      identifier = nativeRequestAnimationFrame(timestamp => {
        pendingFrames.delete(identifier);
        return callback(timestamp);
      });
      pendingFrames.add(identifier);
      return identifier;
    };
    globalThis.cancelAnimationFrame = identifier => {
      pendingFrames.delete(identifier);
      return nativeCancelAnimationFrame(identifier);
    };
  }
  if (globalThis.Element && Element.prototype.animate) {
    const nativeAnimate = Element.prototype.animate;
    Element.prototype.animate = function(...args) {
      const animation = nativeAnimate.apply(this, args);
      pendingAnimations.add(animation);
      animation.finished.then(
        () => pendingAnimations.delete(animation),
        () => pendingAnimations.delete(animation),
      );
      return animation;
    };
  }
  Object.defineProperty(globalThis, '__autodesignPendingWork', {
    value: () => pending.size + pendingFrames.size + pendingAnimations.size
      + document.getAnimations().filter(animation => !['finished','idle'].includes(animation.playState)).length,
    configurable: false, writable: false
  });
})();
"""

GROUNDING = r"""contract => {
  const normalized=value=>String(value||'').normalize('NFC').replace(/\s+/g,' ').trim();
  const tokens=(el,name)=>(el.getAttribute(name)||'').split(/\s+/).filter(Boolean);
  const expected=new Map((contract.source_claims||[]).map(item=>[String(item.id),normalized(item.text)]));
  const visible=el=>{ if(!el)return false; for(let n=el;n;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||['hidden','collapse'].includes(s.visibility)||Number(s.opacity)<=.01)return false;}const r=el.getBoundingClientRect();return r.width>.5&&r.height>.5; };
  const formula=/(?:\\\(|\\\[|\$[^$\n]+\$|\\(?:frac|sum|prod|sqrt|int)\b|[∑∏√∫≈≠≤≥±]|\b[A-Za-z][A-Za-z0-9_]*\s*(?:=|<=|>=|<|>)\s*[^,.;:]+|\^[{]?[-+]?\d)/i;
  const assertion=/(?:https?:\/\/[^\s<>\"']+|(?<![\w])[-+]?\d[\d,]*(?:\.\d+)?(?:\s*[%‰])?)/i;
  const claimNodes=[...document.querySelectorAll('[data-claim-id]')];
  if(!claimNodes.every(el=>{const ids=tokens(el,'data-claim-id');return visible(el)&&ids.length===1&&expected.has(ids[0])&&normalized(el.innerText)===expected.get(ids[0]);}))return false;
  if(![...expected].every(([id])=>claimNodes.some(el=>tokens(el,'data-claim-id').includes(id))))return false;
  if(!(contract.sections||[]).every(section=>{
    const roots=[...document.querySelectorAll('[data-section-role]')].filter(el=>el.getAttribute('data-section-role')===section.role);
    if(roots.length!==1||roots[0].id!==section.id)return false;
    const actual=[roots[0],...roots[0].querySelectorAll('[data-claim-id]')].flatMap(el=>tokens(el,'data-claim-id')).sort();
    return JSON.stringify(actual)===JSON.stringify([...section.claim_ids].sort());
  }))return false;
  const h1=[...document.querySelectorAll('h1')];
  if(h1.length!==1||JSON.stringify(tokens(h1[0],'data-claim-id'))!==JSON.stringify([contract.title_claim_id]))return false;
  const thesis=[...document.querySelectorAll('[data-thesis-claim-id]')].filter(el=>el.getAttribute('data-thesis-claim-id')===contract.thesis_claim_id);
  if(thesis.length!==1||JSON.stringify(tokens(thesis[0],'data-claim-id'))!==JSON.stringify([contract.thesis_claim_id]))return false;
  const unboundText=el=>{
    const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT); let node, text='';
    while((node=walker.nextNode())){
      if(visible(node.parentElement)&&!node.parentElement.closest('[data-claim-id]'))text+=node.data;
    }
    return normalized(text);
  };
  if([...document.body.querySelectorAll('*')].some(el=>{
    if(!visible(el)||el.closest('[data-claim-id]'))return false;
    const text=unboundText(el); return Boolean(text&&(assertion.test(text)||formula.test(text)));
  }))return false;
  return [...document.body.querySelectorAll('*')].every(el=>['::before','::after'].every(pseudo=>{
    const content=getComputedStyle(el,pseudo).content;
    return !content||['none','normal','\"\"',"''"].includes(content);
  }));
}"""

def inside(path, root):
  try: path.relative_to(root); return True
  except ValueError: return False

def main():
  p=argparse.ArgumentParser(); p.add_argument('--workspace',type=Path,required=True); p.add_argument('--html',type=Path,required=True); p.add_argument('--plan',type=Path,required=True); p.add_argument('--report',type=Path,required=True); a=p.parse_args()
  root=a.workspace.resolve(strict=True); html=a.html.resolve(strict=True); plan=json.loads(a.plan.read_text(encoding='utf-8'))
  blocked=[]; request_errors=[]; page_errors=[]; interactions=[]
  def route_handler(route):
    u=urlsplit(route.request.url)
    if u.scheme=='file' and not u.netloc:
      candidate=Path(url2pathname(unquote(u.path))).resolve(strict=False)
      if inside(candidate,root) and candidate.is_file(): route.continue_(); return
    blocked.append(u.scheme or 'unknown'); route.abort('blockedbyclient')
  def websocket_handler(route):
    blocked.append('websocket'); route.close()
  def context(browser, *, javascript=True, reduced=False, width=1440, height=1000):
    c=browser.new_context(java_script_enabled=javascript, offline=True, reduced_motion='reduce' if reduced else 'no-preference', viewport={'width':width,'height':height})
    c.route('**/*',route_handler)
    try: c.route_web_socket('**/*',websocket_handler)
    except AttributeError: pass
    c.add_init_script(BLOCK); c.on('weberror',lambda error: page_errors.append(type(error).__name__)); c.on('requestfailed',lambda request: request_errors.append(urlsplit(request.url).scheme or 'unknown'))
    return c
  result={'format_version':1,'passed':False,'checks':{},'interactions':[]}
  nojs_ok=False; motion_ok=False; internal_ok=False; mobile_ok=False; identity_ok=False; timers_ok=False; runtime_grounding_ok=False
  try:
    with sync_playwright() as pw:
      browser=pw.chromium.launch(headless=True,args=FLAGS)
      try:
        c=context(browser,reduced=True); page=c.new_page(); page.goto(html.as_uri(),wait_until='load',timeout=30000); page.wait_for_timeout(100)
        internal=page.locator('a[href^="#"]'); internal_ok=True
        for i in range(internal.count()):
          href=internal.nth(i).get_attribute('href') or ''
          internal_ok = internal_ok and len(href)>1 and page.locator('#'+href[1:]).count()==1
        motion_ok=page.evaluate("""() => [...document.querySelectorAll('*')].every(el => { const s=getComputedStyle(el); const times=v=>v.split(',').every(x=>{x=x.trim();return x.endsWith('ms')?parseFloat(x)===0:parseFloat(x||'0')===0}); return (s.animationName==='none'||times(s.animationDuration)) && times(s.transitionDuration) && s.scrollBehavior!=='smooth'; })""")
        identity_ok=bool(page.evaluate("""contract => {
          const visible = el => { if (!el) return false; for (let n=el;n;n=n.parentElement) { const s=getComputedStyle(n); if (s.display==='none'||s.visibility==='hidden'||Number(s.opacity)<=0) return false; } const r=el.getBoundingClientRect(); return r.width>0 && r.height>0; };
          const aboveFold = el => { if (!visible(el)) return false; const r=el.getBoundingClientRect(); const w=Math.max(0,Math.min(r.right,innerWidth)-Math.max(r.left,0)); const h=Math.max(0,Math.min(r.bottom,innerHeight)-Math.max(r.top,0)); return w>=Math.min(r.width,innerWidth)*0.5 && h>=Math.min(r.height,innerHeight)*0.5; };
          const h1=[...document.querySelectorAll('h1')].find(el => (el.getAttribute('data-claim-id')||'').split(/\s+/).includes(contract.title_claim_id));
          const thesis=document.querySelector('[data-thesis-claim-id="'+CSS.escape(contract.thesis_claim_id)+'"]');
          const identity=document.querySelector('[data-section-role="identity"]');
          return aboveFold(identity) && aboveFold(h1) && aboveFold(thesis);
        }""",plan))
        page.evaluate("() => document.activeElement && document.activeElement.blur()")
        tab_limit=max(8,page.locator('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]').count()+3)
        for item in plan['interactions']:
          control=page.locator('#'+item['control_id']); target=page.locator('#'+item['target_id']); state=item['state_attribute']
          ok=control.count()==1 and target.count()==1 and control.is_visible() and target.is_visible()
          if ok:
            ok=bool(control.evaluate("""el => { for(let n=el;n;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)<=0)return false;}return true; }""") and target.evaluate("""el => { for(let n=el;n;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)<=0)return false;}return true; }"""))
          before=control.get_attribute(state) if ok else None; target_before=None; focus_visible=False; scroll_before=None
          if ok:
            baseline=control.evaluate("""el => { const s=getComputedStyle(el); return {color:s.color,background:s.backgroundColor,border:s.border,boxShadow:s.boxShadow,textDecoration:s.textDecorationLine}; }""")
            reachable=False
            for _ in range(tab_limit):
              page.keyboard.press('Tab')
              if control.evaluate('(el)=>document.activeElement===el'):
                reachable=True; break
            focused=control.evaluate("""el => { const s=getComputedStyle(el); return {matches:el.matches(':focus-visible'),outlineStyle:s.outlineStyle,outlineWidth:s.outlineWidth,outlineColor:s.outlineColor,color:s.color,background:s.backgroundColor,border:s.border,boxShadow:s.boxShadow,textDecoration:s.textDecorationLine}; }""")
            outline=focused['outlineStyle']!='none' and float((focused['outlineWidth'] or '0px').replace('px','') or 0)>0 and focused['outlineColor'] not in {'transparent','rgba(0, 0, 0, 0)'}
            delta=any(baseline.get(key)!=focused.get(key) for key in ('color','background','border','boxShadow','textDecoration'))
            focus_visible=bool(reachable and focused['matches'] and (outline or focused['boxShadow']!='none' or delta))
            target_before=target.evaluate("""el => { const s=getComputedStyle(el), r=el.getBoundingClientRect(); return {text:el.innerText,display:s.display,visibility:s.visibility,opacity:s.opacity,color:s.color,background:s.backgroundColor,border:s.border,boxShadow:s.boxShadow,outline:s.outline,transform:s.transform,filter:s.filter,x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height}; }""")
            scroll_before=page.evaluate('() => ({x:scrollX,y:scrollY})')
            if reachable: control.press('Enter'); page.wait_for_timeout(100)
            else: ok=False
          after=control.get_attribute(state) if ok else None
          target_after=target.evaluate("""el => { const s=getComputedStyle(el), r=el.getBoundingClientRect(); return {text:el.innerText,display:s.display,visibility:s.visibility,opacity:s.opacity,color:s.color,background:s.backgroundColor,border:s.border,boxShadow:s.boxShadow,outline:s.outline,transform:s.transform,filter:s.filter,x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height}; }""") if ok else None
          scrolled=bool(ok and item.get('kind')=='navigate' and scroll_before!=page.evaluate('() => ({x:scrollX,y:scrollY})'))
          target_changed=bool(ok and (target_before!=target_after or scrolled))
          ok=bool(ok and target.is_visible() and before!=after and target_changed and focus_visible and control.evaluate('(el)=>document.activeElement===el'))
          interactions.append({'id':item['id'],'passed':ok,'state_changed':before!=after,'target_changed':target_changed,'focus_indicator_visible':focus_visible})
        try:
          page.wait_for_function("() => typeof globalThis.__autodesignPendingWork==='function' && globalThis.__autodesignPendingWork()===0",timeout=2500)
          page.wait_for_timeout(100); timers_ok=True
        except Exception:
          timers_ok=False
        runtime_grounding_ok=bool(page.evaluate(GROUNDING,plan))
        c.close()
        c=context(browser,reduced=True,width=390,height=844); mobile=c.new_page(); mobile.goto(html.as_uri(),wait_until='load',timeout=30000); mobile.wait_for_timeout(50)
        usable=0
        for item in plan['interactions']:
          control=mobile.locator('#'+item['control_id']); target=mobile.locator('#'+item['target_id']); state=item['state_attribute']
          if control.count()==1 and target.count()==1 and control.is_visible() and control.is_enabled() and target.is_visible():
            control.scroll_into_view_if_needed(); box=control.bounding_box(); style=control.evaluate("""el => { let visible=true; for(let n=el;n;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)<=0)visible=false;} return {visible,pointer:getComputedStyle(el).pointerEvents}; }""")
            target_visible=target.evaluate("""el => { for(let n=el;n;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)<=0)return false;}return true; }""")
            if box and style['visible'] and target_visible and box['width']>=24 and box['height']>=24 and box['x']>=0 and box['x']+box['width']<=390 and style['pointer']!='none':
              before=control.get_attribute(state)
              target_before=target.evaluate("""el => { const s=getComputedStyle(el), r=el.getBoundingClientRect(); return {text:el.innerText,display:s.display,visibility:s.visibility,opacity:s.opacity,color:s.color,background:s.backgroundColor,border:s.border,boxShadow:s.boxShadow,outline:s.outline,transform:s.transform,filter:s.filter,x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height}; }""")
              scroll_before=mobile.evaluate('() => ({x:scrollX,y:scrollY})'); control.click(); mobile.wait_for_timeout(100)
              target_after=target.evaluate("""el => { const s=getComputedStyle(el), r=el.getBoundingClientRect(); return {text:el.innerText,display:s.display,visibility:s.visibility,opacity:s.opacity,color:s.color,background:s.backgroundColor,border:s.border,boxShadow:s.boxShadow,outline:s.outline,transform:s.transform,filter:s.filter,x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height}; }""")
              scrolled=item.get('kind')=='navigate' and scroll_before!=mobile.evaluate('() => ({x:scrollX,y:scrollY})')
              if item.get('kind') in {'inspect','compare'} and before!=control.get_attribute(state) and (target_before!=target_after or scrolled) and target.is_visible(): usable+=1
        mobile_ok=usable>=1
        try:
          mobile.wait_for_function("() => typeof globalThis.__autodesignPendingWork==='function' && globalThis.__autodesignPendingWork()===0",timeout=2500)
          mobile.wait_for_timeout(100)
        except Exception:
          timers_ok=False
        runtime_grounding_ok=bool(runtime_grounding_ok and mobile.evaluate(GROUNDING,plan))
        c.close()
        c=context(browser,javascript=False,reduced=True); nojs=c.new_page(); nojs.goto(html.as_uri(),wait_until='load',timeout=30000)
        roles=['identity','abstract','method','evidence','results','limitations','resources','citation']
        nojs_ok=all(nojs.locator('[data-section-role="'+role+'"]').count()==1 and nojs.locator('[data-section-role="'+role+'"]').is_visible() for role in roles)
        nojs_ok=nojs_ok and nojs.locator('h1').count()==1 and nojs.locator('h1').is_visible()
        claims={item['id']:item['text'] for item in plan.get('source_claims',[])}
        claim_ids=sorted({plan['title_claim_id'],plan['thesis_claim_id'],*(claim for section in plan['sections'] for claim in section['claim_ids'])})
        visual_ids=sorted({item['visual_id'] for item in plan['visual_allocations']})
        nojs_ok=nojs_ok and bool(nojs.evaluate("""expected => {
          const intersect = (a,b) => ({left:Math.max(a.left,b.left),top:Math.max(a.top,b.top),right:Math.min(a.right,b.right),bottom:Math.min(a.bottom,b.bottom)});
          const area = r => Math.max(0,r.right-r.left)*Math.max(0,r.bottom-r.top);
          const color = value => { const m=String(value||'').match(/rgba?\(([^)]+)\)/i); if(!m)return null; const p=m[1].split(/[\s,\/]+/).filter(Boolean).map(Number); return {r:p[0],g:p[1],b:p[2],a:p.length>3?p[3]:1}; };
          const luminance = c => { const f=v=>{v/=255;return v<=.04045?v/12.92:Math.pow((v+.055)/1.055,2.4);}; return .2126*f(c.r)+.7152*f(c.g)+.0722*f(c.b); };
          const contrast = (a,b) => { const x=luminance(a),y=luminance(b); return (Math.max(x,y)+.05)/(Math.min(x,y)+.05); };
          const background = el => { for(let n=el;n;n=n.parentElement){const c=color(getComputedStyle(n).backgroundColor);if(c&&c.a>=.99)return c;}return {r:255,g:255,b:255,a:1}; };
          const visibleRect = el => {
            if(!el)return null; let result=el.getBoundingClientRect(); if(area(result)<=.5)return null;
            result=intersect(result,{left:0,top:0,right:document.documentElement.scrollWidth,bottom:document.documentElement.scrollHeight}); if(area(result)<=.5)return null;
            for(let n=el;n;n=n.parentElement){
              const s=getComputedStyle(n), r=n.getBoundingClientRect();
              if(s.display==='none'||['hidden','collapse'].includes(s.visibility)||Number(s.opacity)<=.01||s.contentVisibility==='hidden')return null;
              const clip=String(s.clip||'auto').replace(/\s+/g,'').toLowerCase();
              if(s.clipPath!=='none'||(clip!=='auto'&&clip!=='rect(auto,auto,auto,auto)')||(s.maskImage&&s.maskImage!=='none')||(s.webkitMaskImage&&s.webkitMaskImage!=='none'))return null;
              if(/opacity\(\s*0(?:\.0+)?\s*\)/i.test(s.filter||''))return null;
              if(n===el||['hidden','clip','scroll','auto'].includes(s.overflowX)||['hidden','clip','scroll','auto'].includes(s.overflowY))result=intersect(result,r);
              if(area(result)<=.5)return null;
            }
            return result;
          };
          const textPainted = el => {
            const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT); let node,seen=false;
            while((node=walker.nextNode())){
              if(!String(node.data||'').trim())continue; seen=true; const parent=node.parentElement, box=visibleRect(parent); if(!box)return false;
              const s=getComputedStyle(parent), fill=color(s.webkitTextFillColor), ink=fill||color(s.color); if(!ink||ink.a<=.01||contrast(ink,background(parent))<1.2)return false;
              const range=document.createRange(); range.selectNodeContents(node); const painted=[...range.getClientRects()].some(rect=>area(intersect(rect,box))>.5); range.detach(); if(!painted)return false;
            }
            return seen;
          };
          const visible = (el,text=false) => Boolean(visibleRect(el)) && (!text||textPainted(el));
          const tokenVisible = (attribute,id,text=false) => [...document.querySelectorAll('['+attribute+']')].some(el => (el.getAttribute(attribute)||'').split(/\s+/).includes(id) && visible(el,text));
          const normalized = value => String(value||'').normalize('NFC').replace(/\s+/g,' ').trim();
          const claimVisible = id => [...document.querySelectorAll('[data-claim-id]')].some(el => (el.getAttribute('data-claim-id')||'').split(/\s+/).includes(id) && visible(el,true) && (!expected.text[id] || normalized(el.innerText)===normalized(expected.text[id])));
          return expected.claims.every(claimVisible) && expected.visuals.every(id => tokenVisible('data-source-id',id)) && expected.missing.every(id => tokenVisible('data-missing-metadata',id,true));
        }""",{'claims':claim_ids,'text':claims,'visuals':visual_ids,'missing':plan['missing_metadata']}))
        c.close()
      finally: browser.close()
    checks={'no_javascript_core_visible':bool(nojs_ok),'runtime_source_grounding':bool(runtime_grounding_ok),'keyboard_interactions':bool(interactions) and all(x['passed'] for x in interactions),'observable_interaction_effects':bool(interactions) and all(x['target_changed'] for x in interactions),'mobile_interaction_available':bool(mobile_ok),'desktop_identity_thesis_above_fold':bool(identity_ok),'focus_indicators_visible':bool(interactions) and all(x['focus_indicator_visible'] for x in interactions),'reduced_motion_effective':bool(motion_ok),'internal_links_resolve':bool(internal_ok),'delayed_tasks_quiescent':bool(timers_ok),'no_network_attempts':not blocked and not request_errors,'no_page_errors':not page_errors}
    result={'format_version':1,'passed':all(checks.values()),'checks':checks,'interactions':interactions,'blocked_request_count':len(blocked),'request_error_count':len(request_errors),'page_error_count':len(page_errors)}
  except Exception as error:
    result={'format_version':1,'passed':False,'checks':{},'interactions':interactions,'runtime_error':type(error).__name__}
  temp=a.report.with_name('.'+a.report.name+'.tmp'); temp.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n',encoding='utf-8'); os.replace(temp,a.report)
  return 0 if result['passed'] else 2
if __name__=='__main__': raise SystemExit(main())
'''


def _run_interaction_audit(
    *,
    html_path: Path,
    workspace_root: Path,
    output_dir: Path,
    interactions: Sequence[Mapping[str, Any]],
    content_contract: Mapping[str, Any],
    runtime: Any = None,
    browser_cache: Path | None = None,
    allow_install: bool = True,
) -> dict[str, Any]:
    active = runtime
    if active is None:
        active = setup_browser.ensure_browser_runtime(
            cache_root=browser_cache, allow_install=allow_install
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    script = output_dir / ".webpage-interaction-probe.py"
    plan = output_dir / ".webpage-interactions.json"
    report = output_dir / "interaction-audit.json"
    portable.atomic_write_bytes(script, _INTERACTION_PROBE.encode("utf-8"))
    portable.atomic_write_json(
        plan,
        {
            "interactions": [dict(item) for item in interactions],
            "title_claim_id": content_contract["title_claim_id"],
            "thesis_claim_id": content_contract["thesis_claim_id"],
            "sections": [dict(item) for item in content_contract["sections"]],
            "visual_allocations": [
                dict(item) for item in content_contract["visual_allocations"]
            ],
            "missing_metadata": list(content_contract["missing_metadata"]),
            "source_claims": [
                dict(item) for item in content_contract.get("source_claims", [])
            ],
        },
    )
    env = setup_browser.isolated_environment(
        browsers_path=active.browsers_path,
        allow_network_configuration=False,
    )
    command = [
        str(active.python_executable),
        "-I",
        str(script),
        "--workspace",
        str(workspace_root),
        "--html",
        str(html_path),
        "--plan",
        str(plan),
        "--report",
        str(report),
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
            env=env,
        )
        if result.returncode not in {0, 2} or not report.is_file():
            detail = portable.redact_secrets(result.stderr or result.stdout or "probe failed")
            raise WebpageBlockedError(f"interaction browser probe failed: {str(detail)[:500]}")
        return _json_file(report)
    finally:
        script.unlink(missing_ok=True)
        plan.unlink(missing_ok=True)


BrowserAudit = Callable[..., dict[str, Any]]
InteractionAudit = Callable[..., dict[str, Any]]


def _artifact_files(attempt: Path) -> list[str]:
    artifact = attempt / "artifact"
    result: list[str] = []
    for current, directories, files in os.walk(artifact, followlinks=False):
        current_path = Path(current)
        for name in directories:
            if (current_path / name).is_symlink():
                raise WebpageContractError("artifact closure must not contain symlinks")
        for name in files:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise WebpageContractError("artifact closure contains a non-regular file")
            if path.lstat().st_nlink > 1:
                raise WebpageContractError("artifact closure must not contain hardlinked files")
            result.append(f"artifact/{path.relative_to(artifact).as_posix()}")
    return sorted(result)


def validate_webpage_attempt(
    run_dir: Path | str,
    attempt_id: str,
    *,
    browser_cache: Path | None = None,
    allow_browser_install: bool = True,
    browser_audit: BrowserAudit | None = None,
    interaction_audit: InteractionAudit | None = None,
) -> dict[str, Any]:
    """Render desktop/mobile states, verify behavior, and persist deterministic QA."""

    run = _run_root(run_dir)
    resumed = resume_webpage_run(run)
    if resumed.get("next_action") != "validate":
        raise WebpageContractError(f"run is not ready to validate: {resumed.get('next_action')}")
    attempt = _attempt_root(run, attempt_id)
    artifact = attempt / "artifact"
    html_path = artifact / "index.html"
    static = validate_webpage_html(run, attempt_id)
    previews = attempt / "qa" / "previews"
    previews.mkdir(parents=True, exist_ok=True)
    active = None
    browser_report: dict[str, Any] = {"format_version": 1, "passed": False, "skipped": True}
    interaction_report: dict[str, Any] = {"format_version": 1, "passed": False, "skipped": True}
    if static["passed"]:
        try:
            if browser_audit is None or interaction_audit is None:
                active = setup_browser.ensure_browser_runtime(
                    cache_root=browser_cache,
                    allow_install=allow_browser_install,
                )
            browser_function = browser_audit or setup_browser.audit_local_html
            browser_report = browser_function(
                html_path,
                workspace_root=attempt,
                output_dir=previews,
                viewports=("desktop:1440x1000", "mobile:390x844"),
                runtime=active,
                cache_root=browser_cache,
                allow_install=allow_browser_install,
            )
            interaction_function = interaction_audit or _run_interaction_audit
            interaction_contract = {
                **_plan_file(run),
                "source_claims": _json_file(
                    attempt / "provenance" / "source-map.json"
                ).get("claims", []),
            }
            interaction_report = interaction_function(
                html_path=html_path,
                workspace_root=artifact,
                output_dir=attempt / "qa",
                interactions=_plan_file(run)["interactions"],
                content_contract=interaction_contract,
                runtime=active,
                browser_cache=browser_cache,
                allow_install=allow_browser_install,
            )
        except setup_browser.BrowserRuntimeError as error:
            portable.atomic_write_json(
                attempt / "qa" / "browser-blocker.json",
                {"format_version": FORMAT_VERSION, "status": "blocked", "reason": str(portable.redact_secrets(str(error)))},
            )
            raise WebpageBlockedError(f"browser runtime unavailable: {error}") from error
    preview_paths: dict[str, str] = {}
    if browser_report.get("passed") is True:
        for label in ("desktop", "mobile"):
            viewport = browser_report.get("viewports", {}).get(label, {})
            screenshot_name = str(viewport.get("screenshot") or f"{label}.png")
            screenshot = previews / screenshot_name
            if screenshot.is_symlink() or not screenshot.is_file():
                browser_report = {**browser_report, "passed": False, "missing_screenshot": label}
                break
            preview_paths[label] = f"qa/previews/{screenshot_name}"
    passed = bool(
        static.get("passed")
        and browser_report.get("passed") is True
        and interaction_report.get("passed") is True
        and set(preview_paths) == {"desktop", "mobile"}
    )
    combined = {
        "format_version": FORMAT_VERSION,
        "attempt_id": attempt_id,
        "passed": passed,
        "static": static,
        "browser_passed": browser_report.get("passed") is True,
        "interaction_passed": interaction_report.get("passed") is True,
    }
    portable.atomic_write_json(artifact / "webpage-validation.json", combined)
    portable.atomic_write_json(artifact / "browser-audit.json", browser_report)
    portable.atomic_write_json(artifact / "interaction-audit.json", interaction_report)
    checks = [
        {"id": "webpage_static_contract", "passed": static.get("passed") is True, "findings": static.get("findings", [])},
        {"id": "desktop_mobile_browser", "passed": browser_report.get("passed") is True},
        {"id": "keyboard_nojs_reduced_motion", "passed": interaction_report.get("passed") is True, "checks": interaction_report.get("checks", {})},
    ]
    try:
        portable.record_deterministic_result(
            run,
            attempt_id,
            passed=passed,
            checks=checks,
            artifact_paths=_artifact_files(attempt),
            preview_paths=preview_paths,
        )
    except portable.PortableError as error:
        raise WebpageContractError(str(error)) from error
    return combined


def create_webpage_review_context(run_dir: Path | str, attempt_id: str) -> dict[str, Any]:
    run = _run_root(run_dir)
    resumed = resume_webpage_run(run)
    if resumed.get("next_action") != "semantic_review":
        raise WebpageContractError(f"run is not ready for review: {resumed.get('next_action')}")
    rubric = {**RUBRIC, "brief": _plan_file(run)["brief"]}
    return portable.create_review_context(run, attempt_id, rubric=rubric)


def _enforce_review_quality(review: Mapping[str, Any]) -> None:
    if review.get("verdict") != "pass":
        return
    scores = review.get("dimension_scores")
    if not isinstance(scores, dict) or set(scores) != set(RUBRIC["dimensions"]):
        raise WebpageContractError("passing review must score every rubric dimension")
    numeric = [float(scores[dimension]) for dimension in RUBRIC["dimensions"]]
    if (
        any(score < 3 for score in numeric)
        or sum(numeric) / len(numeric) < 4.0
        or any(float(scores[dimension]) < 4 for dimension in ("source_fidelity", "anti_slop"))
        or review.get("blockers")
    ):
        raise WebpageContractError("passing review is below the publication quality threshold")


def record_webpage_review(
    run_dir: Path | str, attempt_id: str, review: Mapping[str, Any]
) -> dict[str, Any]:
    run = _run_root(run_dir)
    resumed = resume_webpage_run(run)
    if resumed.get("next_action") != "semantic_review":
        raise WebpageContractError(f"run is not ready for review: {resumed.get('next_action')}")
    _enforce_review_quality(review)
    try:
        return portable.record_semantic_review(run, attempt_id, review)
    except portable.PortableError as error:
        raise WebpageContractError(str(error)) from error


def finalize_webpage_attempt(run_dir: Path | str, attempt_id: str) -> dict[str, Any]:
    run = _run_root(run_dir)
    resumed = resume_webpage_run(run)
    if resumed.get("next_action") not in {"finalize", "visual_review_or_finalize"}:
        raise WebpageContractError(f"run is not ready to finalize: {resumed.get('next_action')}")
    try:
        return portable.finalize_attempt(run, attempt_id)
    except portable.PortableError as error:
        raise WebpageContractError(str(error)) from error


def doctor_webpage(
    *, browser_cache: Path | None = None, allow_browser_install: bool = True
) -> dict[str, Any]:
    poppler = {
        name: shutil.which(name) for name in ("pdftotext", "pdfinfo", "pdftoppm", "pdfimages")
    }
    try:
        runtime = setup_browser.ensure_browser_runtime(
            cache_root=browser_cache,
            allow_install=allow_browser_install,
        )
        browser = {"status": "ready", "cache_dir": str(runtime.cache_dir)}
    except setup_browser.BrowserRuntimeError as error:
        browser = {"status": "blocked", "reason": str(portable.redact_secrets(str(error)))}
    return {
        "format_version": FORMAT_VERSION,
        "python": sys.version.split()[0],
        "poppler": poppler,
        "browser": browser,
        "status": "ready" if browser["status"] == "ready" else "blocked",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--browser-cache", type=Path)
    doctor.add_argument("--offline-browser", action="store_true")
    init = sub.add_parser("init")
    init.add_argument("--run-dir", type=Path, required=True)
    init.add_argument("--source", type=Path, required=True)
    init.add_argument("--asset", type=Path, action="append", default=[])
    init.add_argument("--reference", type=Path, action="append", default=[])
    init.add_argument("--release-version", default=DEFAULT_RELEASE_VERSION)
    init.add_argument("--archive-sha256")
    init.add_argument("--browser-cache", type=Path)
    init.add_argument("--skip-browser-install", action="store_true")
    plan = sub.add_parser("plan")
    plan.add_argument("--run-dir", type=Path, required=True)
    plan.add_argument("--plan-json", type=Path, required=True)
    bind_visuals = sub.add_parser("bind-visuals")
    bind_visuals.add_argument("--run-dir", type=Path, required=True)
    bind_visuals.add_argument("--review-json", type=Path, required=True)
    begin = sub.add_parser("begin")
    begin.add_argument("--run-dir", type=Path, required=True)
    stage = sub.add_parser("stage-visual")
    stage.add_argument("--run-dir", type=Path, required=True)
    stage.add_argument("--attempt", required=True)
    stage.add_argument("--visual-id", required=True)
    source_map = sub.add_parser("source-map")
    source_map.add_argument("--run-dir", type=Path, required=True)
    source_map.add_argument("--attempt", required=True)
    source_map.add_argument("--claims-json", type=Path, required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--run-dir", type=Path, required=True)
    validate.add_argument("--attempt", required=True)
    validate.add_argument("--browser-cache", type=Path)
    validate.add_argument("--offline-browser", action="store_true")
    review_context = sub.add_parser("review-context")
    review_context.add_argument("--run-dir", type=Path, required=True)
    review_context.add_argument("--attempt", required=True)
    record = sub.add_parser("record-review")
    record.add_argument("--run-dir", type=Path, required=True)
    record.add_argument("--attempt", required=True)
    record.add_argument("--review-json", type=Path, required=True)
    finalize = sub.add_parser("finalize")
    finalize.add_argument("--run-dir", type=Path, required=True)
    finalize.add_argument("--attempt", required=True)
    status = sub.add_parser("status")
    status.add_argument("--run-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            value = doctor_webpage(
                browser_cache=args.browser_cache,
                allow_browser_install=not args.offline_browser,
            )
        elif args.command == "init":
            value = initialize_webpage_run(
                args.run_dir,
                args.source,
                extra_assets=args.asset,
                reference_images=args.reference,
                release_version=args.release_version,
                archive_sha256=args.archive_sha256,
                install_browser=not args.skip_browser_install,
                browser_cache=args.browser_cache,
            )
        elif args.command == "plan":
            value = save_webpage_plan(args.run_dir, _json_file(args.plan_json))
        elif args.command == "bind-visuals":
            value = bind_webpage_visuals(args.run_dir, _json_file(args.review_json))
        elif args.command == "begin":
            value = {"attempt_id": begin_webpage_attempt(args.run_dir)}
        elif args.command == "stage-visual":
            value = {"path": stage_visual(args.run_dir, args.attempt, args.visual_id)}
        elif args.command == "source-map":
            payload = _json_document(args.claims_json)
            claims = payload.get("claims") if isinstance(payload, dict) else payload
            if not isinstance(claims, list):
                raise WebpageContractError("claims JSON must be a list or {claims: [...]} object")
            value = write_webpage_source_map(args.run_dir, args.attempt, claims)
        elif args.command == "validate":
            value = validate_webpage_attempt(
                args.run_dir,
                args.attempt,
                browser_cache=args.browser_cache,
                allow_browser_install=not args.offline_browser,
            )
        elif args.command == "review-context":
            value = create_webpage_review_context(args.run_dir, args.attempt)
        elif args.command == "record-review":
            value = record_webpage_review(
                args.run_dir, args.attempt, _json_file(args.review_json)
            )
        elif args.command == "finalize":
            value = finalize_webpage_attempt(args.run_dir, args.attempt)
        else:
            value = resume_webpage_run(args.run_dir)
        _emit(value)
        return 0
    except WebpageBlockedError as error:
        print(f"BLOCKED: {portable.redact_secrets(str(error))}", file=sys.stderr)
        return 2
    except (OSError, ValueError, json.JSONDecodeError, portable.PortableError, WebpageHarnessError) as error:
        print(f"ERROR: {portable.redact_secrets(str(error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
