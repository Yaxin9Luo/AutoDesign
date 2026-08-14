"""Stable, sortable IDs for runs and layers."""

from __future__ import annotations

import uuid
from datetime import datetime


def new_run_id() -> str:
    """Sortable run id: YYYYMMDD-HHMMSS-shortuuid."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    short = uuid.uuid4().hex[:8]
    return f"{ts}-{short}"


def new_layer_id(prefix: str = "L") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"
