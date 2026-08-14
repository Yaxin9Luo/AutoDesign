"""finalize — flips the runner's exit flag.

This tool's job is only to signal "planner is done" so the runner can
stop the tool-use loop and return a RunResult summary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ._contract import ToolContext, obs_error, obs_ok
from .paper_poster_renderer import (
    AUTHORED_PAPER_POSTER_REQUIRED_STATE_KEY,
    is_academic_paper_poster_context,
)
from ..config import effective_poster_harness_mode
from ..attempt_candidates import (
    SecureRunMemberAccessor,
    SecureRunMemberSnapshot,
    secure_run_member_access,
)
from ..schema import (
    ToolResultRecord,
    VIDEO_MAX_DURATION_S,
    VIDEO_MIN_DURATION_S,
    VideoMediaProbe,
)
from ..util.design_feedback import (
    blocking_design_findings,
    build_design_feedback,
    design_feedback_to_dict,
)
from ..util.io import sha256_file
from ..util.design_spec_fingerprint import design_spec_sha256
from ..util.visual_reference_contract import build_visual_reference_contract


_VIDEO_DURATION_TOLERANCE_S = 0.5
_TIMING_ARITHMETIC_TOLERANCE_S = 0.01


def finalize(args: dict[str, Any], *, ctx: ToolContext) -> ToolResultRecord:
    if not _video_delivery_required(ctx):
        return _finalize_after_video_validation(args, ctx=ctx)
    try:
        with secure_run_member_access(ctx.run_dir) as accessor:
            video_blocker, validated_delivery = _video_delivery_final_blocker(
                ctx,
                accessor=accessor,
            )
            if video_blocker is not None:
                return obs_error(
                    video_blocker["message"],
                    category="validation",
                    payload={"video_delivery_final_blocker": video_blocker},
                )
            assert validated_delivery is not None
            result = _finalize_after_video_validation(
                args,
                ctx=ctx,
                accessor=accessor,
                validated_delivery=validated_delivery,
                mark_finalized=False,
            )
        if accessor.cleanup_warnings:
            cleanup_warnings = list(accessor.cleanup_warnings)
            ctx.state.setdefault("finalize_warnings", []).extend(cleanup_warnings)
            result.payload.setdefault("finalize_warnings", []).extend(
                cleanup_warnings
            )
        if result.status == "ok":
            ctx.state["finalized"] = True
            ctx.state["finalize_notes"] = args.get("notes", "")
        return result
    except (OSError, RuntimeError, ValueError) as exc:
        video_blocker = _video_delivery_issue(
            reason=f"current_video_delivery_invalid: {exc}"
        )
        return obs_error(
            video_blocker["message"],
            category="validation",
            payload={"video_delivery_final_blocker": video_blocker},
        )


def _finalize_after_video_validation(
    args: dict[str, Any],
    *,
    ctx: ToolContext,
    accessor: SecureRunMemberAccessor | None = None,
    validated_delivery: "_ValidatedVideoDelivery | None" = None,
    mark_finalized: bool = True,
) -> ToolResultRecord:
    notes = args.get("notes", "")
    composite_payload = ctx.state.get("last_composite_payload") or {}

    feedback = None
    if composite_payload:
        composite_payload.update(build_visual_reference_contract(composite_payload, ctx=ctx))
        if _payload_has_feedback_sources(composite_payload) or bool(composite_payload.get("visual_reference_attempted")):
            feedback = build_design_feedback(
                composite_payload,
                artifact_type=str(composite_payload.get("artifact_type") or ctx.state.get("artifact_type") or "unknown"),
                iteration=composite_payload.get("iteration"),
            )
        else:
            feedback = ctx.state.get("last_design_feedback") or composite_payload.get("design_feedback")
        ctx.state["last_design_feedback"] = feedback
        ctx.state["last_composite_payload"] = composite_payload
    else:
        feedback = ctx.state.get("last_design_feedback")

    authored_blocker = _authored_paper_poster_final_blocker(ctx, composite_payload)
    if authored_blocker is not None:
        return obs_error(
            authored_blocker["message"],
            category="validation",
            payload={"paper_poster_final_blocker": authored_blocker},
        )

    blocking_findings = blocking_design_findings(feedback)
    if blocking_findings:
        return obs_error(
            "cannot finalize: composite has blocking design_feedback findings; "
            "revise the artifact and call composite again",
            category="validation",
            payload={
                "design_feedback": design_feedback_to_dict(feedback),
                "blocking_findings": blocking_findings,
            },
        )

    critic_blocker = _dogfood_critic_final_blocker(ctx, composite_payload)
    if critic_blocker is not None:
        return obs_error(
            critic_blocker["message"],
            category="validation",
            payload={"dogfood_critic_final_blocker": critic_blocker},
        )

    visual_reference_blocker = _visual_reference_revision_blocker(ctx)
    if visual_reference_blocker is not None:
        return obs_error(
            visual_reference_blocker["message"],
            category="validation",
            payload={"visual_reference_revision": visual_reference_blocker},
        )

    # Back-compat fallback for old composite payloads that predate
    # ``design_feedback`` and are passed directly into finalize tests/tools.
    high_layout_issues = [
        issue for issue in composite_payload.get("layout_grounding_issues", [])
        if issue.get("severity") in {"high", "blocker"}
    ]
    if high_layout_issues:
        return obs_error(
            "cannot finalize: composite has high-severity layout_grounding_issues; "
            "revise layout and call composite again",
            category="validation",
            payload={"layout_grounding_issues": high_layout_issues},
        )
    if validated_delivery is not None:
        assert accessor is not None
        _write_video_delivery_pointer(
            ctx,
            accessor=accessor,
            validated_delivery=validated_delivery,
        )
    if mark_finalized:
        ctx.state["finalized"] = True
        ctx.state["finalize_notes"] = notes
    return obs_ok({})


@dataclass(frozen=True)
class _ValidatedVideoDelivery:
    pointer_payload: Mapping[str, object]
    snapshots: tuple[SecureRunMemberSnapshot, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "pointer_payload",
            MappingProxyType(dict(self.pointer_payload)),
        )


def _write_video_delivery_pointer(
    ctx: ToolContext,
    *,
    accessor: SecureRunMemberAccessor,
    validated_delivery: _ValidatedVideoDelivery,
) -> None:
    del ctx
    snapshots = validated_delivery.snapshots
    accessor.stage_video_delivery_pointer(
        Path("final/video_delivery.json"),
        dict(validated_delivery.pointer_payload),
        label="final Video delivery pointer",
        precondition=lambda: accessor.assert_snapshots_unchanged(snapshots),
    )


def _video_delivery_required(ctx: ToolContext) -> bool:
    spec = ctx.state.get("design_spec")
    artifact_type = getattr(spec, "artifact_type", None)
    artifact_type = getattr(artifact_type, "value", artifact_type)
    return str(artifact_type or ctx.state.get("artifact_type") or "") == "video"


def _video_delivery_issue(*, reason: str | None = None) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "expected_status": "passed",
        "repair_route": "export_video",
        "message": (
            "cannot finalize: current passed video delivery manifest, probe, and exact "
            "MP4 are required; call export_video for this attempt"
        ),
    }
    if reason is not None:
        issue["reason"] = reason
    return issue


def _video_delivery_final_blocker(
    ctx: ToolContext,
    *,
    accessor: SecureRunMemberAccessor,
) -> tuple[dict[str, Any] | None, _ValidatedVideoDelivery | None]:
    spec = ctx.state.get("design_spec")
    if not _video_delivery_required(ctx):
        return None, None

    issue = _video_delivery_issue()
    delivery = ctx.state.get("video_delivery")
    if not isinstance(delivery, dict) or delivery.get("status") != "passed":
        issue["reason"] = "video_delivery_state_missing_or_failed"
        return issue, None

    try:
        delivery_snapshots: dict[Path, SecureRunMemberSnapshot] = {}

        def _remember_snapshot(snapshot: SecureRunMemberSnapshot) -> None:
            existing = delivery_snapshots.get(snapshot.relative_path)
            if existing is not None and existing != snapshot:
                raise ValueError(
                    f"{snapshot.relative_path.as_posix()} has inconsistent "
                    "validated snapshots"
                )
            delivery_snapshots.setdefault(snapshot.relative_path, snapshot)

        try:
            current_pointer = accessor.read_bytes(
                Path("final/video_delivery.json"),
                label="current Video delivery pointer",
            )
        except FileNotFoundError:
            current_pointer = None
        if current_pointer is not None:
            if current_pointer.data is None:
                raise ValueError("current Video delivery pointer was not retained")
            try:
                current_pointer_payload = json.loads(current_pointer.data)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "current Video delivery pointer is malformed"
                ) from exc
            if (
                isinstance(current_pointer_payload, dict)
                and current_pointer_payload.get("status") == "invalidated"
                and current_pointer_payload.get("prior_manifest_sha256")
                == delivery.get("delivery_manifest_sha256")
            ):
                raise ValueError(
                    "current Video delivery was durably invalidated"
                )

        project_dir = accessor.validate_directory(
            delivery["project_dir"],
            label="project directory",
        )
        manifest_snapshot = accessor.read_bytes(
            delivery["manifest_path"],
            label="delivery manifest",
        )
        _remember_snapshot(manifest_snapshot)
        if (
            manifest_snapshot.relative_path
            != project_dir / "delivery_manifest.json"
        ):
            raise ValueError("delivery state does not reference the project manifest")

        def _json_payload(
            snapshot: SecureRunMemberSnapshot,
            *,
            label: str,
        ) -> Any:
            if snapshot.data is None:
                raise ValueError(f"{label} was not retained")
            try:
                return json.loads(snapshot.data)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"{label} is not valid JSON") from exc

        manifest = _json_payload(manifest_snapshot, label="delivery manifest")
        if not isinstance(manifest, dict) or manifest.get("status") != "passed":
            raise ValueError("delivery manifest status is not passed")
        if manifest.get("render_started_at") != delivery.get("render_started_at"):
            raise ValueError("delivery attempt timestamp does not match current state")
        current_spec_hash = design_spec_sha256(spec)
        if manifest.get("design_spec_sha256") != current_spec_hash:
            raise ValueError("delivery manifest does not match the current DesignSpec")
        if delivery.get("design_spec_sha256") != current_spec_hash:
            raise ValueError("delivery state does not match the current DesignSpec")
        current_revision = int(ctx.state.get("spec_revision_count") or 0)
        if int(manifest.get("design_spec_revision") or 0) != current_revision:
            raise ValueError("delivery manifest does not match the current DesignSpec revision")
        if int(delivery.get("design_spec_revision") or 0) != current_revision:
            raise ValueError("delivery state does not match the current DesignSpec revision")
        if (
            delivery.get("delivery_manifest_sha256")
            != manifest_snapshot.sha256
        ):
            raise ValueError("delivery manifest does not match the current delivery state")

        design_spec_snapshot = accessor.read_bytes(
            Path("design_spec.json"),
            label="persisted DesignSpec",
        )
        _remember_snapshot(design_spec_snapshot)
        persisted_spec = _json_payload(
            design_spec_snapshot,
            label="persisted DesignSpec",
        )
        if not isinstance(persisted_spec, dict):
            raise ValueError("persisted DesignSpec must be an object")
        persisted_spec_payload = persisted_spec.get("design_spec")
        persisted_spec_hash = persisted_spec.get("design_spec_sha256")
        persisted_revision = persisted_spec.get("revision")
        if (
            not isinstance(persisted_spec_payload, dict)
            or not isinstance(persisted_spec_hash, str)
            or design_spec_sha256(persisted_spec_payload) != persisted_spec_hash
        ):
            raise ValueError("persisted DesignSpec fingerprint is invalid")
        if persisted_spec_hash != current_spec_hash or (
            persisted_spec_hash != manifest.get("design_spec_sha256")
        ):
            raise ValueError(
                "persisted DesignSpec fingerprint does not match the delivery manifest"
            )
        if (
            type(persisted_revision) is not int
            or persisted_revision != current_revision
            or persisted_revision != manifest.get("design_spec_revision")
        ):
            raise ValueError(
                "persisted DesignSpec revision does not match the delivery manifest"
            )

        def _project_relative_path(value: Any, *, label: str) -> Path:
            return accessor.member_relative_path(
                str(value),
                label=label,
                base=project_dir,
            )

        def _project_file(key: str) -> Path:
            return _project_relative_path(manifest[key], label=key)

        source_html_path = _project_file("source_html_path")
        contract_path = _project_file("contract_path")
        probe_path = _project_file("media_probe_path")
        mp4_path = _project_file("mp4_path")
        state_probe_path = accessor.member_relative_path(
            delivery["media_probe_path"],
            label="delivery state probe",
        )
        state_mp4_path = accessor.member_relative_path(
            delivery["mp4_path"],
            label="delivery state MP4",
        )
        if probe_path != state_probe_path:
            raise ValueError("delivery state probe path does not match manifest")
        if mp4_path != state_mp4_path:
            raise ValueError("delivery state MP4 path does not match manifest")

        source_html_snapshot = accessor.digest(
            source_html_path,
            label="source_html_path",
        )
        contract_snapshot = accessor.read_bytes(
            contract_path,
            label="contract_path",
        )
        probe_snapshot = accessor.read_bytes(
            probe_path,
            label="media_probe_path",
        )
        mp4_snapshot = accessor.digest(mp4_path, label="mp4_path")
        narration_audio_snapshot = accessor.digest(
            _project_file("narration_audio_path"),
            label="narration_audio_path",
        )
        transcript_snapshot = accessor.digest(
            _project_file("transcript_path"),
            label="transcript_path",
        )
        srt_snapshot = accessor.digest(
            _project_file("srt_path"),
            label="srt_path",
        )
        vtt_snapshot = accessor.digest(
            _project_file("vtt_path"),
            label="vtt_path",
        )
        voice_snapshot = accessor.digest(
            _project_file("voice_metadata_path"),
            label="voice_metadata_path",
        )
        timing_snapshot = accessor.read_bytes(
            _project_file("narration_timing_path"),
            label="narration_timing_path",
        )
        for snapshot in (
            source_html_snapshot,
            contract_snapshot,
            probe_snapshot,
            mp4_snapshot,
            narration_audio_snapshot,
            transcript_snapshot,
            srt_snapshot,
            vtt_snapshot,
            voice_snapshot,
            timing_snapshot,
        ):
            _remember_snapshot(snapshot)
        expected_hashes = (
            (source_html_snapshot, "source_html_sha256"),
            (contract_snapshot, "contract_sha256"),
            (probe_snapshot, "media_probe_sha256"),
            (mp4_snapshot, "mp4_sha256"),
            (narration_audio_snapshot, "narration_audio_sha256"),
            (transcript_snapshot, "transcript_sha256"),
            (srt_snapshot, "srt_sha256"),
            (vtt_snapshot, "vtt_sha256"),
            (voice_snapshot, "voice_metadata_sha256"),
            (timing_snapshot, "narration_timing_sha256"),
        )
        for snapshot, hash_key in expected_hashes:
            if snapshot.sha256 != manifest.get(hash_key):
                raise ValueError(
                    f"{snapshot.relative_path.name} does not match {hash_key}"
                )
        if mp4_snapshot.size <= 0:
            raise ValueError("manifest MP4 is missing or empty")

        contract_payload = _json_payload(
            contract_snapshot,
            label="video delivery contract",
        )
        if not isinstance(contract_payload, dict):
            raise ValueError("video delivery contract must be an object")
        narration_contract = contract_payload.get("narration_contract")
        if not isinstance(narration_contract, dict):
            raise ValueError("video delivery narration contract is missing")
        minimum_coverage = float(
            narration_contract.get("minimum_speech_coverage_ratio")
        )
        minimum_spoken_wpm = float(narration_contract.get("minimum_spoken_wpm"))
        if minimum_coverage != 0.72 or minimum_spoken_wpm != 90:
            raise ValueError("video delivery narration contract thresholds are invalid")
        contract_scenes = contract_payload.get("scenes")
        if not isinstance(contract_scenes, list) or not contract_scenes:
            raise ValueError("video delivery authored scene timeline is missing")
        contract_scene_ids: list[str] = []
        authored_timeline_duration = 0.0
        for scene in contract_scenes:
            if not isinstance(scene, dict):
                raise ValueError("video delivery authored scene must be an object")
            scene_id = str(scene.get("scene_id") or "").strip()
            try:
                scene_start = float(scene["start_s"])
                scene_duration = float(scene["duration_s"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("video delivery authored scene timing is invalid") from exc
            if (
                not scene_id
                or scene_id in contract_scene_ids
                or not math.isfinite(scene_start)
                or not math.isfinite(scene_duration)
                or scene_start < 0
                or scene_duration <= 0
            ):
                raise ValueError("video delivery authored scene timeline is invalid")
            contract_scene_ids.append(scene_id)
            authored_timeline_duration = max(
                authored_timeline_duration,
                scene_start + scene_duration,
            )
        probe_payload = _json_payload(
            probe_snapshot,
            label="video delivery media probe",
        )
        probe = VideoMediaProbe.model_validate(probe_payload)
        if probe.subtitle_codec != "mov_text" or probe.subtitle_forced:
            raise ValueError(
                "video delivery MP4 is missing a selectable non-forced mov_text "
                "subtitle track"
            )
        observed_duration = float(probe.duration_s)
        if (
            abs(observed_duration - authored_timeline_duration)
            > _VIDEO_DURATION_TOLERANCE_S
        ):
            raise ValueError(
                "video delivery media duration does not match the authored timeline"
            )
        timing_payload = _json_payload(
            timing_snapshot,
            label="video delivery measured speech timing",
        )
        if not isinstance(timing_payload, list) or not timing_payload:
            raise ValueError("video delivery measured speech timing is missing")
        measured_scene_ids: list[str] = []
        measured_speech_duration = 0.0
        for item in timing_payload:
            if not isinstance(item, dict):
                raise ValueError("video delivery timing record must be an object")
            scene_id = str(item.get("scene_id") or "").strip()
            if not scene_id or scene_id in measured_scene_ids:
                raise ValueError("video delivery timing scene ids must be unique")
            try:
                start_s = float(item["start_s"])
                end_s = float(item["end_s"])
                speech_duration = float(item["speech_duration_s"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("video delivery timing boundaries are invalid") from exc
            if (
                not math.isfinite(start_s)
                or not math.isfinite(end_s)
                or not math.isfinite(speech_duration)
                or start_s < 0
                or speech_duration <= 0
                or end_s <= start_s
                or not math.isclose(
                    start_s + speech_duration,
                    end_s,
                    rel_tol=0.0,
                    abs_tol=_TIMING_ARITHMETIC_TOLERANCE_S,
                )
            ):
                raise ValueError("video delivery timing boundaries are inconsistent")
            measured_scene_ids.append(scene_id)
            measured_speech_duration += speech_duration
        if measured_scene_ids != contract_scene_ids:
            raise ValueError(
                "video delivery timing scene ids and order do not match the contract"
            )
        if int(manifest.get("measured_speech_scene_count") or 0) != len(
            measured_scene_ids
        ):
            raise ValueError("video delivery measured scene count does not match timing")
        target_duration = float(contract_payload.get("target_duration_s") or 0)
        if (
            not math.isfinite(target_duration)
            or target_duration < VIDEO_MIN_DURATION_S
            or target_duration > VIDEO_MAX_DURATION_S
        ):
            raise ValueError("video delivery target duration is invalid")
        if (
            abs(authored_timeline_duration - target_duration)
            > _VIDEO_DURATION_TOLERANCE_S
            or abs(observed_duration - target_duration)
            > _VIDEO_DURATION_TOLERANCE_S
        ):
            raise ValueError(
                "video delivery duration does not match the selected target"
            )
        measured_coverage = measured_speech_duration / observed_duration
        manifest_coverage = float(manifest.get("speech_coverage_ratio"))
        manifest_speech_duration = float(manifest.get("speech_duration_s"))
        manifest_coverage_duration = float(manifest.get("coverage_duration_s"))
        if (
            not math.isfinite(manifest_coverage)
            or not math.isfinite(manifest_speech_duration)
            or not math.isfinite(manifest_coverage_duration)
            or abs(manifest_coverage - measured_coverage) > 1e-6
            or abs(manifest_speech_duration - measured_speech_duration) > 1e-3
            or abs(manifest_coverage_duration - observed_duration) > 1e-3
        ):
            raise ValueError("video delivery speech coverage metrics do not match timing")
        if measured_coverage < minimum_coverage:
            raise ValueError("video delivery speech coverage is below the formal minimum")
        if float(manifest.get("spoken_wpm")) < minimum_spoken_wpm:
            raise ValueError("video delivery spoken WPM is below the formal minimum")
        local_asset_hashes = manifest.get("local_asset_sha256")
        if not isinstance(local_asset_hashes, dict):
            raise ValueError("local_asset_sha256 is missing")
        for rel_path, expected_hash in local_asset_hashes.items():
            asset_path = _project_relative_path(
                rel_path,
                label=f"local asset {rel_path}",
            )
            asset_snapshot = accessor.digest(
                asset_path,
                label=f"local asset {rel_path}",
            )
            if asset_snapshot.sha256 != expected_hash:
                raise ValueError(f"local asset hash mismatch: {rel_path}")
            _remember_snapshot(asset_snapshot)

        if probe_payload != manifest.get("media_probe"):
            raise ValueError("media probe file does not match delivery manifest")
    except Exception as exc:
        issue["reason"] = f"current_video_delivery_invalid: {exc}"
        return issue, None
    return None, _ValidatedVideoDelivery(
        pointer_payload={
            "manifest_path": manifest_snapshot.relative_path.as_posix(),
            "manifest_sha256": manifest_snapshot.sha256,
            "design_spec_sha256": persisted_spec_hash,
            "design_spec_revision": persisted_revision,
        },
        snapshots=tuple(delivery_snapshots.values()),
    )


def _authored_paper_poster_final_blocker(
    ctx: ToolContext,
    composite_payload: dict[str, Any],
) -> dict[str, Any] | None:
    spec = ctx.state.get("design_spec")
    required = bool(ctx.state.get(AUTHORED_PAPER_POSTER_REQUIRED_STATE_KEY)) or is_academic_paper_poster_context(
        spec,
        ctx,
    )
    if not required:
        return None

    issue: dict[str, Any] = {
        "expected_render_mode": "authored_html",
        "repair_route": "revise_authored_html",
    }
    render_mode = str((composite_payload or {}).get("render_mode") or "")
    if render_mode != "authored_html":
        issue.update({
            "id": "candidate_final_not_authored_html",
            "message": (
                "cannot finalize: academic paper poster final was not rendered "
                "through the authored_html route"
            ),
            "actual_render_mode": render_mode or None,
        })
        return issue

    final_dir = ctx.run_dir / "final"
    html_path = final_dir / "poster.html"
    manifest_path = final_dir / "paper_poster_render_manifest.json"
    dom_audit_path = final_dir / "paper_poster_dom_audit.json"
    preview_path = final_dir / "preview.png"
    missing = [
        path.name
        for path in (html_path, preview_path, manifest_path, dom_audit_path)
        if not path.exists()
    ]
    if missing:
        issue.update({
            "id": "candidate_authored_artifact_mismatch",
            "message": "cannot finalize: authored paper poster final is missing required artifacts",
            "missing_artifacts": missing,
        })
        return issue

    try:
        html_text = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        issue.update({
            "id": "candidate_authored_artifact_mismatch",
            "message": f"cannot finalize: final authored HTML could not be read: {e}",
            "final_html_path": str(html_path),
        })
        return issue
    if "paper-poster" not in html_text or 'data-render-mode="authored_html"' not in html_text:
        issue.update({
            "id": "candidate_final_not_authored_html",
            "message": "cannot finalize: final/poster.html is not authored paper-poster HTML",
            "final_html_path": str(html_path),
        })
        return issue

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        issue.update({
            "id": "candidate_authored_artifact_mismatch",
            "message": f"cannot finalize: authored render manifest is unreadable: {e}",
            "manifest_path": str(manifest_path),
        })
        return issue
    try:
        dom_audit = json.loads(dom_audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        issue.update({
            "id": "candidate_authored_artifact_mismatch",
            "message": f"cannot finalize: authored DOM audit is unreadable: {e}",
            "dom_audit_path": str(dom_audit_path),
        })
        return issue

    html_sha = sha256_file(html_path)
    preview_sha = sha256_file(preview_path)
    mismatches: list[str] = []
    if manifest.get("html_sha256") != html_sha:
        mismatches.append("manifest.html_sha256")
    if manifest.get("preview_sha256") != preview_sha:
        mismatches.append("manifest.preview_sha256")
    if manifest.get("dom_audit_html_sha256") not in {None, html_sha}:
        mismatches.append("manifest.dom_audit_html_sha256")
    if dom_audit.get("dom_audit_html_sha256") != html_sha:
        mismatches.append("dom_audit.dom_audit_html_sha256")
    if mismatches:
        issue.update({
            "id": "candidate_authored_artifact_mismatch",
            "message": "cannot finalize: authored manifest/DOM audit do not match final artifacts",
            "mismatched_fields": mismatches,
            "final_html_path": str(html_path),
        })
        return issue
    return None


def _payload_has_feedback_sources(payload: dict[str, Any]) -> bool:
    source_keys = (
        "layout_grounding_issues",
        "quality_lint_findings",
        "paper_density_findings",
        "paper_information_findings",
        "deck_layout_findings",
        "html_artifact_contract_findings",
        "paper_poster_dom_findings",
        "poster_gate_findings",
        "poster_contract_findings",
        "visual_reference_findings",
        "text_overlap_warnings",
        "xref_misses",
        "orphan_callout_warnings",
        "sanitizer_warnings",
        "alignment_warnings",
        "closing_warnings",
    )
    return any(payload.get(key) not in (None, [], {}) for key in source_keys)


def _dogfood_critic_final_blocker(
    ctx: ToolContext,
    composite_payload: dict[str, Any],
) -> dict[str, Any] | None:
    if effective_poster_harness_mode(ctx.settings) != "dogfood":
        return None
    spec = ctx.state.get("design_spec")
    required = bool(ctx.state.get(AUTHORED_PAPER_POSTER_REQUIRED_STATE_KEY)) or is_academic_paper_poster_context(
        spec,
        ctx,
    )
    if not required:
        return None
    issue: dict[str, Any] = {
        "id": "dogfood_missing_latest_critic",
        "repair_route": "call_critique",
        "message": (
            "cannot finalize: POSTER_HARNESS_MODE=dogfood requires at least "
            "one critic pass against the latest academic paper poster composite"
        ),
    }
    crits = ctx.state.get("critique_results") or []
    if not crits:
        return issue
    current_sha = (composite_payload or {}).get("preview_sha256")
    crit_sha = ctx.state.get("last_critique_preview_sha256")
    if current_sha and crit_sha and current_sha != crit_sha:
        issue.update({
            "id": "dogfood_stale_critic",
            "message": (
                "cannot finalize: POSTER_HARNESS_MODE=dogfood critic does not "
                "match the latest composite; call critique again after the repair"
            ),
            "latest_preview_sha256": current_sha,
            "critic_preview_sha256": crit_sha,
            "latest_composite_iteration": (composite_payload or {}).get("iteration"),
            "critic_composite_iteration": ctx.state.get("last_critique_composite_iteration"),
        })
        return issue
    return None


def _visual_reference_revision_blocker(ctx: ToolContext) -> dict[str, Any] | None:
    visual_reference = ctx.state.get("visual_reference")
    if not isinstance(visual_reference, dict):
        return None
    paths = visual_reference.get("visual_reference_paths")
    if not paths:
        return None

    if ctx.state.get("visual_reference_revision_required"):
        return {
            "message": (
                "cannot finalize: visual references were generated successfully; "
                "revise the editable DesignSpec using style_anchor/layout guidance, "
                "then call composite again"
            ),
            "iteration": ctx.state.get("visual_reference_revision_iteration"),
            "source_spec_revision": ctx.state.get("visual_reference_revision_source_spec_revision"),
            "style_anchor": visual_reference.get("style_anchor") or {},
        }

    if ctx.state.get("visual_reference_revision_spec_revision") is not None and not ctx.state.get(
        "visual_reference_revision_composited"
    ):
        return {
            "message": (
                "cannot finalize: visual-reference revision has not been composited; "
                "call composite so final artifacts use the revised editable spec"
            ),
            "iteration": ctx.state.get("visual_reference_revision_iteration"),
            "revision_spec_revision": ctx.state.get("visual_reference_revision_spec_revision"),
            "style_anchor": visual_reference.get("style_anchor") or {},
        }

    return None
