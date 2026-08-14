"""Tool-handler contract — every tool returns a ToolResultRecord.

Shape lives in schema.py; this module exposes the type + ergonomic
constructors so tool implementations stay short.

v2 (training-data capture, 2026-04-22):
- Returns ToolResultRecord (replaces ToolObservation).
- `obs_ok(payload=...)` — payload is the lean state the policy needs to
  act on next turn (IDs / sha256 / minimal data). NO descriptive summary,
  NO next_actions hints, NO file paths. Hints would leak into the policy
  and cause shortcut learning at deploy time. The workflow contract in
  prompts/designer.md is the single source of "what to do next."
- `obs_error(message, category=...)` — full error message preserved
  (no truncation) so the policy can learn recovery; typed category enum
  lets reward models distinguish model-side from environment-side errors.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol

from ..schema import DesignSpec, ErrorCategory, ToolResultRecord


class ToolHandler(Protocol):
    """Signature every registered tool implements."""
    def __call__(self, args: dict[str, Any], *, ctx: "ToolContext") -> ToolResultRecord: ...


class ToolContext:
    """Mutable per-run context handed to every tool call.

    Holds paths, the Settings object, and a dict where tools can stash
    artifacts the runner needs to read after the loop ends (background path,
    layer manifests, the latest DesignSpec, etc.).
    """

    def __init__(
        self,
        *,
        settings,
        run_dir,
        layers_dir,
        run_id,
        cancellation_token=None,
    ):
        if cancellation_token is None:
            from ..run_control import CancellationToken

            cancellation_token = CancellationToken.never(str(run_id))
        self.settings = settings
        self.run_dir = run_dir
        self.layers_dir = layers_dir
        self.run_id = run_id
        from ..attempt_candidates import promotion_run_identity

        run_path = Path(run_dir)
        try:
            self.run_directory_identity = promotion_run_identity(run_path)
        except FileNotFoundError:
            self.run_directory_identity = None
        self.cancellation_token = cancellation_token
        self.state: dict[str, Any] = {
            "artifact_type": "poster",      # set by switch_artifact_type; default=poster
            "design_spec": None,            # populated by propose_design_spec
            "rendered_layers": {},          # layer_id -> {png_path, name, kind, bbox, ...}
            "composition": None,            # CompositionArtifacts after composite (runtime only)
            "critique_results": [],         # list[CritiqueResult]
            "finalized": False,
            # v2.2 versioning — every overwrite-prone write versions itself
            # so revise loops + edit_layer don't lose intermediate state.
            # `layer_versions[layer_id]` is the highest version number written
            # so far (next write becomes N+1). `composite_iter` is the count
            # of completed composite() calls (next call becomes N+1).
            "layer_versions": {},           # dict[str, int]: layer_id -> highest version
            "composite_iter": 0,            # int: count of composite calls completed
        }

    def next_layer_version(self, layer_id: str) -> int:
        """Bump and return the next version number for `layer_id`.
        Call BEFORE writing the file."""
        self.raise_if_cancelled("tool.state.next_layer_version")
        versions = self.state.setdefault("layer_versions", {})
        v = int(versions.get(layer_id, 0)) + 1
        versions[layer_id] = v
        return v

    def next_composite_iter(self) -> int:
        """Bump and return the next composite iteration number.
        Call at the START of each composite() invocation."""
        self.raise_if_cancelled("tool.state.next_composite_iter")
        v = int(self.state.get("composite_iter", 0)) + 1
        self.state["composite_iter"] = v
        return v

    def is_cancelled(self) -> bool:
        return bool(self.cancellation_token.is_cancelled())

    def raise_if_cancelled(self, phase: str) -> None:
        self.cancellation_token.raise_if_cancelled(phase)

    def rehydrate_design_spec_state(self):
        """Restore active revision state from the validated canonical snapshot."""

        from ..design_spec_persistence import (
            DesignSpecPersistenceError,
            load_design_spec_canonical,
        )

        canonical_path = Path(self.run_dir) / "design_spec.json"
        restored = load_design_spec_canonical(canonical_path)
        if restored is None:
            return None
        try:
            spec = DesignSpec.model_validate(restored.design_spec)
            if spec.artifact_type.value != restored.artifact_type:
                raise ValueError(
                    "DesignSpec envelope artifact_type does not match design_spec"
                )
        except Exception as exc:
            raise DesignSpecPersistenceError(
                "canonical_integrity",
                canonical_path,
                exc,
            ) from exc
        self.state["artifact_type"] = restored.artifact_type
        self.state["design_spec"] = spec
        self.state["design_spec_sha256"] = restored.design_spec_sha256
        self.state["spec_revision_count"] = restored.revision
        return restored


def obs_ok(payload: dict[str, Any] | None = None) -> ToolResultRecord:
    """Success path. payload should be the *minimum* state the policy needs:
    IDs the next tool_call must reference, sha256 of artifacts so the policy
    can verify its action produced a unique output, and for critique the full
    CritiqueResult dump."""
    return ToolResultRecord(status="ok", payload=payload or {})


def obs_error(
    message: str,
    category: ErrorCategory = "unknown",
    payload: dict[str, Any] | None = None,
) -> ToolResultRecord:
    """Error path. message is preserved in full (NO truncation) so the policy
    can learn recovery from concrete error text. category is a typed enum so
    reward models can tell environment errors (api/safety_filter) apart from
    model errors (validation). payload may carry minimal diagnostic state."""
    return ToolResultRecord(
        status="error",
        error_message=message,
        error_category=category,
        payload=payload or {},
    )
