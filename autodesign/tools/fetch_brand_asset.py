"""Fetch or resolve a brand/academic identity asset.

The default path resolves assets already known to the run, especially
``academic_identity_assets`` produced by ``ingest_document``. A planner may
also provide an explicit URL; that path is intentionally allowlisted and raster
only, avoiding open-ended logo search inside the harness.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from PIL import Image

from ._contract import ToolContext, obs_error, obs_ok
from ..schema import ToolResultRecord
from ..util.academic_identity import append_identity_asset, find_identity_asset
from ..util.academic_identity_search import find_allowlist_rule, host_allowed_for_identity_url
from ..util.io import atomic_write_json


_MAX_ASSET_BYTES = 4 * 1024 * 1024
_TRUSTED_DOMAINS = {
    "neurips.cc",
    "nips.cc",
    "icml.cc",
    "iclr.cc",
    "thecvf.com",
    "cvpr.thecvf.com",
    "aclweb.org",
    "aaai.org",
    "siggraph.org",
    "kdd.org",
    "acm.org",
    "ieee.org",
    "mit.edu",
    "stanford.edu",
    "berkeley.edu",
    "cmu.edu",
    "princeton.edu",
    "harvard.edu",
    "ox.ac.uk",
    "cam.ac.uk",
    "tsinghua.edu.cn",
    "pku.edu.cn",
    "sjtu.edu.cn",
    "zju.edu.cn",
}
_CONTENT_TYPE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}


def fetch_brand_asset(args: dict[str, Any], *, ctx: ToolContext) -> ToolResultRecord:
    asset_id = str(args.get("asset_id") or args.get("entity_name") or "").strip()
    source_url = str(args.get("source_url") or "").strip()
    if source_url:
        return _fetch_explicit_url(args, ctx=ctx)

    asset = find_identity_asset(ctx.state.get("academic_identity_assets"), asset_id)
    if asset:
        return obs_ok({
            "asset": asset,
            "asset_id": asset.get("asset_id"),
            "rendered_layer_id": asset.get("rendered_layer_id"),
            "local_asset_path": asset.get("local_asset_path"),
            "source": asset.get("source"),
            "safe_to_place": asset.get("safe_to_place"),
        })

    rendered = ctx.state.get("rendered_layers") or {}
    rec = rendered.get(asset_id)
    if isinstance(rec, dict) and rec.get("src_path"):
        payload = {
            "asset_id": asset_id,
            "entity_name": rec.get("identity_entity_name") or rec.get("name") or asset_id,
            "label": rec.get("identity_entity_name") or rec.get("name") or asset_id,
            "role": rec.get("identity_asset_role") or "project",
            "asset_type": "image",
            "rendered_layer_id": asset_id,
            "local_asset_path": rec.get("src_path"),
            "source": rec.get("source") or "rendered_layers",
            "source_file": rec.get("source_file"),
            "source_url": rec.get("source_url"),
            "confidence": 0.6,
            "safe_to_place": bool(rec.get("is_identity_asset")),
        }
        return obs_ok({
            "asset": payload,
            "asset_id": payload["asset_id"],
            "rendered_layer_id": payload["rendered_layer_id"],
            "local_asset_path": payload["local_asset_path"],
            "source": payload["source"],
            "safe_to_place": payload["safe_to_place"],
        })

    available = [
        {
            "asset_id": item.get("asset_id"),
            "entity_name": item.get("entity_name"),
            "rendered_layer_id": item.get("rendered_layer_id"),
            "asset_type": item.get("asset_type"),
        }
        for item in ((ctx.state.get("academic_identity_assets") or {}).get("assets") or [])
        if isinstance(item, dict)
    ][:12]
    return obs_error(
        f"brand_asset '{asset_id or '<unspecified>'}' not found",
        category="not_found",
        payload={"asset_id": asset_id, "available_identity_assets": available},
    )


def _fetch_explicit_url(args: dict[str, Any], *, ctx: ToolContext) -> ToolResultRecord:
    source_url = str(args.get("source_url") or "").strip()
    entity_name = str(args.get("entity_name") or args.get("asset_id") or "brand asset").strip()
    role = str(args.get("role") or "project").strip()
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return obs_error(
            "source_url must be an https URL on a trusted academic/official domain",
            category="validation",
            payload={"source_url": source_url},
        )
    if not _trusted_host(parsed.hostname, entity_name=entity_name, role=role):
        return obs_error(
            f"source_url host is not trusted for explicit logo fetch: {parsed.hostname}",
            category="validation",
            payload={
                "source_url": source_url,
                "trusted_domains": sorted(_TRUSTED_DOMAINS),
                "allowlist_required": True,
            },
        )

    try:
        req = Request(source_url, headers={"User-Agent": "AutoDesign/academic-identity-assets"})
        with urlopen(req, timeout=12) as resp:  # noqa: S310 - allowlisted URL fetch
            content_type = str(resp.headers.get("Content-Type") or "").split(";", 1)[0].lower()
            data = resp.read(_MAX_ASSET_BYTES + 1)
    except Exception as e:  # noqa: BLE001
        return obs_error(
            f"brand_asset fetch failed: {type(e).__name__}: {e}",
            category="api",
            payload={"source_url": source_url},
        )
    if len(data) > _MAX_ASSET_BYTES:
        return obs_error(
            f"brand_asset is too large ({len(data)} bytes > {_MAX_ASSET_BYTES})",
            category="validation",
            payload={"source_url": source_url},
        )

    ext = _CONTENT_TYPE_EXT.get(content_type) or _suffix_from_path(parsed.path)
    if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
        return obs_error(
            "brand_asset fetch only accepts raster PNG/JPEG/WebP assets",
            category="unsupported_format",
            payload={"source_url": source_url, "content_type": content_type},
        )
    if ext == ".jpeg":
        ext = ".jpg"

    sha = hashlib.sha256(data).hexdigest()
    layer_id = f"brand_{_slug(entity_name) or 'asset'}_{sha[:8]}"
    dest = ctx.layers_dir / f"{layer_id}{ext}"
    dest.write_bytes(data)
    try:
        with Image.open(dest) as im:
            w, h = im.size
    except Exception as e:  # noqa: BLE001
        try:
            dest.unlink()
        except OSError:
            pass
        return obs_error(
            f"brand_asset image is not readable: {type(e).__name__}: {e}",
            category="parse_error",
            payload={"source_url": source_url},
        )

    retrieved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    allowlist_rule = find_allowlist_rule(entity_name, role=role)
    provenance = {
        "identity_source": "official_url",
        "source_url": source_url,
        "discovered_from_url": None,
        "discovery_method": "explicit_url",
        "allowlist_rule_id": (allowlist_rule or {}).get("id"),
        "retrieved_at": retrieved_at,
        "sha256": sha,
        "content_type": content_type,
        "confidence": 0.86,
    }
    rec = {
        "layer_id": layer_id,
        "name": entity_name,
        "kind": "image",
        "z_index": 6,
        "bbox": None,
        "src_path": str(dest),
        "aspect_ratio": _aspect_from_dims(w, h),
        "image_size": f"{w}x{h}",
        "sha256": sha,
        "source": "brand_asset_fetch",
        "source_url": source_url,
        "source_id": layer_id,
        "is_identity_asset": True,
        "identity_asset_id": layer_id,
        "identity_asset_role": role,
        "identity_entity_name": entity_name,
        "provenance": provenance,
    }
    ctx.state.setdefault("rendered_layers", {})[layer_id] = rec

    asset = {
        "asset_id": layer_id,
        "entity_name": entity_name,
        "label": entity_name,
        "role": role,
        "asset_type": "image",
        "rendered_layer_id": layer_id,
        "local_asset_path": str(dest),
        "source": "official_url",
        "source_file": None,
        "source_url": source_url,
        "discovered_from_url": None,
        "discovery_method": "explicit_url",
        "allowlist_rule_id": provenance.get("allowlist_rule_id"),
        "retrieved_at": retrieved_at,
        "sha256": sha,
        "content_type": content_type,
        "confidence": 0.86,
        "safe_to_place": True,
        "provenance": provenance,
        "placement": {
            "allowed_regions": ["top_left", "top_right"],
            "max_area_ratio": 0.012,
        },
    }
    identity_state = append_identity_asset(ctx.state.get("academic_identity_assets"), asset)
    ctx.state["academic_identity_assets"] = identity_state
    atomic_write_json(ctx.run_dir / "academic_identity_assets.json", identity_state)

    return obs_ok({
        "asset": asset,
        "asset_id": layer_id,
        "rendered_layer_id": layer_id,
        "local_asset_path": str(dest),
        "source": "official_url",
        "safe_to_place": True,
    })


def _trusted_host(hostname: str, *, entity_name: str = "", role: str = "") -> bool:
    host = hostname.lower()
    if host_allowed_for_identity_url(host, entity_name=entity_name, role=role):
        return True
    return any(host == domain or host.endswith("." + domain) for domain in _TRUSTED_DOMAINS)


def _suffix_from_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return suffix if suffix in {".png", ".jpg", ".jpeg", ".webp"} else ""


def _aspect_from_dims(w: int, h: int) -> str:
    if w <= 0 or h <= 0:
        return "1:1"
    if abs(w - h) / max(w, h) < 0.05:
        return "1:1"
    if w > h:
        return "16:9" if w / h > 1.6 else "3:2"
    return "3:4" if h / w < 1.45 else "2:3"


def _slug(value: Any) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")[:48]
