"""Small JSON cache helpers for expensive pipeline stages."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .io import atomic_write_json


def pipeline_cache_enabled(kind: str | None = None) -> bool:
    canonical_names = []
    legacy_names = []
    if kind:
        safe = "".join(ch if ch.isalnum() else "_" for ch in kind.upper())
        canonical_names.append(f"AUTODESIGN_{safe}_CACHE")
        legacy_names.append(f"DESIGN_ANYTHING_{safe}_CACHE")
    names = [
        *canonical_names,
        "AUTODESIGN_PIPELINE_CACHE",
        *legacy_names,
        "DESIGN_ANYTHING_PIPELINE_CACHE",
    ]
    raw = ""
    for name in names:
        value = os.getenv(name)
        if value is not None:
            raw = value.strip()
            break
    if raw == "":
        return True
    return raw.lower() not in {"0", "false", "no", "off"}


def stable_cache_key(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def cache_entry_dir(settings: Any, kind: str, key: str) -> Path:
    return Path(settings.out_dir) / "cache" / kind / key[:2] / key


def read_json_cache(settings: Any, kind: str, key: str) -> dict[str, Any] | None:
    if not pipeline_cache_enabled(kind):
        return None
    path = cache_entry_dir(settings, kind, key) / "payload.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def write_json_cache(settings: Any, kind: str, key: str, payload: dict[str, Any]) -> Path | None:
    if not pipeline_cache_enabled(kind):
        return None
    path = cache_entry_dir(settings, kind, key) / "payload.json"
    try:
        return atomic_write_json(path, payload)
    except Exception:
        return None
