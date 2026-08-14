"""Cancellable validation and delivery of an editable attempt draft."""

from __future__ import annotations

from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Any
from uuid import uuid4

from .attempt_candidates import load_attempt_candidate
from .attempt_fork import materialize_attempt_candidate_draft
from .agents.atomic_artifact_promotion import publish_artifact_directory
from .candidate_assessment import assess_delivery_issues
from .designer import invoke_designer_tool
from .schema import ToolResultRecord
from .run_control import RunCancelled, durable_replace_json
from .tools._contract import ToolContext
from .util.browser_render import screenshot_html
from .util.io import atomic_write_json, sha256_file
from .video_delivery_validation import validate_current_video_delivery


_PUBLISH_CONTEXT_FILES = (
    "paper_visual_provenance.json",
    "paper_memory.json",
    "paper_memory_dossier.json",
    "paper_visual_storyboard.json",
    "poster_content_brief.json",
    "poster_plan_contract.json",
    "poster_contract_preflight.json",
    "canvas_plan.json",
    "deck_plan.json",
    "paper_memory.md",
    "paper_memory_dossier.md",
    "slides_trusted_source_hashes.json",
    "landing_trusted_source_hashes.json",
    "video_trusted_source_context.json",
    "candidate_delivery_assessment.json",
)

_PUBLISH_CONTEXT_DIRS = ("layers", "paper_evidence_packs")

_VIDEO_CONTEXT_JOURNAL_NAME = ".video-candidate-delivery-promotion.json"
_VIDEO_CONTEXT_TRANSACTION_OWNER = "autodesign.video_candidate_delivery.v1"
_VIDEO_CONTEXT_JOURNAL_VERSION = 2
_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = frozenset(
    {
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            raise
    finally:
        os.close(descriptor)


def _fsync_regular_tree(root: Path) -> None:
    _assert_regular_tree(root, label="Video delivery context")
    directories = [root]
    for member in root.rglob("*"):
        if member.is_dir():
            directories.append(member)
        elif member.is_file():
            with member.open("rb") as handle:
                os.fsync(handle.fileno())
    for directory in reversed(directories):
        _fsync_directory(directory)


def _assert_regular_tree(root: Path, *, label: str) -> None:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"{label} must be a regular directory tree")
    for member in root.rglob("*"):
        if member.is_symlink() or not (member.is_dir() or member.is_file()):
            raise ValueError(f"{label} contains an unsafe member: {member.name}")


def _copy_tree_atomically(
    source: Path,
    destination: Path,
    *,
    staging: Path,
    label: str,
) -> None:
    _assert_regular_tree(source, label=label)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"{label} destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if staging.parent != destination.parent or staging.exists() or staging.is_symlink():
        raise ValueError(f"{label} staging path is invalid")
    try:
        shutil.copytree(source, staging)
        _assert_regular_tree(staging, label=f"copied {label}")
        _fsync_regular_tree(staging)
        os.replace(staging, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _copy_file_atomically(
    source: Path,
    destination: Path,
    *,
    staging: Path,
    label: str,
) -> None:
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"{label} must be a regular file")
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"{label} destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if staging.parent != destination.parent or staging.exists() or staging.is_symlink():
        raise ValueError(f"{label} staging path is invalid")
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(source.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise


def _video_context_journal_path(run_dir: Path) -> Path:
    return run_dir / _VIDEO_CONTEXT_JOURNAL_NAME


def _read_trusted_video_context_journal(
    journal_path: Path,
) -> tuple[dict[str, Any], tuple[int, int]]:
    try:
        initial = journal_path.lstat()
    except FileNotFoundError:
        raise ValueError("Video delivery context promotion journal is missing")
    if not stat.S_ISREG(initial.st_mode):
        raise ValueError("invalid Video delivery context promotion journal path")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(journal_path, flags)
    except OSError as exc:
        raise ValueError(
            "invalid Video delivery context promotion journal path"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or identity != (initial.st_dev, initial.st_ino)
            or opened.st_nlink != 1
        ):
            raise ValueError(
                "invalid Video delivery context promotion journal path"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "invalid Video delivery context promotion journal"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid Video delivery context promotion journal")
    return payload, identity


def _assert_video_context_journal_identity(
    journal_path: Path,
    identity: tuple[int, int],
) -> None:
    try:
        current = journal_path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(
            "Video delivery context promotion journal changed during recovery"
        ) from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
        or current.st_nlink != 1
    ):
        raise ValueError(
            "Video delivery context promotion journal changed during recovery"
        )


def _validated_video_context_entries(
    run_dir: Path,
    payload: dict[str, Any],
) -> list[tuple[Path, str, Path]]:
    if (
        payload.get("version") != _VIDEO_CONTEXT_JOURNAL_VERSION
        or payload.get("transaction_owner") != _VIDEO_CONTEXT_TRANSACTION_OWNER
        or payload.get("phase") not in {
            "prepared",
            "installed",
            "rollback_started",
            "rolled_back",
        }
        or payload.get("run_name") != run_dir.name
    ):
        raise ValueError("invalid Video delivery context promotion journal")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("invalid Video delivery context promotion entries")
    entries: list[tuple[Path, str, Path]] = []
    graph_count = 0
    names: set[str] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ValueError("invalid Video delivery context promotion entry")
        name = raw_entry.get("name")
        kind = raw_entry.get("kind")
        staging_name = raw_entry.get("staging_name")
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in names
            or kind not in {"file", "directory"}
            or not isinstance(staging_name, str)
            or Path(staging_name).name != staging_name
            or not staging_name.startswith(f".{name}-staging-")
            or staging_name == f".{name}-staging-"
            or staging_name in names
        ):
            raise ValueError("unsafe Video delivery context promotion entry")
        if name == "design_spec.json":
            if kind != "file":
                raise ValueError("Video DesignSpec promotion entry must be a file")
        elif name == "specs":
            if kind != "directory":
                raise ValueError("Video DesignSpec archive promotion entry must be a directory")
        elif name.startswith("hyperframes-"):
            if kind != "directory" or name.startswith("hyperframes-."):
                raise ValueError("Video delivery graph promotion entry is invalid")
            graph_count += 1
        else:
            raise ValueError("unexpected Video delivery context promotion entry")
        names.add(name)
        names.add(staging_name)
        entries.append((run_dir / name, kind, run_dir / staging_name))
    if graph_count != 1 or "design_spec.json" not in names:
        raise ValueError("Video delivery context promotion journal is incomplete")
    return entries


def validate_video_delivery_context_promotion(run_dir: Path) -> None:
    journal_path = _video_context_journal_path(run_dir)
    if not journal_path.exists() and not journal_path.is_symlink():
        entries: list[tuple[Path, str, Path]] = []
    else:
        payload, _identity = _read_trusted_video_context_journal(journal_path)
        if payload.get("phase") != "installed":
            raise ValueError("Video delivery context promotion is not installed")
        entries = _validated_video_context_entries(run_dir, payload)
        for path, kind, staging in entries:
            if path.is_symlink() or (kind == "file" and not path.is_file()) or (
                kind == "directory" and not path.is_dir()
            ):
                raise ValueError("Video delivery context promotion entry is missing")
            if staging.exists() or staging.is_symlink():
                raise ValueError("Video delivery context promotion staging remains")
    validation = validate_current_video_delivery(run_dir)
    if not validation.is_passed:
        raise ValueError(
            "published Video delivery failed current-context validation: "
            f"{validation.reason_code}"
        )


def reconcile_video_delivery_context_promotion(
    run_dir: Path,
    *,
    accept: bool,
) -> None:
    journal_path = _video_context_journal_path(run_dir)
    if not journal_path.exists() and not journal_path.is_symlink():
        if accept:
            validate_video_delivery_context_promotion(run_dir)
        return
    payload, identity = _read_trusted_video_context_journal(journal_path)
    entries = _validated_video_context_entries(run_dir, payload)
    if accept:
        if payload.get("phase") != "installed":
            raise ValueError("Video delivery context promotion is not installed")
        for path, kind, staging in entries:
            if path.is_symlink() or (kind == "file" and not path.is_file()) or (
                kind == "directory" and not path.is_dir()
            ):
                raise ValueError("Video delivery context promotion entry is missing")
            if staging.exists() or staging.is_symlink():
                raise ValueError("Video delivery context promotion staging remains")
        validation = validate_current_video_delivery(run_dir)
        if not validation.is_passed:
            raise ValueError(
                "published Video delivery failed current-context validation: "
                f"{validation.reason_code}"
            )
    else:
        phase = str(payload.get("phase") or "")
        cleanup_entries = [
            (candidate, kind)
            for path, kind, staging in entries
            for candidate in (staging, path)
        ]
        for path, kind in cleanup_entries:
            if path.is_symlink():
                raise ValueError("unsafe Video delivery context promotion entry")
            if path.exists() and (
                (kind == "file" and not path.is_file())
                or (kind == "directory" and not path.is_dir())
            ):
                raise ValueError("unsafe Video delivery context promotion entry")
        if phase == "rolled_back":
            if any(
                path.exists() or path.is_symlink()
                for path, _kind in cleanup_entries
            ):
                raise RuntimeError(
                    "Video delivery context rollback is incomplete; journal retained"
                )
        else:
            if phase != "rollback_started":
                _assert_video_context_journal_identity(journal_path, identity)
                payload["phase"] = "rollback_started"
                durable_replace_json(journal_path, payload)
                payload, identity = _read_trusted_video_context_journal(
                    journal_path
                )
                if payload.get("phase") != "rollback_started" or (
                    _validated_video_context_entries(run_dir, payload) != entries
                ):
                    raise ValueError(
                        "Video delivery context promotion journal changed during recovery"
                    )
            for path, kind in reversed(cleanup_entries):
                _assert_video_context_journal_identity(journal_path, identity)
                if not path.exists():
                    continue
                if kind == "directory":
                    shutil.rmtree(path)
                else:
                    path.unlink()
                if path.exists() or path.is_symlink():
                    raise RuntimeError(
                        "Video delivery context rollback could not remove "
                        f"{path.name}; journal retained"
                    )
                _fsync_directory(run_dir)
            _assert_video_context_journal_identity(journal_path, identity)
            payload["phase"] = "rolled_back"
            durable_replace_json(journal_path, payload)
            payload, identity = _read_trusted_video_context_journal(journal_path)
            if payload.get("phase") != "rolled_back" or (
                _validated_video_context_entries(run_dir, payload) != entries
            ):
                raise ValueError(
                    "Video delivery context promotion journal changed during recovery"
                )
    _assert_video_context_journal_identity(journal_path, identity)
    journal_path.unlink()
    _fsync_directory(run_dir)


def _publish_video_delivery_context(work_dir: Path, run_dir: Path) -> list[Path]:
    validation = validate_current_video_delivery(work_dir)
    if not validation.is_passed:
        raise ValueError(
            "staged Video delivery failed current-context validation: "
            f"{validation.reason_code}"
        )
    manifest_relative = Path(validation.public_paths["manifest"])
    if (
        manifest_relative.is_absolute()
        or not manifest_relative.parts
        or manifest_relative.parts[0] in {"final", "."}
        or manifest_relative.parts[0].startswith(".")
    ):
        raise ValueError("staged Video delivery graph path is invalid")
    graph_relative = Path(manifest_relative.parts[0])
    graph_source = work_dir / graph_relative
    graph_destination = run_dir / graph_relative
    design_spec_destination = run_dir / "design_spec.json"
    specs_source = work_dir / "specs"
    specs_destination = run_dir / "specs"
    planned = [
        (
            graph_destination,
            "directory",
            run_dir / f".{graph_destination.name}-staging-{uuid4().hex}",
        ),
        (
            design_spec_destination,
            "file",
            run_dir / f".{design_spec_destination.name}-staging-{uuid4().hex}",
        ),
    ]
    if specs_source.is_dir():
        planned.append((
            specs_destination,
            "directory",
            run_dir / f".{specs_destination.name}-staging-{uuid4().hex}",
        ))
    for destination, _kind, staging in planned:
        if (
            destination.exists()
            or destination.is_symlink()
            or staging.exists()
            or staging.is_symlink()
        ):
            raise ValueError(
                f"Video delivery context destination already exists: {destination.name}"
            )
    journal_path = _video_context_journal_path(run_dir)
    if journal_path.exists() or journal_path.is_symlink():
        raise ValueError("Video delivery context promotion is already pending")
    durable_replace_json(journal_path, {
        "version": _VIDEO_CONTEXT_JOURNAL_VERSION,
        "transaction_owner": _VIDEO_CONTEXT_TRANSACTION_OWNER,
        "phase": "prepared",
        "run_name": run_dir.name,
        "entries": [
            {
                "name": path.name,
                "kind": kind,
                "staging_name": staging.name,
            }
            for path, kind, staging in planned
        ],
    })
    created: list[Path] = []
    try:
        _copy_tree_atomically(
            graph_source,
            graph_destination,
            staging=planned[0][2],
            label="Video delivery graph",
        )
        created.append(graph_destination)
        _copy_file_atomically(
            work_dir / "design_spec.json",
            design_spec_destination,
            staging=planned[1][2],
            label="Video DesignSpec",
        )
        created.append(design_spec_destination)
        if specs_source.is_dir():
            _copy_tree_atomically(
                specs_source,
                specs_destination,
                staging=planned[2][2],
                label="Video DesignSpec archive",
            )
            created.append(specs_destination)
        durable_replace_json(journal_path, {
            "version": _VIDEO_CONTEXT_JOURNAL_VERSION,
            "transaction_owner": _VIDEO_CONTEXT_TRANSACTION_OWNER,
            "phase": "installed",
            "run_name": run_dir.name,
            "entries": [
                {
                    "name": path.name,
                    "kind": kind,
                    "staging_name": staging.name,
                }
                for path, kind, staging in planned
            ],
        })
    except BaseException as publish_error:
        try:
            reconcile_video_delivery_context_promotion(run_dir, accept=False)
        except BaseException as cleanup_error:
            raise cleanup_error from publish_error
        raise
    return created


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _record_delivery_assessment(
    run_dir: Path,
    artifact_type: str,
    issues: list[dict[str, Any]],
) -> list[dict[str, str]]:
    assessment = assess_delivery_issues(artifact_type, issues)
    atomic_write_json(
        run_dir / "candidate_delivery_assessment.json",
        {
            "schema_version": 1,
            "artifact_type": artifact_type,
            "quality_status": assessment.safety_state,
            "quality_diagnostics": [
                item.issue_id for item in assessment.quality_diagnostics
            ],
            "hard_blockers": [
                item.issue_id for item in assessment.hard_blockers
            ],
        },
    )
    return [
        {"issue_id": item.issue_id, "message": item.message}
        for item in assessment.hard_blockers
    ]


def _apply_final_manifest_quality(
    run_dir: Path,
    artifact_type: str,
    *,
    published_run_dir: Path | None = None,
) -> None:
    assessment = _read_json(run_dir / "candidate_delivery_assessment.json")
    quality_status = str(assessment.get("quality_status") or "ready")
    if quality_status not in {"ready", "ready_with_warnings"}:
        raise ValueError("candidate delivery assessment is not publishable")
    quality_diagnostics = assessment.get("quality_diagnostics")
    quality_diagnostics = (
        [str(item) for item in quality_diagnostics if str(item)]
        if isinstance(quality_diagnostics, list)
        else []
    )
    final_dir = run_dir / "final"
    manifest_name, html_name = {
        "poster": ("designer_author_direct_manifest.json", "poster.html"),
        "deck": ("slides_author_manifest.json", "deck.html"),
        "landing": ("landing_author_manifest.json", "index.html"),
        "video": ("video_author_manifest.json", "deck.html"),
    }[artifact_type]
    html_path = final_dir / html_name
    if not html_path.is_file():
        raise ValueError("published candidate HTML is missing")
    manifest = _read_json(final_dir / manifest_name)
    manifest.update({
        "artifact_type": artifact_type,
        "quality_status": quality_status,
        "quality_diagnostics": quality_diagnostics,
        "html_sha256": sha256_file(html_path),
    })
    published_final = (published_run_dir or run_dir) / "final"
    if artifact_type == "landing":
        manifest["html"] = str(published_final / "index.html")
        manifest["preview"] = str(published_final / "preview.png")
    elif artifact_type == "deck":
        preview = manifest.get("preview")
        if isinstance(preview, dict):
            preview["path"] = str(published_final / "preview.png")
    elif artifact_type == "poster":
        preview = manifest.get("preview")
        if isinstance(preview, dict):
            preview["path"] = str(published_final / "preview.png")
    if artifact_type == "deck":
        slides_path = final_dir / "slides.html"
        if not slides_path.is_file() or slides_path.read_bytes() != html_path.read_bytes():
            raise ValueError("final/deck.html and final/slides.html must be byte-identical")
        manifest["slides_html_sha256"] = sha256_file(slides_path)
    sidecar_names = {
        "deck": (
            "slides_asset_catalog.json",
            "slides_visual_plan.json",
            "slides_validation.json",
            "slides_browser_qa.json",
        ),
        "landing": (
            "landing_asset_catalog.json",
            "landing_visual_plan.json",
            "landing_validation.json",
            "landing_browser_qa.json",
        ),
    }.get(artifact_type, ())
    if sidecar_names:
        manifest["sidecar_sha256"] = {
            name: sha256_file(final_dir / name)
            for name in sidecar_names
            if (final_dir / name).is_file()
        }
    atomic_write_json(final_dir / manifest_name, manifest)


def _sidecar_integrity_issue(
    final_dir: Path,
    manifest_name: str,
    required_names: tuple[str, ...],
    *,
    issue_id: str,
) -> dict[str, str] | None:
    manifest = _read_json(final_dir / manifest_name)
    bound_hashes = manifest.get("sidecar_sha256")
    if not isinstance(bound_hashes, dict):
        return {
            "issue_id": issue_id,
            "message": "Final manifest is missing sidecar hash bindings.",
        }
    for name in required_names:
        expected = str(bound_hashes.get(name) or "").strip().lower()
        path = final_dir / name
        if not expected or not path.is_file() or sha256_file(path).lower() != expected:
            return {
                "issue_id": issue_id,
                "message": f"Final sidecar is missing or changed: {name}",
            }
    return None


def validate_candidate_draft(
    run_dir: Path,
    artifact_type: str,
    settings: Any,
    cancellation_token: Any | None = None,
) -> list[dict[str, str]]:
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled("candidate_publish.validation.start")
    final_dir = run_dir / "final"
    if artifact_type == "landing":
        from .agents.external_landing_author import (
            _merge_landing_browser_audit,
            _trusted_landing_source_hashes,
            _validate_landing_output,
            audit_landing_html,
        )

        try:
            trusted_hashes = _trusted_landing_source_hashes(
                run_dir,
                _read_json(final_dir / "landing_asset_catalog.json"),
                require_existing=True,
                require_catalog_match=False,
            )
        except (OSError, ValueError) as exc:
            return _record_delivery_assessment(run_dir, artifact_type, [{
                "issue_id": "landing_trusted_source_anchor_invalid",
                "message": str(exc),
            }])
        catalog_ids = {
            str(item.get("asset_id") or "").strip()
            for item in _read_json(
                final_dir / "landing_asset_catalog.json"
            ).get("assets") or []
            if (
                isinstance(item, dict)
                and str(item.get("asset_id") or "").strip()
                and str(item.get("output_file") or "").strip()
            )
        }
        if set(trusted_hashes) != catalog_ids:
            return _record_delivery_assessment(run_dir, artifact_type, [{
                "issue_id": "landing_trusted_source_catalog_mismatch",
                "message": "Landing source catalog does not match the trusted source anchor.",
            }])
        sidecar_issue = _sidecar_integrity_issue(
            final_dir,
            "landing_author_manifest.json",
            ("landing_asset_catalog.json", "landing_visual_plan.json"),
            issue_id="landing_sidecar_integrity_failed",
        )
        if sidecar_issue is not None:
            return _record_delivery_assessment(
                run_dir,
                artifact_type,
                [sidecar_issue],
            )
        diagnostics = _validate_landing_output(
            final_dir,
            trusted_source_hashes=trusted_hashes,
        )
        metrics = diagnostics.get("metrics")
        metrics = metrics if isinstance(metrics, dict) else {}
        browser_audit = audit_landing_html(
            final_dir / "index.html",
            required_source_ids=metrics.get("used_source_visual_ids") or [],
        )
        diagnostics = _merge_landing_browser_audit(diagnostics, browser_audit)
        atomic_write_json(final_dir / "landing_browser_qa.json", browser_audit)
        atomic_write_json(final_dir / "landing_validation.json", diagnostics)
        return _record_delivery_assessment(
            run_dir,
            artifact_type,
            [
                item
                for item in diagnostics.get("findings") or []
                if isinstance(item, dict)
            ],
        )
    if artifact_type == "deck":
        from .agents.external_slides_author import (
            _merge_slides_browser_audit,
            _trusted_slides_source_hashes,
            _validate_slides,
            audit_slides_html,
        )

        deck_html = final_dir / "deck.html"
        slides_html = final_dir / "slides.html"
        if (
            not deck_html.is_file()
            or not slides_html.is_file()
            or sha256_file(deck_html) != sha256_file(slides_html)
        ):
            return _record_delivery_assessment(run_dir, artifact_type, [{
                "issue_id": "deck_html_alias_mismatch",
                "message": "final/deck.html and final/slides.html must be byte-identical.",
            }])

        validation = _read_json(final_dir / "slides_validation.json")
        expected = int(validation.get("expected_slide_count") or 1)
        catalog = _read_json(final_dir / "slides_asset_catalog.json")
        slides_ctx = ToolContext(
            settings=settings,
            run_dir=run_dir,
            layers_dir=run_dir / "layers",
            run_id=run_dir.name,
            cancellation_token=cancellation_token,
        )
        try:
            trusted_hashes = _trusted_slides_source_hashes(
                slides_ctx,
                catalog,
                require_existing=True,
                require_catalog_match=False,
            )
        except (OSError, ValueError) as exc:
            return _record_delivery_assessment(run_dir, artifact_type, [{
                "issue_id": "slides_trusted_source_anchor_invalid",
                "message": str(exc),
            }])
        catalog_ids: set[str] = set()
        for item in catalog.get("assets") or []:
            if not isinstance(item, dict):
                continue
            rendered_layer = item.get("rendered_layer")
            provenance = item.get("provenance")
            rendered_path = str(
                rendered_layer.get("src_path") or ""
            ).strip() if isinstance(rendered_layer, dict) else ""
            provenance_path = str(
                provenance.get("output_file") or ""
            ).strip() if isinstance(provenance, dict) else ""
            asset_id = str(item.get("asset_id") or "").strip()
            if asset_id and (rendered_path or provenance_path):
                catalog_ids.add(asset_id)
        if set(trusted_hashes) != catalog_ids:
            return _record_delivery_assessment(run_dir, artifact_type, [{
                "issue_id": "slides_trusted_source_catalog_mismatch",
                "message": "Deck source catalog does not match the trusted source anchor.",
            }])
        sidecar_issue = _sidecar_integrity_issue(
            final_dir,
            "slides_author_manifest.json",
            ("slides_asset_catalog.json", "slides_visual_plan.json"),
            issue_id="slides_sidecar_integrity_failed",
        )
        if sidecar_issue is not None:
            return _record_delivery_assessment(
                run_dir,
                artifact_type,
                [sidecar_issue],
            )
        result = _validate_slides(
            deck_html,
            attempt_dir=final_dir,
            expected_slide_count=expected,
            visual_plan=_read_json(final_dir / "slides_visual_plan.json"),
            catalog=catalog,
            trusted_source_hashes=trusted_hashes,
        )
        browser_audit = audit_slides_html(
            deck_html,
            required_source_ids=result.get("source_visual_ids") or [],
            expected_slide_count=expected,
        )
        result = _merge_slides_browser_audit(result, browser_audit)
        atomic_write_json(final_dir / "slides_browser_qa.json", browser_audit)
        atomic_write_json(final_dir / "slides_validation.json", result)
        return _record_delivery_assessment(
            run_dir,
            artifact_type,
            [
                item
                for item in result.get("issues") or []
                if isinstance(item, dict)
            ],
        )
    if artifact_type == "video":
        from .agents.external_video_author import (
            _load_trusted_video_source_context,
            validate_video_author_output,
        )

        source = final_dir / "deck.html"
        project_dir = final_dir / "project"
        project_html = project_dir / "index.html"
        manifest = _read_json(final_dir / "video_author_manifest.json") or _read_json(
            project_dir / "video_author_manifest.json"
        )
        if not source.is_file() or not project_dir.is_dir() or not manifest:
            return _record_delivery_assessment(run_dir, artifact_type, [{
                "issue_id": "video_candidate_project_missing",
                "message": "The editable Video project or author manifest is incomplete.",
            }])
        try:
            trusted = _load_trusted_video_source_context(run_dir)
        except ValueError as exc:
            return _record_delivery_assessment(run_dir, artifact_type, [{
                "issue_id": "video_trusted_source_context_invalid",
                "message": str(exc),
            }])
        shutil.copy2(source, project_html)
        errors = validate_video_author_output(
            project_dir=project_dir,
            manifest=manifest,
            eligible_asset_ids=set(trusted["eligible_asset_ids"]),
            eligible_asset_roles=trusted["eligible_asset_roles"],
            eligible_asset_hashes=trusted["eligible_asset_hashes"],
            required_asset_ids=set(trusted["required_asset_ids"]),
            minimum_required_visual_count=int(
                trusted["minimum_required_visual_count"]
            ),
        )
        if errors:
            return _record_delivery_assessment(run_dir, artifact_type, [{
                "issue_id": "video_source_contract_invalid",
                "message": "; ".join(errors),
            }])
    source = final_dir / ("poster.html" if artifact_type == "poster" else "deck.html")
    try:
        if artifact_type == "poster":
            from .agents.external_designer_author import ExternalDesignerAuthor
            from .tools.ingest_document import _load_ingest_state_from_dir

            ctx = ToolContext(
                settings=settings,
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id=run_dir.name,
                cancellation_token=cancellation_token,
            )
            loaded = _load_ingest_state_from_dir(ctx, run_dir)
            if isinstance(loaded, ToolResultRecord):
                payload = loaded.payload or {}
                return _record_delivery_assessment(run_dir, artifact_type, [{
                    "issue_id": str(
                        payload.get("issue_id")
                        or "poster_validation_context_missing"
                    ),
                    "message": (
                        loaded.error_message
                        or "Poster validation context is incomplete."
                    ),
                }])
            lineage = _read_json(run_dir / "candidate_draft_lineage.json")
            attempt = int(lineage.get("source_attempt") or 1)
            feedback = ExternalDesignerAuthor(
                settings,
                "",
            )._direct_final_validation_feedback(
                ctx,
                attempt_index=attempt,
                attempt_dir=final_dir,
                poster_path=source,
            )
            if feedback is not None:
                payload = feedback.get("payload") if isinstance(
                    feedback.get("payload"), dict
                ) else {}
                summary = feedback.get("summary") if isinstance(
                    feedback.get("summary"), dict
                ) else {}
                raw_issues = payload.get("issues") if isinstance(
                    payload.get("issues"), list
                ) else summary.get("issues") if isinstance(
                    summary.get("issues"), list
                ) else []
                blockers = [
                    {
                        "issue_id": str(
                            item.get("issue_id")
                            or item.get("id")
                            or "poster_validation"
                        ),
                        "message": str(
                            item.get("message")
                            or item.get("reason")
                            or "Poster validation failed."
                        ),
                    }
                    for item in raw_issues
                    if isinstance(item, dict)
                ]
                if blockers:
                    return _record_delivery_assessment(
                        run_dir,
                        artifact_type,
                        blockers,
                    )
                return _record_delivery_assessment(run_dir, artifact_type, [{
                    "issue_id": str(
                        summary.get("issue_id")
                        or payload.get("issue_id")
                        or "poster_validation"
                    ),
                    "message": str(
                        feedback.get("error_message")
                        or "Poster validation failed."
                    ),
                }])
            return _record_delivery_assessment(run_dir, artifact_type, [])
        if cancellation_token is not None:
            cancellation_token.raise_if_cancelled(
                "candidate_publish.validation.before_preview"
            )
        screenshot_html(
            source,
            final_dir / "preview.png",
            viewport_width=1920 if artifact_type == "video" else 1440,
            viewport_height=1080 if artifact_type == "video" else 1000,
            full_page=artifact_type != "video",
        )
    except RunCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        return _record_delivery_assessment(run_dir, artifact_type, [{
            "issue_id": "draft_delivery_validation",
            "message": str(exc),
        }])
    return _record_delivery_assessment(run_dir, artifact_type, [])


def deliver_video_candidate_draft(
    run_dir: Path,
    settings: Any,
    cancellation_token: Any | None = None,
) -> list[dict[str, str]]:
    from .agents.external_video_author import deliver_authored_video_project

    final_dir = run_dir / "final"
    editable_html = final_dir / "deck.html"
    project_dir = final_dir / "project"
    project_html = project_dir / "index.html"
    manifest = _read_json(final_dir / "video_author_manifest.json") or _read_json(
        project_dir / "video_author_manifest.json"
    )
    if not editable_html.is_file() or not project_dir.is_dir():
        return [{
            "issue_id": "video_candidate_project_missing",
            "message": "The editable Video project is incomplete.",
        }]
    if not manifest:
        return [{
            "issue_id": "video_candidate_manifest_missing",
            "message": "The Video author manifest is missing or invalid.",
        }]
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled("candidate_publish.video.before_sync")
    shutil.copy2(editable_html, project_html)
    ctx = ToolContext(
        settings=settings,
        run_dir=run_dir,
        layers_dir=run_dir / "layers",
        run_id=run_dir.name,
        cancellation_token=cancellation_token,
    )
    ctx.state["artifact_type"] = "video"
    result = deliver_authored_video_project(
        project_dir=project_dir,
        manifest=manifest,
        ctx=ctx,
    )
    if result.status == "error":
        return [{
            "issue_id": "video_candidate_delivery_failed",
            "message": result.error_message or "Video delivery failed.",
        }]
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled(
            "candidate_publish.video.before_finalize"
        )
    finalize_result = invoke_designer_tool(
        "finalize",
        {"notes": "Published candidate passed formal Video delivery validation."},
        ctx,
    )
    if finalize_result.status == "error":
        return [{
            "issue_id": "video_candidate_finalize_failed",
            "message": (
                finalize_result.error_message
                or "Video finalization failed after delivery passed."
            ),
        }]
    if not (final_dir / "video_delivery.json").is_file():
        return [{
            "issue_id": "video_candidate_delivery_pointer_missing",
            "message": "Video finalization did not publish final/video_delivery.json.",
        }]
    return []


def run_candidate_publish_job(
    *,
    run_id: str,
    parent_run_id: str,
    conversation_id: str,
    settings: Any,
    cancellation_token: Any,
    runs_dir: Path | None = None,
    source_attempt: int | None = None,
    expected_candidate_sha256: str | None = None,
) -> dict[str, Any]:
    runs_dir = (
        Path(runs_dir) if runs_dir is not None else Path(settings.out_dir) / "runs"
    ).resolve()
    parent_dir = (runs_dir / parent_run_id).resolve()
    run_dir = (runs_dir / run_id).resolve()
    if parent_dir.parent != runs_dir or run_dir.parent != runs_dir:
        raise ValueError("candidate publish path escaped the runs directory")
    if (source_attempt is None) != (expected_candidate_sha256 is None):
        raise ValueError(
            "source_attempt and expected_candidate_sha256 must be provided together"
        )
    if (run_dir / "final").exists():
        raise ValueError("candidate publish run already contains a final directory")
    if _video_context_journal_path(run_dir).exists() or _video_context_journal_path(
        run_dir
    ).is_symlink():
        reconcile_video_delivery_context_promotion(run_dir, accept=False)
    work_dir = Path(tempfile.mkdtemp(prefix=".candidate-publish-staging-", dir=run_dir))
    try:
        if source_attempt is not None:
            if type(source_attempt) is not int or source_attempt <= 0:
                raise ValueError("source_attempt must be a positive integer")
            candidate = load_attempt_candidate(parent_dir, source_attempt)
            if candidate.source_sha256 != expected_candidate_sha256:
                raise ValueError("attempt candidate changed before publication")
            if candidate.safety_state == "blocked":
                raise ValueError("blocked attempt candidate cannot be published")
            fork_result = materialize_attempt_candidate_draft(
                run_id=run_id,
                parent_run_id=parent_run_id,
                conversation_id=conversation_id,
                source_run_dir=parent_dir,
                run_dir=work_dir,
                candidate=candidate,
                settings=settings,
                cancellation_token=cancellation_token,
            )
            lineage = fork_result["lineage"]
            artifact_type = str(fork_result["artifact_type"])
        else:
            lineage = _read_json(parent_dir / "candidate_draft_lineage.json")
            if lineage.get("status") != "draft":
                raise ValueError("candidate draft is not publishable")
            artifact_type = str(lineage.get("artifact_type") or "")
            if artifact_type not in {"poster", "deck", "landing", "video"}:
                raise ValueError("candidate draft artifact type is invalid")

            cancellation_token.raise_if_cancelled("candidate_publish.before_copy")
            shutil.copytree(parent_dir / "final", work_dir / "final")
            for name in _PUBLISH_CONTEXT_FILES:
                cancellation_token.raise_if_cancelled("candidate_publish.copy_context")
                source = parent_dir / name
                if source.is_file():
                    shutil.copy2(source, work_dir / name)
            for name in _PUBLISH_CONTEXT_DIRS:
                cancellation_token.raise_if_cancelled("candidate_publish.copy_context")
                source = parent_dir / name
                if source.is_dir():
                    shutil.copytree(source, work_dir / name)

        validated_lineage = {
            **lineage,
            "status": "validated",
            "published_version_id": f"art_{run_id}:v{int(datetime.now().timestamp() * 1000)}",
            "validated_at": datetime.now(timezone.utc).isoformat(),
            "conversation_id": conversation_id or lineage.get("conversation_id"),
        }
        blockers = validate_candidate_draft(
            work_dir,
            artifact_type,
            settings,
            cancellation_token,
        )
        if not blockers and artifact_type == "video":
            blockers = deliver_video_candidate_draft(
                work_dir,
                settings,
                cancellation_token,
            )
        if blockers:
            raise ValueError(
                "candidate draft validation failed: "
                + json.dumps(blockers, ensure_ascii=False, sort_keys=True)
            )
        cancellation_token.raise_if_cancelled("candidate_publish.after_validation")
        _apply_final_manifest_quality(
            work_dir,
            artifact_type,
            published_run_dir=run_dir,
        )
        staged_final = work_dir / "final"
        source_name = {
            "poster": "poster.html",
            "landing": "index.html",
            "deck": "deck.html",
            "video": "deck.html",
        }[artifact_type]
        if not (staged_final / source_name).is_file():
            raise ValueError("published candidate HTML is missing")
        artifact_name = {
            "poster": "poster",
            "deck": "slides",
            "landing": "landing",
            "video": "video",
        }[artifact_type]
        publication_stage = Path(tempfile.mkdtemp(
            prefix=f".{artifact_name}-final-staging-",
            dir=run_dir,
        ))
        video_delivery_paths: list[Path] = []
        final_publish_committed = False
        try:
            if artifact_type == "video":
                cancellation_token.raise_if_cancelled(
                    "candidate_publish.video.before_delivery_context_publish"
                )
                video_delivery_paths = _publish_video_delivery_context(
                    work_dir,
                    run_dir,
                )
            shutil.copytree(staged_final, publication_stage, dirs_exist_ok=True)

            def post_publish() -> None:
                cancellation_token.raise_if_cancelled(
                    "candidate_publish.after_final_publish"
                )
                if artifact_type == "video":
                    delivery = validate_current_video_delivery(run_dir)
                    if not delivery.is_passed:
                        raise ValueError(
                            "published Video delivery failed current-context "
                            f"validation: {delivery.reason_code}"
                        )

            publish_artifact_directory(
                publication_stage,
                run_dir / "final",
                artifact_name=artifact_name,
                post_publish=post_publish,
            )
            final_publish_committed = True
        except BaseException:
            shutil.rmtree(publication_stage, ignore_errors=True)
            if not final_publish_committed:
                if video_delivery_paths:
                    reconcile_video_delivery_context_promotion(
                        run_dir,
                        accept=False,
                    )
            raise
        for name in _PUBLISH_CONTEXT_FILES:
            source = work_dir / name
            if source.is_file():
                shutil.copy2(source, run_dir / name)
        for name in _PUBLISH_CONTEXT_DIRS:
            source = work_dir / name
            if source.is_dir():
                shutil.copytree(source, run_dir / name, dirs_exist_ok=True)
        atomic_write_json(run_dir / "candidate_draft_lineage.json", validated_lineage)
        cancellation_token.raise_if_cancelled("candidate_publish.after_lineage")
        source_path = run_dir / "final" / source_name
        return {
            "run_id": run_id,
            "artifact_type": artifact_type,
            "source_path": str(source_path),
            "lineage": validated_lineage,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
