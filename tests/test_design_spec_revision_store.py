from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import autodesign.design_spec_persistence as persistence
from autodesign.schema import DesignSpec
from autodesign.tools import ToolContext
from autodesign.tools.apply_design_ops import apply_design_ops
from autodesign.tools.propose_design_spec import propose_design_spec
from autodesign.util.design_spec_fingerprint import design_spec_sha256
from tests.test_design_spec_persistence import _FakeWindowsArchiveIO


_CRASH_WORKER = Path(__file__).parent / "fixtures" / "design_spec_crash_worker.py"
_RACE_WORKER = Path(__file__).parent / "fixtures" / "design_spec_race_worker.py"


def _video_spec(title: str) -> dict[str, object]:
    return {
        "brief": "Exercise crash-safe DesignSpec revision persistence.",
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
                            "text": title,
                        }
                    ],
                }
            ],
        },
    }


def _encoded(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _envelope(
    spec: dict[str, object],
    revision: int,
    *,
    parent_revision: int | None | object = ...,
    parent_sha256: str | None | object = ...,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "artifact_type": "video",
        "is_revision": revision > 1,
        "revision": revision,
        "design_spec_sha256": design_spec_sha256(spec),
        "design_spec": spec,
    }
    if parent_revision is not ...:
        payload["parent_revision"] = parent_revision
    if parent_sha256 is not ...:
        payload["parent_design_spec_sha256"] = parent_sha256
    return payload


def _seed_base(root: Path, title: str = "Base") -> tuple[dict[str, object], str]:
    spec = DesignSpec.model_validate(_video_spec(title)).model_dump(mode="json")
    spec_hash = design_spec_sha256(spec)
    payload = _envelope(spec, 1)
    specs = root / "specs"
    specs.mkdir(parents=True)
    (specs / "design_spec_01.json").write_bytes(_encoded(payload))
    (root / "design_spec.json").write_bytes(_encoded(payload))
    return spec, spec_hash


def _context(root: Path, spec: dict[str, object], revision: int = 1) -> ToolContext:
    layers = root / "layers"
    layers.mkdir(exist_ok=True)
    ctx = ToolContext(
        settings=SimpleNamespace(poster_harness_mode="cheap"),
        run_dir=root,
        layers_dir=layers,
        run_id="revision-store-test",
    )
    validated = DesignSpec.model_validate(spec)
    ctx.state.update(
        {
            "artifact_type": "video",
            "design_spec": validated,
            "design_spec_sha256": design_spec_sha256(validated),
            "spec_revision_count": revision,
            "rendered_layers": {"retained": {"owner": "base"}},
            "composition": {"identity": "base"},
            "visual_reference_revision_required": True,
            "visual_reference_revision_source_spec_revision": revision,
            "visual_reference_revision_spec_revision": revision,
            "visual_reference_revision_composited": True,
            "video_delivery": {"status": "ready"},
            "finalized": True,
            "sentinel": {"nested": ["unchanged"]},
        }
    )
    return ctx


class DesignSpecRevisionStoreTests(unittest.TestCase):
    def _run_commit_race(
        self,
        *,
        root: Path,
        base_hash: str,
        specs: tuple[dict[str, object], dict[str, object]],
    ) -> list[dict[str, object]]:
        start_path = root / "start"
        processes: list[subprocess.Popen[str]] = []
        result_paths: list[Path] = []
        for index, spec in enumerate(specs):
            request_path = root / f"request-{index}.json"
            request_path.write_text(
                json.dumps(
                    {
                        "artifact_type": "video",
                        "design_spec": spec,
                        "expected_base_revision": 1,
                        "expected_base_sha256": base_hash,
                    }
                ),
                encoding="utf-8",
            )
            result_path = root / f"result-{index}.json"
            result_paths.append(result_path)
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        os.fspath(_RACE_WORKER),
                        os.fspath(root),
                        os.fspath(request_path),
                        os.fspath(start_path),
                        os.fspath(result_path),
                    ],
                    cwd=Path(__file__).parents[1],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        start_path.touch()
        for process in processes:
            _stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
        return [json.loads(path.read_text(encoding="utf-8")) for path in result_paths]

    @unittest.skipUnless(os.name == "posix", "retained directory test requires POSIX")
    def test_canonical_lock_does_not_depend_on_strict_realpath_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            canonical = root / "design_spec.json"
            transient = FileNotFoundError(2, "No such file or directory", "/.vol/16777229")

            with mock.patch.object(Path, "resolve", side_effect=transient):
                with persistence._canonical_transaction_lock(canonical) as namespace:
                    self.assertEqual(namespace.canonical_path, canonical.absolute())

    def test_windows_shim_commit_load_idempotency_and_stale_cas(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            _base, base_hash = _seed_base(root)
            native = _FakeWindowsArchiveIO()
            target = _video_spec("Windows winner")
            patches = (
                mock.patch.object(persistence, "_LOCK_OS_NAME", "nt"),
                mock.patch.object(persistence, "_RUNTIME_OS_NAME", "nt"),
                mock.patch.object(
                    persistence,
                    "_windows_archive_io_factory",
                    return_value=native,
                ),
            )
            with patches[0], patches[1], patches[2]:
                committed = persistence.commit_design_spec_revision(
                    canonical_path=root / "design_spec.json",
                    artifact_type="video",
                    design_spec=target,
                    is_revision=True,
                    expected_base_revision=1,
                    expected_base_sha256=base_hash,
                )
                restored = persistence.load_design_spec_canonical(
                    root / "design_spec.json"
                )
                identical = persistence.commit_design_spec_revision(
                    canonical_path=root / "design_spec.json",
                    artifact_type="video",
                    design_spec=target,
                    is_revision=True,
                    expected_base_revision=1,
                    expected_base_sha256=base_hash,
                )
                with self.assertRaises(persistence.DesignSpecPersistenceError) as raised:
                    persistence.commit_design_spec_revision(
                        canonical_path=root / "design_spec.json",
                        artifact_type="video",
                        design_spec=_video_spec("Windows stale loser"),
                        is_revision=True,
                        expected_base_revision=1,
                        expected_base_sha256=base_hash,
                    )

            self.assertEqual(committed.revision, 2)
            self.assertEqual(identical.revision, 2)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.revision, 2)
            self.assertEqual(restored.design_spec_sha256, committed.design_spec_sha256)
            self.assertEqual(raised.exception.phase, "cas")
            self.assertFalse((root / "specs" / "design_spec_03.json").exists())
            self.assertTrue(native.handles)
            self.assertTrue(all(handle.closed for handle in native.handles))

    def test_windows_shim_unsafe_archive_entry_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            _base, base_hash = _seed_base(root)
            canonical_before = (root / "design_spec.json").read_bytes()
            foreign = root / "foreign.json"
            foreign.write_text("{}", encoding="utf-8")
            (root / "specs" / "design_spec_02.json").symlink_to(foreign)
            native = _FakeWindowsArchiveIO()

            with (
                mock.patch.object(persistence, "_LOCK_OS_NAME", "nt"),
                mock.patch.object(persistence, "_RUNTIME_OS_NAME", "nt"),
                mock.patch.object(
                    persistence,
                    "_windows_archive_io_factory",
                    return_value=native,
                ),
            ):
                with self.assertRaises(persistence.DesignSpecPersistenceError) as raised:
                    persistence.commit_design_spec_revision(
                        canonical_path=root / "design_spec.json",
                        artifact_type="video",
                        design_spec=_video_spec("Blocked Windows edit"),
                        is_revision=True,
                        expected_base_revision=1,
                        expected_base_sha256=base_hash,
                    )

            self.assertEqual(raised.exception.phase, "namespace_scan")
            self.assertEqual((root / "design_spec.json").read_bytes(), canonical_before)
            self.assertFalse((root / "specs" / "design_spec_03.json").exists())
            self.assertTrue(all(handle.closed for handle in native.handles))

    def test_different_orphan_is_preserved_and_later_revision_is_committed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            _base, base_hash = _seed_base(root)
            orphan_spec = _video_spec("Orphan A")
            orphan = _envelope(
                orphan_spec,
                2,
                parent_revision=1,
                parent_sha256=base_hash,
            )
            orphan_path = root / "specs" / "design_spec_02.json"
            orphan_path.write_bytes(_encoded(orphan))
            orphan_identity = (orphan_path.stat().st_dev, orphan_path.stat().st_ino)

            target = _video_spec("Different B")
            result = persistence.commit_design_spec_revision(
                canonical_path=root / "design_spec.json",
                artifact_type="video",
                design_spec=target,
                is_revision=True,
                expected_base_revision=1,
                expected_base_sha256=base_hash,
            )

            self.assertEqual(result.revision, 3)
            self.assertEqual(result.design_spec_sha256, design_spec_sha256(target))
            self.assertEqual(
                (orphan_path.stat().st_dev, orphan_path.stat().st_ino),
                orphan_identity,
            )
            self.assertEqual(json.loads(orphan_path.read_bytes()), orphan)
            canonical = json.loads((root / "design_spec.json").read_bytes())
            archived = json.loads((root / "specs" / "design_spec_03.json").read_bytes())
            self.assertEqual(canonical, archived)
            self.assertEqual(canonical["revision"], 3)
            self.assertEqual(canonical["parent_revision"], 1)
            self.assertEqual(canonical["parent_design_spec_sha256"], base_hash)

    def test_exact_current_is_idempotent_before_stale_base_cas_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            _base, base_hash = _seed_base(root)
            first_spec = _video_spec("Winner")
            first = persistence.commit_design_spec_revision(
                canonical_path=root / "design_spec.json",
                artifact_type="video",
                design_spec=first_spec,
                is_revision=True,
                expected_base_revision=1,
                expected_base_sha256=base_hash,
            )
            self.assertEqual(first.revision, 2)

            identical = persistence.commit_design_spec_revision(
                canonical_path=root / "design_spec.json",
                artifact_type="video",
                design_spec=first_spec,
                is_revision=True,
                expected_base_revision=1,
                expected_base_sha256=base_hash,
            )
            self.assertEqual(identical.revision, 2)

            with self.assertRaises(persistence.DesignSpecPersistenceError) as raised:
                persistence.commit_design_spec_revision(
                    canonical_path=root / "design_spec.json",
                    artifact_type="video",
                    design_spec=_video_spec("Stale loser"),
                    is_revision=True,
                    expected_base_revision=1,
                    expected_base_sha256=base_hash,
                )
            self.assertEqual(raised.exception.phase, "cas")
            self.assertFalse((root / "specs" / "design_spec_03.json").exists())

            fresh = persistence.commit_design_spec_revision(
                canonical_path=root / "design_spec.json",
                artifact_type="video",
                design_spec=_video_spec("Fresh edit"),
                is_revision=True,
                expected_base_revision=2,
                expected_base_sha256=first.design_spec_sha256,
            )
            self.assertEqual(fresh.revision, 3)

    @unittest.skipUnless(os.name == "posix", "process lock race requires POSIX")
    def test_same_payload_process_race_converges_on_one_revision(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            _base, base_hash = _seed_base(root)
            target = _video_spec("Same process race")

            results = self._run_commit_race(
                root=root,
                base_hash=base_hash,
                specs=(target, target),
            )

            self.assertEqual({result["status"] for result in results}, {"ok"})
            self.assertEqual({result["revision"] for result in results}, {2})
            self.assertFalse((root / "specs" / "design_spec_03.json").exists())
            self.assertEqual(
                (root / "design_spec.json").read_bytes(),
                (root / "specs" / "design_spec_02.json").read_bytes(),
            )

    @unittest.skipUnless(os.name == "posix", "process lock race requires POSIX")
    def test_different_payload_process_race_has_one_winner_and_one_cas_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            _base, base_hash = _seed_base(root)
            first = _video_spec("First process race")
            second = _video_spec("Second process race")

            results = self._run_commit_race(
                root=root,
                base_hash=base_hash,
                specs=(first, second),
            )

            self.assertEqual(
                sorted(result["status"] for result in results),
                ["error", "ok"],
            )
            self.assertEqual(
                [result.get("phase") for result in results if result["status"] == "error"],
                ["cas"],
                results,
            )
            winner = next(result for result in results if result["status"] == "ok")
            self.assertEqual(winner["revision"], 2)
            self.assertFalse((root / "specs" / "design_spec_03.json").exists())
            self.assertEqual(
                (root / "design_spec.json").read_bytes(),
                (root / "specs" / "design_spec_02.json").read_bytes(),
            )

    def test_legacy_orphan_is_reservation_only_and_numeric_floor_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            _base, base_hash = _seed_base(root)
            target = _video_spec("Desired")
            legacy_orphan = _envelope(target, 2)
            legacy_path = root / "specs" / "design_spec_02.json"
            legacy_path.write_bytes(_encoded(legacy_orphan))
            (root / "specs" / "design_spec_100.json").write_text(
                "{malformed but safely occupied",
                encoding="utf-8",
            )

            result = persistence.commit_design_spec_revision(
                canonical_path=root / "design_spec.json",
                artifact_type="video",
                design_spec=target,
                is_revision=True,
                expected_base_revision=1,
                expected_base_sha256=base_hash,
            )

            self.assertEqual(result.revision, 101)
            self.assertEqual(json.loads(legacy_path.read_bytes()), legacy_orphan)
            self.assertTrue((root / "specs" / "design_spec_101.json").is_file())

    def test_malformed_hash_and_filename_mismatch_all_reserve_numeric_slots(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            _base, base_hash = _seed_base(root)
            specs = root / "specs"
            (specs / "design_spec_98.json").write_text("not json", encoding="utf-8")
            mismatched_hash = _envelope(_video_spec("Hash mismatch"), 99)
            mismatched_hash["design_spec_sha256"] = "0" * 64
            (specs / "design_spec_99.json").write_bytes(_encoded(mismatched_hash))
            wrong_revision = _envelope(_video_spec("Wrong filename"), 2)
            (specs / "design_spec_100.json").write_bytes(_encoded(wrong_revision))

            result = persistence.commit_design_spec_revision(
                canonical_path=root / "design_spec.json",
                artifact_type="video",
                design_spec=_video_spec("After reservations"),
                is_revision=True,
                expected_base_revision=1,
                expected_base_sha256=base_hash,
            )

            self.assertEqual(result.revision, 101)
            self.assertTrue((specs / "design_spec_101.json").is_file())

    @unittest.skipUnless(os.name == "posix", "POSIX no-follow namespace test")
    def test_unsafe_archive_entry_fails_closed_without_changing_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            _base, base_hash = _seed_base(root)
            canonical_before = (root / "design_spec.json").read_bytes()
            foreign = root / "foreign.json"
            foreign.write_text("{}", encoding="utf-8")
            (root / "specs" / "design_spec_02.json").symlink_to(foreign)

            with self.assertRaises(persistence.DesignSpecPersistenceError) as raised:
                persistence.commit_design_spec_revision(
                    canonical_path=root / "design_spec.json",
                    artifact_type="video",
                    design_spec=_video_spec("Blocked"),
                    is_revision=True,
                    expected_base_revision=1,
                    expected_base_sha256=base_hash,
                )
            self.assertEqual(raised.exception.phase, "namespace_scan")
            self.assertEqual((root / "design_spec.json").read_bytes(), canonical_before)
            self.assertFalse((root / "specs" / "design_spec_03.json").exists())

    def test_rehydrate_uses_validated_canonical_not_archive_floor(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            base, base_hash = _seed_base(root)
            (root / "specs" / "design_spec_100.json").write_text(
                "reserved",
                encoding="utf-8",
            )
            ctx = _context(root, _video_spec("Stale memory"), revision=99)

            restored = ctx.rehydrate_design_spec_state()

            self.assertEqual(restored.revision, 1)
            self.assertEqual(ctx.state["spec_revision_count"], 1)
            self.assertEqual(ctx.state["design_spec_sha256"], base_hash)
            self.assertEqual(
                ctx.state["design_spec"].model_dump(mode="json"),
                DesignSpec.model_validate(base).model_dump(mode="json"),
            )
            self.assertNotIn("design_spec_archive_revision_floor", ctx.state)

    def test_rehydrate_rejects_hash_mismatched_canonical_without_state_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            base, _base_hash = _seed_base(root)
            ctx = _context(root, _video_spec("In-memory state"), revision=7)
            before = deepcopy(ctx.state)
            corrupted = json.loads((root / "design_spec.json").read_bytes())
            corrupted["design_spec_sha256"] = "f" * 64
            (root / "design_spec.json").write_bytes(_encoded(corrupted))

            with self.assertRaises(persistence.DesignSpecPersistenceError) as raised:
                ctx.rehydrate_design_spec_state()

            self.assertEqual(raised.exception.phase, "canonical_integrity")
            self.assertEqual(ctx.state, before)
            self.assertNotEqual(ctx.state["design_spec"].model_dump(mode="json"), base)

    def test_rehydrate_rejects_malformed_parent_hash_without_state_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            base, _base_hash = _seed_base(root)
            ctx = _context(root, _video_spec("In-memory state"), revision=7)
            before = deepcopy(ctx.state)
            corrupted = json.loads((root / "design_spec.json").read_bytes())
            corrupted["revision"] = 2
            corrupted["is_revision"] = True
            corrupted["parent_revision"] = 1
            corrupted["parent_design_spec_sha256"] = "not-a-sha256"
            (root / "design_spec.json").write_bytes(_encoded(corrupted))

            with self.assertRaises(persistence.DesignSpecPersistenceError) as raised:
                ctx.rehydrate_design_spec_state()

            self.assertEqual(raised.exception.phase, "canonical_integrity")
            self.assertEqual(ctx.state, before)

    @unittest.skipUnless(os.name == "posix", "POSIX archive entry test")
    def test_hardlinked_and_nonregular_archive_entries_fail_closed(self) -> None:
        for unsafe_kind in ("hardlink", "directory"):
            with self.subTest(unsafe_kind=unsafe_kind), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp)
                _base, base_hash = _seed_base(root)
                unsafe = root / "specs" / "design_spec_02.json"
                if unsafe_kind == "hardlink":
                    foreign = root / "foreign.json"
                    foreign.write_text("{}", encoding="utf-8")
                    os.link(foreign, unsafe)
                else:
                    unsafe.mkdir()
                canonical_before = (root / "design_spec.json").read_bytes()

                with self.assertRaises(persistence.DesignSpecPersistenceError) as raised:
                    persistence.commit_design_spec_revision(
                        canonical_path=root / "design_spec.json",
                        artifact_type="video",
                        design_spec=_video_spec("Blocked"),
                        is_revision=True,
                        expected_base_revision=1,
                        expected_base_sha256=base_hash,
                    )

                self.assertEqual(raised.exception.phase, "namespace_scan")
                self.assertEqual((root / "design_spec.json").read_bytes(), canonical_before)

    def test_propose_and_apply_install_actual_allocated_revision_only_after_commit(self) -> None:
        for caller in ("propose", "apply"):
            with self.subTest(caller=caller), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp)
                base, base_hash = _seed_base(root)
                ctx = _context(root, base)
                orphan = _envelope(
                    _video_spec("Reserved orphan"),
                    2,
                    parent_revision=1,
                    parent_sha256=base_hash,
                )
                (root / "specs" / "design_spec_02.json").write_bytes(_encoded(orphan))

                if caller == "propose":
                    result = propose_design_spec(
                        {"design_spec": _video_spec("Committed proposal")},
                        ctx=ctx,
                    )
                else:
                    result = apply_design_ops(
                        {
                            "ops": [
                                {
                                    "op": "html_replace_text",
                                    "finding_id": "test:revision-store",
                                    "block_id": "headline",
                                    "text": "Committed operation",
                                }
                            ]
                        },
                        ctx=ctx,
                    )

                self.assertEqual(result.status, "ok", result.error_message)
                self.assertEqual(ctx.state["spec_revision_count"], 3)
                self.assertEqual(ctx.state["visual_reference_revision_spec_revision"], 3)
                self.assertFalse(ctx.state["visual_reference_revision_required"])
                self.assertFalse(ctx.state["visual_reference_revision_composited"])
                self.assertNotIn("video_delivery", ctx.state)
                self.assertFalse(ctx.state.get("finalized", False))
                if caller == "apply":
                    self.assertEqual(ctx.state["last_design_ops"]["spec_revision"], 3)
                canonical = json.loads((root / "design_spec.json").read_bytes())
                self.assertEqual(canonical["revision"], 3)
                self.assertEqual(canonical["design_spec_sha256"], ctx.state["design_spec_sha256"])

    def test_stale_caller_failure_preserves_all_nested_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            base, base_hash = _seed_base(root)
            ctx = _context(root, base)
            winner = persistence.commit_design_spec_revision(
                canonical_path=root / "design_spec.json",
                artifact_type="video",
                design_spec=_video_spec("Concurrent winner"),
                is_revision=True,
                expected_base_revision=1,
                expected_base_sha256=base_hash,
            )
            self.assertEqual(winner.revision, 2)
            state_before = deepcopy(ctx.state)

            result = propose_design_spec(
                {"design_spec": _video_spec("Stale caller")},
                ctx=ctx,
            )

            self.assertEqual(result.status, "error")
            self.assertEqual((result.payload or {}).get("phase"), "cas")
            self.assertEqual(ctx.state, state_before)
            self.assertFalse((root / "specs" / "design_spec_03.json").exists())

    @unittest.skipUnless(os.name == "posix", "hard-exit fixture requires POSIX")
    def test_hard_exit_boundaries_recover_without_lock_or_temp_leaks(self) -> None:
        phases = (
            "after_archive_fsync",
            "after_canonical_replace",
            "after_both_directory_fsyncs",
        )
        for phase in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp)
                _base, base_hash = _seed_base(root)
                target = _video_spec(f"Crash target {phase}")
                request_path = root / "request.json"
                request_path.write_text(
                    json.dumps(
                        {
                            "artifact_type": "video",
                            "design_spec": target,
                            "expected_base_revision": 1,
                            "expected_base_sha256": base_hash,
                        }
                    ),
                    encoding="utf-8",
                )
                child = subprocess.run(
                    [
                        sys.executable,
                        os.fspath(_CRASH_WORKER),
                        os.fspath(root),
                        phase,
                        os.fspath(request_path),
                    ],
                    cwd=Path(__file__).parents[1],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(child.returncode, 91, child.stderr)

                if phase == "after_archive_fsync":
                    self.assertEqual(
                        json.loads((root / "design_spec.json").read_bytes())["revision"],
                        1,
                    )
                else:
                    self.assertEqual(
                        json.loads((root / "design_spec.json").read_bytes())["revision"],
                        2,
                    )

                recovered = persistence.commit_design_spec_revision(
                    canonical_path=root / "design_spec.json",
                    artifact_type="video",
                    design_spec=target,
                    is_revision=True,
                    expected_base_revision=1,
                    expected_base_sha256=base_hash,
                )
                self.assertEqual(recovered.revision, 2)
                self.assertEqual(
                    json.loads((root / "design_spec.json").read_bytes()),
                    json.loads((root / "specs" / "design_spec_02.json").read_bytes()),
                )
                self.assertEqual(
                    list((root / "specs").glob("*.tmp"))
                    + list((root / "specs").glob(".*.tmp"))
                    + list(root.glob("*.tmp"))
                    + list(root.glob(".*.tmp")),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
