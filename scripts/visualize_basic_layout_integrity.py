#!/usr/bin/env python3
"""Visualize the image-native basic layout integrity detector.

The report is meant for human inspection while tuning the benchmark rubric. It
draws exactly the signals the detector uses: the heading band, export edge
safety band, OCR boxes, tiny-text boxes, edge-touching text boxes, and
edge-occupied content cells.

Usage:
    uv run python scripts/visualize_basic_layout_integrity.py \
        --out-dir out/eval/report/basic_layout_integrity
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image, ImageDraw, ImageFont

from autodesign.evaluator.ocr import run_ocr
from autodesign.evaluator.spatial import (
    REFERENCE_LONG_EDGE,
    basic_layout_integrity,
    content_occupancy,
)
from autodesign.util.browser_render import screenshot_html


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=_REPO_ROOT / "out" / "eval" / "report" / "basic_layout_integrity")
    parser.add_argument("--candidate", action="append", default=[], help="真实海报输入，格式 name=/path/to/poster.png。可重复。")
    parser.add_argument("--include-real-posters", action="store_true", help="自动读取 out/eval/real_poster_*/candidates/*/poster_quality_report.json 里的真实海报。")
    parser.add_argument("--only-real", action="store_true", help="只输出真实海报，不包含 fixture 和合成 probe cases。")
    parser.add_argument("--only-synthetic", action="store_true", help="只输出合成 probe/stress cases。")
    parser.add_argument("--no-screenshot", action="store_true")
    args = parser.parse_args()
    if args.only_real and args.only_synthetic:
        raise SystemExit("--only-real and --only-synthetic cannot be used together")

    out_dir = args.out_dir.expanduser().resolve()
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    real_inputs = _parse_candidates(args.candidate)
    if args.include_real_posters and not args.only_synthetic:
        real_inputs.extend(_discover_real_poster_inputs())
    if real_inputs and not args.only_synthetic:
        rows.extend(_real_rows(assets_dir, _dedupe_inputs(real_inputs)))
    if args.only_synthetic:
        rows.extend(_synthetic_rows(assets_dir))
    elif not args.only_real:
        rows.extend(_synthetic_rows(assets_dir))

    html_path = out_dir / "basic_layout_integrity_visual_report.html"
    html_path.write_text(_build_html(rows), encoding="utf-8")
    json_path = out_dir / "basic_layout_integrity_visual_report.json"
    json_path.write_text(
        json.dumps(_sidecar_payload(rows, out_dir=out_dir), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"wrote {html_path}")
    print(f"wrote {json_path}")
    if not args.no_screenshot:
        png_path = out_dir / "basic_layout_integrity_visual_report.png"
        result = screenshot_html(
            html_path,
            png_path,
            viewport_width=1400,
            viewport_height=2200,
            full_page=True,
            max_edge=5000,
            timeout_ms=30_000,
        )
        if png_path.exists():
            print(f"rendered {png_path}")
        elif result.warnings:
            print(f"screenshot warnings: {result.warnings}")
    return 0


def _parse_candidates(values: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for value in values:
        if "=" not in value:
            raise SystemExit(f"--candidate must be name=/path, got: {value}")
        name, raw = value.split("=", 1)
        name = name.strip()
        path = Path(raw).expanduser()
        if not name:
            raise SystemExit("--candidate name cannot be empty")
        out.append({
            "name": name,
            "path": path,
            "source": "manual candidate",
            "expectation": "真实海报手动输入：这里主要看 image-native basic_layout_integrity 是否误报、漏报或阈值不合理。",
        })
    return out


def _discover_real_poster_inputs() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for report_path in sorted((_REPO_ROOT / "out" / "eval").glob("real_poster_*/candidates/*/poster_quality_report.json")):
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        artifact = payload.get("artifact")
        candidate = str(payload.get("candidate_name") or report_path.parent.name)
        if artifact:
            path = Path(str(artifact)).expanduser()
            if not path.is_absolute():
                path = _REPO_ROOT / path
        else:
            det = payload.get("deterministic_report_path")
            path = Path(str(det)).parent / "snapshot" / "artifact_preview.png" if det else report_path.parent / "deterministic" / "snapshot" / "artifact_preview.png"
        if not path.exists():
            fallback = report_path.parent / "deterministic" / "snapshot" / "artifact_preview.png"
            path = fallback if fallback.exists() else path
        if not path.exists():
            continue
        prior_dims = {
            str(item.get("id")): item.get("score_0_10")
            for item in payload.get("dimensions", [])
            if isinstance(item, dict)
        }
        out.append({
            "name": f"{report_path.parents[2].name}/{candidate}",
            "path": path,
            "source": str(report_path.relative_to(_REPO_ROOT)),
            "expectation": (
                f"真实 eval 海报；历史总分 {payload.get('overall_score_0_100')} / verdict {payload.get('verdict')}。"
                "这里重点检查 basic_layout_integrity 是否只抓机械损坏，而不是把正常设计差异误判成坏。"
            ),
            "prior": {
                "overall": payload.get("overall_score_0_100"),
                "verdict": payload.get("verdict"),
                "density": prior_dims.get("information_density_and_synthesis"),
                "layout": prior_dims.get("layout_readability"),
            },
        })
    return out


def _dedupe_inputs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = (str(item.get("name") or ""), str(Path(item["path"]).resolve()))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _real_rows(assets_dir: Path, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        path = Path(item["path"]).expanduser()
        if not path.exists():
            continue
        image = Image.open(path).convert("RGB")
        ocr = run_ocr(path, include_segments=True)
        segments = ocr.get("segments") or []
        occupancy = content_occupancy(image, segments=segments)
        report = basic_layout_integrity(
            image,
            segments=segments,
            occupancy=occupancy,
            ocr_status="ok" if ocr.get("available") else str(ocr.get("reason") or ocr.get("error") or "unavailable"),
            include_debug_regions=True,
        )
        overlay = _overlay(image, segments=segments, occupancy=occupancy, report=report)
        safe = _safe_name(str(item["name"]))
        overlay_path = assets_dir / f"real_{safe}.png"
        overlay.save(overlay_path)
        rows.append({
            "group": "真实海报",
            "name": item["name"],
            "expectation": item.get("expectation") or "真实海报样本。",
            "image": overlay_path,
            "report": report,
            "source": item.get("source"),
            "artifact_path": str(path),
            "prior": item.get("prior") or {},
        })
    rows.sort(key=lambda row: (
        0 if (row["report"].get("findings") or []) else 1,
        float(row["report"].get("score_0_10") or 0.0),
        str(row["name"]),
    ))
    return rows


def _synthetic_rows(assets_dir: Path) -> list[dict[str, Any]]:
    cases = [
        _synthetic_clean(),
        _synthetic_low_resolution(),
        _synthetic_tiny_text(),
        _synthetic_text_on_edge(),
        _synthetic_aspect_outlier(),
        _synthetic_content_on_edge(),
        _synthetic_panel_text_overflow(),
        _synthetic_panel_visual_overflow(),
        _synthetic_canvas_overflow_multi_edge(),
        _synthetic_panel_text_overflow_many(),
        _synthetic_panel_visual_overflow_many(),
        _synthetic_cross_panel_gutter_overflow(),
        _synthetic_heading_clipped(),
        _synthetic_overflow_stress_combo(),
        _synthetic_text_overlap(),
        _synthetic_text_visual_overlap(),
    ]
    rows: list[dict[str, Any]] = []
    for case in cases:
        name = case["name"]
        image = case["image"]
        segments = case.get("segments") or []
        occupancy = case.get("occupancy")
        if occupancy is None:
            occupancy = content_occupancy(image, segments=segments)
        report = basic_layout_integrity(
            image,
            segments=segments,
            occupancy=occupancy,
            ocr_status="ok" if segments else "synthetic_no_ocr",
            include_debug_regions=True,
        )
        overlay = _overlay(image, segments=segments, occupancy=occupancy, report=report)
        overlay_path = assets_dir / f"synthetic_{name}.png"
        overlay.save(overlay_path)
        rows.append({
            "group": "合成 probe cases",
            "name": name,
            "expectation": case["expectation"],
            "image": overlay_path,
            "report": report,
        })
    return rows


def _synthetic_clean() -> dict[str, Any]:
    image = Image.new("RGB", (1200, 1700), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    draw.rectangle((56, 48, 1144, 210), fill="#eaf1f2", outline="#b6c2c4", width=2)
    segments: list[dict[str, Any]] = []
    y = 300
    for col_x in (86, 448, 810):
        draw.rectangle((col_x - 18, 260, col_x + 290, 1560), outline="#d2d7d9", width=2)
        for i in range(16):
            x0, y0 = col_x, y + i * 70
            x1, y1 = col_x + 230, y0 + 20
            draw.rounded_rectangle((x0, y0, x1, y1), radius=3, fill="#273033")
            segments.append(_seg(x0, y0, x1, y1, "body text"))
    return {
        "name": "clean_control",
        "expectation": "不应触发 finding：分辨率、比例、边距和正文可读性都正常。",
        "image": image,
        "segments": segments,
    }


def _synthetic_low_resolution() -> dict[str, Any]:
    image = Image.new("RGB", (500, 900), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    segments = []
    for i in range(14):
        x0, y0 = 64, 170 + i * 44
        x1, y1 = 430, y0 + 14
        draw.rectangle((x0, y0, x1, y1), fill="#252525")
        segments.append(_seg(x0, y0, x1, y1, "normal text"))
    return {
        "name": "low_resolution",
        "expectation": "应只触发低分辨率：这是最终导出图像本身的基础可用性问题。",
        "image": image,
        "segments": segments,
    }


def _synthetic_tiny_text() -> dict[str, Any]:
    image = Image.new("RGB", (1200, 1700), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    segments = []
    for i in range(28):
        x0, y0 = 88, 270 + i * 42
        x1, y1 = 1030, y0 + 4
        draw.rectangle((x0, y0, x1, y1), fill="#222")
        segments.append(_seg(x0, y0, x1, y1, "tiny text"))
    return {
        "name": "tiny_body_text",
        "expectation": "应触发正文过小：OCR box 高度低于归一化后的可读性下限。",
        "image": image,
        "segments": segments,
    }


def _synthetic_text_on_edge() -> dict[str, Any]:
    image = Image.new("RGB", (1200, 1700), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    segments = []
    for i in range(18):
        x0, y0 = 0, 280 + i * 64
        x1, y1 = 620, y0 + 18
        draw.rectangle((x0, y0, x1, y1), fill="#242424")
        segments.append(_seg(x0, y0, x1, y1, "edge text"))
    return {
        "name": "body_text_on_export_edge",
        "expectation": "应触发正文贴导出边：这是 image-only 下能稳定检测的明显裁切/溢出风险。",
        "image": image,
        "segments": segments,
    }


def _synthetic_aspect_outlier() -> dict[str, Any]:
    image = Image.new("RGB", (2600, 700), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    segments = []
    for i in range(10):
        x0, y0 = 180, 180 + i * 44
        x1, y1 = 2200, y0 + 16
        draw.rectangle((x0, y0, x1, y1), fill="#222")
        segments.append(_seg(x0, y0, x1, y1, "wide text"))
    return {
        "name": "aspect_outlier",
        "expectation": "应触发最终 poster 比例异常。",
        "image": image,
        "segments": segments,
    }


def _synthetic_content_on_edge() -> dict[str, Any]:
    image = Image.new("RGB", (1200, 1700), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    # Content blocks deliberately touch left/right/bottom safety bands.
    for x0, x1 in ((0, 170), (1030, 1199)):
        for y in range(260, 1580, 54):
            draw.rectangle((x0, y, x1, y + 28), fill="#263137")
    for x in range(0, 1200, 82):
        draw.rectangle((x, 1668, x + 52, 1699), fill="#263137")
    return {
        "name": "content_on_export_edge",
        "expectation": "即使没有 OCR 文本框，也应通过 edge occupancy 触发内容压边。",
        "image": image,
        "segments": [],
    }


def _synthetic_panel_text_overflow() -> dict[str, Any]:
    image = Image.new("RGB", (1200, 1700), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    draw.rectangle((110, 300, 1090, 900), fill="#f4f8fb", outline="#8aa5b0", width=4)
    segments = []
    for i in range(7):
        x0, y0 = 150, 370 + i * 62
        x1, y1 = 1040, y0 + 22
        if i == 3:
            x1 = 1142
        draw.rectangle((x0, y0, x1, y1), fill="#20282c")
        segments.append(_seg(x0, y0, x1, y1, "panel text"))
    return {
        "name": "panel_text_overflow",
        "expectation": "应触发 panel text overflow：正文框右侧穿出 panel/frame 边界。",
        "image": image,
        "segments": segments,
    }


def _synthetic_panel_visual_overflow() -> dict[str, Any]:
    image = Image.new("RGB", (1200, 1700), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    draw.rectangle((110, 300, 1090, 940), fill="#f5f7f6", outline="#8aa5b0", width=4)
    # A chart-like connected visual component whose bounding box extends past the
    # panel. It is intentionally non-rectangular so it does not become a panel.
    draw.ellipse((800, 475, 1152, 830), fill="#d58b36", outline="#794b18", width=4)
    for i in range(9):
        draw.line((830 + i * 30, 780, 900 + i * 28, 520), fill="#fff3d7", width=4)
    return {
        "name": "panel_visual_overflow",
        "expectation": "应触发 panel visual overflow：独立 visual component 穿出 panel/frame 右侧。",
        "image": image,
        "segments": [],
    }


def _synthetic_canvas_overflow_multi_edge() -> dict[str, Any]:
    image = Image.new("RGB", (1200, 1700), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    segments = []
    for i in range(9):
        y0 = 300 + i * 86
        box = (0, y0, 760, y0 + 22)
        draw.rectangle(box, fill="#24292c")
        segments.append(_seg(*box, "left clipped text"))
    for i in range(7):
        y0 = 340 + i * 92
        box = (530, y0, 1199, y0 + 22)
        draw.rectangle(box, fill="#24292c")
        segments.append(_seg(*box, "right clipped text"))
    for i in range(6):
        x0 = 84 + i * 170
        box = (x0, 1678, x0 + 128, 1699)
        draw.rectangle(box, fill="#24292c")
        segments.append(_seg(*box, "bottom clipped text"))
    return {
        "name": "canvas_overflow_multi_edge",
        "expectation": "应明显降分：大量正文同时贴/触左、右、底部真实画布边，属于 canvas overflow 和 export-edge 风险。",
        "image": image,
        "segments": segments,
    }


def _synthetic_panel_text_overflow_many() -> dict[str, Any]:
    image = Image.new("RGB", (1200, 1700), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    segments = []
    panels = [(80, 300, 520, 760), (680, 300, 1120, 760), (80, 900, 520, 1360), (680, 900, 1120, 1360)]
    for panel in panels:
        draw.rectangle(panel, fill="#f3f7f9", outline="#8aa5b0", width=4)
        px0, py0, px1, _py1 = panel
        for i in range(4):
            y0 = py0 + 70 + i * 74
            x0 = px0 + 40
            x1 = px1 + 52
            draw.rectangle((x0, y0, x1, y0 + 24), fill="#20282c")
            segments.append(_seg(x0, y0, x1, y0 + 24, "overflowing panel text"))
    return {
        "name": "panel_text_overflow_many",
        "expectation": "应触发强 panel text overflow：多个 panel 内正文都越过右边界，检查同类大量 overflow 是否仍偏高。",
        "image": image,
        "segments": segments,
    }


def _synthetic_panel_visual_overflow_many() -> dict[str, Any]:
    image = Image.new("RGB", (1200, 1700), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    panels = [(80, 300, 520, 760), (680, 300, 1120, 760), (80, 900, 520, 1360), (680, 900, 1120, 1360)]
    colors = ["#d58b36", "#6aa4c8", "#8fae54", "#b5709b"]
    for idx, panel in enumerate(panels):
        draw.rectangle(panel, fill="#f5f7f6", outline="#8aa5b0", width=4)
        px0, py0, px1, _py1 = panel
        draw.ellipse((px1 - 140, py0 + 120, px1 + 70, py0 + 330), fill=colors[idx], outline="#59452c", width=4)
        for i in range(5):
            draw.line((px1 - 112 + i * 20, py0 + 318, px1 - 66 + i * 18, py0 + 144), fill="#fff4d6", width=4)
    return {
        "name": "panel_visual_overflow_many",
        "expectation": "应触发强 panel visual overflow：多个独立 visual component 穿出 panel/frame 边界。",
        "image": image,
        "segments": [],
    }


def _synthetic_cross_panel_gutter_overflow() -> dict[str, Any]:
    image = Image.new("RGB", (1200, 1700), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    segments = []
    left = (80, 300, 540, 1220)
    right = (660, 300, 1120, 1220)
    draw.rectangle(left, fill="#f3f7f9", outline="#8aa5b0", width=4)
    draw.rectangle(right, fill="#f3f7f9", outline="#8aa5b0", width=4)
    for i in range(8):
        y0 = 380 + i * 82
        box = (410, y0, 610, y0 + 26)
        draw.rectangle(box, fill="#20282c")
        segments.append(_seg(*box, "gutter overflow text"))
    draw.ellipse((430, 840, 720, 1120), fill="#d58b36", outline="#794b18", width=4)
    return {
        "name": "cross_panel_gutter_overflow",
        "expectation": "应触发 panel overflow：内容从左 panel 穿进 gutter/右侧区域，模拟跨 panel 机械越界。",
        "image": image,
        "segments": segments,
    }


def _synthetic_heading_clipped() -> dict[str, Any]:
    image = Image.new("RGB", (1200, 1700), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    segments = []
    box = (180, 0, 1020, 48)
    draw.rectangle(box, fill="#24292c")
    segments.append(_seg(*box, "clipped heading"))
    for i in range(10):
        body = (120, 330 + i * 70, 980, 352 + i * 70)
        draw.rectangle(body, fill="#24292c")
        segments.append(_seg(*body, "normal body text"))
    return {
        "name": "heading_clipped",
        "expectation": "应触发 heading canvas overflow：标题直接贴到真实顶部边界，但只应是轻/中度机械风险。",
        "image": image,
        "segments": segments,
    }


def _synthetic_overflow_stress_combo() -> dict[str, Any]:
    image = Image.new("RGB", (1200, 1700), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    segments = []
    heading = (150, 0, 1110, 54)
    draw.rectangle(heading, fill="#20282c")
    segments.append(_seg(*heading, "clipped heading"))
    panels = [(70, 260, 520, 780), (650, 260, 1100, 780), (70, 900, 520, 1420), (650, 900, 1100, 1420)]
    for panel in panels:
        draw.rectangle(panel, fill="#f3f7f9", outline="#8aa5b0", width=4)
    for i in range(8):
        y0 = 340 + i * 62
        box = (0, y0, 560, y0 + 24)
        draw.rectangle(box, fill="#24292c")
        segments.append(_seg(*box, "left canvas overflow"))
    for i in range(8):
        y0 = 950 + i * 54
        box = (700, y0, 1199, y0 + 24)
        draw.rectangle(box, fill="#24292c")
        segments.append(_seg(*box, "right canvas overflow"))
    for panel in panels[:3]:
        px0, py0, px1, _py1 = panel
        for i in range(3):
            y0 = py0 + 80 + i * 70
            box = (px0 + 42, y0, px1 + 56, y0 + 26)
            draw.rectangle(box, fill="#20282c")
            segments.append(_seg(*box, "panel text overflow"))
    draw.ellipse((940, 470, 1170, 700), fill="#d58b36", outline="#794b18", width=4)
    overlap_a = (220, 1510, 780, 1550)
    overlap_b = (310, 1520, 890, 1560)
    for box in (overlap_a, overlap_b):
        draw.rectangle(box, fill="#502f39")
        segments.append(_seg(*box, "overlapping text"))
    return {
        "name": "overflow_stress_combo",
        "expectation": "应降到低分：heading 裁切、canvas 多边溢出、多个 panel text/visual overflow 和文本重叠同时存在。",
        "image": image,
        "segments": segments,
    }


def _synthetic_text_overlap() -> dict[str, Any]:
    image = Image.new("RGB", (1200, 1700), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    segments = []
    boxes = [
        (200, 500, 800, 540),
        (300, 510, 900, 550),
        (200, 620, 780, 660),
        (280, 630, 860, 670),
    ]
    for i, box in enumerate(boxes):
        draw.rectangle(box, fill="#24292c" if i != 1 else "#60353a")
        segments.append(_seg(*box, "overlap text"))
    return {
        "name": "text_overlap",
        "expectation": "应触发 text overlap：两个 OCR 文本框明显互相覆盖。",
        "image": image,
        "segments": segments,
    }


def _synthetic_text_visual_overlap() -> dict[str, Any]:
    image = Image.new("RGB", (1200, 1700), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    draw.ellipse((330, 430, 890, 900), fill="#8ab7cf", outline="#326173", width=5)
    for i in range(11):
        draw.line((360 + i * 42, 850, 430 + i * 36, 480), fill="#f7fbff", width=5)
    segment = _seg(460, 630, 980, 674, "large overlay text")
    draw.rectangle((460, 630, 980, 674), fill="#1f2428")
    return {
        "name": "text_visual_overlap",
        "expectation": "应触发 text-visual overlap：大号正文直接覆盖独立 visual block。",
        "image": image,
        "segments": [segment],
    }


def _overlay(
    image: Image.Image,
    *,
    segments: list[dict[str, Any]],
    occupancy: dict[str, Any],
    report: dict[str, Any],
) -> Image.Image:
    canvas = image.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas, "RGBA")
    w, h = canvas.size
    scale = max(w, h) / float(REFERENCE_LONG_EDGE)
    heading_y = int(h * 0.14)
    edge_margin = max(2, int(round(10 * scale)))

    # Heading band is ignored for body-text integrity checks.
    draw.rectangle((0, 0, w, heading_y), fill=(120, 120, 120, 38))
    # Export safety band.
    draw.rectangle((0, 0, edge_margin, h), fill=(255, 180, 0, 70))
    draw.rectangle((w - edge_margin, 0, w, h), fill=(255, 180, 0, 70))
    draw.rectangle((0, h - edge_margin, w, h), fill=(255, 180, 0, 70))

    _draw_edge_occupancy(draw, occupancy, w, h)
    _draw_canvas_overflow(draw, report, w, h, edge_margin)
    _draw_debug_regions(draw, report)

    tiny = {p["id"] for p in report.get("penalties", []) if p.get("id") == "basic-layout-text-too-small"}
    edge = {p["id"] for p in report.get("penalties", []) if p.get("id") == "basic-layout-text-on-export-edge"}
    for seg in segments:
        rect = _segment_rect(seg)
        if rect is None:
            continue
        body = (rect["y0"] + rect["y1"]) / 2.0 >= heading_y
        text_h_ref = rect["h"] / scale
        touches_edge = rect["x0"] <= edge_margin or rect["x1"] >= w - edge_margin or rect["y1"] >= h - edge_margin
        if body and touches_edge and edge:
            color = (230, 42, 42, 210)
            width = max(3, int(4 * scale))
        elif body and text_h_ref < 7.5 and tiny:
            color = (137, 57, 190, 210)
            width = max(2, int(3 * scale))
        else:
            color = (0, 130, 78, 170) if body else (70, 70, 70, 130)
            width = max(1, int(2 * scale))
        draw.rectangle((rect["x0"], rect["y0"], rect["x1"], rect["y1"]), outline=color, width=width)

    _draw_integrity_samples(draw, report)
    _draw_badges(draw, canvas.size, report)
    return canvas


def _draw_canvas_overflow(draw: ImageDraw.ImageDraw, report: dict[str, Any], w: int, h: int, edge_margin: int) -> None:
    canvas = report.get("canvas_overflow") or {}
    counts = canvas.get("true_edge_counts") or {}
    side_counts = canvas.get("side_region_counts") or {}
    if not any(counts.get(side) or side_counts.get(side) for side in ("left", "right", "top", "bottom")):
        return
    band = max(3, edge_margin * 2)
    if counts.get("left") or side_counts.get("left"):
        draw.rectangle((0, 0, band, h), fill=(228, 24, 24, 95))
    if counts.get("right") or side_counts.get("right"):
        draw.rectangle((w - band, 0, w, h), fill=(228, 24, 24, 95))
    if counts.get("top") or side_counts.get("top"):
        draw.rectangle((0, 0, w, band), fill=(228, 24, 24, 95))
    if counts.get("bottom") or side_counts.get("bottom"):
        draw.rectangle((0, h - band, w, h), fill=(228, 24, 24, 95))


def _draw_debug_regions(draw: ImageDraw.ImageDraw, report: dict[str, Any]) -> None:
    debug = report.get("debug_regions") or {}
    for section in debug.get("inferred_sections") or []:
        rect = section.get("rect") or {}
        _draw_rect(draw, rect, outline=(0, 164, 194, 210), fill=(0, 164, 194, 18), width=2)
    for section in debug.get("effective_sections") or []:
        rect = section.get("rect") or {}
        _draw_rect(draw, rect, outline=(0, 120, 190, 210), fill=None, width=2)
    for panel in debug.get("panels") or []:
        rect = panel.get("rect") or {}
        _draw_rect(draw, rect, outline=(19, 98, 191, 225), fill=(19, 98, 191, 22), width=3)
    for text in debug.get("fallback_text") or []:
        rect = text.get("rect") or {}
        _draw_rect(draw, rect, outline=(198, 130, 20, 210), fill=(198, 130, 20, 18), width=1)
    for visual in debug.get("visuals") or []:
        rect = visual.get("rect") or {}
        _draw_rect(draw, rect, outline=(224, 125, 34, 210), fill=(224, 125, 34, 32), width=2)
    for visual in debug.get("heading_visuals") or []:
        rect = visual.get("rect") or {}
        _draw_rect(draw, rect, outline=(30, 142, 155, 190), fill=(30, 142, 155, 24), width=2)


def _draw_integrity_samples(draw: ImageDraw.ImageDraw, report: dict[str, Any]) -> None:
    debug = report.get("debug_regions") or {}
    finding_ids = {str(f.get("id")) for f in report.get("findings") or []}
    if any(fid in finding_ids for fid in ("basic-layout-panel-text-overflow", "basic-layout-panel-visual-overflow", "basic-layout-panel-content-tight")):
        for sample in debug.get("panel_overflow_samples") or []:
            rect = sample.get("rect") or {}
            panel_rect = sample.get("panel_rect") or {}
            _draw_rect(draw, panel_rect, outline=(215, 38, 34, 180), width=3)
            _draw_rect(draw, rect, outline=(215, 38, 34, 245), fill=(215, 38, 34, 42), width=5)
            _draw_overflow_line(draw, rect, panel_rect)
    if any(fid in finding_ids for fid in ("basic-layout-text-overlap", "basic-layout-text-visual-overlap", "basic-layout-visual-overlap")):
        for sample in debug.get("overlap_samples") or []:
            rect = sample.get("rect") or {}
            _draw_rect(draw, rect, outline=(127, 47, 190, 245), fill=(127, 47, 190, 58), width=5)
    if "basic-layout-canvas-overflow" in finding_ids:
        for sample in debug.get("canvas_overflow_samples") or []:
            rect = sample.get("rect") or {}
            _draw_rect(draw, rect, outline=(228, 24, 24, 240), fill=(228, 24, 24, 34), width=4)
    if any(fid in finding_ids for fid in ("basic-layout-bottom-truncation", "basic-layout-section-content-overflow", "basic-layout-section-edge-tight", "basic-layout-section-bottom-truncated", "basic-layout-inter-section-collision", "basic-layout-panel-underfilled", "basic-layout-visual-crop-damage")):
        for sample in (debug.get("bottom_truncation_samples") or []) + (debug.get("section_samples") or []) + (debug.get("visual_crop_samples") or []):
            rect = sample.get("rect") or {}
            section_rect = sample.get("section_rect") or {}
            _draw_rect(draw, section_rect, outline=(194, 24, 120, 190), width=3)
            _draw_rect(draw, rect, outline=(194, 24, 120, 245), fill=(194, 24, 120, 46), width=4)
    if any(fid in finding_ids for fid in ("basic-layout-heading-canvas-overflow", "basic-layout-heading-text-overlap", "basic-layout-heading-panel-overflow")):
        for sample in debug.get("heading_samples") or []:
            rect = sample.get("rect") or {}
            if "a" in sample and "b" in sample:
                _draw_rect(draw, rect, outline=(127, 47, 190, 245), fill=(127, 47, 190, 58), width=5)
            elif "divider_y" in sample:
                _draw_rect(draw, rect, outline=(215, 38, 34, 245), fill=(215, 38, 34, 42), width=5)
                y = float(sample.get("divider_y") or 0.0)
                draw.line((float(rect.get("x0", 0.0)), y, float(rect.get("x1", 0.0)), y), fill=(215, 38, 34, 245), width=4)
            else:
                _draw_rect(draw, rect, outline=(228, 24, 24, 240), fill=(228, 24, 24, 34), width=4)


def _draw_overflow_line(draw: ImageDraw.ImageDraw, rect: dict[str, Any], panel_rect: dict[str, Any]) -> None:
    if not rect or not panel_rect:
        return
    cx = (float(rect.get("x0", 0.0)) + float(rect.get("x1", 0.0))) / 2.0
    cy = (float(rect.get("y0", 0.0)) + float(rect.get("y1", 0.0))) / 2.0
    targets = [
        (float(rect.get("x0", 0.0)) - float(panel_rect.get("x0", 0.0)), (float(panel_rect.get("x0", 0.0)), cy)),
        (float(panel_rect.get("x1", 0.0)) - float(rect.get("x1", 0.0)), (float(panel_rect.get("x1", 0.0)), cy)),
        (float(rect.get("y0", 0.0)) - float(panel_rect.get("y0", 0.0)), (cx, float(panel_rect.get("y0", 0.0)))),
        (float(panel_rect.get("y1", 0.0)) - float(rect.get("y1", 0.0)), (cx, float(panel_rect.get("y1", 0.0)))),
    ]
    negative = [(gap, target) for gap, target in targets if gap < 0]
    if not negative:
        return
    _gap, target = min(negative, key=lambda item: item[0])
    draw.line((cx, cy, target[0], target[1]), fill=(215, 38, 34, 245), width=4)
    draw.ellipse((target[0] - 6, target[1] - 6, target[0] + 6, target[1] + 6), fill=(215, 38, 34, 245))


def _draw_rect(
    draw: ImageDraw.ImageDraw,
    rect: dict[str, Any],
    *,
    outline: tuple[int, int, int, int],
    fill: tuple[int, int, int, int] | None = None,
    width: int = 2,
) -> None:
    if not rect:
        return
    box = (
        float(rect.get("x0", 0.0)),
        float(rect.get("y0", 0.0)),
        float(rect.get("x1", 0.0)),
        float(rect.get("y1", 0.0)),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return
    if fill is not None:
        draw.rectangle(box, fill=fill)
    draw.rectangle(box, outline=outline, width=width)


def _draw_edge_occupancy(draw: ImageDraw.ImageDraw, occupancy: dict[str, Any], w: int, h: int) -> None:
    occ = occupancy.get("occ")
    if not isinstance(occ, list) or not occ or not isinstance(occ[0], list):
        return
    rows = len(occ)
    cols = len(occ[0])
    if rows <= 2 or cols <= 2:
        return
    heading_rows = int(occupancy.get("heading_rows") or 0)
    band = max(1, round(min(rows, cols) * 0.025))
    cw, ch = w / cols, h / rows
    for r in range(max(0, heading_rows), rows):
        for c in range(cols):
            edge_cell = c < band or c >= cols - band or r >= rows - band
            if edge_cell and bool(occ[r][c]):
                box = (c * cw, r * ch, (c + 1) * cw, (r + 1) * ch)
                draw.rectangle(box, fill=(255, 66, 0, 75), outline=(255, 66, 0, 160), width=1)


def _draw_badges(draw: ImageDraw.ImageDraw, size: tuple[int, int], report: dict[str, Any]) -> None:
    w, _h = size
    score = report.get("score_0_10")
    findings = report.get("findings") or []
    status = report.get("status") or "ok"
    title = f"基础布局完整性 = {score}/10 ({status})"
    lines = [title]
    coverage = report.get("detector_coverage") or {}
    if coverage:
        lines.append(
            f"coverage: text {coverage.get('effective_text_region_count')} / "
            f"sections {coverage.get('effective_section_count')} / cv {coverage.get('cv_status')}"
        )
    if findings:
        lines.extend(f"{f.get('severity')} {f.get('id')}" for f in findings[:5])
    else:
        lines.append("无 findings")
    font = ImageFont.load_default()
    line_h = 17
    box_w = min(w - 24, max(330, max(len(line) for line in lines) * 7 + 22))
    box_h = line_h * len(lines) + 16
    x0, y0 = 12, 12
    draw.rounded_rectangle((x0, y0, x0 + box_w, y0 + box_h), radius=8, fill=(255, 255, 255, 230), outline=(30, 30, 30, 90), width=1)
    for i, line in enumerate(lines):
        fill = (20, 20, 20, 255) if i == 0 else ((90, 90, 90, 255) if i == 1 and coverage else (170, 38, 38, 255))
        draw.text((x0 + 10, y0 + 9 + i * line_h), line, fill=fill, font=font)


def _segment_rect(seg: dict[str, Any]) -> dict[str, float] | None:
    box = seg.get("box") if isinstance(seg, dict) else None
    if not isinstance(box, list) or len(box) < 3:
        return None
    try:
        xs = [float(pt[0]) for pt in box if isinstance(pt, (list, tuple)) and len(pt) >= 2]
        ys = [float(pt[1]) for pt in box if isinstance(pt, (list, tuple)) and len(pt) >= 2]
    except (TypeError, ValueError):
        return None
    if not xs or not ys:
        return None
    return {
        "x0": min(xs),
        "y0": min(ys),
        "x1": max(xs),
        "y1": max(ys),
        "w": max(0.0, max(xs) - min(xs)),
        "h": max(0.0, max(ys) - min(ys)),
    }


def _seg(x0: float, y0: float, x1: float, y1: float, text: str) -> dict[str, Any]:
    return {
        "box": [[float(x0), float(y0)], [float(x1), float(y0)], [float(x1), float(y1)], [float(x0), float(y1)]],
        "text": text,
        "score": 0.99,
    }


def _sidecar_payload(rows: list[dict[str, Any]], *, out_dir: Path) -> dict[str, Any]:
    out_rows: list[dict[str, Any]] = []
    for row in rows:
        image_path = Path(row["image"])
        try:
            overlay = str(image_path.relative_to(out_dir))
        except ValueError:
            overlay = str(image_path)
        report = _json_report(row.get("report") or {})
        out_rows.append({
            "group": row.get("group"),
            "name": row.get("name"),
            "expectation": row.get("expectation"),
            "source": row.get("source"),
            "artifact_path": row.get("artifact_path"),
            "overlay_path": overlay,
            "score_0_10": report.get("score_0_10"),
            "status": report.get("status"),
            "findings": report.get("findings") or [],
            "detector_coverage": report.get("detector_coverage") or {},
            "debug_counts": {
                "ocr_body_segment_count": report.get("ocr_body_segment_count"),
                "fallback_text_region_count": report.get("fallback_text_region_count"),
                "effective_text_region_count": report.get("effective_text_region_count"),
                "panel_count": report.get("panel_count"),
                "inferred_section_count": report.get("inferred_section_count"),
                "effective_section_count": report.get("effective_section_count"),
                "content_region_count": report.get("content_region_count"),
            },
            "report": report,
        })
    return {"version": 1, "row_count": len(out_rows), "rows": out_rows}


def _json_report(report: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in report.items() if k != "_visual_mask"}


def _build_html(rows: list[dict[str, Any]]) -> str:
    by_group: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_group.setdefault(str(row["group"]), []).append(row)
    sections = "\n".join(
        f"<h2>{_esc(group)}</h2>\n<div class='grid'>{''.join(_card(row) for row in group_rows)}</div>"
        for group, group_rows in by_group.items()
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>基础布局完整性检测可视化报告</title>
  <style>
    body {{ margin: 24px; background: #f7f7f4; color: #161616; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    h1 {{ margin: 0 0 8px; font-size: 26px; }}
    h2 {{ margin: 28px 0 12px; font-size: 18px; }}
    .lead {{ max-width: 980px; color: #4b4b4b; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 14px 0 20px; }}
    .legend span {{ display: inline-flex; align-items: center; gap: 6px; background: #fff; border: 1px solid #ddd; padding: 5px 8px; border-radius: 6px; }}
    .sw {{ width: 16px; height: 10px; border: 2px solid; display: inline-block; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 16px; }}
    .card {{ background: #fff; border: 1px solid #deded8; border-radius: 8px; padding: 12px; }}
    .card h3 {{ margin: 0 0 8px; font-size: 15px; }}
    .expect {{ min-height: 40px; color: #505050; }}
    .imgwrap {{ background: #eee; border: 1px solid #d7d7d0; border-radius: 6px; overflow: hidden; }}
    img {{ display: block; width: 100%; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 10px; font-size: 12px; }}
    td, th {{ border: 1px solid #e3e3dd; padding: 5px 6px; text-align: left; vertical-align: top; }}
    th {{ background: #f3f3ee; }}
    code {{ background: #f0f0eb; padding: 1px 4px; border-radius: 4px; }}
    .ok {{ color: #167346; font-weight: 600; }}
    .warn {{ color: #ad372f; font-weight: 600; }}
  </style>
</head>
<body>
  <h1>Image-native 基础布局完整性检测</h1>
  <p class="lead">
    这个报告可视化 layout 大类下面的独立 deterministic 维度：
    <code>basic_layout_integrity</code>。它只检测最终 rendered image 本身能稳定支持的机械布局损坏：
    导出 canvas 边界 clipping risk、标题区机械损坏、图文超出 panel/frame、图文重叠、panel 内局部压边，以及保留的尺寸/比例/小字基础可用性信号。
    空白、稀疏、内部大 void 继续由 density/layout_readability 负责；源图是否被真实裁掉、图表语义是否可用仍交给 VLM/agent。
  </p>
  <div class="legend">
    <span><i class="sw" style="background:rgba(120,120,120,.25);border-color:#777"></i>标题区 band</span>
    <span><i class="sw" style="background:rgba(30,142,155,.14);border-color:#1e8e9b"></i>标题区 visual/header region</span>
    <span><i class="sw" style="background:rgba(255,180,0,.35);border-color:#d68a00"></i>导出安全边界/老 occupancy 辅助信号</span>
    <span><i class="sw" style="background:rgba(228,24,24,.35);border-color:#e41818"></i>canvas overflow 边带/样本</span>
    <span><i class="sw" style="background:rgba(19,98,191,.12);border-color:#1362bf"></i>检测到的 panel/frame</span>
    <span><i class="sw" style="background:rgba(0,164,194,.10);border-color:#00a4c2"></i>推断 section/effective section</span>
    <span><i class="sw" style="background:rgba(198,130,20,.12);border-color:#c68214"></i>OCR-free fallback text line</span>
    <span><i class="sw" style="background:rgba(224,125,34,.18);border-color:#e07d22"></i>检测到的 visual/content region</span>
    <span><i class="sw" style="background:rgba(194,24,120,.20);border-color:#c21878"></i>section/bottom/crop violation samples</span>
    <span><i class="sw" style="background:rgba(215,38,34,.18);border-color:#d72622"></i>panel overflow / panel 压边</span>
    <span><i class="sw" style="background:rgba(127,47,190,.22);border-color:#7f2fbe"></i>overlap 区域</span>
    <span><i class="sw" style="background:transparent;border-color:#00824e"></i>正常 OCR 正文</span>
    <span><i class="sw" style="background:transparent;border-color:#8939be"></i>过小文本框</span>
    <span><i class="sw" style="background:transparent;border-color:#e62a2a"></i>贴边文本</span>
  </div>
  {sections}
</body>
</html>"""


def _card(row: dict[str, Any]) -> str:
    report = row["report"]
    findings = report.get("findings") or []
    score_class = "ok" if not findings else "warn"
    heading = report.get("heading_integrity") or {}
    canvas = report.get("canvas_overflow") or {}
    bottom = report.get("bottom_truncation") or {}
    panel = report.get("panel_overflow") or {}
    section = report.get("section_bounds") or {}
    crop = report.get("visual_crop_damage") or {}
    overlap = report.get("overlap") or {}
    detector = report.get("detector_coverage") or {}
    findings_html = (
        "<br>".join(_esc(f"{f.get('severity')} {f.get('id')}") for f in findings)
        if findings else "<span class='ok'>无</span>"
    )
    prior = row.get("prior") or {}
    prior_html = ""
    if prior:
        prior_html = (
            f"<tr><td>历史 eval 参考</td><td>overall={_esc(prior.get('overall'))}, verdict={_esc(prior.get('verdict'))}, "
            f"density={_esc(prior.get('density'))}, layout={_esc(prior.get('layout'))}</td></tr>"
        )
    source_html = ""
    if row.get("source") or row.get("artifact_path"):
        source_html = (
            f"<tr><td>来源</td><td>{_esc(row.get('source') or '')}<br><code>{_esc(row.get('artifact_path') or '')}</code></td></tr>"
        )
    return f"""<section class="card">
  <h3>{_esc(row["name"])} <span class="{score_class}">{_esc(report.get("score_0_10"))}/10</span></h3>
  <p class="expect">{_esc(row["expectation"])}</p>
  <div class="imgwrap"><img src="data:image/png;base64,{_b64(row["image"])}"></div>
  <table>
    <tr><th>指标</th><th>值</th></tr>
    {source_html}
    {prior_html}
    <tr><td>status / detector coverage</td><td>{_esc(report.get("status"))}; ocr={_esc(detector.get("ocr_status"))}, cv={_esc(detector.get("cv_status"))}, fallback={_esc(detector.get("fallback_text_region_count"))}, effective_text={_esc(detector.get("effective_text_region_count"))}, sections={_esc(detector.get("effective_section_count"))}, blind={_esc(detector.get("blind"))}</td></tr>
    <tr><td>图像尺寸 / 比例</td><td>{_esc(report.get("width"))}x{_esc(report.get("height"))} / {_esc(report.get("aspect_ratio"))}</td></tr>
    <tr><td>正文 OCR 中位高度</td><td>{_esc(report.get("median_body_text_height_ref_px"))} px @ long-edge {REFERENCE_LONG_EDGE}</td></tr>
    <tr><td>小字占比</td><td>{_esc(report.get("small_text_fraction"))}</td></tr>
    <tr><td>贴边文本面积 / 段落占比</td><td>{_esc(report.get("edge_text_area_ratio"))} / {_esc(report.get("edge_text_segment_ratio"))}</td></tr>
    <tr><td>压边 occupancy</td><td>global={_esc((report.get("edge_occupancy") or {}).get("edge_occupied_ratio"))}; side={_esc((report.get("edge_occupancy_by_side") or {}).get("sides"))}</td></tr>
    <tr><td>panel / section / content regions</td><td>panel={_esc(report.get("panel_count"))}, inferred_section={_esc(report.get("inferred_section_count"))}, effective_section={_esc(report.get("effective_section_count"))}, visual={_esc(report.get("content_region_count"))}</td></tr>
    <tr><td>heading integrity</td><td>text={_esc(heading.get("text_region_count"))}, visual={_esc(heading.get("visual_region_count"))}, true_edge={_esc(heading.get("true_edge_counts"))}, text_overlap={_esc(heading.get("text_overlap_count"))}, divider_overlap={_esc(heading.get("divider_overlap_count"))}</td></tr>
    <tr><td>canvas overflow</td><td>finding={_esc(canvas.get("finding"))}, true_edge={_esc(canvas.get("true_edge_counts"))}, side={_esc(canvas.get("side_region_counts"))}</td></tr>
    <tr><td>bottom truncation</td><td>finding={_esc(bottom.get("finding"))}, severity={_esc(bottom.get("severity"))}, bottom_occ={_esc(bottom.get("bottom_occupancy_ratio"))}, true_touch={_esc(bottom.get("true_bottom_touch_count"))}</td></tr>
    <tr><td>panel overflow</td><td>text={_esc(panel.get("text_overflow_count"))}, visual={_esc(panel.get("visual_overflow_count"))}, tight={_esc(panel.get("tight_count"))}, cross={_esc(panel.get("cross_panel_count"))}</td></tr>
    <tr><td>section bounds</td><td>source={_esc(section.get("source"))}, overflow={_esc(section.get("content_overflow_count"))}, tight={_esc(section.get("edge_tight_count"))}, cross={_esc(section.get("inter_section_collision_count"))}, bottom={_esc(section.get("bottom_truncated_section_count"))}, underfilled={_esc(section.get("underfilled_section_count"))}</td></tr>
    <tr><td>visual crop</td><td>count={_esc(crop.get("crop_damage_count"))}</td></tr>
    <tr><td>overlap</td><td>text={_esc(overlap.get("text_overlap_count"))}, text+visual={_esc(overlap.get("text_visual_overlap_count"))}, visual={_esc(overlap.get("visual_overlap_count"))}, total={_esc(overlap.get("overlap_count"))}</td></tr>
    <tr><td>findings</td><td>{findings_html}</td></tr>
  </table>
</section>"""


def _b64(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def _esc(value: Any) -> str:
    return html.escape(str(value))


def _safe_name(value: str) -> str:
    out = []
    for ch in value.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in {"-", "_", "."}:
            out.append(ch)
        else:
            out.append("_")
    safe = "".join(out).strip("._-")
    return safe or "item"


if __name__ == "__main__":
    raise SystemExit(main())
