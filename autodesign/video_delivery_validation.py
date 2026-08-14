"""Secure current Video delivery validation shared by runtime consumers."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from .attempt_candidates import SecureRunMemberSnapshot, secure_run_member_access
from .schema import (
    VIDEO_MAX_DURATION_S,
    VIDEO_MEDIA_DURATION_TOLERANCE_S,
    VIDEO_MIN_DURATION_S,
    VideoMediaProbe,
)
from .util.design_spec_fingerprint import design_spec_sha256


VideoDeliveryReasonCode = Literal[
    "passed",
    "run_missing",
    "pointer_missing",
    "pointer_unsafe_link",
    "pointer_malformed",
    "pointer_invalidated",
    "stale_design_spec",
    "integrity_mismatch",
]


@dataclass(frozen=True)
class VideoDeliverySnapshot:
    relative_path: Path
    sha256: str
    size: int
    data: bytes | None
    device: int | None
    inode: int | None
    mode: int | None
    nlink: int | None
    mtime_ns: int | None

    @classmethod
    def from_secure_snapshot(
        cls,
        snapshot: SecureRunMemberSnapshot,
    ) -> "VideoDeliverySnapshot":
        return cls(
            relative_path=snapshot.relative_path,
            sha256=snapshot.sha256,
            size=snapshot.size,
            data=snapshot.data,
            device=snapshot.device,
            inode=snapshot.inode,
            mode=snapshot.mode,
            nlink=snapshot.nlink,
            mtime_ns=snapshot.mtime_ns,
        )


@dataclass(frozen=True)
class CurrentVideoDeliveryValidation:
    reason_code: VideoDeliveryReasonCode
    public_paths: dict[str, str]
    snapshots: dict[str, VideoDeliverySnapshot]
    manifest: dict[str, Any] | None = None
    run_dir: Path | None = None

    @property
    def is_passed(self) -> bool:
        return self.reason_code == "passed"

    def __bool__(self) -> bool:
        return self.is_passed

    def __getitem__(self, index: int) -> Any:
        """Temporary positive-result tuple compatibility for internal callers."""

        if not self.is_passed or self.run_dir is None or self.manifest is None:
            raise IndexError(index)
        mp4 = self.run_dir / self.public_paths["mp4"]
        project = self.run_dir / self.public_paths["manifest"]
        values = (mp4.resolve(), self.manifest, project.parent.resolve())
        return values[index]


def _invalid(reason_code: VideoDeliveryReasonCode) -> CurrentVideoDeliveryValidation:
    return CurrentVideoDeliveryValidation(
        reason_code=reason_code,
        public_paths={},
        snapshots={},
    )


def _json_object(snapshot: SecureRunMemberSnapshot, *, label: str) -> dict[str, Any]:
    if snapshot.data is None:
        raise ValueError(f"{label} bytes were not retained")
    try:
        payload = json.loads(snapshot.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is malformed") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain an object")
    return payload


def _pointer_read_failure(exc: BaseException) -> VideoDeliveryReasonCode:
    message = str(exc).lower()
    if any(
        marker in message
        for marker in ("link", "reparse", "non-regular", "hard-linked")
    ):
        return "pointer_unsafe_link"
    return "pointer_malformed"


def validate_current_video_delivery(
    run_dir: Path,
) -> CurrentVideoDeliveryValidation:
    """Validate one exact current-spec passed delivery through retained reads."""

    run_dir = Path(run_dir)
    try:
        with secure_run_member_access(run_dir) as accessor:
            try:
                pointer_snapshot = accessor.read_bytes(
                    Path("final/video_delivery.json"),
                    label="final Video delivery pointer",
                )
            except FileNotFoundError:
                return _invalid("pointer_missing")
            except (OSError, RuntimeError, ValueError) as exc:
                return _invalid(_pointer_read_failure(exc))
            try:
                pointer = _json_object(
                    pointer_snapshot,
                    label="final Video delivery pointer",
                )
            except ValueError:
                return _invalid("pointer_malformed")
            if pointer.get("status") == "invalidated":
                return _invalid("pointer_invalidated")

            manifest_value = pointer.get("manifest_path")
            if not isinstance(manifest_value, str) or not manifest_value:
                return _invalid("pointer_malformed")
            try:
                manifest_relative = accessor.member_relative_path(
                    manifest_value,
                    label="Video delivery manifest",
                )
            except (OSError, RuntimeError, ValueError):
                return _invalid("pointer_malformed")
            if manifest_relative.name != "delivery_manifest.json":
                return _invalid("pointer_malformed")
            try:
                manifest_snapshot = accessor.read_bytes(
                    manifest_relative,
                    label="Video delivery manifest",
                )
                manifest = _json_object(
                    manifest_snapshot,
                    label="Video delivery manifest",
                )
            except (OSError, RuntimeError, ValueError):
                return _invalid("integrity_mismatch")
            if pointer.get("manifest_sha256") != manifest_snapshot.sha256:
                return _invalid("integrity_mismatch")
            if manifest.get("status") != "passed":
                return _invalid("integrity_mismatch")

            try:
                spec_snapshot = accessor.read_bytes(
                    Path("design_spec.json"),
                    label="current Video DesignSpec",
                )
                persisted_spec_snapshot = _json_object(
                    spec_snapshot,
                    label="current Video DesignSpec",
                )
                persisted_spec = persisted_spec_snapshot.get("design_spec")
                if not isinstance(persisted_spec, dict):
                    raise ValueError("current Video DesignSpec payload is missing")
                current_spec_sha256 = design_spec_sha256(persisted_spec)
                current_revision = int(
                    persisted_spec_snapshot.get("revision") or 0
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                return _invalid("stale_design_spec")
            if (
                persisted_spec_snapshot.get("design_spec_sha256")
                != current_spec_sha256
                or pointer.get("design_spec_sha256") != current_spec_sha256
                or manifest.get("design_spec_sha256") != current_spec_sha256
                or type(pointer.get("design_spec_revision")) is not int
                or int(pointer["design_spec_revision"]) != current_revision
                or type(manifest.get("design_spec_revision")) is not int
                or int(manifest["design_spec_revision"]) != current_revision
            ):
                return _invalid("stale_design_spec")

            project_relative = manifest_relative.parent

            def project_member(key: str) -> Path:
                value = manifest.get(key)
                if not isinstance(value, str) or not value:
                    raise ValueError(f"{key} is missing")
                return accessor.member_relative_path(
                    value,
                    label=key,
                    base=project_relative,
                )

            try:
                file_contract = {
                    "source_html_sha256": (project_member("source_html_path"), False),
                    "contract_sha256": (project_member("contract_path"), True),
                    "media_probe_sha256": (project_member("media_probe_path"), True),
                    "mp4_sha256": (project_member("mp4_path"), False),
                    "narration_audio_sha256": (
                        project_member("narration_audio_path"),
                        False,
                    ),
                    "transcript_sha256": (project_member("transcript_path"), False),
                    "srt_sha256": (project_member("srt_path"), False),
                    "vtt_sha256": (project_member("vtt_path"), False),
                    "voice_metadata_sha256": (
                        project_member("voice_metadata_path"),
                        False,
                    ),
                    "narration_timing_sha256": (
                        project_member("narration_timing_path"),
                        False,
                    ),
                }
                retained_files: dict[str, SecureRunMemberSnapshot] = {}
                for hash_key, (relative, retain_bytes) in file_contract.items():
                    snapshot = (
                        accessor.read_bytes(relative, label=hash_key)
                        if retain_bytes
                        else accessor.digest(relative, label=hash_key)
                    )
                    if snapshot.sha256 != manifest.get(hash_key):
                        raise ValueError(f"{hash_key} does not match")
                    retained_files[hash_key] = snapshot
                mp4_snapshot = retained_files["mp4_sha256"]
                if mp4_snapshot.size <= 0:
                    raise ValueError("Video delivery MP4 is empty")

                local_assets = manifest.get("local_asset_sha256")
                if not isinstance(local_assets, dict):
                    raise ValueError("local asset hashes are missing")
                for relative_value, expected_sha256 in local_assets.items():
                    asset = accessor.member_relative_path(
                        str(relative_value),
                        label=f"local asset {relative_value}",
                        base=project_relative,
                    )
                    if accessor.digest(
                        asset,
                        label=f"local asset {relative_value}",
                    ).sha256 != expected_sha256:
                        raise ValueError("local asset integrity mismatch")

                probe_snapshot = retained_files["media_probe_sha256"]
                probe = _json_object(
                    probe_snapshot,
                    label="Video delivery media probe",
                )
                media_probe = VideoMediaProbe.model_validate(probe)
                if media_probe.subtitle_forced or (
                    media_probe.subtitle_codec is not None
                    and media_probe.subtitle_codec != "mov_text"
                ):
                    raise ValueError("Video delivery subtitle track is invalid")
                if probe != manifest.get("media_probe"):
                    raise ValueError("Video delivery media probe does not match")
                contract_snapshot = retained_files["contract_sha256"]
                contract = _json_object(
                    contract_snapshot,
                    label="Video delivery contract",
                )
                target_duration = float(contract.get("target_duration_s") or 0)
                if (
                    not math.isfinite(target_duration)
                    or target_duration < VIDEO_MIN_DURATION_S
                    or target_duration > VIDEO_MAX_DURATION_S
                    or abs(media_probe.duration_s - target_duration)
                    > VIDEO_MEDIA_DURATION_TOLERANCE_S
                ):
                    raise ValueError(
                        "Video delivery duration does not match the selected target"
                    )
                if not str(manifest.get("render_started_at") or "").strip():
                    raise ValueError("Video delivery render identity is missing")
            except (
                KeyError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                ValidationError,
            ):
                return _invalid("integrity_mismatch")

            vtt_relative = file_contract["vtt_sha256"][0]
            return CurrentVideoDeliveryValidation(
                reason_code="passed",
                public_paths={
                    "pointer": pointer_snapshot.relative_path.as_posix(),
                    "manifest": manifest_snapshot.relative_path.as_posix(),
                    "mp4": mp4_snapshot.relative_path.as_posix(),
                    "vtt": vtt_relative.as_posix(),
                },
                snapshots={
                    "pointer": VideoDeliverySnapshot.from_secure_snapshot(
                        pointer_snapshot
                    ),
                    "manifest": VideoDeliverySnapshot.from_secure_snapshot(
                        manifest_snapshot
                    ),
                    "mp4": VideoDeliverySnapshot.from_secure_snapshot(mp4_snapshot),
                },
                manifest=manifest,
                run_dir=run_dir,
            )
    except FileNotFoundError:
        return _invalid("run_missing")
    except (OSError, RuntimeError, ValueError):
        return _invalid("run_missing")


def _snapshot_matches(
    expected: VideoDeliverySnapshot,
    observed: SecureRunMemberSnapshot,
) -> bool:
    return (
        expected.relative_path == observed.relative_path
        and expected.sha256 == observed.sha256
        and expected.size == observed.size
        and expected.data == observed.data
        and expected.device == observed.device
        and expected.inode == observed.inode
        and expected.mode == observed.mode
        and expected.nlink == observed.nlink
        and expected.mtime_ns == observed.mtime_ns
    )


def revalidate_current_video_delivery_snapshots(
    run_dir: Path,
    validation: CurrentVideoDeliveryValidation,
) -> bool:
    """Re-confirm all advertised identities immediately before consumption."""

    if not validation.is_passed or set(validation.snapshots) != {
        "pointer",
        "manifest",
        "mp4",
    }:
        return False
    try:
        with secure_run_member_access(Path(run_dir)) as accessor:
            for role in ("pointer", "manifest", "mp4"):
                expected = validation.snapshots[role]
                observed = (
                    accessor.digest(expected.relative_path, label=f"validated {role}")
                    if role == "mp4"
                    else accessor.read_bytes(
                        expected.relative_path,
                        label=f"validated {role}",
                    )
                )
                if not _snapshot_matches(expected, observed):
                    return False
    except (OSError, RuntimeError, ValueError):
        return False
    return True
