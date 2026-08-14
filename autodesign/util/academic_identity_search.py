"""Controlled web resolver for academic identity logo assets.

This module deliberately does not perform open web search. It resolves only
entities already derived by ingest against a tracked allowlist, then inspects
official pages or preferred asset URLs from that allowlist. The planner only
sees the resulting ``academic_identity_assets`` payload and rendered layer ids.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from PIL import Image

from ..config import REPO_ROOT
from .academic_identity import append_identity_asset, refresh_identity_asset_metrics


ALLOWLIST_PATH = REPO_ROOT / "assets" / "academic_identity" / "allowlist.json"
_MAX_ASSET_BYTES = 4 * 1024 * 1024
_MAX_PAGE_BYTES = 1_500_000
_MAX_PAGES_PER_ENTITY = 4
_MAX_CANDIDATES_PER_ENTITY = 18
_FETCH_RETRIES = 2
_USER_AGENT = "AutoDesign/academic-identity-search (+https://github.com/Yaxin9Luo/AutoDesign)"
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".svg"}
_RASTER_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}
_SVG_CONTENT_TYPES = {"image/svg+xml", "text/svg", "application/svg+xml"}
_LOGO_TERMS = (
    "logo",
    "wordmark",
    "brand",
    "identity",
    "seal",
    "mark",
    "lockup",
    "masthead",
)
_BLOCKED_EXTERNAL_DOMAINS = (
    "wikipedia.org",
    "wikimedia.org",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
    "medium.com",
)
_BLOCKED_ASSET_PATH_TERMS = (
    "/arrow/",
    "/icons/",
    "app-icons",
    "app_icons",
    "button-",
    "button_",
    "social-icon",
    "facebook",
    "instagram",
    "linkedin",
    "twitter",
    "youtube",
    "favicon",
    "apple-touch-icon",
)
_NON_LOGO_RASTER_PATH_TERMS = (
    "hero",
    "thumbnail",
    "thumb",
    "film",
    "video",
    "poster",
    "cover",
    "carousel",
    "applying-the-brand",
    "applying-brand",
    "og-image",
    "og_image",
    "social",
    "story",
    "banner",
    "base_image",
    "coreimg",
    "jcr_content",
    "large_box",
    "people-",
    "responsivegrid",
    "resources-libraries",
    "teaser",
    "texture",
    "photography",
)
_NON_LOGO_VECTOR_PATH_TERMS = (
    "__large",
    "alphaevolve",
    "nav__",
)
_EXPLICIT_LOGO_PATH_TERMS = (
    "logo",
    "wordmark",
    "seal",
    "lockup",
    "masthead",
)


@dataclass(frozen=True)
class FetchedResource:
    """HTTP fetch result used by resolver tests and the default fetcher."""

    url: str
    data: bytes
    content_type: str = ""
    final_url: str | None = None
    status: int | None = None


FetchUrl = Callable[[str, int], FetchedResource]


def academic_identity_search_enabled() -> bool:
    """Return whether automatic network identity resolution is enabled."""

    raw = (
        os.getenv("AUTODESIGN_ACADEMIC_IDENTITY_SEARCH", "").strip()
        or os.getenv("DESIGN_ANYTHING_ACADEMIC_IDENTITY_SEARCH", "1").strip()
    ).lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def resolve_academic_identity_assets(
    *,
    identity_assets: dict[str, Any] | None,
    rendered_layers: dict[str, dict[str, Any]],
    run_dir: Path,
    layers_dir: Path,
    brief: str = "",
    allowlist_path: Path | None = None,
    fetcher: FetchUrl | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Resolve missing academic identity image assets from official sources.

    The input state is preserved when no trusted asset is found. Resolved
    logos are registered as image records in ``rendered_layers`` and appended
    to the identity asset payload. Text badges for the same entity are removed
    only after a verified image is available.
    """

    state = dict(identity_assets or {})
    if not state:
        return {}

    active = academic_identity_search_enabled() if enabled is None else bool(enabled)
    search_payload: dict[str, Any] = {
        "enabled": active,
        "resolver": "academic_identity_search",
        "version": 1,
        "allowlist_path": str(allowlist_path or ALLOWLIST_PATH),
        "results": [],
    }
    if not active:
        search_payload["status"] = "disabled"
        state["search"] = search_payload
        return refresh_identity_asset_metrics(state)

    allowlist = load_academic_identity_allowlist(allowlist_path)
    fetch = fetcher or _cached_fetcher(_fetch_url, cache_dir=REPO_ROOT / "out" / "academic_identity_cache")
    layers_dir.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    for entity in _entities_to_resolve(state, brief=brief):
        if _has_image_asset_for_entity(state, entity):
            search_payload["results"].append({
                "entity_name": entity.get("entity_name"),
                "role": entity.get("role"),
                "required_to_place": entity.get("required_to_place"),
                "primary_identity": entity.get("primary_identity"),
                "status": "skipped_existing_image",
            })
            continue
        rule = find_allowlist_rule(entity.get("entity_name"), role=entity.get("role"), allowlist=allowlist)
        if not rule:
            search_payload["results"].append({
                "entity_name": entity.get("entity_name"),
                "role": entity.get("role"),
                "required_to_place": entity.get("required_to_place"),
                "primary_identity": entity.get("primary_identity"),
                "status": "no_allowlist_rule",
            })
            continue
        asset, result = _resolve_one_entity(
            entity,
            rule,
            fetcher=fetch,
            layers_dir=layers_dir,
            retrieved_at=now_iso,
        )
        search_payload["results"].append(result)
        if asset:
            state = append_identity_asset(state, asset)
            layer_id = str(asset.get("rendered_layer_id") or "")
            rec = asset.get("_rendered_layer_record")
            if layer_id and isinstance(rec, dict):
                rendered_layers[layer_id] = rec
            asset.pop("_rendered_layer_record", None)

    metrics = dict((state.get("metrics") or {}) if isinstance(state.get("metrics"), dict) else {})
    status_counts: dict[str, int] = {}
    failure_category_counts: dict[str, int] = {}
    unresolved_required: list[dict[str, Any]] = []
    for item in search_payload["results"]:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        category = _identity_search_failure_category(item)
        if category and category != status:
            item["failure_category"] = category
            failure_category_counts[category] = failure_category_counts.get(category, 0) + 1
        if status != "resolved" and _entity_required_to_place(item):
            unresolved_required.append({
                "entity_name": item.get("entity_name"),
                "role": item.get("role"),
                "status": status,
            })
    search_payload["status_counts"] = status_counts
    if failure_category_counts:
        search_payload["failure_category_counts"] = failure_category_counts
    if unresolved_required:
        search_payload["unresolved_required_entities"] = unresolved_required
    metrics.update({
        "identity_search_result_count": len(search_payload["results"]),
        "identity_search_resolved_count": sum(
            1 for item in search_payload["results"]
            if isinstance(item, dict) and item.get("status") == "resolved"
        ),
        "identity_search_status_counts": status_counts,
        "identity_search_failure_category_counts": failure_category_counts,
    })
    state["metrics"] = metrics
    state["search"] = search_payload
    return refresh_identity_asset_metrics(state)


def resolve_designer_identity_logo_candidates(
    *,
    identity_assets: dict[str, Any] | None,
    rendered_layers: dict[str, dict[str, Any]],
    run_dir: Path,
    layers_dir: Path,
    candidates: list[dict[str, Any]],
    allowlist_path: Path | None = None,
    fetcher: FetchUrl | None = None,
    enabled: bool | None = None,
    source: str = "designer_author_logo_candidate",
    resolver_name: str = "designer_author_logo_candidates",
    state_key: str = "designer_author_logo_candidate_resolution",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Materialize external-author discovered identity logo candidates.

    The external author may suggest official HTTPS logo URLs, but final poster
    HTML must still use local AutoDesign layers. This resolver keeps that
    boundary: each candidate is checked against the same allowlist used by the
    ingest resolver, downloaded, parsed, and registered as an identity asset.
    """

    state = dict(identity_assets or {})
    state.setdefault("kind", "academic_identity_assets")
    state.setdefault("version", 1)
    active = academic_identity_search_enabled() if enabled is None else bool(enabled)
    payload: dict[str, Any] = {
        "enabled": active,
        "resolver": resolver_name,
        "version": 1,
        "allowlist_path": str(allowlist_path or ALLOWLIST_PATH),
        "results": [],
    }
    if not active:
        payload["status"] = "disabled"
        state[state_key] = payload
        return refresh_identity_asset_metrics(state), payload

    allowlist = load_academic_identity_allowlist(allowlist_path)
    fetch = fetcher or _cached_fetcher(_fetch_url, cache_dir=run_dir / "academic_identity_cache")
    layers_dir.mkdir(parents=True, exist_ok=True)
    retrieved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    for raw in candidates[:8]:
        if not isinstance(raw, dict):
            continue
        entity_name = str(raw.get("entity_name") or raw.get("label") or "").strip()
        role = _normalize_identity_role(raw.get("role"))
        url = str(raw.get("source_url") or raw.get("url") or "").strip()
        discovered_from_url = str(raw.get("discovered_from_url") or "").strip() or None
        result: dict[str, Any] = {
            "entity_name": entity_name,
            "role": role,
            "source_url": url,
            "discovered_from_url": discovered_from_url,
            "required_to_place": bool(raw.get("required_to_place") or raw.get("primary_identity")),
            "primary_identity": bool(raw.get("primary_identity")),
            "status": "rejected",
            "resolved_asset_id": None,
        }
        if not entity_name or not url:
            result["status"] = "asset_rejected"
            result["reason"] = "missing_entity_or_source_url"
            payload["results"].append(result)
            continue
        rule = find_allowlist_rule(entity_name, role=role, allowlist=allowlist)
        if not rule:
            result["status"] = "no_allowlist_rule"
            result["reason"] = "no_allowlist_rule"
            payload["results"].append(result)
            continue
        if _has_image_asset_for_entity(state, {
            "entity_name": entity_name,
            "role": role or rule.get("role"),
        }):
            result.update({
                "status": "skipped_existing_image",
                "reason": "existing_safe_image_asset",
                "allowlist_rule_id": rule.get("id"),
            })
            payload["results"].append(result)
            continue
        entity = {
            "entity_name": entity_name,
            "role": role or rule.get("role"),
            "source": source,
            "placement_intent": raw.get("placement_intent") or "supporting_identity",
            "identity_group": raw.get("identity_group") or role or rule.get("role"),
            "required_to_place": bool(raw.get("required_to_place") or raw.get("primary_identity")),
            "primary_identity": bool(raw.get("primary_identity")),
            "allowed_to_place": True,
            "text_badge_fallback_allowed": True,
        }
        candidate = _candidate(
            url,
            discovered_from_url=discovered_from_url,
            discovery_method=str(raw.get("discovery_method") or source),
            score=_planner_candidate_score(raw),
            logo_hint=True,
            entity_hint=True,
            entity_hint_path=True,
        )
        allowed, reason = _candidate_allowed(candidate, rule)
        if not allowed:
            result["status"] = "asset_rejected"
            result["reason"] = reason
            result["allowlist_rule_id"] = rule.get("id")
            payload["results"].append(result)
            continue
        try:
            resource = fetch(url, _MAX_ASSET_BYTES)
        except Exception as e:  # noqa: BLE001
            result["status"] = "fetch_failed"
            result["reason"] = f"asset_fetch_failed:{type(e).__name__}"
            result["allowlist_rule_id"] = rule.get("id")
            payload["results"].append(result)
            continue
        asset, reject_reason = _materialize_asset(
            entity=entity,
            rule=rule,
            candidate=candidate,
            resource=resource,
            layers_dir=layers_dir,
            retrieved_at=retrieved_at,
        )
        if not asset:
            result["status"] = "asset_rejected"
            result["reason"] = reject_reason or "asset_rejected"
            result["allowlist_rule_id"] = rule.get("id")
            payload["results"].append(result)
            continue
        asset["source"] = source
        asset["candidate_provenance"] = {
            "source_url": url,
            "discovered_from_url": discovered_from_url,
            "discovery_method": candidate.get("discovery_method"),
            "notes": raw.get("notes"),
        }
        if source == "designer_author_logo_candidate":
            asset["designer_author_candidate"] = dict(asset["candidate_provenance"])
        state = append_identity_asset(state, asset)
        rec = asset.get("_rendered_layer_record")
        layer_id = str(asset.get("rendered_layer_id") or "")
        if layer_id and isinstance(rec, dict):
            rec["source"] = source
            rendered_layers[layer_id] = rec
        asset.pop("_rendered_layer_record", None)
        result.update({
            "status": "resolved",
            "resolved_asset_id": asset.get("asset_id"),
            "allowlist_rule_id": rule.get("id"),
            "local_asset_path": asset.get("local_asset_path"),
            "rendered_layer_id": asset.get("rendered_layer_id"),
        })
        payload["results"].append(result)

    payload["resolved_count"] = sum(
        1 for item in payload["results"]
        if isinstance(item, dict) and item.get("status") == "resolved"
    )
    payload["status_counts"] = _status_counts(payload["results"])
    state[state_key] = payload
    return refresh_identity_asset_metrics(state), payload


resolve_planner_identity_logo_candidates = resolve_designer_identity_logo_candidates


def load_academic_identity_allowlist(path: Path | None = None) -> dict[str, Any]:
    """Load the tracked academic identity allowlist."""

    allowlist_path = path or ALLOWLIST_PATH
    try:
        with Path(allowlist_path).open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"version": 1, "rules": []}
    except Exception:
        return {"version": 1, "rules": []}
    if not isinstance(data, dict):
        return {"version": 1, "rules": []}
    rules = [rule for rule in (data.get("rules") or []) if isinstance(rule, dict)]
    data["rules"] = rules
    return data


def find_allowlist_rule(
    entity_name: Any,
    *,
    role: Any = None,
    allowlist: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the best allowlist rule for an entity label."""

    entity_key = _entity_key(entity_name)
    if not entity_key:
        return None
    role_text = _normalize_identity_role(role)
    data = allowlist or load_academic_identity_allowlist()
    best: tuple[int, dict[str, Any]] | None = None
    for rule in data.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if role_text and not _roles_compatible(role_text, str(rule.get("role") or "").lower()):
            continue
        aliases = [
            rule.get("entity_name"),
            *(rule.get("aliases") or []),
        ]
        alias_scores = [
            _alias_match_score(entity_name, alias)
            for alias in aliases
        ]
        alias_scores = [score for score in alias_scores if score > 0]
        if not alias_scores:
            continue
        score = max(alias_scores)
        if role_text and _normalize_identity_role(rule.get("role")) == role_text:
            score += 50
        if best is None or score > best[0]:
            best = (score, rule)
    return best[1] if best else None


def _roles_compatible(entity_role: str, rule_role: str) -> bool:
    entity_role = _normalize_identity_role(entity_role)
    rule_role = _normalize_identity_role(rule_role)
    if not entity_role or not rule_role:
        return True
    if entity_role == rule_role:
        return True
    compatible = {
        "venue": {"venue", "publisher", "society", "source"},
        "source": {"venue", "publisher", "society", "source"},
        "publisher": {"venue", "publisher", "society", "source"},
        "institution": {"institution", "lab", "company"},
        "lab": {"institution", "lab", "company"},
        "company": {"institution", "lab", "company"},
        "project": {"project"},
    }
    return rule_role in compatible.get(entity_role, {entity_role})


def _normalize_identity_role(role: Any) -> str:
    text = str(role or "").strip().lower().replace("-", "_")
    aliases = {
        "affiliation": "institution",
        "university": "institution",
        "college": "institution",
        "school": "institution",
        "institute": "institution",
        "laboratory": "lab",
        "research_lab": "lab",
        "conference": "venue",
        "workshop": "venue",
        "journal": "publisher",
        "publisher_journal": "publisher",
        "archive": "source",
        "preprint": "source",
        "repository": "source",
        "org": "society",
        "organization": "society",
        "association": "society",
    }
    return aliases.get(text, text)


def _alias_match_score(entity_name: Any, alias: Any) -> int:
    entity_raw = str(entity_name or "")
    alias_raw = str(alias or "")
    entity_key = _entity_key(entity_raw)
    alias_key = _entity_key(alias_raw)
    if not entity_key or not alias_key:
        return 0
    if entity_key == alias_key:
        return 100 + len(alias_key)
    entity_tokens = [
        _entity_key(token)
        for token in re.split(r"[^A-Za-z0-9]+", entity_raw)
        if _entity_key(token)
    ]
    if len(alias_key) <= 3:
        return 80 + len(alias_key) if alias_key in entity_tokens else 0
    if alias_key in entity_tokens:
        return 90 + len(alias_key)
    if len(alias_key) >= 6 and (entity_key.startswith(alias_key) or alias_key in entity_key):
        return 40 + len(alias_key)
    return 0


def host_allowed_for_identity_url(
    hostname: str,
    *,
    entity_name: Any = "",
    role: Any = None,
    allowlist: dict[str, Any] | None = None,
) -> bool:
    """Return whether ``hostname`` is covered by the identity allowlist."""

    host = str(hostname or "").lower().strip(".")
    if not host:
        return False
    data = allowlist or load_academic_identity_allowlist()
    rule = find_allowlist_rule(entity_name, role=role, allowlist=data) if entity_name else None
    rules = [rule] if rule else [item for item in data.get("rules") or [] if isinstance(item, dict)]
    for item in rules:
        domains = _rule_domains(item, include_asset_domains=True)
        if any(_host_matches(host, domain) for domain in domains):
            return True
    return False


def _resolve_one_entity(
    entity: dict[str, Any],
    rule: dict[str, Any],
    *,
    fetcher: FetchUrl,
    layers_dir: Path,
    retrieved_at: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    entity_name = str(entity.get("entity_name") or rule.get("entity_name") or "").strip()
    result: dict[str, Any] = {
        "entity_name": entity_name,
        "role": entity.get("role") or rule.get("role"),
        "allowlist_rule_id": rule.get("id"),
        "required_to_place": entity.get("required_to_place"),
        "primary_identity": entity.get("primary_identity"),
        "placement_intent": entity.get("placement_intent"),
        "status": "not_found",
        "resolved_asset_id": None,
        "candidates": [],
        "rejected": [],
    }
    candidates: list[dict[str, Any]] = []

    for url in _iter_urls(rule.get("preferred_asset_urls")):
        candidates.append(_candidate(
            url,
            discovered_from_url=None,
            discovery_method="allowlist_preferred_asset",
            score=130,
            logo_hint=True,
            entity_hint=True,
        ))

    page_count = 0
    for page_url in _iter_urls([*(rule.get("preferred_page_urls") or []), *(rule.get("homepages") or [])]):
        if page_count >= _MAX_PAGES_PER_ENTITY:
            break
        if not _official_url_allowed(page_url, rule):
            result["rejected"].append(_reject(page_url, "page_host_not_allowlisted"))
            continue
        page_count += 1
        try:
            page = fetcher(page_url, _MAX_PAGE_BYTES)
        except Exception as e:  # noqa: BLE001
            result["rejected"].append(_reject(page_url, f"page_fetch_failed:{type(e).__name__}"))
            continue
        if page.final_url and not _official_url_allowed(page.final_url, rule):
            result["rejected"].append(_reject(page.final_url, "page_redirected_off_allowlist"))
            continue
        page_type = _content_type(page.content_type)
        if _is_image_content(page_type, page_url):
            candidates.append(_candidate(
                page.final_url or page.url,
                discovered_from_url=None,
                discovery_method="allowlist_page_is_asset",
                score=84,
                logo_hint=True,
                entity_hint=True,
            ))
            continue
        if "html" not in page_type and not _looks_like_html(page.data):
            result["rejected"].append(_reject(page.final_url or page.url, f"page_not_html:{page_type or 'unknown'}"))
            continue
        candidates.extend(_extract_html_candidates(page, rule, entity_name))

    candidates = _dedupe_candidates(candidates)
    candidates.sort(key=_candidate_rank, reverse=True)
    result["candidates"] = [_public_candidate(c) for c in candidates[:_MAX_CANDIDATES_PER_ENTITY]]

    for candidate in candidates[:_MAX_CANDIDATES_PER_ENTITY]:
        allowed, reason = _candidate_allowed(candidate, rule)
        if not allowed:
            result["rejected"].append(_reject(candidate.get("url"), reason, candidate=candidate))
            continue
        if int(candidate.get("score") or 0) < 50:
            result["rejected"].append(_reject(candidate.get("url"), "candidate_score_too_low", candidate=candidate))
            continue
        try:
            asset_response = fetcher(str(candidate["url"]), _MAX_ASSET_BYTES)
        except Exception as e:  # noqa: BLE001
            result["rejected"].append(_reject(candidate.get("url"), f"asset_fetch_failed:{type(e).__name__}", candidate=candidate))
            continue
        asset, reject_reason = _materialize_asset(
            entity=entity,
            rule=rule,
            candidate=candidate,
            resource=asset_response,
            layers_dir=layers_dir,
            retrieved_at=retrieved_at,
        )
        if not asset:
            result["rejected"].append(_reject(candidate.get("url"), reject_reason or "asset_rejected", candidate=candidate))
            continue
        result["status"] = "resolved"
        result["resolved_asset_id"] = asset.get("asset_id")
        result["source_url"] = asset.get("source_url")
        result["discovered_from_url"] = asset.get("discovered_from_url")
        return asset, result

    return None, result


def _extract_html_candidates(
    page: FetchedResource,
    rule: dict[str, Any],
    entity_name: str,
) -> list[dict[str, Any]]:
    base_url = page.final_url or page.url
    soup = BeautifulSoup(page.data, "html.parser")
    candidates: list[dict[str, Any]] = []

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src:
            continue
        text = " ".join(
            str(value or "")
            for value in (
                img.get("alt"),
                img.get("title"),
                img.get("class"),
                img.get("id"),
                src,
            )
        )
        structural_text = " ".join(
            str(value or "")
            for value in (
                img.get("class"),
                img.get("id"),
                src,
            )
        )
        entity_hint = _has_entity_hint(text, rule, entity_name)
        entity_hint_path = _has_entity_hint(src, rule, entity_name)
        logo_hint = _has_logo_hint(structural_text, entity_name)
        score = 82 if logo_hint else 35
        if _url_path_has_logo_hint(src):
            score += 12
            logo_hint = True
        if entity_hint_path:
            score += 20
        candidates.append(_candidate(
            urljoin(base_url, src),
            discovered_from_url=base_url,
            discovery_method="official_page_img",
            score=score,
            logo_hint=logo_hint,
            entity_hint=entity_hint,
            entity_hint_path=entity_hint_path,
        ))

    for link in soup.find_all("link"):
        href = link.get("href")
        if not href:
            continue
        rel_text = " ".join(str(item) for item in (link.get("rel") or []))
        text = " ".join([rel_text, str(link.get("title") or ""), href])
        entity_hint = _has_entity_hint(text, rule, entity_name)
        entity_hint_path = _has_entity_hint(href, rule, entity_name)
        logo_hint = _has_logo_hint(text, entity_name)
        score = 72 if logo_hint else 28
        method = "official_page_link_logo" if logo_hint else "official_page_icon"
        if any(token in rel_text.lower() for token in ("icon", "mask-icon", "apple-touch-icon")):
            score = max(score, 30)
            method = "official_page_icon"
        if _url_path_has_logo_hint(href):
            score += 12
            logo_hint = True
        if entity_hint_path:
            score += 20
        candidates.append(_candidate(
            urljoin(base_url, href),
            discovered_from_url=base_url,
            discovery_method=method,
            score=score,
            logo_hint=logo_hint,
            entity_hint=entity_hint,
            entity_hint_path=entity_hint_path,
        ))

    for meta in soup.find_all("meta"):
        prop = str(meta.get("property") or meta.get("name") or "").lower()
        content = meta.get("content")
        if not content or prop not in {"og:image", "twitter:image", "twitter:image:src"}:
            continue
        entity_hint = _has_entity_hint(content, rule, entity_name)
        logo_hint = _url_path_has_logo_hint(content)
        candidates.append(_candidate(
            urljoin(base_url, content),
            discovered_from_url=base_url,
            discovery_method="official_page_meta_image",
            score=(68 if entity_hint else 48 if logo_hint else 30),
            logo_hint=logo_hint,
            entity_hint=entity_hint,
            entity_hint_path=entity_hint,
        ))

    for anchor in soup.find_all("a"):
        href = anchor.get("href")
        if not href or Path(urlparse(href).path).suffix.lower() not in _IMAGE_EXTS:
            continue
        text = " ".join([anchor.get_text(" ", strip=True), str(anchor.get("title") or ""), href])
        entity_hint = _has_entity_hint(text, rule, entity_name)
        entity_hint_path = _has_entity_hint(href, rule, entity_name)
        logo_hint = _has_logo_hint(text, entity_name)
        if not logo_hint:
            continue
        candidates.append(_candidate(
            urljoin(base_url, href),
            discovered_from_url=base_url,
            discovery_method="official_page_asset_link",
            score=96 if entity_hint_path else 76,
            logo_hint=True,
            entity_hint=entity_hint,
            entity_hint_path=entity_hint_path,
        ))

    return candidates


def _materialize_asset(
    *,
    entity: dict[str, Any],
    rule: dict[str, Any],
    candidate: dict[str, Any],
    resource: FetchedResource,
    layers_dir: Path,
    retrieved_at: str,
) -> tuple[dict[str, Any] | None, str | None]:
    source_url = resource.final_url or resource.url
    final_candidate = dict(candidate)
    final_candidate["url"] = source_url
    allowed, reason = _candidate_allowed(final_candidate, rule)
    if not allowed:
        return None, f"final_{reason}"
    content_type = _content_type(resource.content_type)
    ext = _ext_for_resource(content_type, source_url)
    if ext not in _IMAGE_EXTS:
        return None, f"unsupported_content_type:{content_type or 'unknown'}"
    data = resource.data
    if len(data) > _MAX_ASSET_BYTES:
        return None, "asset_too_large"
    source_sha = hashlib.sha256(data).hexdigest()
    entity_name = str(entity.get("entity_name") or rule.get("entity_name") or "identity asset").strip()
    role = str(entity.get("role") or rule.get("role") or "project").strip()
    layer_id = f"identity_{_slug(entity_name) or 'asset'}_{source_sha[:8]}"
    original_svg_path: Path | None = None

    try:
        if ext == ".svg":
            original_svg_path = layers_dir / f"{layer_id}.svg"
            original_svg_path.write_bytes(data)
            dest = layers_dir / f"{layer_id}.png"
            _rasterize_svg_to_png(data, dest)
            content_path = dest
        else:
            if ext == ".jpeg":
                ext = ".jpg"
            dest = layers_dir / f"{layer_id}{ext}"
            dest.write_bytes(data)
            content_path = dest
        width, height = _validate_logo_image(content_path)
        visual_reject = _logo_asset_visual_reject_reason(
            candidate=final_candidate,
            source_url=source_url,
            content_type=content_type,
            ext=ext,
            width=width,
            height=height,
        )
        if visual_reject:
            for path in (locals().get("dest"), original_svg_path):
                if isinstance(path, Path):
                    try:
                        path.unlink()
                    except OSError:
                        pass
            return None, visual_reject
    except Exception as e:  # noqa: BLE001
        for path in (locals().get("dest"), original_svg_path):
            if isinstance(path, Path):
                try:
                    path.unlink()
                except OSError:
                    pass
        return None, f"image_parse_failed:{type(e).__name__}"

    png_sha = hashlib.sha256(content_path.read_bytes()).hexdigest()
    discovery_method = str(candidate.get("discovery_method") or "allowlist")
    discovered_from_url = candidate.get("discovered_from_url")
    confidence = _asset_confidence(rule, candidate)
    required_to_place = _entity_required_to_place(entity)
    provenance = {
        "identity_source": "academic_identity_search",
        "source_url": source_url,
        "discovered_from_url": discovered_from_url,
        "discovery_method": discovery_method,
        "allowlist_rule_id": rule.get("id"),
        "retrieved_at": retrieved_at,
        "sha256": png_sha,
        "source_sha256": source_sha,
        "content_type": content_type or ("image/svg+xml" if ext == ".svg" else ""),
        "confidence": confidence,
    }
    if original_svg_path:
        provenance["source_svg_path"] = str(original_svg_path)

    rec = {
        "layer_id": layer_id,
        "name": entity_name,
        "kind": "image",
        "z_index": 6,
        "bbox": None,
        "src_path": str(content_path),
        "aspect_ratio": _aspect_from_dims(width, height),
        "image_size": f"{width}x{height}",
        "sha256": png_sha,
        "source": "academic_identity_search",
        "source_url": source_url,
        "source_id": layer_id,
        "is_identity_asset": True,
        "identity_asset_id": layer_id,
        "identity_asset_role": role,
        "identity_entity_name": entity_name,
        "identity_asset_intent": entity.get("placement_intent") or "supporting_identity",
        "identity_required_to_place": required_to_place,
        "identity_allowed_to_place": entity.get("allowed_to_place") is not False,
        "identity_primary": bool(entity.get("primary_identity")),
        "provenance": provenance,
    }
    asset = {
        "asset_id": layer_id,
        "entity_name": entity_name,
        "label": entity_name,
        "role": role,
        "asset_type": "image",
        "rendered_layer_id": layer_id,
        "local_asset_path": str(content_path),
        "source": "academic_identity_search",
        "source_file": None,
        "source_url": source_url,
        "discovered_from_url": discovered_from_url,
        "discovery_method": discovery_method,
        "allowlist_rule_id": rule.get("id"),
        "retrieved_at": retrieved_at,
        "sha256": png_sha,
        "source_sha256": source_sha,
        "content_type": provenance["content_type"],
        "confidence": confidence,
        "safe_to_place": True,
        "allowed_to_place": entity.get("allowed_to_place") is not False,
        "required_to_place": required_to_place,
        "primary_identity": bool(entity.get("primary_identity")),
        "placement_intent": entity.get("placement_intent") or "supporting_identity",
        "identity_group": entity.get("identity_group") or role,
        "canonical_entity_key": entity.get("canonical_entity_key") or _entity_key(entity_name),
        "entity_source": entity.get("source"),
        "text_badge_fallback_allowed": bool(entity.get("text_badge_fallback_allowed", True)),
        "provenance": provenance,
        "placement": {
            "allowed_regions": ["top_left", "top_right"],
            "max_area_ratio": 0.012,
        },
        "_rendered_layer_record": rec,
    }
    if original_svg_path:
        asset["source_svg_path"] = str(original_svg_path)
    return asset, None


def _fetch_url(url: str, max_bytes: int) -> FetchedResource:
    last_error: Exception | None = None
    for attempt in range(_FETCH_RETRIES):
        req = Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urlopen(req, timeout=8) as resp:  # noqa: S310 - URLs are allowlist-checked before fetch
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError(f"response exceeds max_bytes={max_bytes}")
                return FetchedResource(
                    url=url,
                    final_url=resp.geturl(),
                    content_type=str(resp.headers.get("Content-Type") or ""),
                    status=getattr(resp, "status", None),
                    data=data,
                )
        except HTTPError:
            raise
        except (TimeoutError, URLError) as e:
            last_error = e
            if attempt + 1 >= _FETCH_RETRIES:
                raise
            time.sleep(0.35)
    if last_error:
        raise last_error
    raise RuntimeError("fetch failed without an exception")


def _cached_fetcher(fetcher: FetchUrl, *, cache_dir: Path) -> FetchUrl:
    def fetch(url: str, max_bytes: int) -> FetchedResource:
        cached = _read_cached_resource(cache_dir, url=url, max_bytes=max_bytes)
        if cached:
            return cached
        resource = fetcher(url, max_bytes)
        _write_cached_resource(cache_dir, resource)
        return resource

    return fetch


def _read_cached_resource(cache_dir: Path, *, url: str, max_bytes: int) -> FetchedResource | None:
    key = hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()
    meta_path = cache_dir / f"{key}.json"
    data_path = cache_dir / f"{key}.bin"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        data = data_path.read_bytes()
    except Exception:
        return None
    if len(data) > max_bytes:
        return None
    if str(meta.get("url") or "") != str(url or ""):
        return None
    return FetchedResource(
        url=str(meta.get("url") or url),
        final_url=str(meta.get("final_url") or "") or None,
        content_type=str(meta.get("content_type") or ""),
        status=int(meta["status"]) if meta.get("status") is not None else None,
        data=data,
    )


def _write_cached_resource(cache_dir: Path, resource: FetchedResource) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(str(resource.url or "").encode("utf-8")).hexdigest()
        meta_path = cache_dir / f"{key}.json"
        data_path = cache_dir / f"{key}.bin"
        tmp_suffix = f".{os.getpid()}.tmp"
        (cache_dir / f"{key}.bin{tmp_suffix}").write_bytes(resource.data)
        (cache_dir / f"{key}.json{tmp_suffix}").write_text(json.dumps({
            "url": resource.url,
            "final_url": resource.final_url,
            "content_type": resource.content_type,
            "status": resource.status,
            "cached_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }, indent=2, sort_keys=True), encoding="utf-8")
        (cache_dir / f"{key}.bin{tmp_suffix}").replace(data_path)
        (cache_dir / f"{key}.json{tmp_suffix}").replace(meta_path)
    except Exception:
        return


def _rasterize_svg_to_png(data: bytes, dest: Path) -> None:
    import fitz  # pymupdf

    doc = fitz.open(stream=data, filetype="svg")
    if doc.page_count < 1:
        raise ValueError("SVG has no renderable page")
    page = doc[0]
    rect = page.rect
    max_edge = max(float(rect.width), float(rect.height), 1.0)
    scale = max(1.0, min(4.0, 640.0 / max_edge))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=True)
    pix.save(str(dest))


def _validate_logo_image(path: Path) -> tuple[int, int]:
    with Image.open(path) as im:
        width, height = im.size
        im.verify()
    if width < 24 or height < 24:
        raise ValueError(f"logo image too small: {width}x{height}")
    ratio = width / max(1, height)
    if ratio > 12 or ratio < 0.08:
        raise ValueError(f"logo image aspect ratio is implausible: {ratio:.3f}")
    return width, height


def _logo_asset_visual_reject_reason(
    *,
    candidate: dict[str, Any],
    source_url: str,
    content_type: str,
    ext: str,
    width: int,
    height: int,
) -> str | None:
    """Reject common official-page media assets that are not usable logos."""

    method = str(candidate.get("discovery_method") or "")
    parsed = urlparse(str(source_url or ""))
    path = parsed.path.lower()
    path_with_query = f"{path}?{parsed.query.lower()}" if parsed.query else path
    raster = ext in {".jpg", ".jpeg", ".png", ".webp"} or content_type in _RASTER_CONTENT_TYPES
    has_logo_path = _url_path_has_explicit_logo_hint(source_url)
    if not raster:
        if not has_logo_path and any(term in path_with_query for term in _NON_LOGO_VECTOR_PATH_TERMS):
            return "non_logo_vector_asset_path"
        return None
    ratio = width / max(1, height)
    area = width * height
    media_path = any(term in path_with_query for term in _NON_LOGO_RASTER_PATH_TERMS)
    large_media = area >= 240_000 and 1.45 <= ratio <= 2.2
    if method == "official_page_meta_image" and not has_logo_path:
        return "non_logo_meta_image"
    if method == "official_page_img" and large_media and media_path and not has_logo_path:
        return "non_logo_official_page_media"
    if method.startswith("designer_author") or method == "identity_logo_agent":
        if large_media and media_path and not has_logo_path:
            return "non_logo_candidate_media"
    return None


def _entities_to_resolve(state: dict[str, Any], *, brief: str) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    for item in state.get("entities") or []:
        if isinstance(item, dict) and item.get("entity_name"):
            entities.append(dict(item))
    for asset in state.get("assets") or []:
        if not isinstance(asset, dict) or not asset.get("entity_name"):
            continue
        entities.append({
            "entity_name": asset.get("entity_name"),
            "role": asset.get("role"),
            "source": asset.get("source") or "identity_asset",
            "confidence": asset.get("confidence"),
            "allowed_to_place": asset.get("allowed_to_place"),
            "required_to_place": asset.get("required_to_place"),
            "primary_identity": asset.get("primary_identity"),
            "placement_intent": asset.get("placement_intent"),
            "identity_group": asset.get("identity_group"),
            "canonical_entity_key": asset.get("canonical_entity_key"),
        })
    if brief:
        for token in re.findall(r"\b[A-Z][A-Za-z&.\-]*(?:\s+[A-Z][A-Za-z&.\-]*){0,3}\b", brief):
            if any(term in token.lower() for term in ("university", "institute", "conference", "lab")):
                entities.append({"entity_name": token, "role": None, "source": "brief_phrase", "confidence": 0.45})

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entity in entities:
        if not _entity_should_resolve(entity):
            continue
        key = _entity_dedupe_key(entity)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(entity)
    return out[:10]


def _entity_should_resolve(entity: dict[str, Any]) -> bool:
    if entity.get("allowed_to_place") is False:
        return False
    if str(entity.get("placement_intent") or "").lower() in {"context_mention", "reference_only", "do_not_place"}:
        return False
    return True


def _entity_dedupe_key(entity: dict[str, Any]) -> str:
    role = str(entity.get("role") or "").lower()
    if role == "venue":
        key = _venue_base_key(entity.get("entity_name"))
        return f"venue:{key}" if key else ""
    return _entity_key(entity.get("entity_name"))


def _has_image_asset_for_entity(state: dict[str, Any], entity: dict[str, Any]) -> bool:
    entity_key = _entity_key(entity.get("entity_name"))
    if not entity_key:
        return False
    for asset in state.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        if asset.get("asset_type") != "image" or not asset.get("safe_to_place"):
            continue
        if _entity_key(asset.get("entity_name")) == entity_key:
            return True
    return False


def _official_url_allowed(url: str, rule: dict[str, Any]) -> bool:
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    return any(_host_matches(parsed.hostname, domain) for domain in _rule_domains(rule))


def _candidate_allowed(candidate: dict[str, Any], rule: dict[str, Any]) -> tuple[bool, str]:
    url = str(candidate.get("url") or "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False, "asset_url_not_https"
    method = str(candidate.get("discovery_method") or "")
    if not method.startswith("allowlist_preferred"):
        path = parsed.path.lower()
        path_with_query = f"{path}?{parsed.query.lower()}" if parsed.query else path
        if any(term in path_with_query for term in _BLOCKED_ASSET_PATH_TERMS):
            return False, "asset_path_blocked"
    role = str(rule.get("role") or "").lower()
    if method.startswith("official_page") and role in {"institution", "lab", "company"}:
        if method == "official_page_icon":
            return False, "candidate_icon_not_logo"
        if not candidate.get("entity_hint"):
            return False, "candidate_entity_hint_missing"
        if method == "official_page_img" and not candidate.get("logo_hint"):
            return False, "candidate_logo_hint_missing"
    host = parsed.hostname.lower()
    if any(_host_matches(host, domain) for domain in _BLOCKED_EXTERNAL_DOMAINS):
        return False, "external_domain_blocked"
    if any(_host_matches(host, domain) for domain in _rule_domains(rule, include_asset_domains=True)):
        return True, "allowlisted_host"
    if method.startswith("designer_author"):
        return False, "asset_host_not_allowlisted"
    discovered_from = str(candidate.get("discovered_from_url") or "")
    if discovered_from and bool(candidate.get("logo_hint")) and _official_url_allowed(discovered_from, rule):
        return True, "official_page_external_asset"
    return False, "asset_host_not_allowlisted"


def _rule_domains(rule: dict[str, Any], *, include_asset_domains: bool = False) -> list[str]:
    domains = [str(item).lower().strip(".") for item in (rule.get("official_domains") or []) if str(item).strip()]
    if include_asset_domains:
        domains.extend(
            str(item).lower().strip(".")
            for item in (rule.get("allowed_asset_domains") or [])
            if str(item).strip()
        )
    return domains


def _host_matches(hostname: str, domain: str) -> bool:
    host = hostname.lower().strip(".")
    dom = domain.lower().strip(".")
    return bool(dom) and (host == dom or host.endswith("." + dom))


def _candidate(
    url: str,
    *,
    discovered_from_url: str | None,
    discovery_method: str,
    score: int,
    logo_hint: bool,
    entity_hint: bool = False,
    entity_hint_path: bool = False,
) -> dict[str, Any]:
    return {
        "url": url,
        "discovered_from_url": discovered_from_url,
        "discovery_method": discovery_method,
        "score": int(score),
        "logo_hint": bool(logo_hint),
        "entity_hint": bool(entity_hint),
        "entity_hint_path": bool(entity_hint_path),
    }


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in candidates:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        prior = out.get(url)
        if prior is None or int(item.get("score") or 0) > int(prior.get("score") or 0):
            out[url] = item
    return list(out.values())


def _candidate_rank(candidate: dict[str, Any]) -> tuple[int, int, int]:
    url = str(candidate.get("url") or "")
    ext = Path(urlparse(url).path).suffix.lower()
    format_bonus = 8 if ext == ".svg" else 4 if ext in {".png", ".webp"} else 0
    explicit_logo_bonus = 30 if _url_path_has_explicit_logo_hint(url) else 0
    hint_bonus = 20 if candidate.get("entity_hint_path") else 8 if candidate.get("logo_hint") else 0
    method_bonus = 40 if str(candidate.get("discovery_method") or "") == "allowlist_preferred_asset" else 0
    return (int(candidate.get("score") or 0) + method_bonus + format_bonus + explicit_logo_bonus + hint_bonus, len(url), 1)


def _public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": candidate.get("url"),
        "discovered_from_url": candidate.get("discovered_from_url"),
        "discovery_method": candidate.get("discovery_method"),
        "score": candidate.get("score"),
        "logo_hint": candidate.get("logo_hint"),
        "entity_hint": candidate.get("entity_hint"),
        "entity_hint_path": candidate.get("entity_hint_path"),
    }


def _reject(url: Any, reason: str, *, candidate: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "url": url,
        "reason": reason,
    }
    if candidate:
        payload.update({
            "discovered_from_url": candidate.get("discovered_from_url"),
            "discovery_method": candidate.get("discovery_method"),
            "score": candidate.get("score"),
            "entity_hint": candidate.get("entity_hint"),
            "entity_hint_path": candidate.get("entity_hint_path"),
        })
    return payload


def _status_counts(results: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _identity_search_failure_category(result: dict[str, Any]) -> str | None:
    status = str(result.get("status") or "")
    if status in {"resolved", "skipped_existing_image", "disabled"}:
        return None
    if status in {"no_allowlist_rule", "fetch_failed", "asset_rejected", "not_found"}:
        if status != "not_found":
            return status
    reasons = [
        str(item.get("reason") or "")
        for item in (result.get("rejected") or [])
        if isinstance(item, dict)
    ]
    reason_text = " ".join(reasons)
    if "fetch_failed" in reason_text:
        return "fetch_failed"
    if reasons or any(term in reason_text for term in ("rejected", "unsupported", "too_large", "blocked", "not_allowlisted")):
        return "asset_rejected"
    return "not_found" if status == "not_found" else None


def _planner_candidate_score(raw: dict[str, Any]) -> int:
    for key in ("priority", "score"):
        value = raw.get(key)
        if value is None:
            continue
        try:
            return max(1, min(100, int(float(value))))
        except Exception:
            continue
    confidence = raw.get("confidence")
    if confidence is not None:
        try:
            value = float(confidence)
            return max(1, min(100, int(round(value * 100 if value <= 1 else value))))
        except Exception:
            pass
    return 88


def _iter_urls(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    return [str(item).strip() for item in (values or []) if str(item).strip()]


def _content_type(value: Any) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def _is_image_content(content_type: str, url: str) -> bool:
    return content_type in _RASTER_CONTENT_TYPES or content_type in _SVG_CONTENT_TYPES or Path(urlparse(url).path).suffix.lower() in _IMAGE_EXTS


def _ext_for_resource(content_type: str, url: str) -> str:
    if content_type in _RASTER_CONTENT_TYPES:
        return _RASTER_CONTENT_TYPES[content_type]
    if content_type in _SVG_CONTENT_TYPES:
        return ".svg"
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix == ".jpeg":
        return ".jpg"
    return suffix if suffix in _IMAGE_EXTS else ""


def _looks_like_html(data: bytes) -> bool:
    head = data[:256].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<body" in head


def _has_logo_hint(text: str, entity_name: str) -> bool:
    lower = str(text or "").lower()
    return any(term in lower for term in _LOGO_TERMS)


def _has_entity_hint(text: str, rule: dict[str, Any], entity_name: str) -> bool:
    hostless_text = re.sub(r"(?:https?:)?//[^/\s\"']+", "", str(text or ""), flags=re.IGNORECASE)
    compact = _entity_key(hostless_text)
    if not compact:
        return False
    text_tokens = {
        _entity_key(token)
        for token in re.split(r"[^A-Za-z0-9]+", hostless_text)
        if _entity_key(token)
    }
    aliases = [entity_name, rule.get("entity_name"), *(rule.get("aliases") or [])]
    stopwords = {"university", "institute", "college", "school", "research", "laboratory", "lab"}
    for alias in aliases:
        alias_key = _entity_key(alias)
        if not alias_key:
            continue
        if len(alias_key) <= 3:
            if alias_key in text_tokens:
                return True
            continue
        if len(alias_key) >= 6 and alias_key in compact:
            return True
        for token in re.split(r"[^A-Za-z0-9]+", str(alias or "")):
            token_key = _entity_key(token)
            if len(token_key) >= 4 and token_key not in stopwords and token_key in compact:
                return True
    return False


def _entity_required_to_place(entity: dict[str, Any]) -> bool:
    if entity.get("required_to_place"):
        return True
    source = str(entity.get("source") or "").lower()
    role = str(entity.get("role") or "").lower()
    return source == "affiliation" and role in {"institution", "lab", "company"}


def _url_path_has_logo_hint(url: str) -> bool:
    path = urlparse(str(url or "")).path.lower()
    return any(term in path for term in _LOGO_TERMS)


def _url_path_has_explicit_logo_hint(url: str) -> bool:
    path = urlparse(str(url or "")).path.lower()
    return any(term in path for term in _EXPLICIT_LOGO_PATH_TERMS)


def _asset_confidence(rule: dict[str, Any], candidate: dict[str, Any]) -> float:
    base = _safe_float(rule.get("confidence"), 0.78)
    score = int(candidate.get("score") or 0)
    bonus = 0.08 if str(candidate.get("discovery_method") or "").startswith("allowlist_preferred") else 0.0
    if candidate.get("discovered_from_url"):
        bonus += 0.03
    return round(min(0.96, max(base, 0.68) + min(score, 100) / 1000.0 + bonus), 2)


def _aspect_from_dims(w: int, h: int) -> str:
    if w <= 0 or h <= 0:
        return "1:1"
    if abs(w - h) / max(w, h) < 0.05:
        return "1:1"
    if w > h:
        return "16:9" if w / h > 1.6 else "3:2"
    return "3:4" if h / w < 1.45 else "2:3"


def _entity_key(value: Any) -> str:
    text = re.sub(r"\b20\d{2}\b", "", str(value or "").lower())
    return re.sub(r"[^a-z0-9]+", "", text)


def _venue_base_key(value: Any) -> str:
    key = _entity_key(value)
    if key == "nips":
        return "neurips"
    return key


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")[:48]


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default
