"""Quality assets for visual direction, references, and HTML linting.

The data in this module is distilled from the sibling ``design-anything`` repo's
direction library, craft anti-slop rules, and template skill playbooks. It is
kept intentionally small so the planner gets concrete defaults without pulling
in that repo's daemon or skill runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .util.academic_palette import load_academic_palette_library


VisualProfileId = Literal[
    "editorial-monocle",
    "modern-minimal",
    "warm-soft",
    "tech-utility",
    "brutalist-experimental",
]

LintSeverity = Literal["P0", "P1", "P2"]


@dataclass(frozen=True)
class VisualProfile:
    id: VisualProfileId
    label: str
    mood: str
    references: tuple[str, ...]
    display_font: str
    body_font: str
    mono_font: str
    palette: dict[str, str]
    posture: tuple[str, ...]

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "mood": self.mood,
            "references": list(self.references),
            "display_font": self.display_font,
            "body_font": self.body_font,
            "mono_font": self.mono_font,
            "palette": dict(self.palette),
            "posture": list(self.posture),
        }


VISUAL_PROFILES: dict[VisualProfileId, VisualProfile] = {
    "editorial-monocle": VisualProfile(
        id="editorial-monocle",
        label="Editorial - Monocle / FT magazine",
        mood=(
            "Print-magazine feel: generous whitespace, serif headlines, "
            "off-white paper, ink, and one warm accent."
        ),
        references=("Monocle", "Financial Times Weekend", "NYT Magazine", "It's Nice That"),
        display_font="'Iowan Old Style', 'Charter', Georgia, serif",
        body_font="-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
        mono_font="'JetBrains Mono', 'IBM Plex Mono', ui-monospace, Menlo, monospace",
        palette={
            "bg": "oklch(97% 0.012 80)",
            "surface": "oklch(99% 0.005 80)",
            "fg": "oklch(20% 0.02 60)",
            "muted": "oklch(48% 0.015 60)",
            "border": "oklch(89% 0.012 80)",
            "accent": "oklch(58% 0.16 35)",
        },
        posture=(
            "serif display, sans body, mono metadata",
            "borders and whitespace over shadows",
            "one decisive image or figure",
            "accent appears at most twice per screen",
        ),
    ),
    "modern-minimal": VisualProfile(
        id="modern-minimal",
        label="Modern minimal - Linear / Vercel",
        mood=(
            "Quiet software-native precision: near greyscale surfaces, system "
            "fonts, hairline borders, and one saturated accent."
        ),
        references=("Linear", "Vercel", "Notion", "Stripe docs"),
        display_font="-apple-system, BlinkMacSystemFont, 'SF Pro Display', system-ui, sans-serif",
        body_font="-apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, sans-serif",
        mono_font="'JetBrains Mono', 'IBM Plex Mono', ui-monospace, Menlo, monospace",
        palette={
            "bg": "oklch(99% 0.002 240)",
            "surface": "oklch(100% 0 0)",
            "fg": "oklch(18% 0.012 250)",
            "muted": "oklch(54% 0.012 250)",
            "border": "oklch(92% 0.005 250)",
            "accent": "oklch(58% 0.18 255)",
        },
        posture=(
            "tight grid, thin dividers, no decorative cards",
            "tabular numerics for metrics",
            "content-led layout over hero illustration",
            "accent reserved for links and primary CTA",
        ),
    ),
    "warm-soft": VisualProfile(
        id="warm-soft",
        label="Warm soft - Stripe pre-2020 / Headspace",
        mood=(
            "Cream canvas, soft serif display, gentle radii, and a terracotta "
            "accent. Friendly without being cute."
        ),
        references=("Stripe pre-2020", "Headspace", "Substack", "Mercury"),
        display_font="'Tiempos Headline', 'Newsreader', 'Iowan Old Style', Georgia, serif",
        body_font="'Sohne', -apple-system, BlinkMacSystemFont, system-ui, sans-serif",
        mono_font="'JetBrains Mono', 'IBM Plex Mono', ui-monospace, Menlo, monospace",
        palette={
            "bg": "oklch(97% 0.018 70)",
            "surface": "oklch(99% 0.008 70)",
            "fg": "oklch(22% 0.02 50)",
            "muted": "oklch(50% 0.018 50)",
            "border": "oklch(90% 0.014 70)",
            "accent": "oklch(64% 0.13 28)",
        },
        posture=(
            "serif display with calm sans body",
            "gentle 12-16px radii only when useful",
            "soft inner glow over heavy shadows",
            "real imagery or honest placeholders over icons",
        ),
    ),
    "tech-utility": VisualProfile(
        id="tech-utility",
        label="Tech utility - Datadog / GitHub",
        mood=(
            "Data-dense, legible, operational. Built for engineers who need "
            "information per square inch."
        ),
        references=("Datadog", "GitHub", "Cloudflare dashboard", "Sentry"),
        display_font="-apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', system-ui, sans-serif",
        body_font="-apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', system-ui, sans-serif",
        mono_font="'JetBrains Mono', 'IBM Plex Mono', ui-monospace, Menlo, monospace",
        palette={
            "bg": "oklch(98% 0.005 250)",
            "surface": "oklch(100% 0 0)",
            "fg": "oklch(22% 0.02 240)",
            "muted": "oklch(50% 0.018 240)",
            "border": "oklch(90% 0.008 240)",
            "accent": "oklch(58% 0.16 145)",
        },
        posture=(
            "dense tables, status chips, and tabular numerics",
            "mono only for code, ids, hashes, and metrics",
            "avoid marketing hero imagery",
            "show the product or data state first",
        ),
    ),
    "brutalist-experimental": VisualProfile(
        id="brutalist-experimental",
        label="Brutalist experimental - Are.na / Yale",
        mood=(
            "Loud type, visible grid, hard borders, and asymmetric tension. "
            "Deliberate roughness as confidence."
        ),
        references=("Are.na", "Yale Center for British Art", "MSCHF", "Read.cv"),
        display_font="'Times New Roman', 'Iowan Old Style', Georgia, serif",
        body_font="ui-monospace, 'IBM Plex Mono', 'JetBrains Mono', Menlo, monospace",
        mono_font="ui-monospace, 'IBM Plex Mono', 'JetBrains Mono', Menlo, monospace",
        palette={
            "bg": "oklch(96% 0.004 100)",
            "surface": "oklch(100% 0 0)",
            "fg": "oklch(15% 0.02 100)",
            "muted": "oklch(40% 0.02 100)",
            "border": "oklch(15% 0.02 100)",
            "accent": "oklch(60% 0.22 25)",
        },
        posture=(
            "oversized serif display and mono body",
            "1.5-2px full-strength borders",
            "0-2px radius, no shadows, no gradients",
            "asymmetric 70/30 compositions",
        ),
    ),
}


DESIGN_REFERENCE_CARDS: dict[str, str] = {
    "linear-app": "precise software UI, grey surfaces, hairline borders, cobalt accent restraint",
    "stripe": "technical credibility, refined docs/product storytelling, calm whitespace",
    "vercel": "minimal developer launch pages, black/white contrast, product-first hierarchy",
    "apple": "premium media surface, huge type, restrained motion, inspection-grade imagery",
    "xiaohongshu": "social editorial energy, warm cards, creator commerce, high-density imagery",
    "wired": "tech editorial drama, bold crops, punchy display typography, data-story rhythm",
    "publication": "magazine structure, serif hierarchy, pull quotes, clear editorial pacing",
    "notion": "quiet productivity, modular blocks, soft ink, document-native hierarchy",
    "figma": "creative tooling, collaborative cues, colorful but systematic product surfaces",
    "github": "developer workflow, mono metadata, dense lists, familiar repo/status patterns",
}


TEMPLATE_PLAYBOOK = """Quality template playbook:
- Posters: one dominant visual surface, clear title band, text-free generated background, honest placeholders instead of invented claims.
- Landings: first viewport needs headline, subhead, CTA or product image; 3-6 sections; no generic hero/features/pricing/FAQ autopilot.
- Decks: plan slide rhythm before rendering; no 3+ identical visual beats in a row; cover and closing slides must be intentional.
- Template rule: reuse stable primitives and layout archetypes before inventing new CSS or slide geometry.
- Self-check: philosophy, hierarchy, execution, specificity, restraint. Any weak dimension gets one revision before finalize.
"""


AI_DEFAULT_INDIGO = (
    "#6366f1", "#4f46e5", "#4338ca", "#3730a3",
    "#8b5cf6", "#7c3aed", "#a855f7",
)
PURPLE_HEXES = (
    "#a855f7", "#9333ea", "#7c3aed", "#6d28d9", "#581c87",
    "#8b5cf6", "#a78bfa", "#c4b5fd", "#ddd6fe", "#ede9fe",
    "#6366f1", "#4f46e5", "#4338ca", "#3730a3", "#312e81",
    "#818cf8", "#a5b4fc", "#c7d2fe", "#e0e7ff", "#eef2ff",
)
AI_DEFAULT_PURPLE_INDIGO = tuple(dict.fromkeys((*AI_DEFAULT_INDIGO, *PURPLE_HEXES)))
TRUST_BLUE_HEXES = (
    "#3b82f6", "#2563eb", "#1d4ed8", "#1e40af", "#1e3a8a",
    "#60a5fa", "#93c5fd", "#bfdbfe", "#0ea5e9", "#0284c7",
    "#0369a1", "#38bdf8", "#7dd3fc",
)
TRUST_CYAN_HEXES = (
    "#06b6d4", "#0891b2", "#0e7490", "#155e75", "#164e63",
    "#22d3ee", "#67e8f9", "#a5f3fc",
)
SLOP_EMOJI = ("✨", "🚀", "🎯", "⚡", "🔥", "💡", "📈", "🎨", "🛡️", "🌟")
INVENTED_METRIC_PATTERNS = (
    re.compile(r"\b10[×x]\s+(faster|better|easier)\b", re.I),
    re.compile(r"\b100[×x]\s+(faster|better)\b", re.I),
    re.compile(r"\b99\.\d+%\s+uptime\b", re.I),
    re.compile(r"\bzero[- ]downtime\b", re.I),
    re.compile(r"\b3[×x]\s+more\s+(productive|efficient)\b", re.I),
)
FILLER_PATTERNS = (
    re.compile(r"\bfeature\s+(one|two|three|1|2|3)\b", re.I),
    re.compile(r"\blorem\s+ipsum\b", re.I),
    re.compile(r"\bdolor\s+sit\s+amet\b", re.I),
    re.compile(r"\bplaceholder\s+text\b", re.I),
    re.compile(r"\bsample\s+content\b", re.I),
)


def get_visual_profile(profile_id: str | None) -> VisualProfile | None:
    if profile_id in VISUAL_PROFILES:
        return VISUAL_PROFILES[profile_id]  # type: ignore[index]
    return None


def lint_html_quality(raw_html: str) -> list[dict[str, Any]]:
    """Return deterministic anti-slop findings for generated HTML."""
    if not raw_html:
        return []
    html = _strip_heavy_inline_assets(re.sub(r"<!--[\s\S]*?-->", "", raw_html))
    visible_text = _visible_text(html)
    findings: list[dict[str, Any]] = []

    _append_gradient_findings(html, findings)
    _append_ai_indigo_finding(html, findings)
    _append_emoji_finding(html, findings)
    _append_left_accent_card_finding(html, findings)
    _append_pattern_finding(
        visible_text,
        findings,
        FILLER_PATTERNS,
        "filler-copy",
        "P0",
        "Filler copy found in generated artifact.",
        "Replace placeholders with brief-specific copy or an honest labelled stub.",
    )
    _append_pattern_finding(
        visible_text,
        findings,
        INVENTED_METRIC_PATTERNS,
        "invented-metric",
        "P0",
        "Likely invented metric found.",
        "Use sourced numbers with evidence or replace with a labelled placeholder.",
    )
    _append_external_placeholder_finding(html, findings)
    _append_accent_overuse_finding(html, findings)
    return findings


def count_p0_findings(findings: list[dict[str, Any]]) -> int:
    return sum(1 for f in findings if f.get("severity") == "P0")


def audit_paper_poster_density(
    layers: list[dict[str, Any]],
    canvas: dict[str, Any],
    *,
    rendered_layers: dict[str, dict[str, Any]] | None = None,
    poster_plan_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return deterministic density findings for paper-to-poster composites.

    The audit is intentionally non-fatal. Composite payloads surface the
    findings so the planner can perform one concrete revision when a paper
    poster is too text-heavy or too sparse.
    """
    cw = _to_int(canvas.get("w_px"), 0)
    ch = _to_int(canvas.get("h_px"), 0)
    canvas_area = max(1, cw * ch)
    rendered_layers = rendered_layers or {}
    contract = poster_plan_contract if isinstance(poster_plan_contract, dict) else {}
    content_fill_targets = (
        contract.get("content_fill_targets")
        if isinstance(contract.get("content_fill_targets"), dict)
        else {}
    )
    density_targets = (
        contract.get("density_targets")
        if isinstance(contract.get("density_targets"), dict)
        else {}
    )
    dense_content_fill_mode = (
        str(contract.get("reference_profile") or "") == "research_synthesis_dense"
        or bool(content_fill_targets)
    )

    placed_visuals = [L for L in layers if _is_paper_visual_layer(L)]
    available_visuals = [
        rec for rec in rendered_layers.values()
        if _is_paper_visual_layer(rec)
    ]
    is_paper_poster = bool(placed_visuals or available_visuals)
    is_a0 = cw >= 3000 and ch >= 4500
    aspect = cw / max(1, ch)
    is_portrait_academic = cw >= 2000 and ch >= 2800 and 0.62 <= aspect <= 0.82
    is_wide_academic = cw >= 2800 and ch >= 1400 and aspect >= 1.6
    is_landscape_academic = cw >= 2800 and ch >= 1800 and 1.25 <= aspect < 1.6
    is_academic_poster = (
        is_a0 or is_portrait_academic or is_wide_academic or is_landscape_academic
    )
    target_ratio = (
        0.42 if is_a0 else
        0.34 if is_portrait_academic else
        0.34 if is_wide_academic else
        0.34 if is_landscape_academic else
        0.30
    )
    p0_margin = 0.07 if (is_a0 or is_wide_academic) else 0.05
    contract_required = _to_int(density_targets.get("min_visual_count"), 0)
    contract_max = _to_int(density_targets.get("max_visual_count"), 0)
    base_required = contract_required if contract_required > 0 else (8 if is_academic_poster else 5)
    available_count = len(available_visuals)
    required_count = (
        min(base_required, available_count)
        if available_count > 0 else 0
    )
    source_limited_visuals = 0 < available_count < base_required
    if source_limited_visuals:
        if available_count <= 2:
            target_ratio = min(target_ratio, 0.24)
        elif available_count <= 3:
            target_ratio = min(target_ratio, 0.30)
        elif available_count <= 5:
            target_ratio = min(target_ratio, 0.38)
    if is_paper_poster:
        target_ratio = min(
            target_ratio,
            _mixed_panel_visual_area_target(
                layers,
                placed_visuals,
                is_a0=is_a0,
                is_portrait_academic=is_portrait_academic,
                is_wide_academic=is_wide_academic,
                is_landscape_academic=is_landscape_academic,
            ),
        )
        if dense_content_fill_mode:
            # Dense synthesis references are not screenshot walls. They need a
            # readable source-visual floor, while the expensive work happens in
            # filled native text/table/card panels around those visuals.
            readable_floor = _to_float(
                content_fill_targets.get("min_readable_source_visual_area_ratio"),
                0.12,
            )
            readable_floor = min(0.24, max(0.08, readable_floor))
            target_ratio = min(target_ratio, max(readable_floor, min(0.18, readable_floor + 0.04)))
    p0_target_ratio = max(0.0, target_ratio - p0_margin)

    visual_area = sum(_clipped_bbox_area(L.get("bbox") or {}, cw, ch)
                      for L in placed_visuals)
    visual_area_ratio = min(1.0, visual_area / float(canvas_area))
    placed_table_count = sum(1 for L in placed_visuals if L.get("kind") == "table")
    findings: list[dict[str, Any]] = []

    if is_paper_poster and required_count > 0 and len(placed_visuals) < required_count:
        findings.append(_finding(
            "P0",
            "paper-visual-count-low",
            (
                f"Paper poster places {len(placed_visuals)} figure/table panels; "
                f"target is at least {required_count} for this canvas."
            ),
            "Place more ingested figures/tables or use a denser paper poster archetype.",
            f"placed={len(placed_visuals)} required={required_count}",
        ))

    if (
        is_paper_poster
        and available_count >= max(2, required_count)
        and visual_area_ratio < target_ratio
    ):
        gap = max(0.0, target_ratio - visual_area_ratio)
        if visual_area_ratio < p0_target_ratio:
            severity = "P0"
        elif gap <= 0.04 and required_count > 0 and len(placed_visuals) >= required_count:
            severity = "P2"
        else:
            severity = "P1"
        if dense_content_fill_mode:
            fix = (
                "Restore enough readable paper visuals to meet the source-evidence "
                "floor, then spend the remaining area on dense sourced text, native "
                "tables/cards/pipelines, local figure explanations, and filled panels. "
                "Do not solve dense-reference gaps by enlarging screenshots and "
                "shrinking synthesis copy."
            )
        elif gap >= 0.06:
            fix = (
                "Use a structural poster rewrite, not only incremental bbox "
                "growth: reserve a compact title/thesis band, make the main "
                "method/results/qualitative panels primarily visual, shrink "
                "or delete low-value body copy, and keep captions attached to "
                "their figure/table slots."
            )
        else:
            fix = (
                "Near target: preserve information contracts while shifting "
                "more panel area to source figures/tables and shortening "
                "secondary text."
            )
        findings.append(_finding(
            severity,
            "paper-visual-area-low",
            (
                f"Paper poster visual area is {visual_area_ratio:.2f}; "
                f"target is {target_ratio:.2f}."
            ),
            fix,
            (
                f"visual_area_ratio={visual_area_ratio:.3f}; "
                f"target={target_ratio:.3f}; p0_threshold={p0_target_ratio:.3f}; "
                f"gap={gap:.3f}"
            ),
        ))

    missing_assets = [
        str(L.get("layer_id") or L.get("name") or "?")
        for L in placed_visuals
        if _visual_asset_missing(L)
    ]
    if missing_assets:
        findings.append(_finding(
            "P0",
            "paper-visual-asset-missing",
            "One or more placed paper visuals have no loadable image/table asset.",
            "Reuse registered ingest_fig_NN/ingest_table_NN layer ids and keep src_path hydrated.",
            ", ".join(missing_assets[:6]),
        ))

    available_categories = _paper_visual_categories(available_visuals)
    placed_categories = _paper_visual_categories(placed_visuals)
    if "method" in available_categories and "method" not in placed_categories:
        findings.append(_finding(
            "P0",
            "paper-method-visual-missing",
            "A method/overview figure is available but not placed on the poster.",
            "Place one method, architecture, pipeline, or overview figure prominently.",
            ", ".join(sorted(available_categories.get("method", []))[:4]),
        ))
    if "evidence" in available_categories and "evidence" not in placed_categories:
        findings.append(_finding(
            "P0",
            "paper-evidence-visual-missing",
            "Evidence/result visuals are available but none are placed on the poster.",
            "Place benchmark, qualitative, ablation, result, or table evidence visuals.",
            ", ".join(sorted(available_categories.get("evidence", []))[:4]),
        ))

    long_text_layers = [
        str(L.get("layer_id") or L.get("name") or "?")
        for L in layers
        if L.get("kind") == "text" and len(_words(L.get("text") or "")) >= 90
    ]
    if len(long_text_layers) >= 2:
        findings.append(_finding(
            "P1",
            "paper-long-body-blocks",
            "Poster has multiple long text blocks, which lowers information scan density.",
            "Split long paragraphs into short claims, captions, or callouts beside or below figures.",
            ", ".join(long_text_layers[:6]),
        ))

    aspect_mismatches = _paper_visual_aspect_mismatches(placed_visuals)
    if aspect_mismatches:
        findings.append(_finding(
            "P1",
            "paper-visual-aspect-mismatch",
            "Some paper visuals are placed in slots with severe aspect mismatch.",
            "Resize slots to match the source figure/table aspect or use a different candidate.",
            ", ".join(aspect_mismatches[:5]),
        ))

    large_gap = _largest_internal_vertical_gap(layers, ch)
    if is_paper_poster and large_gap >= 0.18:
        findings.append(_finding(
            "P2",
            "paper-empty-band",
            f"Poster has an internal empty vertical band covering {large_gap:.0%} of height.",
            "Redistribute visual panels or captions to fill the empty band.",
            f"gap_ratio={large_gap:.3f}",
        ))

    p0_count = count_p0_findings(findings)
    return {
        "paper_density_findings": findings,
        "paper_density_p0_count": p0_count,
        "placed_visual_count": len(placed_visuals),
        "placed_table_count": placed_table_count,
        "available_visual_count": available_count,
        "visual_area_ratio": round(visual_area_ratio, 4),
        "paper_density_required_visual_count": required_count,
        "paper_density_max_visual_count": (
            min(contract_max, available_count) if contract_max > 0 and available_count > 0 else contract_max
        ),
        "paper_density_target_visual_area_ratio": target_ratio,
        "paper_density_p0_visual_area_ratio": p0_target_ratio,
        "paper_density_canvas_class": (
            "conference-poster-portrait"
            if is_a0 or is_portrait_academic else
            "academic-wide-3280x1860"
            if is_wide_academic else
            "academic-landscape-1.414"
            if is_landscape_academic else
            "compact-poster"
        ),
        "paper_density_reference_profile": contract.get("reference_profile"),
        "paper_density_dense_content_fill_mode": dense_content_fill_mode,
    }


def _mixed_panel_visual_area_target(
    layers: list[dict[str, Any]],
    placed_visuals: list[dict[str, Any]],
    *,
    is_a0: bool,
    is_portrait_academic: bool,
    is_wide_academic: bool,
    is_landscape_academic: bool,
) -> float:
    """Return a lower visual-area target when paper synthesis is doing real work.

    Academic posters should not be forced into screenshot walls. If the layout
    already has enough concise visual interpretation, method notes, short result discussion, and dense
    source-backed text, a mixed-panel poster with about 28-34% visual area can
    be preferable to a 50% visual wall.
    """
    if (
        not (is_a0 or is_portrait_academic or is_wide_academic or is_landscape_academic)
        or len(placed_visuals) < 4
    ):
        return 1.0

    visible_texts = _paper_visible_texts(layers)
    visible_words = sum(len(_words(text)) for text in visible_texts)
    info_units = sum(_information_unit_count(text) for text in visible_texts)
    caption_counts = [
        len(_words(_paper_visual_caption(layer, layers)))
        for layer in placed_visuals
    ]
    valid_caption_count = sum(1 for count in caption_counts if count >= 8)
    caption_coverage = valid_caption_count / max(1, len(caption_counts))
    method_callouts = _method_callout_count(layers)
    evidence_bullets = _evidence_bullet_count(layers)

    if is_a0:
        if (
            len(placed_visuals) >= 6
            and visible_words >= 500
            and info_units >= 24
            and caption_coverage >= 0.65
            and method_callouts + evidence_bullets >= 8
        ):
            return 0.28
        if (
            len(placed_visuals) >= 5
            and visible_words >= 420
            and info_units >= 18
            and caption_coverage >= 0.45
            and method_callouts + evidence_bullets >= 5
        ):
            return 0.34

    if (
        len(placed_visuals) >= 6
        and visible_words >= 380
        and info_units >= 20
        and caption_coverage >= 0.75
        and method_callouts >= 2
        and evidence_bullets >= 4
    ):
        return 0.28 if is_wide_academic else 0.30
    if (
        len(placed_visuals) >= 5
        and visible_words >= 300
        and info_units >= 16
        and caption_coverage >= 0.55
        and method_callouts + evidence_bullets >= 5
    ):
        return 0.34 if is_wide_academic else 0.36
    return 1.0


def audit_paper_poster_information(
    layers: list[dict[str, Any]],
    canvas: dict[str, Any],
    *,
    rendered_layers: dict[str, dict[str, Any]] | None = None,
    poster_plan_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return deterministic information-density findings for paper posters.

    This complements visual density: figures can be plentiful while the poster
    still underserves the paper's claims. The audit treats dense edited panel
    text as the primary hard signal because the expensive human work is
    condensing the paper into filled, source-backed boxes; screenshot area is
    only supporting evidence.
    """
    cw = _to_int(canvas.get("w_px"), 0)
    ch = _to_int(canvas.get("h_px"), 0)
    rendered_layers = rendered_layers or {}
    contract = poster_plan_contract if isinstance(poster_plan_contract, dict) else {}
    native_targets = (
        contract.get("native_information_targets")
        if isinstance(contract.get("native_information_targets"), dict)
        else {}
    )
    reference_profile = str(contract.get("reference_profile") or "")
    editorial_flow_mode = reference_profile == "conference_editorial_flow"
    dense_content_fill_mode = reference_profile == "research_synthesis_dense"

    placed_visuals = [L for L in layers if _is_paper_visual_layer(L)]
    available_visuals = [
        rec for rec in rendered_layers.values()
        if _is_paper_visual_layer(rec)
    ]
    is_paper_poster = bool(placed_visuals or available_visuals)
    is_a0 = cw >= 3000 and ch >= 4500
    aspect = cw / max(1, ch)
    is_portrait_academic = cw >= 2000 and ch >= 2800 and 0.62 <= aspect <= 0.82
    is_wide_academic = cw >= 2800 and ch >= 1400 and aspect >= 1.6
    is_landscape_academic = cw >= 2800 and ch >= 1800 and 1.25 <= aspect < 1.6
    is_academic_poster = (
        is_a0 or is_portrait_academic or is_wide_academic or is_landscape_academic
    )

    visible_texts = _paper_visible_texts(layers)
    visible_text_word_count = sum(len(_words(text)) for text in visible_texts)
    text_info_units = sum(_information_unit_count(text) for text in visible_texts)

    caption_counts = [] if editorial_flow_mode else [
        len(_words(_paper_visual_caption(layer, layers)))
        for layer in placed_visuals
    ]
    caption_word_count_avg = (
        round(sum(caption_counts) / len(caption_counts), 1)
        if caption_counts else 0.0
    )
    valid_caption_count = sum(1 for count in caption_counts if count >= 10)
    caption_coverage_ratio = (
        1.0 if editorial_flow_mode and placed_visuals else
        round(valid_caption_count / len(caption_counts), 4)
        if caption_counts else 0.0
    )
    caption_info_units = 0 if editorial_flow_mode else valid_caption_count

    method_callout_count = _method_callout_count(layers)
    evidence_bullet_count = _evidence_bullet_count(layers)
    conclusion_or_limitation_present = _has_conclusion_or_limitation(layers)
    paper_info_unit_count = text_info_units + caption_info_units

    findings: list[dict[str, Any]] = []
    if is_paper_poster and is_academic_poster:
        target_info_units = 24 if is_a0 else 18
        target_method_callouts = 5 if is_a0 else 4
        target_evidence_bullets = 8 if is_a0 else 6
        target_visible_words = 800 if is_a0 else 600
        if dense_content_fill_mode:
            target_info_units = max(
                target_info_units,
                _to_int(native_targets.get("min_native_information_units"), target_info_units),
            )
            target_visible_words = max(
                target_visible_words,
                _to_int(native_targets.get("min_visible_words"), target_visible_words),
            )
        if paper_info_unit_count < target_info_units:
            findings.append(_finding(
                "P0",
                "paper-info-unit-count-low",
                (
                    f"Paper poster has {paper_info_unit_count} detected "
                    f"information units; target is at least {target_info_units}."
                ),
                (
                    "Add sourced method callouts, evidence bullets, figure "
                    "local readouts, and compact takeaway/limitation text."
                ),
                f"info_units={paper_info_unit_count}",
            ))
        if method_callout_count < target_method_callouts:
            findings.append(_finding(
                "P0",
                "paper-method-callouts-low",
                (
                    f"Paper poster has {method_callout_count} method callouts; "
                    f"target is at least {target_method_callouts}."
                ),
                (
                    "Add short labels beside or below the method/overview figure that "
                    "explain components, data flow, objectives, or losses."
                ),
                f"method_callouts={method_callout_count}",
            ))
        if evidence_bullet_count < target_evidence_bullets:
            findings.append(_finding(
                "P0",
                "paper-evidence-bullets-low",
                (
                    f"Paper poster has {evidence_bullet_count} evidence bullets; "
                    f"target is at least {target_evidence_bullets}."
                ),
                (
                    "Add sourced result, ablation, qualitative, benchmark, "
                    "or figure-referenced evidence bullets near the visuals."
                ),
                f"evidence_bullets={evidence_bullet_count}",
            ))
        if placed_visuals and not editorial_flow_mode and caption_coverage_ratio < 0.5:
            findings.append(_finding(
                "P0",
                "paper-caption-coverage-low",
                (
                    f"Only {caption_coverage_ratio:.0%} of placed paper visuals "
                    "have local explanatory text with at least 10 words."
                ),
                (
                    "Replace bare Fig./Table labels with sourced local prose "
                    "that states what each figure/table proves."
                ),
                (
                    f"valid_captions={valid_caption_count} "
                    f"placed_visuals={len(placed_visuals)}"
                ),
            ))
        if visible_text_word_count < target_visible_words:
            findings.append(_finding(
                "P0",
                "paper-visible-text-low",
                (
                    f"Paper poster has {visible_text_word_count} visible text "
                    f"words; target is at least {target_visible_words} "
                    "for this dense academic board."
                ),
                (
                    "Fill each box with paper-faithful synthesis: short claims, "
                    "local figure explanations, result interpretation, method "
                    "callouts, and limitations rather than relying on screenshots."
                ),
                f"visible_words={visible_text_word_count}",
            ))
        if not conclusion_or_limitation_present:
            findings.append(_finding(
                "P1",
                "paper-conclusion-limitation-missing",
                "Paper poster lacks a detectable conclusion, takeaway, or limitation block.",
                "Add a compact conclusion/takeaway plus limitation or failure-mode note.",
                "missing conclusion/takeaway/limitation keyword",
            ))

    p0_count = count_p0_findings(findings)
    return {
        "paper_information_findings": findings,
        "paper_information_p0_count": p0_count,
        "paper_info_unit_count": paper_info_unit_count,
        "paper_information_target_info_units": target_info_units if is_paper_poster and is_academic_poster else None,
        "paper_information_target_visible_words": target_visible_words if is_paper_poster and is_academic_poster else None,
        "visible_text_word_count": visible_text_word_count,
        "caption_word_count_avg": caption_word_count_avg,
        "caption_coverage_ratio": caption_coverage_ratio,
        "method_callout_count": method_callout_count,
        "evidence_bullet_count": evidence_bullet_count,
        "conclusion_or_limitation_present": conclusion_or_limitation_present,
        "paper_information_reference_profile": contract.get("reference_profile"),
        "paper_information_dense_content_fill_mode": dense_content_fill_mode,
    }


_METHOD_KEYWORDS = (
    "method", "overview", "architecture", "pipeline", "framework", "model",
    "algorithm", "approach", "system", "flow", "motion",
)
_EVIDENCE_KEYWORDS = (
    "result", "benchmark", "experiment", "evaluation", "qualitative",
    "comparison", "ablation", "performance", "metric", "table", "accuracy",
    "score", "baseline", "state-of-the-art", "sota",
)


def _is_paper_visual_layer(layer: dict[str, Any]) -> bool:
    kind = str(layer.get("kind") or "")
    if kind not in ("image", "table"):
        return False
    layer_id = str(layer.get("layer_id") or "")
    source = str(layer.get("source") or "")
    source_id = str(
        layer.get("source_id")
        or layer.get("data_source_id")
        or layer.get("sourceId")
        or ""
    )
    return (
        source == "ingested_pdf"
        or layer_id.startswith("ingest_fig_")
        or layer_id.startswith("ingest_table_")
        or source_id.startswith("ingest_fig_")
        or source_id.startswith("ingest_table_")
    )


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clipped_bbox_area(bbox: dict[str, Any], cw: int, ch: int) -> int:
    x = _to_int(bbox.get("x"), 0)
    y = _to_int(bbox.get("y"), 0)
    w = _to_int(bbox.get("w"), 0)
    h = _to_int(bbox.get("h"), 0)
    if w <= 0 or h <= 0 or cw <= 0 or ch <= 0:
        return 0
    x1 = max(0, min(cw, x))
    y1 = max(0, min(ch, y))
    x2 = max(0, min(cw, x + w))
    y2 = max(0, min(ch, y + h))
    return max(0, x2 - x1) * max(0, y2 - y1)


def _visual_asset_missing(layer: dict[str, Any]) -> bool:
    if layer.get("kind") == "table" and (layer.get("rows") or layer.get("headers")):
        return False
    src = layer.get("src_path")
    if not src:
        return True
    try:
        return not Path(str(src)).exists()
    except OSError:
        return True


def _paper_visual_categories(
    layers: list[dict[str, Any]],
) -> dict[str, set[str]]:
    categories: dict[str, set[str]] = {}
    for layer in layers:
        text = " ".join(str(layer.get(k) or "") for k in (
            "layer_id", "name", "caption", "caption_short", "title",
        )).lower()
        layer_id = str(layer.get("layer_id") or "?")
        if any(k in text for k in _METHOD_KEYWORDS):
            categories.setdefault("method", set()).add(layer_id)
        if layer.get("kind") == "table" or any(k in text for k in _EVIDENCE_KEYWORDS):
            categories.setdefault("evidence", set()).add(layer_id)
    return categories


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9'._%-]*", text)


def _paper_visible_texts(layers: list[dict[str, Any]]) -> list[str]:
    explicit_texts: list[str] = []
    group_texts: list[str] = []
    table_texts: list[str] = []
    for layer in layers:
        kind = str(layer.get("kind") or "")
        if kind in {"text", "caption", "metric", "quote"}:
            text = str(layer.get("text") or "")
            if text.strip():
                explicit_texts.append(text)
        elif kind == "callout":
            text = str(layer.get("callout_text") or layer.get("text") or "")
            if text.strip():
                explicit_texts.append(text)
        elif kind == "table":
            text = str(layer.get("text") or layer.get("caption") or layer.get("title") or "")
            if text.strip():
                table_texts.append(text)
        elif kind == "group":
            text = str(layer.get("text") or "")
            if len(_words(text)) >= 4:
                group_texts.append(text)
    explicit_word_count = sum(len(_words(text)) for text in explicit_texts + table_texts)
    group_word_count = sum(len(_words(text)) for text in group_texts)
    if group_texts and (explicit_word_count == 0 or group_word_count > max(120, int(explicit_word_count * 1.5))):
        return group_texts
    return explicit_texts + table_texts


def _paper_visual_caption(
    layer: dict[str, Any],
    layers: list[dict[str, Any]] | None = None,
) -> str:
    direct = str(layer.get("caption") or layer.get("title") or "").strip()
    if direct:
        return direct
    if not layers:
        return ""
    slot_id = str(layer.get("slot_id") or "").strip()
    layer_box = layer.get("bbox") if isinstance(layer.get("bbox"), dict) else {}
    candidates: list[str] = []
    for other in layers:
        if other is layer:
            continue
        if str(other.get("kind") or "") not in {"text", "caption", "metric", "quote", "group"}:
            continue
        role_blob = " ".join(str(other.get(k) or "") for k in (
            "role", "name", "layer_id", "panel_role",
        )).lower()
        text = str(other.get("text") or other.get("caption") or "").strip()
        if not text:
            continue
        same_slot = slot_id and str(other.get("slot_id") or "").strip() == slot_id
        captionish = (
            any(token in role_blob for token in (
                "caption",
                "readout",
                "reading",
                "explanation",
                "interpret",
                "takeaway",
                "source-flow",
                "figure-flow",
            ))
            or re.search(r"\b(?:fig|figure|table)\.?\s*\d+", text.lower())
        )
        adjacent = _caption_adjacent_to_visual(layer_box, other.get("bbox"))
        if captionish and (same_slot or adjacent):
            candidates.append(text)
    if not candidates:
        return ""
    return max(candidates, key=lambda value: len(_words(value)))


def _caption_adjacent_to_visual(
    visual_bbox: Any,
    caption_bbox: Any,
) -> bool:
    if not isinstance(visual_bbox, dict) or not isinstance(caption_bbox, dict):
        return False
    vx = _to_int(visual_bbox.get("x"), 0)
    vy = _to_int(visual_bbox.get("y"), 0)
    vw = _to_int(visual_bbox.get("w"), 0)
    vh = _to_int(visual_bbox.get("h"), 0)
    cx = _to_int(caption_bbox.get("x"), 0)
    cy = _to_int(caption_bbox.get("y"), 0)
    cw = _to_int(caption_bbox.get("w"), 0)
    ch = _to_int(caption_bbox.get("h"), 0)
    if min(vw, vh, cw, ch) <= 0:
        return False
    overlap_w = max(0, min(vx + vw, cx + cw) - max(vx, cx))
    overlap_h = max(0, min(vy + vh, cy + ch) - max(vy, cy))
    if overlap_w * overlap_h >= min(vw * vh, cw * ch) * 0.35:
        return True
    x_overlap = max(0, min(vx + vw, cx + cw) - max(vx, cx))
    if x_overlap < min(vw, cw) * 0.35:
        return False
    vertical_gap = min(abs(cy - (vy + vh)), abs(vy - (cy + ch)))
    return vertical_gap <= 180


def _information_segments(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", str(text).strip())
    if not normalized:
        return []
    normalized = re.sub(r"\b(Fig|fig|Table|table)\.\s*", r"\1 ", normalized)
    pieces = re.split(r"(?:\s*[•·]\s*|\n+|(?<=[.;:!?])\s+)", normalized)
    return [piece.strip(" -–—\t") for piece in pieces if piece.strip(" -–—\t")]


def _information_unit_count(text: str) -> int:
    return sum(1 for segment in _information_segments(text)
               if len(_words(segment)) >= 4)


_METHOD_CALLOUT_KEYWORDS = _METHOD_KEYWORDS + (
    "encoder", "decoder", "conditioning", "module", "loss", "objective",
    "representation", "embedding", "training", "inference", "token",
)
_CONCLUSION_KEYWORDS = (
    "conclusion", "takeaway", "takeaways", "limitation", "limitations",
    "failure", "future", "caveat", "contribution", "contributions",
)


def _method_callout_count(layers: list[dict[str, Any]]) -> int:
    count = 0
    for raw in _paper_visible_texts(layers):
        for segment in _information_segments(raw):
            words = _words(segment)
            if 2 <= len(words) <= 35 and any(
                keyword in segment.lower()
                for keyword in _METHOD_CALLOUT_KEYWORDS
            ):
                count += 1
    return count


def _evidence_bullet_count(layers: list[dict[str, Any]]) -> int:
    count = 0
    for raw in _paper_visible_texts(layers):
        for segment in _information_segments(raw):
            lower = segment.lower()
            words = _words(segment)
            if len(words) < 4:
                continue
            has_evidence_term = any(
                keyword in lower for keyword in _EVIDENCE_KEYWORDS
            )
            has_fig_ref = bool(re.search(r"\b(?:fig(?:ure)?|table)\.?\s*\d+\b", lower))
            has_metric = bool(re.search(r"\b\d+(?:\.\d+)?\s*(?:%|x|×|pts?|score|frames?)\b", lower))
            if has_evidence_term or has_fig_ref or has_metric:
                count += 1
    return count


def _has_conclusion_or_limitation(layers: list[dict[str, Any]]) -> bool:
    for raw_text in _paper_visible_texts(layers):
        raw = raw_text.lower()
        if any(keyword in raw for keyword in _CONCLUSION_KEYWORDS):
            return True
    return False


def _paper_visual_aspect_mismatches(layers: list[dict[str, Any]]) -> list[str]:
    misses: list[str] = []
    for layer in layers:
        bbox = layer.get("bbox") or {}
        bw = _to_int(bbox.get("w"), 0)
        bh = _to_int(bbox.get("h"), 0)
        if bw <= 0 or bh <= 0:
            continue
        source_aspect = _aspect_from_record(layer)
        if source_aspect <= 0:
            continue
        slot_aspect = bw / float(bh)
        ratio = max(slot_aspect / source_aspect, source_aspect / slot_aspect)
        if ratio >= 2.4:
            misses.append(f"{layer.get('layer_id', '?')}:{ratio:.1f}x")
    return misses


def _aspect_from_record(layer: dict[str, Any]) -> float:
    size = str(layer.get("image_size") or "")
    match = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", size)
    if match:
        w = int(match.group(1))
        h = int(match.group(2))
        if w > 0 and h > 0:
            return w / float(h)
    aspect = str(layer.get("aspect_ratio") or "")
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*$", aspect)
    if match:
        w = float(match.group(1))
        h = float(match.group(2))
        if w > 0 and h > 0:
            return w / h
    return 0.0


def _largest_internal_vertical_gap(layers: list[dict[str, Any]], ch: int) -> float:
    if ch <= 0:
        return 0.0
    intervals: list[tuple[int, int]] = []
    for layer in layers:
        if str(layer.get("kind") or "") == "background":
            continue
        bbox = layer.get("bbox") or {}
        y = _to_int(bbox.get("y"), 0)
        h = _to_int(bbox.get("h"), 0)
        if h <= 0:
            continue
        intervals.append((max(0, y), min(ch, y + h)))
    if not intervals:
        return 1.0
    intervals.sort()
    merged: list[list[int]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    largest = 0
    for (_, prev_end), (next_start, _) in zip(merged, merged[1:]):
        # Ignore deliberate title/footer breathing room at the edges.
        if prev_end < 0.08 * ch or next_start > 0.94 * ch:
            continue
        largest = max(largest, next_start - prev_end)
    return largest / float(ch)


def _append_gradient_findings(html: str, findings: list[dict[str, Any]]) -> None:
    for gradient in re.findall(r"linear-gradient\([^)]*\)", html, flags=re.I):
        lower = gradient.lower()
        if any(hex_value in lower for hex_value in PURPLE_HEXES) or re.search(r"\b(purple|violet|indigo)\b", lower):
            findings.append(_finding(
                "P0",
                "purple-gradient",
                "Violet/purple gradient background is an AI-slop tell.",
                "Use a flat surface or one intentional accent from the visual profile.",
                gradient,
            ))
            return
        has_blue = any(hex_value in lower for hex_value in TRUST_BLUE_HEXES) or re.search(r"\b(blue|sky)\b", lower)
        has_cyan = any(hex_value in lower for hex_value in TRUST_CYAN_HEXES) or re.search(r"\b(cyan|teal)\b", lower)
        if has_blue and has_cyan:
            findings.append(_finding(
                "P0",
                "trust-gradient",
                "Blue-to-cyan trust gradient is a generic SaaS hero pattern.",
                "Use a flat surface or one design-token color instead.",
                gradient,
            ))
            return


def _append_ai_indigo_finding(html: str, findings: list[dict[str, Any]]) -> None:
    scoped = _strip_global_token_blocks(html)
    lower = scoped.lower()
    selected_variable_spans = _selected_academic_palette_variable_spans(scoped)
    for hex_value in AI_DEFAULT_PURPLE_INDIGO:
        start = 0
        while True:
            idx = lower.find(hex_value, start)
            if idx < 0:
                break
            end = idx + len(hex_value)
            if _span_inside_any(idx, end, selected_variable_spans):
                start = end
                continue
            findings.append(_finding(
                "P0",
                "ai-default-indigo",
                f"Default LLM purple/indigo color found: {hex_value}.",
                "Replace with the selected visual profile or active design-system accent.",
                scoped[idx:end],
            ))
            return


_POSTER_CSS_VAR_BY_ROLE = {
    "background": "--poster-bg",
    "text": "--poster-text",
    "primary": "--poster-primary",
    "secondary": "--poster-secondary",
    "accent": "--poster-accent",
    "header_text": "--poster-header-text",
    "bar": "--poster-bar",
}


def _selected_academic_palette_variable_spans(html: str) -> list[tuple[int, int]]:
    known_palette_vars = _known_academic_palette_css_variable_sets()
    if not known_palette_vars:
        return []
    spans: list[tuple[int, int]] = []
    spans.extend(_selected_academic_palette_css_block_spans(html, known_palette_vars))
    spans.extend(_selected_academic_palette_inline_style_spans(html, known_palette_vars))
    return spans


def _selected_academic_palette_css_block_spans(
    html: str,
    known_palette_vars: list[dict[str, str]],
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for block in re.finditer(r"(?is)([^{}]*\.paper-poster[^{}]*)\{([^{}]*)\}", html):
        selector = block.group(1)
        if not _selector_targets_paper_poster(selector):
            continue
        declarations = block.group(2)
        declaration_start = block.start(2)
        spans.extend(_academic_palette_declaration_value_spans(declarations, declaration_start, known_palette_vars))
    return spans


def _selected_academic_palette_inline_style_spans(
    html: str,
    known_palette_vars: list[dict[str, str]],
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for tag in re.finditer(r"(?is)<[a-z][^>]*>", html):
        attrs = tag.group(0)
        if not _tag_has_paper_poster_class(attrs):
            continue
        style = re.search(r"(?is)\bstyle\s*=\s*(['\"])(.*?)\1", attrs)
        if not style:
            continue
        declarations = style.group(2)
        declaration_start = tag.start() + style.start(2)
        spans.extend(_academic_palette_declaration_value_spans(declarations, declaration_start, known_palette_vars))
    return spans


def _academic_palette_declaration_value_spans(
    declarations: str,
    declaration_start: int,
    known_palette_vars: list[dict[str, str]],
) -> list[tuple[int, int]]:
    declarations_found: list[tuple[str, str, int, int]] = []
    for declaration in re.finditer(r"(?i)(--poster-[a-z-]+)\s*:\s*(#[0-9a-f]{6})", declarations):
        var_name = declaration.group(1).lower()
        value = declaration.group(2).upper()
        declarations_found.append((var_name, value, declaration.start(2), declaration.end(2)))
    if not declarations_found:
        return []
    declared = {var_name: value for var_name, value, _, _ in declarations_found}
    matching_palette: dict[str, str] | None = None
    for palette_vars in known_palette_vars:
        if not all(declared.get(var_name) == expected for var_name, expected in palette_vars.items()):
            continue
        if any(var_name not in palette_vars or value != palette_vars.get(var_name) for var_name, value in declared.items()):
            continue
        matching_palette = palette_vars
        break
    if not matching_palette:
        return []
    return [
        (declaration_start + start, declaration_start + end)
        for var_name, value, start, end in declarations_found
        if matching_palette.get(var_name) == value
    ]


def _known_academic_palette_css_variable_sets() -> list[dict[str, str]]:
    library = load_academic_palette_library()
    palettes = list(library.get("palettes") or [])
    known: list[dict[str, str]] = []
    for palette in palettes:
        if not isinstance(palette, dict):
            continue
        roles = palette.get("roles") if isinstance(palette.get("roles"), dict) else {}
        variables: dict[str, str] = {}
        for role, var_name in _POSTER_CSS_VAR_BY_ROLE.items():
            value = str(roles.get(role) or "").strip().upper()
            if not re.fullmatch(r"#[0-9A-F]{6}", value):
                continue
            variables[var_name] = value
        if set(variables) == set(_POSTER_CSS_VAR_BY_ROLE.values()):
            known.append(variables)
    return known


def _selector_targets_paper_poster(selector: str) -> bool:
    return bool(re.search(r"(?<![\w-])\.paper-poster(?![\w-])", selector))


def _tag_has_paper_poster_class(attrs: str) -> bool:
    match = re.search(r"(?is)\bclass\s*=\s*(['\"])(.*?)\1", attrs)
    if not match:
        return False
    classes = re.split(r"\s+", match.group(2).strip())
    return "paper-poster" in classes


def _span_inside_any(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(span_start <= start and end <= span_end for span_start, span_end in spans)


def _append_emoji_finding(html: str, findings: list[dict[str, Any]]) -> None:
    for emoji in SLOP_EMOJI:
        if emoji not in html:
            continue
        match = re.search(
            rf"<(?:h[1-6]|button|li|span[^>]*class=[\"'][^\"']*icon[^\"']*[\"'])[^>]*>[^<]*{re.escape(emoji)}",
            html,
            flags=re.I,
        )
        if match:
            findings.append(_finding(
                "P0",
                "emoji-icon",
                f"Emoji {emoji!r} used as a UI icon.",
                "Replace it with a restrained inline SVG, text label, or no icon.",
                match.group(0),
            ))
            return


def _append_left_accent_card_finding(html: str, findings: list[dict[str, Any]]) -> None:
    match = re.search(
        r"\.[a-zA-Z0-9_-]+\s*\{[^}]*border-left\s*:\s*\d+px\s+solid\s+[^;]+;[^}]*border-radius\s*:\s*[1-9]",
        html,
        flags=re.I,
    )
    if not match:
        return
    findings.append(_finding(
        "P0",
        "left-accent-card",
        "Rounded card with colored left border is a generic AI dashboard pattern.",
        "Drop the radius or the left accent; use full hairline borders or spacing.",
        match.group(0),
    ))


def _append_pattern_finding(
    html: str,
    findings: list[dict[str, Any]],
    patterns: tuple[re.Pattern[str], ...],
    id_: str,
    severity: LintSeverity,
    message: str,
    fix: str,
) -> None:
    for pattern in patterns:
        match = pattern.search(html)
        if match:
            findings.append(_finding(severity, id_, message, fix, match.group(0)))
            return


def _append_external_placeholder_finding(html: str, findings: list[dict[str, Any]]) -> None:
    match = re.search(r"https?://(?:[^\"'\s>]*)(?:unsplash\.com|placehold\.co|picsum\.photos)", html, flags=re.I)
    if not match:
        return
    findings.append(_finding(
        "P1",
        "external-placeholder-image",
        "External placeholder image CDN found.",
        "Use a local/generated asset, an ingested figure, or an honest placeholder block.",
        match.group(0),
    ))


def _append_accent_overuse_finding(html: str, findings: list[dict[str, Any]]) -> None:
    count = len(re.findall(r"var\(\s*--(?:ld-)?accent\s*\)", html, flags=re.I))
    if count < 6:
        return
    findings.append(_finding(
        "P1",
        "accent-overuse",
        f"Accent token appears {count} times.",
        "Reserve accent for the primary CTA, one data point, or one editorial flourish.",
        "var(--accent)",
    ))


def _finding(
    severity: LintSeverity,
    id_: str,
    message: str,
    fix: str,
    snippet: str,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "id": id_,
        "message": message,
        "fix": fix,
        "snippet": _clip(snippet),
    }


def _clip(snippet: str, limit: int = 220) -> str:
    clean = " ".join(str(snippet).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "..."


def _strip_global_token_blocks(html: str) -> str:
    """Remove global token declarations so intentional profile accents pass."""
    # Keep this selector-specific. A broad "any selector block with --tokens"
    # regex can spend too long scanning HTML with large inline data URIs.
    return re.sub(
        r"(?is)(?::root|html|body|[^{]*\[data-theme[^{]*)\s*\{[^{}]*--[^{}]*\}",
        "",
        html,
    )


def _strip_heavy_inline_assets(html: str) -> str:
    """Remove heavyweight inline assets before style/copy lint regexes."""
    return re.sub(
        r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+",
        "data:image/omitted",
        html,
    )


def _visible_text(html: str) -> str:
    """Best-effort visible text extraction for copy-only lint rules."""
    without_scripts = re.sub(r"<(script|style)\b[\s\S]*?</\1>", " ", html, flags=re.I)
    without_tags = re.sub(r"<[^>]+>", " ", without_scripts)
    return " ".join(without_tags.split())
