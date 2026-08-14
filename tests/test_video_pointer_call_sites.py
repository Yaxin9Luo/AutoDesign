from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from io import BytesIO
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

from autodesign import attempt_candidates as attempt_candidates_module
from autodesign import run_worker as run_worker_module
from autodesign import runner as runner_module
from autodesign import video_pointer_transaction as pointer_transaction_module
from autodesign.config import Settings
from autodesign.run_control import CancellationToken, RunCancelled, RunControlStore
from autodesign.run_supervisor import RunSupervisor, WorkerOutcome
from autodesign.run_worker_protocol import (
    VideoExportRetryWorkerRequest,
    decode_worker_result,
    encode_request,
)
from autodesign.schema import DesignSpec, VideoDeliveryContract, VideoMediaProbe
from autodesign.tools._contract import ToolContext, obs_ok
from autodesign.tools.apply_design_ops import apply_design_ops
from autodesign.tools.finalize import finalize
from autodesign.tools.propose_design_spec import propose_design_spec
from autodesign.util.design_spec_fingerprint import design_spec_sha256
from autodesign.util.io import atomic_write_json, sha256_file
from autodesign.web_run_services import WebRunServices
from scripts import web_server
from tests.test_video_delivery_contract import _authored_html, _scenes
from tests.test_video_runner_finalize import (
    _FakeWindowsRetainedHandleAPI,
    _context as _finalize_context,
    _install_passed_delivery,
)
from tests.test_video_web_delivery import _passed_delivery


REPO_ROOT = Path(__file__).resolve().parents[1]
VIDEO_MODULE = importlib.import_module("autodesign.tools.export_video")
PROPOSE_MODULE = importlib.import_module("autodesign.tools.propose_design_spec")
APPLY_MODULE = importlib.import_module("autodesign.tools.apply_design_ops")
CORE_VIDEO_VALIDATOR_MODULE = "autodesign.video_delivery_validation"
CORE_VIDEO_VALIDATOR_NAME = "validate_current_video_delivery"
CONSUMER_VIDEO_VALIDATOR_ALIAS = "_validate_current_video_delivery"


def _video_spec(text: str) -> dict[str, object]:
    return {
        "brief": "Create a source-grounded conference video.",
        "artifact_type": "video",
        "canvas": {"w_px": 1920, "h_px": 1080},
        "html_artifact": {
            "target": "video",
            "theme": {},
            "frames": [
                {
                    "frame_id": "scene_01",
                    "kind": "scene",
                    "role": "intro",
                    "blocks": [
                        {
                            "block_id": "headline",
                            "kind": "text",
                            "text": text,
                        }
                    ],
                }
            ],
        },
    }


def _poster_spec(text: str) -> dict[str, object]:
    return {
        "brief": "Exercise failure-atomic rendered-layer ownership.",
        "artifact_type": "poster",
        "canvas": {
            "w_px": 768,
            "h_px": 1024,
            "dpi": 150,
            "aspect_ratio": "3:4",
            "color_mode": "RGB",
        },
        "palette": ["#f7f2e8", "#1c1917"],
        "typography": {},
        "mood": ["academic"],
        "composition_notes": "One editable rendered text layer.",
        "layer_graph": [
            {
                "layer_id": "title",
                "name": "Title",
                "kind": "text",
                "z_index": 1,
                "bbox": {"x": 40, "y": 40, "w": 688, "h": 160},
                "text": text,
                "font_family": "NotoSansSC",
                "font_size_px": 64,
                "font_weight": 700,
                "align": "left",
                "effects": {"fill": "#1c1917"},
            }
        ],
    }


def _export_context(root: Path) -> ToolContext:
    scenes = _scenes()
    root.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(
        settings=SimpleNamespace(
            enable_video_composer=False,
            prompts_dir=root,
            composer_model="test-composer",
            poster_harness_mode="cheap",
        ),
        run_dir=root,
        layers_dir=root / "layers",
        run_id="normal-video-export",
    )
    ctx.layers_dir.mkdir(exist_ok=True)
    ctx.state["artifact_type"] = "video"
    ctx.state["spec_revision_count"] = 1
    ctx.state["design_spec"] = SimpleNamespace(
        artifact_type="video",
        brief="Paper title and conference summary",
        palette=["#111111", "#eeeeee"],
        mood=["academic"],
        typography={},
        design_system=None,
        model_dump=lambda mode=None: {
            "artifact_type": "video",
            "brief": "Paper title and conference summary",
            "scene_ids": [scene.scene_id for scene in scenes],
        },
        html_artifact=SimpleNamespace(
            model_dump=lambda mode=None: {
                "frames": [
                    {
                        "frame_id": scene.scene_id,
                        "kind": "scene",
                        "title": scene.title,
                        "duration_s": scene.duration_s,
                        "speaker_notes": scene.narration_text,
                        "blocks": [],
                    }
                    for scene in scenes
                ]
            }
        ),
    )
    return ctx


def _rewrite_web_delivery_for_spec(
    root: Path,
    spec: DesignSpec,
    *,
    revision: int = 1,
) -> tuple[Path, Path, Path]:
    manifest_path, mp4_path = _passed_delivery(root)
    spec_payload = spec.model_dump(mode="json")
    spec_hash = design_spec_sha256(spec)
    atomic_write_json(
        root / "design_spec.json",
        {
            "artifact_type": "video",
            "is_revision": False,
            "revision": revision,
            "design_spec_sha256": spec_hash,
            "design_spec": spec_payload,
        },
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["design_spec_sha256"] = spec_hash
    manifest["design_spec_revision"] = revision
    atomic_write_json(manifest_path, manifest)
    pointer_path = root / "final" / "video_delivery.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer.update(
        {
            "manifest_sha256": sha256_file(manifest_path),
            "design_spec_sha256": spec_hash,
            "design_spec_revision": revision,
        }
    )
    atomic_write_json(pointer_path, pointer)
    return manifest_path, mp4_path, pointer_path


def _design_context_with_delivery(root: Path) -> tuple[ToolContext, Path, Path]:
    spec = DesignSpec.model_validate(_video_spec("Prior grounded result"))
    manifest_path, _mp4_path, pointer_path = _rewrite_web_delivery_for_spec(
        root,
        spec,
    )
    ctx = ToolContext(
        settings=SimpleNamespace(poster_harness_mode="cheap"),
        run_dir=root,
        layers_dir=root / "layers",
        run_id="video-spec-transaction",
    )
    ctx.layers_dir.mkdir(exist_ok=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ctx.state.update(
        {
            "artifact_type": "video",
            "design_spec": spec,
            "design_spec_sha256": design_spec_sha256(spec),
            "spec_revision_count": 1,
            "rendered_layers": {"retained": {"value": "before"}},
            "layer_versions": {"retained": 4},
            "composition": {"identity": "prior-composition"},
            "video_delivery": {
                "status": "passed",
                "project_dir": str(manifest_path.parent),
                "manifest_path": str(manifest_path),
                "media_probe_path": str(manifest_path.parent / "media_probe.json"),
                "mp4_path": str(
                    manifest_path.parent / str(manifest["mp4_path"])
                ),
                "render_started_at": manifest["render_started_at"],
                "design_spec_sha256": design_spec_sha256(spec),
                "design_spec_revision": 1,
                "delivery_manifest_sha256": sha256_file(manifest_path),
            },
            "finalized": True,
            "last_composite_payload": {
                "artifact_type": "video",
                "identity": "prior-composite-payload",
            },
            "last_design_feedback": {"identity": "prior-feedback"},
            "visual_reference_revision_required": True,
            "visual_reference_revision_source_spec_revision": 8,
            "visual_reference_revision_spec_revision": 1,
            "visual_reference_revision_composited": True,
            "custom_absent_control": None,
        }
    )
    ctx.state.pop("custom_absent_control")
    return ctx, pointer_path, manifest_path


def _poster_design_context(root: Path) -> ToolContext:
    root.mkdir(parents=True, exist_ok=True)
    spec = DesignSpec.model_validate(_poster_spec("Prior owned title"))
    spec_hash = design_spec_sha256(spec)
    atomic_write_json(
        root / "design_spec.json",
        {
            "artifact_type": "poster",
            "is_revision": False,
            "revision": 1,
            "design_spec_sha256": spec_hash,
            "design_spec": spec.model_dump(mode="json"),
        },
    )
    ctx = ToolContext(
        settings=SimpleNamespace(poster_harness_mode="cheap"),
        run_dir=root,
        layers_dir=root / "layers",
        run_id="poster-spec-transaction",
    )
    ctx.layers_dir.mkdir(exist_ok=True)
    prior_layer_path = ctx.layers_dir / "title_v1.png"
    prior_layer_path.write_bytes(b"prior rendered layer")
    ctx.state.update(
        {
            "artifact_type": "poster",
            "design_spec": spec,
            "design_spec_sha256": spec_hash,
            "spec_revision_count": 1,
            "rendered_layers": {
                "title": {
                    "png_path": str(prior_layer_path),
                    "owner": "prior",
                },
                "untouched": {"owner": "prior-untouched"},
            },
            "layer_versions": {"title": 1, "untouched": 4},
            "composition": {"identity": "prior-poster-composition"},
            "last_composite_payload": {"artifact_type": "poster"},
        }
    )
    return ctx


def _normalized(value: object) -> object:
    if hasattr(value, "model_dump"):
        return _normalized(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, SimpleNamespace):
        return _normalized(vars(value))
    return deepcopy(value)


def _same_parent_bound_entry(
    actual: Path,
    expected: Path,
    *,
    dst_dir_fd: int | None = None,
) -> bool:
    if actual.name != expected.name:
        return False
    if dst_dir_fd is not None and not actual.is_absolute():
        opened_parent = os.fstat(dst_dir_fd)
        expected_parent = expected.parent.stat()
        return (opened_parent.st_dev, opened_parent.st_ino) == (
            expected_parent.st_dev,
            expected_parent.st_ino,
        )
    return (
        actual.parent.resolve(strict=True)
        == expected.parent.resolve(strict=True)
    )


def _state_signature(state: dict[str, object]) -> dict[str, tuple[bool, object]]:
    return {
        key: (key in state, _normalized(state[key]))
        for key in sorted(state)
    }


def _call_without_escape(callable_, *args, **kwargs):
    try:
        return callable_(*args, **kwargs), None
    except BaseException as exc:  # tests convert escaped tool failures to RED assertions
        return None, exc


def _make_retry_project(run_dir: Path) -> Path:
    scenes = _scenes()
    project = run_dir / "hyperframes-paper-video"
    (project / "assets").mkdir(parents=True)
    (project / "renders").mkdir()
    (project / "narration").mkdir()
    (project / "index.html").write_text(
        _authored_html(scenes),
        encoding="utf-8",
    )
    contract = VideoDeliveryContract(scenes=scenes)
    (project / "video_delivery_contract.json").write_text(
        json.dumps(contract.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    (project / "meta.json").write_text(
        json.dumps({"id": "paper-video"}) + "\n",
        encoding="utf-8",
    )
    spec = {"artifact_type": "video", "name": "Paper video"}
    atomic_write_json(
        run_dir / "design_spec.json",
        {
            "design_spec": spec,
            "design_spec_sha256": design_spec_sha256(spec),
            "revision": 3,
        },
    )
    return project


def _install_prior_retry_pointer(
    run_dir: Path,
    *,
    project_dir: Path | None = None,
) -> tuple[Path, bytes, Path, Path]:
    spec_snapshot = json.loads((run_dir / "design_spec.json").read_text(encoding="utf-8"))
    prior_project = project_dir or run_dir / "hyperframes-prior-delivery"
    (prior_project / "renders").mkdir(parents=True, exist_ok=True)
    prior_mp4 = prior_project / "renders" / "prior.mp4"
    prior_mp4.write_bytes(b"prior referenced media")
    prior_manifest = prior_project / "delivery_manifest.json"
    atomic_write_json(
        prior_manifest,
        {
            "status": "passed",
            "design_spec_sha256": spec_snapshot["design_spec_sha256"],
            "design_spec_revision": 3,
            "render_started_at": "2026-08-05T00:00:00+00:00",
            "mp4_path": "renders/prior.mp4",
            "mp4_sha256": sha256_file(prior_mp4),
        },
    )
    pointer_path = run_dir / "final" / "video_delivery.json"
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_payload = {
        "manifest_path": prior_manifest.relative_to(run_dir).as_posix(),
        "manifest_sha256": sha256_file(prior_manifest),
        "design_spec_sha256": spec_snapshot["design_spec_sha256"],
        "design_spec_revision": 3,
    }
    pointer_path.write_text(
        json.dumps(pointer_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    return pointer_path, pointer_path.read_bytes(), prior_manifest, prior_mp4


def _video_transaction_inventory(final_dir: Path) -> tuple[set[str], set[str]]:
    role_names: set[str] = set()
    transaction_ids: set[str] = set()
    prefix = ".video_delivery.json."
    if not final_dir.is_dir():
        return role_names, transaction_ids
    for path in final_dir.iterdir():
        name = path.name
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix) :]
        transaction_id, separator, role = suffix.partition(".")
        if len(transaction_id) == 32 and all(
            character in "0123456789abcdef" for character in transaction_id
        ):
            transaction_ids.add(transaction_id)
            if separator and (
                role in {"new", "prior", "displaced"}
                or role.startswith("conflict-")
            ):
                role_names.add(name)
    return role_names, transaction_ids


_VIDEO_PHASE_NAME = re.compile(
    r"^\.video_delivery\.json\.([0-9a-f]{32})\."
    r"phase-([0-9]{6})-([a-z][a-z0-9-]{0,47})\.json$"
)


def _video_phase_inventory(final_dir: Path) -> dict[str, set[str]]:
    phases: dict[str, set[str]] = {}
    if not final_dir.is_dir():
        return phases
    for path in final_dir.iterdir():
        match = _VIDEO_PHASE_NAME.fullmatch(path.name)
        if match is not None:
            phases.setdefault(match.group(1), set()).add(match.group(3))
    return phases


def _prepared_prior_sha256(final_dir: Path, txid: str) -> str | None:
    for path in final_dir.iterdir():
        match = _VIDEO_PHASE_NAME.fullmatch(path.name)
        if match is None or match.group(1) != txid or match.group(3) != "prepared":
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        prior_snapshot = payload.get("prior_snapshot")
        if isinstance(prior_snapshot, dict):
            stable = prior_snapshot.get("stable")
            if isinstance(stable, dict):
                value = stable.get("sha256")
                return value if isinstance(value, str) else None
    return None


def _crash_pointer_publication(
    run_dir: Path,
    *,
    prior_bytes: bytes | None,
    payload: dict[str, object],
    crash_phase: str,
) -> subprocess.CompletedProcess[str]:
    run_dir.mkdir(parents=True, exist_ok=True)
    pointer_path = run_dir / "final" / "video_delivery.json"
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    if prior_bytes is not None:
        pointer_path.write_bytes(prior_bytes)
    script = (
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "from unittest.mock import patch\n"
        "from autodesign import video_pointer_transaction as tx\n"
        "from autodesign.attempt_candidates import update_video_delivery_pointer\n"
        "run=Path(sys.argv[1]); payload=json.loads(sys.argv[2]); wanted=sys.argv[3]\n"
        "def crash(phase, **details):\n"
        " if phase == wanted: os._exit(91)\n"
        "with patch.object(tx, '_video_pointer_transaction_phase_hook', side_effect=crash):\n"
        " update_video_delivery_pointer(run, mode='publish', payload=payload)\n"
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(run_dir),
            json.dumps(payload),
            crash_phase,
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


@contextmanager
def _patched_expensive_video_seams(
    video_module,
    *,
    captioned_name: str | None = None,
):
    base_probe = VideoMediaProbe(
        video_codec="h264",
        pixel_format="yuv420p",
        audio_codec="aac",
        width=1920,
        height=1080,
        fps=30,
        duration_s=360,
    )
    captioned_probe = base_probe.model_copy(
        update={"subtitle_codec": "mov_text", "subtitle_forced": False}
    )

    def synthesize(proj_dir, *, scene_manifest, **_kwargs):
        audio_path = proj_dir / "assets" / "narration.wav"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"mock narration wav")
        timings = [
            {
                "scene_id": scene["scene_id"],
                "start_s": scene["start_s"],
                "speech_duration_s": 25.0,
                "end_s": scene["start_s"] + 25.0,
                "speed": 1.0,
            }
            for scene in scene_manifest
        ]
        return "tts ok", True, audio_path, timings

    def render(proj_dir, *_args, **_kwargs):
        raw_mp4 = proj_dir / "renders" / "retried.mp4"
        raw_mp4.parent.mkdir(parents=True, exist_ok=True)
        raw_mp4.write_bytes(b"mock rendered mp4")
        return "render ok", True, raw_mp4, base_probe

    def mux(raw_mp4, _subtitle_path, **_kwargs):
        captioned = raw_mp4.with_name(
            captioned_name or f"{raw_mp4.stem}-captions.mp4"
        )
        captioned.write_bytes(b"mock captioned mp4")
        return "mux ok", True, captioned

    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(
                video_module,
                "_run_hyperframes_lint",
                return_value=("lint ok", True),
            )
        )
        stack.enter_context(
            mock.patch.object(
                video_module,
                "_synthesize_timed_narration",
                side_effect=synthesize,
            )
        )
        stack.enter_context(
            mock.patch.object(
                video_module,
                "_run_hyperframes_render",
                side_effect=render,
            )
        )
        stack.enter_context(
            mock.patch.object(
                video_module,
                "_mux_optional_subtitle_track",
                side_effect=mux,
            )
        )
        stack.enter_context(
            mock.patch.object(
                video_module,
                "_probe_video",
                return_value=(captioned_probe, None),
            )
        )
        yield


def _retained_bytes(root: Path) -> list[bytes]:
    values: list[bytes] = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                values.append(path.read_bytes())
            except OSError:
                pass
    return values


def _write_resume_metadata(run_dir: Path) -> None:
    (run_dir / "run_brief.json").write_text(
        json.dumps({"version": 1, "artifact_type": "video"}),
        encoding="utf-8",
    )
    (run_dir / "resume_state.json").write_text(
        json.dumps({"artifact_type": "video"}),
        encoding="utf-8",
    )
    (run_dir / "canvas_plan.json").write_text(
        json.dumps({"artifact_type": "video"}),
        encoding="utf-8",
    )
    attempt_dir = run_dir / "video_author" / "attempt_01"
    project_dir = attempt_dir / "project"
    project_dir.mkdir(parents=True)
    (project_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    (attempt_dir / "video_author_manifest.json").write_text(
        json.dumps({"version": 1, "scenes": []}),
        encoding="utf-8",
    )
    (run_dir / "run_events.jsonl").write_text(
        json.dumps({"event": "run.done", "terminal_status": "pass"}) + "\n",
        encoding="utf-8",
    )


def _mutate_delivery_case(root: Path, case: str) -> None:
    pointer_path = root / "final" / "video_delivery.json"
    if case == "tombstone":
        payload = json.loads(pointer_path.read_text(encoding="utf-8"))
        payload.update(
            {
                "status": "invalidated",
                "invalidation_id": "test-invalidation",
                "reason": "test",
                "prior_pointer_sha256": "0" * 64,
                "invalidated_at": "2026-08-05T00:00:00+00:00",
            }
        )
        atomic_write_json(pointer_path, payload)
    elif case == "link":
        real_pointer = pointer_path.with_name("video_delivery.real.json")
        pointer_path.rename(real_pointer)
        pointer_path.symlink_to(real_pointer.name)
    elif case == "stale":
        spec_path = root / "design_spec.json"
        snapshot = json.loads(spec_path.read_text(encoding="utf-8"))
        snapshot["revision"] = int(snapshot.get("revision") or 0) + 1
        snapshot["design_spec"] = {
            **snapshot["design_spec"],
            "brief": "A genuinely newer DesignSpec",
        }
        snapshot["design_spec_sha256"] = design_spec_sha256(snapshot["design_spec"])
        atomic_write_json(spec_path, snapshot)
    elif case == "integrity":
        manifest_path = root / "hyperframes-paper-video" / "delivery_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        (manifest_path.parent / str(manifest["mp4_path"])).write_bytes(b"tampered")
    elif case == "malformed":
        pointer_path.write_text("{malformed", encoding="utf-8")
    else:  # pragma: no cover - test helper invariant
        raise AssertionError(case)


def _typed_validation_observation(
    value: object,
    *,
    root: Path | None = None,
) -> dict[str, object]:
    reason = getattr(value, "reason_code", None)
    public_paths = getattr(value, "public_paths", None)
    snapshots = getattr(value, "snapshots", None)
    relative_paths = False
    if isinstance(public_paths, dict) and public_paths:
        relative_paths = all(
            bool(Path(str(path)).parts)
            and not Path(str(path)).is_absolute()
            and ".." not in Path(str(path)).parts
            for path in public_paths.values()
        )
    snapshot_digests: dict[str, str] = {}
    snapshot_data_matches: dict[str, bool] = {}
    if isinstance(snapshots, dict):
        for name, snapshot in snapshots.items():
            digest = (
                snapshot.get("sha256")
                if isinstance(snapshot, dict)
                else getattr(snapshot, "sha256", None)
            )
            if isinstance(digest, str):
                snapshot_digests[str(name)] = digest
            if root is None or not isinstance(public_paths, dict):
                continue
            public_path = public_paths.get(name)
            if public_path is None or not isinstance(digest, str):
                continue
            relative = Path(str(public_path))
            disk_path = root / relative
            snapshot_relative = (
                snapshot.get("relative_path")
                if isinstance(snapshot, dict)
                else getattr(snapshot, "relative_path", None)
            )
            snapshot_data = (
                snapshot.get("data")
                if isinstance(snapshot, dict)
                else getattr(snapshot, "data", None)
            )
            snapshot_size = (
                snapshot.get("size")
                if isinstance(snapshot, dict)
                else getattr(snapshot, "size", None)
            )
            try:
                disk_bytes = disk_path.read_bytes()
            except OSError:
                snapshot_data_matches[str(name)] = False
                continue
            snapshot_data_matches[str(name)] = (
                digest == hashlib.sha256(disk_bytes).hexdigest()
                and (snapshot_relative is None or Path(snapshot_relative) == relative)
                and (snapshot_size is None or snapshot_size == len(disk_bytes))
                and (
                    snapshot_data is None
                    or (
                        isinstance(snapshot_data, bytes)
                        and snapshot_data == disk_bytes
                        and hashlib.sha256(snapshot_data).hexdigest() == digest
                    )
                )
            )
    required_roles = {"manifest", "mp4", "pointer"}
    return {
        "typed_leaf": type(value).__module__.startswith("autodesign."),
        "leaf_boundary_violations": _leaf_boundary_violations(value),
        "reason_code": reason,
        "run_relative_paths": relative_paths,
        "snapshot_roles": sorted(snapshot_digests),
        "snapshot_digests_valid": bool(snapshot_digests)
        and all(
            len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
            for digest in snapshot_digests.values()
        ),
        "snapshot_digests_match_files": (
            set(snapshot_data_matches) == required_roles
            and all(snapshot_data_matches.values())
        ),
    }


def _leaf_boundary_violations(value: object) -> list[str]:
    module_name = type(value).__module__
    if not module_name.startswith("autodesign."):
        return ["validator_result_not_in_autodesign_leaf"]
    return _module_boundary_violations(module_name)


def _callable_leaf_boundary_violations(value: object) -> list[str]:
    module_name = str(getattr(value, "__module__", ""))
    if not callable(value) or not module_name.startswith("autodesign."):
        return ["validator_callable_not_in_autodesign_leaf"]
    return _module_boundary_violations(module_name)


def _module_boundary_violations(module_name: str) -> list[str]:
    module = sys.modules.get(module_name)
    if not isinstance(module, ModuleType):
        try:
            module = importlib.import_module(module_name)
        except (ImportError, AttributeError, ValueError):
            return ["validator_leaf_not_importable"]

    violations: set[str] = set()
    for dependency in vars(module).values():
        if isinstance(dependency, ModuleType):
            dependency_module = dependency.__name__
            dependency_name = dependency.__name__.rsplit(".", 1)[-1]
        else:
            dependency_module = str(getattr(dependency, "__module__", ""))
            dependency_name = str(getattr(dependency, "__name__", ""))
        if dependency_module == "scripts.web_server":
            violations.add("web_transport")
        if (
            dependency is ToolContext
            or (
                dependency_module == "autodesign.tools._contract"
                and dependency_name == "ToolContext"
            )
        ):
            violations.add("ToolContext")
        if dependency is finalize or dependency_module == "autodesign.tools.finalize":
            violations.add("finalize")
    return sorted(violations)


class _CrashBeforeVideoStateClear(dict):
    def pop(self, key, *args):
        if key == "video_delivery":
            raise SystemExit("simulated crash before stale state clear")
        return super().pop(key, *args)


class _CancelAfterPointerCommit:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.cancelled = False

    def raise_if_cancelled(self, phase: str) -> None:
        if self.cancelled:
            raise RunCancelled(self.run_id, phase)

    def is_cancelled(self) -> bool:
        return self.cancelled


class VideoPointerCallSiteRedTests(unittest.TestCase):
    def test_runtime_call_sites_never_unlink_the_exact_pointer(self) -> None:
        video_module = VIDEO_MODULE

        cases = ("normal_export", "retry", "design_spec_revision")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp) / "run"
                attempted_unlinks: list[Path] = []
                if case == "normal_export":
                    ctx = _export_context(root)
                    _install_passed_delivery(ctx)
                    self.assertEqual(finalize({}, ctx=ctx).status, "ok")
                    pointer_path = root / "final" / "video_delivery.json"
                elif case == "retry":
                    project = _make_retry_project(root)
                    pointer_path, _prior, _manifest, _mp4 = _install_prior_retry_pointer(root)
                else:
                    ctx, pointer_path, _manifest = _design_context_with_delivery(root)
                prior_pointer = pointer_path.read_bytes()
                original_unlink = Path.unlink

                def record_unlink(path_self: Path, *args, **kwargs):
                    if Path(path_self).resolve() == pointer_path.resolve():
                        attempted_unlinks.append(Path(path_self))
                    return original_unlink(path_self, *args, **kwargs)

                with mock.patch.object(Path, "unlink", new=record_unlink):
                    if case == "normal_export":
                        result, escaped = _call_without_escape(
                            video_module.export_video,
                            {"video_id": "replacement"},
                            ctx=ctx,
                        )
                    elif case == "retry":
                        with mock.patch.object(
                            video_module,
                            "_run_hyperframes_lint",
                            return_value=("expected stop after invalidation", False),
                        ):
                            result, escaped = _call_without_escape(
                                video_module.retry_video_export_project,
                                root,
                                project,
                            )
                    else:
                        result, escaped = _call_without_escape(
                            propose_design_spec,
                            {"design_spec": _video_spec("Revised grounded result")},
                            ctx=ctx,
                        )

                observed = {
                    "escaped": None if escaped is None else type(escaped).__name__,
                    "exact_unlinks": len(attempted_unlinks),
                    "pointer_retained": pointer_path.is_file(),
                    "revision_pointer_unchanged": (
                        pointer_path.read_bytes() == prior_pointer
                        if case == "design_spec_revision" and pointer_path.is_file()
                        else case != "design_spec_revision"
                    ),
                    "tool_status": getattr(result, "status", None)
                    if case != "retry"
                    else (result or {}).get("phase"),
                }
                expected_status = (
                    "ok"
                    if case == "design_spec_revision"
                    else "authoring_lint" if case == "retry" else "error"
                )
                self.assertEqual(
                    observed,
                    {
                        "escaped": None,
                        "exact_unlinks": 0,
                        "pointer_retained": True,
                        "revision_pointer_unchanged": True,
                        "tool_status": expected_status,
                    },
                )

    def test_secure_invalidation_retains_identity_and_is_idempotent(self) -> None:
        video_module = VIDEO_MODULE

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "run"
            ctx = _finalize_context(root)
            manifest_path, _mp4_path = _install_passed_delivery(ctx)
            self.assertEqual(finalize({}, ctx=ctx).status, "ok")
            pointer_path = root / "final" / "video_delivery.json"
            prior_bytes = pointer_path.read_bytes()
            prior_pointer = json.loads(prior_bytes)
            prior_delivery = deepcopy(ctx.state["video_delivery"])
            before_names, before_ids = _video_transaction_inventory(
                pointer_path.parent
            )

            _first, first_error = _call_without_escape(
                video_module._clear_stale_video_delivery,
                manifest_path.parent,
                ctx,
            )
            first_bytes = pointer_path.read_bytes() if pointer_path.is_file() else b""
            try:
                tombstone = json.loads(first_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError):
                tombstone = {}
            first_names, first_ids = _video_transaction_inventory(
                pointer_path.parent
            )
            _second, second_error = _call_without_escape(
                video_module._clear_stale_video_delivery,
                manifest_path.parent,
                ctx,
            )
            second_bytes = pointer_path.read_bytes() if pointer_path.is_file() else b""
            try:
                second_tombstone = json.loads(second_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError):
                second_tombstone = {}
            second_names, second_ids = _video_transaction_inventory(
                pointer_path.parent
            )

            observed = {
                "errors": [
                    None if error is None else type(error).__name__
                    for error in (first_error, second_error)
                ],
                "status": tombstone.get("status"),
                "has_fresh_id": bool(tombstone.get("invalidation_id")),
                "has_reason": bool(tombstone.get("reason")),
                "has_timestamp": bool(
                    tombstone.get("invalidated_at") or tombstone.get("timestamp")
                ),
                "prior_pointer_sha256": tombstone.get("prior_pointer_sha256"),
                "prior_manifest_sha256": tombstone.get("prior_manifest_sha256"),
                "prior_spec_sha256": tombstone.get("prior_design_spec_sha256"),
                "prior_spec_revision": tombstone.get("prior_design_spec_revision"),
                "prior_render_started_at": tombstone.get("prior_render_started_at"),
                "prior_bytes_retained": prior_bytes in _retained_bytes(root / "final"),
                "idempotent_bytes": first_bytes == second_bytes and bool(first_bytes),
                "first_invalidation_added_one_transaction": len(first_ids)
                == len(before_ids) + 1,
                "first_invalidation_retained_a_role": len(first_names)
                > len(before_names),
                "second_invalidation_added_no_transaction": second_ids == first_ids,
                "second_invalidation_added_no_role": second_names == first_names,
                "same_invalidation_id": bool(tombstone.get("invalidation_id"))
                and second_tombstone.get("invalidation_id")
                == tombstone.get("invalidation_id")
                and bool(second_bytes),
            }
            self.assertEqual(
                observed,
                {
                    "errors": [None, None],
                    "status": "invalidated",
                    "has_fresh_id": True,
                    "has_reason": True,
                    "has_timestamp": True,
                    "prior_pointer_sha256": hashlib.sha256(prior_bytes).hexdigest(),
                    "prior_manifest_sha256": prior_pointer["manifest_sha256"],
                    "prior_spec_sha256": prior_delivery["design_spec_sha256"],
                    "prior_spec_revision": prior_delivery["design_spec_revision"],
                    "prior_render_started_at": prior_delivery["render_started_at"],
                    "prior_bytes_retained": True,
                    "idempotent_bytes": True,
                    "first_invalidation_added_one_transaction": True,
                    "first_invalidation_retained_a_role": True,
                    "second_invalidation_added_no_transaction": True,
                    "second_invalidation_added_no_role": True,
                    "same_invalidation_id": True,
                },
            )

    def test_windows_consecutive_pointer_updates_seed_sid_before_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "run"
            root.mkdir()
            expected_sid = "S-1-5-21-consecutive-pointer-test"
            native_api = _FakeWindowsRetainedHandleAPI()
            portable_os = SimpleNamespace(
                name="nt",
                path=os.path,
                fspath=os.fspath,
            )
            security_sids: list[str] = []
            security_cleanup_count = 0
            guard_events: list[tuple[str, Path]] = []

            @contextmanager
            def replacement_guard(path: Path):
                guard_events.append(("enter", Path(path)))
                try:
                    yield 41
                finally:
                    guard_events.append(("exit", Path(path)))

            def security_attributes(user_sid: str):
                nonlocal security_cleanup_count
                security_sids.append(user_sid)
                marker = object()

                def cleanup() -> None:
                    nonlocal security_cleanup_count
                    security_cleanup_count += 1

                return marker, cleanup

            def publish_once(iteration: int) -> None:
                metadata = os.lstat(root)
                accessor = attempt_candidates_module.SecureRunMemberAccessor(
                    root_reference=root,
                    root_identity=(metadata.st_dev, metadata.st_ino),
                    accepted_roots=(root,),
                    requested_root=root,
                    canonical_root=root,
                    binding=None,
                )
                self._publish_pointer_with_accessor(
                    accessor,
                    iteration,
                )

            with (
                mock.patch.object(
                    attempt_candidates_module,
                    "_windows_directory_replacement_guard",
                    side_effect=replacement_guard,
                ),
                mock.patch.object(
                    attempt_candidates_module,
                    "_windows_current_user_sid",
                    return_value=expected_sid,
                ),
                mock.patch.object(
                    pointer_transaction_module,
                    "_RUNTIME_OS",
                    portable_os,
                ),
                mock.patch.object(
                    pointer_transaction_module,
                    "_windows_native_handle_api_factory",
                    return_value=native_api,
                ),
                mock.patch.object(
                    pointer_transaction_module,
                    "_windows_pointer_security_attributes_factory",
                    side_effect=security_attributes,
                ),
            ):
                publish_once(1)
                first_phases = _video_phase_inventory(root / "final")
                self.assertEqual(len(first_phases), 1)
                first_txid = next(iter(first_phases))
                self.assertIn("committed", first_phases[first_txid])
                first_pointer_bytes = (
                    root / "final" / "video_delivery.json"
                ).read_bytes()

                publish_once(2)

            final_dir = root / "final"
            final_phases = _video_phase_inventory(final_dir)
            new_txids = set(final_phases) - {first_txid}
            self.assertEqual(len(new_txids), 1)
            second_txid = next(iter(new_txids))
            self.assertIn(
                "recovery-committed-confirmed",
                final_phases[first_txid],
            )
            self.assertIn("committed", final_phases[second_txid])
            self.assertEqual(
                json.loads(
                    (final_dir / "video_delivery.json").read_text(
                        encoding="utf-8"
                    )
                ),
                {"iteration": 2},
            )
            self.assertIn(first_pointer_bytes, _retained_bytes(final_dir))
            self.assertTrue(security_sids)
            self.assertEqual(set(security_sids), {expected_sid})
            created_calls = [
                call for call in native_api.create_calls if call[4] == 1
            ]
            self.assertTrue(created_calls)
            self.assertTrue(all(call[3] is not None for call in created_calls))
            self.assertTrue(
                any(
                    "recovery-committed-confirmed" in call[0].name
                    for call in created_calls
                )
            )
            self.assertEqual(security_cleanup_count, len(created_calls))
            self.assertFalse(native_api.handles)
            self.assertEqual(len(native_api.closed), len(set(native_api.closed)))
            self.assertEqual(
                [event for event, _path in guard_events],
                ["enter", "exit", "enter", "exit"],
            )

    @staticmethod
    def _publish_pointer_with_accessor(accessor, iteration: int) -> None:
        with attempt_candidates_module._secure_run_member_accessor_lifecycle(
            accessor
        ) as retained:
            retained.stage_video_delivery_pointer(
                "final/video_delivery.json",
                {"iteration": iteration},
                label="video delivery pointer",
            )

    def test_windows_pointer_sid_failure_stops_before_recovery_and_closes_guards(
        self,
    ) -> None:
        failure_cases = (
            ("lookup-error", OSError("SID lookup failed"), OSError),
            ("none", None, ValueError),
            ("empty", "", ValueError),
            ("whitespace", "   ", ValueError),
        )
        for name, sid_result, expected_error in failure_cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp) / "run"
                root.mkdir()
                metadata = os.lstat(root)
                accessor = attempt_candidates_module.SecureRunMemberAccessor(
                    root_reference=root,
                    root_identity=(metadata.st_dev, metadata.st_ino),
                    accepted_roots=(root,),
                    requested_root=root,
                    canonical_root=root,
                    binding=None,
                )
                guard_events: list[str] = []

                @contextmanager
                def replacement_guard(_path: Path):
                    guard_events.append("enter")
                    try:
                        yield 41
                    finally:
                        guard_events.append("exit")

                sid_lookup = (
                    mock.Mock(side_effect=sid_result)
                    if isinstance(sid_result, BaseException)
                    else mock.Mock(return_value=sid_result)
                )
                recovery = mock.Mock(
                    side_effect=AssertionError("recovery must not start")
                )
                publication = mock.Mock(
                    side_effect=AssertionError("publication must not start")
                )
                with (
                    mock.patch.object(
                        attempt_candidates_module,
                        "_windows_directory_replacement_guard",
                        side_effect=replacement_guard,
                    ),
                    mock.patch.object(
                        attempt_candidates_module,
                        "_windows_current_user_sid",
                        sid_lookup,
                    ),
                    mock.patch.object(
                        attempt_candidates_module,
                        "recover_video_delivery_pointer",
                        recovery,
                    ),
                    mock.patch.object(
                        attempt_candidates_module,
                        "stage_video_delivery_pointer",
                        publication,
                    ),
                ):
                    _result, escaped = _call_without_escape(
                        accessor.stage_video_delivery_pointer,
                        "final/video_delivery.json",
                        {"iteration": 1},
                        label="video delivery pointer",
                    )

                self.assertIsInstance(escaped, expected_error)
                self.assertFalse(recovery.called)
                self.assertFalse(publication.called)
                self.assertEqual(
                    guard_events.count("enter"),
                    guard_events.count("exit"),
                )

    def test_invalidate_if_present_fails_closed_on_classification_races(self) -> None:
        video_module = VIDEO_MODULE

        class WindowsReparseRaceAPI:
            open_reparse_point = 0x00200000
            reparse_attribute = 0x400

            def __init__(self) -> None:
                self.next_handle = 100
                self.live_handles: dict[int, tuple[Path, int | None]] = {}
                self.handle_is_reparse: dict[int, bool] = {}
                self.create_calls: list[tuple[int, Path, int]] = []
                self.read_handles: list[int] = []
                self.close_counts: dict[int, int] = {}
                self.reparse_read_attempted = False
                self.reparse_basic_reported = False

            def create_file(
                self,
                path,
                _desired_access,
                _share_mode,
                _security_attributes,
                _creation_disposition,
                flags_and_attributes,
            ):
                target = Path(path)
                if not flags_and_attributes & self.open_reparse_point:
                    raise AssertionError(
                        "Windows pointer opens must use OPEN_REPARSE_POINT"
                    )
                handle = self.next_handle
                self.next_handle += 1
                is_reparse = target.is_symlink()
                descriptor = None if is_reparse else os.open(target, os.O_RDONLY)
                self.live_handles[handle] = (target, descriptor)
                self.handle_is_reparse[handle] = is_reparse
                self.create_calls.append(
                    (handle, target, int(flags_and_attributes))
                )
                return handle

            def _metadata(self, handle: int):
                target, descriptor = self.live_handles[handle]
                if self.handle_is_reparse[handle]:
                    return os.lstat(target)
                assert descriptor is not None
                return os.fstat(descriptor)

            def get_file_information_by_handle_ex(
                self,
                handle,
                info_class,
            ):
                metadata = self._metadata(handle)
                if info_class == "FileIdInfo":
                    return {
                        "volume_serial_number": int(metadata.st_dev),
                        "file_id": int(metadata.st_ino).to_bytes(
                            16,
                            "little",
                            signed=False,
                        ),
                    }
                if info_class == "FileStandardInfo":
                    return {
                        "number_of_links": int(metadata.st_nlink),
                        "end_of_file": int(metadata.st_size),
                        "directory": False,
                    }
                if info_class == "FileBasicInfo":
                    is_reparse = self.handle_is_reparse[handle]
                    if is_reparse:
                        self.reparse_basic_reported = True
                    return {
                        "file_attributes": (
                            self.reparse_attribute if is_reparse else 0x20
                        ),
                        "last_write_time": int(metadata.st_mtime_ns),
                        "creation_time": int(metadata.st_ctime_ns),
                        "change_time": int(metadata.st_ctime_ns),
                        "last_access_time": int(metadata.st_atime_ns),
                    }
                raise AssertionError(f"unexpected info class {info_class}")

            def set_file_pointer_ex(self, handle, offset):
                _target, descriptor = self.live_handles[handle]
                if descriptor is not None:
                    os.lseek(descriptor, offset, os.SEEK_SET)

            def read_file(self, handle, size):
                self.read_handles.append(handle)
                if self.handle_is_reparse[handle]:
                    self.reparse_read_attempted = True
                    raise AssertionError("reparse handle must never be read")
                _target, descriptor = self.live_handles[handle]
                assert descriptor is not None
                return os.read(descriptor, size)

            def close_handle(self, handle):
                self.close_counts[handle] = self.close_counts.get(handle, 0) + 1
                if self.close_counts[handle] != 1:
                    raise AssertionError("native HANDLE was closed twice")
                _target, descriptor = self.live_handles.pop(handle)
                if descriptor is not None:
                    os.close(descriptor)

        for race in (
            "absent_to_present",
            "regular_to_link",
            "windows_reparse",
        ):
            with self.subTest(race=race), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp) / "run"
                project = _make_retry_project(root)
                pointer_path = root / "final" / "video_delivery.json"
                pointer_path.parent.mkdir(parents=True, exist_ok=True)
                winner_bytes = b'{"foreign":"winner"}\n'
                if race in {"regular_to_link", "windows_reparse"}:
                    pointer_path.write_bytes(b'{"manifest_path":"prior"}\n')
                sentinel = project / "delivery_manifest.json"
                sentinel.write_bytes(b"must survive invalidation failure")
                ctx = ToolContext(
                    settings=SimpleNamespace(),
                    run_dir=root,
                    layers_dir=root / "layers",
                    run_id=f"race-{race}",
                )
                ctx.state.update(
                    {
                        "artifact_type": "video",
                        "video_delivery": {"status": "passed"},
                        "finalized": True,
                        "composition": {"identity": "old"},
                    }
                )
                injected = False
                guard_events: list[tuple[str, Path]] = []
                native_api: WindowsReparseRaceAPI | None = None
                publication_entry: mock.Mock | None = None
                initial_closed_before_hook = False
                hook_snapshot_data: bytes | None = None
                retained_pointer = pointer_path.with_name(
                    "video_delivery.race-winner"
                )
                transaction_inventory_before = _video_transaction_inventory(
                    pointer_path.parent
                )

                def adapter_hook(event: str, **details):
                    nonlocal injected, initial_closed_before_hook, hook_snapshot_data
                    if injected:
                        return
                    if race == "absent_to_present" and event == "classified_absent":
                        pointer_path.write_bytes(winner_bytes)
                        injected = True
                    elif (
                        race in {"regular_to_link", "windows_reparse"}
                        and event == "classified_present"
                    ):
                        if race == "windows_reparse" and native_api is not None:
                            regular_handles = [
                                handle
                                for handle, is_reparse in (
                                    native_api.handle_is_reparse.items()
                                )
                                if not is_reparse
                            ]
                            initial_closed_before_hook = (
                                len(regular_handles) == 1
                                and native_api.close_counts.get(
                                    regular_handles[0],
                                    0,
                                )
                                == 1
                                and regular_handles[0]
                                not in native_api.live_handles
                            )
                            hook_snapshot_data = getattr(
                                details.get("snapshot"),
                                "data",
                                None,
                            )
                        pointer_path.rename(retained_pointer)
                        pointer_path.symlink_to(retained_pointer.name)
                        injected = True

                @contextmanager
                def replacement_guard(path: Path):
                    guard_events.append(("enter", Path(path)))
                    try:
                        yield 41
                    finally:
                        guard_events.append(("exit", Path(path)))

                with ExitStack() as stack:
                    stack.enter_context(
                        mock.patch.object(
                            attempt_candidates_module,
                            "_video_delivery_pointer_adapter_hook",
                            side_effect=adapter_hook,
                            create=True,
                        )
                    )
                    if race == "windows_reparse":
                        native_api = WindowsReparseRaceAPI()
                        publication_entry = mock.Mock(
                            side_effect=AssertionError(
                                "transaction publication must not start"
                            )
                        )
                        portable_os = SimpleNamespace(
                            name="nt",
                            path=os.path,
                            fspath=os.fspath,
                        )
                        stack.enter_context(
                            mock.patch.object(
                                attempt_candidates_module,
                                "_RUNTIME_OS",
                                portable_os,
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(
                                pointer_transaction_module,
                                "_RUNTIME_OS",
                                portable_os,
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(
                                attempt_candidates_module,
                                "_windows_directory_replacement_guard",
                                side_effect=replacement_guard,
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(
                                attempt_candidates_module,
                                "_windows_current_user_sid",
                                return_value="S-1-5-21-reparse-race-test",
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(
                                pointer_transaction_module,
                                "_windows_native_handle_api_factory",
                                return_value=native_api,
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(
                                attempt_candidates_module,
                                "stage_video_delivery_pointer",
                                publication_entry,
                            )
                        )
                    _result, escaped = _call_without_escape(
                        video_module._clear_stale_video_delivery,
                        project,
                        ctx,
                    )

                transaction_inventory_after = _video_transaction_inventory(
                    pointer_path.parent
                )
                regular_handles = (
                    [
                        handle
                        for handle, is_reparse in native_api.handle_is_reparse.items()
                        if not is_reparse
                    ]
                    if native_api is not None
                    else []
                )
                reparse_handles = (
                    [
                        handle
                        for handle, is_reparse in native_api.handle_is_reparse.items()
                        if is_reparse
                    ]
                    if native_api is not None
                    else []
                )
                observed = {
                    "hook_injected": injected,
                    "failed_closed": escaped is not None,
                    "winner_retained": winner_bytes in _retained_bytes(root / "final")
                    if race == "absent_to_present"
                    else any(
                        value == b'{"manifest_path":"prior"}\n'
                        for value in _retained_bytes(root / "final")
                    ),
                    "media_retained": sentinel.read_bytes()
                    if sentinel.is_file()
                    else None,
                    "state_retained": ctx.state.get("video_delivery")
                    == {"status": "passed"}
                    and ctx.state.get("finalized") is True,
                    "windows_guard_balanced": (
                        bool(guard_events)
                        and [event for event, _path in guard_events].count("enter")
                        == [event for event, _path in guard_events].count("exit")
                        and guard_events[0] == ("enter", root.resolve())
                        if race == "windows_reparse"
                        else True
                    ),
                    "windows_initial_observation": (
                        hook_snapshot_data == b'{"manifest_path":"prior"}\n'
                        and initial_closed_before_hook
                        if race == "windows_reparse"
                        else True
                    ),
                    "windows_reparse_opened_no_follow": (
                        native_api is not None
                        and len(reparse_handles) == 1
                        and all(
                            flags & native_api.open_reparse_point
                            for handle, _path, flags in native_api.create_calls
                            if handle in reparse_handles
                        )
                        and native_api.reparse_basic_reported
                        and not native_api.reparse_read_attempted
                        and not any(
                            handle in native_api.read_handles
                            for handle in reparse_handles
                        )
                        if race == "windows_reparse"
                        else True
                    ),
                    "windows_leaves_closed_once": (
                        native_api is not None
                        and len(regular_handles) == 1
                        and len(reparse_handles) == 1
                        and all(
                            native_api.close_counts.get(handle) == 1
                            for handle in regular_handles + reparse_handles
                        )
                        and not native_api.live_handles
                        if race == "windows_reparse"
                        else True
                    ),
                    "windows_publication_not_entered": (
                        publication_entry is not None
                        and not publication_entry.called
                        if race == "windows_reparse"
                        else True
                    ),
                    "windows_unsafe_error": (
                        escaped is not None
                        and any(
                            marker in str(escaped).lower()
                            for marker in ("unsafe", "reparse")
                        )
                        if race == "windows_reparse"
                        else True
                    ),
                    "windows_transaction_inventory_unchanged": (
                        transaction_inventory_after == transaction_inventory_before
                        if race == "windows_reparse"
                        else True
                    ),
                    "windows_swapped_pointer_retained": (
                        pointer_path.is_symlink()
                        and retained_pointer.is_file()
                        and retained_pointer.read_bytes()
                        == b'{"manifest_path":"prior"}\n'
                        if race == "windows_reparse"
                        else True
                    ),
                }
                self.assertEqual(
                    observed,
                    {
                        "hook_injected": True,
                        "failed_closed": True,
                        "winner_retained": True,
                        "media_retained": b"must survive invalidation failure",
                        "state_retained": True,
                        "windows_guard_balanced": True,
                        "windows_initial_observation": True,
                        "windows_reparse_opened_no_follow": True,
                        "windows_leaves_closed_once": True,
                        "windows_publication_not_entered": True,
                        "windows_unsafe_error": True,
                        "windows_transaction_inventory_unchanged": True,
                        "windows_swapped_pointer_retained": True,
                    },
                )

    def test_step2e_adapter_recovers_before_hook_and_payload_factory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "run"
            prior_bytes = b'{"marker":"post-recovery-prior"}\n'
            crashed = _crash_pointer_publication(
                root,
                prior_bytes=prior_bytes,
                payload={"marker": "interrupted-publication"},
                crash_phase="published",
            )
            self.assertEqual(
                crashed.returncode,
                91,
                {"stdout": crashed.stdout, "stderr": crashed.stderr},
            )
            final_dir = root / "final"
            pointer_path = final_dir / "video_delivery.json"
            interrupted_bytes = pointer_path.read_bytes()
            crash_phases = _video_phase_inventory(final_dir)
            self.assertEqual(len(crash_phases), 1)
            crashed_txid = next(iter(crash_phases))
            observations: list[tuple[str, bytes | None]] = []
            real_stage = (
                attempt_candidates_module.SecureRunMemberAccessor
                .stage_video_delivery_pointer
            )

            def adapter_hook(_event: str, **details):
                snapshot = details.get("snapshot")
                observations.append(("hook", getattr(snapshot, "data", None)))

            def observe_payload_factory(accessor, *args, **kwargs):
                payload_factory = kwargs.get("payload_factory")
                if callable(payload_factory):
                    def recording_factory(current):
                        observations.append(
                            ("payload_factory", getattr(current, "data", None))
                        )
                        return payload_factory(current)

                    kwargs["payload_factory"] = recording_factory
                return real_stage(accessor, *args, **kwargs)

            with (
                mock.patch.object(
                    attempt_candidates_module,
                    "_video_delivery_pointer_adapter_hook",
                    side_effect=adapter_hook,
                ),
                mock.patch.object(
                    attempt_candidates_module.SecureRunMemberAccessor,
                    "stage_video_delivery_pointer",
                    autospec=True,
                    side_effect=observe_payload_factory,
                ),
            ):
                update, escaped = _call_without_escape(
                    attempt_candidates_module.update_video_delivery_pointer,
                    root,
                    mode="invalidate_if_present",
                    reason="step2e-recovery-order",
                )

            after_phases = _video_phase_inventory(final_dir)
            new_txids = set(after_phases) - set(crash_phases)
            new_txid = next(iter(new_txids)) if len(new_txids) == 1 else None
            update_payload = getattr(update, "payload", None)
            try:
                final_payload = json.loads(pointer_path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                final_payload = {}
            observed = {
                "interrupted_pointer_was_distinct": interrupted_bytes != prior_bytes,
                "escaped": None if escaped is None else type(escaped).__name__,
                "observations": observations,
                "status": getattr(update, "status", None),
                "payload_prior_sha256": (
                    update_payload.get("prior_pointer_sha256")
                    if isinstance(update_payload, dict)
                    else None
                ),
                "final_status": final_payload.get("status"),
                "crashed_transaction_aborted": "aborted"
                in after_phases.get(crashed_txid, set()),
                "one_positive_transaction": len(new_txids) == 1,
                "positive_prior_sha256": (
                    _prepared_prior_sha256(final_dir, new_txid)
                    if new_txid is not None
                    else None
                ),
                "recovered_prior_retained": prior_bytes in _retained_bytes(final_dir),
            }
            self.assertEqual(
                observed,
                {
                    "interrupted_pointer_was_distinct": True,
                    "escaped": None,
                    "observations": [
                        ("hook", prior_bytes),
                        ("payload_factory", prior_bytes),
                    ],
                    "status": "invalidated",
                    "payload_prior_sha256": hashlib.sha256(prior_bytes).hexdigest(),
                    "final_status": "invalidated",
                    "crashed_transaction_aborted": True,
                    "one_positive_transaction": True,
                    "positive_prior_sha256": hashlib.sha256(prior_bytes).hexdigest(),
                    "recovered_prior_retained": True,
                },
            )

    def test_step2e_expected_prior_preflight_rejects_rescan_race(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "run"
            pointer_path = root / "final" / "video_delivery.json"
            pointer_path.parent.mkdir(parents=True)
            classified_bytes = b'{"marker":"classified-prior"}\n'
            concurrent_bytes = b'{"marker":"concurrent-winner"}\n'
            pointer_path.write_bytes(classified_bytes)
            retained_classified = pointer_path.with_name(
                "video_delivery.classified-retained.json"
            )
            final_dir = pointer_path.parent
            before_roles, before_ids = _video_transaction_inventory(final_dir)
            before_phases = _video_phase_inventory(final_dir)
            real_transaction_entry = (
                attempt_candidates_module.stage_video_delivery_pointer
            )
            events: list[str] = []
            injected = False

            def adapter_hook(_event: str, **_details):
                events.append("classified")

            def race_before_transaction(*args, **kwargs):
                nonlocal injected
                events.append("transaction-entry")
                if not injected:
                    pointer_path.rename(retained_classified)
                    pointer_path.write_bytes(concurrent_bytes)
                    injected = True
                return real_transaction_entry(*args, **kwargs)

            with (
                mock.patch.object(
                    attempt_candidates_module,
                    "_video_delivery_pointer_adapter_hook",
                    side_effect=adapter_hook,
                ),
                mock.patch.object(
                    attempt_candidates_module,
                    "stage_video_delivery_pointer",
                    side_effect=race_before_transaction,
                ),
            ):
                _update, escaped = _call_without_escape(
                    attempt_candidates_module.update_video_delivery_pointer,
                    root,
                    mode="publish",
                    payload={"marker": "must-not-publish"},
                )

            after_roles, after_ids = _video_transaction_inventory(final_dir)
            after_phases = _video_phase_inventory(final_dir)
            error_text = str(escaped or "").lower()
            self.assertEqual(
                {
                    "injected": injected,
                    "events": events,
                    "raised_mismatch": escaped is not None
                    and "changed before transaction publication" in error_text,
                    "classified_bytes_retained": retained_classified.read_bytes(),
                    "concurrent_bytes_unchanged": pointer_path.read_bytes(),
                    "transaction_ids": after_ids,
                    "roles": after_roles,
                    "phases": after_phases,
                    "new_file_absent": not any(
                        path.name.endswith(".new") for path in final_dir.iterdir()
                    ),
                },
                {
                    "injected": True,
                    "events": ["classified", "transaction-entry"],
                    "raised_mismatch": True,
                    "classified_bytes_retained": classified_bytes,
                    "concurrent_bytes_unchanged": concurrent_bytes,
                    "transaction_ids": before_ids,
                    "roles": before_roles,
                    "phases": before_phases,
                    "new_file_absent": True,
                },
            )

    def test_step2e_recovery_warning_survives_no_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "run"
            tombstone = {
                "status": "invalidated",
                "invalidation_id": "step2e-existing-invalidation",
                "reason": "already-invalidated",
                "prior_pointer_sha256": "a" * 64,
                "prior_manifest_sha256": None,
                "prior_design_spec_sha256": None,
                "prior_design_spec_revision": None,
                "prior_render_started_at": None,
                "invalidated_at": "2026-08-05T00:00:00+00:00",
            }
            crashed = _crash_pointer_publication(
                root,
                prior_bytes=None,
                payload=tombstone,
                crash_phase="committed",
            )
            self.assertEqual(
                crashed.returncode,
                91,
                {"stdout": crashed.stdout, "stderr": crashed.stderr},
            )
            final_dir = root / "final"
            pointer_path = final_dir / "video_delivery.json"
            committed_bytes = pointer_path.read_bytes()
            before_phases = _video_phase_inventory(final_dir)
            self.assertEqual(len(before_phases), 1)
            committed_txid = next(iter(before_phases))
            warning_marker = "step2e no-publication recovery warning"
            warning_injected = False

            def inject_recovery_warning(phase: str, **_details):
                nonlocal warning_injected
                if phase == "recovery-committed-confirmed":
                    warning_injected = True
                    raise OSError(warning_marker)

            with mock.patch.object(
                pointer_transaction_module,
                "_video_pointer_transaction_phase_hook",
                side_effect=inject_recovery_warning,
            ):
                update, escaped = _call_without_escape(
                    attempt_candidates_module.update_video_delivery_pointer,
                    root,
                    mode="invalidate_if_present",
                    reason="step2e-no-publication",
                )

            cleanup_warnings = getattr(update, "cleanup_warnings", None)
            after_phases = _video_phase_inventory(final_dir)
            self.assertEqual(
                {
                    "escaped": None if escaped is None else type(escaped).__name__,
                    "warning_injected": warning_injected,
                    "status": getattr(update, "status", None),
                    "payload": getattr(update, "payload", None),
                    "warning_container": type(cleanup_warnings).__name__,
                    "warning_visible": warning_marker
                    in " | ".join(cleanup_warnings or ()),
                    "pointer_unchanged": pointer_path.read_bytes() == committed_bytes,
                    "transaction_ids_unchanged": set(after_phases)
                    == set(before_phases),
                    "recovery_confirmed": "recovery-committed-confirmed"
                    in after_phases.get(committed_txid, set()),
                },
                {
                    "escaped": None,
                    "warning_injected": True,
                    "status": "already_invalidated",
                    "payload": tombstone,
                    "warning_container": "tuple",
                    "warning_visible": True,
                    "pointer_unchanged": True,
                    "transaction_ids_unchanged": True,
                    "recovery_confirmed": True,
                },
            )

    def test_step2e_expected_prior_mismatch_aggregates_recovery_warning(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "run"
            crashed = _crash_pointer_publication(
                root,
                prior_bytes=None,
                payload={"marker": "committed-prior"},
                crash_phase="committed",
            )
            self.assertEqual(
                crashed.returncode,
                91,
                {"stdout": crashed.stdout, "stderr": crashed.stderr},
            )
            final_dir = root / "final"
            pointer_path = final_dir / "video_delivery.json"
            committed_bytes = pointer_path.read_bytes()
            retained_committed = pointer_path.with_name(
                "video_delivery.committed-retained.json"
            )
            concurrent_bytes = b'{"marker":"warning-race-winner"}\n'
            before_roles, before_ids = _video_transaction_inventory(final_dir)
            before_phases = _video_phase_inventory(final_dir)
            real_transaction_entry = (
                attempt_candidates_module.stage_video_delivery_pointer
            )
            warning_marker = "step2e mismatch recovery warning"
            events: list[str] = []
            injected = False

            def inject_recovery_warning(phase: str, **_details):
                if phase == "recovery-committed-confirmed":
                    events.append("recovery-warning")
                    raise OSError(warning_marker)

            def race_before_transaction(*args, **kwargs):
                nonlocal injected
                events.append("transaction-entry")
                if not injected:
                    pointer_path.rename(retained_committed)
                    pointer_path.write_bytes(concurrent_bytes)
                    injected = True
                return real_transaction_entry(*args, **kwargs)

            with (
                mock.patch.object(
                    pointer_transaction_module,
                    "_video_pointer_transaction_phase_hook",
                    side_effect=inject_recovery_warning,
                ),
                mock.patch.object(
                    attempt_candidates_module,
                    "stage_video_delivery_pointer",
                    side_effect=race_before_transaction,
                ),
            ):
                _update, escaped = _call_without_escape(
                    attempt_candidates_module.update_video_delivery_pointer,
                    root,
                    mode="publish",
                    payload={"marker": "must-not-publish"},
                )

            after_roles, after_ids = _video_transaction_inventory(final_dir)
            after_phases = _video_phase_inventory(final_dir)
            new_txids = set(after_phases) - set(before_phases)
            new_phase_tokens = {
                phase
                for txid in new_txids
                for phase in after_phases.get(txid, set())
            }
            error_text = str(escaped or "").lower()
            self.assertEqual(
                {
                    "injected": injected,
                    "events": events,
                    "raised": escaped is not None,
                    "mismatch_visible": "changed before transaction publication"
                    in error_text,
                    "warning_visible": warning_marker in error_text,
                    "committed_bytes_retained": retained_committed.read_bytes(),
                    "concurrent_bytes_unchanged": pointer_path.read_bytes(),
                    "transaction_ids": after_ids,
                    "roles": after_roles,
                    "new_prepublication_phases": new_phase_tokens
                    & {"prepared", "publish-intent", "published"},
                    "new_file_absent": not any(
                        path.name.endswith(".new") for path in final_dir.iterdir()
                    ),
                },
                {
                    "injected": True,
                    "events": ["recovery-warning", "transaction-entry"],
                    "raised": True,
                    "mismatch_visible": True,
                    "warning_visible": True,
                    "committed_bytes_retained": committed_bytes,
                    "concurrent_bytes_unchanged": concurrent_bytes,
                    "transaction_ids": before_ids,
                    "roles": before_roles,
                    "new_prepublication_phases": set(),
                    "new_file_absent": True,
                },
            )

    def test_committed_tombstone_blocks_stale_state_after_crash(self) -> None:
        video_module = VIDEO_MODULE

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "run"
            ctx = _finalize_context(root)
            manifest_path, _mp4_path = _install_passed_delivery(ctx)
            self.assertEqual(finalize({}, ctx=ctx).status, "ok")
            pointer_path = root / "final" / "video_delivery.json"
            prior_manifest_sha = sha256_file(manifest_path)
            ctx.state = _CrashBeforeVideoStateClear(ctx.state)

            _result, crash = _call_without_escape(
                video_module._clear_stale_video_delivery,
                manifest_path.parent,
                ctx,
            )
            pointer_before_finalize = (
                json.loads(pointer_path.read_text(encoding="utf-8"))
                if pointer_path.is_file()
                else {}
            )
            finalize_result, finalize_escape = _call_without_escape(
                finalize,
                {},
                ctx=ctx,
            )
            pointer_after_finalize = (
                pointer_path.read_bytes() if pointer_path.is_file() else b""
            )

            observed = {
                "crashed_at_state_clear": isinstance(crash, SystemExit),
                "stale_state_survived": ctx.state.get("video_delivery", {}).get("status")
                == "passed",
                "tombstone_status": pointer_before_finalize.get("status"),
                "tombstone_manifest": pointer_before_finalize.get(
                    "prior_manifest_sha256"
                ),
                "finalize_escaped": finalize_escape is not None,
                "finalize_status": getattr(finalize_result, "status", None),
                "pointer_not_resurrected": pointer_after_finalize
                == json.dumps(pointer_before_finalize, ensure_ascii=False, indent=2).encode(
                    "utf-8"
                ),
            }
            self.assertEqual(
                observed,
                {
                    "crashed_at_state_clear": True,
                    "stale_state_survived": True,
                    "tombstone_status": "invalidated",
                    "tombstone_manifest": prior_manifest_sha,
                    "finalize_escaped": False,
                    "finalize_status": "error",
                    "pointer_not_resurrected": True,
                },
            )

    def test_retry_invalidation_failure_stops_cleanup_and_authoring(self) -> None:
        video_module = VIDEO_MODULE

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "run"
            project = _make_retry_project(root)
            pointer_path, prior_pointer, manifest_path, mp4_path = (
                _install_prior_retry_pointer(root, project_dir=project)
            )
            narration_path = project / "assets" / "narration.wav"
            probe_path = project / "media_probe.json"
            narration_path.write_bytes(b"prior narration")
            probe_path.write_bytes(b"prior media probe")
            prior_manifest = manifest_path.read_bytes()
            prior_mp4 = mp4_path.read_bytes()
            injected = False
            authoring_calls = 0

            def adapter_hook(event: str, **_details):
                nonlocal injected
                if event == "classified_present" and not injected:
                    retained = pointer_path.with_name("video_delivery.retry-race")
                    pointer_path.rename(retained)
                    pointer_path.symlink_to(retained.name)
                    injected = True

            def authoring_lint(*_args, **_kwargs):
                nonlocal authoring_calls
                authoring_calls += 1
                return "authoring should not start", False

            with (
                mock.patch.object(
                    attempt_candidates_module,
                    "_video_delivery_pointer_adapter_hook",
                    side_effect=adapter_hook,
                    create=True,
                ),
                mock.patch.object(
                    video_module,
                    "_run_hyperframes_authoring_lint",
                    side_effect=authoring_lint,
                ),
            ):
                result, escaped = _call_without_escape(
                    video_module.retry_video_export_project,
                    root,
                    project,
                    cancellation_token=CancellationToken.never("retry-race"),
                )

            self.assertEqual(
                {
                    "hook_injected": injected,
                    "escaped": None if escaped is None else type(escaped).__name__,
                    "result_ok": (result or {}).get("ok"),
                    "result_phase": (result or {}).get("phase"),
                    "authoring_calls": authoring_calls,
                    "pointer_bytes_retained": prior_pointer
                    in _retained_bytes(root / "final"),
                    "manifest_retained": manifest_path.is_file()
                    and manifest_path.read_bytes() == prior_manifest,
                    "mp4_retained": mp4_path.is_file()
                    and mp4_path.read_bytes() == prior_mp4,
                    "narration_retained": narration_path.is_file()
                    and narration_path.read_bytes() == b"prior narration",
                    "probe_retained": probe_path.is_file()
                    and probe_path.read_bytes() == b"prior media probe",
                },
                {
                    "hook_injected": True,
                    "escaped": None,
                    "result_ok": False,
                    "result_phase": "final_pointer",
                    "authoring_calls": 0,
                    "pointer_bytes_retained": True,
                    "manifest_retained": True,
                    "mp4_retained": True,
                    "narration_retained": True,
                    "probe_retained": True,
                },
            )

    def test_cancel_after_invalidation_commit_stops_before_media_cleanup(self) -> None:
        video_module = VIDEO_MODULE

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "run"
            ctx = _export_context(root)
            _install_passed_delivery(ctx)
            self.assertEqual(finalize({}, ctx=ctx).status, "ok")
            token = _CancelAfterPointerCommit(ctx.run_id)
            ctx.cancellation_token = token
            project = root / "hyperframes-cancelled"
            (project / "renders").mkdir(parents=True)
            manifest_sentinel = project / "delivery_manifest.json"
            media_sentinel = project / "renders" / "prior.mp4"
            manifest_sentinel.write_bytes(b"prior project manifest")
            media_sentinel.write_bytes(b"prior project media")
            invalidation_committed = False

            def transaction_hook(phase: str, **_details):
                nonlocal invalidation_committed
                if phase == "committed" and not invalidation_committed:
                    invalidation_committed = True
                    token.cancelled = True

            with mock.patch.object(
                pointer_transaction_module,
                "_video_pointer_transaction_phase_hook",
                side_effect=transaction_hook,
            ):
                result, escaped = _call_without_escape(
                    video_module.export_video,
                    {"video_id": "cancelled"},
                    ctx=ctx,
                )

            pointer_path = root / "final" / "video_delivery.json"
            try:
                pointer_payload = json.loads(pointer_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pointer_payload = {}
            self.assertEqual(
                {
                    "invalidation_committed": invalidation_committed,
                    "cancelled": isinstance(escaped, RunCancelled),
                    "result_absent": result is None,
                    "tombstone_status": pointer_payload.get("status"),
                    "stale_state_cleared": "video_delivery" not in ctx.state
                    and not ctx.state.get("finalized", False),
                    "manifest_retained": manifest_sentinel.is_file()
                    and manifest_sentinel.read_bytes() == b"prior project manifest",
                    "media_retained": media_sentinel.is_file()
                    and media_sentinel.read_bytes() == b"prior project media",
                    "authoring_not_started": not (project / "meta.json").exists(),
                },
                {
                    "invalidation_committed": True,
                    "cancelled": True,
                    "result_absent": True,
                    "tombstone_status": "invalidated",
                    "stale_state_cleared": True,
                    "manifest_retained": True,
                    "media_retained": True,
                    "authoring_not_started": True,
                },
            )

    def test_retry_transaction_winner_reentry_and_cancellation_linearize(self) -> None:
        video_module = VIDEO_MODULE

        for case in (
            "destination_winner",
            "reentry",
            "cancel_before",
            "cancel_after_commit",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_tmp:
                runs_dir = Path(raw_tmp) / "runs"
                parent_dir = runs_dir / "parent"
                project = _make_retry_project(parent_dir)
                token: object = CancellationToken.never("retry")
                prior_bytes = b""
                transaction_started = False
                retry_hook_toggled = False
                if case == "reentry":
                    _pointer, prior_bytes, _manifest, _mp4 = _install_prior_retry_pointer(
                        parent_dir
                    )
                if case == "cancel_before":
                    token = _CancelAfterPointerCommit("retry")
                if case == "cancel_after_commit":
                    token = _CancelAfterPointerCommit("retry")
                winner = b'{"destination":"winner"}\n'
                injected = False

                def transaction_hook(phase: str, **_details):
                    nonlocal injected, transaction_started
                    transaction_started = True
                    if phase == "prepared" and case == "destination_winner" and not injected:
                        pointer = parent_dir / "final" / "video_delivery.json"
                        pointer.write_bytes(winner)
                        injected = True
                    if phase == "committed" and case == "cancel_after_commit":
                        token.cancelled = True

                def retry_phase_hook(phase: str, **_details):
                    nonlocal retry_hook_toggled
                    if (
                        case == "cancel_before"
                        and phase == "manifest_durable_before_pointer"
                    ):
                        retry_hook_toggled = True
                        token.cancelled = True

                with (
                    _patched_expensive_video_seams(video_module),
                    mock.patch.object(
                        pointer_transaction_module,
                        "_video_pointer_transaction_phase_hook",
                        side_effect=transaction_hook,
                    ),
                    mock.patch.object(
                        video_module,
                        "_video_export_retry_phase_hook",
                        side_effect=retry_phase_hook,
                        create=True,
                    ),
                ):
                    if case == "cancel_after_commit":
                        request = VideoExportRetryWorkerRequest(
                            job_kind="video_export_retry",
                            run_id="cancel-after-child",
                            parent_run_id="parent",
                            source_project=str(project),
                            conversation_id="conversation",
                            baseline_artifact_json="{}",
                            runs_dir=str(runs_dir),
                        )
                        result, escaped = _call_without_escape(
                            run_worker_module._run_video_export_retry,
                            request,
                            token,
                        )
                        active_run_dir = runs_dir / "cancel-after-child"
                    else:
                        result, escaped = _call_without_escape(
                            video_module.retry_video_export_project,
                            parent_dir,
                            project,
                            cancellation_token=token,
                        )
                        active_run_dir = parent_dir

                pointer_path = active_run_dir / "final" / "video_delivery.json"
                if case == "destination_winner":
                    observed = {
                        "injected": injected,
                        "phase": (result or {}).get("phase"),
                        "ok": (result or {}).get("ok"),
                        "escaped": None if escaped is None else type(escaped).__name__,
                        "winner_retained": winner in _retained_bytes(parent_dir / "final"),
                    }
                    expected = {
                        "injected": True,
                        "phase": "final_pointer",
                        "ok": False,
                        "escaped": None,
                        "winner_retained": True,
                    }
                elif case == "reentry":
                    observed = {
                        "phase": (result or {}).get("phase"),
                        "ok": (result or {}).get("ok"),
                        "prior_retained": prior_bytes in _retained_bytes(
                            parent_dir / "final"
                        ),
                        "published": pointer_path.is_file(),
                    }
                    expected = {
                        "phase": "done",
                        "ok": True,
                        "prior_retained": True,
                        "published": True,
                    }
                elif case == "cancel_before":
                    observed = {
                        "hook_toggled_after_manifest": retry_hook_toggled,
                        "cancelled": isinstance(escaped, RunCancelled),
                        "pointer_absent": not pointer_path.exists(),
                        "transaction_not_started": not transaction_started,
                        "manifest_is_durable": (
                            project / "delivery_manifest.json"
                        ).is_file(),
                    }
                    expected = {
                        "hook_toggled_after_manifest": True,
                        "cancelled": True,
                        "pointer_absent": True,
                        "transaction_not_started": True,
                        "manifest_is_durable": True,
                    }
                else:
                    observed = {
                        "cancelled": isinstance(escaped, RunCancelled),
                        "pointer_retained": pointer_path.is_file(),
                        "pointer_is_passed": (
                            json.loads(pointer_path.read_text(encoding="utf-8")).get(
                                "status"
                            )
                            != "invalidated"
                            if pointer_path.is_file()
                            else False
                        ),
                    }
                    expected = {
                        "cancelled": True,
                        "pointer_retained": True,
                        "pointer_is_passed": True,
                    }
                self.assertEqual(observed, expected)

    def test_task4b_retry_manifest_hook_rejects_stale_snapshot(self) -> None:
        video_module = VIDEO_MODULE

        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp) / "run"
            project = _make_retry_project(run_dir)
            manifest_mutated = False

            def mutate_manifest(phase: str, **details):
                nonlocal manifest_mutated
                if phase != "manifest_durable_before_pointer":
                    return
                manifest_path = Path(details["manifest_path"])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["task4b_snapshot_mutation"] = "benign valid JSON"
                manifest_path.write_text(
                    json.dumps(manifest, indent=2) + "\n",
                    encoding="utf-8",
                )
                manifest_mutated = True

            with (
                _patched_expensive_video_seams(video_module),
                mock.patch.object(
                    video_module,
                    "_video_export_retry_phase_hook",
                    side_effect=mutate_manifest,
                    create=True,
                ),
            ):
                result, escaped = _call_without_escape(
                    video_module.retry_video_export_project,
                    run_dir,
                    project,
                    cancellation_token=CancellationToken.never("task4b-retry"),
                )

            persisted_manifest = json.loads(
                (project / "delivery_manifest.json").read_text(encoding="utf-8")
            )
            error_text = str((result or {}).get("error") or "").lower()
            snapshot_diagnostic = (
                "delivery_manifest.json" in error_text
                and "snapshot" in error_text
                and any(
                    token in error_text
                    for token in ("changed", "mismatch", "does not match")
                )
            )
            self.assertEqual(
                {
                    "mutated": manifest_mutated,
                    "valid_json_mutation": persisted_manifest.get(
                        "task4b_snapshot_mutation"
                    )
                    == "benign valid JSON",
                    "escaped": None if escaped is None else type(escaped).__name__,
                    "ok": (result or {}).get("ok"),
                    "phase": (result or {}).get("phase"),
                    "snapshot_diagnostic": snapshot_diagnostic,
                    "pointer_absent": not (
                        run_dir / "final" / "video_delivery.json"
                    ).exists(),
                },
                {
                    "mutated": True,
                    "valid_json_mutation": True,
                    "escaped": None,
                    "ok": False,
                    "phase": "final_pointer",
                    "snapshot_diagnostic": True,
                    "pointer_absent": True,
                },
            )

    def test_task4b_retry_rejects_manifest_declared_member_hash_mismatch(
        self,
    ) -> None:
        video_module = VIDEO_MODULE

        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp) / "run"
            project = _make_retry_project(run_dir)
            real_atomic_write_json = video_module.atomic_write_json
            member_mutated = False

            def write_manifest_then_mutate(path, payload):
                nonlocal member_mutated
                real_atomic_write_json(path, payload)
                if Path(path).resolve() != (
                    project / "delivery_manifest.json"
                ).resolve():
                    return
                mp4_path = project / str(payload["mp4_path"])
                mp4_path.write_bytes(mp4_path.read_bytes() + b" mutated")
                member_mutated = True

            with (
                _patched_expensive_video_seams(
                    video_module,
                    captioned_name="attempt.mp4",
                ),
                mock.patch.object(
                    video_module,
                    "atomic_write_json",
                    side_effect=write_manifest_then_mutate,
                ),
            ):
                result, escaped = _call_without_escape(
                    video_module.retry_video_export_project,
                    run_dir,
                    project,
                    cancellation_token=CancellationToken.never("task4b-retry"),
                )

            error_text = str((result or {}).get("error") or "").lower()
            declared_hash_diagnostic = (
                "attempt.mp4" in error_text
                and any(
                    token in error_text
                    for token in ("manifest", "declared", "mp4_sha256")
                )
                and any(token in error_text for token in ("hash", "sha256"))
                and any(
                    token in error_text
                    for token in ("mismatch", "does not match", "differs")
                )
            )
            self.assertEqual(
                {
                    "mutated": member_mutated,
                    "escaped": None if escaped is None else type(escaped).__name__,
                    "ok": (result or {}).get("ok"),
                    "phase": (result or {}).get("phase"),
                    "declared_hash_diagnostic": declared_hash_diagnostic,
                    "pointer_absent": not (
                        run_dir / "final" / "video_delivery.json"
                    ).exists(),
                },
                {
                    "mutated": True,
                    "escaped": None,
                    "ok": False,
                    "phase": "final_pointer",
                    "declared_hash_diagnostic": True,
                    "pointer_absent": True,
                },
            )

    def test_task4b_retry_precommit_mutation_restores_exact_prior(self) -> None:
        video_module = VIDEO_MODULE

        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp) / "run"
            project = _make_retry_project(run_dir)
            pointer_path = run_dir / "final" / "video_delivery.json"
            sentinel = b'{"sentinel":"Task4B retry prior"}\n'
            mp4_path: Path | None = None
            phases: list[str] = []
            mutated = False

            def install_prior(phase: str, **details):
                nonlocal mp4_path
                if phase != "manifest_durable_before_pointer":
                    return
                manifest = json.loads(
                    Path(details["manifest_path"]).read_text(encoding="utf-8")
                )
                mp4_path = project / str(manifest["mp4_path"])
                pointer_path.parent.mkdir(parents=True, exist_ok=True)
                pointer_path.write_bytes(sentinel)

            def mutate_published_mp4(phase: str, **_details):
                nonlocal mutated
                token = str(getattr(phase, "value", phase)).lower().replace(
                    "_", "-"
                )
                phases.append(token)
                if token == "published" and not mutated:
                    assert mp4_path is not None
                    mp4_path.write_bytes(mp4_path.read_bytes() + b" mutated")
                    mutated = True

            with (
                _patched_expensive_video_seams(
                    video_module,
                    captioned_name="attempt.mp4",
                ),
                mock.patch.object(
                    video_module,
                    "_video_export_retry_phase_hook",
                    side_effect=install_prior,
                    create=True,
                ),
                mock.patch.object(
                    pointer_transaction_module,
                    "_video_pointer_transaction_phase_hook",
                    side_effect=mutate_published_mp4,
                ),
            ):
                result, escaped = _call_without_escape(
                    video_module.retry_video_export_project,
                    run_dir,
                    project,
                    cancellation_token=CancellationToken.never("task4b-retry"),
                )

            durable_phases = set().union(
                *_video_phase_inventory(pointer_path.parent).values()
            )
            all_phases = set(phases) | durable_phases
            error_text = str((result or {}).get("error") or "").lower()
            precommit_snapshot_diagnostic = (
                "attempt.mp4" in error_text
                and "snapshot" in error_text
                and any(
                    token in error_text
                    for token in ("changed", "mismatch", "does not match")
                )
                and any(
                    token in error_text
                    for token in (
                        "precommit",
                        "pre-commit",
                        "before commit",
                        "before durable",
                        "commit precondition",
                    )
                )
            )
            self.assertEqual(
                {
                    "mutated": mutated,
                    "escaped": None if escaped is None else type(escaped).__name__,
                    "ok": (result or {}).get("ok"),
                    "phase": (result or {}).get("phase"),
                    "prior_restored": pointer_path.is_file()
                    and pointer_path.read_bytes() == sentinel,
                    "published_seen": "published" in all_phases,
                    "committed_absent": "committed" not in all_phases,
                    "aborted_seen": "aborted" in all_phases,
                    "precommit_snapshot_diagnostic": (
                        precommit_snapshot_diagnostic
                    ),
                },
                {
                    "mutated": True,
                    "escaped": None,
                    "ok": False,
                    "phase": "final_pointer",
                    "prior_restored": True,
                    "published_seen": True,
                    "committed_absent": True,
                    "aborted_seen": True,
                    "precommit_snapshot_diagnostic": True,
                },
            )

    def test_task4b_retry_success_pointer_matches_complete_manifest_graph(
        self,
    ) -> None:
        video_module = VIDEO_MODULE

        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp) / "run"
            project = _make_retry_project(run_dir)
            with _patched_expensive_video_seams(video_module):
                result = video_module.retry_video_export_project(
                    run_dir,
                    project,
                    cancellation_token=CancellationToken.never("task4b-control"),
                )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["phase"], "done")
            manifest_path = Path(result["manifest_path"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pointer = json.loads(
                (run_dir / "final" / "video_delivery.json").read_text(
                    encoding="utf-8"
                )
            )
            spec_snapshot = json.loads(
                (run_dir / "design_spec.json").read_text(encoding="utf-8")
            )
            member_contract = {
                "source_html_path": "source_html_sha256",
                "contract_path": "contract_sha256",
                "media_probe_path": "media_probe_sha256",
                "mp4_path": "mp4_sha256",
                "narration_audio_path": "narration_audio_sha256",
                "transcript_path": "transcript_sha256",
                "srt_path": "srt_sha256",
                "vtt_path": "vtt_sha256",
                "voice_metadata_path": "voice_metadata_sha256",
                "narration_timing_path": "narration_timing_sha256",
            }
            member_hashes_match = all(
                sha256_file(project / str(manifest[path_key]))
                == manifest[hash_key]
                for path_key, hash_key in member_contract.items()
            )
            local_hashes_match = all(
                sha256_file(project / relative_path) == expected_hash
                for relative_path, expected_hash in manifest[
                    "local_asset_sha256"
                ].items()
            )
            unique_members = {
                str(manifest[path_key]) for path_key in member_contract
            } | set(manifest["local_asset_sha256"])
            self.assertEqual(
                {
                    "pointer_manifest_path": pointer.get("manifest_path"),
                    "pointer_manifest_sha256": pointer.get("manifest_sha256"),
                    "pointer_spec_sha256": pointer.get("design_spec_sha256"),
                    "pointer_spec_revision": pointer.get("design_spec_revision"),
                    "member_hashes_match": member_hashes_match,
                    "local_hashes_match": local_hashes_match,
                    "unique_project_member_count": len(unique_members),
                },
                {
                    "pointer_manifest_path": manifest_path.relative_to(
                        run_dir.resolve()
                    ).as_posix(),
                    "pointer_manifest_sha256": sha256_file(manifest_path),
                    "pointer_spec_sha256": spec_snapshot["design_spec_sha256"],
                    "pointer_spec_revision": spec_snapshot["revision"],
                    "member_hashes_match": True,
                    "local_hashes_match": True,
                    "unique_project_member_count": 10,
                },
            )

    def test_web_and_resume_use_secure_typed_current_delivery_validation(self) -> None:
        cases = ("valid", "tombstone", "link", "stale", "integrity", "malformed")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp) / "video-run"
                _passed_delivery(root)
                _write_resume_metadata(root)
                if case != "valid":
                    _mutate_delivery_case(root, case)

                validation = web_server._validated_video_delivery(root)
                resume = runner_module._load_resume_state(root)
                typed = _typed_validation_observation(validation, root=root)
                observed = {
                    "typed_leaf": typed["typed_leaf"],
                    "leaf_boundary_violations": typed[
                        "leaf_boundary_violations"
                    ],
                    "reason_code": typed["reason_code"],
                    "resume_classification": (
                        "non_final" if isinstance(resume, dict) else "final"
                    ),
                }
                expected_reason = {
                    "valid": "passed",
                    "tombstone": "pointer_invalidated",
                    "link": "pointer_unsafe_link",
                    "stale": "stale_design_spec",
                    "integrity": "integrity_mismatch",
                    "malformed": "pointer_malformed",
                }[case]
                if case == "valid":
                    observed.update(
                        {
                            "run_relative_paths": typed["run_relative_paths"],
                            "snapshot_roles": typed["snapshot_roles"],
                            "snapshot_digests_valid": typed[
                                "snapshot_digests_valid"
                            ],
                            "snapshot_digests_match_files": typed[
                                "snapshot_digests_match_files"
                            ],
                        }
                    )
                self.assertEqual(
                    observed,
                    (
                        {
                            "typed_leaf": True,
                            "leaf_boundary_violations": [],
                            "reason_code": "passed",
                            "resume_classification": "final",
                            "run_relative_paths": True,
                            "snapshot_roles": ["manifest", "mp4", "pointer"],
                            "snapshot_digests_valid": True,
                            "snapshot_digests_match_files": True,
                        }
                        if case == "valid"
                        else {
                            "typed_leaf": True,
                            "leaf_boundary_violations": [],
                            "reason_code": expected_reason,
                            "resume_classification": "non_final",
                        }
                    ),
                )

    def test_web_and_resume_route_through_same_leaf_validator_callable(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "video-run"
            _passed_delivery(root)
            _write_resume_metadata(root)

            core_module, core_import_error = _call_without_escape(
                importlib.import_module,
                CORE_VIDEO_VALIDATOR_MODULE,
            )
            core_validator = (
                getattr(core_module, CORE_VIDEO_VALIDATOR_NAME, None)
                if isinstance(core_module, ModuleType)
                else None
            )
            web_bound_validator = getattr(
                web_server,
                CONSUMER_VIDEO_VALIDATOR_ALIAS,
                None,
            )
            runner_bound_validator = getattr(
                runner_module,
                CONSUMER_VIDEO_VALIDATOR_ALIAS,
                None,
            )

            if callable(core_validator):
                direct_result, direct_error = _call_without_escape(
                    core_validator,
                    root,
                )
                shared_spy = mock.Mock(wraps=core_validator)
            else:
                direct_result = None
                direct_error = None
                shared_spy = mock.Mock(return_value=None)
            direct_observation = _typed_validation_observation(
                direct_result,
                root=root,
            )

            with (
                mock.patch.object(
                    web_server,
                    CONSUMER_VIDEO_VALIDATOR_ALIAS,
                    new=shared_spy,
                    create=True,
                ),
                mock.patch.object(
                    runner_module,
                    CONSUMER_VIDEO_VALIDATOR_ALIAS,
                    new=shared_spy,
                    create=True,
                ),
            ):
                before_web = shared_spy.call_count
                web_result, web_error = _call_without_escape(
                    web_server._validated_video_delivery,
                    root,
                )
                web_spy_calls = shared_spy.call_count - before_web
                before_resume = shared_spy.call_count
                resume_result, resume_error = _call_without_escape(
                    runner_module._load_resume_state,
                    root,
                )
                resume_spy_calls = shared_spy.call_count - before_resume

            web_observation = _typed_validation_observation(
                web_result,
                root=root,
            )
            self.assertEqual(
                {
                    "core_import_error": None
                    if core_import_error is None
                    else type(core_import_error).__name__,
                    "core_callable_found": callable(core_validator),
                    "core_defining_module": getattr(
                        core_validator,
                        "__module__",
                        None,
                    ),
                    "core_leaf_boundary_violations": (
                        _callable_leaf_boundary_violations(core_validator)
                    ),
                    "consumer_aliases_are_exact_core": (
                        callable(core_validator)
                        and web_bound_validator is core_validator
                        and runner_bound_validator is core_validator
                    ),
                    "direct_error": None
                    if direct_error is None
                    else type(direct_error).__name__,
                    "direct_reason": direct_observation["reason_code"],
                    "direct_snapshots_match": direct_observation[
                        "snapshot_digests_match_files"
                    ],
                    "web_error": None
                    if web_error is None
                    else type(web_error).__name__,
                    "web_spy_calls": web_spy_calls,
                    "web_reason": web_observation["reason_code"],
                    "resume_error": None
                    if resume_error is None
                    else type(resume_error).__name__,
                    "resume_spy_calls": resume_spy_calls,
                    "resume_classification": (
                        "non_final"
                        if isinstance(resume_result, dict)
                        else "final"
                    ),
                },
                {
                    "core_import_error": None,
                    "core_callable_found": True,
                    "core_defining_module": CORE_VIDEO_VALIDATOR_MODULE,
                    "core_leaf_boundary_violations": [],
                    "consumer_aliases_are_exact_core": True,
                    "direct_error": None,
                    "direct_reason": "passed",
                    "direct_snapshots_match": True,
                    "web_error": None,
                    "web_spy_calls": 1,
                    "web_reason": "passed",
                    "resume_error": None,
                    "resume_spy_calls": 1,
                    "resume_classification": "final",
                },
            )

    def test_artifact_builder_rejects_substitution_after_validation(self) -> None:
        for target_role in ("pointer", "manifest", "mp4"):
            with (
                self.subTest(target_role=target_role),
                tempfile.TemporaryDirectory() as raw_tmp,
            ):
                root = Path(raw_tmp) / "video-run"
                manifest_path, mp4_path = _passed_delivery(root)
                pointer_path = root / "final" / "video_delivery.json"
                targets = {
                    "pointer": pointer_path,
                    "manifest": manifest_path,
                    "mp4": mp4_path,
                }
                target_path = targets[target_role]
                validated_bytes = target_path.read_bytes()
                retained_path = target_path.with_name(
                    f"{target_path.name}.validated-retained"
                )
                foreign_bytes = f"foreign {target_role} after validation".encode()
                original_validator = web_server._validated_video_delivery
                validation_observation: dict[str, object] = {}
                substituted = False

                def validate_then_substitute(run_dir: Path):
                    nonlocal substituted
                    result = original_validator(run_dir)
                    if not substituted:
                        validation_observation.update(
                            _typed_validation_observation(result, root=root)
                        )
                        target_path.rename(retained_path)
                        target_path.write_bytes(foreign_bytes)
                        substituted = True
                    return result

                with mock.patch.object(
                    web_server,
                    "_validated_video_delivery",
                    side_effect=validate_then_substitute,
                ):
                    artifact, escaped = _call_without_escape(
                        web_server._build_video_artifact,
                        root,
                        "video-run",
                        baseline_artifact_json=None,
                    )

                self.assertEqual(
                    {
                        "escaped": None if escaped is None else type(escaped).__name__,
                        "validator_called_before_substitution": substituted,
                        "validated_snapshots_match_exact_files": (
                            validation_observation.get(
                                "snapshot_digests_match_files"
                            )
                            is True
                        ),
                        "artifact_rejected": artifact is None,
                        "validated_bytes_retained": (
                            retained_path.read_bytes()
                            if retained_path.is_file()
                            else None
                        ),
                        "foreign_bytes_not_advertised": (
                            target_path.read_bytes() if target_path.is_file() else None
                        ),
                    },
                    {
                        "escaped": None,
                        "validator_called_before_substitution": True,
                        "validated_snapshots_match_exact_files": True,
                        "artifact_rejected": True,
                        "validated_bytes_retained": validated_bytes,
                        "foreign_bytes_not_advertised": foreign_bytes,
                    },
                )

    def test_design_spec_failures_restore_exact_state_and_prior_delivery(self) -> None:
        for caller in ("propose", "apply"):
            for failure_point in ("archive", "canonical"):
                with (
                    self.subTest(caller=caller, failure_point=failure_point),
                    tempfile.TemporaryDirectory() as raw_tmp,
                ):
                    root = Path(raw_tmp) / "run"
                    ctx, pointer_path, _manifest_path = _design_context_with_delivery(root)
                    prior_state = _state_signature(ctx.state)
                    prior_canonical = (root / "design_spec.json").read_bytes()
                    prior_pointer = pointer_path.read_bytes()
                    real_replace = os.replace
                    if failure_point == "archive":
                        (root / "specs").write_bytes(b"archive parent is not a directory")

                    def injected_replace(source, destination, *args, **kwargs):
                        if (
                            failure_point == "canonical"
                            and _same_parent_bound_entry(
                                Path(destination),
                                root / "design_spec.json",
                                dst_dir_fd=kwargs.get("dst_dir_fd"),
                            )
                        ):
                            raise OSError("injected canonical persistence failure")
                        return real_replace(source, destination, *args, **kwargs)

                    with mock.patch.object(
                        os,
                        "replace",
                        side_effect=injected_replace,
                    ):
                        if caller == "propose":
                            result, escaped = _call_without_escape(
                                propose_design_spec,
                                {"design_spec": _video_spec("Revised by proposal")},
                                ctx=ctx,
                            )
                        else:
                            result, escaped = _call_without_escape(
                                apply_design_ops,
                                {
                                    "ops": [
                                        {
                                            "op": "html_replace_text",
                                            "finding_id": "test:grounding",
                                            "block_id": "headline",
                                            "text": "Revised by targeted operation",
                                        }
                                    ]
                                },
                                ctx=ctx,
                            )

                    archive_path = root / "specs" / "design_spec_02.json"
                    prior_validation = web_server._validated_video_delivery(root)
                    prior_validation_observation = _typed_validation_observation(
                        prior_validation,
                        root=root,
                    )
                    observed = {
                        "escaped": None if escaped is None else type(escaped).__name__,
                        "tool_status": getattr(result, "status", None),
                        "state_exact": _state_signature(ctx.state) == prior_state,
                        "canonical_exact": (root / "design_spec.json").read_bytes()
                        == prior_canonical,
                        "pointer_exact": pointer_path.is_file()
                        and pointer_path.read_bytes() == prior_pointer,
                        "prior_delivery_still_valid": (
                            prior_validation_observation["reason_code"] == "passed"
                            and prior_validation_observation[
                                "snapshot_digests_match_files"
                            ]
                        ),
                        "archive_present_after_canonical_failure": archive_path.is_file(),
                    }
                    self.assertEqual(
                        observed,
                        {
                            "escaped": None,
                            "tool_status": "error",
                            "state_exact": True,
                            "canonical_exact": True,
                            "pointer_exact": True,
                            "prior_delivery_still_valid": True,
                            "archive_present_after_canonical_failure": False,
                        },
                    )

    def test_design_spec_state_installs_only_after_canonical_write_returns(self) -> None:
        for caller in ("propose", "apply"):
            with self.subTest(caller=caller), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp) / "run"
                if caller == "propose":
                    ctx, _pointer_path, _manifest_path = (
                        _design_context_with_delivery(root)
                    )
                else:
                    ctx = _poster_design_context(root)
                prior_state = _state_signature(ctx.state)
                archive_path = root / "specs" / "design_spec_02.json"
                real_replace = os.replace
                canonical_observations: list[dict[str, bool]] = []
                render_calls: list[str] = []

                def rerender(render_args: dict[str, object], *, ctx: ToolContext):
                    layer_id = str(render_args["layer_id"])
                    version = ctx.next_layer_version(layer_id)
                    rendered_path = ctx.layers_dir / f"{layer_id}_v{version}.png"
                    rendered_path.write_bytes(b"successful proposed rendered layer")
                    ctx.state["rendered_layers"][layer_id] = {
                        "png_path": str(rendered_path),
                        "owner": "successful-proposed",
                    }
                    render_calls.append(layer_id)
                    return obs_ok({"layer_id": layer_id})

                def observe_canonical_replace(source, destination, *args, **kwargs):
                    candidate = Path(destination)
                    if _same_parent_bound_entry(
                        candidate,
                        root / "design_spec.json",
                        dst_dir_fd=kwargs.get("dst_dir_fd"),
                    ):
                        observation = {
                            "archive_already_durable": archive_path.is_file(),
                            "prior_state_before_write": _state_signature(ctx.state)
                            == prior_state,
                            "prior_state_after_write": False,
                        }
                        canonical_observations.append(observation)
                        result = real_replace(
                            source,
                            destination,
                            *args,
                            **kwargs,
                        )
                        observation["prior_state_after_write"] = (
                            _state_signature(ctx.state) == prior_state
                        )
                        return result
                    return real_replace(source, destination, *args, **kwargs)

                with ExitStack() as stack:
                    stack.enter_context(
                        mock.patch.object(
                            os,
                            "replace",
                            side_effect=observe_canonical_replace,
                        )
                    )
                    if caller == "apply":
                        stack.enter_context(
                            mock.patch.object(
                                APPLY_MODULE,
                                "render_text_layer",
                                side_effect=rerender,
                            )
                        )
                    if caller == "propose":
                        result, escaped = _call_without_escape(
                            propose_design_spec,
                            {
                                "design_spec": _video_spec(
                                    "Install proposal only after persistence"
                                )
                            },
                            ctx=ctx,
                        )
                    else:
                        result, escaped = _call_without_escape(
                            apply_design_ops,
                            {
                                "ops": [
                                    {
                                        "op": "replace_text",
                                        "finding_id": "test:canonical-order",
                                        "layer_id": "title",
                                        "text": "Install operation only after persistence",
                                    }
                                ]
                            },
                            ctx=ctx,
                        )

                current_spec = ctx.state.get("design_spec")
                current_hash = (
                    design_spec_sha256(current_spec)
                    if isinstance(current_spec, DesignSpec)
                    else None
                )
                self.assertEqual(
                    {
                        "escaped": None if escaped is None else type(escaped).__name__,
                        "status": getattr(result, "status", None),
                        "render_calls": render_calls,
                        "canonical_write_count": len(canonical_observations),
                        "archive_already_durable": bool(canonical_observations)
                        and all(
                            observation["archive_already_durable"]
                            for observation in canonical_observations
                        ),
                        "prior_state_before_write": bool(canonical_observations)
                        and all(
                            observation["prior_state_before_write"]
                            for observation in canonical_observations
                        ),
                        "prior_state_after_write": bool(canonical_observations)
                        and all(
                            observation["prior_state_after_write"]
                            for observation in canonical_observations
                        ),
                        "proposed_state_installed_after_return": (
                            _state_signature(ctx.state) != prior_state
                            and ctx.state.get("spec_revision_count") == 2
                            and ctx.state.get("design_spec_sha256") == current_hash
                        ),
                    },
                    {
                        "escaped": None,
                        "status": "ok",
                        "render_calls": ["title"] if caller == "apply" else [],
                        "canonical_write_count": 1,
                        "archive_already_durable": True,
                        "prior_state_before_write": True,
                        "prior_state_after_write": True,
                        "proposed_state_installed_after_return": True,
                    },
                )

    def test_apply_failure_restores_rerendered_layer_ownership(self) -> None:
        for failure_point in ("archive", "canonical"):
            with (
                self.subTest(failure_point=failure_point),
                tempfile.TemporaryDirectory() as raw_tmp,
            ):
                root = Path(raw_tmp) / "run"
                ctx = _poster_design_context(root)
                prior_state = _state_signature(ctx.state)
                prior_canonical = (root / "design_spec.json").read_bytes()
                real_replace = os.replace
                render_calls: list[str] = []

                if failure_point == "archive":
                    (root / "specs").write_bytes(b"archive parent is not a directory")

                def rerender(render_args: dict[str, object], *, ctx: ToolContext):
                    layer_id = str(render_args["layer_id"])
                    version = ctx.next_layer_version(layer_id)
                    rendered_path = ctx.layers_dir / f"{layer_id}_v{version}.png"
                    rendered_path.write_bytes(b"proposed rendered layer")
                    ctx.state["rendered_layers"][layer_id] = {
                        "png_path": str(rendered_path),
                        "owner": "proposed",
                    }
                    render_calls.append(layer_id)
                    return obs_ok({"layer_id": layer_id})

                def injected_replace(source, destination, *args, **kwargs):
                    if (
                        failure_point == "canonical"
                        and _same_parent_bound_entry(
                            Path(destination),
                            root / "design_spec.json",
                            dst_dir_fd=kwargs.get("dst_dir_fd"),
                        )
                    ):
                        raise OSError("injected canonical persistence failure")
                    return real_replace(source, destination, *args, **kwargs)

                with (
                    mock.patch.object(
                        APPLY_MODULE,
                        "render_text_layer",
                        side_effect=rerender,
                    ),
                    mock.patch.object(
                        os,
                        "replace",
                        side_effect=injected_replace,
                    ),
                ):
                    result, escaped = _call_without_escape(
                        apply_design_ops,
                        {
                            "ops": [
                                {
                                    "op": "replace_text",
                                    "finding_id": "test:rerender-ownership",
                                    "layer_id": "title",
                                    "text": "Proposed rerendered title",
                                }
                            ]
                        },
                        ctx=ctx,
                    )

                archive_path = root / "specs" / "design_spec_02.json"
                self.assertEqual(
                    {
                        "escaped": None if escaped is None else type(escaped).__name__,
                        "status": getattr(result, "status", None),
                        "render_calls": render_calls,
                        "state_exact": _state_signature(ctx.state) == prior_state,
                        "canonical_exact": (root / "design_spec.json").read_bytes()
                        == prior_canonical,
                        "archive_present_after_canonical_failure": archive_path.is_file(),
                    },
                    {
                        "escaped": None,
                        "status": "error",
                        "render_calls": ["title"],
                        "state_exact": True,
                        "canonical_exact": True,
                        "archive_present_after_canonical_failure": False,
                    },
                )

    def test_design_spec_archive_is_immutable_idempotent_and_race_safe(self) -> None:
        propose_module = PROPOSE_MODULE

        for case in ("identical", "conflict", "concurrent_winner"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp) / "run"
                ctx, pointer_path, _manifest = _design_context_with_delivery(root)
                prior_state = _state_signature(ctx.state)
                prior_canonical = (root / "design_spec.json").read_bytes()
                prior_pointer = pointer_path.read_bytes()
                new_spec = DesignSpec.model_validate(_video_spec("Immutable revision"))
                archive_payload = {
                    "artifact_type": "video",
                    "is_revision": True,
                    "revision": 2,
                    "design_spec_sha256": design_spec_sha256(new_spec),
                    "design_spec": new_spec.model_dump(mode="json"),
                }
                archive_path = root / "specs" / "design_spec_02.json"
                if case == "identical":
                    atomic_write_json(archive_path, archive_payload)
                elif case == "conflict":
                    atomic_write_json(archive_path, {"foreign": "archive"})
                prior_archive_identity = (
                    (archive_path.stat().st_dev, archive_path.stat().st_ino)
                    if archive_path.is_file()
                    else None
                )
                injected = False
                winner_bytes = b'{"foreign":"concurrent archive winner"}\n'

                def commit_hook(phase: str, *, path: Path, **_details):
                    nonlocal injected
                    if (
                        case == "concurrent_winner"
                        and phase == "before_archive_publish"
                        and not injected
                    ):
                        Path(path).parent.mkdir(parents=True, exist_ok=True)
                        Path(path).write_bytes(winner_bytes)
                        injected = True

                with mock.patch.object(
                    propose_module,
                    "_design_spec_commit_phase_hook",
                    side_effect=commit_hook,
                    create=True,
                ):
                    result, escaped = _call_without_escape(
                        propose_design_spec,
                        {"design_spec": new_spec.model_dump(mode="json")},
                        ctx=ctx,
                    )

                if case in {"identical", "conflict"}:
                    expected_status = "ok"
                    expected_state_exact = False
                    expected_canonical_exact = False
                    expected_pointer_exact = True
                    expected_archive = (
                        json.dumps(
                            archive_payload,
                            ensure_ascii=False,
                            indent=2,
                        ).encode("utf-8")
                        if case == "identical"
                        else b'{\n  "foreign": "archive"\n}'
                    )
                else:
                    expected_status = "error"
                    expected_state_exact = True
                    expected_canonical_exact = True
                    expected_pointer_exact = True
                    expected_archive = winner_bytes
                observed_archive = archive_path.read_bytes() if archive_path.is_file() else b""
                observed_canonical = json.loads(
                    (root / "design_spec.json").read_bytes()
                )
                observed_archive_identity = (
                    (archive_path.stat().st_dev, archive_path.stat().st_ino)
                    if archive_path.is_file()
                    else None
                )
                self.assertEqual(
                    {
                        "escaped": None if escaped is None else type(escaped).__name__,
                        "status": getattr(result, "status", None),
                        "state_exact": _state_signature(ctx.state) == prior_state,
                        "canonical_exact": (root / "design_spec.json").read_bytes()
                        == prior_canonical,
                        "canonical_revision": observed_canonical.get("revision"),
                        "pointer_exact": pointer_path.is_file()
                        and pointer_path.read_bytes() == prior_pointer,
                        "race_injected": injected,
                        "archive_bytes": observed_archive,
                        "preexisting_archive_identity_retained": (
                            observed_archive_identity == prior_archive_identity
                            if prior_archive_identity is not None
                            else None
                        ),
                    },
                    {
                        "escaped": None,
                        "status": expected_status,
                        "state_exact": expected_state_exact,
                        "canonical_exact": expected_canonical_exact,
                        "canonical_revision": (
                            3 if case in {"identical", "conflict"} else 1
                        ),
                        "pointer_exact": expected_pointer_exact,
                        "race_injected": case == "concurrent_winner",
                        "archive_bytes": expected_archive,
                        "preexisting_archive_identity_retained": (
                            True if prior_archive_identity is not None else None
                        ),
                    },
                )

    def test_successful_video_revisions_retain_a_stale_pointer(self) -> None:
        for caller in ("propose", "apply"):
            with self.subTest(caller=caller), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp) / "run"
                ctx, pointer_path, _manifest = _design_context_with_delivery(root)
                prior_pointer = pointer_path.read_bytes()
                if caller == "propose":
                    result, escaped = _call_without_escape(
                        propose_design_spec,
                        {"design_spec": _video_spec("Successful proposal revision")},
                        ctx=ctx,
                    )
                else:
                    result, escaped = _call_without_escape(
                        apply_design_ops,
                        {
                            "ops": [
                                {
                                    "op": "html_replace_text",
                                    "finding_id": "test:successful-revision",
                                    "block_id": "headline",
                                    "text": "Successful operation revision",
                                }
                            ]
                        },
                        ctx=ctx,
                    )
                self.assertEqual(
                    {
                        "escaped": None if escaped is None else type(escaped).__name__,
                        "status": getattr(result, "status", None),
                        "old_pointer_retained": pointer_path.is_file()
                        and pointer_path.read_bytes() == prior_pointer,
                        "old_pointer_is_stale": getattr(
                            web_server._validated_video_delivery(root),
                            "reason_code",
                            None,
                        )
                        == "stale_design_spec",
                        "stale_state_cleared": "video_delivery" not in ctx.state
                        and not ctx.state.get("finalized", False),
                    },
                    {
                        "escaped": None,
                        "status": "ok",
                        "old_pointer_retained": True,
                        "old_pointer_is_stale": True,
                        "stale_state_cleared": True,
                    },
                )

    def test_normal_export_propagates_deduplicated_pointer_cleanup_warnings(self) -> None:
        video_module = VIDEO_MODULE

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "run"
            ctx = _export_context(root)
            warning = "normal invalidation recovery close warning"
            update = attempt_candidates_module.VideoDeliveryPointerUpdate(
                status="absent",
                pointer_snapshot=None,
                payload=None,
                cleanup_warnings=(warning, warning),
            )
            log_calls: list[tuple[str, dict[str, object]]] = []

            def capture_log(event: str, **details: object) -> None:
                log_calls.append((event, details))

            with (
                mock.patch.object(
                    video_module,
                    "update_video_delivery_pointer",
                    return_value=update,
                ),
                mock.patch.object(video_module, "log", side_effect=capture_log),
            ):
                result, escaped = _call_without_escape(
                    video_module.export_video,
                    {"video_id": "warning-propagation"},
                    ctx=ctx,
                )

            logged_warning_lists = [
                list(details["pointer_cleanup_warnings"])
                for _event, details in log_calls
                if isinstance(details.get("pointer_cleanup_warnings"), (list, tuple))
            ]
            self.assertEqual(
                {
                    "escaped": None if escaped is None else type(escaped).__name__,
                    "state": ctx.state.get("pointer_cleanup_warnings"),
                    "payload": (
                        result.payload.get("pointer_cleanup_warnings")
                        if result is not None
                        else None
                    ),
                    "structured_log": bool(logged_warning_lists)
                    and all(
                        warnings == [warning]
                        for warnings in logged_warning_lists
                    ),
                },
                {
                    "escaped": None,
                    "state": [warning],
                    "payload": [warning],
                    "structured_log": True,
                },
            )

    def test_retry_propagates_cleanup_warnings_from_every_post_invalidation_return(
        self,
    ) -> None:
        video_module = VIDEO_MODULE
        cases = (
            ("authoring_lint", "authoring_lint"),
            ("tts_synthesis", "tts"),
            ("tts_contract", "tts"),
            ("subtitle_readability", "subtitles"),
            ("subtitle_value", "subtitles"),
            ("lint", "lint"),
            ("render_process", "render"),
            ("render_subtitle_track", "render"),
            ("render_duration", "render"),
            ("delivery", "delivery"),
            ("final_pointer", "final_pointer"),
            ("done", "done"),
        )
        invalidation_warning = "z-first retry invalidation recovery close warning"
        publication_warning = "a-second retry publication recovery close warning"

        for case, expected_phase in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp) / "run"
                project = _make_retry_project(root)
                base_probe = VideoMediaProbe(
                    video_codec="h264",
                    pixel_format="yuv420p",
                    audio_codec="aac",
                    width=1920,
                    height=1080,
                    fps=30,
                    duration_s=350 if case == "render_duration" else 360,
                )
                captioned_probe = base_probe.model_copy(
                    update={
                        "subtitle_codec": "mov_text",
                        "subtitle_forced": False,
                    }
                )
                speech_metric_calls = 0

                def update_pointer(
                    _run_dir: Path,
                    *,
                    mode: str,
                    **_kwargs: object,
                ) -> object:
                    if mode == "invalidate_if_present":
                        return attempt_candidates_module.VideoDeliveryPointerUpdate(
                            status="absent",
                            pointer_snapshot=None,
                            payload=None,
                            cleanup_warnings=(
                                invalidation_warning,
                                invalidation_warning,
                            ),
                        )
                    if case == "final_pointer":
                        raise OSError("injected publication failure")
                    return attempt_candidates_module.VideoDeliveryPointerUpdate(
                        status="published",
                        pointer_snapshot=None,
                        payload={},
                        cleanup_warnings=(
                            invalidation_warning,
                            publication_warning,
                            publication_warning,
                        ),
                    )

                def write_narration_artifacts(
                    proj_dir: Path,
                    _scene_manifest: list[dict[str, object]],
                    _voice_preset: str,
                    *,
                    speech_timing: object = None,
                ) -> dict[str, object]:
                    if speech_timing is not None and case == "subtitle_readability":
                        raise video_module._SubtitleReadabilityError(
                            "subtitle hard limit exceeded",
                            diagnostics=[{"scene_id": "scene_01"}],
                        )
                    if speech_timing is not None and case == "subtitle_value":
                        raise ValueError("subtitle generation failed")
                    narration_dir = proj_dir / "narration"
                    narration_dir.mkdir(parents=True, exist_ok=True)
                    files = {
                        "transcript_path": narration_dir / "transcript.en.txt",
                        "srt_path": narration_dir / "subtitles.en.srt",
                        "vtt_path": narration_dir / "subtitles.en.vtt",
                        "voice_metadata_path": narration_dir / "voice.json",
                        "narration_timing_path": narration_dir / "timing.json",
                    }
                    for name, path in files.items():
                        path.write_text(
                            "{}\n" if name == "voice_metadata_path" else "test\n",
                            encoding="utf-8",
                        )
                    return {
                        name: path.relative_to(proj_dir).as_posix()
                        for name, path in files.items()
                    } | {
                        "subtitle_diagnostics": [],
                        "subtitle_soft_limit_exceeded": False,
                    }

                def synthesize(
                    proj_dir: Path,
                    *,
                    scene_manifest: list[dict[str, object]],
                    **_kwargs: object,
                ) -> tuple[str, bool, Path | None, list[dict[str, object]]]:
                    if case == "tts_synthesis":
                        return "injected synthesis failure", False, None, []
                    audio_path = proj_dir / "assets" / "narration.wav"
                    audio_path.parent.mkdir(parents=True, exist_ok=True)
                    audio_path.write_bytes(b"narration")
                    timings = [
                        {
                            "scene_id": scene["scene_id"],
                            "start_s": scene["start_s"],
                            "speech_duration_s": 25.0,
                            "end_s": float(scene["start_s"]) + 25.0,
                            "speed": 1.0,
                        }
                        for scene in scene_manifest
                    ]
                    return "tts ok", True, audio_path, timings

                def speech_metrics(
                    *_args: object,
                    **_kwargs: object,
                ) -> tuple[dict[str, object], str | None]:
                    nonlocal speech_metric_calls
                    speech_metric_calls += 1
                    metrics = {
                        "spoken_word_count": 720,
                        "spoken_wpm": 120.0,
                        "speech_coverage_ratio": 0.8,
                    }
                    if case == "tts_contract" and speech_metric_calls == 1:
                        return metrics, "injected TTS delivery contract failure"
                    if case == "delivery" and speech_metric_calls == 2:
                        return metrics, "injected final delivery failure"
                    return metrics, None

                def render(
                    proj_dir: Path,
                    *_args: object,
                    **_kwargs: object,
                ) -> tuple[str, bool, Path | None, VideoMediaProbe | None]:
                    if case == "render_process":
                        return "injected render failure", False, None, None
                    raw_mp4 = proj_dir / "renders" / "retry.mp4"
                    raw_mp4.parent.mkdir(parents=True, exist_ok=True)
                    raw_mp4.write_bytes(b"rendered mp4")
                    return "render ok", True, raw_mp4, base_probe

                def prepare_captioned(
                    mp4_path: Path,
                    _subtitle_path: Path,
                    **_kwargs: object,
                ) -> tuple[str, bool, Path | None, VideoMediaProbe | None]:
                    if case == "render_subtitle_track":
                        return "injected subtitle track failure", False, None, None
                    captioned = mp4_path.with_name("retry-captioned.mp4")
                    captioned.write_bytes(b"captioned mp4")
                    return "subtitle track ok", True, captioned, captioned_probe

                with (
                    mock.patch.object(
                        video_module,
                        "update_video_delivery_pointer",
                        side_effect=update_pointer,
                    ),
                    mock.patch.object(
                        video_module,
                        "_write_narration_artifacts",
                        side_effect=write_narration_artifacts,
                    ),
                    mock.patch.object(
                        video_module,
                        "_run_hyperframes_authoring_lint",
                        return_value=(
                            ("injected authoring lint failure", False)
                            if case == "authoring_lint"
                            else ("authoring lint ok", True)
                        ),
                    ),
                    mock.patch.object(
                        video_module,
                        "_synthesize_timed_narration",
                        side_effect=synthesize,
                    ),
                    mock.patch.object(
                        video_module,
                        "_speech_delivery_metrics",
                        side_effect=speech_metrics,
                    ),
                    mock.patch.object(
                        video_module,
                        "_run_hyperframes_lint",
                        return_value=(
                            ("injected lint failure", False)
                            if case == "lint"
                            else ("lint ok", True)
                        ),
                    ),
                    mock.patch.object(
                        video_module,
                        "_run_hyperframes_render",
                        side_effect=render,
                    ),
                    mock.patch.object(
                        video_module,
                        "_prepare_captioned_delivery_mp4",
                        side_effect=prepare_captioned,
                    ),
                ):
                    result, escaped = _call_without_escape(
                        video_module.retry_video_export_project,
                        root,
                        project,
                    )

                expected_warnings = [invalidation_warning]
                if case == "done":
                    expected_warnings.append(publication_warning)
                self.assertEqual(
                    {
                        "escaped": None if escaped is None else type(escaped).__name__,
                        "phase": (result or {}).get("phase"),
                        "pointer_cleanup_warnings": (result or {}).get(
                            "pointer_cleanup_warnings"
                        ),
                    },
                    {
                        "escaped": None,
                        "phase": expected_phase,
                        "pointer_cleanup_warnings": expected_warnings,
                    },
                )

    def test_worker_retry_is_immediately_publishable_by_derived_completion(self) -> None:
        video_module = VIDEO_MODULE

        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            parent_id = "retry-parent"
            child_id = "retry-child"
            source_project = _make_retry_project(runs_dir / parent_id)
            request = VideoExportRetryWorkerRequest(
                job_kind="video_export_retry",
                run_id=child_id,
                parent_run_id=parent_id,
                source_project=str(source_project),
                conversation_id="conversation",
                baseline_artifact_json="{}",
                runs_dir=str(runs_dir),
            )
            with _patched_expensive_video_seams(video_module):
                result = run_worker_module._run_video_export_retry(
                    request,
                    CancellationToken.never(child_id),
                )
            outcome = WorkerOutcome(
                run_id=child_id,
                job_kind="video_export_retry",
                returncode=0,
                ok=True,
                result=result,
                error=None,
                relayed_events=0,
            )
            state = web_server._RunState(
                artifact_type="video",
                conversation_id="conversation",
                baseline_artifact_json="{}",
            )
            with mock.patch.object(web_server, "RUNS_DIR", runs_dir):
                success, artifact, message, event_data = (
                    web_server._prepare_derived_completion(
                        run_id=child_id,
                        state=state,
                        job_kind="video_export_retry",
                        parent_run_id=parent_id,
                        descriptor={
                            "job_kind": "video_export_retry",
                            "parent_run_id": parent_id,
                        },
                        outcome=outcome,
                    )
                )

            pointer_path = runs_dir / child_id / "final" / "video_delivery.json"
            self.assertEqual(
                {
                    "success": success,
                    "artifact_type": getattr(artifact, "artifact_type", None),
                    "artifact_download": bool(getattr(artifact, "download_url", None)),
                    "message_status": message.status,
                    "event_source": event_data.get("source"),
                    "pointer_cleanup_warnings": result.get(
                        "pointer_cleanup_warnings"
                    ),
                    "pointer_is_real": pointer_path.is_file()
                    and not pointer_path.is_symlink(),
                },
                {
                    "success": True,
                    "artifact_type": "video",
                    "artifact_download": True,
                    "message_status": "done",
                    "event_source": "video_export_retry",
                    "pointer_cleanup_warnings": [],
                    "pointer_is_real": True,
                },
            )

    def test_final_pointer_phase_survives_worker_outcome_and_web_diagnostics(self) -> None:
        video_module = VIDEO_MODULE

        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            parent_id = "phase-parent"
            child_id = "phase-child"
            source_project = _make_retry_project(runs_dir / parent_id)
            store = RunControlStore(runs_dir)
            record = store.reserve(child_id, "video", parent_job_id=parent_id)
            record = store.transition(child_id, record, "queued")
            store.transition(child_id, record, "running")
            request = VideoExportRetryWorkerRequest(
                job_kind="video_export_retry",
                run_id=child_id,
                parent_run_id=parent_id,
                source_project=str(source_project),
                conversation_id="conversation",
                baseline_artifact_json="{}",
                runs_dir=str(runs_dir),
            )

            def fail_pointer_transaction(phase: str, **_details):
                if phase == "prepared":
                    raise OSError("injected pointer publication failure")

            stdin = SimpleNamespace(buffer=BytesIO(encode_request(request)))
            with (
                _patched_expensive_video_seams(video_module),
                mock.patch.object(
                    pointer_transaction_module,
                    "_video_pointer_transaction_phase_hook",
                    side_effect=fail_pointer_transaction,
                ),
                mock.patch.object(sys, "argv", ["run_worker", "--run-id", child_id]),
                mock.patch.object(sys, "stdin", stdin),
                mock.patch.object(run_worker_module.signal, "signal"),
                mock.patch.object(run_worker_module.os, "getsid", return_value=os.getpid()),
            ):
                exit_code = run_worker_module.worker_main()

            worker_payload = json.loads(
                (runs_dir / child_id / "worker_result.json").read_text(
                    encoding="utf-8"
                )
            )
            supervisor = RunSupervisor(runs_dir, control_store=store)

            class _FinishedProcess:
                async def wait(self):
                    return exit_code

            async def map_outcome() -> WorkerOutcome:
                async def done(value):
                    return value

                return await supervisor._monitor(
                    request,
                    _FinishedProcess(),
                    stdout_task=asyncio.create_task(done(None)),
                    stderr_task=asyncio.create_task(done(None)),
                    relay_task=asyncio.create_task(done(0)),
                )

            outcome = asyncio.run(map_outcome())
            outcome_phase = getattr(
                outcome,
                "failure_phase",
                getattr(outcome, "phase", None),
            )

            class _AcceptFailure:
                async def accept_completion(self, _run_id: str, **_kwargs):
                    return SimpleNamespace(state="failed")

            state = web_server._RunState(
                artifact_type="video",
                conversation_id="conversation",
            )
            runtime = SimpleNamespace(control_store=store, supervisor=_AcceptFailure())
            with (
                mock.patch.object(web_server, "RUNS_DIR", runs_dir),
                mock.patch.object(web_server, "_web_run_runtime", return_value=runtime),
                mock.patch.object(web_server, "_list_produced_artifacts", return_value=[]),
            ):
                asyncio.run(
                    web_server._monitor_supervised_derived_job(
                        run_id=child_id,
                        state=state,
                        job_kind="video_export_retry",
                        parent_run_id=parent_id,
                        descriptor={"job_kind": "video_export_retry"},
                        recovered_outcome=outcome,
                    )
                )
            failure = getattr(state.result_message, "failure", None)
            web_phase = getattr(failure, "phase", None)
            retry_route = getattr(failure, "retry_route", None)
            raw_error = worker_payload.get("error")
            raw_phase = raw_error.get("phase") if isinstance(raw_error, dict) else None

            self.assertEqual(
                {
                    "exit_code": exit_code,
                    "worker_ok": worker_payload.get("ok"),
                    "worker_phase": raw_phase,
                    "outcome_ok": outcome.ok,
                    "outcome_phase": outcome_phase,
                    "web_phase": web_phase,
                    "not_full_authoring": retry_route != "full_authoring",
                },
                {
                    "exit_code": 1,
                    "worker_ok": False,
                    "worker_phase": "final_pointer",
                    "outcome_ok": False,
                    "outcome_phase": "final_pointer",
                    "web_phase": "final_pointer",
                    "not_full_authoring": True,
                },
            )

    def test_cold_web_recovery_preserves_persisted_final_pointer_phase(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            out_dir = Path(raw_tmp) / "out"
            runs_dir = out_dir / "runs"
            runs_dir.mkdir(parents=True)
            run_id = "cold-phase-child"
            parent_id = "cold-phase-parent"
            store = RunControlStore(runs_dir)
            record = store.reserve(run_id, "video", parent_job_id=parent_id)
            record = store.transition(run_id, record, "queued")
            record = store.transition(run_id, record, "running")
            store.transition(run_id, record, "completing")
            run_dir = runs_dir / run_id
            atomic_write_json(
                run_dir / "derived_job.json",
                {
                    "version": 1,
                    "job_kind": "video_export_retry",
                    "run_id": run_id,
                    "parent_run_id": parent_id,
                    "artifact_type": "video",
                    "conversation_id": "conversation",
                    "baseline_artifact_json": "{}",
                    "source_artifact_id": f"art_{parent_id}",
                    "artifact_name": "Video",
                    "source_relative_path": "hyperframes-paper-video",
                },
            )
            worker_envelope = {
                "job_kind": "video_export_retry",
                "run_id": run_id,
                "ok": False,
                "error": {
                    "type": "VideoPointerPublicationError",
                    "message": "pointer transaction failed",
                    "phase": "final_pointer",
                    "pointer_cleanup_warnings": [],
                },
            }
            atomic_write_json(
                run_dir / "worker_result.json",
                worker_envelope,
            )
            decoded, protocol_error = _call_without_escape(
                decode_worker_result,
                worker_envelope,
                expected_run_id=run_id,
                expected_job_kind="video_export_retry",
            )
            decoded_error = (
                decoded.get("error") if isinstance(decoded, dict) else None
            )
            decoded_phase = (
                decoded_error.get("phase")
                if isinstance(decoded_error, dict)
                else None
            )
            decoded_warnings = (
                decoded_error.get("pointer_cleanup_warnings")
                if isinstance(decoded_error, dict)
                else None
            )
            settings = Settings(
                anthropic_api_key="",
                anthropic_base_url=None,
                gemini_api_key="",
                designer_model="designer",
                critic_model="critic",
                repo_root=REPO_ROOT,
                out_dir=out_dir,
            )
            supervisor = RunSupervisor(runs_dir, control_store=store)
            runtime = web_server._WebRunRuntime(
                runs_dir=runs_dir.resolve(),
                control_store=store,
                supervisor=supervisor,
                services=WebRunServices(
                    runs_dir,
                    control_store=store,
                    supervisor=supervisor,
                ),
            )

            async def recover() -> tuple[bool, object | None]:
                await web_server._recover_web_run_controls()
                state = web_server._RUNS.get(run_id)
                if state is None:
                    return False, None
                if state.task is not None:
                    await state.task
                return True, getattr(state.result_message, "failure", None)

            web_server._RUNS.pop(run_id, None)
            with (
                mock.patch.object(web_server, "RUNS_DIR", runs_dir),
                mock.patch.object(web_server, "_BOOT_OUT_DIR", out_dir),
                mock.patch.object(web_server, "SETTINGS", settings),
                mock.patch.object(web_server, "_WEB_RUN_RUNTIME", runtime),
                mock.patch.object(web_server, "_list_produced_artifacts", return_value=[]),
            ):
                try:
                    state_recovered, failure = asyncio.run(recover())
                    recovery_error = None
                except BaseException as exc:  # convert pre-GREEN gaps to RED data
                    state_recovered = False
                    failure = None
                    recovery_error = type(exc).__name__
            web_server._RUNS.pop(run_id, None)

            self.assertEqual(
                {
                    "protocol_accepted": protocol_error is None,
                    "protocol_error": None
                    if protocol_error is None
                    else type(protocol_error).__name__,
                    "decoded_phase": decoded_phase,
                    "decoded_warnings": decoded_warnings,
                    "recovery_error": recovery_error,
                    "state_recovered": state_recovered,
                    "phase": getattr(failure, "phase", None),
                    "failure_warnings": getattr(
                        failure,
                        "pointer_cleanup_warnings",
                        None,
                    ),
                    "retry_route": getattr(failure, "retry_route", None),
                    "control_state": store.read(run_id).state,
                },
                {
                    "protocol_accepted": True,
                    "protocol_error": None,
                    "decoded_phase": "final_pointer",
                    "decoded_warnings": [],
                    "recovery_error": None,
                    "state_recovered": True,
                    "phase": "final_pointer",
                    "failure_warnings": [],
                    "retry_route": "none",
                    "control_state": "failed",
                },
            )


if __name__ == "__main__":
    unittest.main()
