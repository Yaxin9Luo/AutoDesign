"""Persistent run-event telemetry summaries."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from .io import atomic_write_json


def write_run_telemetry_summary(run_dir: str | Path) -> Path | None:
    """Aggregate run_events.jsonl into a compact timing/token/cost report."""
    root = Path(run_dir)
    events_path = root / "run_events.jsonl"
    if not events_path.exists():
        return None
    events = _read_events(events_path)
    if not events:
        return None
    summary = build_run_telemetry_summary(events)
    return atomic_write_json(root / "run_telemetry_summary.json", summary)


def build_run_telemetry_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    first_ms = _safe_int(events[0].get("ts_epoch_ms"))
    last_ms = _safe_int(events[-1].get("ts_epoch_ms"))
    stage_stats: dict[str, dict[str, Any]] = {}
    for event in events:
        name = str(event.get("event") or "unknown")
        stage = name.split(".", 1)[0] or "unknown"
        stats = stage_stats.setdefault(stage, {
            "event_count": 0,
            "first_event": name,
            "last_event": name,
            "first_ts_epoch_ms": _safe_int(event.get("ts_epoch_ms")),
            "last_ts_epoch_ms": _safe_int(event.get("ts_epoch_ms")),
            "explicit_wall_s_sum": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_create_tokens": 0,
        })
        stats["event_count"] += 1
        stats["last_event"] = name
        stats["last_ts_epoch_ms"] = _safe_int(event.get("ts_epoch_ms"))
        stats["explicit_wall_s_sum"] = round(
            float(stats["explicit_wall_s_sum"]) + _safe_float(event.get("wall_s")),
            3,
        )
        stats["input_tokens"] += _safe_int(event.get("input_tokens"))
        stats["output_tokens"] += _safe_int(event.get("output_tokens"))
        stats["cache_read_tokens"] += _safe_int(
            event.get("cache_read_tokens", event.get("cache_read")),
        )
        stats["cache_create_tokens"] += _safe_int(
            event.get("cache_create_tokens", event.get("cache_create")),
        )

    for stats in stage_stats.values():
        start = _safe_int(stats.get("first_ts_epoch_ms"))
        end = _safe_int(stats.get("last_ts_epoch_ms"))
        stats["observed_wall_s"] = round(max(0, end - start) / 1000.0, 3)

    run_done = _last_event(events, "run.done") or {}
    run_token_totals = {
        "input_tokens": _safe_int(run_done.get("total_input_tokens")),
        "output_tokens": _safe_int(run_done.get("total_output_tokens")),
        "cache_read_tokens": _safe_int(run_done.get("total_cache_read_tokens")),
        "cache_create_tokens": _safe_int(run_done.get("total_cache_create_tokens")),
    }
    model_usage = _model_usage(events, run_done)
    estimated_cost = _estimate_cost(model_usage)
    return {
        "event_count": len(events),
        "first_ts": events[0].get("ts"),
        "last_ts": events[-1].get("ts"),
        "observed_wall_s": (
            round(max(0, last_ms - first_ms) / 1000.0, 3)
            if first_ms and last_ms else None
        ),
        "run_done_wall_s": run_done.get("wall_s"),
        "terminal_status": run_done.get("terminal_status"),
        "stage_stats": stage_stats,
        "run_token_totals": run_token_totals,
        "model_usage": model_usage,
        "estimated_cost": estimated_cost,
    }


def _read_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _last_event(events: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event") == name:
            return event
    return None


def _model_usage(
    events: list[dict[str, Any]],
    run_done: dict[str, Any],
) -> dict[str, dict[str, int]]:
    usage: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_create_tokens": 0,
        }
    )
    for event in events:
        model = str(event.get("model") or "").strip()
        if not model:
            continue
        input_tokens = _safe_int(event.get("input_tokens"))
        output_tokens = _safe_int(event.get("output_tokens"))
        if input_tokens <= 0 and output_tokens <= 0:
            continue
        bucket = usage[model]
        bucket["input_tokens"] += input_tokens
        bucket["output_tokens"] += output_tokens
        bucket["cache_read_tokens"] += _safe_int(
            event.get("cache_read_tokens", event.get("cache_read")),
        )
        bucket["cache_create_tokens"] += _safe_int(
            event.get("cache_create_tokens", event.get("cache_create")),
        )

    designer_model = str(run_done.get("designer_model") or "").strip()
    if designer_model:
        bucket = usage[designer_model]
        bucket["input_tokens"] += _safe_int(run_done.get("total_input_tokens"))
        bucket["output_tokens"] += _safe_int(run_done.get("total_output_tokens"))
        bucket["cache_read_tokens"] += _safe_int(run_done.get("total_cache_read_tokens"))
        bucket["cache_create_tokens"] += _safe_int(run_done.get("total_cache_create_tokens"))
    return {model: dict(tokens) for model, tokens in sorted(usage.items())}


def _estimate_cost(model_usage: dict[str, dict[str, int]]) -> dict[str, Any]:
    prices = _load_prices()
    if not prices:
        return {
            "estimated_total_usd": None,
            "reason": "set AUTODESIGN_MODEL_PRICES_JSON for price estimates",
        }
    by_model: dict[str, Any] = {}
    missing: list[str] = []
    total = 0.0
    for model, usage in model_usage.items():
        price = prices.get(model)
        if not isinstance(price, dict):
            missing.append(model)
            continue
        input_rate = _safe_float(price.get("input_per_million", price.get("input")))
        output_rate = _safe_float(price.get("output_per_million", price.get("output")))
        cache_read_rate = _safe_float(price.get("cache_read_per_million", price.get("cache_read")))
        cache_create_rate = _safe_float(price.get("cache_create_per_million", price.get("cache_create")))
        cost = (
            usage.get("input_tokens", 0) * input_rate
            + usage.get("output_tokens", 0) * output_rate
            + usage.get("cache_read_tokens", 0) * cache_read_rate
            + usage.get("cache_create_tokens", 0) * cache_create_rate
        ) / 1_000_000.0
        total += cost
        by_model[model] = {
            "estimated_usd": round(cost, 6),
            "usage": usage,
        }
    return {
        "estimated_total_usd": round(total, 6),
        "by_model": by_model,
        "unpriced_models": missing,
    }


def _load_prices() -> dict[str, Any]:
    raw = (
        os.getenv("AUTODESIGN_MODEL_PRICES_JSON", "").strip()
        or os.getenv("DESIGN_ANYTHING_MODEL_PRICES_JSON", "").strip()
    )
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
