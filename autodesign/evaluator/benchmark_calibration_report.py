"""Small report helpers for poster benchmark calibration review.

These helpers prepare anonymous visual review inputs and render supplied audit
fields. They intentionally do not score posters, reweight metrics, or branch on
specific methods/systems.
"""

from __future__ import annotations

from collections import defaultdict
import html as H
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image, ImageDraw, ImageOps


_ARTIFACT_FIELDS = (
    "artifact",
    "artifact_path",
    "preview",
    "preview_path",
    "image",
    "image_path",
    "poster_png",
    "path",
)


def build_anonymous_system_contact_sheet(
    records: Sequence[Mapping[str, Any]],
    out_path: Path,
    *,
    max_items: int = 100,
    columns: int = 10,
    thumb_size: tuple[int, int] = (320, 180),
    gap: int = 10,
) -> dict[str, Any]:
    """Build a per-system contact sheet without labels in the rendered pixels."""
    selected = list(records)[:max(0, max_items)]
    notes: list[str] = []
    if len(records) > len(selected):
        notes.append(f"truncated from {len(records)} records to {len(selected)} contact-sheet items")

    rendered, missing = _write_contact_sheet_grid(
        selected,
        out_path,
        columns=columns,
        thumb_size=thumb_size,
        gap=gap,
        notes=notes,
    )
    return {
        "image_path": str(out_path),
        "records_seen": len(records),
        "items_total": len(selected),
        "items_rendered": rendered,
        "missing_artifacts": missing,
        "degraded_notes": notes,
        "batch_style_judge_input": {
            "image_path": str(out_path),
            "labels_in_pixels": False,
            "system_labels_in_pixels": False,
        },
    }


def build_same_paper_comparison_section(
    records: Sequence[Mapping[str, Any]],
    out_dir: Path,
    *,
    max_papers: int = 30,
    image_name: str = "same_paper_comparison_contact_sheet.jpg",
    thumb_size: tuple[int, int] = (260, 145),
    gap: int = 10,
) -> dict[str, Any]:
    """Build an anonymous same-paper sheet plus an HTML report section.

    The contact sheet is suitable as a style-judge input because it contains no
    system labels in the pixels. System labels are present only in the returned
    HTML section for the surrounding report.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    grouped = _group_records_by_paper(records)
    comparable = [(key, rows) for key, rows in grouped if len(rows) >= 2]
    singletons = [(key, rows) for key, rows in grouped if len(rows) < 2]
    selected = _balanced_group_selection(comparable, max(0, max_papers))
    if not selected and singletons:
        selected = _balanced_group_selection(singletons, max(0, max_papers))
        notes.append("no same-paper comparison groups with multiple systems; rendered singleton groups")
    if len(comparable) > len(selected):
        notes.append(f"truncated from {len(comparable)} same-paper groups to {len(selected)}")
    if len(comparable) < max_papers:
        notes.append(f"only {len(comparable)} same-paper comparison groups available")

    max_columns = max((len(rows) for _, rows in selected), default=1)
    sheet_path = out_dir / image_name
    rendered, missing, positions = _write_comparison_sheet(
        selected,
        sheet_path,
        max_columns=max_columns,
        thumb_size=thumb_size,
        gap=gap,
        notes=notes,
    )
    html_section = _same_paper_html_section(sheet_path, positions)
    return {
        "contact_sheet_path": str(sheet_path),
        "html_section": html_section,
        "papers_selected": len(selected),
        "selected_discipline_counts": {
            discipline: sum(1 for key, _rows in selected if key[0] == discipline)
            for discipline in sorted({key[0] for key, _rows in selected})
        },
        "items_total": sum(len(rows) for _, rows in selected),
        "items_rendered": rendered,
        "missing_artifacts": missing,
        "degraded_notes": notes,
        "batch_style_judge_input": {
            "image_path": str(sheet_path),
            "labels_in_pixels": False,
            "system_labels_in_pixels": False,
        },
    }


def _balanced_group_selection(
    groups: Sequence[tuple[tuple[str, str], list[Mapping[str, Any]]]],
    limit: int,
) -> list[tuple[tuple[str, str], list[Mapping[str, Any]]]]:
    if limit <= 0:
        return []
    by_discipline: dict[str, list[tuple[tuple[str, str], list[Mapping[str, Any]]]]] = defaultdict(list)
    for group in groups:
        by_discipline[group[0][0]].append(group)
    selected: list[tuple[tuple[str, str], list[Mapping[str, Any]]]] = []
    index = 0
    disciplines = sorted(by_discipline)
    while len(selected) < limit:
        added = False
        for discipline in disciplines:
            rows = by_discipline[discipline]
            if index < len(rows):
                selected.append(rows[index])
                added = True
                if len(selected) >= limit:
                    break
        if not added:
            break
        index += 1
    return selected


def render_system_explainability_fields(system_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Render supplied per-system calibration fields as an HTML table."""
    degraded_notes: list[str] = []
    body: list[str] = []
    for row in system_rows:
        normalized, missing = _normalize_explainability_row(row)
        if missing:
            degraded_notes.append(
                f"{normalized['system_label']} missing explainability fields: {', '.join(missing)}"
            )
        body.append(
            "<tr>"
            f"<td><b>{_esc(normalized['system_label'])}</b></td>"
            f"<td class='num'>{_fmt(normalized['raw_professional_aesthetics'])}</td>"
            f"<td class='num'>{_fmt(normalized['adjusted_professional_aesthetics'])}</td>"
            f"<td class='num'>{_fmt(normalized['style_adaptability'])}</td>"
            f"<td class='num'>{_fmt(normalized['homogeneity_adjustment'])}</td>"
            f"<td class='num'>{_fmt(normalized['evidence_group_count'])}</td>"
            f"<td class='num'>{_fmt(normalized['evidence_area_ratio'])}</td>"
            f"<td class='num'>{_fmt(normalized['legibility_cap'])}</td>"
            f"<td>{_esc(normalized['trusted_layout_p1_source'])}</td>"
            f"<td class='num'>{_fmt(normalized['trusted_layout_p1_count'])}</td>"
            f"<td class='num'>{_fmt(normalized['trusted_layout_p1_rate'])}</td>"
            f"<td class='num'>{_fmt(normalized['presentation_viability_mean'])}</td>"
            f"<td class='num'>{_fmt(normalized['presentation_viability_trigger_count'])}</td>"
            f"<td class='num'>{_fmt(normalized['presentation_viability_trigger_rate'])}</td>"
            f"<td class='num'>{_fmt(normalized['presentation_viability_ceiling'])}</td>"
            f"<td>{_fmt_weak_dimensions(normalized['presentation_viability_weak_dimensions'])}</td>"
            "</tr>"
        )

    html = (
        "<p class='small presentation-viability-note'>Presentation viability is a method-agnostic "
        "non-compensability/pass-eligibility rule, not a method penalty.</p>"
        "<table class='benchmark-calibration-explainability'>"
        "<thead><tr>"
        "<th>System</th>"
        "<th>Raw professional aesthetics</th>"
        "<th>Adjusted professional aesthetics</th>"
        "<th>Style adaptability</th>"
        "<th>Homogeneity adjustment</th>"
        "<th>Evidence group count</th>"
        "<th>Evidence area</th>"
        "<th>Legibility cap</th>"
        "<th>Trusted layout P1 source</th>"
        "<th>Trusted layout P1 count</th>"
        "<th>Trusted layout P1 rate</th>"
        "<th>Presentation viability mean</th>"
        "<th>Presentation viability trigger count</th>"
        "<th>Presentation viability trigger rate</th>"
        "<th>Presentation viability ceiling</th>"
        "<th>Weak dimensions</th>"
        "</tr></thead>"
        f"<tbody>{''.join(body) or '<tr><td colspan=\"16\">No system rows supplied.</td></tr>'}</tbody>"
        "</table>"
    )
    return {
        "html": html,
        "rows_rendered": len(system_rows),
        "degraded_notes": degraded_notes,
    }


def _write_contact_sheet_grid(
    records: Sequence[Mapping[str, Any]],
    out_path: Path,
    *,
    columns: int,
    thumb_size: tuple[int, int],
    gap: int,
    notes: list[str],
) -> tuple[int, int]:
    columns = max(1, columns)
    rows = max(1, (len(records) + columns - 1) // columns)
    thumb_w, thumb_h = thumb_size
    canvas = Image.new(
        "RGB",
        (columns * thumb_w + (columns + 1) * gap, rows * thumb_h + (rows + 1) * gap),
        "white",
    )
    draw = ImageDraw.Draw(canvas)
    rendered = 0
    missing = 0
    for idx, record in enumerate(records):
        col = idx % columns
        row = idx // columns
        x = gap + col * (thumb_w + gap)
        y = gap + row * (thumb_h + gap)
        status = _paste_record_image(canvas, draw, record, (x, y), thumb_size)
        if status is None:
            rendered += 1
        else:
            missing += 1
            notes.append(_missing_note(record, status))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return rendered, missing


def _write_comparison_sheet(
    groups: Sequence[tuple[tuple[str, str], list[Mapping[str, Any]]]],
    out_path: Path,
    *,
    max_columns: int,
    thumb_size: tuple[int, int],
    gap: int,
    notes: list[str],
) -> tuple[int, int, list[dict[str, Any]]]:
    max_columns = max(1, max_columns)
    rows = max(1, len(groups))
    thumb_w, thumb_h = thumb_size
    canvas = Image.new(
        "RGB",
        (max_columns * thumb_w + (max_columns + 1) * gap, rows * thumb_h + (rows + 1) * gap),
        "white",
    )
    draw = ImageDraw.Draw(canvas)
    rendered = 0
    missing = 0
    positions: list[dict[str, Any]] = []
    for row_idx, (paper_key, paper_records) in enumerate(groups):
        sorted_records = sorted(
            paper_records,
            key=lambda item: (str(item.get("system_label") or item.get("system") or ""), str(item.get("artifact") or "")),
        )
        entries: list[dict[str, str]] = []
        for col_idx, record in enumerate(sorted_records):
            x = gap + col_idx * (thumb_w + gap)
            y = gap + row_idx * (thumb_h + gap)
            status = _paste_record_image(canvas, draw, record, (x, y), thumb_size)
            if status is None:
                rendered += 1
            else:
                missing += 1
                notes.append(_missing_note(record, status))
            entries.append({
                "column": f"C{col_idx + 1}",
                "system_label": str(record.get("system_label") or record.get("system") or "Unknown system"),
                "artifact": str(_artifact_path(record) or ""),
            })
        positions.append({
            "paper_code": f"P{row_idx + 1:02d}",
            "discipline": paper_key[0],
            "case": paper_key[1],
            "entries": entries,
        })
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return rendered, missing, positions


def _paste_record_image(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    record: Mapping[str, Any],
    xy: tuple[int, int],
    thumb_size: tuple[int, int],
) -> str | None:
    x, y = xy
    thumb_w, thumb_h = thumb_size
    path = _artifact_path(record)
    if path is None:
        _draw_missing_tile(draw, x, y, thumb_w, thumb_h)
        return "missing artifact path"
    if not path.exists():
        _draw_missing_tile(draw, x, y, thumb_w, thumb_h)
        return f"missing artifact: {path}"
    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image.convert("RGB"))
            thumb = ImageOps.contain(image, thumb_size, Image.Resampling.LANCZOS)
    except Exception as exc:  # noqa: BLE001
        _draw_missing_tile(draw, x, y, thumb_w, thumb_h)
        return f"unreadable artifact: {path} ({type(exc).__name__}: {exc})"
    tile = Image.new("RGB", thumb_size, "white")
    tile.paste(thumb, ((thumb_w - thumb.width) // 2, (thumb_h - thumb.height) // 2))
    canvas.paste(tile, (x, y))
    draw.rectangle((x, y, x + thumb_w - 1, y + thumb_h - 1), outline="#d0d5dd")
    return None


def _draw_missing_tile(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int) -> None:
    draw.rectangle((x, y, x + w - 1, y + h - 1), fill="#f2f4f7", outline="#c7ccd4")
    draw.line((x + 8, y + 8, x + w - 9, y + h - 9), fill="#98a2b3", width=2)
    draw.line((x + w - 9, y + 8, x + 8, y + h - 9), fill="#98a2b3", width=2)


def _group_records_by_paper(
    records: Sequence[Mapping[str, Any]],
) -> list[tuple[tuple[str, str], list[Mapping[str, Any]]]]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    fallback_idx = 0
    for record in records:
        key = _paper_key(record)
        if key is None:
            fallback_idx += 1
            key = ("unknown", f"record-{fallback_idx:03d}")
        grouped[key].append(record)
    return sorted(grouped.items(), key=lambda item: item[0])


def _paper_key(record: Mapping[str, Any]) -> tuple[str, str] | None:
    discipline = str(record.get("discipline_label") or record.get("discipline") or "").strip()
    case = str(record.get("case") or record.get("case_slug") or record.get("paper_id") or "").strip()
    if case:
        return (discipline or "unknown", case)
    paper = record.get("paper") or record.get("paper_path")
    if paper:
        return (discipline or "unknown", Path(str(paper)).stem)
    return None


def _same_paper_html_section(sheet_path: Path, positions: Sequence[Mapping[str, Any]]) -> str:
    rows: list[str] = []
    for position in positions:
        entries = position.get("entries") or []
        labels = "; ".join(
            f"{_esc(entry.get('column'))}: {_esc(entry.get('system_label'))}"
            for entry in entries
            if isinstance(entry, Mapping)
        )
        rows.append(
            "<tr>"
            f"<td>{_esc(position.get('paper_code'))}</td>"
            f"<td>{_esc(position.get('discipline'))}</td>"
            f"<td><code>{_esc(position.get('case'))}</code></td>"
            f"<td>{labels}</td>"
            "</tr>"
        )
    return (
        "<section class='same-paper-comparison'>"
        "<h2>Same-paper comparison</h2>"
        "<p class='small'>The contact sheet image is anonymous; system labels are shown only in this report section.</p>"
        f"<img src='{_esc(sheet_path.name)}' alt='Anonymous same-paper comparison contact sheet'>"
        "<table><thead><tr><th>Paper</th><th>Discipline</th><th>Case</th><th>Report-only system labels</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=\"4\">No comparison groups available.</td></tr>'}</tbody></table>"
        "</section>"
    )


def _normalize_explainability_row(row: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    trusted = _trusted_layout_p1(row)
    values = {
        "system_label": str(row.get("system_label") or row.get("system") or "Unknown system"),
        "raw_professional_aesthetics": _first_value(
            row,
            ("raw_professional_aesthetics",),
            ("professional_aesthetics_raw",),
            ("dimension_raw", "professional_aesthetics"),
            ("dimension_components", "professional_aesthetics", "raw_score_0_10"),
        ),
        "adjusted_professional_aesthetics": _first_value(
            row,
            ("adjusted_professional_aesthetics",),
            ("professional_aesthetics_adjusted",),
            ("dimensions", "professional_aesthetics"),
            ("professional_aesthetics",),
        ),
        "style_adaptability": _first_value(
            row,
            ("style_adaptability",),
            ("style", "adaptability"),
            ("metrics", "style_adaptability"),
        ),
        "homogeneity_adjustment": _first_value(
            row,
            ("homogeneity_adjustment",),
            ("homogeneity", "adjustment"),
            ("metrics", "homogeneity_adjustment"),
        ),
        "evidence_group_count": _first_value(
            row,
            ("evidence_group_count",),
            ("metrics", "evidence_group_count"),
        ),
        "evidence_area_ratio": _first_value(
            row,
            ("evidence_area_ratio",),
            ("evidence_area",),
            ("metrics", "evidence_area_ratio"),
        ),
        "legibility_cap": _first_value(
            row,
            ("legibility_cap",),
            ("layout_readability_cap",),
            ("dimension_caps", "layout_readability"),
            ("metrics", "legibility_cap"),
        ),
        "trusted_layout_p1_source": _first_value(
            row,
            ("trusted_layout_p1_source",),
            ("trusted_layout_p1", "source"),
        ) or trusted.get("source"),
        "trusted_layout_p1_count": _first_value(
            row,
            ("trusted_layout_p1_count",),
        ),
        "trusted_layout_p1_rate": _first_value(
            row,
            ("trusted_layout_p1_rate",),
        ),
        "presentation_viability_mean": _first_value(
            row,
            ("presentation_viability_mean",),
            ("presentation_viability", "mean"),
        ),
        "presentation_viability_trigger_count": _first_value(
            row,
            ("presentation_viability_trigger_count",),
            ("presentation_viability", "trigger_count"),
        ),
        "presentation_viability_trigger_rate": _first_value(
            row,
            ("presentation_viability_trigger_rate",),
            ("presentation_viability", "trigger_rate"),
        ),
        "presentation_viability_ceiling": _first_value(
            row,
            ("presentation_viability_ceiling",),
            ("presentation_viability", "ceiling"),
        ),
        "presentation_viability_weak_dimensions": _first_value(
            row,
            ("presentation_viability_weak_dimensions",),
            ("presentation_viability", "weak_dimensions"),
        ),
    }
    missing = [
        key
        for key, value in values.items()
        if key != "system_label" and _is_missing(value)
    ]
    return values, missing


def _trusted_layout_p1(row: Mapping[str, Any]) -> dict[str, Any]:
    findings = row.get("findings") or []
    if not isinstance(findings, Iterable) or isinstance(findings, (str, bytes)):
        return {}
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        if str(finding.get("severity") or "").upper() != "P1":
            continue
        dimension = str(finding.get("dimension") or "")
        if dimension and dimension != "basic_layout_integrity":
            continue
        evidence = finding.get("evidence") if isinstance(finding.get("evidence"), Mapping) else {}
        return {
            "source": evidence.get("source") or finding.get("id"),
            "confidence": evidence.get("confidence") or evidence.get("trusted_confidence"),
        }
    return {}


def _first_value(row: Mapping[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = row
        for part in path:
            if isinstance(current, Mapping) and part in current:
                current = current[part]
            else:
                current = None
                break
        if not _is_missing(current):
            return current
    return None


def _artifact_path(record: Mapping[str, Any]) -> Path | None:
    for field in _ARTIFACT_FIELDS:
        value = record.get(field)
        if isinstance(value, Mapping):
            value = value.get("path") or value.get("file") or value.get("uri")
        if value:
            text = str(value)
            if text.startswith("file://"):
                return Path(text[7:])
            return Path(text)
    return None


def _missing_note(record: Mapping[str, Any], status: str) -> str:
    case = record.get("case") or record.get("paper_id") or record.get("candidate_name") or "unknown case"
    system = record.get("system_label") or record.get("system") or "unknown system"
    return f"{system} / {case}: {status}"


def _fmt(value: Any) -> str:
    if _is_missing(value):
        return "—"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return _esc(value)


def _fmt_weak_dimensions(value: Any) -> str:
    if _is_missing(value):
        return "—"
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        return ", ".join(_esc(item) for item in value)
    return _esc(value)


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return H.escape(str(value))


def _is_missing(value: Any) -> bool:
    return value is None or value == ""
