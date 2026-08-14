"""ChatSession — outer container for multi-turn conversational design.

A `ChatSession` wraps N artifacts produced across the session's turns. Each
user brief (non-slash input) → full DesignerLoop → one artifact reference
appended to `artifacts`. Slash commands mutate session state without producing
an artifact.

Session state persists to `sessions/<session_id>.json`. Artifacts (PSD/SVG/
PNG) still live under `out/runs/<run_id>/`; the session file stores only
metadata + refs to those artifacts.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .schema import ArtifactType
from .util.io import atomic_write_json


SESSION_SCHEMA_VERSION = "v2.0-design-memory"


class ChatMessage(BaseModel):
    """One turn of the user↔assistant conversation."""
    role: Literal["user", "assistant", "system"]
    content: str
    timestamp: datetime = Field(default_factory=datetime.now)
    artifact_id: str | None = None


class ArtifactRef(BaseModel):
    """Lightweight pointer from session → product artifact on disk."""
    run_id: str
    artifact_type: ArtifactType
    created_at: datetime
    run_dir: str
    preview_path: str | None = None
    psd_path: str | None = None
    svg_path: str | None = None
    html_path: str | None = None
    pdf_path: str | None = None
    pptx_path: str | None = None
    n_layers: int
    verdict: Literal["pass", "revise", "fail"] | None = None
    score: float | None = None
    wall_s: float = 0.0


class ChatSession(BaseModel):
    """Persistent conversation state across N artifact generations."""
    session_id: str
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    current_artifact_type: ArtifactType = ArtifactType.POSTER
    message_history: list[ChatMessage] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    # v1.1 paper2any: files queued via `:attach <path>` that will be
    # consumed + cleared on the NEXT non-slash turn's brief.
    pending_attachments: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # ---- ergonomic helpers -------------------------------------------------

    def append_user(self, content: str) -> ChatMessage:
        msg = ChatMessage(role="user", content=content)
        self.message_history.append(msg)
        self.updated_at = datetime.now()
        return msg

    def append_assistant(self, content: str, artifact_id: str | None = None) -> ChatMessage:
        msg = ChatMessage(role="assistant", content=content, artifact_id=artifact_id)
        self.message_history.append(msg)
        self.updated_at = datetime.now()
        return msg

    def append_system(self, content: str) -> ChatMessage:
        msg = ChatMessage(role="system", content=content)
        self.message_history.append(msg)
        self.updated_at = datetime.now()
        return msg

    def latest_artifact(self) -> ArtifactRef | None:
        return self.artifacts[-1] if self.artifacts else None

    def total_wall_s(self) -> float:
        return round(sum(t.wall_s for t in self.artifacts), 2)


# ---- Persistence --------------------------------------------------------


def new_session_id() -> str:
    """Sortable session id: YYYYMMDD-HHMMSS-shortuuid."""
    import uuid
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"session_{ts}_{uuid.uuid4().hex[:8]}"


def session_path(sessions_dir: Path, session_id: str) -> Path:
    return sessions_dir / f"{session_id}.json"


def save_session(session: ChatSession, sessions_dir: Path) -> Path:
    path = session_path(sessions_dir, session.session_id)
    payload = session.model_dump(mode="json")
    payload["_schema_version"] = SESSION_SCHEMA_VERSION
    atomic_write_json(path, payload)
    return path


def load_session(sessions_dir: Path, session_id: str) -> ChatSession:
    path = session_path(sessions_dir, session_id)
    if not path.exists():
        raise FileNotFoundError(f"session not found: {path}")
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    payload.pop("_schema_version", None)
    _upgrade_legacy_payload(payload)
    try:
        return ChatSession.model_validate(payload)
    except ValidationError as e:
        raise RuntimeError(
            f"session {session_id} failed schema validation: "
            f"{e.errors(include_url=False)[:3]}"
        ) from e


def list_sessions(sessions_dir: Path, limit: int = 20) -> list[tuple[str, datetime, int]]:
    """Return (session_id, updated_at, n_artifacts) for recent sessions, newest first."""
    if not sessions_dir.exists():
        return []
    items: list[tuple[str, datetime, int]] = []
    for p in sessions_dir.glob("session_*.json"):
        try:
            with open(p, encoding="utf-8") as f:
                raw = json.load(f)
            items.append((
                raw["session_id"],
                datetime.fromisoformat(raw["updated_at"]),
                len(raw.get("artifacts", raw.get("trajectories", []))),
            ))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    items.sort(key=lambda x: x[1], reverse=True)
    return items[:limit]


def _upgrade_legacy_payload(payload: dict[str, Any]) -> None:
    if "artifacts" not in payload and "trajectories" in payload:
        artifacts: list[dict[str, Any]] = []
        for item in payload.get("trajectories") or []:
            if not isinstance(item, dict):
                continue
            run_id = item.get("run_id", "")
            run_dir = item.get("run_dir")
            if not run_dir and item.get("trajectory_path"):
                tp = Path(str(item["trajectory_path"]))
                run_dir = str(tp.parent.parent / "runs" / run_id)
            artifacts.append({
                **item,
                "run_dir": run_dir or "",
            })
        payload["artifacts"] = artifacts
        payload.pop("trajectories", None)
    for msg in payload.get("message_history") or []:
        if isinstance(msg, dict) and "artifact_id" not in msg and msg.get("trajectory_id"):
            msg["artifact_id"] = msg.pop("trajectory_id")
