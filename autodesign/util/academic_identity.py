"""Academic identity asset resolution for paper posters.

The resolver is deliberately conservative. It promotes user-supplied logo-like
images and manifest/brief-derived venue names into a small, provenance-carrying
contract that the planner can place in the title band. It does not perform
open-ended web search.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_LOGO_HINTS = (
    "logo",
    "brand",
    "badge",
    "seal",
    "mark",
    "identity",
    "affiliation",
    "institution",
    "institute",
    "university",
    "school",
    "lab",
    "sponsor",
)

_VENUE_PATTERNS = (
    r"\bNeurIPS(?:\s+20\d{2})?\b",
    r"\bNIPS(?:\s+20\d{2})?\b",
    r"\bICML(?:\s+20\d{2})?\b",
    r"\bICLR(?:\s+20\d{2})?\b",
    r"\bCVPR(?:\s+20\d{2})?\b",
    r"\bICCV(?:\s+20\d{2})?\b",
    r"\bECCV(?:\s+20\d{2})?\b",
    r"\bACL(?:\s+20\d{2})?\b",
    r"\bEMNLP(?:\s+20\d{2})?\b",
    r"\bAAAI(?:\s+20\d{2})?\b",
    r"\bKDD(?:\s+20\d{2})?\b",
    r"\bCHI(?:\s+20\d{2})?\b",
    r"\bSIGGRAPH(?:\s+20\d{2})?\b",
    r"\barXiv\b",
)

_ENTITY_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"\bMIT(?:\s+CSAIL)?\b", "MIT", "institution"),
    (r"\bCSAIL\b", "MIT CSAIL", "lab"),
    (r"\bStanford(?:\s+University)?\b", "Stanford University", "institution"),
    (r"\bUC\s+Berkeley\b|\bUniversity\s+of\s+California,\s+Berkeley\b", "UC Berkeley", "institution"),
    (r"\bBAIR\b|\bBerkeley\s+AI\s+Research\b", "Berkeley AI Research", "lab"),
    (r"\bCarnegie\s+Mellon(?:\s+University)?\b|\bCMU\b", "Carnegie Mellon University", "institution"),
    (r"\bPrinceton(?:\s+University)?\b", "Princeton University", "institution"),
    (r"\bHarvard(?:\s+University)?\b", "Harvard University", "institution"),
    (r"\bUniversity\s+of\s+Oxford\b|\bOxford\s+University\b", "University of Oxford", "institution"),
    (r"\bUniversity\s+of\s+Cambridge\b|\bCambridge\s+University\b", "University of Cambridge", "institution"),
    (r"\bTsinghua(?:\s+University)?\b", "Tsinghua University", "institution"),
    (r"\bPeking\s+University\b", "Peking University", "institution"),
    (r"\bShanghai\s+Jiao\s+Tong(?:\s+University)?\b", "Shanghai Jiao Tong University", "institution"),
    (r"\bZhejiang(?:\s+University)?\b", "Zhejiang University", "institution"),
    (r"\bMicrosoft\s+Research\b", "Microsoft Research", "company"),
    (r"\bGoogle\s+Research\b|\bGoogle\s+DeepMind\b", "Google DeepMind", "company"),
    (r"\bDeepMind\b", "DeepMind", "company"),
    (r"\bMeta\s+AI\b|\bFAIR\b", "Meta AI", "company"),
    (r"\bOpenAI\b", "OpenAI", "company"),
)

_AFFILIATION_ENTITY_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"\bNational\s+University\s+of\s+Singapore\b|\bNUS\b", "National University of Singapore", "institution"),
    (r"\bTsinghua(?:\s+University)?\b", "Tsinghua University", "institution"),
    (r"\bState\s+University\s+of\s+New\s+York\s+at\s+Buffalo\b|\bUniversity\s+at\s+Buffalo\b|\bSUNY\s+Buffalo\b", "University at Buffalo", "institution"),
    (r"\bMicrosoft(?:\s+Research)?\b", "Microsoft Research", "company"),
)

_AFFILIATION_ROLE_ALLOWLIST = {"institution", "lab", "company", "publisher", "society"}
_ALLOWLIST_PATH = Path(__file__).resolve().parents[2] / "assets" / "academic_identity" / "allowlist.json"


def build_academic_identity_assets(
    *,
    summaries: list[dict[str, Any]],
    rendered_layers: dict[str, dict[str, Any]],
    brief: str = "",
) -> dict[str, Any]:
    """Return provenance-backed identity assets for academic posters.

    The payload is intentionally planner-facing and compact. Image assets point
    at already registered rendered_layers entries; text badges are fallback
    labels the planner can render as native editable text.
    """

    raw_brief = str(brief or "")
    raw_entities = _dedupe_entities(
        _entities_from_summaries(summaries) + _entities_from_text(raw_brief, source="brief")
    )
    primary_venue_key = _primary_venue_key(raw_entities)
    entities = [
        _annotate_identity_entity(entity, primary_venue_key=primary_venue_key)
        for entity in raw_entities
    ]
    explicit_logo_request = _brief_requests_identity(raw_brief)
    uploaded_count = sum(1 for layer_id, rec in rendered_layers.items() if _is_uploaded_image(layer_id, rec))

    assets: list[dict[str, Any]] = []
    for layer_id, rec in sorted(rendered_layers.items()):
        if not isinstance(rec, dict):
            continue
        asset = _asset_from_uploaded_layer(
            layer_id,
            rec,
            explicit_logo_request=explicit_logo_request,
            uploaded_count=uploaded_count,
            primary_venue_key=primary_venue_key,
        )
        if not asset:
            continue
        assets.append(asset)
        _mark_rendered_layer_identity(rec, asset)

    image_entity_keys = {_entity_key(asset.get("entity_name")) for asset in assets}
    for entity in entities:
        if not _entity_exposes_identity_asset(entity):
            continue
        if _entity_key(entity.get("entity_name")) in image_entity_keys:
            continue
        assets.append(_text_badge_asset(entity, len(assets) + 1))

    if not assets and not entities:
        return {}

    payload = {
        "kind": "academic_identity_assets",
        "version": 1,
        "assets": assets,
        "entities": entities,
        "primary_identity": {
            "venue_key": primary_venue_key or None,
            "venue": next(
                (
                    entity.get("entity_name")
                    for entity in entities
                    if entity.get("primary_identity") and entity.get("role") == "venue"
                ),
                None,
            ),
        },
        "placement_policy": {
            "slot_id": "title_meta",
            "allowed_regions": ["top_left", "top_right"],
            "max_total_area_ratio": 0.03,
            "max_asset_area_ratio": 0.012,
            "max_band_height_ratio": 0.16,
            "min_clearance_px": 24,
            "fallback": "editable_text_badge",
        },
        "metrics": {
            "identity_asset_count": len(assets),
            "safe_to_place_count": sum(1 for asset in assets if asset.get("safe_to_place")),
            "image_asset_count": sum(1 for asset in assets if asset.get("asset_type") == "image"),
            "text_badge_count": sum(1 for asset in assets if asset.get("asset_type") == "text_badge"),
        },
    }
    return payload


def find_identity_asset(identity_assets: dict[str, Any] | None, asset_id: str) -> dict[str, Any] | None:
    """Find an identity asset by asset id, rendered layer id, label, or entity."""

    query = _lookup_key(asset_id)
    if not query:
        return None
    state = identity_assets if isinstance(identity_assets, dict) else {}
    for asset in state.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        keys = {
            _lookup_key(asset.get("asset_id")),
            _lookup_key(asset.get("rendered_layer_id")),
            _lookup_key(asset.get("label")),
            _lookup_key(asset.get("entity_name")),
        }
        if query in keys:
            return asset
    return None


def append_identity_asset(
    identity_assets: dict[str, Any] | None,
    asset: dict[str, Any],
) -> dict[str, Any]:
    """Return identity state with ``asset`` added or replaced by asset_id."""

    state = dict(identity_assets or {})
    state.setdefault("kind", "academic_identity_assets")
    state.setdefault("version", 1)
    assets = [item for item in (state.get("assets") or []) if isinstance(item, dict)]
    asset_id = str(asset.get("asset_id") or "")
    assets = [item for item in assets if str(item.get("asset_id") or "") != asset_id]
    entity_key = _entity_key(asset.get("entity_name"))
    if entity_key and asset.get("asset_type") == "image":
        assets = [
            item for item in assets
            if not (
                item.get("asset_type") == "text_badge"
                and _entity_key(item.get("entity_name")) == entity_key
            )
        ]
    assets.append(asset)
    state["assets"] = assets
    policy = state.setdefault("placement_policy", {})
    if isinstance(policy, dict):
        policy.setdefault("slot_id", "title_meta")
        policy.setdefault("allowed_regions", ["top_left", "top_right"])
        policy.setdefault("max_total_area_ratio", 0.03)
        policy.setdefault("max_asset_area_ratio", 0.012)
        policy.setdefault("max_band_height_ratio", 0.16)
        policy.setdefault("min_clearance_px", 24)
        policy.setdefault("fallback", "editable_text_badge")
    return refresh_identity_asset_metrics(state)


def refresh_identity_asset_metrics(identity_assets: dict[str, Any] | None) -> dict[str, Any]:
    """Refresh derived identity asset counts without dropping other metrics."""

    state = dict(identity_assets or {})
    assets = [item for item in (state.get("assets") or []) if isinstance(item, dict)]
    metrics = dict(state.get("metrics") or {})
    metrics.update({
        "identity_asset_count": len(assets),
        "safe_to_place_count": sum(1 for item in assets if item.get("safe_to_place")),
        "image_asset_count": sum(1 for item in assets if item.get("asset_type") == "image"),
        "text_badge_count": sum(1 for item in assets if item.get("asset_type") == "text_badge"),
    })
    state["metrics"] = metrics
    return state


def _entities_from_summaries(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        manifest = summary.get("manifest") if isinstance(summary.get("manifest"), dict) else {}
        venue = str(manifest.get("venue") or "").strip()
        if venue and venue.lower() not in {"none", "null", "n/a"}:
            entities.append({
                "entity_name": venue,
                "role": "venue",
                "source": "manifest",
                "confidence": 0.82,
            })
        for affiliation in manifest.get("affiliations") or []:
            entities.extend(_entities_from_affiliation_text(str(affiliation or ""), allow_direct=True))
        entities.extend(_entities_from_affiliation_text(str(summary.get("raw_text") or "")))
    return entities


def _primary_venue_key(entities: list[dict[str, Any]]) -> str:
    venue_entities = [
        entity for entity in entities
        if str(entity.get("role") or "").lower() == "venue"
    ]
    for entity in venue_entities:
        if str(entity.get("source") or "").lower() == "manifest":
            key = _venue_base_key(entity.get("entity_name"))
            if key:
                return key
    for entity in venue_entities:
        key = _venue_base_key(entity.get("entity_name"))
        if key:
            return key
    return ""


def _annotate_identity_entity(entity: dict[str, Any], *, primary_venue_key: str) -> dict[str, Any]:
    out = dict(entity)
    role = str(out.get("role") or _role_from_name(str(out.get("entity_name") or ""))).lower()
    entity_name = _clean_label(out.get("entity_name"))
    canonical_key = _venue_base_key(entity_name) if role == "venue" else _entity_key(entity_name)
    out["role"] = role
    out["canonical_entity_key"] = canonical_key

    if role == "venue":
        is_primary = bool(primary_venue_key and canonical_key == primary_venue_key)
        out["primary_identity"] = is_primary
        out["required_to_place"] = is_primary
        out["allowed_to_place"] = is_primary
        out["placement_intent"] = "primary_venue" if is_primary else "context_mention"
        out["identity_group"] = "primary_venue" if is_primary else "context_venue"
        return out

    # Institution/lab/company support is allowed only when explicitly extracted
    # as an affiliation-like identity. If no verified image is found, the
    # planner may still place a compact editable text badge instead of a weak
    # remote/page image.
    out["primary_identity"] = False
    if str(out.get("source") or "").lower() == "affiliation":
        out.setdefault("required_to_place", True)
        out.setdefault("text_badge_fallback_allowed", True)
        out.setdefault("placement_intent", "verified_affiliation")
        out.setdefault("identity_group", "verified_affiliation")
    else:
        out.setdefault("required_to_place", False)
        out.setdefault("text_badge_fallback_allowed", True)
        out.setdefault("placement_intent", "supporting_identity")
        out.setdefault("identity_group", role or "project")
    out.setdefault("allowed_to_place", True)
    return out


def _entity_exposes_identity_asset(entity: dict[str, Any]) -> bool:
    if entity.get("allowed_to_place") is False:
        return False
    if str(entity.get("placement_intent") or "").lower() in {"context_mention", "reference_only", "do_not_place"}:
        return False
    if str(entity.get("role") or "").lower() != "venue" and entity.get("text_badge_fallback_allowed") is False:
        return False
    return True


def _entities_from_affiliation_text(text: str, *, allow_direct: bool = False) -> list[dict[str, Any]]:
    affiliation_text = _affiliation_candidate_text(text)
    if not affiliation_text and allow_direct:
        affiliation_text = _clean_label(str(text or ""))[:500]
    if not affiliation_text:
        return []
    entities: list[dict[str, Any]] = []
    seen: set[str] = set()
    for label, role, confidence in _allowlist_affiliation_matches(affiliation_text):
        key = _entity_key(label)
        if not key or key in seen:
            continue
        seen.add(key)
        entities.append(_affiliation_entity(label, role, confidence))
        if len(entities) >= 2:
            return entities
    for pattern, label, role in _AFFILIATION_ENTITY_PATTERNS:
        if not re.search(pattern, affiliation_text, flags=re.IGNORECASE):
            continue
        key = _entity_key(label)
        if not key or key in seen:
            continue
        seen.add(key)
        entities.append(_affiliation_entity(label, role, 0.78 if role == "institution" else 0.74))
        if len(entities) >= 2:
            break
    if allow_direct and not entities:
        generic = _manifest_affiliation_entity(affiliation_text)
        if generic:
            entities.append(generic)
    return entities


def _affiliation_entity(label: str, role: str, confidence: float) -> dict[str, Any]:
    return {
        "entity_name": label,
        "role": role,
        "source": "affiliation",
        "confidence": confidence,
        "required_to_place": True,
        "allowed_to_place": True,
        "placement_intent": "verified_affiliation",
        "identity_group": "verified_affiliation",
        "text_badge_fallback_allowed": True,
    }


def _allowlist_affiliation_matches(text: str) -> list[tuple[str, str, float]]:
    compact_text = _entity_key(text)
    if not compact_text:
        return []
    matches: list[tuple[int, str, str, float]] = []
    for rule in _load_identity_allowlist_rules():
        if not isinstance(rule, dict):
            continue
        role = str(rule.get("role") or "").strip().lower()
        if role not in _AFFILIATION_ROLE_ALLOWLIST:
            continue
        label = _clean_label(rule.get("entity_name"))
        aliases = [label, *(rule.get("aliases") or [])]
        best_len = 0
        for alias in aliases:
            alias_key = _entity_key(alias)
            if len(alias_key) < 4 and compact_text != alias_key:
                continue
            if alias_key in compact_text:
                best_len = max(best_len, len(alias_key))
        if not best_len:
            continue
        confidence = _safe_float(rule.get("confidence"), 0.76)
        matches.append((best_len, label, role, confidence))
    matches.sort(key=lambda item: item[0], reverse=True)
    out: list[tuple[str, str, float]] = []
    seen: set[str] = set()
    for _, label, role, confidence in matches:
        key = _entity_key(label)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((label, role, confidence))
    return out


def _load_identity_allowlist_rules() -> list[dict[str, Any]]:
    try:
        data = json.loads(_ALLOWLIST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [rule for rule in (data.get("rules") or []) if isinstance(rule, dict)]


def _manifest_affiliation_entity(text: str) -> dict[str, Any] | None:
    label = _clean_label(text)
    label = _clean_label(re.sub(r"^\d+\s*", "", label))
    label = re.sub(r"\b[\w.+-]+@[\w.-]+\.\w+\b", "", label)
    label = _clean_label(label)
    if not label or len(label) < 3 or len(label) > 120:
        return None
    if re.search(r"https?://|www\.|\babstract\b|\bkeywords?\b", label, flags=re.IGNORECASE):
        return None
    if not re.search(r"[A-Za-z]", label):
        return None
    if len(re.findall(r"\b[A-Z][A-Za-z&.\-]*\b", label)) > 10:
        return None
    role = _role_from_affiliation_label(label)
    return _affiliation_entity(label, role, 0.64)


def _role_from_affiliation_label(label: str) -> str:
    lower = str(label or "").lower()
    if any(token in lower for token in (" inc", " corp", " ltd", " llc", " company", " technologies", " labs inc")):
        return "company"
    if any(token in lower for token in (" lab", "laboratory", "research center", "research centre", "research group")):
        return "lab"
    if any(token in lower for token in ("university", "institute", "school", "college", "academy", "hospital", "medical center", "department", "faculty")):
        return "institution"
    return "institution"


def _affiliation_candidate_text(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    head = raw[:7000]
    marker = re.search(r"\n\s*(abstract|introduction|keywords?)\b", head, flags=re.IGNORECASE)
    if marker:
        head = head[:marker.start()]
    lines = [_clean_label(re.sub(r"(?<=\d)(?=[A-Z][a-z])", " ", line)) for line in head.splitlines()]
    lines = [line for line in lines if line]
    allowlist_rules = _load_identity_allowlist_rules()
    kept: list[str] = []
    for line in lines[:80]:
        lower = line.lower()
        if (
            any(token in lower for token in ("university", "institute", "laboratory", "lab", "research", "college", "school"))
            or any(re.search(pattern, line, flags=re.IGNORECASE) for pattern, _, _ in _AFFILIATION_ENTITY_PATTERNS)
            or (
                _line_looks_like_numbered_affiliation(line)
                and _line_has_allowlist_affiliation_alias(line, allowlist_rules)
            )
        ):
            kept.append(line)
    return "\n".join(kept[:24])


def _line_looks_like_numbered_affiliation(line: str) -> bool:
    match = re.match(r"^\d+\s*(.+)$", str(line or "").strip())
    if not match:
        return False
    label = match.group(1).strip()
    words = re.findall(r"[A-Za-z][A-Za-z&.-]*", label)
    if not words or len(words) > 8:
        return False
    return all(word[:1].isupper() or word.isupper() for word in words)


def _line_has_allowlist_affiliation_alias(line: str, rules: list[dict[str, Any]]) -> bool:
    compact = _entity_key(line)
    if not compact:
        return False
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        role = str(rule.get("role") or "").strip().lower()
        if role not in _AFFILIATION_ROLE_ALLOWLIST:
            continue
        aliases = [_clean_label(rule.get("entity_name")), *(rule.get("aliases") or [])]
        for alias in aliases:
            alias_key = _entity_key(alias)
            if len(alias_key) >= 4 and alias_key in compact:
                return True
    return False


def _entities_from_text(text: str, *, source: str) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    for pattern in _VENUE_PATTERNS:
        for match in re.finditer(pattern, text or "", flags=re.IGNORECASE):
            label = _clean_label(match.group(0))
            if label:
                entities.append({
                    "entity_name": label,
                    "role": "venue",
                    "source": source,
                    "confidence": 0.72,
                })
    for pattern, label, role in _ENTITY_PATTERNS:
        if re.search(pattern, text or "", flags=re.IGNORECASE):
            entities.append({
                "entity_name": label,
                "role": role,
                "source": source,
                "confidence": 0.68,
            })
    return entities


def _asset_from_uploaded_layer(
    layer_id: str,
    rec: dict[str, Any],
    *,
    explicit_logo_request: bool,
    uploaded_count: int,
    primary_venue_key: str,
) -> dict[str, Any] | None:
    if not _is_uploaded_image(layer_id, rec):
        return None
    source_file = str(rec.get("source_file") or "")
    name_hint = " ".join([
        Path(source_file).stem,
        str(rec.get("name") or ""),
        str(rec.get("caption_short") or ""),
    ]).strip()
    lower = name_hint.lower()
    has_logo_hint = any(hint in lower for hint in _LOGO_HINTS)
    if not has_logo_hint and not (explicit_logo_request and uploaded_count <= 6):
        return None

    entity_name = _entity_name_from_filename(name_hint) or "Uploaded identity asset"
    role = _role_from_name(entity_name)
    venue_key = _venue_base_key(entity_name) if role == "venue" else ""
    primary_identity = bool(role == "venue" and venue_key and venue_key == primary_venue_key)
    confidence = 0.92 if has_logo_hint else 0.72
    asset_id = f"identity_asset_{_slug(entity_name) or layer_id}_{layer_id[-8:]}"
    return {
        "asset_id": asset_id,
        "entity_name": entity_name,
        "label": entity_name,
        "role": role,
        "asset_type": "image",
        "rendered_layer_id": layer_id,
        "local_asset_path": rec.get("src_path"),
        "source": "user_upload",
        "source_file": source_file or None,
        "source_url": None,
        "confidence": round(confidence, 2),
        "safe_to_place": True,
        "allowed_to_place": True,
        "required_to_place": primary_identity,
        "primary_identity": primary_identity,
        "placement_intent": "primary_venue" if primary_identity else "supporting_identity",
        "identity_group": "primary_venue" if primary_identity else role,
        "canonical_entity_key": venue_key or _entity_key(entity_name),
        "placement": {
            "allowed_regions": ["top_left", "top_right"],
            "max_area_ratio": 0.012,
        },
    }


def _text_badge_asset(entity: dict[str, Any], index: int) -> dict[str, Any]:
    entity_name = _clean_label(entity.get("entity_name"))
    role = str(entity.get("role") or _role_from_name(entity_name))
    allowed_to_place = entity.get("allowed_to_place")
    if allowed_to_place is None:
        allowed_to_place = True
    return {
        "asset_id": f"identity_badge_{index:02d}_{_slug(entity_name)}",
        "entity_name": entity_name,
        "label": entity_name,
        "role": role,
        "asset_type": "text_badge",
        "rendered_layer_id": None,
        "local_asset_path": None,
        "source": entity.get("source") or "derived",
        "source_url": None,
        "confidence": round(_safe_float(entity.get("confidence"), 0.65), 2),
        "safe_to_place": True,
        "allowed_to_place": bool(allowed_to_place),
        "required_to_place": bool(entity.get("required_to_place")),
        "primary_identity": bool(entity.get("primary_identity")),
        "placement_intent": entity.get("placement_intent") or "supporting_identity",
        "identity_group": entity.get("identity_group") or role,
        "canonical_entity_key": entity.get("canonical_entity_key") or _entity_key(entity_name),
        "entity_source": entity.get("source"),
        "text_badge_fallback_allowed": bool(entity.get("text_badge_fallback_allowed", True)),
        "placement": {
            "allowed_regions": ["top_left", "top_right"],
            "max_area_ratio": 0.006,
        },
    }


def _mark_rendered_layer_identity(rec: dict[str, Any], asset: dict[str, Any]) -> None:
    rec.setdefault("source_id", asset.get("rendered_layer_id"))
    rec["is_identity_asset"] = True
    rec["identity_asset_id"] = asset.get("asset_id")
    rec["identity_asset_role"] = asset.get("role")
    rec["identity_entity_name"] = asset.get("entity_name")
    rec["identity_asset_intent"] = asset.get("placement_intent")
    rec["identity_required_to_place"] = bool(asset.get("required_to_place"))
    rec["identity_allowed_to_place"] = asset.get("allowed_to_place") is not False
    rec["identity_primary"] = bool(asset.get("primary_identity"))
    provenance = rec.get("provenance") if isinstance(rec.get("provenance"), dict) else {}
    provenance.update({
        "identity_asset_id": asset.get("asset_id"),
        "identity_source": asset.get("source"),
        "source_file": asset.get("source_file"),
        "source_url": asset.get("source_url"),
    })
    rec["provenance"] = provenance


def _is_uploaded_image(layer_id: str, rec: dict[str, Any]) -> bool:
    return (
        str(layer_id).startswith("ingest_img_")
        and str(rec.get("kind") or "").lower() == "image"
        and str(rec.get("source") or "").lower() == "ingested"
        and bool(rec.get("src_path"))
    )


def _brief_requests_identity(text: str) -> bool:
    lower = str(text or "").lower()
    return any(token in lower for token in ("logo", "logos", "brand", "badge", "affiliation", "institution"))


def _dedupe_entities(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entity in entities:
        key = _entity_dedupe_key(entity)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(entity)
    return out[:8]


def _entity_dedupe_key(entity: dict[str, Any]) -> str:
    role = str(entity.get("role") or "").lower()
    if role == "venue":
        venue_key = _venue_base_key(entity.get("entity_name"))
        return f"venue:{venue_key}" if venue_key else ""
    return _entity_key(entity.get("entity_name"))


def _venue_base_key(value: Any) -> str:
    text = re.sub(r"\b20\d{2}\b", "", str(value or ""), flags=re.IGNORECASE)
    key = _entity_key(text)
    if key == "nips":
        return "neurips"
    return key


def _entity_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _lookup_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9_:-]+", "", str(value or "").lower())


def _entity_name_from_filename(value: str) -> str:
    text = re.sub(r"[_\-]+", " ", str(value or "")).strip()
    text = re.sub(r"\b(logo|brand|badge|seal|mark|identity|transparent|white|black|color|colour|png|jpg|jpeg|webp)\b", "", text, flags=re.IGNORECASE)
    text = _clean_label(text)
    return text[:80]


def _role_from_name(name: str) -> str:
    lower = str(name or "").lower()
    if any(re.search(pattern, name or "", flags=re.IGNORECASE) for pattern in _VENUE_PATTERNS):
        return "venue"
    if "lab" in lower or "research" in lower or lower in {"csail", "bair", "fair"}:
        return "lab"
    if "university" in lower or "institute" in lower or "school" in lower:
        return "institution"
    if any(token in lower for token in ("google", "microsoft", "deepmind", "meta", "openai")):
        return "company"
    return "project"


def _clean_label(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" -_|")


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")[:48]


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default
