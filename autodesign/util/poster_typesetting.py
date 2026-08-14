"""Deterministic final HTML typesetting rhythm repairs for paper posters."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

from .io import sha256_file


_STYLE_MARKER = "typesetting-rhythm-v1"


def apply_poster_typesetting_patch(
    html_path: Path,
    *,
    record_path: Path | None = None,
    backup_path: Path | None = None,
    phase: str = "final",
) -> dict[str, Any]:
    """Inject a scoped poster rhythm stylesheet into a standalone poster HTML.

    The pass intentionally avoids text edits and DOM restructuring. It normalizes
    paragraph/list/table line rhythm and list marker geometry through a single
    idempotent style tag so later validation can accept or reject the visual
    consequences.
    """

    html_path = Path(html_path)
    if not html_path.exists():
        return {"version": 1, "applied": False, "reason": "html_missing", "html": str(html_path)}
    text = html_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")
    root = _poster_root(soup)
    if root is None:
        return {"version": 1, "applied": False, "reason": "poster_root_missing", "html": str(html_path)}
    if soup.head is None:
        html_tag = soup.html or soup.new_tag("html")
        if soup.html is None:
            soup.insert(0, html_tag)
        head = soup.new_tag("head")
        html_tag.insert(0, head)
    assert soup.head is not None

    profile = _rhythm_profile(root)
    css = _typesetting_css(profile)
    existing = _existing_marker_styles(soup)
    if len(existing) == 1 and (existing[0].string or "").strip() == css.strip():
        record = {
            "version": 1,
            "applied": False,
            "reason": "already_current",
            "html": str(html_path),
            "phase": phase,
            "profile": profile,
            "before_sha256": sha256_file(html_path),
            "after_sha256": sha256_file(html_path),
        }
        _write_record(record_path, record)
        return record
    before_sha = sha256_file(html_path)
    for style in existing:
        style.decompose()
    style_tag = soup.new_tag("style")
    style_tag["data-od-auto-repair"] = _STYLE_MARKER
    style_tag.string = css
    soup.head.append(style_tag)

    backup = backup_path or html_path.with_name(f"{html_path.stem}.before_typesetting{html_path.suffix}")
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(html_path, backup)
    html_path.write_text(str(soup), encoding="utf-8")
    record = {
        "version": 1,
        "applied": True,
        "repair": "typesetting_rhythm",
        "phase": phase,
        "html": str(html_path),
        "backup": str(backup),
        "profile": profile,
        "before_sha256": before_sha,
        "after_sha256": sha256_file(html_path),
        "style_marker": _STYLE_MARKER,
    }
    _write_record(record_path, record)
    return record


def revert_poster_typesetting_patch(record: dict[str, Any]) -> bool:
    html = Path(str(record.get("html") or ""))
    backup = Path(str(record.get("backup") or ""))
    if not html.exists() or not backup.exists():
        return False
    shutil.copy2(backup, html)
    return True


def _poster_root(soup: BeautifulSoup) -> Tag | None:
    for selector in (".paper-poster", ".editorial-poster", "[data-render-mode='authored_html']"):
        found = soup.select_one(selector)
        if isinstance(found, Tag):
            return found
    return None


def _existing_marker_styles(soup: BeautifulSoup) -> list[Tag]:
    return [
        style for style in soup.find_all("style", attrs={"data-od-auto-repair": _STYLE_MARKER})
        if isinstance(style, Tag)
    ]


def _rhythm_profile(root: Tag) -> dict[str, Any]:
    words = len(re.findall(r"\b[\w'-]+\b", root.get_text(" ", strip=True)))
    if words >= 1800:
        body_line = 1.15
        paragraph_gap = 0.30
        list_gap = 0.18
    elif words >= 1250:
        body_line = 1.18
        paragraph_gap = 0.36
        list_gap = 0.20
    elif words >= 850:
        body_line = 1.21
        paragraph_gap = 0.42
        list_gap = 0.24
    else:
        body_line = 1.24
        paragraph_gap = 0.48
        list_gap = 0.28
    return {
        "word_count": words,
        "body_line": body_line,
        "compact_line": max(1.10, body_line - 0.05),
        "table_line": max(1.05, body_line - 0.10),
        "paragraph_gap_em": paragraph_gap,
        "list_item_gap_em": list_gap,
        "bullet_indent_em": 1.05,
        "source_flow_bullet_indent_em": 1.25,
    }


def _typesetting_css(profile: dict[str, Any]) -> str:
    body_line = float(profile["body_line"])
    compact_line = float(profile["compact_line"])
    table_line = float(profile["table_line"])
    paragraph_gap = float(profile["paragraph_gap_em"])
    list_gap = float(profile["list_item_gap_em"])
    bullet_indent = float(profile["bullet_indent_em"])
    source_flow_bullet_indent = float(profile["source_flow_bullet_indent_em"])
    return (
        "/* AutoDesign auto repair: final poster typesetting rhythm v1. */\n"
        ".paper-poster{\n"
        f"  --od-body-line:{body_line:.2f};\n"
        f"  --od-compact-line:{compact_line:.2f};\n"
        f"  --od-table-line:{table_line:.2f};\n"
        f"  --od-paragraph-gap:{paragraph_gap:.2f}em;\n"
        f"  --od-list-item-gap:{list_gap:.2f}em;\n"
        f"  --od-bullet-indent:{bullet_indent:.2f}em;\n"
        f"  --od-source-flow-bullet-indent:{source_flow_bullet_indent:.2f}em;\n"
        "}\n"
        ".paper-poster :where(p,li,[data-block-kind=\"text\"],[data-block-kind=\"quote\"]):not(.formula):not(.math-block):not(.equation):not([data-block-kind=\"formula\"]):not([data-role~=\"formula\"]){\n"
        "  line-height:var(--od-body-line)!important;\n"
        "}\n"
        ".paper-poster :where(.formula,.math-block,.equation,[data-block-kind=\"formula\"],[data-role~=\"formula\"]){\n"
        "  line-height:1.12!important;\n"
        "}\n"
        ".paper-poster :where(.formula,.math-block,.equation,[data-block-kind=\"formula\"],[data-role~=\"formula\"]) :where(.katex,.katex *){\n"
        "  line-height:normal!important;\n"
        "}\n"
        ".paper-poster :where(figcaption,[data-block-kind=\"caption\"],.caption,.readout,.source-readout,.source-note){\n"
        "  line-height:var(--od-compact-line)!important;\n"
        "}\n"
        ".paper-poster :where(table,thead,tbody,tfoot,tr,th,td){\n"
        "  line-height:var(--od-table-line)!important;\n"
        "}\n"
        ".paper-poster :where(p){\n"
        "  margin-block:0 var(--od-paragraph-gap)!important;\n"
        "}\n"
        ".paper-poster :where(ul,ol){\n"
        "  margin-block:calc(var(--od-paragraph-gap) * .7) var(--od-paragraph-gap)!important;\n"
        "  margin-inline-start:.35em!important;\n"
        "}\n"
        ".paper-poster ul{\n"
        "  list-style-position:outside!important;\n"
        "  list-style-type:disc!important;\n"
        "  padding-inline-start:var(--od-bullet-indent)!important;\n"
        "}\n"
        ".paper-poster ul>li{\n"
        "  padding-inline-start:.08em!important;\n"
        "  margin-block:var(--od-list-item-gap)!important;\n"
        "}\n"
        ".paper-poster ul>li::marker{\n"
        "  font-size:.72em;\n"
        "  font-weight:700;\n"
        "}\n"
        ".paper-poster ol{\n"
        "  padding-inline-start:1.35em!important;\n"
        "}\n"
        ".paper-poster ol>li{\n"
        "  padding-inline-start:.12em!important;\n"
        "  margin-block:var(--od-list-item-gap)!important;\n"
        "}\n"
        ".paper-poster :where(.source-flow-unit,.figure-flow-unit)>:where(ul,ol){\n"
        "  display:flow-root!important;\n"
        "  margin-block:calc(var(--od-paragraph-gap) * .6) var(--od-paragraph-gap)!important;\n"
        "  margin-inline-start:0!important;\n"
        "  padding-inline-start:var(--od-source-flow-bullet-indent)!important;\n"
        "  list-style-position:outside!important;\n"
        "}\n"
        ".paper-poster :where(.source-flow-unit,.figure-flow-unit)>ul>li,\n"
        ".paper-poster :where(.source-flow-unit,.figure-flow-unit)>ol>li{\n"
        "  padding-inline-start:.28em!important;\n"
        "}\n"
        ".paper-poster :where(li>p,li>div){\n"
        "  margin-block:0!important;\n"
        "  line-height:inherit!important;\n"
        "}\n"
        ".paper-poster :where(.poster-section,section[data-block-id])>:first-child{margin-top:0!important;}\n"
        ".paper-poster :where(.poster-section,section[data-block-id])>:last-child{margin-bottom:0!important;}\n"
    ).strip()


def _write_record(record_path: Path | None, record: dict[str, Any]) -> None:
    if record_path is None:
        return
    record_path = Path(record_path)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
