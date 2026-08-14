"""Authored HTML renderer for academic paper posters.

This path is intentionally separate from the legacy poster layer renderer:
the model authors final-canvas HTML/CSS, while this module enforces the
paper-poster shell, local asset constraints, physical page size, and DOM QA.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
from typing import Any

from bs4 import BeautifulSoup, Tag
from PIL import Image, ImageDraw, ImageFilter

from ..config import effective_poster_harness_mode
from ..util.io import sha256_file
from ..util.browser_render import (
    BrowserRenderResult,
    downsample_image_to_max_edge,
    export_html_pdf,
    screenshot_html,
)
from ..util.math_typesetting import (
    collect_autodesign_math_status,
    has_tex_math,
    inline_katex_bundle,
    lint_tex_math_source,
    wait_for_autodesign_math,
)
from ..util.poster_gate_audit import audit_authored_poster_gate


_POSTER_SIZE_PRESETS: dict[str, tuple[float, float, str]] = {
    "a0_portrait": (841.0, 1189.0, "A0 portrait"),
    "a0_landscape": (1189.0, 841.0, "A0 landscape"),
    "a1_portrait": (594.0, 841.0, "A1 portrait"),
    "a1_landscape": (841.0, 594.0, "A1 landscape"),
    "36x48_portrait": (914.4, 1219.2, "36x48 in portrait"),
    "36x48_landscape": (1219.2, 914.4, "36x48 in landscape"),
    "42x48_portrait": (1066.8, 1219.2, "42x48 in portrait"),
    "42x48_landscape": (1219.2, 1066.8, "42x48 in landscape"),
}
_DEFAULT_DPI = 150.0
_REMOTE_URL_RE = re.compile(r"""(?i)(https?:)?//|(?:url\s*\(\s*['"]?\s*(?:https?:|//|data:|javascript:))""")
_UNSAFE_CSS_RE = re.compile(r"(?i)@import|javascript:|expression\s*\(|</?\s*style")
_TEXT_TOKEN_TAG_RE = re.compile(r"^[a-z][a-z0-9]*[+\-*/=<>][a-z0-9+\-*/=<>.]*$")
_SOURCE_VISUAL_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
_DOC_TAGS = {"html", "head", "body", "script", "style", "link", "meta", "base", "title"}
_SKIP_CONTENT_TAGS = {"script", "style"}
_ALLOWED_TAGS = {
    "main", "section", "article", "aside", "header", "footer", "div", "span",
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "em", "i",
    "small", "sup", "sub", "code", "kbd", "samp", "var", "mark", "abbr",
    "br", "hr", "ul", "ol", "li", "blockquote", "figure", "figcaption",
    "caption", "a", "img", "table", "thead", "tbody", "tfoot", "tr", "th",
    "td", "colgroup", "col",
}
_VOID_TAGS = {"br", "hr", "img", "col"}
_TEXTUAL_KINDS = {"text", "caption", "metric", "quote"}
_VISUAL_KINDS = {"image", "table", "chart", "embed"}
_IDENTITY_FIELDS = {
    "is_identity_asset",
    "identity_asset_id",
    "identity_asset_role",
    "identity_entity_name",
    "identity_required_to_place",
    "identity_allowed_to_place",
    "identity_primary",
    "identity_asset_intent",
    "identity_group",
    "canonical_entity_key",
    "asset_id",
    "asset_type",
}
AUTHORED_PAPER_POSTER_REQUIRED_STATE_KEY = "academic_paper_poster_authored_required"


@dataclass
class AuthoredPaperPosterRenderResult:
    html_path: Path
    pdf_path: Path
    preview_path: Path
    manifest_path: Path
    dom_audit_path: Path
    gate_audit_path: Path
    size: dict[str, Any]
    sanitized: dict[str, Any]
    dom_audit: dict[str, Any]
    gate_audit: dict[str, Any]
    preview: BrowserRenderResult
    pdf: BrowserRenderResult
    pseudo_layers: list[dict[str, Any]]
    preview_fallback_used: bool = False


@dataclass
class _SanitizedHtml:
    body_html: str
    css: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    used_block_ids: set[str] = field(default_factory=set)
    block_manifest: list[dict[str, Any]] = field(default_factory=list)
    asset_manifest: list[dict[str, Any]] = field(default_factory=list)

    @property
    def p0_count(self) -> int:
        return sum(1 for finding in self.findings if finding.get("severity") == "P0")


def find_authored_paper_poster_frame(spec: Any) -> Any | None:
    artifact = getattr(spec, "html_artifact", None)
    frames = list(getattr(artifact, "frames", []) or [])
    for frame in frames:
        if (
            str(getattr(frame, "kind", "") or "") == "canvas"
            and str(getattr(frame, "render_mode", "") or "") == "authored_html"
        ):
            return frame
    return None


def is_academic_paper_poster_context(spec: Any, ctx: Any) -> bool:
    """Return true when a poster run is contractually a paper/academic poster.

    This is intentionally separate from ``should_use_authored_paper_poster``:
    the former decides whether legacy poster composite is allowed at all,
    while the latter decides whether the current spec has the authored frame
    needed to render through the HTML-first path.
    """
    if not _artifact_type_is_poster(spec, ctx):
        return False
    state = getattr(ctx, "state", {}) if ctx is not None else {}
    if not isinstance(state, dict):
        state = {}
    if state.get(AUTHORED_PAPER_POSTER_REQUIRED_STATE_KEY):
        return True

    brief = state.get("poster_content_brief")
    if isinstance(brief, dict):
        if brief.get("kind") == "paper_poster_content_brief":
            return True
        brief_text = " ".join(
            str(brief.get(k) or "")
            for k in ("kind", "poster_kind", "subtype", "artifact_subtype")
        ).lower()
        if "paper" in brief_text or "academic" in brief_text:
            return True

    contract = state.get("poster_plan_contract")
    if isinstance(contract, dict):
        contract_text = " ".join(
            str(contract.get(k) or "")
            for k in (
                "kind",
                "poster_kind",
                "subtype",
                "artifact_subtype",
                "layout_archetype",
            )
        ).lower()
        if "paper" in contract_text or "academic" in contract_text:
            return True
        canvas_plan = contract.get("canvas_plan")
        if isinstance(canvas_plan, dict):
            plan_text = " ".join(str(canvas_plan.get(k) or "") for k in ("preset_id", "archetype")).lower()
            if "paper" in plan_text or "academic" in plan_text:
                return True

    return False


def should_use_authored_paper_poster(spec: Any, ctx: Any) -> bool:
    if not is_academic_paper_poster_context(spec, ctx):
        return False
    return find_authored_paper_poster_frame(spec) is not None


def _artifact_type_is_poster(spec: Any, ctx: Any) -> bool:
    artifact_type = getattr(spec, "artifact_type", None) if spec is not None else None
    value = getattr(artifact_type, "value", None) or artifact_type
    if value is None:
        state = getattr(ctx, "state", {}) if ctx is not None else {}
        if isinstance(state, dict):
            value = state.get("artifact_type")
    return str(value or "") == "poster"


def render_authored_paper_poster(
    spec: Any,
    ctx: Any,
    *,
    iter_dir: Path,
    iter_num: int,
    timeout_ms: int = 15_000,
) -> AuthoredPaperPosterRenderResult:
    frame = find_authored_paper_poster_frame(spec)
    if frame is None:
        raise ValueError("paper poster authored HTML frame not found")

    canvas = getattr(spec, "canvas", {}) or {}
    cw = int(canvas.get("w_px") or 0)
    ch = int(canvas.get("h_px") or 0)
    if cw <= 0 or ch <= 0:
        raise ValueError("DesignSpec.canvas must include positive w_px/h_px")
    size = resolve_poster_size(spec, frame)
    sanitized = sanitize_authored_paper_poster(frame, ctx)
    if sanitized.p0_count:
        raise ValueError(f"authored paper poster HTML failed sanitizer: {sanitized.findings[:4]}")

    source_visual_assets = _self_contain_source_visual_assets(
        sanitized,
        ctx=ctx,
        iter_dir=iter_dir,
    )

    html_path = iter_dir / "poster.html"
    pdf_path = iter_dir / "poster.pdf"
    preview_path = iter_dir / "preview.png"
    manifest_path = iter_dir / "paper_poster_render_manifest.json"
    dom_audit_path = iter_dir / "paper_poster_dom_audit.json"
    gate_audit_path = iter_dir / "poster_gate_audit.json"
    html_path.write_text(
        _build_shell_html(
            spec=spec,
            frame=frame,
            body_html=sanitized.body_html,
            authored_css=sanitized.css,
            size=size,
            ctx=ctx,
        ),
        encoding="utf-8",
    )
    authored_dom_fit = _apply_authored_paper_poster_dom_fit_pass(
        html_path,
        spec=spec,
        frame=frame,
        timeout_ms=timeout_ms,
    )

    preview = screenshot_html(
        html_path,
        preview_path,
        viewport_width=cw,
        viewport_height=ch,
        selector=".paper-poster",
        max_edge=getattr(ctx.settings, "poster_preview_max_edge", None),
        timeout_ms=timeout_ms,
    )
    preview_fallback_used = bool(preview.warnings)
    if preview.warnings:
        _write_preview_fallback(spec, frame, preview_path, sanitized=sanitized)
        scale, width_px, height_px = downsample_image_to_max_edge(
            preview_path,
            getattr(ctx.settings, "poster_preview_max_edge", None),
        )
        preview.scale = scale
        preview.width_px = width_px
        preview.height_px = height_px

    pdf = export_html_pdf(
        html_path,
        pdf_path,
        viewport_width=cw,
        viewport_height=ch,
        page_width=f"{size['width_mm']:.4f}mm",
        page_height=f"{size['height_mm']:.4f}mm",
        fallback_pngs=[preview_path],
        enforce_single_page=True,
        canvas_selector=".paper-poster",
        canvas_width_px=cw,
        canvas_height_px=ch,
        timeout_ms=timeout_ms,
    )
    dom_audit = audit_authored_paper_poster_dom(
        html_path,
        spec=spec,
        frame=frame,
        sanitized=sanitized,
        ctx=ctx,
        timeout_ms=timeout_ms,
    )
    _audit_source_asset_bindings(dom_audit, ctx, sanitized)
    _augment_preview_pixel_audit(
        dom_audit,
        preview_path,
        cw=cw,
        ch=ch,
        hard=_dogfood_dense_dom_fill_enabled(ctx) and not preview_fallback_used,
        ctx=ctx,
    )
    html_sha = sha256_file(html_path)
    preview_sha = sha256_file(preview_path)
    pdf_sha = sha256_file(pdf_path) if pdf_path.exists() else None
    dom_audit["render_mode"] = "authored_html"
    dom_audit["html_path"] = str(html_path)
    dom_audit["html_sha256"] = html_sha
    dom_audit["dom_audit_html_sha256"] = html_sha
    dom_audit["authored_dom_fit"] = authored_dom_fit
    dom_audit.setdefault("paper_poster_dom_metrics", {}).update({
        "authored_dom_fit_applied": bool(authored_dom_fit.get("applied")),
        "authored_dom_fit_rule_count": int(authored_dom_fit.get("rule_count") or 0),
    })
    gate_audit = audit_authored_poster_gate(
        html_path,
        canvas=canvas,
        poster_size=size,
        dom_audit=dom_audit,
        timeout_ms=timeout_ms,
    )
    gate_audit["html_path"] = str(html_path)
    gate_audit["html_sha256"] = html_sha
    pseudo_layers = authored_poster_pseudo_layers(frame, ctx, dom_audit=dom_audit)
    gate_findings = [
        finding for finding in gate_audit.get("findings") or []
        if isinstance(finding, dict)
    ]
    manifest = {
        "artifact_type": "poster",
        "iteration": iter_num,
        "render_mode": "authored_html",
        "frame_id": getattr(frame, "frame_id", None),
        "html_path": str(html_path),
        "preview_path": str(preview_path),
        "pdf_path": str(pdf_path),
        "html_sha256": html_sha,
        "preview_sha256": preview_sha,
        "pdf_sha256": pdf_sha,
        "dom_audit_html_sha256": html_sha,
        "poster_size": size,
        "sanitizer": _sanitized_payload(sanitized),
        "block_count": len(sanitized.block_manifest),
        "asset_count": len(sanitized.asset_manifest),
        "source_visual_asset_count": len(source_visual_assets),
        "source_visual_assets": source_visual_assets,
        "authored_dom_fit": authored_dom_fit,
        "source_asset_manifest_sha256": _source_asset_manifest_sha256(ctx),
        "pseudo_layer_count": len(pseudo_layers),
        "poster_gate_audit_relative_path": "poster_gate_audit.json",
        "poster_gate_backend": gate_audit.get("backend"),
        "poster_gate_p0_count": int(gate_audit.get("p0_count") or 0),
        "poster_gate_p1_count": int(gate_audit.get("p1_count") or 0),
        "poster_gate_findings_sample": gate_findings[:6],
        "preview_backend": preview.backend,
        "preview_warnings": list(preview.warnings or []),
        "preview_fallback_used": preview_fallback_used,
        "pdf_backend": pdf.backend,
        "pdf_warnings": list(pdf.warnings or []),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    dom_audit_path.write_text(json.dumps(dom_audit, ensure_ascii=False, indent=2), encoding="utf-8")
    gate_audit_path.write_text(json.dumps(gate_audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return AuthoredPaperPosterRenderResult(
        html_path=html_path,
        pdf_path=pdf_path,
        preview_path=preview_path,
        manifest_path=manifest_path,
        dom_audit_path=dom_audit_path,
        gate_audit_path=gate_audit_path,
        size=size,
        sanitized=manifest["sanitizer"],
        dom_audit=dom_audit,
        gate_audit=gate_audit,
        preview=preview,
        pdf=pdf,
        pseudo_layers=pseudo_layers,
        preview_fallback_used=preview_fallback_used,
    )


def resolve_poster_size(spec: Any, frame: Any) -> dict[str, Any]:
    canvas = getattr(spec, "canvas", {}) or {}
    cw = int(canvas.get("w_px") or 0)
    ch = int(canvas.get("h_px") or 0)
    dpi = float(canvas.get("dpi") or _DEFAULT_DPI)
    meta = _model_or_dict(getattr(frame, "poster_size", None))
    preset = str(meta.get("preset") or "").strip()
    source = str(meta.get("source") or "").strip() or ("fallback" if not meta else "custom")

    width_mm = _float_or_none(meta.get("width_mm"))
    height_mm = _float_or_none(meta.get("height_mm"))
    if width_mm is None and _float_or_none(meta.get("width_in")) is not None:
        width_mm = float(meta["width_in"]) * 25.4
    if height_mm is None and _float_or_none(meta.get("height_in")) is not None:
        height_mm = float(meta["height_in"]) * 25.4
    if (width_mm is None or height_mm is None) and preset in _POSTER_SIZE_PRESETS:
        width_mm, height_mm, preset_label = _POSTER_SIZE_PRESETS[preset]
    else:
        preset_label = str(meta.get("label") or preset or "").strip()
    if width_mm is None or height_mm is None:
        width_mm = max(1.0, cw / max(dpi, 1.0) * 25.4)
        height_mm = max(1.0, ch / max(dpi, 1.0) * 25.4)
        if not preset:
            preset = "custom"
        if not preset_label:
            preset_label = f"canvas {cw}x{ch} @ {dpi:g}dpi"

    orientation = str(meta.get("orientation") or "").strip()
    if not orientation:
        orientation = "landscape" if width_mm > height_mm else "portrait" if height_mm > width_mm else "square"
    return {
        "preset": preset or "custom",
        "label": str(meta.get("label") or preset_label or preset or "custom"),
        "source": source,
        "orientation": orientation,
        "width_mm": round(width_mm, 4),
        "height_mm": round(height_mm, 4),
        "width_in": round(width_mm / 25.4, 4),
        "height_in": round(height_mm / 25.4, 4),
        "canvas_w_px": cw,
        "canvas_h_px": ch,
        "dpi": dpi,
    }


def sanitize_authored_paper_poster(frame: Any, ctx: Any) -> _SanitizedHtml:
    blocks = _flatten_blocks([_model_or_dict(b) for b in list(getattr(frame, "blocks", []) or [])])
    block_index = {str(block.get("block_id") or ""): block for block in blocks if str(block.get("block_id") or "")}
    allowed_assets = _allowed_asset_index(blocks, ctx)
    findings: list[dict[str, Any]] = []
    body_html = str(getattr(frame, "authored_body_html", None) or "").strip()
    css = str(getattr(frame, "authored_css", None) or "")

    if not body_html:
        findings.append(_finding("P0", "authored-html-empty", "Authored paper poster frame has no authored_body_html.", "Write the poster body DOM inside the controlled paper-poster shell."))
    if re.search(r"(?is)<\s*/?\s*(html|head|body)\b", body_html):
        findings.append(_finding("P0", "authored-html-document-tags", "authored_body_html includes full document tags.", "Return only the poster body DOM; the renderer owns html/head/body."))
    if re.search(r"(?is)<\s*(script|style|iframe|object)\b", body_html):
        findings.append(_finding("P0", "authored-html-unsafe-tag", "authored_body_html includes unsafe embedded tags.", "Remove script/style/iframe/object tags; put CSS in authored_css."))
    if _REMOTE_URL_RE.search(body_html):
        findings.append(_finding("P0", "authored-html-remote-url", "authored_body_html references a remote or data URL.", "Use only declared local assets from the paper manifest."))
    if _UNSAFE_CSS_RE.search(css) or _REMOTE_URL_RE.search(css):
        findings.append(_finding("P0", "authored-css-unsafe", "authored_css includes import, script-like CSS, style tags, or remote URLs.", "Use scoped local CSS with no imports or remote resources."))
    for issue in lint_tex_math_source(body_html):
        findings.append(_finding(
            str(issue.get("severity") or "P0"),
            str(issue.get("id") or "paper-poster-math-source-invalid"),
            str(issue.get("message") or "Paper poster math source is invalid."),
            str(issue.get("fix") or "Rewrite the formula with valid TeX delimiters."),
            evidence=issue.get("evidence") if isinstance(issue.get("evidence"), dict) else None,
        ))

    explicit_visual_children = _explicit_visual_child_block_ids(body_html)
    parser = _BodySanitizer(
        block_index=block_index,
        allowed_assets=allowed_assets,
        findings=findings,
        explicit_visual_children=explicit_visual_children,
    )
    try:
        parser.feed(body_html)
        parser.close()
    except Exception as e:
        findings.append(_finding("P0", "authored-html-parse-error", f"Could not parse authored_body_html: {type(e).__name__}: {e}", "Rewrite valid HTML body markup."))

    required_ids = {
        block_id for block_id, block in block_index.items()
        if bool(block.get("editable", True))
        and str(block.get("kind") or "") in (_TEXTUAL_KINDS | _VISUAL_KINDS)
    }
    missing = sorted(required_ids - parser.used_block_ids)
    for block_id in missing[:12]:
        findings.append(_finding(
            "P0",
            "authored-html-missing-block",
            f"Editable block '{block_id}' is declared in blocks[] but missing from DOM.",
            "Add an element with the matching data-block-id.",
            block_id=block_id,
        ))

    return _SanitizedHtml(
        body_html=parser.output_html(),
        css=css,
        findings=findings,
        used_block_ids=set(parser.used_block_ids),
        block_manifest=[
            {
                "block_id": block_id,
                "kind": str(block.get("kind") or ""),
                "role": block.get("role"),
                "layer_id": block.get("layer_id"),
                "source_id": block.get("source_id"),
                "editable": bool(block.get("editable", True)),
                "dom_present": block_id in parser.used_block_ids,
                **{
                    key: value
                    for key, value in _block_identity_metadata(block).items()
                    if value is not None
                },
            }
            for block_id, block in sorted(block_index.items())
        ],
        asset_manifest=list(allowed_assets["manifest"]),
    )


def _explicit_visual_child_block_ids(body_html: str) -> set[str]:
    """Visual containers that already authored an image/table child do not need hydration."""
    if not body_html.strip():
        return set()
    try:
        soup = BeautifulSoup(body_html, "html.parser")
    except Exception:
        return set()
    out: set[str] = set()
    for node in soup.find_all(attrs={"data-block-id": True}):
        if not isinstance(node, Tag):
            continue
        block_id = str(node.get("data-block-id") or "").strip()
        if not block_id:
            continue
        for child in node.find_all(["img", "table"]):
            if not isinstance(child, Tag) or child is node:
                continue
            if child.name == "table":
                out.add(block_id)
                break
            if (
                str(child.get("src") or "").strip()
                or str(child.get("data-source-id") or "").strip()
                or str(child.get("data-layer-id") or "").strip()
                or str(child.get("data-block-id") or "").strip()
            ):
                out.add(block_id)
                break
    return out


def audit_authored_paper_poster_dom(
    html_path: Path,
    *,
    spec: Any,
    frame: Any,
    sanitized: _SanitizedHtml,
    ctx: Any | None = None,
    timeout_ms: int = 15_000,
) -> dict[str, Any]:
    canvas = getattr(spec, "canvas", {}) or {}
    cw = int(canvas.get("w_px") or 0)
    ch = int(canvas.get("h_px") or 0)
    findings = list(sanitized.findings)
    warnings: list[str] = []
    metrics: dict[str, Any] = {}
    dom_layers: list[dict[str, Any]] = []
    backend = "static"

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        warnings.append(f"playwright_unavailable: {type(e).__name__}: {e}")
        return _dom_audit_payload(findings, warnings, metrics, dom_layers, backend=backend)

    try:
        with sync_playwright() as p:
            browser = _launch_chromium_for_audit(p)
            page = browser.new_page(
                viewport={"width": max(1, cw), "height": max(1, ch)},
                device_scale_factor=1,
            )
            page.set_default_timeout(timeout_ms)
            page.goto(html_path.resolve().as_uri(), wait_until="load", timeout=timeout_ms)
            wait_for_autodesign_math(page, timeout_ms=min(3000, timeout_ms))
            math_status = collect_autodesign_math_status(page)
            data = page.evaluate(
                """() => {
                  const root = document.querySelector('.paper-poster');
                  if (!root) return {missingRoot: true};
                  const rr = root.getBoundingClientRect();
                  const rectObj = r => ({x: r.x - rr.x, y: r.y - rr.y, w: r.width, h: r.height, right: r.right - rr.x, bottom: r.bottom - rr.y});
                  const clippedRect = (el, r) => {
                    let left = r.left;
                    let top = r.top;
                    let right = r.right;
                    let bottom = r.bottom;
                    let node = el.parentElement;
                    while (node) {
                      const cs = getComputedStyle(node);
                      const overflow = `${cs.overflow} ${cs.overflowX} ${cs.overflowY}`.toLowerCase();
                      if (
                        node === root ||
                        node.hasAttribute('data-lane') ||
                        /(hidden|clip|scroll|auto)/.test(overflow)
                      ) {
                        const cr = node.getBoundingClientRect();
                        left = Math.max(left, cr.left);
                        top = Math.max(top, cr.top);
                        right = Math.min(right, cr.right);
                        bottom = Math.min(bottom, cr.bottom);
                      }
                      if (node === root) break;
                      node = node.parentElement;
                    }
                    if (right < left) right = left;
                    if (bottom < top) bottom = top;
                    return {x: left, y: top, width: right - left, height: bottom - top, right, bottom};
                  };
	                  const textRectsFor = el => {
                    const text = (el.innerText || '').trim();
                    if (!text) return [];
                    const rects = [];
                    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                    let node;
                    while ((node = walker.nextNode())) {
                      if (!node.nodeValue || !node.nodeValue.trim()) continue;
                      const range = document.createRange();
                      range.selectNodeContents(node);
                      for (const r of Array.from(range.getClientRects())) {
                        if (r.width > 1 && r.height > 1) rects.push(rectObj(r));
                        if (rects.length >= 80) break;
                      }
                      range.detach();
                      if (rects.length >= 80) break;
                    }
	                    return rects;
	                  };
                  const panelIdFor = el => {
                    const panel = el.closest('.poster-section,.flow-panel,[data-panel-role],[data-slot-id]');
                    if (!panel || panel === el) return '';
                    return panel.getAttribute('data-block-id') || panel.getAttribute('data-slot-id') || panel.getAttribute('data-panel-role') || '';
                  };
                  const directTextFor = el => Array.from(el.childNodes || [])
                    .filter(node => node.nodeType === Node.TEXT_NODE)
                    .map(node => node.nodeValue || '')
                    .join(' ')
                    .trim()
                    .slice(0, 500);
                  const sourceFlowUnitFor = el => el.closest('.source-flow-unit,.figure-flow-unit');
                  const floatedSourceChildrenFor = unit => {
                    if (!unit) return [];
                    return Array.from(unit.children || []).filter(child => {
                      const tag = child.tagName ? child.tagName.toLowerCase() : '';
                      const className = typeof child.className === 'string' ? child.className.toLowerCase() : '';
                      const hasSourceMarker =
                        tag === 'figure' ||
                        tag === 'img' ||
                        className.includes('flow-asset') ||
                        className.includes('flow-figure') ||
                        className.includes('source-asset') ||
                        className.includes('source-table') ||
                        child.hasAttribute('data-source-id') ||
                        child.hasAttribute('data-layer-id') ||
                        child.getAttribute('data-block-kind') === 'table' ||
                        !!child.querySelector(':scope > img');
                      if (!hasSourceMarker) return false;
                      const cs = window.getComputedStyle(child);
                      return cs.float === 'left' || cs.float === 'right' || className.includes('float-left') || className.includes('float-right');
                    });
                  };
                  const elements = Array.from(root.querySelectorAll('[data-block-id]')).map(el => {
                    const r = el.getBoundingClientRect();
                    const vr = clippedRect(el, r);
                    const cs = window.getComputedStyle(el);
                    const role = el.getAttribute('data-role') || el.getAttribute('role') || '';
                    const kind = el.getAttribute('data-block-kind') || el.tagName.toLowerCase();
                    return {
	                      block_id: el.getAttribute('data-block-id') || '',
                      panel_id: panelIdFor(el),
                      role, kind, tag: el.tagName.toLowerCase(),
                      class_name: typeof el.className === 'string' ? el.className : '',
                      text: (el.innerText || el.getAttribute('alt') || '').slice(0, 500),
                      direct_text: directTextFor(el),
                      child_block_id_count: el.querySelectorAll('[data-block-id]').length,
                      rect: rectObj(r),
                      visible_rect: rectObj(vr),
                      scrollWidth: el.scrollWidth || 0,
                      scrollHeight: el.scrollHeight || 0,
                      clientWidth: el.clientWidth || 0,
                      clientHeight: el.clientHeight || 0,
                      overflowX: cs.overflowX || '',
	                      overflowY: cs.overflowY || '',
                      cssFloat: cs.float || '',
	                      fontSize: cs.fontSize || '',
	                      lineHeight: cs.lineHeight || '',
	                      line_rects: textRectsFor(el),
	                      backgroundColor: cs.backgroundColor || '',
                      borderTopWidth: cs.borderTopWidth || '',
                      borderRightWidth: cs.borderRightWidth || '',
                      borderBottomWidth: cs.borderBottomWidth || '',
                      borderLeftWidth: cs.borderLeftWidth || '',
                      borderTopStyle: cs.borderTopStyle || '',
                      borderRightStyle: cs.borderRightStyle || '',
                      borderBottomStyle: cs.borderBottomStyle || '',
                      borderLeftStyle: cs.borderLeftStyle || '',
                      borderRadius: cs.borderRadius || '',
                      boxShadow: cs.boxShadow || '',
                    };
                  });
	                  const images = Array.from(root.querySelectorAll('img')).map(img => {
	                    const r = img.getBoundingClientRect();
                    const cs = window.getComputedStyle(img);
                    const wrapper = img.closest('figure,.figure-flow-unit,.source-flow-unit,.source-table,.flow-figure,[data-block-kind="table"]');
                    const wrapperStyle = wrapper ? window.getComputedStyle(wrapper) : null;
	                    return {
	                      block_id: img.getAttribute('data-block-id') || '',
                      panel_id: panelIdFor(img),
	                      role: img.getAttribute('data-role') || '',
                      class_name: typeof img.className === 'string' ? img.className : '',
                      wrapper_class_name: wrapper && typeof wrapper.className === 'string' ? wrapper.className : '',
                      cssFloat: cs.float || '',
                      wrapper_css_float: wrapperStyle ? (wrapperStyle.float || '') : '',
                      src: img.getAttribute('src') || '',
                      complete: img.complete,
                      naturalWidth: img.naturalWidth || 0,
                      naturalHeight: img.naturalHeight || 0,
                      rect: rectObj(r),
                    };
                  });
                  const lists = Array.from(root.querySelectorAll('ul,ol')).map((list, index) => {
                    const r = list.getBoundingClientRect();
                    const cs = window.getComputedStyle(list);
                    const unit = sourceFlowUnitFor(list);
                    const floatedSourceChildren = unit && list.parentElement === unit ? floatedSourceChildrenFor(unit) : [];
                    const sourceFlowRect = unit ? unit.getBoundingClientRect() : null;
                    return {
                      element_id: list.getAttribute('data-block-id') || `list_${index}`,
                      block_id: list.getAttribute('data-block-id') || '',
                      panel_id: panelIdFor(list),
                      tag: list.tagName.toLowerCase(),
                      class_name: typeof list.className === 'string' ? list.className : '',
                      text: (list.innerText || '').slice(0, 500),
                      item_count: list.querySelectorAll(':scope > li').length,
                      rect: rectObj(r),
                      display: cs.display || '',
                      paddingInlineStart: cs.paddingInlineStart || '',
                      paddingInlineStartPx: parseFloat(cs.paddingInlineStart || cs.paddingLeft || '0') || 0,
                      paddingLeftPx: parseFloat(cs.paddingLeft || '0') || 0,
                      marginInlineStart: cs.marginInlineStart || '',
                      marginLeftPx: parseFloat(cs.marginLeft || '0') || 0,
                      listStylePosition: cs.listStylePosition || '',
                      textIndentPx: parseFloat(cs.textIndent || '0') || 0,
                      source_flow_id: unit ? (unit.getAttribute('data-block-id') || unit.getAttribute('data-source-id') || unit.getAttribute('data-layer-id') || '') : '',
                      source_flow_class_name: unit && typeof unit.className === 'string' ? unit.className : '',
                      source_flow_rect: sourceFlowRect ? rectObj(sourceFlowRect) : null,
                      has_source_flow_ancestor: !!unit,
                      is_direct_source_flow_child: !!unit && list.parentElement === unit,
                      has_floated_source_sibling: floatedSourceChildren.length > 0,
                      floated_source_sibling_count: floatedSourceChildren.length,
                    };
                  });
                  return {
                    missingRoot: false,
                    root: {w: rr.width, h: rr.height, scrollWidth: root.scrollWidth, scrollHeight: root.scrollHeight, clientWidth: root.clientWidth, clientHeight: root.clientHeight},
                    elements, images, lists,
                    body: {scrollWidth: document.documentElement.scrollWidth, scrollHeight: document.documentElement.scrollHeight}
                  };
                }"""
            )
            if isinstance(data, dict):
                data["math"] = math_status
            browser.close()
        backend = "playwright"
    except Exception as e:
        warnings.append(f"playwright_dom_audit_failed: {type(e).__name__}: {e}")
        return _dom_audit_payload(findings, warnings, metrics, dom_layers, backend=backend)

    if data.get("missingRoot"):
        findings.append(_finding("P0", "paper-poster-root-missing", "Rendered HTML has no .paper-poster root.", "Use the controlled renderer shell and keep the poster root intact."))
        return _dom_audit_payload(findings, warnings, metrics, dom_layers, backend=backend)

    root = data.get("root") or {}
    metrics.update({
        "root_w_px": round(float(root.get("w") or 0), 2),
        "root_h_px": round(float(root.get("h") or 0), 2),
        "root_scroll_w_px": int(root.get("scrollWidth") or 0),
        "root_scroll_h_px": int(root.get("scrollHeight") or 0),
    })
    _append_math_render_findings(
        findings,
        metrics,
        data.get("math") if isinstance(data.get("math"), dict) else {},
    )
    if abs(float(root.get("w") or 0) - cw) > 2 or abs(float(root.get("h") or 0) - ch) > 2:
        findings.append(_finding("P0", "paper-poster-size-mismatch", "Rendered poster root does not match DesignSpec.canvas.", "Revise CSS so .paper-poster uses the renderer-provided canvas size."))
    if int(root.get("scrollWidth") or 0) > cw + 4 or int(root.get("scrollHeight") or 0) > ch + 4:
        findings.append(_finding(
            "P1",
            "paper-poster-overflow",
            "Poster root has scroll overflow.",
            "Repair authored CSS/layout during refinement so all content fits inside the canvas.",
            repair_route="revise_authored_html",
        ))

    elements = [el for el in data.get("elements") or [] if isinstance(el, dict)]
    images = [img for img in data.get("images") or [] if isinstance(img, dict)]
    lists = [lst for lst in data.get("lists") or [] if isinstance(lst, dict)]
    for el in elements:
        rect = _rect(el.get("rect"))
        block_id = str(el.get("block_id") or "")
        if _out_of_bounds(rect, cw, ch):
            findings.append(_finding(
                "P1",
                "paper-poster-block-out-of-bounds",
                f"Block '{block_id}' renders outside the poster canvas.",
                "Move or resize the authored HTML/CSS block during refinement.",
                block_id=block_id,
                repair_route="revise_authored_html",
            ))
        dom_layers.append(_dom_layer_from_element(el, frame))

    dense_dom_fill = _dogfood_dense_dom_fill_enabled(ctx)
    fill_metrics, fill_findings = _dom_canvas_fill_findings(
        elements,
        cw=cw,
        ch=ch,
        hard=dense_dom_fill,
    )
    panel_fill_metrics, panel_fill_findings = _dom_panel_fill_findings(
        elements,
        cw=cw,
        ch=ch,
        hard=dense_dom_fill,
        frame=frame,
    )
    editorial_metrics, editorial_findings = _dom_editorial_layout_findings(
        elements,
        cw=cw,
        ch=ch,
        hard=dense_dom_fill,
    )
    boxiness_metrics, boxiness_findings = _dom_template_boxiness_findings(
        elements,
        cw=cw,
        ch=ch,
        hard=False,
    )
    metrics.update(fill_metrics)
    metrics.update(panel_fill_metrics)
    metrics.update(editorial_metrics)
    metrics.update(boxiness_metrics)
    findings.extend(fill_findings)
    findings.extend(panel_fill_findings)
    findings.extend(editorial_findings)
    findings.extend(boxiness_findings)

    text_overflow_count = 0
    text_overflow_p0_count = 0
    for el in elements:
        if not _is_text_like_dom_element(el):
            continue
        overflow = _dom_text_overflow(el)
        if overflow["word_count"] < 4 or overflow["overflow_ratio"] < 0.08:
            continue
        text_overflow_count += 1
        severity = "P0" if (
            overflow["overflow_ratio"] >= 0.22
            or overflow["height_gap_px"] >= 24
            or overflow["width_gap_px"] >= 40
        ) else "P1"
        if severity == "P0":
            text_overflow_p0_count += 1
        if text_overflow_count <= 12:
            findings.append(_finding(
                severity,
                "paper-poster-text-overflow",
                f"Text block '{el.get('block_id') or '?'}' has clipped or overflowing content.",
                (
                    "Move or reflow nearby elements, increase the block size, split content into "
                    "lanes/table rows, or lower local font size/line-height so all text fits while "
                    "preserving the same paper-specific content density."
                ),
                block_id=str(el.get("block_id") or ""),
                repair_route="reflow_or_shrink_text",
                evidence=overflow,
            ))

    source_flow_metrics, source_flow_findings = _dom_source_flow_text_findings(
        elements,
        images,
        cw=cw,
        ch=ch,
        hard=dense_dom_fill,
    )
    list_gutter_metrics, list_gutter_findings = _dom_source_flow_list_gutter_findings(
        lists,
        hard=dense_dom_fill,
    )
    metrics.update(source_flow_metrics)
    metrics.update(list_gutter_metrics)
    findings.extend(source_flow_findings)
    findings.extend(list_gutter_findings)

    text_overlaps = _dom_text_overlap_findings(elements)
    for finding in text_overlaps[:12]:
        findings.append(finding)
    metrics["text_overflow_count"] = text_overflow_count
    metrics["text_overflow_p0_count"] = text_overflow_p0_count
    metrics["text_overlap_count"] = len(text_overlaps)
    metrics["text_overlap_p0_count"] = sum(
        1 for finding in text_overlaps if str(finding.get("severity") or "").upper() == "P0"
    )
    for img in images:
        if not bool(img.get("complete")) or int(img.get("naturalWidth") or 0) <= 0:
            findings.append(_finding(
                "P0",
                "paper-poster-image-not-loaded",
                f"Image block '{img.get('block_id') or '?'}' did not load.",
                "Use a declared local asset path from the paper manifest.",
                block_id=str(img.get("block_id") or ""),
                repair_route="swap_visual",
            ))

    figure_area = sum(max(0.0, _rect(img.get("rect"))["w"]) * max(0.0, _rect(img.get("rect"))["h"]) for img in images)
    metrics["image_count"] = len(images)
    metrics["figure_area_ratio"] = round(figure_area / float(max(1, cw * ch)), 4)
    min_figure_area = _figure_area_floor_for_canvas(cw=cw, ch=ch)
    metrics["figure_area_min_ratio"] = min_figure_area
    if images and metrics["figure_area_ratio"] < min_figure_area:
        findings.append(_finding(
            "P1",
            "paper-poster-figure-area-low",
            "Authored poster devotes little visible area to source figures for this canvas archetype.",
            "For dense portrait posters, fill sparse regions with source-backed text, native tables, and local explanations; only enlarge figures when readability is actually below the canvas-specific floor.",
            repair_route="revise_authored_html",
            evidence={"figure_area_ratio": metrics["figure_area_ratio"], "min_figure_area_ratio": min_figure_area},
        ))

    captions = [el for el in elements if _is_caption_for_overlap_audit(el)]
    for caption in captions:
        cap_rect = _rect(caption.get("rect"))
        for img in images:
            if _overlap_area(cap_rect, _rect(img.get("rect"))) > 4:
                findings.append(_finding(
                    "P1",
                    "paper-poster-caption-overlap",
                    "Caption overlaps a figure image.",
                    "Move caption below/next to the figure or add spacing.",
                    block_id=str(caption.get("block_id") or ""),
                    repair_route="revise_authored_html",
                ))
                break
    footers = [el for el in elements if any(token in str(el.get("role") or "").lower() for token in ("footer", "takeaway"))]
    for i, left in enumerate(footers):
        for right in footers[i + 1:]:
            if _overlap_area(_rect(left.get("rect")), _rect(right.get("rect"))) > 8:
                findings.append(_finding("P1", "paper-poster-footer-overlap", "Footer/takeaway blocks overlap.", "Compress footer content or adjust the authored CSS grid.", repair_route="shrink_text"))

    return _dom_audit_payload(findings, warnings, metrics, dom_layers, backend=backend, images=images, lists=lists)


def _apply_authored_paper_poster_dom_fit_pass(
    html_path: Path,
    *,
    spec: Any,
    frame: Any,
    timeout_ms: int = 15_000,
) -> dict[str, Any]:
    """Append deterministic CSS repairs for fit-only authored DOM defects."""
    canvas = getattr(spec, "canvas", {}) or {}
    cw = int(canvas.get("w_px") or 0)
    ch = int(canvas.get("h_px") or 0)
    result: dict[str, Any] = {
        "attempted": True,
        "applied": False,
        "backend": "static",
        "rule_count": 0,
        "actions": [],
        "warnings": [],
    }
    if cw <= 0 or ch <= 0 or not html_path.exists():
        result["warnings"].append("dom_fit_skipped_missing_canvas_or_html")
        return result

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        result["warnings"].append(f"playwright_unavailable: {type(e).__name__}: {e}")
        return result

    try:
        with sync_playwright() as p:
            browser = _launch_chromium_for_audit(p)
            page = browser.new_page(
                viewport={"width": max(1, cw), "height": max(1, ch)},
                device_scale_factor=1,
            )
            page.set_default_timeout(timeout_ms)
            page.goto(html_path.resolve().as_uri(), wait_until="load", timeout=timeout_ms)
            wait_for_autodesign_math(page, timeout_ms=min(3000, timeout_ms))
            data = page.evaluate(_AUTHORED_DOM_FIT_SNAPSHOT_JS)
            browser.close()
        result["backend"] = "playwright"
    except Exception as e:
        result["warnings"].append(f"playwright_dom_fit_failed: {type(e).__name__}: {e}")
        return result

    if not isinstance(data, dict) or data.get("missingRoot"):
        result["warnings"].append("dom_fit_skipped_missing_root")
        return result

    css_rules, actions = _authored_dom_fit_css_rules(data, frame=frame, cw=cw, ch=ch)
    if not css_rules:
        return result
    _append_authored_dom_fit_css(html_path, css_rules)
    result.update({
        "applied": True,
        "rule_count": len(css_rules),
        "css_rules": css_rules,
        "actions": actions[:40],
    })
    return result


_AUTHORED_DOM_FIT_SNAPSHOT_JS = """() => {
  const root = document.querySelector('.paper-poster');
  if (!root) return {missingRoot: true};
  const rr = root.getBoundingClientRect();
  const rectObj = r => ({
    x: r.x - rr.x,
    y: r.y - rr.y,
    w: r.width,
    h: r.height,
    right: r.right - rr.x,
    bottom: r.bottom - rr.y
  });
  const clippedRect = (el, r) => {
    let left = r.left;
    let top = r.top;
    let right = r.right;
    let bottom = r.bottom;
    let node = el.parentElement;
    while (node) {
      const cs = getComputedStyle(node);
      const overflow = `${cs.overflow} ${cs.overflowX} ${cs.overflowY}`.toLowerCase();
      if (
        node === root ||
        node.hasAttribute('data-lane') ||
        /(hidden|clip|scroll|auto)/.test(overflow)
      ) {
        const cr = node.getBoundingClientRect();
        left = Math.max(left, cr.left);
        top = Math.max(top, cr.top);
        right = Math.min(right, cr.right);
        bottom = Math.min(bottom, cr.bottom);
      }
      if (node === root) break;
      node = node.parentElement;
    }
    if (right < left) right = left;
    if (bottom < top) bottom = top;
    return {x: left, y: top, width: right - left, height: bottom - top, right, bottom};
  };
  const nearestBlock = el => {
    let parent = el.parentElement;
    while (parent && parent !== root) {
      const id = parent.getAttribute('data-block-id') || '';
      if (id) return id;
      parent = parent.parentElement;
    }
    return '';
  };
  const elements = Array.from(root.querySelectorAll('[data-block-id]')).map(el => {
    const r = el.getBoundingClientRect();
    const vr = clippedRect(el, r);
    const cs = window.getComputedStyle(el);
    return {
      block_id: el.getAttribute('data-block-id') || '',
      parent_block_id: nearestBlock(el),
      role: el.getAttribute('data-role') || el.getAttribute('role') || '',
      kind: el.getAttribute('data-block-kind') || el.tagName.toLowerCase(),
      tag: el.tagName.toLowerCase(),
      class_name: typeof el.className === 'string' ? el.className : '',
      text: (el.innerText || el.getAttribute('alt') || '').slice(0, 1000),
      rect: rectObj(r),
      visible_rect: rectObj(vr),
      scrollWidth: el.scrollWidth || 0,
      scrollHeight: el.scrollHeight || 0,
      clientWidth: el.clientWidth || 0,
      clientHeight: el.clientHeight || 0,
      overflowX: cs.overflowX || '',
      overflowY: cs.overflowY || '',
      fontSize: cs.fontSize || '',
      lineHeight: cs.lineHeight || '',
      position: cs.position || ''
    };
  });
  return {
    missingRoot: false,
    root: {w: rr.width, h: rr.height, scrollWidth: root.scrollWidth, scrollHeight: root.scrollHeight},
    elements
  };
}"""


def _authored_dom_fit_css_rules(
    data: dict[str, Any],
    *,
    frame: Any,
    cw: int,
    ch: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    elements = [el for el in data.get("elements") or [] if isinstance(el, dict)]
    by_id, duplicate_ids = _dom_fit_element_index(elements)
    current_rects = {
        block_id: _rect(el.get("rect"))
        for block_id, el in by_id.items()
    }
    rules: dict[str, dict[str, Any]] = {}
    actions: list[dict[str, Any]] = []
    text_ids = [
        block_id for block_id, el in by_id.items()
        if block_id not in duplicate_ids and _is_text_like_dom_element(el)
    ]
    text_ids.sort(key=lambda block_id: (
        current_rects[block_id]["y"],
        current_rects[block_id]["x"],
        block_id,
    ))

    for block_id in text_ids:
        el = by_id[block_id]
        rect = current_rects[block_id]
        overflow = _dom_fit_overflow(el, rect)
        font_px = _dom_fit_font_px(el)
        line_px = _dom_fit_line_height_px(el, font_px)
        word_count = int(overflow.get("word_count") or 0)
        collapsed = word_count >= 3 and (
            rect["h"] <= max(10.0, line_px * 0.92)
            or rect["w"] <= 24
        )
        needs_height = (
            collapsed
            or overflow["height_gap_px"] >= 4
            or overflow["overflow_ratio"] >= 0.04
        )
        if not needs_height and overflow["width_gap_px"] < 8:
            continue

        if needs_height:
            target_h = _dom_fit_target_height(rect, overflow, line_px, ch)
            delta = int(round(target_h - rect["h"]))
            if delta >= 4:
                shift_ids = _dom_fit_safe_shift_ids(
                    block_id,
                    delta,
                    by_id=by_id,
                    current_rects=current_rects,
                    cw=cw,
                    ch=ch,
                )
                if shift_ids is not None:
                    _dom_fit_set_rule(rules, block_id, "height", target_h)
                    current_rects[block_id] = {**rect, "h": target_h, "bottom": rect["y"] + target_h}
                    actions.append({
                        "type": "expand_text_box",
                        "block_id": block_id,
                        "height_px": round(target_h, 2),
                        "shifted_siblings": shift_ids,
                    })
                    for shifted_id in shift_ids:
                        shifted = current_rects[shifted_id]
                        new_y = shifted["y"] + delta
                        current_rects[shifted_id] = {
                            **shifted,
                            "y": new_y,
                            "bottom": new_y + shifted["h"],
                        }
                        _dom_fit_set_rule(rules, shifted_id, "top", new_y)
                    rect = current_rects[block_id]

        overflow_after_expand = _dom_fit_overflow(el, rect)
        if overflow_after_expand["width_gap_px"] >= 8 or overflow_after_expand["height_gap_px"] >= 4:
            new_font = _dom_fit_reduced_font_px(el, rect, overflow_after_expand)
            if new_font is not None and new_font < font_px - 0.4:
                _dom_fit_set_rule(rules, block_id, "font_size", new_font)
                _dom_fit_set_rule(rules, block_id, "line_height", _dom_fit_line_height_value(el))
                actions.append({
                    "type": "reduce_font_size",
                    "block_id": block_id,
                    "from_px": round(font_px, 2),
                    "to_px": round(new_font, 2),
                    "evidence": {
                        "height_gap_px": overflow_after_expand["height_gap_px"],
                        "width_gap_px": overflow_after_expand["width_gap_px"],
                    },
                })

    _dom_fit_repair_text_overlaps(
        text_ids,
        by_id=by_id,
        current_rects=current_rects,
        rules=rules,
        actions=actions,
        cw=cw,
        ch=ch,
    )
    return _dom_fit_emit_css(rules, frame=frame), actions


def _dom_fit_element_index(elements: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for el in elements:
        block_id = str(el.get("block_id") or "").strip()
        if not block_id:
            continue
        if block_id in by_id:
            duplicate_ids.add(block_id)
            continue
        by_id[block_id] = el
    return by_id, duplicate_ids


def _dom_fit_overflow(el: dict[str, Any], rect: dict[str, float]) -> dict[str, Any]:
    adjusted = dict(el)
    adjusted["rect"] = rect
    adjusted["clientWidth"] = max(_float_value(el.get("clientWidth")), rect["w"])
    adjusted["clientHeight"] = max(_float_value(el.get("clientHeight")), rect["h"])
    return _dom_text_overflow(adjusted)


def _dom_fit_font_px(el: dict[str, Any]) -> float:
    return max(1.0, _float_value(str(el.get("fontSize") or "").replace("px", ""), 16.0))


def _dom_fit_line_height_px(el: dict[str, Any], font_px: float) -> float:
    raw = str(el.get("lineHeight") or "").strip()
    if raw.endswith("px"):
        return max(1.0, _float_value(raw[:-2], font_px * 1.2))
    value = _float_value(raw, 0.0)
    if value > 0 and value < 4:
        return max(1.0, font_px * value)
    return max(1.0, font_px * 1.2)


def _dom_fit_line_height_value(el: dict[str, Any]) -> float:
    font_px = _dom_fit_font_px(el)
    line_px = _dom_fit_line_height_px(el, font_px)
    return round(max(1.05, min(1.28, line_px / max(1.0, font_px))), 3)


def _dom_fit_target_height(
    rect: dict[str, float],
    overflow: dict[str, Any],
    line_px: float,
    ch: int,
) -> float:
    scroll_h = _float_value(overflow.get("scroll_height_px"))
    wanted = max(rect["h"] + 4.0, scroll_h + 6.0, line_px * 1.25)
    max_growth = max(36.0, min(180.0, rect["h"] * 1.8))
    wanted = min(wanted, rect["h"] + max_growth)
    return min(max(wanted, rect["h"]), max(rect["h"], ch - rect["y"] - 4.0))


def _dom_fit_safe_shift_ids(
    block_id: str,
    delta: int,
    *,
    by_id: dict[str, dict[str, Any]],
    current_rects: dict[str, dict[str, float]],
    cw: int,
    ch: int,
) -> list[str] | None:
    el = by_id[block_id]
    rect = current_rects[block_id]
    new_rect = {**rect, "h": rect["h"] + delta, "bottom": rect["y"] + rect["h"] + delta}
    if _out_of_bounds(new_rect, cw, ch):
        return None
    parent_id = str(el.get("parent_block_id") or "")
    shift_ids: list[str] = []
    for other_id, other in by_id.items():
        if other_id == block_id:
            continue
        if str(other.get("parent_block_id") or "") != parent_id:
            continue
        other_rect = current_rects[other_id]
        if other_rect["y"] < rect["y"] + rect["h"] - 2:
            continue
        if _dom_fit_horizontal_overlap(rect, other_rect) < min(rect["w"], other_rect["w"]) * 0.10:
            continue
        if not _dom_fit_can_move(other):
            return None
        shift_ids.append(other_id)

    moved_rects = {block_id: new_rect}
    for shifted_id in shift_ids:
        shifted = current_rects[shifted_id]
        moved = {**shifted, "y": shifted["y"] + delta, "bottom": shifted["y"] + delta + shifted["h"]}
        if _out_of_bounds(moved, cw, ch):
            return None
        moved_rects[shifted_id] = moved

    for moved_id, moved in moved_rects.items():
        old = current_rects[moved_id]
        for other_id, other_rect in current_rects.items():
            if other_id == moved_id or other_id in moved_rects:
                continue
            old_overlap = _overlap_area(old, other_rect)
            new_overlap = _overlap_area(moved, other_rect)
            if new_overlap > max(old_overlap + 16.0, 36.0):
                return None
    return shift_ids


def _dom_fit_can_move(el: dict[str, Any]) -> bool:
    return str(el.get("position") or "").lower() in {"absolute", "fixed"}


def _dom_fit_horizontal_overlap(left: dict[str, float], right: dict[str, float]) -> float:
    return max(0.0, min(left["x"] + left["w"], right["x"] + right["w"]) - max(left["x"], right["x"]))


def _dom_fit_reduced_font_px(
    el: dict[str, Any],
    rect: dict[str, float],
    overflow: dict[str, Any],
) -> float | None:
    font_px = _dom_fit_font_px(el)
    width_ratio = _float_value(overflow.get("scroll_width_px")) / max(1.0, rect["w"])
    height_ratio = _float_value(overflow.get("scroll_height_px")) / max(1.0, rect["h"])
    pressure = max(width_ratio, height_ratio)
    if pressure <= 1.02:
        return None
    target = font_px / min(1.65, pressure * 1.05)
    min_font = 8.0 if font_px <= 15 else max(9.0, font_px * 0.62)
    return max(min_font, min(font_px - 0.5, target))


def _dom_fit_set_rule(rules: dict[str, dict[str, Any]], block_id: str, key: str, value: Any) -> None:
    rules.setdefault(block_id, {})[key] = value


def _dom_fit_repair_text_overlaps(
    text_ids: list[str],
    *,
    by_id: dict[str, dict[str, Any]],
    current_rects: dict[str, dict[str, float]],
    rules: dict[str, dict[str, Any]],
    actions: list[dict[str, Any]],
    cw: int,
    ch: int,
) -> None:
    for _ in range(12):
        repaired = False
        ordered = sorted(text_ids, key=lambda block_id: (
            current_rects[block_id]["y"],
            current_rects[block_id]["x"],
            block_id,
        ))
        for idx, left_id in enumerate(ordered):
            left = current_rects[left_id]
            for right_id in ordered[idx + 1:]:
                right = current_rects[right_id]
                if str(by_id[left_id].get("parent_block_id") or "") != str(by_id[right_id].get("parent_block_id") or ""):
                    continue
                overlap = _overlap_area(left, right)
                if overlap <= 24:
                    continue
                smaller = max(1.0, min(left["w"] * left["h"], right["w"] * right["h"]))
                if overlap / smaller < 0.08 and overlap < 160:
                    continue
                move_id = right_id if right["y"] >= left["y"] else left_id
                if not _dom_fit_can_move(by_id[move_id]):
                    continue
                move_rect = current_rects[move_id]
                other_rect = current_rects[left_id if move_id == right_id else right_id]
                delta = int(round(min(move_rect["y"] + move_rect["h"], other_rect["y"] + other_rect["h"]) - max(move_rect["y"], other_rect["y"]) + 8))
                if delta <= 0:
                    continue
                new_y = move_rect["y"] + delta
                moved = {**move_rect, "y": new_y, "bottom": new_y + move_rect["h"]}
                if _out_of_bounds(moved, cw, ch):
                    continue
                if _dom_fit_shift_creates_collision(move_id, moved, current_rects):
                    continue
                current_rects[move_id] = moved
                _dom_fit_set_rule(rules, move_id, "top", new_y)
                actions.append({
                    "type": "shift_overlapping_text",
                    "block_id": move_id,
                    "delta_y_px": delta,
                    "paired_block_id": left_id if move_id == right_id else right_id,
                })
                repaired = True
                break
            if repaired:
                break
        if not repaired:
            return


def _dom_fit_shift_creates_collision(
    moved_id: str,
    moved: dict[str, float],
    current_rects: dict[str, dict[str, float]],
) -> bool:
    old = current_rects[moved_id]
    for other_id, other_rect in current_rects.items():
        if other_id == moved_id:
            continue
        old_overlap = _overlap_area(old, other_rect)
        new_overlap = _overlap_area(moved, other_rect)
        if new_overlap > max(old_overlap + 16.0, 36.0):
            return True
    return False


def _dom_fit_emit_css(rules: dict[str, dict[str, Any]], *, frame: Any) -> list[str]:
    if not rules:
        return []
    parent_by_id = _authored_dom_parent_block_map(frame)
    blocks = _flatten_blocks([_model_or_dict(b) for b in list(getattr(frame, "blocks", []) or [])])
    lines = ["/* Renderer authored DOM fit pass: fit-only CSS repairs. */"]
    for block_id in sorted(rules):
        values = rules[block_id]
        declarations: list[str] = []
        if "top" in values:
            top_value = float(values["top"])
            parent_id = parent_by_id.get(block_id)
            parent_block = _block_by_id_from_blocks(blocks, parent_id) if parent_id else None
            parent_bbox = _block_bbox(parent_block) if parent_block is not None else None
            css_top = top_value - float(parent_bbox["y"]) if parent_bbox is not None else top_value
            declarations.append(f"top:{max(0.0, css_top):.2f}px !important")
            declarations.append("bottom:auto !important")
        if "height" in values:
            height = max(1.0, float(values["height"]))
            declarations.append(f"height:{height:.2f}px !important")
            declarations.append(f"min-height:{height:.2f}px !important")
            declarations.append("max-height:none !important")
        if "font_size" in values:
            declarations.append(f"font-size:{max(1.0, float(values['font_size'])):.2f}px !important")
        if "line_height" in values:
            declarations.append(f"line-height:{float(values['line_height']):.3f} !important")
        if "height" in values or "font_size" in values:
            declarations.append("overflow:hidden !important")
            declarations.append("overflow-wrap:anywhere !important")
        if declarations:
            lines.append(f"{_dom_fit_selector(block_id)} {{ {'; '.join(declarations)}; }}")
    return lines if len(lines) > 1 else []


def _dom_fit_selector(block_id: str) -> str:
    return f'.paper-poster [data-block-id="{_css_attr_value(block_id)}"][data-block-id]'


def _append_authored_dom_fit_css(html_path: Path, css_rules: list[str]) -> None:
    css = "\n".join(css_rules)
    html = html_path.read_text(encoding="utf-8")
    if re.search(r"(?i)</style>", html):
        html = re.sub(r"(?i)</style>", f"\n{css}\n</style>", html, count=1)
    elif re.search(r"(?i)</head>", html):
        html = re.sub(r"(?i)</head>", f"<style>\n{css}\n</style>\n</head>", html, count=1)
    else:
        html += f"\n<style>\n{css}\n</style>\n"
    html_path.write_text(html, encoding="utf-8")


def authored_poster_pseudo_layers(frame: Any, ctx: Any, *, dom_audit: dict[str, Any]) -> list[dict[str, Any]]:
    dom_by_id = {
        str(layer.get("layer_id") or ""): layer
        for layer in dom_audit.get("dom_layers") or []
        if isinstance(layer, dict)
    }
    rendered = ctx.state.get("rendered_layers") or {}
    identity_assets = _identity_assets_from_state(ctx)
    layers: list[dict[str, Any]] = []
    for block in _flatten_blocks([_model_or_dict(b) for b in list(getattr(frame, "blocks", []) or [])]):
        block_id = str(block.get("block_id") or "")
        kind = str(block.get("kind") or "text")
        layer_id = str(block.get("layer_id") or block.get("source_id") or block_id)
        hydrated = rendered.get(layer_id) or rendered.get(str(block.get("source_id") or "")) or {}
        dom = dom_by_id.get(block_id) or dom_by_id.get(layer_id) or {}
        identity_asset = _matching_identity_asset(block, hydrated, dom, identity_assets)
        identity_meta = _merged_identity_metadata(block, hydrated, dom, identity_asset)
        layer_kind = "text" if kind in _TEXTUAL_KINDS else "table" if kind == "table" else "image" if kind in _VISUAL_KINDS else kind
        layer = {
            "layer_id": layer_id,
            "name": str(block.get("title") or block.get("role") or block_id),
            "kind": layer_kind,
            "role": block.get("role") or dom.get("role") or hydrated.get("role"),
            "class_name": dom.get("class_name") or (block.get("style") or {}).get("class_name") or hydrated.get("class_name"),
            "source": block.get("source") or hydrated.get("source"),
            "source_id": block.get("source_id") or hydrated.get("source_id"),
            "source_page": block.get("source_page") or hydrated.get("source_page"),
            "bbox": dom.get("bbox") or block.get("bbox") or {},
            "text": dom.get("text") or block.get("text") or block.get("caption") or "",
            "caption": block.get("caption") or hydrated.get("caption"),
            "src_path": _prefer_loadable_asset_src(block.get("src_path"), hydrated.get("src_path")),
            "image_size": block.get("image_size") or hydrated.get("image_size"),
            "rows": block.get("rows") or hydrated.get("rows"),
            "headers": block.get("headers") or hydrated.get("headers"),
            "z_index": int((block.get("style") or {}).get("z_index") or 1),
        }
        layer.update({key: value for key, value in identity_meta.items() if value is not None})
        layers.append(layer)
    return layers


def _asset_path_exists(value: Any) -> bool:
    return _existing_local_asset_file(value) is not None


def _existing_local_asset_file(
    value: Any,
    *,
    ctx: Any | None = None,
    iter_dir: Path | None = None,
) -> Path | None:
    for candidate in _local_asset_candidate_paths(value, ctx=ctx, iter_dir=iter_dir):
        try:
            if candidate.exists() and candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _local_asset_candidate_paths(
    value: Any,
    *,
    ctx: Any | None = None,
    iter_dir: Path | None = None,
) -> list[Path]:
    raw = str(value or "").strip()
    if not raw or _unsafe_url(raw):
        return []
    try:
        path = Path(raw).expanduser()
    except OSError:
        return []

    candidates: list[Path] = []

    def add(candidate: Path | None) -> None:
        if candidate is None:
            return
        if candidate not in candidates:
            candidates.append(candidate)

    if path.is_absolute():
        add(path)
        return candidates

    add(path)
    if iter_dir is not None:
        add(iter_dir / path)
    run_dir = getattr(ctx, "run_dir", None) if ctx is not None else None
    if run_dir:
        add(Path(run_dir) / path)
    layers_dir = getattr(ctx, "layers_dir", None) if ctx is not None else None
    if layers_dir:
        layers_path = Path(layers_dir)
        if path.parts and path.parts[0] == "layers":
            add(layers_path / path.name)
        else:
            add(layers_path / path)
    return candidates


def _is_source_visual_file_path(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in _SOURCE_VISUAL_SUFFIXES


def _prefer_loadable_asset_src(primary: Any, fallback: Any) -> Any:
    if _asset_path_exists(fallback) and not _asset_path_exists(primary):
        return fallback
    return primary or fallback


def _self_contain_source_visual_assets(
    sanitized: _SanitizedHtml,
    *,
    ctx: Any,
    iter_dir: Path,
) -> list[dict[str, Any]]:
    """Copy rendered source visuals beside poster.html and rewrite img src.

    ``final/poster.html`` is a symlink to ``composites/iter_NN/poster.html``.
    Some file URL consumers resolve relative assets from the symlink path while
    others resolve from the target path, so we keep the canonical copy beside
    the iteration HTML and mirror the same files under ``final/assets``.
    """
    if not sanitized.body_html.strip() or not sanitized.asset_manifest:
        return []
    allowed_assets = _asset_index_from_manifest(sanitized.asset_manifest)
    soup = BeautifulSoup(sanitized.body_html, "html.parser")
    assets_dir = iter_dir / "assets" / "source_visuals"
    final_assets_dir = _final_source_visual_assets_dir(ctx)
    copied_by_src: dict[str, dict[str, Any]] = {}
    used_names: set[str] = set()
    manifest: list[dict[str, Any]] = []

    for img in soup.find_all("img"):
        if not isinstance(img, Tag):
            continue
        source_path, evidence = _resolve_source_visual_img_path(img, allowed_assets)
        if not source_path:
            continue
        src_path = _existing_local_asset_file(source_path, ctx=ctx, iter_dir=iter_dir)
        if src_path is None:
            continue
        canonical_src = str(src_path.resolve())
        record = copied_by_src.get(canonical_src)
        if record is None:
            filename = _source_visual_asset_filename(src_path, evidence, used_names)
            target = assets_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, target)
            if final_assets_dir is not None:
                final_target = final_assets_dir / filename
                final_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, final_target)
            record = {
                "source_path": canonical_src,
                "relative_path": f"assets/source_visuals/{filename}",
                "filename": filename,
                "sha256": sha256_file(target),
            }
            copied_by_src[canonical_src] = record
            manifest.append(record)
        img["src"] = record["relative_path"]
        if evidence.get("source_id") and not img.get("data-source-id"):
            img["data-source-id"] = evidence["source_id"]

    sanitized.body_html = str(soup)
    return manifest


def _asset_index_from_manifest(manifest: list[dict[str, Any]]) -> dict[str, Any]:
    values: set[str] = set()
    basenames: set[str] = set()
    by_value: dict[str, str] = {}
    by_basename: dict[str, str] = {}
    entries: list[dict[str, Any]] = []

    def add_alias(alias: str, canonical: str) -> None:
        alias = str(alias or "").strip()
        if alias:
            values.add(alias)
            by_value[alias] = canonical

    for entry in manifest:
        if not isinstance(entry, dict):
            continue
        canonical = str(entry.get("src_path") or "").strip()
        if not canonical:
            continue
        raw = str(entry.get("original_src_path") or canonical).strip()
        entries.append(entry)
        for value in (canonical, raw):
            add_alias(value, canonical)
            try:
                path = Path(value).expanduser()
                add_alias(str(path), canonical)
                add_alias(str(path.resolve()), canonical)
                if path.name:
                    basenames.add(path.name)
                    by_basename[path.name] = canonical
            except OSError:
                pass
        for ref_key, include_bare in (
            (entry.get("block_id"), True),
            (
                entry.get("source"),
                str(entry.get("source") or "") not in {
                    "block",
                    "rendered_layers",
                    "layers_dir",
                    "paper_visual_provenance",
                    "academic_identity_assets",
                },
            ),
        ):
            for alias in _local_asset_ref_aliases(ref_key, include_bare=include_bare):
                add_alias(alias, canonical)
    return {
        "values": values,
        "basenames": basenames,
        "by_value": by_value,
        "by_basename": by_basename,
        "manifest": entries,
    }


def _resolve_source_visual_img_path(img: Tag, allowed_assets: dict[str, Any]) -> tuple[str, dict[str, str]]:
    refs: list[tuple[str, str]] = []
    for attr in ("src", "data-source-id", "data-layer-id", "data-block-id"):
        value = str(img.get(attr) or "").strip()
        if value:
            refs.append((attr, value))
    parent = img.parent
    while isinstance(parent, Tag):
        parent_source_id = str(parent.get("data-source-id") or "").strip()
        if parent_source_id:
            refs.append(("parent-data-source-id", parent_source_id))
            break
        parent = parent.parent

    for ref_kind, value in refs:
        source_path = _canonical_declared_asset_src(value, allowed_assets)
        if source_path:
            evidence = {
                "matched_ref": value,
                "matched_ref_kind": ref_kind,
                "block_id": str(img.get("data-block-id") or "").strip(),
                "source_id": str(img.get("data-source-id") or "").strip(),
            }
            if not evidence["source_id"] and ref_kind in {"data-source-id", "parent-data-source-id"}:
                evidence["source_id"] = value
            return source_path, evidence
    return "", {}


def _source_visual_asset_filename(
    src_path: Path,
    evidence: dict[str, str],
    used_names: set[str],
) -> str:
    stem = _safe_asset_stem(
        evidence.get("source_id")
        or evidence.get("block_id")
        or src_path.stem
    )
    suffix = src_path.suffix.lower() or ".png"
    candidate = f"{stem}{suffix}"
    if candidate not in used_names:
        used_names.add(candidate)
        return candidate
    for idx in range(2, 10_000):
        candidate = f"{stem}_{idx}{suffix}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
    fallback = f"{stem}_{abs(hash(str(src_path)))}{suffix}"
    used_names.add(fallback)
    return fallback


def _safe_asset_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    stem = stem.strip("._-")
    return stem or "source_visual"


def _final_source_visual_assets_dir(ctx: Any) -> Path | None:
    run_dir = getattr(ctx, "run_dir", None)
    if not run_dir:
        return None
    return Path(run_dir) / "final" / "assets" / "source_visuals"


def _build_shell_html(*, spec: Any, frame: Any, body_html: str, authored_css: str, size: dict[str, Any], ctx: Any) -> str:
    artifact = getattr(spec, "html_artifact", None)
    title = escape(str(
        getattr(frame, "title", None)
        or getattr(artifact, "title", None)
        or "Paper Poster"
    ))
    cw = int(size["canvas_w_px"])
    ch = int(size["canvas_h_px"])
    width_mm = float(size["width_mm"])
    height_mm = float(size["height_mm"])
    frame_id_raw = str(getattr(frame, "frame_id", None) or "poster_canvas")
    designer_owned_css = _designer_owned_css_frame(frame)
    manifest_css = "" if designer_owned_css else _authored_block_geometry_css(frame)
    safety_css = _authored_layout_safety_css(frame, designer_owned_css=designer_owned_css)
    katex_block = (
        inline_katex_bundle(ctx.settings.repo_root, root_selector=".paper-poster")
        if has_tex_math(body_html)
        else ""
    )
    base_css = f"""
@page {{ size: {width_mm:.4f}mm {height_mm:.4f}mm; margin: 0; }}
html, body {{
  margin: 0;
  width: {cw}px;
  min-height: {ch}px;
  background: #e7e2d8;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
* {{ box-sizing: border-box; }}
.paper-poster {{
  position: relative;
  width: {cw}px;
  height: {ch}px;
  overflow: hidden;
  margin: 0;
  background: #fbfaf6;
  color: #151515;
}}
.paper-poster img {{ display: block; max-width: 100%; height: auto; }}
.paper-poster table {{ border-collapse: collapse; width: 100%; max-width: 100%; table-layout: fixed; }}
.paper-poster [data-block-kind="table"] > table {{ height: 100%; }}
.paper-poster th, .paper-poster td {{ overflow-wrap: anywhere; text-align: left; }}
.paper-poster [contenteditable="true"] {{ outline: 0; }}
.paper-poster [contenteditable="true"]:focus {{ outline: 3px solid rgba(37, 99, 235, 0.55); outline-offset: 4px; }}
@media print {{
  html, body {{ width: {width_mm:.4f}mm; height: {height_mm:.4f}mm; min-height: 0; overflow: hidden; background: #ffffff; }}
  .paper-poster {{ width: 100%; height: 100%; }}
}}
"""
    main_open = _paper_poster_main_open_tag(
        _frame_root_shell(frame),
        base_attrs={
            "data-frame-id": frame_id_raw,
            "data-render-mode": "authored_html",
            "data-poster-size-source": str(size.get("source") or ""),
            "data-w": str(cw),
            "data-h": str(ch),
        },
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width={cw}, initial-scale=1">
  <title>{title}</title>
  <style>
{base_css}
{authored_css}
{manifest_css}
{safety_css}
  </style>
{katex_block}
</head>
<body>
  {main_open}
{body_html}
  </main>
</body>
</html>
"""


def _frame_root_shell(frame: Any) -> dict[str, Any] | None:
    style = getattr(frame, "style", None)
    if not isinstance(style, dict):
        return None
    root_shell = style.get("root_shell")
    return root_shell if isinstance(root_shell, dict) else None


def _paper_poster_main_open_tag(root_shell: dict[str, Any] | None, *, base_attrs: dict[str, Any]) -> str:
    classes = ["paper-poster"]
    attrs: dict[str, str] = {
        str(key): str(value)
        for key, value in base_attrs.items()
        if re.fullmatch(r"data-[A-Za-z0-9_.:-]+", str(key))
    }
    if isinstance(root_shell, dict):
        for cls in root_shell.get("classes") or []:
            cls_str = str(cls).strip()
            if cls_str and cls_str not in classes and _safe_html_class_token(cls_str):
                classes.append(cls_str)
        shell_attrs = root_shell.get("attrs")
        if isinstance(shell_attrs, dict):
            for key, value in shell_attrs.items():
                key_str = str(key or "").strip()
                if key_str in attrs or not _safe_root_shell_attr_name(key_str):
                    continue
                attrs[key_str] = str(value)
    attr_parts = [f'class="{escape(" ".join(classes), quote=True)}"']
    attr_parts.extend(
        f'{key}="{escape(value, quote=True)}"'
        for key, value in attrs.items()
    )
    return f"<main {' '.join(attr_parts)}>"


def _safe_html_class_token(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", value))


def _safe_root_shell_attr_name(value: str) -> bool:
    if value in {"class", "id", "style"}:
        return False
    if value.startswith("data-frame") or value in {"data-w", "data-h"}:
        return False
    return bool(re.fullmatch(r"data-[A-Za-z0-9_.:-]+", value))


def _absolute_geometry_declarations(x: int, y: int, w: int, h: int, *, extras: str = "") -> str:
    return (
        "position:absolute !important; box-sizing:border-box; "
        f"left:{x}px !important; top:{y}px !important; "
        f"width:{w}px !important; height:{h}px !important; "
        "right:auto !important; bottom:auto !important; "
        "transform:none !important; translate:none !important; margin:0;"
        f"{extras}"
    )


def _authored_layout_safety_css(frame: Any, *, designer_owned_css: bool = False) -> str:
    """Final paint-only clipping guard for authored poster DOM."""
    if designer_owned_css:
        return "/* Renderer authored layout safety disabled for designer-owned flow. */"
    blocks = _flatten_blocks([_model_or_dict(b) for b in list(getattr(frame, "blocks", []) or [])])
    panel_ids = sorted({
        str(block.get("block_id") or "").strip()
        for block in blocks
        if _is_panel_container_block(block) and str(block.get("block_id") or "").strip()
    })
    visual_ids = sorted({
        str(block.get("block_id") or "").strip()
        for block in blocks
        if (
            str(block.get("kind") or "").lower() in _VISUAL_KINDS
            and str(block.get("block_id") or "").strip()
        )
    })
    panel_selectors = [
        ".paper-poster.paper-poster [data-panel-role]",
        ".paper-poster.paper-poster [role~=\"panel\"]",
        ".paper-poster.paper-poster .panel",
        ".paper-poster.paper-poster .paper-panel",
        ".paper-poster.paper-poster .poster-panel",
        ".paper-poster.paper-poster .content-panel",
        ".paper-poster.paper-poster .evidence-panel",
        ".paper-poster.paper-poster [class*=\"-panel\"]",
        ".paper-poster.paper-poster [data-block-id^=\"panel_\"]",
        ".paper-poster.paper-poster [data-block-kind=\"panel\"]",
    ]
    panel_selectors.extend(
        f".paper-poster.paper-poster {_block_attr_selector(block_id)}"
        for block_id in panel_ids
    )
    visual_selectors = [
        ".paper-poster.paper-poster figure",
        ".paper-poster.paper-poster .visual-card",
        ".paper-poster.paper-poster [data-block-kind=\"image\"]",
        ".paper-poster.paper-poster [data-block-kind=\"chart\"]",
        ".paper-poster.paper-poster [data-block-kind=\"table\"]",
    ]
    visual_selectors.extend(
        f".paper-poster.paper-poster {_block_attr_selector(block_id)}"
        for block_id in visual_ids
    )
    return "\n".join([
        "/* Renderer authored layout safety: final root/panel clipping guard. */",
        "html, body { overflow:hidden !important; }",
        (
            ".paper-poster.paper-poster { "
            "overflow:hidden !important; overflow-clip-margin:0 !important; "
            "clip-path:inset(0) !important; contain:layout paint style !important; "
            "isolation:isolate !important; "
            "}"
        ),
        (
            ", ".join(dict.fromkeys(panel_selectors))
            + " { overflow:hidden !important; overflow-clip-margin:0 !important; "
            "clip-path:inset(0) !important; contain:layout paint !important; "
            "isolation:isolate !important; }"
        ),
        (
            ", ".join(dict.fromkeys(visual_selectors))
            + " { overflow:hidden !important; overflow-clip-margin:0 !important; "
            "clip-path:inset(0) !important; }"
        ),
        (
            ".paper-poster.paper-poster img, .paper-poster.paper-poster svg, "
            ".paper-poster.paper-poster canvas { max-width:100% !important; max-height:100% !important; }"
        ),
        (
            ".paper-poster.paper-poster table { max-width:100% !important; table-layout:fixed; } "
            ".paper-poster.paper-poster [data-block-kind=\"table\"] > table { "
            "width:100% !important; height:100% !important; } "
            ".paper-poster.paper-poster th, .paper-poster.paper-poster td { overflow-wrap:anywhere; }"
        ),
    ])


def _designer_owned_css_frame(frame: Any) -> bool:
    style = getattr(frame, "style", None)
    if isinstance(style, dict):
        for key in (
            "designer_owned_css",
            "free_css",
            "css_first",
            "browser_flow",
            "layout_mode",
            "compiler_mode",
            "render_contract",
        ):
            if _designer_owned_css_token(style.get(key)):
                return True
    for attr in (
        "designer_owned_css",
        "free_css",
        "css_first",
        "browser_flow",
        "layout_mode",
        "compiler_mode",
        "render_contract",
    ):
        if _designer_owned_css_token(getattr(frame, attr, None)):
            return True
    body = str(getattr(frame, "authored_body_html", "") or "")
    if not body.strip():
        return False
    try:
        soup = BeautifulSoup(body, "html.parser")
    except Exception:
        return False
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        classes = tag.get("class")
        class_tokens = [str(token).strip() for token in classes] if isinstance(classes, list) else str(classes or "").split()
        if {
            "flow-panel",
            "poster-grid",
            "flow-poster",
            "editorial-poster",
            "poster-columns",
            "poster-column",
            "poster-section",
        }.intersection(class_tokens):
            return True
        for key in (
            "data-layout-mode",
            "data-poster-layout-mode",
            "data-css-mode",
            "data-compiler-mode",
            "data-render-contract",
            "data-render-mode",
        ):
            if _designer_owned_css_token(tag.get(key)):
                return True
        for key in ("data-designer-owned-css", "data-free-css", "data-css-first", "data-browser-flow"):
            if _truthy_attr(tag.get(key)):
                return True
    return False


def _designer_owned_css_token(value: Any) -> bool:
    if _truthy_attr(value):
        return True
    text = str(value or "").strip().lower().replace("_", "-")
    if not text:
        return False
    return any(
        token in text
        for token in (
            "designer-owned-css",
            "planner-owned-css",
            "authored-css",
            "free-css",
            "css-first",
            "browser-flow",
            "panel-flow",
            "flow-dom",
            "flow-poster",
        )
    )


def _truthy_attr(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {
        "1", "true", "yes", "y", "on",
        "designer_owned_css", "designer-owned-css",
        "planner_owned_css", "planner-owned-css",
    }


def _authored_block_geometry_css(frame: Any) -> str:
    """Realize html_artifact block bboxes even when authored CSS omits them."""
    blocks = _flatten_blocks([_model_or_dict(b) for b in list(getattr(frame, "blocks", []) or [])])
    if not blocks:
        return ""
    panels = [block for block in blocks if _is_panel_container_block(block) and _block_bbox(block)]
    dom_parent_by_id = _authored_dom_parent_block_map(frame)
    lines = [
        "/* Manifest geometry: fallback realization of html_artifact.blocks[].bbox. */",
    ]
    for block in blocks:
        block_id = str(block.get("block_id") or "").strip()
        bbox = _block_bbox(block)
        if not block_id or bbox is None:
            continue
        paint_bbox = _source_visual_intrinsic_fit_bbox(block, bbox)
        x, y, w, h = paint_bbox["x"], paint_bbox["y"], paint_bbox["w"], paint_bbox["h"]
        extras = ""
        if str(block.get("kind") or "").lower() in _VISUAL_KINDS:
            extras = " object-fit:contain !important; object-position:center top !important;"
        selector = _manifest_geometry_selector(block, blocks)
        if selector is not None:
            lines.append(
                f"{selector} {{ {_absolute_geometry_declarations(x, y, w, h, extras=extras)} }}"
            )
        dom_parent_id = dom_parent_by_id.get(block_id)
        dom_parent = _block_by_id_from_blocks(blocks, dom_parent_id) if dom_parent_id else None
        dom_parent_bbox = _block_bbox(dom_parent) if dom_parent is not None else None
        if dom_parent_id and dom_parent_bbox is not None:
                rel_x = max(0, x - dom_parent_bbox["x"])
                rel_y = max(0, y - dom_parent_bbox["y"])
                lines.append(
                    f"{_block_selector(dom_parent_id)} > {_block_attr_selector(block_id)} "
                    f"{{ {_absolute_geometry_declarations(rel_x, rel_y, w, h, extras=extras)} }}"
                )
                continue
        parent = _containing_panel_block(block, panels)
        if parent is not None:
            parent_id = str(parent.get("block_id") or "").strip()
            parent_bbox = _block_bbox(parent)
            if parent_id and parent_bbox is not None:
                rel_x = max(0, x - parent_bbox["x"])
                rel_y = max(0, y - parent_bbox["y"])
                lines.append(
                    f"{_block_selector(parent_id)} > {_block_attr_selector(block_id)} "
                    f"{{ {_absolute_geometry_declarations(rel_x, rel_y, w, h, extras=extras)} }}"
                )
    lines.extend(_authored_title_typography_guard_css(blocks))
    lines.extend(_authored_title_stack_guard_css(blocks, panels))
    lines.append(
        ".paper-poster img[data-hydrated-from-block=\"true\"] "
        "{ left:0 !important; top:0 !important; width:100% !important; height:100% !important; "
        "object-fit:contain !important; object-position:center top !important; }"
    )
    lines.append(
        ".paper-poster img[data-source-id], .paper-poster img[data-layer-id], "
        ".paper-poster img[src*=\"source_visuals/\"] "
        "{ object-fit:contain !important; object-position:center top !important; }"
    )
    lines.append(
        ".paper-poster .visual-card:not([data-block-id]) "
        "{ position:absolute !important; transform:none !important; }"
    )
    lines.append(
        ".paper-poster .visual-card:not([data-block-id]) > [data-block-id] "
        "{ position:static !important; left:auto !important; top:auto !important; }"
    )
    return "\n".join(lines)


def _authored_dom_parent_block_map(frame: Any) -> dict[str, str]:
    body = str(getattr(frame, "authored_body_html", "") or "")
    if not body.strip():
        return {}
    try:
        soup = BeautifulSoup(body, "html.parser")
    except Exception:
        return {}
    out: dict[str, str] = {}
    for node in soup.find_all(attrs={"data-block-id": True}):
        block_id = str(node.get("data-block-id") or "").strip()
        if not block_id:
            continue
        parent = getattr(node, "parent", None)
        while parent is not None and getattr(parent, "name", None) is not None:
            parent_id = str(parent.get("data-block-id") or "").strip() if hasattr(parent, "get") else ""
            if parent_id:
                out[block_id] = parent_id
                break
            parent = getattr(parent, "parent", None)
    return out


def _block_by_id_from_blocks(blocks: list[dict[str, Any]], block_id: str | None) -> dict[str, Any] | None:
    if not block_id:
        return None
    for block in blocks:
        if str(block.get("block_id") or "") == str(block_id):
            return block
    return None


def _manifest_geometry_selector(block: dict[str, Any], blocks: list[dict[str, Any]]) -> str | None:
    """Scope fallback geometry so authored nested layouts do not double-offset.

    The planner often authors wrapper elements such as `.model-card-rows` or
    `.venue-wrap` that position their children in CSS. A broad
    `.paper-poster [data-block-id]` rule then applies absolute bbox offsets
    inside those already-positioned wrappers, pushing elements off-canvas. The
    renderer fallback should only realize manifest geometry for top-level
    blocks; panel-contained children get a direct-child fallback below.
    """
    block_id = str(block.get("block_id") or "").strip()
    bbox = _block_bbox(block)
    if not block_id or bbox is None:
        return None
    for other in blocks:
        other_id = str(other.get("block_id") or "").strip()
        other_bbox = _block_bbox(other)
        if not other_id or other_id == block_id or other_bbox is None:
            continue
        if _strictly_contains_bbox(other_bbox, bbox):
            return None
    return f".paper-poster > {_block_attr_selector(block_id)}"


def _authored_title_typography_guard_css(blocks: list[dict[str, Any]]) -> list[str]:
    bboxes = [bbox for block in blocks if (bbox := _block_bbox(block)) is not None]
    if not bboxes:
        return []
    canvas_w = max(bbox["x"] + bbox["w"] for bbox in bboxes)
    canvas_h = max(bbox["y"] + bbox["h"] for bbox in bboxes)
    lines: list[str] = ["/* Renderer title guard: keep long paper titles out of author/meta rows. */"]
    for block in blocks:
        block_id = str(block.get("block_id") or "").strip()
        if not block_id:
            continue
        bbox = _block_bbox(block)
        if bbox is None or not _is_top_title_block(block, bbox, canvas_w, canvas_h):
            continue
        text = str(block.get("text") or block.get("title") or block.get("caption") or "").strip()
        font_px = _fit_top_title_font_px(text, bbox)
        if font_px is None:
            continue
        selector = _block_selector(block_id)
        lines.append(
            f"{selector} {{ font-size:{font_px}px !important; line-height:1.08 !important; "
            "overflow-wrap:normal !important; word-break:normal !important; hyphens:none !important; "
            "max-height:none !important; }"
        )
        lines.append(
            f"{selector}.paper-poster-title-fit, {selector} {{ --paper-poster-title-fit:{font_px}; }}"
        )
    return lines if len(lines) > 1 else []


def _authored_title_stack_guard_css(blocks: list[dict[str, Any]], panels: list[dict[str, Any]]) -> list[str]:
    bboxes = [bbox for block in blocks if (bbox := _block_bbox(block)) is not None]
    if not bboxes:
        return []
    canvas_w = max(bbox["x"] + bbox["w"] for bbox in bboxes)
    canvas_h = max(bbox["y"] + bbox["h"] for bbox in bboxes)
    lines: list[str] = []
    for panel in panels:
        panel_bbox = _block_bbox(panel)
        panel_id = str(panel.get("block_id") or "").strip()
        if not panel_id or panel_bbox is None or not _is_top_title_panel(panel, panel_bbox, canvas_w, canvas_h):
            continue
        stack_items: list[tuple[str, dict[str, Any], dict[str, int]]] = []
        for block in blocks:
            block_id = str(block.get("block_id") or "").strip()
            if not block_id or block_id == panel_id:
                continue
            bbox = _block_bbox(block)
            if bbox is None:
                continue
            slot = _title_stack_slot(block, bbox, panel_bbox)
            if slot is None:
                continue
            if (
                not _title_stack_panel_contains_or_clips(panel_bbox, bbox)
                and not _title_stack_overflow_candidate(panel_bbox, bbox, slot)
            ):
                continue
            stack_items.append((slot, block, bbox))
        if not _title_stack_needs_guard(stack_items, panel_bbox):
            continue
        lines.extend(_emit_title_stack_guard_css(panel_id, panel_bbox, stack_items))
    return lines


def _is_top_title_block(block: dict[str, Any], bbox: dict[str, int], canvas_w: int, canvas_h: int) -> bool:
    block_id = str(block.get("block_id") or "").lower()
    role = str(block.get("role") or "").lower()
    kind = str(block.get("kind") or "").lower()
    if kind not in _TEXTUAL_KINDS:
        return False
    title_ids = {"title", "main_title", "title_main", "paper_title", "poster_title", "title_text", "headline", "paper_heading", "poster_heading"}
    if role != "title" and block_id not in title_ids and "title" not in block_id and "headline" not in block_id:
        return False
    main_title_ids = title_ids
    if (
        block_id not in main_title_ids
        and not block_id.startswith(("paper_title", "poster_title", "main_title", "title_main", "headline"))
    ):
        return False
    if bbox["y"] > max(180, canvas_h * 0.14):
        return False
    min_w = max(520, canvas_w * 0.38)
    if bbox["w"] < min_w:
        return False
    return True


def _is_top_title_panel(block: dict[str, Any], bbox: dict[str, int], canvas_w: int, canvas_h: int) -> bool:
    block_id = str(block.get("block_id") or "").lower()
    role = str(block.get("role") or "").lower()
    if bbox["y"] > max(180, canvas_h * 0.13):
        return False
    min_panel_h = 110 if canvas_w > canvas_h else 220
    if bbox["w"] < canvas_w * 0.65 or bbox["h"] < min_panel_h:
        return False
    return (
        "title" in block_id
        or "meta" in block_id
        or "header" in block_id
        or role in {"title", "header", "banner"}
    )


def _title_stack_slot(block: dict[str, Any], bbox: dict[str, int], panel_bbox: dict[str, int]) -> str | None:
    block_id = str(block.get("block_id") or "").lower()
    role = str(block.get("role") or "").lower()
    kind = str(block.get("kind") or "").lower()
    if kind not in _TEXTUAL_KINDS:
        return None
    if bbox["x"] > panel_bbox["x"] + panel_bbox["w"] * 0.72:
        return None
    if "kicker" in block_id or "kicker" in role or "eyebrow" in block_id or "eyebrow" in role:
        return "kicker"
    title_ids = {"title", "main_title", "title_main", "paper_title", "poster_title", "title_text", "headline", "paper_heading", "poster_heading"}
    if (
        role == "title"
        or block_id in title_ids
        or block_id.startswith(("paper_title", "poster_title", "main_title", "title_main", "headline"))
    ):
        return "title"
    if role == "subtitle" or "subtitle" in block_id:
        return "subtitle"
    if (
        role.startswith("meta")
        or block_id in {"meta", "meta_row", "venue_row", "affiliation_row"}
        or block_id.startswith(("meta_claim", "meta_chip", "meta_chips", "venue", "affiliation", "header_positioning"))
        or any(token in role for token in ("section-bar", "section-title", "section_heading", "section_label", "panel-label", "badge", "kicker", "eyebrow"))
    ):
        if "badge" not in block_id:
            return "meta"
    if role in {"meta", "byline"} or "author" in block_id:
        if "venue" not in block_id and "badge" not in block_id:
            return "authors"
    if "thesis" in block_id or "claim" in block_id:
        return "thesis"
    return None


def _title_stack_panel_contains_or_clips(panel_bbox: dict[str, int], child_bbox: dict[str, int]) -> bool:
    if _strictly_contains_bbox(panel_bbox, child_bbox):
        return True
    panel_area = max(1.0, float(panel_bbox["w"] * panel_bbox["h"]))
    child_area = max(1.0, float(child_bbox["w"] * child_bbox["h"]))
    overlap = _bbox_overlap_area(panel_bbox, child_bbox)
    if overlap <= 0:
        return False
    if overlap / min(panel_area, child_area) < 0.35:
        return False
    panel_bottom = panel_bbox["y"] + panel_bbox["h"]
    return child_bbox["y"] <= panel_bottom + max(32, int(round(panel_bbox["h"] * 0.16)))


def _title_stack_overflow_candidate(panel_bbox: dict[str, int], child_bbox: dict[str, int], slot: str) -> bool:
    if slot not in {"kicker", "title", "subtitle", "authors", "meta", "thesis"}:
        return False
    if child_bbox["x"] + child_bbox["w"] < panel_bbox["x"] or child_bbox["x"] > panel_bbox["x"] + panel_bbox["w"]:
        return False
    panel_bottom = panel_bbox["y"] + panel_bbox["h"]
    overflow_allowance = max(72, int(round(panel_bbox["h"] * 1.05)))
    return child_bbox["y"] <= panel_bottom + overflow_allowance


def _bbox_overlap_area(a: dict[str, int], b: dict[str, int]) -> float:
    x1 = max(float(a["x"]), float(b["x"]))
    y1 = max(float(a["y"]), float(b["y"]))
    x2 = min(float(a["x"] + a["w"]), float(b["x"] + b["w"]))
    y2 = min(float(a["y"] + a["h"]), float(b["y"] + b["h"]))
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _title_stack_needs_guard(
    stack_items: list[tuple[str, dict[str, Any], dict[str, int]]],
    panel_bbox: dict[str, int],
) -> bool:
    if len(stack_items) < 2:
        return False
    panel_bottom = panel_bbox["y"] + panel_bbox["h"]
    if any(item[2]["y"] + item[2]["h"] > panel_bottom for item in stack_items):
        return True
    if len(stack_items) < 3:
        return False
    ordered = sorted(stack_items, key=lambda item: (item[2]["y"], _title_stack_order(item[0])))
    y_values = [item[2]["y"] for item in ordered]
    if max(y_values) - min(y_values) <= 40:
        return True
    for left, right in zip(ordered, ordered[1:]):
        left_bbox = left[2]
        right_bbox = right[2]
        overlap = min(left_bbox["y"] + left_bbox["h"], right_bbox["y"] + right_bbox["h"]) - max(left_bbox["y"], right_bbox["y"])
        if overlap > min(left_bbox["h"], right_bbox["h"]) * 0.25:
            return True
    return False


def _emit_title_stack_guard_css(
    panel_id: str,
    panel_bbox: dict[str, int],
    stack_items: list[tuple[str, dict[str, Any], dict[str, int]]],
) -> list[str]:
    by_slot: dict[str, tuple[dict[str, Any], dict[str, int]]] = {}
    for slot, block, bbox in sorted(stack_items, key=lambda item: _title_stack_order(item[0])):
        by_slot.setdefault(slot, (block, bbox))
    if "title" not in by_slot:
        return []
    min_rel_x = min(max(0, bbox["x"] - panel_bbox["x"]) for _, bbox in by_slot.values())
    raw_top = min(max(0, bbox["y"] - panel_bbox["y"]) for _, bbox in by_slot.values())
    gap = max(6, min(10, int(round(panel_bbox["h"] * 0.018))))
    bottom_pad = max(8, min(18, int(round(panel_bbox["h"] * 0.035))))
    cursor = max(8, min(raw_top, max(8, int(round(panel_bbox["h"] * 0.12)))))
    ordered_slots = [slot for slot in ("kicker", "title", "subtitle", "authors", "meta", "thesis") if slot in by_slot]
    heights = {
        slot: _title_stack_height(slot, by_slot[slot][1])
        for slot in ordered_slots
    }
    available_height = max(1, panel_bbox["h"] - cursor - bottom_pad - gap * max(0, len(ordered_slots) - 1))
    base_height = sum(heights.values())
    if base_height > available_height:
        scale = available_height / float(max(1, base_height))
        for slot in ordered_slots:
            heights[slot] = max(_title_stack_min_height(slot), int(round(heights[slot] * scale)))
        while sum(heights.values()) > available_height:
            reducible = [
                slot for slot in ordered_slots
                if heights[slot] > _title_stack_min_height(slot)
            ]
            if not reducible:
                break
            slot = max(reducible, key=lambda item: heights[item])
            heights[slot] -= 1
    total_height = sum(heights.values()) + gap * max(0, len(ordered_slots) - 1)
    if cursor + total_height > panel_bbox["h"] - bottom_pad:
        cursor = max(8, panel_bbox["h"] - bottom_pad - total_height)
    lines = ["/* Renderer title stack guard: repair collapsed title-band vertical bboxes. */"]
    for slot in ordered_slots:
        item = by_slot[slot]
        block, bbox = item
        block_id = str(block.get("block_id") or "").strip()
        height = heights[slot]
        width = _title_stack_width(slot, bbox, panel_bbox, min_rel_x)
        font_css = _title_stack_font_css(slot, block, {**bbox, "w": width, "h": height})
        lines.append(
            f"{_block_selector(panel_id)} {_block_attr_selector(block_id)} "
            f"{{ left:{min_rel_x}px !important; top:{cursor}px !important; "
            f"width:{width}px !important; height:{height}px !important; "
            f"max-height:none !important; overflow:hidden !important; {font_css} }}"
        )
        cursor += height + gap
    return lines if len(lines) > 1 else []


def _title_stack_order(slot: str) -> int:
    return {"kicker": 0, "title": 1, "subtitle": 2, "authors": 3, "meta": 4, "thesis": 5}.get(slot, 99)


def _title_stack_height(slot: str, bbox: dict[str, int]) -> int:
    minimum = {"kicker": 20, "title": 70, "subtitle": 30, "authors": 20, "meta": 18, "thesis": 24}.get(slot, 24)
    maximum = {"kicker": 36, "title": 170, "subtitle": 80, "authors": 60, "meta": 40, "thesis": 78}.get(slot, 78)
    return max(minimum, min(maximum, bbox["h"]))


def _title_stack_min_height(slot: str) -> int:
    return {"kicker": 16, "title": 52, "subtitle": 22, "authors": 16, "meta": 14, "thesis": 18}.get(slot, 16)


def _title_stack_width(slot: str, bbox: dict[str, int], panel_bbox: dict[str, int], rel_x: int) -> int:
    right_margin = int(round(panel_bbox["w"] * (0.30 if slot in {"kicker", "title", "subtitle", "authors", "meta"} else 0.36)))
    max_w = max(360, panel_bbox["w"] - rel_x - right_margin)
    return max(320, min(max_w, bbox["w"]))


def _title_stack_font_css(slot: str, block: dict[str, Any], bbox: dict[str, int]) -> str:
    height = max(1, int(bbox.get("h") or 1))
    if slot == "kicker":
        return f"font-size:{max(11, min(18, int(round(height * 0.62))))}px !important; line-height:1.06 !important;"
    if slot == "title":
        text = str(block.get("text") or block.get("title") or block.get("caption") or "")
        fitted = _fit_top_title_font_px(text, bbox) or min(110, max(42, int(round(height * 0.72))))
        font_px = max(28, min(fitted, int(round(height * 0.78))))
        return f"font-size:{font_px}px !important; line-height:1.06 !important;"
    if slot == "subtitle":
        return f"font-size:{max(16, min(42, int(round(height * 0.70))))}px !important; line-height:1.10 !important;"
    if slot == "authors":
        return f"font-size:{max(12, min(22, int(round(height * 0.66))))}px !important; line-height:1.18 !important;"
    if slot == "meta":
        return f"font-size:{max(11, min(18, int(round(height * 0.64))))}px !important; line-height:1.18 !important;"
    return f"font-size:{max(12, min(24, int(round(height * 0.66))))}px !important; line-height:1.20 !important;"


def _fit_top_title_font_px(text: str, bbox: dict[str, int]) -> int | None:
    units = _text_fit_units(text)
    if units <= 0:
        return None
    width = max(1, bbox["w"])
    min_font = max(24, min(58, int(round(bbox["h"] * 0.72))))
    if units > 40:
        cap = 74
    elif units > 32:
        cap = 78
    elif units > 26:
        cap = 84
    elif units > 20:
        cap = 94
    else:
        cap = 110
    if len(text) > 32 or units * cap > width * 0.84:
        cap = min(cap, int(max(min_font, bbox["h"] * 1.06 / (2 * 1.08))))
    width_cap = int(width * 1.92 / units)
    return max(min_font, min(cap, width_cap))


def _text_fit_units(text: str) -> float:
    units = 0.0
    for char in text:
        if char.isspace():
            units += 0.28
        elif ord(char) > 127:
            units += 0.95
        elif char in ":-–—/()[]{}":
            units += 0.34
        elif char in "il.,'":
            units += 0.25
        elif char.isupper():
            units += 0.62
        else:
            units += 0.52
    return units


def _block_selector(block_id: str) -> str:
    return f".paper-poster {_block_attr_selector(block_id)}"


def _block_attr_selector(block_id: str) -> str:
    return f'[data-block-id="{_css_attr_value(block_id)}"]'


def _css_attr_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _block_bbox(block: dict[str, Any]) -> dict[str, int] | None:
    bbox = block.get("bbox") if isinstance(block.get("bbox"), dict) else None
    if not bbox:
        return None
    try:
        x = int(round(float(bbox.get("x") or 0)))
        y = int(round(float(bbox.get("y") or 0)))
        w = max(1, int(round(float(bbox.get("w") or 0))))
        h = max(1, int(round(float(bbox.get("h") or 0))))
    except (TypeError, ValueError):
        return None
    return {"x": x, "y": y, "w": w, "h": h}


def _source_visual_intrinsic_fit_bbox(block: dict[str, Any], bbox: dict[str, int]) -> dict[str, int]:
    if not _is_intrinsic_source_visual_block(block):
        return bbox
    aspect = _source_visual_aspect_from_block(block)
    if aspect <= 0:
        return bbox
    max_w = max(1, int(bbox["w"]))
    max_h = max(1, int(bbox["h"]))
    fit_w = float(max_w)
    fit_h = fit_w / aspect
    if fit_h > max_h:
        fit_h = float(max_h)
        fit_w = fit_h * aspect
    w = max(1, min(max_w, int(round(fit_w))))
    h = max(1, min(max_h, int(round(fit_h))))
    return {
        "x": int(bbox["x"]) + max(0, int(round((max_w - w) / 2.0))),
        "y": int(bbox["y"]),
        "w": w,
        "h": h,
    }


def _is_intrinsic_source_visual_block(block: dict[str, Any]) -> bool:
    if bool(block.get("is_identity_asset")):
        return False
    role_blob = " ".join(
        str(block.get(key) or "")
        for key in (
            "block_id",
            "role",
            "layer_id",
            "source_id",
            "source",
            "src_path",
            "asset_type",
        )
    ).lower()
    if any(token in role_blob for token in ("identity", "logo", "badge", "avatar", "qr")):
        return False
    kind = str(block.get("kind") or "").lower()
    if kind not in _VISUAL_KINDS:
        return False
    source = str(block.get("source") or "").lower()
    if source in {"paper_visual", "ingested_pdf", "paper_visual_provenance"}:
        return True
    return any(
        token in role_blob
        for token in (
            "source_visual",
            "source-figure",
            "source_figure",
            "ingest_fig",
            "ingest_table",
            "paper_visual",
            "local_evidence",
        )
    )


def _source_visual_aspect_from_block(block: dict[str, Any]) -> float:
    parsed = _aspect_from_image_size(block.get("image_size"))
    if parsed > 0:
        return parsed
    path = _existing_local_asset_file(block.get("src_path"))
    if path is None:
        return 0.0
    try:
        with Image.open(path) as img:
            w, h = img.size
    except Exception:
        return 0.0
    if w <= 0 or h <= 0:
        return 0.0
    return float(w) / float(h)


def _aspect_from_image_size(value: Any) -> float:
    if isinstance(value, dict):
        try:
            w = float(value.get("w") or value.get("width") or value.get("naturalWidth") or 0)
            h = float(value.get("h") or value.get("height") or value.get("naturalHeight") or 0)
        except (TypeError, ValueError):
            return 0.0
        return w / h if w > 0 and h > 0 else 0.0
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*$", str(value or ""))
    if not match:
        return 0.0
    try:
        w = float(match.group(1))
        h = float(match.group(2))
    except ValueError:
        return 0.0
    return w / h if w > 0 and h > 0 else 0.0


def _is_panel_container_block(block: dict[str, Any]) -> bool:
    block_id = str(block.get("block_id") or "").lower()
    kind = str(block.get("kind") or "").lower()
    role = str(block.get("role") or "").lower()
    slot_id = str(block.get("slot_id") or "").lower()
    return (
        kind == "group"
        or "panel" in role
        or block_id.startswith("panel_")
        or "panel" in slot_id
        or "band" in slot_id
    )


def _containing_panel_block(block: dict[str, Any], panels: list[dict[str, Any]]) -> dict[str, Any] | None:
    block_id = str(block.get("block_id") or "")
    bbox = _block_bbox(block)
    if bbox is None:
        return None
    candidates: list[tuple[int, dict[str, Any]]] = []
    for panel in panels:
        panel_id = str(panel.get("block_id") or "")
        if not panel_id or panel_id == block_id:
            continue
        panel_bbox = _block_bbox(panel)
        if panel_bbox is None:
            continue
        if _strictly_contains_bbox(panel_bbox, bbox):
            candidates.append((panel_bbox["w"] * panel_bbox["h"], panel))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _strictly_contains_bbox(parent: dict[str, int], child: dict[str, int]) -> bool:
    if parent["w"] * parent["h"] <= child["w"] * child["h"]:
        return False
    return (
        child["x"] >= parent["x"]
        and child["y"] >= parent["y"]
        and child["x"] + child["w"] <= parent["x"] + parent["w"]
        and child["y"] + child["h"] <= parent["y"] + parent["h"]
    )


class _BodySanitizer(HTMLParser):
    def __init__(
        self,
        *,
        block_index: dict[str, dict[str, Any]],
        allowed_assets: dict[str, Any],
        findings: list[dict[str, Any]],
        explicit_visual_children: set[str] | None = None,
    ):
        super().__init__(convert_charrefs=False)
        self.block_index = block_index
        self.allowed_assets = allowed_assets
        self.findings = findings
        self.explicit_visual_children = set(explicit_visual_children or set())
        self.used_block_ids: set[str] = set()
        self.out: list[str] = []
        self.skip_depth = 0
        self.open_blocks: list[tuple[str, str, str]] = []

    def output_html(self) -> str:
        return "".join(self.out)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start_tag(tag, attrs, closed=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start_tag(tag, attrs, closed=True)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_depth:
            if tag in _SKIP_CONTENT_TAGS:
                self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag in _DOC_TAGS:
            return
        if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.out.append(f"</{tag}>")
            self._pop_open_block(tag)

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.out.append(escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self.skip_depth:
            self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.skip_depth:
            self.out.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        return

    def _start_tag(self, tag: str, attrs: list[tuple[str, str | None]], *, closed: bool) -> None:
        tag = tag.lower()
        if self.skip_depth:
            if tag in _SKIP_CONTENT_TAGS:
                self.skip_depth += 1
            return
        if tag in _SKIP_CONTENT_TAGS:
            self.findings.append(_finding("P0", "authored-html-unsafe-tag", f"Unsafe <{tag}> tag is not allowed.", "Remove scripts/styles from authored_body_html."))
            self.skip_depth = 1
            return
        if tag in _DOC_TAGS:
            self.findings.append(_finding("P0", "authored-html-document-tag", f"Document-level <{tag}> tag is not allowed.", "Return only body-internal DOM."))
            return
        if tag not in _ALLOWED_TAGS:
            if not attrs and _TEXT_TOKEN_TAG_RE.match(tag):
                self.out.append(escape(f"<{tag}>", quote=False))
                self.findings.append(_finding(
                    "low",
                    "authored-html-text-token-escaped",
                    f"Text-like token <{tag}> was escaped instead of treated as an HTML element.",
                    "Write comparison/math tokens such as T+1 as text or escaped HTML entities.",
                ))
                return
            self.findings.append(_finding("P0", "authored-html-disallowed-tag", f"Tag <{tag}> is not allowed in paper poster body HTML.", "Use semantic div/section/figure/table/text tags only."))
            return

        attr_map = {str(k).lower(): "" if v is None else str(v) for k, v in attrs}
        block_id = attr_map.get("data-block-id", "").strip()
        parent_block_id = self._nearest_open_block_id()
        parent_source_id = self._nearest_open_source_id()
        if block_id and block_id not in self.block_index:
            repaired_block_id = ""
            if tag == "img":
                repaired_block_id = _infer_image_block_id_from_src(
                    attr_map.get("src", ""),
                    self.block_index,
                    self.allowed_assets,
                )
            if not repaired_block_id:
                repaired_block_id = _repair_authored_dom_block_id(
                    block_id,
                    self.block_index,
                    visual_only=(tag == "img"),
                )
            if repaired_block_id:
                block_id = repaired_block_id
                attr_map["data-block-id"] = repaired_block_id
            elif tag != "img" and parent_block_id and parent_block_id in self.block_index:
                self.findings.append(_finding(
                    "low",
                    "authored-html-nested-unknown-block-stripped",
                    (
                        f"Nested structural element used undeclared data-block-id "
                        f"'{block_id}'; sanitizer removed the id and kept it under "
                        f"parent block '{parent_block_id}'."
                    ),
                    "Declare the wrapper as a manifest block only if it needs independent geometry.",
                    block_id=block_id,
                    evidence=_html_attr_evidence(attr_map, parent_block_id=parent_block_id),
                ))
                attr_map.pop("data-block-id", None)
                block_id = ""
        if tag == "img" and not block_id:
            inferred_block_id = _infer_image_block_id_from_src(
                attr_map.get("src", ""),
                self.block_index,
                self.allowed_assets,
            )
            if not inferred_block_id and parent_source_id:
                inferred_block_id = _infer_image_block_id_from_src(
                    parent_source_id,
                    self.block_index,
                    self.allowed_assets,
                )
            if not inferred_block_id:
                inferred_block_id = _infer_block_id_from_html_attrs(
                    attr_map,
                    self.block_index,
                    visual_only=True,
                )
            if not inferred_block_id and parent_block_id:
                parent_block = self.block_index.get(parent_block_id) or {}
                if str(parent_block.get("kind") or "") in _VISUAL_KINDS:
                    inferred_block_id = parent_block_id
            if inferred_block_id:
                block_id = inferred_block_id
                attr_map["data-block-id"] = inferred_block_id
        block = self.block_index.get(block_id) if block_id else None
        if block_id:
            self.used_block_ids.add(block_id)
            if block is None:
                self.findings.append(_finding(
                    "P0",
                    "authored-html-unknown-block",
                    f"DOM references unknown data-block-id '{block_id}'.",
                    "Use only block ids declared in html_artifact.frames[].blocks[].",
                    block_id=block_id,
                    evidence=_html_attr_evidence(attr_map, parent_block_id=parent_block_id),
                ))
        if tag == "img" and not block_id:
            self.findings.append(_finding(
                "P0",
                "authored-html-image-missing-block-id",
                "Image element is missing data-block-id.",
                "Bind every figure/table image to a block manifest id.",
                evidence=_html_attr_evidence(attr_map, parent_block_id=parent_block_id),
            ))

        clean_attrs: list[tuple[str, str]] = []
        for key, value in attr_map.items():
            if block and key == "data-block-kind":
                continue
            if key.startswith("on"):
                self.findings.append(_finding("P0", "authored-html-event-handler", f"Event handler attribute '{key}' is not allowed.", "Remove event handlers; poster HTML must be static."))
                continue
            if key not in _allowed_attrs_for(tag) and not key.startswith("data-") and not key.startswith("aria-"):
                continue
            if key in {"src", "href"}:
                if _unsafe_url(value):
                    self.findings.append(_finding("P0", "authored-html-unsafe-url", f"Unsafe URL in {key}.", "Use only declared local assets or local anchors.", block_id=block_id or None))
                    continue
                if tag == "img":
                    canonical_src = _canonical_declared_asset_src(value, self.allowed_assets)
                    if not canonical_src:
                        self.findings.append(_finding("P0", "authored-html-undeclared-asset", f"Image src is not declared in the asset manifest: {value}", "Use the src_path from the matching image/table block.", block_id=block_id or None))
                        continue
                    value = canonical_src
                if tag == "img" and block:
                    declared_src = _declared_asset_src_for_block(block_id, block, self.allowed_assets)
                    if declared_src:
                        value = declared_src
            if key == "style" and (_REMOTE_URL_RE.search(value) or _UNSAFE_CSS_RE.search(value)):
                self.findings.append(_finding("P0", "authored-html-unsafe-inline-style", "Inline style contains unsafe URL/import/script-like CSS.", "Remove URL/import/expression from inline style.", block_id=block_id or None))
                continue
            clean_attrs.append((key, value))

        if block:
            kind = str(block.get("kind") or "")
            role = str(block.get("role") or kind or "")
            clean_attrs.append(("data-block-kind", kind))
            if role and not any(k == "data-role" for k, _ in clean_attrs):
                clean_attrs.append(("data-role", role))
            if tag == "img" and kind in _VISUAL_KINDS and not any(k == "src" for k, _ in clean_attrs):
                src = _declared_asset_src_for_block(block_id, block, self.allowed_assets)
                resolved_parent_source_id = ""
                if not src:
                    src, resolved_parent_source_id = _declared_parent_asset_src_for_img(
                        parent_block_id,
                        self.block_index,
                        self.allowed_assets,
                    )
                if not src and parent_source_id:
                    src = _canonical_declared_asset_src(parent_source_id, self.allowed_assets)
                    resolved_parent_source_id = parent_source_id if src else ""
                if src:
                    clean_attrs.append(("src", src))
                if resolved_parent_source_id and not any(k == "data-source-id" for k, _ in clean_attrs):
                    clean_attrs.append(("data-source-id", resolved_parent_source_id))
            if tag == "img" and not any(k == "alt" for k, _ in clean_attrs):
                alt = str(block.get("title") or block.get("caption") or block.get("role") or block_id).strip()
                if alt:
                    clean_attrs.append(("alt", alt))
            if kind in _TEXTUAL_KINDS and tag not in {"img", "table", "thead", "tbody", "tfoot", "tr"}:
                if not any(k == "contenteditable" for k, _ in clean_attrs):
                    clean_attrs.append(("contenteditable", "true"))
        else:
            kind = ""

        hydrated_visual_src = ""
        if (
            block
            and tag in {"div", "span", "figure"}
            and kind in _VISUAL_KINDS
            and block_id not in self.explicit_visual_children
        ):
            hydrated_visual_src = _declared_asset_src_for_block(block_id, block, self.allowed_assets)

        attr_text = "".join(f' {escape(k, quote=True)}="{escape(v, quote=True)}"' for k, v in clean_attrs)
        if closed or tag in _VOID_TAGS:
            self.out.append(f"<{tag}{attr_text}>")
        else:
            self.out.append(f"<{tag}{attr_text}>")
            source_id = str(attr_map.get("data-source-id") or "").strip()
            self.open_blocks.append((tag, block_id if block is not None else "", source_id))
            if hydrated_visual_src:
                role = escape(str(block.get("role") or kind or ""), quote=True)
                alt = escape(
                    str(block.get("title") or block.get("caption") or block.get("role") or block_id).strip(),
                    quote=True,
                )
                src = escape(hydrated_visual_src, quote=True)
                bid = escape(block_id, quote=True)
                self.out.append(
                    f'<img src="{src}" alt="{alt}" data-block-id="{bid}" '
                    f'data-block-kind="image" data-role="{role}" data-hydrated-from-block="true">'
                )

    def _nearest_open_block_id(self) -> str:
        for _, block_id, _ in reversed(self.open_blocks):
            if block_id:
                return block_id
        return ""

    def _nearest_open_source_id(self) -> str:
        for _, _, source_id in reversed(self.open_blocks):
            if source_id:
                return source_id
        return ""

    def _pop_open_block(self, tag: str) -> None:
        if not self.open_blocks:
            return
        if self.open_blocks[-1][0] == tag:
            self.open_blocks.pop()
            return
        for idx in range(len(self.open_blocks) - 1, -1, -1):
            if self.open_blocks[idx][0] == tag:
                del self.open_blocks[idx:]
                return


def _allowed_attrs_for(tag: str) -> set[str]:
    base = {"class", "id", "role", "title", "style", "contenteditable", "tabindex"}
    if tag == "img":
        return base | {"src", "alt", "width", "height", "loading", "decoding"}
    if tag == "a":
        return base | {"href"}
    if tag in {"td", "th"}:
        return base | {"colspan", "rowspan", "scope"}
    if tag == "col":
        return base | {"span", "width"}
    return base


def _html_attr_evidence(attr_map: dict[str, str], *, parent_block_id: str = "") -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for key in ("src", "alt", "id", "class", "role"):
        value = str(attr_map.get(key) or "").strip()
        if value:
            evidence[key] = value[:240]
    if parent_block_id:
        evidence["parent_block_id"] = parent_block_id
    return evidence


def _infer_block_id_from_html_attrs(
    attr_map: dict[str, str],
    block_index: dict[str, dict[str, Any]],
    *,
    visual_only: bool = False,
) -> str:
    tokens: list[str] = []
    for key in ("id", "class", "role", "data-role"):
        raw = str(attr_map.get(key) or "")
        tokens.extend(token for token in re.split(r"[\s,]+", raw) if token)
    for token in tokens:
        repaired = _repair_authored_dom_block_id(token, block_index, visual_only=visual_only)
        if repaired:
            return repaired
    return ""


def _repair_authored_dom_block_id(
    value: str,
    block_index: dict[str, dict[str, Any]],
    *,
    visual_only: bool = False,
) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    lower_index = {key.lower(): key for key in block_index}
    for candidate in _authored_dom_block_id_aliases(raw):
        key = candidate if candidate in block_index else lower_index.get(candidate.lower(), "")
        if not key:
            continue
        if visual_only and str((block_index.get(key) or {}).get("kind") or "") not in _VISUAL_KINDS:
            continue
        return key
    return ""


def _authored_dom_block_id_aliases(value: str) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    aliases: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip("_- ")
        if candidate and candidate not in aliases:
            aliases.append(candidate)
            swapped = candidate.replace("-", "_")
            if swapped and swapped not in aliases:
                aliases.append(swapped)
            swapped = candidate.replace("_", "-")
            if swapped and swapped not in aliases:
                aliases.append(swapped)

    queue = [raw]
    seen: set[str] = set()
    suffixes = (
        "_img_inner", "_image_inner", "_visual_img", "_visual_image",
        "_img", "_image", "_inner", "_el", "_node",
        "-img-inner", "-image-inner", "-visual-img", "-visual-image",
        "-img", "-image", "-inner", "-el", "-node",
    )
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        add(current)
        for suffix in suffixes:
            if current.lower().endswith(suffix):
                queue.append(current[: -len(suffix)])
    return aliases


def _allowed_asset_index(blocks: list[dict[str, Any]], ctx: Any) -> dict[str, Any]:
    rendered = ctx.state.get("rendered_layers") if hasattr(ctx, "state") else {}
    rendered = rendered or {}
    entries: list[dict[str, Any]] = []
    values: set[str] = set()
    basenames: set[str] = set()
    by_value: dict[str, str] = {}
    by_basename: dict[str, str] = {}

    def prefer_candidate(existing: str, candidate: str) -> str:
        if not existing:
            return candidate
        existing_exists = _asset_path_exists(existing)
        candidate_exists = _asset_path_exists(candidate)
        if candidate_exists and not existing_exists:
            return candidate
        return existing

    def add(path_value: Any, *, block_id: str | None = None, source: str | None = None) -> None:
        if not path_value:
            return
        raw = str(path_value)
        variants = {raw}
        canonical = raw
        existing = _existing_local_asset_file(raw, ctx=ctx)
        if existing is not None:
            canonical = str(existing)
            variants.add(canonical)
        try:
            p = Path(raw).expanduser()
            variants.add(str(p))
            resolved = str(p.resolve())
            variants.add(resolved)
            if p.exists():
                canonical = resolved
            basenames.add(p.name)
            by_basename[p.name] = prefer_candidate(by_basename.get(p.name, ""), canonical)
        except OSError:
            pass
        values.update(v for v in variants if v)
        for variant in variants:
            if variant:
                by_value[variant] = canonical
        for ref_key, include_bare in (
            (block_id, True),
            (source, source not in {"block", "rendered_layers", "layers_dir", "paper_visual_provenance", "academic_identity_assets"}),
        ):
            for alias in _local_asset_ref_aliases(ref_key, include_bare=include_bare):
                values.add(alias)
                by_value[alias] = canonical
        entries.append({"src_path": canonical, "original_src_path": raw, "block_id": block_id, "source": source})

    for block in blocks:
        block_id = str(block.get("block_id") or "")
        add(block.get("src_path"), block_id=block_id, source="block")
        for key in (block.get("layer_id"), block.get("source_id")):
            rec = rendered.get(str(key or ""))
            if isinstance(rec, dict):
                add(rec.get("src_path"), block_id=block_id, source=str(key))
    for layer_id, rec in rendered.items():
        if isinstance(rec, dict):
            add(rec.get("src_path"), block_id=str(layer_id), source="rendered_layers")
    if hasattr(ctx, "state"):
        identity_assets = ctx.state.get("academic_identity_assets") or {}
        for asset in list(identity_assets.get("assets") or []):
            if not isinstance(asset, dict):
                continue
            asset_id = str(asset.get("rendered_layer_id") or asset.get("asset_id") or "").strip()
            for key in ("local_asset_path", "source_svg_path", "source_file"):
                add(asset.get(key), block_id=asset_id, source=asset_id or "academic_identity_assets")

        provenance = ctx.state.get("paper_visual_provenance") or {}
        for asset in list(provenance.get("assets") or []):
            if not isinstance(asset, dict):
                continue
            asset_id = str(
                asset.get("layer_id")
                or asset.get("asset_id")
                or asset.get("source_id")
                or ""
            ).strip()
            for key in ("src_path", "output_file", "local_asset_path", "path"):
                add(asset.get(key), block_id=asset_id, source=asset_id or "paper_visual_provenance")

    layer_dirs: list[Path] = []
    layers_dir = getattr(ctx, "layers_dir", None)
    if layers_dir:
        layer_dirs.append(Path(layers_dir))
    run_dir = getattr(ctx, "run_dir", None)
    if run_dir:
        run_layers = Path(run_dir) / "layers"
        if run_layers not in layer_dirs:
            layer_dirs.append(run_layers)
    for layer_dir in layer_dirs:
        try:
            for path in layer_dir.iterdir():
                if not _is_source_visual_file_path(path):
                    continue
                stem = path.stem
                layer_key = stem
                if stem.startswith("img_"):
                    layer_key = stem[4:]
                add(str(path), block_id=layer_key, source="layers_dir")
        except OSError:
            pass
    return {
        "values": values,
        "basenames": basenames,
        "by_value": by_value,
        "by_basename": by_basename,
        "manifest": entries,
    }


def _is_declared_asset(value: str, allowed_assets: dict[str, Any]) -> bool:
    return bool(_canonical_declared_asset_src(value, allowed_assets))


def _canonical_declared_asset_src(value: str, allowed_assets: dict[str, Any]) -> str:
    if not value:
        return ""
    value = value.strip()
    by_value = allowed_assets.get("by_value") or {}
    by_basename = allowed_assets.get("by_basename") or {}
    if value in by_value:
        return str(by_value[value])
    normalized_alias = _normalize_local_asset_ref(value)
    if normalized_alias and normalized_alias in by_value:
        return str(by_value[normalized_alias])
    try:
        p = Path(value).expanduser()
        expanded = str(p)
        if expanded in by_value:
            return str(by_value[expanded])
        resolved = str(p.resolve())
        if resolved in by_value:
            return str(by_value[resolved])
        if p.name in by_basename:
            return str(by_basename[p.name])
    except OSError:
        return ""
    if value in allowed_assets.get("values", set()):
        return value
    return ""


def _local_asset_ref_aliases(value: Any, *, include_bare: bool = False) -> set[str]:
    key = str(value or "").strip()
    if not key:
        return set()
    aliases = {
        f"{{{{{key}}}}}",
        f"{{{{layer:{key}}}}}",
        f"{{{{asset:{key}}}}}",
        f"layer:{key}",
        f"asset:{key}",
    }
    if include_bare:
        aliases.add(key)
    return aliases


def _normalize_local_asset_ref(value: str) -> str:
    raw = str(value or "").strip()
    match = re.fullmatch(r"\{\{\s*(layer|asset)\s*:\s*([^{}]+?)\s*\}\}", raw)
    if match:
        return f"{{{{{match.group(1)}:{match.group(2).strip()}}}}}"
    match = re.fullmatch(r"\{\{\s*([^{}:]+?)\s*\}\}", raw)
    if match:
        return f"{{{{{match.group(1).strip()}}}}}"
    match = re.fullmatch(r"(layer|asset)\s*:\s*(.+)", raw)
    if match:
        return f"{match.group(1)}:{match.group(2).strip()}"
    return raw


def _infer_image_block_id_from_src(
    value: str,
    block_index: dict[str, dict[str, Any]],
    allowed_assets: dict[str, Any],
) -> str:
    canonical_src = _canonical_declared_asset_src(value, allowed_assets)
    if not canonical_src:
        return ""
    candidates: list[str] = []
    for block_id, block in block_index.items():
        if str(block.get("kind") or "") not in _VISUAL_KINDS:
            continue
        declared_src = _declared_asset_src_for_block(block_id, block, allowed_assets)
        if declared_src and declared_src == canonical_src:
            candidates.append(block_id)
    return candidates[0] if len(candidates) == 1 else ""


def _asset_manifest_rank(entry: dict[str, Any], canonical_src: str) -> tuple[int, int, int]:
    source = str(entry.get("source") or "")
    try:
        absolute = Path(canonical_src).expanduser().is_absolute()
    except OSError:
        absolute = False
    return (
        1 if _asset_path_exists(canonical_src) else 0,
        1 if source != "block" else 0,
        1 if absolute else 0,
    )


def _declared_asset_src_for_block(
    block_id: str,
    block: dict[str, Any],
    allowed_assets: dict[str, Any],
) -> str:
    keys = {
        str(block_id or "").strip(),
        str(block.get("layer_id") or "").strip(),
        str(block.get("source_id") or "").strip(),
    }
    keys.discard("")
    manifest = [
        entry for entry in list(allowed_assets.get("manifest") or [])
        if isinstance(entry, dict) and str(entry.get("src_path") or "").strip()
    ]
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for match_key in ("block_id", "source"):
        for entry in manifest:
            if str(entry.get(match_key) or "").strip() in keys:
                src = str(entry.get("src_path") or "").strip()
                canonical_src = _canonical_declared_asset_src(src, allowed_assets)
                if canonical_src:
                    candidates.append((_asset_manifest_rank(entry, canonical_src), canonical_src))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return ""


def _declared_parent_asset_src_for_img(
    parent_block_id: str,
    block_index: dict[str, dict[str, Any]],
    allowed_assets: dict[str, Any],
) -> tuple[str, str]:
    parent_block = block_index.get(str(parent_block_id or "").strip())
    if not isinstance(parent_block, dict):
        return "", ""
    if str(parent_block.get("kind") or "") not in _VISUAL_KINDS:
        return "", ""
    src = _declared_asset_src_for_block(parent_block_id, parent_block, allowed_assets)
    if not src:
        return "", ""
    source_id = str(parent_block.get("source_id") or parent_block.get("layer_id") or "").strip()
    return src, source_id


def _unsafe_url(value: str) -> bool:
    raw = value.strip().lower()
    return raw.startswith(("http://", "https://", "//", "data:", "javascript:", "file:"))


def _write_preview_fallback(spec: Any, frame: Any, preview_path: Path, *, sanitized: _SanitizedHtml) -> None:
    canvas = getattr(spec, "canvas", {}) or {}
    cw = max(1, int(canvas.get("w_px") or 1))
    ch = max(1, int(canvas.get("h_px") or 1))
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (cw, ch), "#fbfaf6")
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, cw - 1, ch - 1], outline="#d8d2c4", width=max(1, cw // 600))
    y = max(24, ch // 40)
    for block in sanitized.block_manifest[:18]:
        text = f"{block.get('kind')}: {block.get('block_id')}"
        draw.text((max(24, cw // 40), y), text, fill="#2f3437")
        y += max(18, ch // 80)
    image.save(preview_path)


def _dom_audit_payload(
    findings: list[dict[str, Any]],
    warnings: list[str],
    metrics: dict[str, Any],
    dom_layers: list[dict[str, Any]],
    *,
    backend: str,
    images: list[dict[str, Any]] | None = None,
    lists: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "paper_poster_dom_backend": backend,
        "paper_poster_dom_warnings": warnings,
        "paper_poster_dom_findings": findings,
        "paper_poster_dom_p0_count": sum(1 for finding in findings if finding.get("severity") == "P0"),
        "paper_poster_dom_metrics": metrics,
        "dom_layers": dom_layers,
        "dom_images": images or [],
        "dom_lists": lists or [],
    }


def _figure_area_floor_for_canvas(*, cw: int, ch: int) -> float:
    if cw <= 0 or ch <= 0:
        return 0.12
    if cw < ch:
        return 0.12
    if cw >= ch * 1.7:
        return 0.16
    return 0.18


def _dogfood_dense_dom_fill_enabled(ctx: Any | None = None) -> bool:
    settings = getattr(ctx, "settings", None) if ctx is not None else None
    return effective_poster_harness_mode(settings) == "dogfood"


def _dom_canvas_fill_findings(
    elements: list[dict[str, Any]],
    *,
    cw: int,
    ch: int,
    hard: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    boxes = _dom_content_fill_boxes(elements, cw=cw, ch=ch)
    severity = "P0" if hard else "P1"
    if not boxes:
        metrics = {
            "content_bottom_ratio": 0.0,
            "lower_quarter_content_coverage": 0.0,
            "lower_half_content_coverage": 0.0,
            "middle_lower_content_coverage": 0.0,
        }
        return metrics, [_finding(
            severity,
            "paper-poster-canvas-underfilled",
            "Dense paper poster has no measurable text/figure/table content.",
            "Fill the poster canvas with dense source-backed panels before finalizing.",
            repair_route="revise_authored_html",
            evidence=metrics,
        )]

    bottom_px = max(float(box["y"] + box["h"]) for box in boxes)
    bottom_ratio = round(bottom_px / float(max(1, ch)), 4)
    lower_quarter = _dom_band_grid_coverage(boxes, cw=cw, ch=ch, y0=ch * 0.75, y1=ch)
    lower_half = _dom_band_grid_coverage(boxes, cw=cw, ch=ch, y0=ch * 0.50, y1=ch)
    middle_lower = _dom_band_grid_coverage(boxes, cw=cw, ch=ch, y0=ch * 0.42, y1=ch * 0.74)
    is_portrait = ch > cw
    min_bottom = 0.92 if is_portrait else 0.90
    min_lower_quarter = 0.10 if is_portrait else 0.12
    min_lower_half = 0.18 if is_portrait else 0.22
    min_middle_lower = 0.16 if is_portrait else 0.18
    metrics = {
        "content_bottom_ratio": bottom_ratio,
        "content_bottom_px": round(bottom_px, 2),
        "lower_quarter_content_coverage": lower_quarter,
        "lower_half_content_coverage": lower_half,
        "middle_lower_content_coverage": middle_lower,
        "min_content_bottom_ratio": min_bottom,
        "min_lower_quarter_content_coverage": min_lower_quarter,
        "min_lower_half_content_coverage": min_lower_half,
        "min_middle_lower_content_coverage": min_middle_lower,
    }
    reasons: list[str] = []
    if bottom_ratio < min_bottom:
        reasons.append("content_stops_before_bottom")
    if lower_quarter < min_lower_quarter:
        reasons.append("lower_quarter_sparse")
    if lower_half < min_lower_half:
        reasons.append("lower_half_sparse")
    if middle_lower < min_middle_lower:
        reasons.append("middle_lower_sparse")
    if not reasons:
        return metrics, []
    if (
        reasons == ["content_stops_before_bottom"]
        and bottom_ratio >= min_bottom - 0.035
        and lower_quarter >= min_lower_quarter * 2.0
        and lower_half >= min_lower_half * 2.0
        and middle_lower >= min_middle_lower * 2.0
    ):
        metrics["near_threshold_bottom_margin_warn"] = True
        metrics["reasons"] = reasons
        return metrics, []
    metrics["reasons"] = reasons
    return metrics, [_finding(
        severity,
        "paper-poster-canvas-underfilled",
        "Dense paper poster leaves too much of the fixed canvas visibly unused.",
        "Extend the dense storyboard to the bottom edge with evidence panels, source visuals, compact tables, limitations, and provenance instead of leaving a blank lower band.",
        repair_route="revise_authored_html",
        evidence=metrics,
    )]


def _dom_panel_fill_findings(
    elements: list[dict[str, Any]],
    *,
    cw: int,
    ch: int,
    hard: bool,
    frame: Any | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    panels = _dom_panel_boxes(elements, cw=cw, ch=ch)
    content_boxes = _dom_content_fill_boxes(elements, cw=cw, ch=ch)
    checked: list[dict[str, Any]] = []
    sparse: list[dict[str, Any]] = []
    blank_band_panels: list[dict[str, Any]] = []
    content_type_fail_panels: list[dict[str, Any]] = []
    earned_area_fail_panels: list[dict[str, Any]] = []
    earned_scores: list[float] = []
    canvas_area = max(1.0, float(cw * ch))
    for panel in panels:
        rect = panel["rect"]
        large_panel = _is_large_dom_panel(rect, cw=cw, ch=ch)
        local_elements = [
            el for el in elements
            if _is_dom_content_for_fill(el, canvas_area=max(1.0, float(cw * ch)), ch=ch)
            and _rect_center_inside(_rect(el.get("rect")), rect)
            and not _same_rectish(_rect(el.get("rect")), rect)
        ]
        local_content = [
            box for box in content_boxes
            if _rect_center_inside(box, rect) and not _same_rectish(box, rect)
        ]
        content_count = len(local_content)
        coverage = _dom_region_grid_coverage(local_content, rect, cols=12, rows=8)
        panel_area = max(1.0, float(rect["w"] * rect["h"]))
        panel_area_ratio = round(panel_area / canvas_area, 4)
        content_area_ratio = round(
            min(1.0, sum(_overlap_area(box, rect) for box in local_content) / panel_area),
            4,
        )
        lower_band = {
            "x": rect["x"],
            "y": rect["y"] + rect["h"] * 0.62,
            "w": rect["w"],
            "h": rect["h"] * 0.38,
        }
        lower_band_coverage = _dom_region_grid_coverage(local_content, lower_band, cols=12, rows=4)
        internal_blank_band = _dom_panel_internal_blank_band(local_content, rect, rows=24)
        local_word_count = sum(
            _dom_word_count(str(el.get("text") or ""))
            for el in local_elements
            if _is_text_like_dom_element(el)
        )
        local_visual_table_count = sum(
            1 for el in local_elements
            if str(el.get("kind") or "").lower() in _VISUAL_KINDS
            or str(el.get("tag") or "").lower() in {"img", "table"}
        )
        local_visual_table_area_ratio = _dom_panel_visual_table_area_ratio(local_elements, rect, frame=frame)
        native_unit_count = _dom_panel_native_unit_count(local_elements, frame=frame)
        content_types = _dom_panel_content_types(local_elements, frame=frame)
        earned_area = _dom_panel_earned_area_record(
            panel_area_ratio=panel_area_ratio,
            large_panel=large_panel,
            local_word_count=local_word_count,
            local_visual_table_count=local_visual_table_count,
            local_visual_table_area_ratio=local_visual_table_area_ratio,
            native_unit_count=native_unit_count,
            content_types=content_types,
            lower_band_coverage=lower_band_coverage,
            internal_blank_band=internal_blank_band,
        )
        earned_scores.append(float(earned_area.get("earned_area_score") or 1.0))
        min_content_type_count = 2 if large_panel else 1
        min_coverage = 0.36 if rect["h"] >= ch * 0.12 and rect["w"] >= cw * 0.22 else 0.30
        min_area_ratio = 0.14 if rect["h"] >= ch * 0.12 and rect["w"] >= cw * 0.22 else 0.10
        min_lower_band = 0.12 if rect["h"] >= ch * 0.14 and rect["w"] >= cw * 0.24 else 0.06
        min_local_words = 45 if rect["h"] >= ch * 0.12 and rect["w"] >= cw * 0.22 else 24
        area_sparse = content_area_ratio < min_area_ratio and not (
            local_word_count >= 75 and coverage >= min_coverage * 0.85
        )
        lower_band_sparse = lower_band_coverage < min_lower_band and not (
            local_word_count >= 120 and content_area_ratio >= min_area_ratio
        )
        internal_blank_sparse = large_panel and (
            internal_blank_band["max_blank_run_ratio"] >= 0.28
            or (
                internal_blank_band["lower_half_content_coverage"] < 0.22
                and internal_blank_band["lower_third_content_coverage"] < 0.16
            )
        )
        low_information_sparse = (
            local_visual_table_count <= 0
            and local_word_count < min_local_words
            and rect["w"] * rect["h"] >= float(cw * ch) * 0.025
        )
        content_type_sparse = large_panel and len(content_types) < min_content_type_count
        sparse_reasons: list[str] = []
        if content_count < 2:
            sparse_reasons.append("too_few_content_boxes")
        if coverage < min_coverage:
            sparse_reasons.append("low_grid_coverage")
        if area_sparse:
            sparse_reasons.append("low_content_area")
        if lower_band_sparse:
            sparse_reasons.append("low_lower_band_fill")
        if internal_blank_sparse:
            sparse_reasons.append("internal_blank_band")
        if low_information_sparse:
            sparse_reasons.append("low_local_information")
        if content_type_sparse:
            sparse_reasons.append("low_content_type_diversity")
        if earned_area.get("earned_area_failed"):
            sparse_reasons.append("earned_area_low")
        severity = "P0" if hard and large_panel and (
            internal_blank_sparse
            or content_type_sparse
            or earned_area.get("severity") == "P0"
            or (coverage < min_coverage * 0.82)
            or (content_area_ratio < min_area_ratio * 0.82)
        ) else "P1"
        record = {
            "label": panel["label"],
            "role": panel.get("role", ""),
            "rect": _bbox_from_rect(rect),
            "large_panel": large_panel,
            "panel_area_ratio": panel_area_ratio,
            "content_box_count": content_count,
            "content_coverage": coverage,
            "min_content_coverage": min_coverage,
            "content_area_ratio": content_area_ratio,
            "min_content_area_ratio": min_area_ratio,
            "lower_band_content_coverage": lower_band_coverage,
            "min_lower_band_content_coverage": min_lower_band,
            "internal_blank_band": internal_blank_band,
            "local_word_count": local_word_count,
            "min_local_word_count_without_visual": min_local_words,
            "local_visual_table_count": local_visual_table_count,
            "local_visual_table_area_ratio": local_visual_table_area_ratio,
            "native_unit_count": native_unit_count,
            "earned_area": earned_area,
            "content_types": sorted(content_types),
            "content_type_count": len(content_types),
            "min_content_type_count": min_content_type_count,
            "sparse_reasons": sparse_reasons,
            "severity": severity,
        }
        checked.append(record)
        if sparse_reasons:
            sparse.append(record)
        if internal_blank_sparse:
            blank_band_panels.append(record)
        if content_type_sparse:
            content_type_fail_panels.append(record)
        if earned_area.get("earned_area_failed"):
            earned_area_fail_panels.append(record)
    metrics = {
        "panel_fill_checked_count": len(checked),
        "panel_underfilled_count": len(sparse),
        "panel_underfilled_p0_count": sum(1 for item in sparse if item.get("severity") == "P0"),
        "panel_min_content_coverage": min((item["content_coverage"] for item in checked), default=0.0),
        "panel_avg_content_coverage": round(
            sum(float(item["content_coverage"]) for item in checked) / max(1, len(checked)),
            4,
        ),
        "panel_underfilled_sample": sparse[:8],
        "panel_internal_underfilled_count": len(sparse),
        "panel_internal_underfilled_p0_count": sum(1 for item in sparse if item.get("severity") == "P0"),
        "panel_internal_word_budget_fail_count": sum(
            1 for item in checked if "low_local_information" in item.get("sparse_reasons", [])
        ),
        "panel_internal_native_unit_fail_count": len(content_type_fail_panels),
        "panel_internal_min_coverage": min((item["content_coverage"] for item in checked), default=0.0),
        "panel_internal_avg_coverage": round(
            sum(float(item["content_coverage"]) for item in checked) / max(1, len(checked)),
            4,
        ),
        "panel_internal_max_blank_run_ratio": max(
            (float((item.get("internal_blank_band") or {}).get("max_blank_run_ratio") or 0.0) for item in checked),
            default=0.0,
        ),
        "panel_internal_blank_band_count": len(blank_band_panels),
        "panel_internal_blank_band_max_ratio": max(
            (float((item.get("internal_blank_band") or {}).get("max_blank_run_ratio") or 0.0) for item in blank_band_panels),
            default=0.0,
        ),
        "panel_content_type_fail_count": len(content_type_fail_panels),
        "panel_content_type_fail_sample": content_type_fail_panels[:8],
        "panel_earned_area_fail_count": len(earned_area_fail_panels),
        "panel_earned_area_p0_count": sum(1 for item in earned_area_fail_panels if item.get("severity") == "P0"),
        "panel_earned_area_min_score": round(min(earned_scores or [1.0]), 4),
        "panel_earned_area_sample": earned_area_fail_panels[:8],
    }
    findings: list[dict[str, Any]] = []
    if sparse:
        findings.append(_finding(
            "P0" if hard and any(item.get("severity") == "P0" for item in sparse) else "P1",
            "paper-poster-panel-underfilled",
            "One or more large poster panels have visibly sparse interiors.",
            (
                "Fill each sparse panel with PDF-supported local explanation, "
                "source readouts, compact native tables/rows, method steps, "
                "limitations, or takeaways; do not leave empty cream space."
            ),
            repair_route="revise_authored_html",
            evidence={"underfilled_panels": sparse[:8], "checked_panel_count": len(checked)},
        ))
    if blank_band_panels:
        findings.append(_finding(
            "P0" if hard else "P1",
            "paper-poster-panel-internal-blank-band",
            "A large poster panel has a blank lower/interior band despite occupying a large bbox.",
            "Refill or split the panel so its lower/interior region carries source-backed notes, result rows, table cells, local readouts, limitations, or takeaways.",
            repair_route="revise_authored_html",
            evidence={"blank_band_panels": blank_band_panels[:8], "checked_panel_count": len(checked)},
        ))
    if content_type_fail_panels:
        findings.append(_finding(
            "P0" if hard else "P1",
            "paper-poster-panel-content-types-low",
            "A large poster panel uses too few real content types.",
            "Each large panel should contain at least two of source-backed evidence, visual/figure, native table/result structure, and substantive text.",
            repair_route="revise_authored_html",
            evidence={"panels": content_type_fail_panels[:8], "checked_panel_count": len(checked)},
        ))
    if earned_area_fail_panels:
        findings.append(_finding(
            "P0" if hard and any(item.get("severity") == "P0" for item in earned_area_fail_panels) else "P1",
            "paper-poster-panel-earned-area-low",
            "A large poster panel does not earn back its canvas area with enough real information.",
            (
                "Shrink, split, or fill the panel with source-backed prose, effective source visuals, "
                "native benchmark/table rows, method labels, figure-reading notes, or synthesis takeaways. "
                "Do not rely on pale boxes, repeated short cards, or a small floating figure to satisfy density."
            ),
            repair_route="revise_authored_html",
            evidence={"panels": earned_area_fail_panels[:8], "checked_panel_count": len(checked)},
        ))
    return metrics, findings


def _dom_editorial_layout_findings(
    elements: list[dict[str, Any]],
    *,
    cw: int,
    ch: int,
    hard: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    header = _first_editorial_element(elements, ("poster-header", "identity_header", "poster_header"))
    columns_root = _first_editorial_element(elements, ("poster-columns", "poster_columns", "columns"))
    metrics: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []
    severity = "P0" if hard else "P1"

    if header and columns_root:
        header_rect = _rect(header.get("rect"))
        columns_rect = _rect(columns_root.get("rect"))
        gap = round(float(columns_rect["y"] - (header_rect["y"] + header_rect["h"])), 2)
        metrics["editorial_header_body_gap_px"] = gap
        if gap < 8:
            findings.append(_finding(
                "P0" if hard or gap < 0 else "P1",
                "paper-poster-header-body-overlap",
                "Poster heading is too close to or overlapping the body columns.",
                "Keep the canvas fixed and move the body grid below the header; do not solve this by globally compressing all panels.",
                block_id=str(header.get("block_id") or ""),
                repair_route="revise_authored_html",
                evidence={"gap_px": gap},
            ))

    columns = [
        el for el in elements
        if _editorial_haystack(el).find("poster-column") >= 0
        or str(el.get("kind") or "").lower() == "column"
    ]
    if not columns and columns_root:
        columns_root_rect = _rect(columns_root.get("rect"))
        columns = [
            el for el in elements
            if _rect_center_inside(_rect(el.get("rect")), columns_root_rect)
            and _rect(el.get("rect"))["w"] >= max(1.0, float(cw) * 0.22)
            and _rect(el.get("rect"))["h"] >= max(1.0, float(ch) * 0.25)
        ][:3]

    underfilled: list[dict[str, Any]] = []
    out_of_canvas: list[dict[str, Any]] = []
    section_count_by_column: list[dict[str, Any]] = []
    sections = [
        el for el in elements
        if "poster-section" in _editorial_haystack(el)
        or str(el.get("kind") or "").lower() in {"panel", "section"}
    ]
    for column in columns[:4]:
        column_rect = _rect(column.get("rect"))
        local_sections = [
            section for section in sections
            if _rect_center_inside(_rect(section.get("rect")), column_rect)
            and not _same_rectish(_rect(section.get("rect")), column_rect)
        ]
        section_count_by_column.append({
            "column_block_id": str(column.get("block_id") or ""),
            "section_count": len(local_sections),
        })
        if not local_sections:
            continue
        for section in local_sections:
            section_rect = _rect(section.get("rect"))
            bottom = float(section_rect["y"] + section_rect["h"])
            if bottom > float(ch) + 6 or float(section_rect["y"]) >= float(ch):
                out_of_canvas.append({
                    "column_block_id": str(column.get("block_id") or ""),
                    "section_block_id": str(section.get("block_id") or ""),
                    "overflow_px": round(max(0.0, bottom - float(ch)), 2),
                })
        last_bottom = max(float(_rect(section.get("rect"))["y"] + _rect(section.get("rect"))["h"]) for section in local_sections)
        bottom_gap = round(float(ch) - last_bottom, 2)
        if bottom_gap > max(150.0, float(ch) * 0.12):
            underfilled.append({
                "column_block_id": str(column.get("block_id") or ""),
                "bottom_gap_px": bottom_gap,
                "last_section_block_id": str(max(local_sections, key=lambda section: float(_rect(section.get("rect"))["y"] + _rect(section.get("rect"))["h"])).get("block_id") or ""),
            })

    metrics["editorial_column_count"] = len(columns)
    metrics["editorial_section_count_by_column"] = section_count_by_column
    metrics["editorial_column_underfill_count"] = len(underfilled)
    metrics["editorial_section_out_of_canvas_count"] = len(out_of_canvas)
    metrics["editorial_column_underfill_sample"] = underfilled[:4]
    metrics["editorial_section_out_of_canvas_sample"] = out_of_canvas[:4]

    if out_of_canvas:
        findings.append(_finding(
            "P0",
            "paper-poster-editorial-section-out-of-canvas",
            "A column section extends outside the fixed poster canvas.",
            "Shorten text or reduce max-height in earlier local sections of the same column to make room; keep the global canvas unchanged.",
            block_id=str(out_of_canvas[0].get("section_block_id") or ""),
            repair_route="revise_authored_html",
            evidence={"issues": out_of_canvas[:6]},
        ))
    if underfilled:
        findings.append(_finding(
            severity,
            "paper-poster-editorial-column-underfilled",
            "A poster column leaves a large unused lower band.",
            "Expand existing source-backed flow content or add one concise source-backed panel in that column; do not globally shrink the poster.",
            block_id=str(underfilled[0].get("column_block_id") or ""),
            repair_route="revise_authored_html",
            evidence={"issues": underfilled[:6]},
        ))
    return metrics, findings


def _first_editorial_element(elements: list[dict[str, Any]], needles: tuple[str, ...]) -> dict[str, Any] | None:
    for el in elements:
        haystack = _editorial_haystack(el)
        if any(needle in haystack for needle in needles):
            return el
    return None


def _editorial_haystack(el: dict[str, Any]) -> str:
    return " ".join(
        str(el.get(key) or "")
        for key in ("block_id", "role", "kind", "class_name", "panel_id")
    ).lower()


def _dom_template_boxiness_findings(
    elements: list[dict[str, Any]],
    *,
    cw: int,
    ch: int,
    hard: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    canvas_area = max(1.0, float(cw * ch))
    text_units = [
        el for el in elements
        if _is_text_like_dom_element(el)
        and _dom_word_count(str(el.get("text") or "")) >= 3
        and not _is_dom_panel_candidate(el, canvas_area=canvas_area, cw=cw, ch=ch)
    ]
    boxy_units = [el for el in text_units if _is_boxy_text_unit(el, canvas_area=canvas_area)]
    micro_box_units = [
        el for el in boxy_units
        if _rect(el.get("rect"))["w"] * _rect(el.get("rect"))["h"] <= canvas_area * 0.018
    ]
    size_bins: set[tuple[int, int]] = set()
    for el in boxy_units:
        rect = _rect(el.get("rect"))
        size_bins.add((int(round(rect["w"] / 24.0)), int(round(rect["h"] / 16.0))))
    ratio = round(len(boxy_units) / max(1, len(text_units)), 4)
    too_boxy = (
        len(boxy_units) >= 10 and ratio >= 0.35
    ) or (
        len(micro_box_units) >= 8 and len(size_bins) <= max(6, len(boxy_units) // 2)
    )
    pressure_reasons: list[str] = []
    if too_boxy:
        pressure_reasons.append("template_boxiness_high")
    if len(boxy_units) >= 10 and ratio >= 0.12:
        pressure_reasons.append("many_boxy_text_units")
    if len(micro_box_units) >= 8 and ratio >= 0.10:
        pressure_reasons.append("many_micro_boxy_text_units")
    sample = [
        {
            "label": str(el.get("block_id") or el.get("role") or "")[:120],
            "role": str(el.get("role") or "")[:120],
            "class_name": str(el.get("class_name") or "")[:160],
            "rect": _bbox_from_rect(_rect(el.get("rect"))),
            "word_count": _dom_word_count(str(el.get("text") or "")),
        }
        for el in boxy_units[:12]
    ]
    metrics = {
        "boxy_text_unit_count": len(boxy_units),
        "text_unit_count": len(text_units),
        "boxy_text_unit_ratio": ratio,
        "micro_boxy_text_unit_count": len(micro_box_units),
        "boxy_text_size_bin_count": len(size_bins),
        "template_boxiness_high": too_boxy,
        "template_boxiness_pressure": bool(pressure_reasons),
        "template_boxiness_pressure_reasons": pressure_reasons,
        "boxy_text_unit_sample": sample,
    }
    if not too_boxy:
        return metrics, []
    return metrics, [_finding(
        "P0" if hard else "P1",
        "paper-poster-template-boxiness-high",
        "Poster overuses small regular bordered/card text boxes.",
        (
            "Replace repeated mini-card grids with natural academic poster "
            "composition: continuous panel prose, inline emphasis, annotated "
            "figures, compact native tables, and thin separators. Keep borders "
            "for panel frames, tables, and a few high-value callouts only."
        ),
        repair_route="revise_authored_html",
        evidence=metrics,
    )]


def _augment_preview_pixel_audit(
    dom_audit: dict[str, Any],
    preview_path: Path,
    *,
    cw: int,
    ch: int,
    hard: bool,
    ctx: Any | None = None,
) -> None:
    metrics = dom_audit.setdefault("paper_poster_dom_metrics", {})
    findings = dom_audit.setdefault("paper_poster_dom_findings", [])
    preview_metrics, preview_findings = _preview_pixel_audit_findings(
        preview_path,
        dom_audit=dom_audit,
        cw=cw,
        ch=ch,
        hard=hard,
        ctx=ctx,
    )
    if preview_metrics:
        metrics.update(preview_metrics)
    if preview_findings:
        dom_audit.setdefault("paper_poster_preview_quality_findings", []).extend(preview_findings)
        findings.extend(preview_findings)
    dom_audit["paper_poster_dom_p0_count"] = sum(
        1 for finding in findings if finding.get("severity") == "P0"
    )


def _preview_pixel_audit_findings(
    preview_path: Path,
    *,
    dom_audit: dict[str, Any],
    cw: int,
    ch: int,
    hard: bool,
    ctx: Any | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not preview_path.exists():
        return {}, []
    try:
        with Image.open(preview_path) as src:
            img = src.convert("RGB")
    except Exception as exc:
        return {"preview_pixel_audit_error": f"{type(exc).__name__}: {exc}"}, []

    metrics: dict[str, Any] = {}
    existing_dom_metrics = dom_audit.get("paper_poster_dom_metrics")
    if isinstance(existing_dom_metrics, dict):
        for key in (
            "panel_internal_min_coverage",
            "panel_internal_avg_coverage",
            "panel_internal_underfilled_count",
            "panel_internal_underfilled_p0_count",
        ):
            if key in existing_dom_metrics:
                metrics[key] = existing_dom_metrics[key]
    findings: list[dict[str, Any]] = []
    full_stats = _preview_ink_grid_stats(img)
    lower_half_stats = _preview_ink_grid_stats(img.crop((0, img.height // 2, img.width, img.height)))
    lower_quarter_stats = _preview_ink_grid_stats(
        img.crop((0, int(round(img.height * 0.75)), img.width, img.height))
    )
    metrics.update({
        "preview_pixel_ink_ratio": full_stats["ink_ratio"],
        "preview_pixel_grid_coverage": full_stats["grid_coverage"],
        "preview_pixel_max_blank_run_ratio": full_stats["longest_blank_row_run_ratio"],
        "preview_lower_half_ink_ratio": lower_half_stats["ink_ratio"],
        "preview_lower_half_grid_coverage": lower_half_stats["grid_coverage"],
        "preview_lower_quarter_ink_ratio": lower_quarter_stats["ink_ratio"],
        "preview_lower_quarter_grid_coverage": lower_quarter_stats["grid_coverage"],
    })
    density_metrics, density_findings = _preview_density_floor_findings(
        img,
        dom_audit=dom_audit,
        ctx=ctx,
        hard=hard,
    )
    metrics.update(density_metrics)
    findings.extend(density_findings)
    if hard and (
        full_stats["longest_blank_row_run_ratio"] >= 0.34
        or (
            lower_half_stats["grid_coverage"] < 0.12
            and lower_quarter_stats["grid_coverage"] < 0.08
            and lower_quarter_stats["ink_ratio"] < 0.055
        )
    ):
        findings.append(_finding(
            "P0",
            "paper-poster-preview-bottom-band-underfilled",
            "Rendered preview has an extreme blank or low-ink lower band.",
            "Fill the lower body with source-backed panels, native result rows, limitations, local readouts, or takeaways before finalizing.",
            repair_route="revise_authored_html",
            evidence={
                "full": full_stats,
                "lower_half": lower_half_stats,
                "lower_quarter": lower_quarter_stats,
            },
        ))

    panel_metrics, panel_findings = _preview_panel_visual_findings(
        img,
        dom_audit=dom_audit,
        cw=cw,
        ch=ch,
        hard=hard,
    )
    metrics.update(panel_metrics)
    findings.extend(panel_findings)

    bbox_evidence = _bbox_filled_but_low_ink_online_evidence(metrics)
    if bbox_evidence:
        findings.append(_finding(
            "P0" if hard else "P1",
            "paper-poster-bbox-filled-but-low-ink",
            "DOM panel bboxes look filled, but rendered pixels show low ink or sparse visual coverage.",
            "Replace blank/pale panel interiors with readable source figures, native tables, result rows, and substantive local text.",
            repair_route="revise_authored_html",
            evidence=bbox_evidence,
        ))
    return metrics, findings


def _preview_density_floor_findings(
    img: Image.Image,
    *,
    dom_audit: dict[str, Any],
    ctx: Any | None,
    hard: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    stats = _preview_information_density_stats(img)
    contract = _poster_plan_contract_from_ctx(ctx)
    profile = str(contract.get("reference_profile") or "default")
    floors = _preview_density_floors(profile, contract)
    metrics = {
        "preview_density_reference_profile": profile,
        "preview_density_nonwhite_ratio": stats["nonwhite_ratio"],
        "preview_density_dark_ink_ratio": stats["dark_ink_ratio"],
        "preview_density_edge_ratio": stats["edge_density"],
        "preview_density_vertical_band_min": stats["vertical_band_min"],
        "preview_density_vertical_band_ratios": stats["vertical_band_ratios"],
        "preview_density_min_nonwhite_ratio": floors["min_nonwhite_ratio"],
        "preview_density_min_vertical_band_ratio": floors["min_vertical_band_ratio"],
        "preview_density_min_dark_ink_ratio": floors["min_dark_ink_ratio"],
    }
    reasons: list[str] = []
    if stats["nonwhite_ratio"] < floors["min_nonwhite_ratio"]:
        reasons.append("low_nonwhite_density")
    if stats["vertical_band_min"] < floors["min_vertical_band_ratio"]:
        reasons.append("low_vertical_band_occupancy")
    if stats["dark_ink_ratio"] < floors["min_dark_ink_ratio"]:
        reasons.append("low_dark_ink")
    metrics["preview_density_floor_fail_reasons"] = reasons
    if not hard or not reasons:
        return metrics, []
    severe = (
        (
            stats["nonwhite_ratio"] < floors["min_nonwhite_ratio"]
            and stats["vertical_band_min"] < floors["min_vertical_band_ratio"]
        )
        or stats["vertical_band_min"] < floors["min_vertical_band_ratio"] * 0.86
        or stats["nonwhite_ratio"] < floors["min_nonwhite_ratio"] * 0.82
    )
    if not severe:
        return metrics, []
    dom_metrics = dom_audit.get("paper_poster_dom_metrics")
    if not isinstance(dom_metrics, dict):
        dom_metrics = {}
    return metrics, [_finding(
        "P0",
        "paper-poster-preview-density-floor-low",
        "Rendered preview is too sparse for the active poster profile even though DOM boxes occupy the canvas.",
        (
            "Increase real information density with readable native text hierarchy, "
            "source-backed tables, result rows, figure/readout pairs, or panel-local "
            "evidence. Do not pass layout repair by spreading content into pale empty boxes."
        ),
        repair_route="revise_authored_html",
        evidence={
            **metrics,
            "dom_panel_internal_avg_coverage": dom_metrics.get("panel_internal_avg_coverage"),
            "dom_overall_content_coverage": dom_metrics.get("overall_content_coverage"),
        },
    )]


def _preview_information_density_stats(img: Image.Image) -> dict[str, Any]:
    small = img.convert("RGB")
    small.thumbnail((640, 640), Image.Resampling.LANCZOS)
    width, height = small.size
    pixels = list(small.getdata())
    if not pixels or width <= 0 or height <= 0:
        return {
            "nonwhite_ratio": 0.0,
            "dark_ink_ratio": 0.0,
            "edge_density": 0.0,
            "vertical_band_min": 0.0,
            "vertical_band_ratios": [],
        }
    nonwhite_count = 0
    dark_count = 0
    row_nonwhite = [0 for _ in range(height)]
    for idx, (r, g, b) in enumerate(pixels):
        luma = _pixel_luma(int(r), int(g), int(b))
        sat = _pixel_saturation(int(r), int(g), int(b))
        if luma < 245 or sat > 0.08:
            nonwhite_count += 1
            row_nonwhite[min(height - 1, idx // max(1, width))] += 1
        if luma < 185:
            dark_count += 1
    row_ratios = [count / float(max(1, width)) for count in row_nonwhite]
    band_ratios: list[float] = []
    band_count = 10
    for band in range(band_count):
        start = int(round(band * len(row_ratios) / band_count))
        end = int(round((band + 1) * len(row_ratios) / band_count))
        segment = row_ratios[start:max(start + 1, end)]
        band_ratios.append(round(sum(segment) / max(1, len(segment)), 4))
    edges = small.convert("L").filter(ImageFilter.FIND_EDGES)
    edge_pixels = list(edges.getdata())
    edge_density = sum(1 for value in edge_pixels if int(value) > 28) / float(max(1, len(edge_pixels)))
    total = float(max(1, len(pixels)))
    return {
        "nonwhite_ratio": round(nonwhite_count / total, 4),
        "dark_ink_ratio": round(dark_count / total, 4),
        "edge_density": round(edge_density, 4),
        "vertical_band_min": round(min(band_ratios or [0.0]), 4),
        "vertical_band_ratios": band_ratios,
    }


def _poster_plan_contract_from_ctx(ctx: Any | None) -> dict[str, Any]:
    state = getattr(ctx, "state", None)
    if not isinstance(state, dict):
        return {}
    contract = state.get("poster_plan_contract")
    return contract if isinstance(contract, dict) else {}


def _preview_density_floors(profile: str, contract: dict[str, Any]) -> dict[str, float]:
    if profile == "research_synthesis_dense":
        base_nonwhite = 0.29
        base_band = 0.18
        base_dark = 0.055
    elif profile == "visual_evidence_wall":
        base_nonwhite = 0.24
        base_band = 0.16
        base_dark = 0.045
    else:
        base_nonwhite = 0.22
        base_band = 0.14
        base_dark = 0.04
    content_targets = contract.get("content_fill_targets")
    if isinstance(content_targets, dict):
        min_fill = _float_value(content_targets.get("min_effective_content_fill_ratio"), 0.0)
        if min_fill >= 0.76:
            base_nonwhite = max(base_nonwhite, 0.28)
            base_band = max(base_band, 0.17)
    return {
        "min_nonwhite_ratio": round(base_nonwhite, 4),
        "min_vertical_band_ratio": round(base_band, 4),
        "min_dark_ink_ratio": round(base_dark, 4),
    }


def _preview_panel_visual_findings(
    img: Image.Image,
    *,
    dom_audit: dict[str, Any],
    cw: int,
    ch: int,
    hard: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    layers = [layer for layer in (dom_audit.get("dom_layers") or []) if isinstance(layer, dict)]
    if not layers:
        return {}, []
    img_w, img_h = img.size
    canvas_area = max(1.0, float(cw * ch))
    scale_x = img_w / float(max(1, cw))
    scale_y = img_h / float(max(1, ch))
    panels = [
        layer for layer in layers
        if _is_preview_panel_candidate(layer, canvas_area=canvas_area, cw=cw, ch=ch)
    ]
    samples: list[dict[str, Any]] = []
    ink_ratios: list[float] = []
    coverages: list[float] = []
    blank_runs: list[float] = []
    for panel in panels:
        bbox = panel.get("bbox") if isinstance(panel.get("bbox"), dict) else {}
        x = int(round(_float_value(bbox.get("x"), 0.0) * scale_x))
        y = int(round(_float_value(bbox.get("y"), 0.0) * scale_y))
        w = int(round(_float_value(bbox.get("w"), 0.0) * scale_x))
        h = int(round(_float_value(bbox.get("h"), 0.0) * scale_y))
        if w < 24 or h < 24:
            continue
        x1 = max(0, min(img_w - 1, x))
        y1 = max(0, min(img_h - 1, y))
        x2 = max(x1 + 1, min(img_w, x + w))
        y2 = max(y1 + 1, min(img_h, y + h))
        stats = _preview_ink_grid_stats(img.crop((x1, y1, x2, y2)))
        ink_ratios.append(stats["ink_ratio"])
        coverages.append(stats["grid_coverage"])
        blank_runs.append(stats["longest_blank_row_run_ratio"])
        panel_area_ratio = (
            _float_value(bbox.get("w"), 0.0)
            * _float_value(bbox.get("h"), 0.0)
            / canvas_area
        )
        large_panel = panel_area_ratio >= (0.045 if ch > cw else 0.065)
        min_ink = 0.16 if large_panel else 0.095
        min_coverage = 0.42 if large_panel else 0.26
        max_blank = 0.42 if large_panel else 0.55
        reasons: list[str] = []
        if stats["ink_ratio"] < min_ink:
            reasons.append("low_visual_ink")
        if stats["grid_coverage"] < min_coverage:
            reasons.append("low_visual_grid_coverage")
        if stats["longest_blank_row_run_ratio"] > max_blank:
            reasons.append("long_visual_blank_band")
        if not reasons:
            continue
        severe_large = large_panel and (
            stats["ink_ratio"] < min_ink * 0.88
            or stats["grid_coverage"] < min_coverage * 0.82
            or stats["longest_blank_row_run_ratio"] > max_blank
        )
        samples.append({
            "block_id": panel.get("layer_id"),
            "role": panel.get("role"),
            "class_name": panel.get("class_name"),
            "bbox": bbox,
            "panel_area_ratio": round(panel_area_ratio, 4),
            "large_panel": large_panel,
            "visual_ink_ratio": stats["ink_ratio"],
            "visual_grid_coverage": stats["grid_coverage"],
            "longest_visual_blank_row_run_ratio": stats["longest_blank_row_run_ratio"],
            "min_visual_ink_ratio": min_ink,
            "min_visual_grid_coverage": min_coverage,
            "max_visual_blank_row_run_ratio": max_blank,
            "reasons": reasons,
            "severity": "P0" if hard and severe_large else "P1",
        })
    p0_count = sum(1 for sample in samples if sample.get("severity") == "P0")
    metrics = {
        "panel_visual_audited_count": len(panels),
        "panel_visual_underfilled_count": len(samples),
        "panel_visual_underfilled_p0_count": p0_count,
        "panel_visual_min_ink_ratio": round(min(ink_ratios or [1.0]), 4),
        "panel_visual_avg_ink_ratio": round(sum(ink_ratios) / max(1, len(ink_ratios)), 4),
        "panel_visual_min_grid_coverage": round(min(coverages or [1.0]), 4),
        "panel_visual_max_blank_run_ratio": round(max(blank_runs or [0.0]), 4),
        "panel_visual_underfilled_samples": samples[:8],
    }
    if not samples:
        return metrics, []
    return metrics, [_finding(
        "P0" if hard and p0_count else "P1",
        "paper-poster-panel-visual-underfilled",
        "One or more large panel bboxes are visually underfilled in the rendered preview.",
        "A filled DOM box must contain readable ink: enlarge/crop source visuals, add native tables, or fill blank panel areas with substantive source-backed text.",
        repair_route="revise_authored_html",
        evidence={**metrics, "samples": samples[:8]},
    )]


def _preview_ink_grid_stats(img: Image.Image) -> dict[str, float]:
    small = img.copy()
    small.thumbnail((360, 360), Image.Resampling.LANCZOS)
    width, height = small.size
    pixels = list(small.getdata())
    if not pixels or width <= 0 or height <= 0:
        return {"ink_ratio": 0.0, "grid_coverage": 0.0, "longest_blank_row_run_ratio": 1.0}
    ink_mask: list[bool] = []
    for r, g, b in pixels:
        luma = _pixel_luma(int(r), int(g), int(b))
        sat = _pixel_saturation(int(r), int(g), int(b))
        ink_mask.append(luma < 236 or sat > 0.14)
    ink_ratio = sum(1 for value in ink_mask if value) / max(1, len(ink_mask))
    rows = 12
    cols = 12
    marked = 0
    longest_blank = 0
    current_blank = 0
    for row in range(rows):
        row_marked = False
        y0 = int(round(row * height / rows))
        y1 = int(round((row + 1) * height / rows))
        for col in range(cols):
            x0 = int(round(col * width / cols))
            x1 = int(round((col + 1) * width / cols))
            total = 0
            ink = 0
            for yy in range(y0, max(y0 + 1, y1)):
                base = yy * width
                for xx in range(x0, max(x0 + 1, x1)):
                    idx = min(len(ink_mask) - 1, base + min(width - 1, xx))
                    total += 1
                    ink += 1 if ink_mask[idx] else 0
            if total and ink / total >= 0.025:
                marked += 1
                row_marked = True
        if row_marked:
            current_blank = 0
        else:
            current_blank += 1
            longest_blank = max(longest_blank, current_blank)
    return {
        "ink_ratio": round(ink_ratio, 4),
        "grid_coverage": round(marked / float(max(1, rows * cols)), 4),
        "longest_blank_row_run_ratio": round(longest_blank / float(max(1, rows)), 4),
    }


def _is_preview_panel_candidate(
    layer: dict[str, Any],
    *,
    canvas_area: float,
    cw: int,
    ch: int,
) -> bool:
    bbox = layer.get("bbox") if isinstance(layer.get("bbox"), dict) else {}
    area = _float_value(bbox.get("w"), 0.0) * _float_value(bbox.get("h"), 0.0)
    if area < canvas_area * 0.018:
        return False
    kind = str(layer.get("kind") or "").lower()
    if kind in {"text", "caption", "metric", "quote", "table", "image", "chart", "embed"}:
        return False
    role = str(layer.get("role") or "").lower()
    class_name = str(layer.get("class_name") or "").lower()
    layer_id = str(layer.get("layer_id") or "").lower()
    haystack = " ".join((kind, role, class_name, layer_id))
    if any(token in haystack for token in (
        "panel-head", "section-no", "badge", "metric-chip", "logo",
        "header", "title", "thesis", "footer", "citation", "provenance",
    )):
        return False
    rect = {
        "x": _float_value(bbox.get("x"), 0.0),
        "y": _float_value(bbox.get("y"), 0.0),
        "w": _float_value(bbox.get("w"), 0.0),
        "h": _float_value(bbox.get("h"), 0.0),
    }
    if any(token in haystack for token in ("title", "header", "footer", "identity", "logo", "badge", "meta")):
        if rect["y"] < ch * 0.16 or rect["y"] + rect["h"] > ch * 0.92:
            return False
    return any(token in haystack for token in (
        "panel", "hero", "evidence", "lower-grid", "method", "analysis",
        "results", "representation", "synthesis", "qual", "grid",
        "ablation", "limitation", "takeaway", "slot",
    ))


def _bbox_filled_but_low_ink_online_evidence(metrics: dict[str, Any]) -> dict[str, Any] | None:
    panel_visual_count = int(_float_value(metrics.get("panel_visual_underfilled_count"), 0.0))
    if panel_visual_count <= 0:
        return None
    panel_internal_min = _float_value(metrics.get("panel_internal_min_coverage"), 0.0)
    panel_internal_avg = _float_value(metrics.get("panel_internal_avg_coverage"), 0.0)
    visual_min_ink = _float_value(metrics.get("panel_visual_min_ink_ratio"), 1.0)
    visual_min_grid = _float_value(metrics.get("panel_visual_min_grid_coverage"), 1.0)
    visual_blank = _float_value(metrics.get("panel_visual_max_blank_run_ratio"), 0.0)
    dom_coverage_high = panel_internal_min >= 0.58 or panel_internal_avg >= 0.70
    preview_ink_low = visual_min_ink < 0.16 or visual_min_grid < 0.42 or visual_blank > 0.42
    if not (dom_coverage_high and preview_ink_low):
        return None
    return {
        "panel_visual_underfilled_count": panel_visual_count,
        "panel_visual_underfilled_p0_count": int(_float_value(metrics.get("panel_visual_underfilled_p0_count"), 0.0)),
        "panel_internal_min_coverage": round(panel_internal_min, 4),
        "panel_internal_avg_coverage": round(panel_internal_avg, 4),
        "panel_visual_min_ink_ratio": round(visual_min_ink, 4),
        "panel_visual_min_grid_coverage": round(visual_min_grid, 4),
        "panel_visual_max_blank_run_ratio": round(visual_blank, 4),
        "samples": list(metrics.get("panel_visual_underfilled_samples") or [])[:3],
    }


def _pixel_luma(r: int, g: int, b: int) -> float:
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _pixel_saturation(r: int, g: int, b: int) -> float:
    high = max(r, g, b)
    low = min(r, g, b)
    return 0.0 if high == 0 else (high - low) / high


def _is_large_dom_panel(rect: dict[str, float], *, cw: int, ch: int) -> bool:
    return rect["h"] >= ch * 0.12 and rect["w"] >= cw * 0.22


def _dom_panel_internal_blank_band(
    local_content: list[dict[str, float]],
    rect: dict[str, float],
    *,
    rows: int,
) -> dict[str, Any]:
    if not local_content:
        return {
            "max_blank_run_ratio": 1.0,
            "blank_start_ratio": 0.0,
            "blank_end_ratio": 1.0,
            "lower_half_content_coverage": 0.0,
            "lower_third_content_coverage": 0.0,
        }
    row_h = max(1.0, float(rect["h"]) / max(1, rows))
    row_marked: list[bool] = []
    for row in range(rows):
        y0 = float(rect["y"]) + row * row_h
        y1 = float(rect["y"]) + (row + 1) * row_h
        overlap_width = 0.0
        for box in local_content:
            ih = min(float(box["y"] + box["h"]), y1) - max(float(box["y"]), y0)
            if ih <= row_h * 0.08:
                continue
            overlap_width += max(
                0.0,
                min(float(box["x"] + box["w"]), float(rect["x"] + rect["w"]))
                - max(float(box["x"]), float(rect["x"])),
            )
        row_marked.append(overlap_width >= float(rect["w"]) * 0.08)
    best_start = 0
    best_len = 0
    run_start = -1
    run_len = 0
    for idx, marked in enumerate(row_marked):
        if not marked:
            if run_start < 0:
                run_start = idx
            run_len += 1
            if run_len > best_len:
                best_start = run_start
                best_len = run_len
        else:
            run_start = -1
            run_len = 0
    lower_half = {
        "x": rect["x"],
        "y": rect["y"] + rect["h"] * 0.5,
        "w": rect["w"],
        "h": rect["h"] * 0.5,
    }
    lower_third = {
        "x": rect["x"],
        "y": rect["y"] + rect["h"] * 0.66,
        "w": rect["w"],
        "h": rect["h"] * 0.34,
    }
    return {
        "max_blank_run_ratio": round(best_len / float(max(1, rows)), 4),
        "blank_start_ratio": round(best_start / float(max(1, rows)), 4) if best_len else None,
        "blank_end_ratio": round((best_start + best_len) / float(max(1, rows)), 4) if best_len else None,
        "lower_half_content_coverage": _dom_region_grid_coverage(local_content, lower_half, cols=12, rows=4),
        "lower_third_content_coverage": _dom_region_grid_coverage(local_content, lower_third, cols=12, rows=3),
    }


def _dom_panel_content_types(
    local_elements: list[dict[str, Any]],
    *,
    frame: Any | None,
) -> set[str]:
    content_types: set[str] = set()
    for el in local_elements:
        block_id = str(el.get("block_id") or "")
        block = _block_by_id(frame, block_id) if frame is not None else None
        block = block or {}
        kind = str(block.get("kind") or el.get("kind") or "").lower()
        tag = str(el.get("tag") or "").lower()
        role = str(block.get("role") or el.get("role") or "").lower()
        text = str(el.get("text") or block.get("text") or block.get("caption") or "")
        has_source_metadata = any(
            str(value or "").strip()
            for value in (
                block.get("source"),
                block.get("source_id"),
                block.get("layer_id"),
                block.get("claim_id"),
                block.get("provenance"),
            )
        )
        if kind == "table" or tag == "table" or "table" in role:
            content_types.add("table")
        elif kind in {"image", "chart", "embed"} or tag == "img":
            content_types.add("visual")
        elif _is_text_like_dom_element(el) and _dom_word_count(text) >= 8:
            content_types.add("text")
            if has_source_metadata:
                content_types.add("source")
        elif has_source_metadata and _dom_word_count(text) >= 4:
            content_types.add("source")
    return content_types


def _dom_panel_visual_table_area_ratio(
    local_elements: list[dict[str, Any]],
    rect: dict[str, float],
    *,
    frame: Any | None,
) -> float:
    panel_area = max(1.0, float(rect.get("w") or 0) * float(rect.get("h") or 0))
    area = 0.0
    for el in local_elements:
        block_id = str(el.get("block_id") or "")
        block = _block_by_id(frame, block_id) if frame is not None else None
        block = block or {}
        kind = str(block.get("kind") or el.get("kind") or "").lower()
        tag = str(el.get("tag") or "").lower()
        role = str(block.get("role") or el.get("role") or "").lower()
        if kind not in _VISUAL_KINDS and tag not in {"img", "table"} and "table" not in role:
            continue
        area += _overlap_area(_rect(el.get("rect")), rect)
    return round(min(1.0, area / panel_area), 4)


def _dom_panel_native_unit_count(
    local_elements: list[dict[str, Any]],
    *,
    frame: Any | None,
) -> int:
    units = 0
    for el in local_elements:
        block_id = str(el.get("block_id") or "")
        block = _block_by_id(frame, block_id) if frame is not None else None
        block = block or {}
        kind = str(block.get("kind") or el.get("kind") or "").lower()
        tag = str(el.get("tag") or "").lower()
        role = str(block.get("role") or el.get("role") or "").lower()
        text = str(el.get("text") or block.get("text") or block.get("caption") or "")
        if kind == "table" or tag == "table" or "table" in role:
            rows = block.get("rows") if isinstance(block.get("rows"), list) else []
            units += 2 if len(rows) >= 4 else 1
            continue
        if kind in {"chart", "image", "embed"} or tag == "img":
            units += 1
            continue
        if kind == "metric" or any(token in role for token in ("metric", "stat", "result-band")):
            units += 1
            continue
        if (
            _is_text_like_dom_element(el)
            and _dom_word_count(text) >= 8
            and any(token in role for token in ("native", "information", "result_or_metric"))
        ):
            units += 1
            continue
        if _is_text_like_dom_element(el) and _dom_word_count(text) >= 14:
            if any(token in role for token in (
                "claim", "takeaway", "caption", "callout", "step", "limitation",
                "analysis", "model", "card", "finding", "reading",
            )):
                units += 1
    return min(12, units)


def _dom_panel_earned_area_record(
    *,
    panel_area_ratio: float,
    large_panel: bool,
    local_word_count: int,
    local_visual_table_count: int,
    local_visual_table_area_ratio: float,
    native_unit_count: int,
    content_types: set[str],
    lower_band_coverage: float,
    internal_blank_band: dict[str, Any],
) -> dict[str, Any]:
    if not large_panel or panel_area_ratio < 0.055:
        return {
            "earned_area_score": 1.0,
            "earned_area_failed": False,
            "reasons": [],
        }

    has_visual_or_table = local_visual_table_count > 0
    target_words = 62 if has_visual_or_table else (180 if panel_area_ratio >= 0.08 else 145)
    target_visual_area = 0.035 if has_visual_or_table else 0.0
    target_native_units = 2 if panel_area_ratio >= 0.08 else 1

    text_score = min(1.0, local_word_count / float(max(1, target_words)))
    visual_score = 1.0 if not has_visual_or_table else min(1.0, local_visual_table_area_ratio / target_visual_area)
    native_score = min(1.0, native_unit_count / float(max(1, target_native_units)))
    lower_score = min(1.0, lower_band_coverage / (0.18 if panel_area_ratio >= 0.08 else 0.12))
    score = round(
        text_score * 0.34
        + visual_score * 0.24
        + native_score * 0.28
        + lower_score * 0.14,
        4,
    )

    earned_modes: set[str] = set()
    if local_word_count >= target_words * 0.75:
        earned_modes.add("text")
    if has_visual_or_table and local_visual_table_area_ratio >= target_visual_area * 0.75:
        earned_modes.add("effective_visual_or_table")
    if native_unit_count >= target_native_units:
        earned_modes.add("native_units")
    if {"table", "visual", "source"} & content_types:
        earned_modes.update({"source_evidence"} & (content_types | {"source_evidence"}))

    max_blank = float((internal_blank_band or {}).get("max_blank_run_ratio") or 0.0)
    reasons: list[str] = []
    if score < (0.70 if panel_area_ratio >= 0.08 else 0.58):
        reasons.append("earned_area_score_low")
    if panel_area_ratio >= 0.08 and len(earned_modes) < 2:
        reasons.append("too_few_earned_content_modes")
    if not has_visual_or_table and local_word_count < target_words:
        reasons.append("large_text_only_panel_too_thin")
    if has_visual_or_table and local_visual_table_area_ratio < target_visual_area * 0.72:
        reasons.append("visual_or_table_too_small_for_panel")
    if native_unit_count < target_native_units and panel_area_ratio >= 0.08:
        reasons.append("native_units_too_low_for_panel")
    if max_blank >= 0.24 and lower_band_coverage < 0.20:
        reasons.append("interior_blank_band_remaining")

    severe = (
        panel_area_ratio >= 0.085
        and (
            score < 0.62
            or "too_few_earned_content_modes" in reasons
            or "large_text_only_panel_too_thin" in reasons
        )
    )
    return {
        "earned_area_score": score,
        "earned_area_failed": bool(reasons),
        "severity": "P0" if severe else "P1",
        "panel_area_ratio": panel_area_ratio,
        "local_word_count": local_word_count,
        "target_word_count": target_words,
        "local_visual_table_count": local_visual_table_count,
        "local_visual_table_area_ratio": local_visual_table_area_ratio,
        "target_visual_table_area_ratio": target_visual_area,
        "native_unit_count": native_unit_count,
        "target_native_unit_count": target_native_units,
        "lower_band_content_coverage": lower_band_coverage,
        "earned_modes": sorted(earned_modes),
        "content_types": sorted(content_types),
        "reasons": reasons,
    }


def _dom_content_fill_boxes(
    elements: list[dict[str, Any]],
    *,
    cw: int,
    ch: int,
) -> list[dict[str, float]]:
    boxes: list[dict[str, float]] = []
    canvas_area = max(1.0, float(cw * ch))
    for el in elements:
        if not _is_dom_content_for_fill(el, canvas_area=canvas_area, ch=ch):
            continue
        rect = _clip_rect_to_canvas(_rect(el.get("rect")), cw=cw, ch=ch)
        if rect and rect["w"] * rect["h"] >= 120:
            boxes.append(rect)
    return boxes


def _is_dom_content_for_fill(el: dict[str, Any], *, canvas_area: float, ch: int) -> bool:
    kind = str(el.get("kind") or "").lower()
    tag = str(el.get("tag") or "").lower()
    role = str(el.get("role") or "").lower()
    class_name = str(el.get("class_name") or "").lower()
    block_id = str(el.get("block_id") or "").lower()
    haystack = " ".join((kind, tag, role, class_name, block_id))
    rect = _rect(el.get("rect"))
    if kind in {"group", "shape", "container", "grid"}:
        return False
    if tag in {"section", "article", "main", "figure", "div"} and any(
        token in haystack for token in ("panel", "grid", "wrapper", "visual-card")
    ):
        return False
    if "logo" in haystack and rect["y"] < ch * 0.18 and rect["w"] * rect["h"] < canvas_area * 0.02:
        return False
    if kind in _VISUAL_KINDS or tag in {"img", "table"}:
        return True
    return _is_text_like_dom_element(el) and _dom_word_count(str(el.get("text") or "")) >= 2


def _dom_panel_boxes(
    elements: list[dict[str, Any]],
    *,
    cw: int,
    ch: int,
) -> list[dict[str, Any]]:
    canvas_area = max(1.0, float(cw * ch))
    panels: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for el in elements:
        if not _is_dom_panel_candidate(el, canvas_area=canvas_area, cw=cw, ch=ch):
            continue
        rect = _clip_rect_to_canvas(_rect(el.get("rect")), cw=cw, ch=ch)
        if not rect:
            continue
        key = (
            int(round(rect["x"] / 8.0)),
            int(round(rect["y"] / 8.0)),
            int(round(rect["w"] / 8.0)),
            int(round(rect["h"] / 8.0)),
        )
        if key in seen:
            continue
        seen.add(key)
        panels.append({
            "label": str(el.get("block_id") or el.get("role") or el.get("class_name") or "panel")[:160],
            "role": str(el.get("role") or ""),
            "rect": rect,
        })
    return panels


def _is_dom_panel_candidate(
    el: dict[str, Any],
    *,
    canvas_area: float,
    cw: int,
    ch: int,
) -> bool:
    kind = str(el.get("kind") or "").lower()
    tag = str(el.get("tag") or "").lower()
    role = str(el.get("role") or "").lower()
    class_name = str(el.get("class_name") or "").lower()
    block_id = str(el.get("block_id") or "").lower()
    haystack = " ".join((kind, tag, role, class_name, block_id))
    rect = _rect(el.get("rect"))
    area = rect["w"] * rect["h"]
    if area < canvas_area * 0.012 or area > canvas_area * 0.55:
        return False
    if rect["w"] < max(160.0, cw * 0.10) or rect["h"] < max(120.0, ch * 0.055):
        return False
    if any(token in haystack for token in ("title", "header", "footer", "identity", "logo", "badge", "meta")):
        # Header/footer bands are intentionally sparse and should not drive the
        # main panel-fill finding.
        if rect["y"] < ch * 0.16 or rect["y"] + rect["h"] > ch * 0.92:
            return False
    if tag not in {"section", "article", "aside", "div", "figure"}:
        return False
    return any(token in haystack for token in ("panel", "slot", "evidence", "method", "result", "analysis", "limitation"))


def _dom_region_grid_coverage(
    boxes: list[dict[str, float]],
    region: dict[str, float],
    *,
    cols: int,
    rows: int,
) -> float:
    if not boxes:
        return 0.0
    x0 = float(region.get("x") or 0.0)
    y0 = float(region.get("y") or 0.0)
    width = max(1.0, float(region.get("w") or 0.0))
    height = max(1.0, float(region.get("h") or 0.0))
    cell_w = width / max(1, cols)
    cell_h = height / max(1, rows)
    marked = 0
    for row in range(rows):
        cy = y0 + row * cell_h
        for col in range(cols):
            cx = x0 + col * cell_w
            cell = {"x": cx, "y": cy, "w": cell_w, "h": cell_h}
            cell_area = max(1.0, cell_w * cell_h)
            if any(_overlap_area(box, cell) >= cell_area * 0.04 for box in boxes):
                marked += 1
    return round(marked / float(max(1, cols * rows)), 4)


def _rect_center_inside(child: dict[str, float], parent: dict[str, float]) -> bool:
    cx = float(child.get("x") or 0.0) + float(child.get("w") or 0.0) / 2.0
    cy = float(child.get("y") or 0.0) + float(child.get("h") or 0.0) / 2.0
    return (
        cx >= float(parent.get("x") or 0.0)
        and cy >= float(parent.get("y") or 0.0)
        and cx <= float(parent.get("x") or 0.0) + float(parent.get("w") or 0.0)
        and cy <= float(parent.get("y") or 0.0) + float(parent.get("h") or 0.0)
    )


def _same_rectish(a: dict[str, float], b: dict[str, float]) -> bool:
    return (
        abs(float(a.get("x") or 0.0) - float(b.get("x") or 0.0)) <= 3.0
        and abs(float(a.get("y") or 0.0) - float(b.get("y") or 0.0)) <= 3.0
        and abs(float(a.get("w") or 0.0) - float(b.get("w") or 0.0)) <= 6.0
        and abs(float(a.get("h") or 0.0) - float(b.get("h") or 0.0)) <= 6.0
    )


def _is_boxy_text_unit(el: dict[str, Any], *, canvas_area: float) -> bool:
    tag = str(el.get("tag") or "").lower()
    if tag in {"table", "th", "td", "h1", "h2"}:
        return False
    role = str(el.get("role") or "").lower()
    class_name = str(el.get("class_name") or "").lower()
    block_id = str(el.get("block_id") or "").lower()
    haystack = " ".join((role, class_name, block_id))
    if any(token in haystack for token in ("panel", "slot", "header", "footer", "caption", "section-bar")):
        return False
    rect = _rect(el.get("rect"))
    area = rect["w"] * rect["h"]
    if area <= 0 or area > canvas_area * 0.06:
        return False
    styled_box = _has_box_border(el) or _has_distinct_background(el) or _has_shadow(el)
    return styled_box


def _has_box_border(el: dict[str, Any]) -> bool:
    styles = [
        ("borderTopWidth", "borderTopStyle"),
        ("borderRightWidth", "borderRightStyle"),
        ("borderBottomWidth", "borderBottomStyle"),
        ("borderLeftWidth", "borderLeftStyle"),
    ]
    for width_key, style_key in styles:
        if _css_px(el.get(width_key)) >= 0.8 and str(el.get(style_key) or "").lower() not in {"", "none", "hidden"}:
            return True
    return False


def _has_distinct_background(el: dict[str, Any]) -> bool:
    bg = str(el.get("backgroundColor") or "").strip().lower()
    if not bg or bg == "transparent" or bg == "rgba(0, 0, 0, 0)":
        return False
    # Treat plain white/near-white text flow as unboxed; cream/tint fills still
    # count when used on many repeated text units.
    numbers = [int(v) for v in re.findall(r"\d+", bg)[:3]]
    if len(numbers) >= 3 and min(numbers) >= 248:
        return False
    return True


def _has_shadow(el: dict[str, Any]) -> bool:
    shadow = str(el.get("boxShadow") or "").strip().lower()
    return bool(shadow and shadow != "none")


def _css_px(value: Any) -> float:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    if not match:
        return 0.0
    try:
        return float(match.group(0))
    except ValueError:
        return 0.0


def _clip_rect_to_canvas(rect: dict[str, float], *, cw: int, ch: int) -> dict[str, float] | None:
    x1 = max(0.0, float(rect.get("x") or 0.0))
    y1 = max(0.0, float(rect.get("y") or 0.0))
    x2 = min(float(cw), float(rect.get("x") or 0.0) + float(rect.get("w") or 0.0))
    y2 = min(float(ch), float(rect.get("y") or 0.0) + float(rect.get("h") or 0.0))
    if x2 <= x1 or y2 <= y1:
        return None
    return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}


def _dom_band_grid_coverage(
    boxes: list[dict[str, float]],
    *,
    cw: int,
    ch: int,
    y0: float,
    y1: float,
) -> float:
    band_top = max(0.0, min(float(ch), y0))
    band_bottom = max(band_top + 1.0, min(float(ch), y1))
    cols = 48
    rows = 12
    cell_w = float(cw) / cols
    cell_h = (band_bottom - band_top) / rows
    marked = 0
    for row in range(rows):
        cy = band_top + row * cell_h
        for col in range(cols):
            cx = col * cell_w
            cell = {"x": cx, "y": cy, "w": cell_w, "h": cell_h}
            cell_area = max(1.0, cell_w * cell_h)
            if any(_overlap_area(box, cell) >= cell_area * 0.03 for box in boxes):
                marked += 1
    return round(marked / float(max(1, cols * rows)), 4)


def _sanitized_payload(sanitized: _SanitizedHtml) -> dict[str, Any]:
    return {
        "authored_html_sanitizer_findings": sanitized.findings,
        "authored_html_sanitizer_p0_count": sanitized.p0_count,
        "authored_html_used_block_ids": sorted(sanitized.used_block_ids),
        "block_manifest": sanitized.block_manifest,
        "asset_manifest": sanitized.asset_manifest,
    }


def _source_asset_manifest_sha256(ctx: Any) -> str | None:
    run_dir = getattr(ctx, "run_dir", None)
    if run_dir:
        path = Path(run_dir) / "paper_visual_provenance.json"
        if path.exists():
            try:
                return sha256_file(path)
            except OSError:
                return None
    return None


def _audit_source_asset_bindings(
    dom_audit: dict[str, Any],
    ctx: Any,
    sanitized: _SanitizedHtml,
) -> None:
    state = ctx.state if hasattr(ctx, "state") and isinstance(ctx.state, dict) else {}
    provenance = state.get("paper_visual_provenance") if isinstance(state.get("paper_visual_provenance"), dict) else {}
    assets = [
        asset for asset in list(provenance.get("assets") or [])
        if isinstance(asset, dict) and str(asset.get("asset_id") or "").strip()
    ]
    if not assets:
        return

    asset_by_id = {str(asset.get("asset_id")): asset for asset in assets}
    source_values: dict[str, str] = {}
    for asset in assets:
        asset_id = str(asset.get("asset_id") or "")
        output = str(asset.get("output_file") or "")
        if not output:
            continue
        source_values[output] = asset_id
        try:
            p = Path(output)
            source_values[p.name] = asset_id
            run_dir = getattr(ctx, "run_dir", None)
            if run_dir and not p.is_absolute():
                source_values[str((Path(run_dir) / p).resolve())] = asset_id
        except OSError:
            pass

    block_by_id = {
        str(block.get("block_id") or ""): block
        for block in sanitized.block_manifest
        if str(block.get("block_id") or "").strip()
    }
    selected_ids = _selected_source_asset_ids(state)
    placed_ids: set[str] = set()
    unbacked: list[dict[str, Any]] = []
    for block_id, block in block_by_id.items():
        kind = str(block.get("kind") or "").strip().lower()
        if kind not in _VISUAL_KINDS:
            continue
        refs = [
            block_id,
            str(block.get("layer_id") or ""),
            str(block.get("source_id") or ""),
            str(block.get("asset_id") or ""),
            str(block.get("canonical_entity_key") or ""),
        ]
        matched = next((ref for ref in refs if ref in asset_by_id), "")
        if matched:
            placed_ids.add(matched)
    for image in list(dom_audit.get("dom_images") or []):
        if not isinstance(image, dict):
            continue
        block_id = str(image.get("block_id") or "")
        src = str(image.get("src") or "")
        block = block_by_id.get(block_id) or {}
        refs = [
            block_id,
            str(block.get("layer_id") or ""),
            str(block.get("source_id") or ""),
        ]
        matched = next((ref for ref in refs if ref in asset_by_id), "")
        if not matched:
            matched = _match_source_asset_by_src(src, source_values)
        if matched:
            placed_ids.add(matched)
            continue
        if _looks_like_source_paper_image(block_id, src, block):
            unbacked.append({"block_id": block_id, "src": src})

    metrics = dom_audit.setdefault("paper_poster_dom_metrics", {})
    metrics["source_provenance_asset_count"] = len(assets)
    metrics["source_backed_dom_image_count"] = len(placed_ids)
    metrics["unbacked_source_image_count"] = len(unbacked)
    metrics["selected_source_asset_count"] = len(selected_ids)
    metrics["selected_source_asset_dom_placed_count"] = len(selected_ids & placed_ids)
    metrics["selected_source_asset_dom_missing_count"] = len(selected_ids - placed_ids)
    metrics["source_asset_manifest_sha256"] = _source_asset_manifest_sha256(ctx)

    findings = dom_audit.setdefault("paper_poster_dom_findings", [])
    if unbacked:
        findings.append(_finding(
            "P1",
            "paper-poster-unbacked-source-image",
            "Authored HTML includes paper-like images that are not bound to paper_visual_provenance assets.",
            "Bind each paper figure/table image block to a selected source asset id and use its local output_file/src_path.",
            repair_route="revise_visual_curation",
            evidence={"unbacked_images": unbacked[:8]},
        ))
    missing_selected = sorted(selected_ids - placed_ids)
    if missing_selected:
        findings.append(_finding(
            "P1",
            "paper-poster-selected-source-assets-missing",
            "Some storyboard-selected source visuals are missing from the authored HTML DOM.",
            "Place paper_visual_storyboard.selected_assets before adding lower-priority visuals or prose.",
            repair_route="revise_visual_curation",
            evidence={"missing_selected_source_assets": missing_selected[:12]},
        ))
    dom_audit["paper_poster_dom_p0_count"] = sum(
        1 for finding in findings if str(finding.get("severity")).upper() == "P0"
    )


def _selected_source_asset_ids(state: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    storyboard = state.get("paper_visual_storyboard") if isinstance(state.get("paper_visual_storyboard"), dict) else {}
    contract = state.get("poster_plan_contract") if isinstance(state.get("poster_plan_contract"), dict) else {}
    for source in (
        storyboard.get("layout_selected_assets"),
        contract.get("layout_selected_assets"),
    ):
        if not isinstance(source, list) or not source:
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            asset_id = str(item.get("asset_id") or item.get("layer_id") or "").strip()
            if asset_id:
                ids.add(asset_id)
        if ids:
            return ids
    for item in list(storyboard.get("selected_assets") or []):
        if isinstance(item, dict) and str(item.get("asset_id") or "").strip():
            ids.add(str(item.get("asset_id")))
    for key in ("selected_visuals", "storyboard_selected_assets"):
        for item in list(contract.get(key) or []):
            if isinstance(item, dict) and str(item.get("layer_id") or "").strip():
                ids.add(str(item.get("layer_id")))
    return ids


def _match_source_asset_by_src(src: str, source_values: dict[str, str]) -> str:
    if not src:
        return ""
    if src in source_values:
        return source_values[src]
    try:
        p = Path(src)
        if p.name in source_values:
            return source_values[p.name]
        resolved = str(p.expanduser().resolve())
        if resolved in source_values:
            return source_values[resolved]
    except OSError:
        pass
    return ""


def _looks_like_source_paper_image(block_id: str, src: str, block: dict[str, Any]) -> bool:
    identity_text = " ".join(str(value or "") for value in (
        block_id,
        block.get("layer_id"),
        block.get("source_id"),
        block.get("role"),
        block.get("source"),
        block.get("identity_asset_id"),
        block.get("identity_asset_role"),
    )).lower()
    if block.get("is_identity_asset") or "identity" in identity_text or "academic_identity_search" in identity_text:
        return False
    text = " ".join(str(value or "") for value in (
        block_id,
        src,
        block.get("layer_id"),
        block.get("source_id"),
        block.get("kind"),
        block.get("role"),
    )).lower()
    if any(marker in text for marker in ("ingest_fig_", "ingest_table_", "paper", "figure", "table", "source")):
        return True
    return str(block.get("kind") or "") in _VISUAL_KINDS and not block.get("is_identity_asset")


def _is_caption_for_overlap_audit(el: dict[str, Any]) -> bool:
    role_kind = str(el.get("role") or el.get("kind") or "").lower()
    tag = str(el.get("tag") or "").lower()
    block_id = str(el.get("block_id") or "").lower()
    class_name = str(el.get("class_name") or "").lower()
    if "callout" in " ".join((role_kind, block_id, class_name)):
        return False
    return "caption" in role_kind or tag == "figcaption"


def _dom_source_flow_text_findings(
    elements: list[dict[str, Any]],
    images: list[dict[str, Any]],
    *,
    cw: int,
    ch: int,
    hard: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source_images = [img for img in images if _is_source_dom_image(img)]
    text_records: list[dict[str, Any]] = []
    for el in elements:
        if not _is_text_like_dom_element(el):
            continue
        words = _dom_word_count(str(el.get("text") or ""))
        if words < 3:
            continue
        role_blob = " ".join(
            str(el.get(key) or "")
            for key in ("block_id", "role", "class_name", "tag")
        ).lower()
        if any(token in role_blob for token in ("title", "header", "footer", "author", "venue")):
            continue
        line_rects = _dom_text_line_rects(el)
        if not line_rects:
            continue
        text_records.append({
            "block_id": str(el.get("block_id") or ""),
            "panel_id": str(el.get("panel_id") or ""),
            "role": str(el.get("role") or ""),
            "class_name": str(el.get("class_name") or ""),
            "word_count": words,
            "line_rects": line_rects,
        })

    overlap_issues: list[dict[str, Any]] = []
    underfill_issues: list[dict[str, Any]] = []
    for image in source_images:
        image_rect = _rect(image.get("rect"))
        if image_rect["w"] < 32 or image_rect["h"] < 32:
            continue
        image_panel_id = str(image.get("panel_id") or "")
        side = _source_image_float_side(image, image_rect=image_rect, cw=cw)
        side_line_intervals: list[tuple[float, float]] = []
        side_gap_values: list[float] = []
        overlapping_lines: list[dict[str, Any]] = []
        nearby_word_count = 0
        for text in text_records:
            if image_panel_id and str(text.get("panel_id") or "") and str(text.get("panel_id") or "") != image_panel_id:
                continue
            local_has_line = False
            for line in text["line_rects"]:
                vertical_overlap = _vertical_overlap(line, image_rect)
                if vertical_overlap <= 2:
                    continue
                if _source_line_far_from_image(line, image_rect, cw=cw):
                    continue
                local_has_line = True
                overlap = _overlap_area(line, image_rect)
                if overlap > 10:
                    smaller = max(1.0, min(line["w"] * line["h"], image_rect["w"] * image_rect["h"]))
                    ratio = overlap / smaller
                    if ratio >= 0.018 or overlap >= 42:
                        overlapping_lines.append({
                            "text_block_id": text["block_id"],
                            "overlap_area_px": round(overlap, 2),
                            "overlap_ratio_of_smaller": round(ratio, 4),
                            "line_bbox": _bbox_from_rect(line),
                        })
                        continue
                if side and _line_is_beside_source_image(line, image_rect, side):
                    side_line_intervals.append((
                        max(float(line["y"]), float(image_rect["y"])),
                        min(float(line["y"] + line["h"]), float(image_rect["y"] + image_rect["h"])),
                    ))
                    gap = (
                        float(line["x"]) - float(image_rect["x"] + image_rect["w"])
                        if side == "left"
                        else float(image_rect["x"]) - float(line["x"] + line["w"])
                    )
                    if gap >= 0:
                        side_gap_values.append(gap)
            if local_has_line:
                nearby_word_count += int(text["word_count"])

        if overlapping_lines:
            worst = max(overlapping_lines, key=lambda item: float(item.get("overlap_area_px") or 0.0))
            overlap_issues.append({
                "image_block_id": str(image.get("block_id") or ""),
                "image_role": str(image.get("role") or ""),
                "image_bbox": _bbox_from_rect(image_rect),
                "overlap_count": len(overlapping_lines),
                "worst_overlap": worst,
            })

        role_blob = " ".join(str(image.get(key) or "") for key in ("block_id", "role", "src")).lower()
        if side and image_rect["h"] >= 170 and not any(token in role_blob for token in ("asset-wide", "source-table")):
            coverage = _interval_coverage(side_line_intervals) / max(1.0, float(image_rect["h"]))
            largest_gap = max(side_gap_values or [0.0])
            if coverage < 0.52 or largest_gap > 56:
                underfill_issues.append({
                    "image_block_id": str(image.get("block_id") or ""),
                    "image_role": str(image.get("role") or ""),
                    "image_bbox": _bbox_from_rect(image_rect),
                    "float_side": side,
                    "side_text_vertical_coverage": round(coverage, 4),
                    "min_side_text_vertical_coverage": 0.52,
                    "largest_side_gap_px": round(largest_gap, 2),
                    "nearby_word_count": nearby_word_count,
                })

    metrics = {
        "source_flow_image_count": len(source_images),
        "source_flow_text_overlap_count": len(overlap_issues),
        "source_flow_wrap_underfilled_count": len(underfill_issues),
        "source_flow_text_overlap_sample": overlap_issues[:6],
        "source_flow_wrap_underfilled_sample": underfill_issues[:6],
    }
    findings: list[dict[str, Any]] = []
    if overlap_issues:
        findings.append(_finding(
            "P0" if hard else "P1",
            "paper-poster-source-text-overlap",
            "Local source readout text overlaps a paper figure/table image.",
            (
                "Keep each source asset and its readout in one flow unit, but reserve enough "
                "space around the floated image/table so real text lines do not cross the visual."
            ),
            block_id=str(overlap_issues[0].get("image_block_id") or ""),
            repair_route="revise_authored_html",
            evidence={"issues": overlap_issues[:8]},
        ))
    if underfill_issues:
        findings.append(_finding(
            "P0" if hard and any(float(item.get("side_text_vertical_coverage") or 0.0) < 0.50 for item in underfill_issues) else "P1",
            "paper-poster-source-flow-wrap-underfilled",
            "A floated source figure/table leaves too much adjacent wrap space unused.",
            (
                "Lengthen or reposition the local readout/takeaway in the same figure-flow-unit, "
                "or resize the floated asset so the text wraps through the available side space."
            ),
            block_id=str(underfill_issues[0].get("image_block_id") or ""),
            repair_route="revise_authored_html",
            evidence={"issues": underfill_issues[:8]},
        ))
    return metrics, findings


def _dom_source_flow_list_gutter_findings(
    lists: list[dict[str, Any]],
    *,
    hard: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    min_padding_px = 18.0
    source_flow_lists = [
        item for item in lists
        if bool(item.get("has_source_flow_ancestor")) and bool(item.get("is_direct_source_flow_child"))
    ]
    risky: list[dict[str, Any]] = []
    for item in source_flow_lists:
        if not bool(item.get("has_floated_source_sibling")):
            continue
        if int(item.get("item_count") or 0) <= 0:
            continue
        padding = max(
            _float_value(item.get("paddingInlineStartPx")),
            _float_value(item.get("paddingLeftPx")),
        )
        text_indent = _float_value(item.get("textIndentPx"))
        if padding >= min_padding_px and text_indent >= -1.0:
            continue
        risky.append({
            "element_id": str(item.get("element_id") or item.get("block_id") or ""),
            "block_id": str(item.get("block_id") or ""),
            "tag": str(item.get("tag") or ""),
            "class_name": str(item.get("class_name") or ""),
            "source_flow_id": str(item.get("source_flow_id") or ""),
            "source_flow_class_name": str(item.get("source_flow_class_name") or ""),
            "padding_inline_start_px": round(padding, 2),
            "min_padding_inline_start_px": min_padding_px,
            "text_indent_px": round(text_indent, 2),
            "display": str(item.get("display") or ""),
            "list_style_position": str(item.get("listStylePosition") or ""),
            "floated_source_sibling_count": int(item.get("floated_source_sibling_count") or 0),
            "item_count": int(item.get("item_count") or 0),
            "bbox": _bbox_from_rect(_rect(item.get("rect"))),
        })

    metrics = {
        "source_flow_list_count": len(source_flow_lists),
        "source_flow_list_gutter_low_count": len(risky),
        "source_flow_list_gutter_low_sample": risky[:6],
    }
    if not risky:
        return metrics, []
    findings = [_finding(
        "P0" if hard else "P1",
        "paper_poster_source_flow_list_marker_gutter_low",
        "A floated source-flow bullet list does not reserve enough marker gutter.",
        (
            "Give the direct sibling source-flow list scoped CSS such as "
            "`display: flow-root; padding-inline-start: 1.25em; list-style-position: outside; "
            "li { padding-inline-start: .28em; }`, or switch the source asset to a stacked/full-width flow."
        ),
        block_id=str(risky[0].get("block_id") or risky[0].get("element_id") or ""),
        repair_route="revise_authored_html",
        evidence={"issues": risky[:8]},
    )]
    return metrics, findings


def _is_source_dom_image(image: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(image.get(key) or "")
        for key in ("block_id", "role", "src", "class_name", "wrapper_class_name")
    ).lower()
    return any(
        token in haystack
        for token in (
            "ingest_fig_",
            "ingest_table_",
            "source_visual",
            "source_visuals/",
            "flow-figure",
            "source-table",
        )
    )


def _dom_text_line_rects(el: dict[str, Any]) -> list[dict[str, float]]:
    rects: list[dict[str, float]] = []
    raw_rects = el.get("line_rects") if isinstance(el.get("line_rects"), list) else []
    for raw in raw_rects:
        if not isinstance(raw, dict):
            continue
        rect = _rect(raw)
        if rect["w"] <= 1 or rect["h"] <= 1:
            continue
        rects.append(rect)
    return rects


def _source_image_float_side(image: dict[str, Any], *, image_rect: dict[str, float], cw: int) -> str:
    role_blob = " ".join(
        str(image.get(key) or "")
        for key in ("block_id", "role", "src", "class_name", "wrapper_class_name")
    ).lower()
    if (
        "asset-wide" in role_blob
        or "source-table" in role_blob
        or float(image_rect["w"]) >= float(cw) * 0.24
    ):
        return ""
    css_float = " ".join(
        str(image.get(key) or "")
        for key in ("cssFloat", "wrapper_css_float")
    ).lower()
    if "left" in css_float:
        return "left"
    if "right" in css_float:
        return "right"
    if "float-left" in role_blob or "wrap-left" in role_blob:
        return "left"
    if "float-right" in role_blob or "wrap-right" in role_blob:
        return "right"
    return ""


def _source_line_far_from_image(line: dict[str, float], image_rect: dict[str, float], *, cw: int) -> bool:
    if _horizontal_gap(line, image_rect) > max(180.0, float(cw) * 0.08):
        return True
    return False


def _line_is_beside_source_image(line: dict[str, float], image_rect: dict[str, float], side: str) -> bool:
    if side == "left":
        return float(line["x"]) >= float(image_rect["x"] + image_rect["w"]) - 2.0
    if side == "right":
        return float(line["x"] + line["w"]) <= float(image_rect["x"]) + 2.0
    return False


def _vertical_overlap(a: dict[str, float], b: dict[str, float]) -> float:
    return max(0.0, min(float(a["y"] + a["h"]), float(b["y"] + b["h"])) - max(float(a["y"]), float(b["y"])))


def _horizontal_gap(a: dict[str, float], b: dict[str, float]) -> float:
    if float(a["x"] + a["w"]) < float(b["x"]):
        return float(b["x"]) - float(a["x"] + a["w"])
    if float(b["x"] + b["w"]) < float(a["x"]):
        return float(a["x"]) - float(b["x"] + b["w"])
    return 0.0


def _interval_coverage(intervals: list[tuple[float, float]]) -> float:
    clean = sorted((float(a), float(b)) for a, b in intervals if float(b) > float(a))
    if not clean:
        return 0.0
    total = 0.0
    start, end = clean[0]
    for next_start, next_end in clean[1:]:
        if next_start <= end:
            end = max(end, next_end)
            continue
        total += end - start
        start, end = next_start, next_end
    total += end - start
    return total


def _is_text_like_dom_element(el: dict[str, Any]) -> bool:
    text = str(el.get("text") or "").strip()
    if not text:
        return False
    tag = str(el.get("tag") or "").lower()
    kind = str(el.get("kind") or "").lower()
    role = str(el.get("role") or "").lower()
    block_id = str(el.get("block_id") or "").lower()
    class_name = str(el.get("class_name") or "").lower()
    child_block_count = int(el.get("child_block_id_count") or 0)
    direct_text = str(el.get("direct_text") or "").strip()
    if (
        child_block_count > 0
        and tag in {"div", "section", "article", "main", "figure", "header", "footer", "aside"}
        and not direct_text
    ):
        return False
    if tag in {"img", "svg", "canvas", "table", "thead", "tbody", "tr"}:
        return False
    if kind in _VISUAL_KINDS or kind in {"group", "container", "grid", "shape"}:
        return False
    haystack = " ".join((kind, role, block_id, class_name))
    if tag in {"section", "article", "main", "figure", "div"} and any(
        token in haystack
        for token in (
            "panel",
            "grid",
            "wrapper",
            "visual-card",
            "source-visual",
            "figure",
            "table-wrap",
            "image",
        )
    ):
        return False
    if tag in {
        "p",
        "span",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "td",
        "th",
        "figcaption",
        "small",
        "strong",
        "em",
        "blockquote",
        "label",
    }:
        return True
    return any(
        token in haystack
        for token in (
            "title",
            "subtitle",
            "caption",
            "body",
            "text",
            "label",
            "thesis",
            "callout",
            "footer",
            "takeaway",
            "author",
            "venue",
            "meta",
            "bullet",
            "quote",
            "metric",
            "stat",
            "formula",
            "provenance",
            "limitation",
            "analysis",
            "result",
        )
    )


def _dom_text_overflow(el: dict[str, Any]) -> dict[str, Any]:
    client_w = _float_value(el.get("clientWidth"))
    client_h = _float_value(el.get("clientHeight"))
    scroll_w = _float_value(el.get("scrollWidth"))
    scroll_h = _float_value(el.get("scrollHeight"))
    rect = _rect(el.get("rect"))
    width_gap = max(0.0, scroll_w - max(client_w, rect["w"]) - 3.0)
    height_gap = max(0.0, scroll_h - max(client_h, rect["h"]) - 3.0)
    width_ratio = width_gap / max(1.0, client_w or rect["w"])
    height_ratio = height_gap / max(1.0, client_h or rect["h"])
    text = str(el.get("text") or "")
    return {
        "block_id": str(el.get("block_id") or ""),
        "word_count": _dom_word_count(text),
        "overflow_ratio": round(max(width_ratio, height_ratio), 4),
        "width_gap_px": round(width_gap, 2),
        "height_gap_px": round(height_gap, 2),
        "scroll_width_px": round(scroll_w, 2),
        "scroll_height_px": round(scroll_h, 2),
        "client_width_px": round(client_w, 2),
        "client_height_px": round(client_h, 2),
        "bbox": _bbox_from_rect(rect),
        "overflow_x": str(el.get("overflowX") or ""),
        "overflow_y": str(el.get("overflowY") or ""),
        "font_size": str(el.get("fontSize") or ""),
        "line_height": str(el.get("lineHeight") or ""),
    }


def _dom_text_overlap_findings(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text_elements: list[dict[str, Any]] = []
    for el in elements:
        if not _is_text_like_dom_element(el):
            continue
        rect = _rect(el.get("visible_rect") or el.get("rect"))
        if rect["w"] <= 1 or rect["h"] <= 1:
            continue
        words = _dom_word_count(str(el.get("text") or ""))
        if words < 3:
            continue
        text_elements.append({
            "block_id": str(el.get("block_id") or ""),
            "role": str(el.get("role") or ""),
            "class_name": str(el.get("class_name") or ""),
            "word_count": words,
            "rect": rect,
            "area": max(1.0, rect["w"] * rect["h"]),
        })

    findings: list[dict[str, Any]] = []
    for i, left in enumerate(text_elements):
        for right in text_elements[i + 1:]:
            overlap = _overlap_area(left["rect"], right["rect"])
            if overlap <= 24:
                continue
            smaller = max(1.0, min(left["area"], right["area"]))
            ratio = overlap / smaller
            if ratio < 0.08 and overlap < 160:
                continue
            severity = "P0" if ratio >= 0.15 or (overlap >= 1200 and ratio >= 0.12) else "P1"
            findings.append(_finding(
                severity,
                "paper-poster-text-overlap",
                "Two editable text blocks overlap in the rendered poster.",
                "Separate the blocks, reserve fixed lanes, or reduce text/font size so text does not collide.",
                block_id=left["block_id"],
                repair_route="revise_authored_html",
                evidence={
                    "left_block_id": left["block_id"],
                    "right_block_id": right["block_id"],
                    "left_word_count": left["word_count"],
                    "right_word_count": right["word_count"],
                    "overlap_area_px": round(overlap, 2),
                    "overlap_ratio_of_smaller": round(ratio, 4),
                    "left_bbox": _bbox_from_rect(left["rect"]),
                    "right_bbox": _bbox_from_rect(right["rect"]),
                },
            ))
    return findings


def _dom_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9_+./%-]*", text or ""))


def _dom_layer_from_element(el: dict[str, Any], frame: Any) -> dict[str, Any]:
    block_id = str(el.get("block_id") or "")
    block = _block_by_id(frame, block_id) or {}
    kind = str(block.get("kind") or el.get("kind") or "text")
    layer = {
        "layer_id": block_id,
        "kind": "text" if kind in _TEXTUAL_KINDS else "table" if kind == "table" else "image" if kind in _VISUAL_KINDS else kind,
        "role": el.get("role") or block.get("role"),
        "class_name": el.get("class_name") or block.get("class_name"),
        "source": block.get("source"),
        "source_id": block.get("source_id") or block.get("layer_id"),
        "src_path": block.get("src_path"),
        "bbox": _bbox_from_rect(_rect(el.get("rect"))),
        "text": el.get("text") or block.get("text") or block.get("caption") or "",
    }
    layer.update({key: value for key, value in _block_identity_metadata(block).items() if value is not None})
    return layer


def _block_identity_metadata(block: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    provenance = block.get("provenance") if isinstance(block.get("provenance"), dict) else {}
    for key in _IDENTITY_FIELDS:
        value = block.get(key)
        if value is None:
            value = provenance.get(key)
        metadata[key] = value
    return metadata


def _identity_assets_from_state(ctx: Any) -> list[dict[str, Any]]:
    state = ctx.state if hasattr(ctx, "state") and isinstance(ctx.state, dict) else {}
    assets: list[dict[str, Any]] = []
    for source in (
        state.get("academic_identity_assets"),
        (state.get("poster_plan_contract") or {}).get("identity_system")
        if isinstance(state.get("poster_plan_contract"), dict)
        else None,
    ):
        raw_assets = source.get("assets") if isinstance(source, dict) else None
        if not isinstance(raw_assets, list):
            continue
        for asset in raw_assets:
            if isinstance(asset, dict):
                assets.append(asset)
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asset in assets:
        key = str(asset.get("asset_id") or asset.get("rendered_layer_id") or id(asset))
        if key in seen:
            continue
        unique.append(asset)
        seen.add(key)
    return unique


def _matching_identity_asset(
    block: dict[str, Any],
    hydrated: dict[str, Any],
    dom: dict[str, Any],
    identity_assets: list[dict[str, Any]],
) -> dict[str, Any] | None:
    keys = _identity_match_keys(block, hydrated, dom)
    text = " ".join(
        str(value or "")
        for value in (
            block.get("text"),
            block.get("title"),
            block.get("caption"),
            hydrated.get("text"),
            hydrated.get("name"),
            dom.get("text"),
        )
    ).lower()
    src_values = {
        str(value or "").strip()
        for value in (block.get("src_path"), hydrated.get("src_path"), dom.get("src_path"))
        if str(value or "").strip()
    }
    for asset in identity_assets:
        if not isinstance(asset, dict):
            continue
        asset_ids = {
            str(asset.get("asset_id") or "").strip(),
            str(asset.get("rendered_layer_id") or "").strip(),
            str(asset.get("source_id") or "").strip(),
        }
        asset_ids.discard("")
        if keys & asset_ids:
            return asset
        local_path = str(asset.get("local_asset_path") or asset.get("src_path") or "").strip()
        if local_path and local_path in src_values:
            return asset
        label = str(asset.get("label") or asset.get("entity_name") or "").strip().lower()
        if (
            asset.get("asset_type") == "text_badge"
            and label
            and label in text
            and _is_plausible_identity_badge_record(block, hydrated, dom, label=label)
        ):
            return asset
    return None


def _is_plausible_identity_badge_record(
    block: dict[str, Any],
    hydrated: dict[str, Any],
    dom: dict[str, Any],
    *,
    label: str,
) -> bool:
    merged_text = " ".join(
        str(record.get(key) or "")
        for record in (block, hydrated, dom)
        if isinstance(record, dict)
        for key in ("text", "title", "caption", "name")
    ).strip()
    tokens = re.findall(r"[A-Za-z0-9]+", merged_text)
    if len(tokens) > 6:
        return False
    role_text = " ".join(
        str(record.get(key) or "")
        for record in (block, hydrated, dom)
        if isinstance(record, dict)
        for key in ("role", "kind", "class_name", "block_id", "layer_id")
    ).lower()
    if not any(token in role_text for token in ("badge", "label", "identity", "logo", "venue")):
        return False
    bbox = (
        dom.get("bbox") if isinstance(dom.get("bbox"), dict)
        else block.get("bbox") if isinstance(block.get("bbox"), dict)
        else hydrated.get("bbox") if isinstance(hydrated.get("bbox"), dict)
        else {}
    )
    width = _float_value(bbox.get("w"), 0.0) if isinstance(bbox, dict) else 0.0
    height = _float_value(bbox.get("h"), 0.0) if isinstance(bbox, dict) else 0.0
    if width and height and width * height > 240_000:
        return False
    return label in merged_text.lower()


def _identity_match_keys(*records: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
        for key in (
            "block_id",
            "layer_id",
            "source_id",
            "identity_asset_id",
            "asset_id",
            "rendered_layer_id",
        ):
            value = record.get(key)
            if value is None:
                value = provenance.get(key)
            if str(value or "").strip():
                keys.add(str(value).strip())
    return keys


def _merged_identity_metadata(
    block: dict[str, Any],
    hydrated: dict[str, Any],
    dom: dict[str, Any],
    asset: dict[str, Any] | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for source in (dom, hydrated, block):
        out.update({key: value for key, value in _record_identity_metadata(source).items() if value is not None})
    if asset:
        out.update({
            "is_identity_asset": True,
            "identity_asset_id": asset.get("asset_id") or out.get("identity_asset_id"),
            "asset_id": asset.get("asset_id") or out.get("asset_id"),
            "asset_type": asset.get("asset_type") or out.get("asset_type"),
            "identity_asset_role": asset.get("role") or out.get("identity_asset_role"),
            "identity_entity_name": asset.get("entity_name") or asset.get("label") or out.get("identity_entity_name"),
            "identity_required_to_place": asset.get("required_to_place"),
            "identity_allowed_to_place": asset.get("allowed_to_place"),
            "identity_primary": asset.get("primary_identity"),
            "identity_asset_intent": asset.get("placement_intent"),
            "identity_group": asset.get("identity_group"),
            "canonical_entity_key": asset.get("canonical_entity_key"),
        })
    if out.get("identity_asset_id") or out.get("asset_id") or out.get("identity_asset_role"):
        out["is_identity_asset"] = True
    return out


def _record_identity_metadata(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    metadata = {key: record.get(key) for key in _IDENTITY_FIELDS if record.get(key) is not None}
    provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    for key in _IDENTITY_FIELDS:
        if metadata.get(key) is None and provenance.get(key) is not None:
            metadata[key] = provenance.get(key)
    return metadata


def _block_by_id(frame: Any, block_id: str) -> dict[str, Any] | None:
    for block in _flatten_blocks([_model_or_dict(b) for b in list(getattr(frame, "blocks", []) or [])]):
        if str(block.get("block_id") or "") == block_id:
            return block
    return None


def _rect(raw: Any) -> dict[str, float]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "x": float(raw.get("x") or 0),
        "y": float(raw.get("y") or 0),
        "w": float(raw.get("w") or 0),
        "h": float(raw.get("h") or 0),
    }


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bbox_from_rect(rect: dict[str, float]) -> dict[str, int]:
    return {
        "x": int(round(rect["x"])),
        "y": int(round(rect["y"])),
        "w": max(0, int(round(rect["w"]))),
        "h": max(0, int(round(rect["h"]))),
    }


def _out_of_bounds(rect: dict[str, float], cw: int, ch: int) -> bool:
    return rect["x"] < -4 or rect["y"] < -4 or rect["x"] + rect["w"] > cw + 4 or rect["y"] + rect["h"] > ch + 4


def _overlap_area(a: dict[str, float], b: dict[str, float]) -> float:
    x1 = max(a["x"], b["x"])
    y1 = max(a["y"], b["y"])
    x2 = min(a["x"] + a["w"], b["x"] + b["w"])
    y2 = min(a["y"] + a["h"], b["y"] + b["h"])
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _model_or_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return {}


def _flatten_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        out.append(block)
        out.extend(_flatten_blocks([b for b in block.get("children") or [] if isinstance(b, dict)]))
    return out


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _append_math_render_findings(
    findings: list[dict[str, Any]],
    metrics: dict[str, Any],
    status: dict[str, Any],
) -> None:
    if not status:
        return
    bundle_present = bool(status.get("bundlePresent"))
    has_source = bool(status.get("hasSource"))
    rendered_count = _int_metric(status.get("renderedCount"))
    error_count = _int_metric(status.get("katexErrorCount"))
    raw_backslash_count = _int_metric(status.get("rawBackslashDelimiterCount"))
    raw_dollar_count = _int_metric(status.get("rawDollarDelimiterCount"))
    before_raw_count = _int_metric(status.get("beforeRawDelimiterCount"))
    metrics.update({
        "math_bundle_present": bundle_present,
        "math_source_detected": has_source,
        "math_rendered_count": rendered_count,
        "math_katex_error_count": error_count,
        "math_raw_backslash_delimiter_count": raw_backslash_count,
        "math_raw_dollar_delimiter_count": raw_dollar_count,
    })
    if not bundle_present:
        return
    if has_source and rendered_count <= 0:
        findings.append(_finding(
            "P0",
            "paper-poster-math-not-rendered",
            "TeX math delimiters were detected, but KaTeX did not render any formulas.",
            "Keep formulas inside \\(...\\) or \\[...\\] and let AutoDesign inject KaTeX; do not hand-write math scripts.",
            evidence={k: v for k, v in status.items() if k != "errorMessage"},
        ))
    if error_count > 0:
        findings.append(_finding(
            "P0",
            "paper-poster-math-render-error",
            "KaTeX reported at least one formula render error.",
            "Rewrite the invalid TeX formula using KaTeX-supported syntax.",
            evidence=status,
        ))
    if has_source and raw_backslash_count > 0 and rendered_count < max(1, before_raw_count):
        findings.append(_finding(
            "P0",
            "paper-poster-math-raw-delimiters",
            "Raw TeX delimiters remain visible after math rendering.",
            "Use valid \\(...\\) or \\[...\\] formulas and avoid nesting unsupported HTML inside math.",
            evidence=status,
        ))
    elif has_source and raw_dollar_count > 0 and rendered_count <= 0:
        findings.append(_finding(
            "P1",
            "paper-poster-math-dollar-delimiters-visible",
            "Dollar math delimiters may still be visible after rendering.",
            "Prefer \\(...\\) or \\[...\\] delimiters in paper posters to avoid currency/dollar ambiguity.",
            evidence=status,
        ))


def _int_metric(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _finding(
    severity: str,
    finding_id: str,
    message: str,
    fix: str,
    *,
    block_id: str | None = None,
    repair_route: str = "revise_authored_html",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    finding = {
        "severity": severity,
        "id": finding_id,
        "message": message,
        "fix": fix,
        "stage": "rendering_export",
        "repair_route": repair_route,
    }
    if block_id:
        finding["block_id"] = block_id
    if evidence:
        finding["evidence"] = evidence
    return finding


def _launch_chromium_for_audit(p: Any) -> Any:
    try:
        return p.chromium.launch(args=["--no-sandbox"])
    except Exception as primary:
        try:
            return p.chromium.launch(channel="chrome", args=["--no-sandbox"])
        except Exception as secondary:
            raise RuntimeError(f"{primary}; chrome_channel_failed: {secondary}") from secondary
