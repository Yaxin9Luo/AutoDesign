"""Deterministic fingerprint for binding derived deliveries to a DesignSpec."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any


def design_spec_sha256(spec: Any) -> str:
    if hasattr(spec, "model_dump"):
        payload = spec.model_dump(mode="json")
    elif isinstance(spec, dict):
        payload = spec
    else:
        raise TypeError("design spec must be a Pydantic model or mapping")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()
