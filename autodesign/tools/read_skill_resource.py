"""Read a stage-scoped v2 runtime skill resource from the selected snapshot."""

from __future__ import annotations

from typing import Any

from ..skills.registry import SkillBundle
from ..util.logging import log
from ._contract import ToolContext, obs_error, obs_ok


def read_skill_resource(args: dict[str, Any], *, ctx: ToolContext):
    skill_id = str(args.get("skill_id") or "").strip()
    resource_id = str(args.get("resource_id") or "").strip()
    stage = str(ctx.state.get("runtime_skill_stage") or "").strip().lower()
    if not skill_id or not resource_id or not stage:
        return obs_error(
            "runtime skill resource request is missing skill_id, resource_id, or active stage",
            category="validation",
        )

    bundle = SkillBundle.from_runtime_state(ctx.state.get("skills"))
    pack = bundle.get(skill_id)
    content = bundle.read_resource(
        skill_id=skill_id,
        resource_id=resource_id,
        stage=stage,
    )
    resource = pack.resource(resource_id) if pack is not None else None
    if content is None or pack is None or resource is None:
        return obs_error(
            "runtime skill resource is unavailable for the selected active stage",
            category="validation",
        )

    telemetry = ctx.state.setdefault("runtime_skill_resource_telemetry", {
        "read_count": 0,
        "unique_resources": [],
        "char_count": 0,
    })
    unique_resources = set(telemetry.get("unique_resources") or [])
    unique_resources.add(f"{skill_id}:{resource_id}")
    telemetry["read_count"] = int(telemetry.get("read_count") or 0) + 1
    telemetry["unique_resources"] = sorted(unique_resources)
    telemetry["char_count"] = int(telemetry.get("char_count") or 0) + len(content)
    log(
        "skills.resource.read",
        skill_id=skill_id,
        resource_id=resource_id,
        stage=stage,
        chars=len(content),
        read_count=telemetry["read_count"],
        unique_count=len(unique_resources),
        char_count=telemetry["char_count"],
    )
    return obs_ok({
        "skill_id": skill_id,
        "resource_id": resource_id,
        "stage": stage,
        "media_type": resource.media_type,
        "content": content,
        "content_hash": pack.content_hash,
        "resource_hash": pack.resource_hashes.get(resource.id),
    })
