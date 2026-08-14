"""Exact selected-palette validation for non-Poster HTML artifacts."""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from ..tools.propose_paper_poster_html import authored_palette_diagnostics


def validate_artifact_palette(
    html: str,
    css: str,
    required_color_system: dict[str, Any],
    artifact_prefix: str,
) -> dict[str, Any]:
    """Audit a non-Poster document using the established Poster palette parser."""
    prefix = str(artifact_prefix or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", prefix):
        raise ValueError("artifact_prefix must be lowercase snake_case")

    required_palette_id = str(required_color_system.get("palette_id") or "").strip()
    if not required_palette_id:
        raise ValueError("required_color_system.palette_id is required")

    soup = BeautifulSoup(str(html or ""), "html.parser")
    root = soup.html or soup.find(True)
    actual_palette_id = ""
    if isinstance(root, Tag):
        actual_palette_id = str(root.get("data-palette-id") or "").strip()
        root_classes = [str(item) for item in (root.get("class") or [])]
        if "paper-poster" not in root_classes:
            root["class"] = [*root_classes, "paper-poster"]

    diagnostics = authored_palette_diagnostics(
        str(soup),
        str(css or ""),
        required_color_system,
        require_selected=True,
    )
    css_variable_mismatches = _diagnostic_items(
        diagnostics,
        "paper_poster_html_palette_css_variable_mismatch",
        "mismatches",
    )
    strict_soup = BeautifulSoup(str(soup), "html.parser")
    _isolate_source_media_from_authored_captions(strict_soup)
    strict_diagnostics = authored_palette_diagnostics(
        str(strict_soup),
        str(css or ""),
        required_color_system,
        require_selected=True,
    )
    shell_extra_colors = _diagnostic_colors(strict_diagnostics, "shell")
    source_visual_extra_colors = _diagnostic_colors(diagnostics, "source_visual")

    findings: list[dict[str, Any]] = []
    if not actual_palette_id:
        findings.append({
            "issue_id": f"{prefix}_required_palette_id_missing",
            "message": "document root must declare the required data-palette-id",
            "required_palette_id": required_palette_id,
            "actual_palette_id": "",
        })
    elif actual_palette_id != required_palette_id:
        findings.append({
            "issue_id": f"{prefix}_required_palette_id_mismatch",
            "message": "document root data-palette-id does not match the required palette",
            "required_palette_id": required_palette_id,
            "actual_palette_id": actual_palette_id,
        })
    elif css_variable_mismatches:
        findings.append({
            "issue_id": f"{prefix}_required_palette_css_variable_mismatch",
            "message": "document root must define every exact required --poster-* CSS variable",
            "required_palette_id": required_palette_id,
            "mismatches": css_variable_mismatches,
        })

    if shell_extra_colors:
        findings.append({
            "issue_id": f"{prefix}_required_palette_foreign_shell_color",
            "message": "authored shell and UI colors must come from the required palette",
            "required_palette_id": required_palette_id,
            "shell_extra_colors": shell_extra_colors,
        })

    return {
        "accepted": not findings,
        "blocking_findings": findings,
        "debug_metrics": {
            "artifact_prefix": prefix,
            "palette_contract_pass": not findings,
            "required_palette_id": required_palette_id,
            "actual_palette_id": actual_palette_id,
            "raw_diagnostic_count": len(diagnostics),
            "raw_diagnostic_ids": [
                str(item.get("issue_id") or "")
                for item in diagnostics
                if isinstance(item, dict)
            ],
            "css_variable_mismatches": css_variable_mismatches,
            "shell_extra_colors": shell_extra_colors,
            "source_visual_extra_colors": source_visual_extra_colors,
        },
    }


def _diagnostic_items(
    diagnostics: list[dict[str, Any]],
    issue_id: str,
    field: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for diagnostic in diagnostics:
        if str(diagnostic.get("issue_id") or "") != issue_id:
            continue
        values = diagnostic.get(field)
        if isinstance(values, list):
            items.extend(dict(item) for item in values if isinstance(item, dict))
    return items


def _diagnostic_colors(
    diagnostics: list[dict[str, Any]],
    scope: str,
) -> list[str]:
    colors: set[str] = set()
    for diagnostic in diagnostics:
        if str(diagnostic.get("issue_id") or "") != "paper_poster_html_palette_extra_authored_hex":
            continue
        for field in (f"{scope}_extra_colors", f"{scope}_extra_hexes"):
            values = diagnostic.get(field)
            if isinstance(values, list):
                colors.update(str(item) for item in values if str(item or "").strip())
    return sorted(colors)


def _isolate_source_media_from_authored_captions(soup: BeautifulSoup) -> None:
    for wrapper in soup.select("figure[data-source-id], picture[data-source-id]"):
        if not isinstance(wrapper, Tag):
            continue
        source_id = str(wrapper.get("data-source-id") or "").strip()
        if not source_id:
            continue
        media = wrapper.find_all(["img", "picture", "svg", "canvas", "table"])
        for tag in media:
            if isinstance(tag, Tag) and not str(tag.get("data-source-id") or "").strip():
                tag["data-source-id"] = source_id
        del wrapper["data-source-id"]
