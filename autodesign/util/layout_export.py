"""Serialize in-browser drag/resize state to a portable layout.json payload.

The landing HTML renderer writes a ``.draggable-resizable`` wrapper around every
image layer and persists bbox edits into ``localStorage`` under
``autodesign.layout.<run_id>``. Existing ``designanything.layout.<run_id>``
state remains readable. The toolbar's ``📐 layout.json`` button calls
into the browser with the live state dict; this module defines the canonical
shape of that payload so a future re-ingest pass (v2.5+) can consume it as
authoritative layout truth.

The function is intentionally tiny and side-effect-free so it can be unit-tested
without a browser. Keep the schema in sync with ``LS_KEY`` consumers in
``autodesign/tools/html_renderer.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

LAYOUT_SCHEMA = "autodesign.layout.v1"


@dataclass(frozen=True)
class LayerLayout:
    """Per-layer bbox override captured from the browser."""

    layer_id: str
    tx: float = 0.0
    ty: float = 0.0
    w: float | None = None
    h: float | None = None

    def to_dict(self) -> dict[str, float | str | None]:
        return {
            "layer_id": self.layer_id,
            "tx": float(self.tx),
            "ty": float(self.ty),
            "w": None if self.w is None else float(self.w),
            "h": None if self.h is None else float(self.h),
        }


def _coerce_layer(layer_id: str, raw: Mapping[str, object]) -> LayerLayout:
    def _num(key: str, default: float | None = 0.0) -> float | None:
        value = raw.get(key)
        if value is None or value == "":
            return default
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    return LayerLayout(
        layer_id=layer_id,
        tx=_num("tx", 0.0) or 0.0,
        ty=_num("ty", 0.0) or 0.0,
        w=_num("w", None),
        h=_num("h", None),
    )


def layout_state_to_json(
    state: Mapping[str, Mapping[str, object]],
    *,
    run_id: str | None = None,
    artifact_type: str = "landing",
) -> bytes:
    """Serialize a ``{layer_id: {tx, ty, w, h}}`` dict to a layout.json blob.

    The returned bytes are valid UTF-8 JSON the browser can hand straight to a
    ``Blob`` download, and a re-ingest pipeline can parse without transforms.
    Unknown keys in each layer's dict are dropped; missing numeric fields fall
    back to the frozen-default in :class:`LayerLayout`.
    """
    layers = [_coerce_layer(str(k), v).to_dict() for k, v in state.items()]
    payload: dict[str, object] = {
        "schema": LAYOUT_SCHEMA,
        "artifact_type": artifact_type,
        "layers": layers,
    }
    if run_id:
        payload["run_id"] = run_id
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
