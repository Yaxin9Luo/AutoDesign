"""Academic paper-poster color palette selection."""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any


PALETTE_ASSET_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "poster"
    / "visual_recipe"
    / "assets"
    / "academic_color_palettes.json"
)
DEFAULT_PALETTE_ID = "bright_cobalt"
USAGE_CONTRACT = (
    'Use the selected palette sparingly. The identity header uses the fixed '
    'white/near-white treatment with a single top accent rule only; do not '
    'use filled title bands, four-sided outlines, or mixed header styles for new '
    'paper posters. Use palette color elsewhere for compact filled section title '
    'bands with white text, thin dividers, and a few lead-key accents; keep panel interiors, '
    'table cells, and ordinary readouts white or neutral. Keep source figure/table wrapper '
    'DOM boxes for measurement, but make wrapper borders transparent with no visible outline or shadow.'
)
_CSS_VAR_BY_ROLE = {
    "background": "--poster-bg",
    "text": "--poster-text",
    "primary": "--poster-primary",
    "secondary": "--poster-secondary",
    "accent": "--poster-accent",
    "header_text": "--poster-header-text",
    "bar": "--poster-bar",
}
_FALLBACK_PALETTES: list[dict[str, Any]] = [{'avoid_keywords': ['avoid cardinal red', 'avoid red'],
  'domain_tags': ['general', 'conference', 'formal'],
  'id': 'bright_cobalt',
  'name': 'Cardinal Red',
  'roles': {'accent': '#C1121F',
            'background': '#FFFFFF',
            'bar': '#C1121F',
            'header_text': '#FFFFFF',
            'primary': '#C1121F',
            'secondary': '#F7DEE1',
            'text': '#21181B'},
  'selection_keywords': ['conference',
                         'academic poster',
                         'formal poster',
                         'sectioning',
                         'strong red',
                         'cardinal red',
                         'reference poster'],
  'tone_tags': ['strong', 'formal', 'high visibility', 'conference'],
  'use_when': 'Strong red sectioning, closest to the reference conference posters.'},
 {'avoid_keywords': ['avoid iclr purple', 'avoid purple'],
  'domain_tags': ['theory', 'optimization', 'algorithms', 'math'],
  'id': 'teal_coral',
  'name': 'ICLR Purple',
  'roles': {'accent': '#6F2DA8',
            'background': '#FFFFFF',
            'bar': '#6F2DA8',
            'header_text': '#FFFFFF',
            'primary': '#6F2DA8',
            'secondary': '#E8DCF7',
            'text': '#211927'},
  'selection_keywords': ['iclr',
                         'purple',
                         'theory',
                         'optimization',
                         'graph',
                         'algorithm',
                         'proof',
                         'theorem',
                         'complexity',
                         'self-supervised',
                         'self supervised',
                         'pretraining',
                         'pre-training',
                         'rotation',
                         'patch rotation',
                         'patch token',
                         'patchrot',
                         'figures already carry color',
                         'iclr purple',
                         'high contrast',
                         'saturated',
                         'bright color'],
  'tone_tags': ['restrained', 'purple', 'plain', 'academic', 'saturated', 'high contrast'],
  'use_when': 'High-contrast purple identity without filling content panels; useful when figures '
              'already carry color.'},
 {'avoid_keywords': ['avoid teal'],
  'domain_tags': ['applied_ai', 'systems', 'visualization'],
  'id': 'plum_sage',
  'name': 'Teal',
  'roles': {'accent': '#007E78',
            'background': '#FFFFFF',
            'bar': '#007E78',
            'header_text': '#FFFFFF',
            'primary': '#007E78',
            'secondary': '#D8F1EF',
            'text': '#142625'},
  'selection_keywords': ['teal',
                         'technical',
                         'fresh',
                         'method',
                         'systems',
                         'applied ai',
                         'visualization',
                         'multimodal',
                         'agent',
                         'high contrast',
                         'saturated',
                         'bright color'],
  'tone_tags': ['fresh', 'technical', 'strong', 'saturated', 'high contrast'],
  'use_when': 'Saturated technical teal with a strong header-rule accent and high contrast.'},
 {'avoid_keywords': ['avoid royal blue', 'avoid blue'],
  'domain_tags': ['nlp', 'language_models', 'ai'],
  'id': 'tangerine_blue',
  'name': 'Royal Blue',
  'roles': {'accent': '#285BB8',
            'background': '#FFFFFF',
            'bar': '#285BB8',
            'header_text': '#FFFFFF',
            'primary': '#285BB8',
            'secondary': '#E0E7FA',
            'text': '#17223A'},
  'selection_keywords': ['royal blue',
                         'language',
                         'nlp',
                         'llm',
                         'large language',
                         'transformer',
                         'attention',
                         'reasoning',
                         'rag',
                         'retrieval',
                         'benchmark',
                         'high contrast',
                         'saturated',
                         'bright color'],
  'tone_tags': ['safe', 'technical', 'restrained', 'saturated', 'high contrast'],
  'use_when': 'Saturated AI blue with strong conference visibility.'},
 {'avoid_keywords': ['avoid emerald', 'avoid green'],
  'domain_tags': ['life_science', 'health', 'applied_science'],
  'id': 'blossom_teal',
  'name': 'Emerald',
  'roles': {'accent': '#007F50',
            'background': '#FFFFFF',
            'bar': '#007F50',
            'header_text': '#FFFFFF',
            'primary': '#007F50',
            'secondary': '#D9F2E7',
            'text': '#16251F'},
  'selection_keywords': ['emerald',
                         'green',
                         'bio',
                         'health',
                         'applied science',
                         'biology',
                         'medicine',
                         'ecology',
                         'high contrast',
                         'saturated',
                         'bright color'],
  'tone_tags': ['fresh', 'green', 'strong', 'saturated', 'high contrast'],
  'use_when': 'High-contrast emerald for bio, health, and applied science posters.'},
 {'avoid_keywords': ['avoid indigo'],
  'domain_tags': ['ai', 'machine_learning', 'technical'],
  'id': 'lavender_forest',
  'name': 'Indigo',
  'roles': {'accent': '#4049B8',
            'background': '#FFFFFF',
            'bar': '#4049B8',
            'header_text': '#FFFFFF',
            'primary': '#4049B8',
            'secondary': '#E0E3FA',
            'text': '#1D2034'},
  'selection_keywords': ['indigo',
                         'ai',
                         'machine learning',
                         'model',
                         'benchmark',
                         'language',
                         'cool',
                         'high contrast',
                         'saturated',
                         'bright color'],
  'tone_tags': ['cool', 'indigo', 'clean', 'saturated', 'high contrast'],
  'use_when': 'A strong AI-friendly indigo with reliable white-title contrast.'},
 {'avoid_keywords': ['avoid rosewood'],
  'domain_tags': ['medical', 'policy', 'social_science'],
  'id': 'mulberry_mint',
  'name': 'Burgundy',
  'roles': {'accent': '#A4113F',
            'background': '#FFFFFF',
            'bar': '#A4113F',
            'header_text': '#FFFFFF',
            'primary': '#A4113F',
            'secondary': '#F0D6DF',
            'text': '#24191D'},
  'selection_keywords': ['rosewood',
                         'red family',
                         'academic heading',
                         'medical',
                         'policy',
                         'social',
                         'burgundy',
                         'high contrast',
                         'saturated',
                         'bright color'],
  'tone_tags': ['restrained', 'warm', 'plain', 'saturated', 'high contrast'],
  'use_when': 'Replaces soft rosewood with a saturated red-wine academic heading.'},
 {'avoid_keywords': ['avoid navy', 'avoid blue'],
  'domain_tags': ['ai', 'machine_learning', 'technical'],
  'id': 'deep_navy',
  'name': 'Deep Navy',
  'roles': {'accent': '#123B6D',
            'background': '#FFFFFF',
            'bar': '#123B6D',
            'header_text': '#FFFFFF',
            'primary': '#123B6D',
            'secondary': '#DCE8F4',
            'text': '#111C2B'},
  'selection_keywords': ['deep navy', 'navy', 'stable ai', 'machine learning', 'technical navy'],
  'tone_tags': ['deep', 'stable', 'formal', 'high contrast'],
  'use_when': 'Stable AI/ML navy with more weight than bright blue.'},
 {'avoid_keywords': ['avoid petrol', 'avoid teal'],
  'domain_tags': ['systems', 'applied_ai', 'visualization'],
  'id': 'petrol_teal',
  'name': 'Petrol Teal',
  'roles': {'accent': '#00656D',
            'background': '#FFFFFF',
            'bar': '#00656D',
            'header_text': '#FFFFFF',
            'primary': '#00656D',
            'secondary': '#D8ECEE',
            'text': '#132426'},
  'selection_keywords': ['petrol teal', 'blue green', 'technical teal', 'serious teal'],
  'tone_tags': ['technical', 'serious', 'cool', 'high contrast'],
  'use_when': 'Serious blue-green technical palette, calmer than ordinary teal.'},
 {'avoid_keywords': ['avoid orange', 'avoid warm'],
  'domain_tags': ['general', 'applied_science', 'engineering'],
  'id': 'burnt_orange',
  'name': 'Burnt Orange',
  'roles': {'accent': '#B84A16',
            'background': '#FFFFFF',
            'bar': '#B84A16',
            'header_text': '#FFFFFF',
            'primary': '#B84A16',
            'secondary': '#F4DFD2',
            'text': '#251A13'},
  'selection_keywords': ['burnt orange', 'orange', 'warm', 'non blue', 'warm technical'],
  'tone_tags': ['warm', 'grounded', 'strong', 'high contrast'],
  'use_when': 'Warm non-blue option that stays academic when used only as structure color.'},
 {'avoid_keywords': ['avoid oxide', 'avoid red'],
  'domain_tags': ['formal', 'medical', 'general'],
  'id': 'oxide_red',
  'name': 'Oxide Red',
  'roles': {'accent': '#9E2F24',
            'background': '#FFFFFF',
            'bar': '#9E2F24',
            'header_text': '#FFFFFF',
            'primary': '#9E2F24',
            'secondary': '#EEDBD8',
            'text': '#241715'},
  'selection_keywords': ['oxide red', 'earth red', 'muted red', 'serious red'],
  'tone_tags': ['earthy', 'serious', 'warm', 'high contrast'],
  'use_when': 'Earthier red with less brightness than ruby or crimson.'},
 {'avoid_keywords': ['avoid wine', 'avoid rose'],
  'domain_tags': ['medical', 'social_science', 'policy'],
  'id': 'wine_rose',
  'name': 'Wine Rose',
  'roles': {'accent': '#8F1D4F',
            'background': '#FFFFFF',
            'bar': '#8F1D4F',
            'header_text': '#FFFFFF',
            'primary': '#8F1D4F',
            'secondary': '#EED9E3',
            'text': '#231720'},
  'selection_keywords': ['wine rose', 'wine', 'rose', 'scholarly rose'],
  'tone_tags': ['scholarly', 'warm', 'restrained', 'high contrast'],
  'use_when': 'Scholarly rose/wine tone, less loud than magenta.'},
 {'avoid_keywords': ['avoid cyan', 'avoid blue'],
  'domain_tags': ['vision', 'ai', 'computational_science'],
  'id': 'deep_cyan',
  'name': 'Deep Cyan',
  'roles': {'accent': '#006B8F',
            'background': '#FFFFFF',
            'bar': '#006B8F',
            'header_text': '#FFFFFF',
            'primary': '#006B8F',
            'secondary': '#D8EAF1',
            'text': '#132331'},
  'selection_keywords': ['deep cyan', 'cyan blue', 'cool blue', 'technical cyan'],
  'tone_tags': ['cool', 'clear', 'technical', 'high contrast'],
  'use_when': 'Cool cyan-blue with stronger contrast than pale blue.'}]
_KEYWORD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("plum_sage", (
        "cvpr", "computer vision", "segmentation", "detection",
        "visual recognition", "object detection", "instance segmentation",
        "semantic segmentation", "ocr", "图像识别", "目标检测", "分割",
    )),
    ("teal_coral", (
        "theory", "math", "mathematical", "optimization", "optimisation",
        "graph", "algorithm", "formal", "proof", "theorem", "complexity",
        "self-supervised", "self supervised", "pretraining", "pre-training",
        "rotation", "patch rotation", "patch token", "patchrot", "patch",
        "理论", "数学", "优化",
        "图算法", "算法", "证明",
    )),
    ("tangerine_blue", (
        "language", "nlp", "llm", "large language", "attention", "transformer",
        "reasoning", "agent", "retrieval", "rag", "语言", "大模型", "注意力",
    )),
    ("lavender_forest", (
        "system", "systems", "hardware", "infrastructure", "robotics", "robot",
        "training pipeline", "pipeline", "distributed", "compiler", "accelerator",
        "compute", "serving", "工程", "系统", "硬件", "机器人", "训练",
    )),
    ("blossom_teal", (
        "bio", "medical", "medicine", "clinical", "health", "biomed",
        "biomedical", "biology", "genomics", "医疗", "生物",
    )),
)
_LATIN_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)
_LATIN_PHRASE_SEPARATOR = r"[\s_\-+\/&]+"
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_NEGATION_WINDOW_CHARS = 56
_NEGATION_TOKEN_RE = re.compile(
    r"\bavoid\b|\bno\b|\bnot\b|\bwithout\b|\bdo\s+not\b|\bdon['’]?t\b|不要|避免",
    re.I,
)
_NEGATION_HARD_BOUNDARY_RE = re.compile(r"[.!?。！？\n\r;；]")
_NEGATION_CONTRAST_RE = re.compile(r"\b(?:but|however|instead|rather|choose|select|prefer)\b", re.I)
_NEGATION_USE_RE = re.compile(r"\buse\b", re.I)
_GENERIC_SELECTION_KEYWORDS = {
    "academic",
    "analysis",
    "architecture",
    "conference",
    "data",
    "dense",
    "evaluation",
    "formal",
    "method",
    "poster",
    "result",
    "results",
    "science",
    "scientific",
}


class AcademicPaletteCatalogError(RuntimeError):
    """The canonical academic palette asset is unreadable or invalid."""


def load_academic_palette_library(
    path: Path | None = None,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Load the repo palette asset, falling back to the built-in academic palettes."""
    asset_path = path or PALETTE_ASSET_PATH
    try:
        payload = json.loads(asset_path.read_text(encoding="utf-8"))
    except Exception as exc:
        if strict:
            raise AcademicPaletteCatalogError(
                f"unable to read academic palette catalog: {asset_path}"
            ) from exc
        payload = {}
    if strict:
        if not isinstance(payload, dict):
            raise AcademicPaletteCatalogError(
                f"academic palette catalog must be a JSON object: {asset_path}"
            )
        raw_palettes = payload.get("palettes")
        if not isinstance(raw_palettes, list) or not raw_palettes:
            raise AcademicPaletteCatalogError(
                f"academic palette catalog must contain a non-empty palettes list: {asset_path}"
            )
        default_id = str(payload.get("default_palette_id") or "").strip()
        if not default_id:
            raise AcademicPaletteCatalogError(
                f"academic palette catalog is missing default_palette_id: {asset_path}"
            )
        normalized: list[dict[str, Any]] = []
        ids: set[str] = set()
        required_roles = set(_CSS_VAR_BY_ROLE)
        for index, item in enumerate(raw_palettes):
            if not isinstance(item, dict):
                raise AcademicPaletteCatalogError(
                    f"academic palette at index {index} is not an object: {asset_path}"
                )
            if not isinstance(item.get("id"), str) or not item["id"].strip():
                raise AcademicPaletteCatalogError(
                    f"academic palette at index {index} is missing a valid id: {asset_path}"
                )
            if not isinstance(item.get("name"), str) or not item["name"].strip():
                raise AcademicPaletteCatalogError(
                    f"academic palette at index {index} is missing a valid name: {asset_path}"
                )
            raw_roles = item.get("roles")
            if not isinstance(raw_roles, dict) or set(raw_roles) != required_roles:
                raise AcademicPaletteCatalogError(
                    f"academic palette at index {index} has invalid roles: {asset_path}"
                )
            palette = _normalize_palette(item)
            if not palette:
                raise AcademicPaletteCatalogError(
                    f"academic palette at index {index} is invalid: {asset_path}"
                )
            palette_id = palette["id"]
            if palette_id in ids:
                raise AcademicPaletteCatalogError(
                    f"academic palette catalog contains duplicate id {palette_id!r}: {asset_path}"
                )
            ids.add(palette_id)
            normalized.append(palette)
        if default_id not in ids:
            raise AcademicPaletteCatalogError(
                f"academic palette catalog references unknown default_palette_id {default_id!r}: {asset_path}"
            )
        version = payload.get("version")
        if not isinstance(version, int) or version < 1:
            version = 1
        return {
            "version": version,
            "kind": "academic_poster_color_palettes",
            "default_palette_id": default_id,
            "usage_contract": str(payload.get("usage_contract") or USAGE_CONTRACT),
            "palettes": normalized,
        }

    payload_dict = payload if isinstance(payload, dict) else {}
    palettes = payload_dict.get("palettes")
    if not isinstance(palettes, list):
        palettes = _FALLBACK_PALETTES
    normalized = [_normalize_palette(item) for item in palettes if isinstance(item, dict)]
    normalized = [item for item in normalized if item]
    if not normalized:
        normalized = [_normalize_palette(item) for item in _FALLBACK_PALETTES]
        normalized = [item for item in normalized if item]
    ids = {item["id"] for item in normalized}
    default_id = str(payload_dict.get("default_palette_id") or DEFAULT_PALETTE_ID).strip()
    if default_id not in ids:
        default_id = DEFAULT_PALETTE_ID if DEFAULT_PALETTE_ID in ids else normalized[0]["id"]
    version = payload_dict.get("version")
    if not isinstance(version, int) or version < 1:
        version = 1
    return {
        "version": version,
        "kind": "academic_poster_color_palettes",
        "default_palette_id": default_id,
        "usage_contract": str(payload_dict.get("usage_contract") or USAGE_CONTRACT),
        "palettes": normalized,
    }


def academic_palette_catalog_payload(path: Path | None = None) -> dict[str, Any]:
    library = load_academic_palette_library(path, strict=True)
    return {
        "version": library["version"],
        "kind": library["kind"],
        "palettes": [
            {
                "id": palette["id"],
                "name": palette["name"],
                "roles": dict(palette["roles"]),
            }
            for palette in library["palettes"]
        ],
    }


def require_academic_color_system(
    palette_id: str,
    *,
    selection_reason: str = "",
    palette_asset_path: Path | None = None,
) -> dict[str, Any]:
    library = load_academic_palette_library(palette_asset_path, strict=True)
    palettes = {item["id"]: item for item in library["palettes"]}
    normalized_id = _palette_id_from_label(str(palette_id or ""), palettes)
    if not normalized_id:
        raise ValueError(f"unknown academic palette: {palette_id}")
    palette = palettes[normalized_id]
    return _color_system(
        palette,
        selection_reason or f"user selected academic palette: {palette['name']}",
        usage_contract=library["usage_contract"],
        selection_metadata=_empty_selection_metadata(),
    )


def select_academic_color_system(
    *,
    raw_brief: str = "",
    manifest: dict[str, Any] | None = None,
    recommended_text_units: dict[str, Any] | None = None,
    palette_asset_path: Path | None = None,
) -> dict[str, Any]:
    library = load_academic_palette_library(palette_asset_path)
    palettes = {item["id"]: item for item in library["palettes"]}
    return _select_academic_color_system_from_library(
        library,
        palettes,
        raw_brief=raw_brief,
        manifest=manifest or {},
        recommended_text_units=recommended_text_units or {},
    )


def academic_color_system_options(palette_asset_path: Path | None = None) -> list[dict[str, Any]]:
    """Return all allowed academic palette options as full color-system records."""
    library = load_academic_palette_library(palette_asset_path)
    return [
        _color_system(
            palette,
            f"available academic palette option: {palette['name']}",
            usage_contract=library["usage_contract"],
        )
        for palette in library["palettes"]
    ]


def rank_academic_color_system_options(
    *,
    raw_brief: str = "",
    manifest: dict[str, Any] | None = None,
    recommended_text_units: dict[str, Any] | None = None,
    selected_palette_id: str = "",
    palette_asset_path: Path | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return palette options ordered by the same cues used for recommendation."""
    library = load_academic_palette_library(palette_asset_path)
    palettes = {item["id"]: item for item in library["palettes"]}
    raw_text = str(raw_brief or "")
    explicit_id = _explicit_palette_id(raw_text, palettes)
    excluded_ids = _excluded_palette_ids(raw_text, palettes)
    if explicit_id:
        excluded_ids.discard(explicit_id)
    ranked_ids = _random_palette_ids(library, palettes, excluded_ids=excluded_ids)
    selected_id = _palette_id_from_label(selected_palette_id, palettes)
    if selected_id and selected_id not in excluded_ids:
        ranked_ids = [selected_id, *(item for item in ranked_ids if item != selected_id)]
    if explicit_id:
        ranked_ids = [explicit_id, *(item for item in ranked_ids if item != explicit_id)]
    out: list[dict[str, Any]] = []
    for palette_id in ranked_ids:
        palette = palettes.get(palette_id)
        if not palette:
            continue
        metadata = _empty_selection_metadata()
        reason = (
            f"explicit user prompt requested {palette['name']} palette"
            if explicit_id and palette_id == explicit_id else
            f"random curated palette option: {palette['name']}"
        )
        out.append(_color_system(
            palette,
            reason,
            usage_contract=library["usage_contract"],
            selection_metadata=metadata,
        ))
        if limit is not None and len(out) >= max(0, limit):
            break
    return out


def academic_color_system_from_palette_id(
    palette_id_or_name: str,
    *,
    selection_reason: str = "",
    palette_asset_path: Path | None = None,
) -> dict[str, Any]:
    """Return a known palette color-system by id/name, or empty dict."""
    library = load_academic_palette_library(palette_asset_path)
    palettes = {item["id"]: item for item in library["palettes"]}
    palette_id = _palette_id_from_label(palette_id_or_name, palettes)
    if not palette_id:
        return {}
    palette = palettes.get(palette_id)
    if not palette:
        return {}
    return _color_system(
        palette,
        selection_reason or f"selected academic palette option: {palette['name']}",
        usage_contract=library["usage_contract"],
        selection_metadata=_empty_selection_metadata(),
    )


def active_academic_color_system(
    *sources: Any,
    raw_brief: str = "",
    manifest: dict[str, Any] | None = None,
    recommended_text_units: dict[str, Any] | None = None,
    palette_asset_path: Path | None = None,
) -> dict[str, Any]:
    """Return the first valid source color system, or select a fallback."""
    library = load_academic_palette_library(palette_asset_path)
    palettes = {item["id"]: item for item in library["palettes"]}
    for source in sources:
        color_system = _extract_color_system(
            source,
            palettes=palettes,
            usage_contract=library["usage_contract"],
        )
        if color_system:
            return color_system

    return _select_academic_color_system_from_library(
        library,
        palettes,
        raw_brief=raw_brief,
        manifest=manifest or {},
        recommended_text_units=recommended_text_units or {},
    )


def explicit_academic_color_system(
    *,
    raw_brief: str = "",
    palette_asset_path: Path | None = None,
) -> dict[str, Any]:
    """Return a prompt-explicit palette override, if the prompt names one."""
    library = load_academic_palette_library(palette_asset_path)
    palettes = {item["id"]: item for item in library["palettes"]}
    explicit_id = _explicit_palette_id(str(raw_brief or ""), palettes)
    if not explicit_id:
        return {}
    return _color_system(
        palettes[explicit_id],
        f"explicit user prompt requested {palettes[explicit_id]['name']} palette",
        usage_contract=library["usage_contract"],
    )


def _select_academic_color_system_from_library(
    library: dict[str, Any],
    palettes: dict[str, dict[str, Any]],
    *,
    raw_brief: str,
    manifest: dict[str, Any],
    recommended_text_units: dict[str, Any],
) -> dict[str, Any]:
    explicit_id = _explicit_palette_id(str(raw_brief or ""), palettes)
    if explicit_id:
        return _color_system(
            palettes[explicit_id],
            f"explicit user prompt requested {palettes[explicit_id]['name']} palette",
            usage_contract=library["usage_contract"],
        )

    excluded_ids = _excluded_palette_ids(str(raw_brief or ""), palettes)
    palette_id = _random_palette_id(library, palettes, excluded_ids=excluded_ids)
    if palette_id:
        return _color_system(
            palettes[palette_id],
            f"random curated palette selection: {palettes[palette_id]['name']}",
            usage_contract=library["usage_contract"],
            selection_metadata=_empty_selection_metadata(),
        )
    default_id = str(library.get("default_palette_id") or DEFAULT_PALETTE_ID)
    canonical = [str(item.get("id") or "") for item in library.get("palettes") or []]
    fallback_id = next((item for item in [default_id, *canonical] if item in palettes and item not in excluded_ids), default_id)
    palette = palettes.get(fallback_id) or palettes.get(default_id) or palettes[DEFAULT_PALETTE_ID]
    return _color_system(
        palette,
        f"no strong domain match; using default {palette['name']} academic palette",
        usage_contract=library["usage_contract"],
        selection_metadata=_empty_selection_metadata(),
    )


def _normalize_palette(raw: dict[str, Any]) -> dict[str, Any]:
    palette_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or palette_id.replace("_", " ").title()).strip()
    roles_raw = raw.get("roles") if isinstance(raw.get("roles"), dict) else {}
    roles = {
        role: _normalize_hex(roles_raw.get(role))
        for role in _CSS_VAR_BY_ROLE
    }
    if not palette_id or any(not value for value in roles.values()):
        return {}
    normalized = {
        "id": palette_id,
        "name": name,
        "use_when": str(raw.get("use_when") or "").strip(),
        "roles": roles,
    }
    for key in ("selection_keywords", "domain_tags", "tone_tags", "avoid_keywords"):
        normalized[key] = _normalize_string_list(raw.get(key))
    return normalized


def _color_system(
    palette: dict[str, Any],
    selection_reason: str,
    *,
    usage_contract: str,
    selection_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    roles = dict(palette.get("roles") or {})
    css_variables = {
        _CSS_VAR_BY_ROLE[role]: roles[role]
        for role in _CSS_VAR_BY_ROLE
        if role in roles
    }
    allowed_hexes = _unique_hexes([roles[role] for role in _CSS_VAR_BY_ROLE if role in roles])
    metadata = selection_metadata if isinstance(selection_metadata, dict) else _empty_selection_metadata()
    out = {
        "version": 1,
        "palette_id": str(palette.get("id") or DEFAULT_PALETTE_ID),
        "palette_name": str(palette.get("name") or "Cardinal Red"),
        "use_when": str(palette.get("use_when") or "").strip(),
        "domain_tags": list(palette.get("domain_tags") or []),
        "tone_tags": list(palette.get("tone_tags") or []),
        "selection_reason": selection_reason,
        "roles": roles,
        "css_variables": css_variables,
        "allowed_hexes": allowed_hexes,
        "usage_contract": usage_contract or USAGE_CONTRACT,
    }
    out.update({
        "selection_score": int(metadata.get("selection_score") or 0),
        "paper_selection_score": int(metadata.get("paper_selection_score") or 0),
        "raw_selection_score": int(metadata.get("raw_selection_score") or 0),
        "selection_matches": [
            str(item)
            for item in (metadata.get("selection_matches") or [])
            if str(item or "").strip()
        ][:12],
    })
    return out


def _explicit_palette_id(text: str, palettes: dict[str, dict[str, Any]]) -> str:
    for palette_id, palette in palettes.items():
        candidates = {
            palette_id,
            palette_id.replace("_", " "),
            palette_id.replace("_", "-"),
            str(palette.get("name") or ""),
        }
        for candidate in candidates:
            if candidate and _contains_active_phrase(text, candidate):
                return palette_id
    return ""


def _rank_palette_ids(
    library: dict[str, Any],
    palettes: dict[str, dict[str, Any]],
    text: str,
) -> list[str]:
    ranked = _rank_palette_scores(library, palettes, paper_text=text, raw_text="")
    ranked_ids = [palette_id for palette_id, _score, _matches, _metadata in ranked]
    if ranked_ids:
        return ranked_ids
    default_id = str(library.get("default_palette_id") or DEFAULT_PALETTE_ID)
    canonical = [str(item.get("id") or "") for item in library.get("palettes") or []]
    return [item for item in [default_id, *canonical] if item in palettes]


def _random_palette_id(
    library: dict[str, Any],
    palettes: dict[str, dict[str, Any]],
    *,
    excluded_ids: set[str] | None = None,
) -> str:
    ids = _random_palette_ids(library, palettes, excluded_ids=excluded_ids)
    return ids[0] if ids else ""


def _random_palette_ids(
    library: dict[str, Any],
    palettes: dict[str, dict[str, Any]],
    *,
    excluded_ids: set[str] | None = None,
) -> list[str]:
    excluded = set(excluded_ids or set())
    ids = [
        str(item.get("id") or "")
        for item in library.get("palettes") or []
        if str(item.get("id") or "") in palettes and str(item.get("id") or "") not in excluded
    ]
    random.shuffle(ids)
    return ids


def _excluded_palette_ids(text: str, palettes: dict[str, dict[str, Any]]) -> set[str]:
    raw_text = str(text or "")
    if not raw_text.strip():
        return set()
    return {
        palette_id
        for palette_id, palette in palettes.items()
        if _palette_is_excluded(raw_text, palette_id, palette)
    }


def _palette_is_excluded(text: str, palette_id: str, palette: dict[str, Any]) -> bool:
    avoid_keywords = _normalize_string_list(palette.get("avoid_keywords"))
    if any(_contains_active_phrase(text, keyword) for keyword in avoid_keywords):
        return True
    candidates = {
        palette_id,
        palette_id.replace("_", " "),
        palette_id.replace("_", "-"),
        str(palette.get("name") or ""),
        *(_avoid_target_phrase(keyword) for keyword in avoid_keywords),
    }
    return any(
        candidate and _contains_negated_phrase(text, candidate)
        for candidate in candidates
    )


def _rank_palette_scores(
    library: dict[str, Any],
    palettes: dict[str, dict[str, Any]],
    *,
    paper_text: str,
    raw_text: str,
) -> list[tuple[str, int, list[str], dict[str, Any]]]:
    scored: list[tuple[str, int, int, list[str], dict[str, Any]]] = []
    for index, palette in enumerate(library.get("palettes") or []):
        palette_id = str(palette.get("id") or "")
        if palette_id not in palettes:
            continue
        metadata = _palette_selection_metadata(palette, paper_text=paper_text, raw_text=raw_text)
        score = int(metadata.get("selection_score") or 0)
        matches = [
            str(item)
            for item in (metadata.get("selection_matches") or [])
            if str(item or "").strip()
        ]
        scored.append((palette_id, score, index, matches, metadata))
    scored.sort(key=lambda item: (-item[1], item[2]))
    return [
        (palette_id, score, matches, metadata)
        for palette_id, score, _index, matches, metadata in scored
    ]


def _palette_selection_score(palette: dict[str, Any], text: str) -> tuple[int, list[str]]:
    metadata = _scored_palette_text(
        palette,
        str(text or ""),
        include_tone_tags=False,
    )
    return int(metadata.get("score") or 0), list(metadata.get("matches") or [])


def _palette_selection_metadata(
    palette: dict[str, Any],
    *,
    paper_text: str,
    raw_text: str,
) -> dict[str, Any]:
    paper = _scored_palette_text(palette, str(paper_text or ""), include_tone_tags=False)
    raw = _scored_palette_text(palette, str(raw_text or ""), include_tone_tags=True)
    paper_score = int(paper.get("score") or 0)
    raw_score = int(raw.get("score") or 0)
    matches = list(dict.fromkeys([
        *(str(item) for item in (raw.get("matches") or []) if str(item or "").strip()),
        *(str(item) for item in (paper.get("matches") or []) if str(item or "").strip()),
    ]))
    return {
        "selection_score": paper_score + raw_score,
        "paper_selection_score": paper_score,
        "raw_selection_score": raw_score,
        "selection_matches": matches[:12],
    }


def _scored_palette_text(
    palette: dict[str, Any],
    text: str,
    *,
    include_tone_tags: bool,
) -> dict[str, Any]:
    raw_text = str(text or "")
    if not raw_text.strip():
        return {"score": 0, "matches": []}
    avoid_matches = [
        keyword for keyword in _normalize_string_list(palette.get("avoid_keywords"))
        if _contains_active_phrase(raw_text, keyword)
    ]
    if avoid_matches:
        return {"score": -100, "matches": avoid_matches[:4]}
    matches: list[str] = []
    score = 0
    weighted_sources: list[tuple[int, list[str]]] = [
        (4, _palette_selection_keywords(palette)),
        (3, _palette_selection_domain_tags(palette)),
    ]
    if include_tone_tags:
        weighted_sources.append((3, _palette_selection_tone_tags(palette)))
    for weight, keywords in weighted_sources:
        for keyword in keywords:
            if keyword in matches:
                continue
            if _contains_active_phrase(raw_text, keyword):
                matches.append(keyword)
                score += weight
    return {"score": score, "matches": matches}


def _palette_selection_keywords(palette: dict[str, Any]) -> list[str]:
    palette_id = str(palette.get("id") or "")
    keywords = _normalize_string_list(palette.get("selection_keywords"))
    for rule_palette_id, rule_keywords in _KEYWORD_RULES:
        if rule_palette_id == palette_id:
            keywords.extend(rule_keywords)
            break
    color_label_keys = _palette_color_label_keys(palette)
    return list(dict.fromkeys(
        keyword
        for item in keywords
        for keyword in [str(item).strip()]
        if (
            keyword
            and keyword.strip().lower() not in _GENERIC_SELECTION_KEYWORDS
            and _palette_label_key(keyword) not in color_label_keys
        )
    ))


def _palette_selection_domain_tags(palette: dict[str, Any]) -> list[str]:
    return [
        keyword
        for keyword in _normalize_string_list(palette.get("domain_tags"))
        if keyword.strip().lower() not in _GENERIC_SELECTION_KEYWORDS
    ]


def _palette_selection_tone_tags(palette: dict[str, Any]) -> list[str]:
    return [
        keyword
        for keyword in _normalize_string_list(palette.get("tone_tags"))
        if keyword.strip().lower() not in _GENERIC_SELECTION_KEYWORDS
    ]


def _palette_color_label_keys(palette: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for value in (palette.get("id"), palette.get("name")):
        tokens = _LATIN_TOKEN_RE.findall(str(value or "").lower())
        for token in tokens:
            keys.add(_palette_label_key(token))
        for start in range(len(tokens)):
            for end in range(start + 2, min(len(tokens), start + 3) + 1):
                keys.add(_palette_label_key(" ".join(tokens[start:end])))
    return {key for key in keys if key}


def _selection_text(raw_brief: str, manifest: dict[str, Any], recommended_text_units: dict[str, Any]) -> str:
    pieces = _selection_text_pieces(manifest, recommended_text_units)
    pieces.append(raw_brief)
    return ". ".join(str(piece) for piece in pieces if str(piece or "").strip())


def _paper_selection_text(manifest: dict[str, Any], recommended_text_units: dict[str, Any]) -> str:
    return ". ".join(str(piece) for piece in _selection_text_pieces(manifest, recommended_text_units) if str(piece or "").strip())


def _selection_text_pieces(manifest: dict[str, Any], recommended_text_units: dict[str, Any]) -> list[str]:
    pieces: list[str] = []
    for key in ("title", "paper_title", "abstract", "summary", "venue"):
        value = manifest.get(key)
        if isinstance(value, str):
            pieces.append(value)
    pieces.extend(str(item) for item in manifest.get("keywords") or [] if str(item).strip())
    pieces.extend(_flatten_text_units(recommended_text_units))
    return pieces


def _empty_selection_metadata() -> dict[str, Any]:
    return {
        "selection_score": 0,
        "paper_selection_score": 0,
        "raw_selection_score": 0,
        "selection_matches": [],
    }


def _selection_metadata_from_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _empty_selection_metadata()
    return {
        "selection_score": int(value.get("selection_score") or 0),
        "paper_selection_score": int(value.get("paper_selection_score") or 0),
        "raw_selection_score": int(value.get("raw_selection_score") or 0),
        "selection_matches": [
            str(item)
            for item in (value.get("selection_matches") or [])
            if str(item or "").strip()
        ][:12],
    }


def _flatten_text_units(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            out.extend(_flatten_text_units(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_flatten_text_units(item))
    elif isinstance(value, str):
        out.append(value)
    return out[:120]


def _extract_color_system(
    source: Any,
    *,
    palettes: dict[str, dict[str, Any]],
    usage_contract: str,
    depth: int = 0,
) -> dict[str, Any]:
    if source is None or depth > 4:
        return {}

    if isinstance(source, dict):
        direct = _coerce_color_system(source, palettes=palettes, usage_contract=usage_contract)
        if direct:
            return direct
        for key in ("color_system", "poster_content_brief", "poster_plan_contract"):
            value = source.get(key)
            found = _extract_color_system(
                value,
                palettes=palettes,
                usage_contract=usage_contract,
                depth=depth + 1,
            )
            if found:
                return found
        html_artifact = source.get("html_artifact")
        if isinstance(html_artifact, dict):
            found = _extract_color_system(
                html_artifact.get("theme"),
                palettes=palettes,
                usage_contract=usage_contract,
                depth=depth + 1,
            )
            if found:
                return found
        theme = source.get("theme")
        if isinstance(theme, dict):
            return _extract_color_system(
                theme,
                palettes=palettes,
                usage_contract=usage_contract,
                depth=depth + 1,
            )
        return {}

    for attr in ("color_system", "poster_content_brief", "poster_plan_contract"):
        found = _extract_color_system(
            getattr(source, attr, None),
            palettes=palettes,
            usage_contract=usage_contract,
            depth=depth + 1,
        )
        if found:
            return found
    html_artifact = getattr(source, "html_artifact", None)
    theme = getattr(html_artifact, "theme", None) if html_artifact is not None else None
    return _extract_color_system(
        theme,
        palettes=palettes,
        usage_contract=usage_contract,
        depth=depth + 1,
    )


def _coerce_color_system(
    value: Any,
    *,
    palettes: dict[str, dict[str, Any]],
    usage_contract: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    palette_id = _palette_id_from_label(_first_string(
        value.get("palette_id"),
        value.get("id"),
        value.get("palette_name"),
        value.get("name"),
    ), palettes)
    if palette_id and palette_id in palettes:
        reason = str(value.get("selection_reason") or "existing artifact palette id")
        return _color_system(
            palettes[palette_id],
            reason,
            usage_contract=usage_contract,
            selection_metadata=_selection_metadata_from_value(value),
        )
    raw_roles = value.get("roles") if isinstance(value.get("roles"), dict) else {}
    roles = {
        role: _normalize_hex(raw_roles.get(role))
        for role in _CSS_VAR_BY_ROLE
    }
    if any(not item for item in roles.values()):
        return {}
    for known_id, palette in palettes.items():
        known_roles = palette.get("roles") if isinstance(palette.get("roles"), dict) else {}
        if roles == {
            role: _normalize_hex(known_roles.get(role))
            for role in _CSS_VAR_BY_ROLE
        }:
            reason = str(value.get("selection_reason") or "existing artifact palette roles")
            return _color_system(
                palettes[known_id],
                reason,
                usage_contract=usage_contract,
                selection_metadata=_selection_metadata_from_value(value),
            )
    return {}


def _first_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _palette_id_from_label(label: str, palettes: dict[str, dict[str, Any]]) -> str:
    key = _palette_label_key(label)
    if not key:
        return ""
    for palette_id, palette in palettes.items():
        if key in {
            _palette_label_key(palette_id),
            _palette_label_key(str(palette.get("name") or "")),
        }:
            return palette_id
    return ""


def _palette_label_key(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", str(value or "").strip().lower())


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _contains_active_phrase(text: str, phrase: str) -> bool:
    return bool(_active_phrase_matches(text, phrase))


def _contains_negated_phrase(text: str, phrase: str) -> bool:
    raw_text = str(text or "")
    raw_phrase = str(phrase or "").strip()
    if not raw_text or not raw_phrase:
        return False
    return any(_is_negated_match(raw_text, start) for start, _end in _phrase_matches(raw_text, raw_phrase))


def _avoid_target_phrase(phrase: str) -> str:
    return re.sub(
        r"^\s*(?:avoid|no|not|without|do\s+not(?:\s+use)?|don['’]?t(?:\s+use)?|不要|避免)\s+",
        "",
        str(phrase or "").strip(),
        flags=re.I,
    ).strip()


def _active_phrase_matches(text: str, phrase: str) -> list[tuple[int, int]]:
    matches: list[tuple[int, int]] = []
    raw_text = str(text or "")
    raw_phrase = str(phrase or "").strip()
    if not raw_text or not raw_phrase:
        return matches
    for start, end in _phrase_matches(raw_text, raw_phrase):
        if not _is_negated_match(raw_text, start):
            matches.append((start, end))
    return matches


def _phrase_matches(text: str, phrase: str) -> list[tuple[int, int]]:
    if _CJK_RE.search(phrase):
        matches: list[tuple[int, int]] = []
        start = text.find(phrase)
        while start != -1:
            matches.append((start, start + len(phrase)))
            start = text.find(phrase, start + len(phrase))
        return matches

    pattern = _latin_phrase_pattern(phrase)
    if not pattern:
        return []
    return [match.span() for match in re.finditer(pattern, text, flags=re.I)]


def _latin_phrase_pattern(phrase: str) -> str:
    tokens = _LATIN_TOKEN_RE.findall(phrase)
    if not tokens:
        return ""
    body = _LATIN_PHRASE_SEPARATOR.join(re.escape(token) for token in tokens)
    return r"(?<![a-z0-9])" + body + r"(?![a-z0-9])"


def _is_negated_match(text: str, start: int) -> bool:
    prefix = text[max(0, start - _NEGATION_WINDOW_CHARS):start]
    negations = list(_NEGATION_TOKEN_RE.finditer(prefix))
    for match in reversed(negations):
        segment = prefix[match.end():]
        if _NEGATION_HARD_BOUNDARY_RE.search(segment):
            continue
        normalized_segment = re.sub(r"\s+", " ", segment).strip().lower()
        if _NEGATION_CONTRAST_RE.search(segment):
            continue
        use_match = _NEGATION_USE_RE.search(segment)
        if use_match and normalized_segment not in {"use", "to use"} and not normalized_segment.startswith((
            "use ",
            "use-",
            "use_",
            "to use ",
            "to use-",
            "to use_",
        )):
            continue
        return True
    return False


def _contains_phrase(text: str, phrase: str) -> bool:
    if not phrase:
        return False
    return _contains_active_phrase(text, phrase)


def _normalize_hex(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        return text.upper()
    return ""


def _unique_hexes(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        normalized = _normalize_hex(value)
        if normalized and normalized not in seen:
            out.append(normalized)
            seen.add(normalized)
    return out
