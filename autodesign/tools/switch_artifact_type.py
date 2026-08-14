"""switch_artifact_type — declare what kind of artifact we're producing.

Sets `ctx.state["artifact_type"]` to one of `poster | deck | landing`. This
drives renderer selection in `composite` and serves as the fallback default
for `propose_design_spec` when the spec JSON omits `artifact_type`.

The planner is expected to call this at the START of any new artifact
(per the workflow contract in prompts/designer.md). Idempotent: re-calling
with the same type is a no-op.

v2 training-data shape: success payload is the minimal `{"type": ..., "switched": bool}`
the policy needs to know its own state. No prose hints — `prompts/designer.md` is
the single source of "what to do next."
"""

from __future__ import annotations

from typing import Any

from ._contract import ToolContext, obs_error, obs_ok
from ..schema import ArtifactType, ToolResultRecord
from ..util.logging import log


VALID_TYPES = {t.value for t in ArtifactType}


def switch_artifact_type(args: dict[str, Any], *, ctx: ToolContext) -> ToolResultRecord:
    raw_type = args.get("type")
    if raw_type is None:
        return obs_error("missing required arg 'type'", category="validation")
    if raw_type not in VALID_TYPES:
        return obs_error(
            f"invalid artifact type {raw_type!r}; allowed: {sorted(VALID_TYPES)}",
            category="validation",
        )

    previous = ctx.state.get("artifact_type", "poster")
    ctx.state["artifact_type"] = raw_type
    is_switch = previous != raw_type

    log(
        "artifact.switch" if is_switch else "artifact.reaffirm",
        previous=previous,
        current=raw_type,
    )

    return obs_ok({"type": raw_type, "switched": is_switch})
