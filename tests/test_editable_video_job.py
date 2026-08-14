from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from autodesign.process_supervision import ProcessLedger, process_identity, process_is_alive
from autodesign.run_control import CancellationToken, RunCancelled


def _artifact(*, image_srcs: list[str] | None = None) -> dict[str, object]:
    layers: list[dict[str, object]] = [
        {
            "layer_id": "frame-1",
            "kind": "background",
            "name": "Frame",
            "bbox": {"x": 0, "y": 0, "w": 1600, "h": 900},
            "fill_color": "#ffffff",
            "z_index": 0,
        },
        {
            "layer_id": "title",
            "kind": "text",
            "name": "Title",
            "bbox": {"x": 100, "y": 80, "w": 800, "h": 120},
            "text": "Editable video",
            "font_size_px": 54,
            "z_index": 2,
        },
    ]
    for index, image_src in enumerate(image_srcs or []):
        layers.append(
            {
                "layer_id": f"figure-{index}",
                "kind": "image",
                "name": f"Figure {index}",
                "bbox": {"x": 100 + index * 650, "y": 240, "w": 600, "h": 400},
                "src": image_src,
                "z_index": 1 + index,
            }
        )
    return {
        "artifact_type": "video",
        "layers": layers,
        "video_project": {
            "fps": 24,
            "scenes": [
                {
                    "scene_id": "scene-1",
                    "name": "Opening",
                    "frame_layer_id": "frame-1",
                    "duration_s": 2.5,
                }
            ],
        },
    }


def _digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class EditableVideoJobTests(unittest.TestCase):
    def test_project_stages_only_approved_local_assets(self) -> None:
        from autodesign.editable_video_job import write_editable_video_project

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs_dir = root / "runs"
            source_run_dir = runs_dir / "source"
            editor_assets = root / "editor-assets"
            run_dir = runs_dir / "render-1"
            source_run_dir.mkdir(parents=True)
            editor_assets.mkdir(parents=True)
            (editor_assets / "figure.png").write_bytes(b"png-data")

            result = write_editable_video_project(
                artifact=_artifact(image_srcs=["/api/files/editor-assets/figure.png"]),
                runs_dir=runs_dir,
                editor_assets_dir=editor_assets,
                source_run_dir=source_run_dir,
                run_id="render-1",
                run_dir=run_dir,
                cancellation_token=CancellationToken.never("render-1"),
            )

            project_dir = Path(result["project_dir"])
            manifest = json.loads((project_dir / "scene_manifest.json").read_text())
            staged_src = manifest["scenes"][0]["layers"][1]["src"]
            self.assertRegex(staged_src, r"^assets/editor-assets/figure-[0-9a-f]{16}\.png$")
            self.assertEqual((project_dir / staged_src).read_bytes(), b"png-data")
            self.assertIn("data-composition-id=\"editable-video-demo\"", (project_dir / "index.html").read_text())

    def test_run_asset_must_belong_to_explicit_source_run(self) -> None:
        from autodesign.editable_video_job import EditableVideoJobError, write_editable_video_project

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs_dir = root / "runs"
            source_run_dir = runs_dir / "source"
            other_run_dir = runs_dir / "other"
            editor_assets = root / "editor-assets"
            source_run_dir.mkdir(parents=True)
            other_run_dir.mkdir(parents=True)
            editor_assets.mkdir(parents=True)
            (other_run_dir / "secret.png").write_bytes(b"not-authorized")

            with self.assertRaises(EditableVideoJobError) as raised:
                write_editable_video_project(
                    artifact=_artifact(image_srcs=["/api/files/runs/other/secret.png"]),
                    runs_dir=runs_dir,
                    editor_assets_dir=editor_assets,
                    source_run_dir=source_run_dir,
                    run_id="render-denied",
                    run_dir=runs_dir / "render-denied",
                    cancellation_token=CancellationToken.never("render-denied"),
                )

            self.assertEqual(raised.exception.code, "unauthorized_run_asset")
            self.assertFalse(any((runs_dir / "render-denied").rglob("secret.png")))

    def test_run_asset_rejects_internal_symlink_and_hardlink_aliases(self) -> None:
        from autodesign.editable_video_job import EditableVideoJobError, write_editable_video_project

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs_dir = root / "runs"
            source_run_dir = runs_dir / "source"
            source_final = source_run_dir / "final"
            victim = runs_dir / "victim" / "final" / "secret.png"
            editor_assets = root / "editor-assets"
            source_final.mkdir(parents=True)
            victim.parent.mkdir(parents=True)
            editor_assets.mkdir(parents=True)
            (source_run_dir / "RUN_CONTROL.JSON").write_bytes(b"private-control")
            victim.write_bytes(b"private-victim")
            (source_final / "symlink.png").symlink_to(victim)
            (source_final / "hardlink.png").hardlink_to(victim)
            cases = {
                "internal": "/api/files/runs/source/RUN_CONTROL.JSON",
                "symlink": "/api/files/runs/source/final/symlink.png",
                "hardlink": "/api/files/runs/source/final/hardlink.png",
                "encoded_separator": (
                    "/api/files/runs/source%2F..%2Fvictim/final/secret.png"
                ),
                "encoded_internal": (
                    "/api/files/runs/source/%52UN_CONTROL.JSON"
                ),
            }

            for label, image_url in cases.items():
                with self.subTest(label=label), self.assertRaises(EditableVideoJobError) as raised:
                    write_editable_video_project(
                        artifact=_artifact(image_srcs=[image_url]),
                        runs_dir=runs_dir,
                        editor_assets_dir=editor_assets,
                        source_run_dir=source_run_dir,
                        run_id=f"render-{label}",
                        run_dir=runs_dir / f"render-{label}",
                        cancellation_token=CancellationToken.never(f"render-{label}"),
                    )


    def test_run_asset_copy_uses_the_validated_open_file_after_path_swap(self) -> None:
        import autodesign.editable_video_job as editable_video_job

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs_dir = root / "runs"
            source_run_dir = runs_dir / "source"
            source = source_run_dir / "final" / "figure.png"
            victim = runs_dir / "victim" / "final" / "secret.png"
            editor_assets = root / "editor-assets"
            source.parent.mkdir(parents=True)
            victim.parent.mkdir(parents=True)
            editor_assets.mkdir(parents=True)
            source.write_bytes(b"validated-public-image")
            victim.write_bytes(b"private-image-after-swap")
            original_namer = editable_video_job._asset_destination_name

            def name_then_swap(*args: object, **kwargs: object) -> str:
                name = original_namer(*args, **kwargs)
                source.unlink()
                source.symlink_to(victim)
                return name

            with mock.patch.object(
                editable_video_job,
                "_asset_destination_name",
                side_effect=name_then_swap,
            ):
                result = editable_video_job.write_editable_video_project(
                    artifact=_artifact(
                        image_srcs=["/api/files/runs/source/final/figure.png"]
                    ),
                    runs_dir=runs_dir,
                    editor_assets_dir=editor_assets,
                    source_run_dir=source_run_dir,
                    run_id="render-path-swap",
                    run_dir=runs_dir / "render-path-swap",
                    cancellation_token=CancellationToken.never("render-path-swap"),
                )

            staged = next(
                layer["src"]
                for layer in result["manifest"]["scenes"][0]["layers"]
                if layer["kind"] == "image"
            )
            self.assertEqual(
                (Path(result["project_dir"]) / staged).read_bytes(),
                b"validated-public-image",
            )

    def test_runs_root_cannot_be_used_as_source_run_authority(self) -> None:
        from autodesign.editable_video_job import EditableVideoJobError, write_editable_video_project

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs_dir = root / "runs"
            other_run = runs_dir / "other"
            editor_assets = root / "editor-assets"
            other_run.mkdir(parents=True)
            editor_assets.mkdir(parents=True)
            (other_run / "secret.png").write_bytes(b"not-authorized")

            with self.assertRaises(EditableVideoJobError) as raised:
                write_editable_video_project(
                    artifact=_artifact(image_srcs=["/api/files/runs/other/secret.png"]),
                    runs_dir=runs_dir,
                    editor_assets_dir=editor_assets,
                    source_run_dir=runs_dir,
                    run_id="render-broad-authority",
                    run_dir=runs_dir / "render-broad-authority",
                    cancellation_token=CancellationToken.never("render-broad-authority"),
                )

            self.assertEqual(raised.exception.code, "invalid_source_run")

    def test_asset_names_do_not_collide_when_run_paths_flatten_the_same(self) -> None:
        from autodesign.editable_video_job import write_editable_video_project

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs_dir = root / "runs"
            source_run_dir = runs_dir / "source"
            editor_assets = root / "editor-assets"
            (source_run_dir / "a").mkdir(parents=True)
            editor_assets.mkdir(parents=True)
            (source_run_dir / "a" / "b.png").write_bytes(b"nested")
            (source_run_dir / "a-b.png").write_bytes(b"flat")

            result = write_editable_video_project(
                artifact=_artifact(
                    image_srcs=[
                        "/api/files/runs/source/a/b.png",
                        "/api/files/runs/source/a-b.png",
                    ]
                ),
                runs_dir=runs_dir,
                editor_assets_dir=editor_assets,
                source_run_dir=source_run_dir,
                run_id="render-collisions",
                run_dir=runs_dir / "render-collisions",
                cancellation_token=CancellationToken.never("render-collisions"),
            )

            manifest = result["manifest"]
            sources = [layer["src"] for layer in manifest["scenes"][0]["layers"] if layer["kind"] == "image"]
            self.assertEqual(len(sources), 2)
            self.assertEqual(len(set(sources)), 2)
            self.assertEqual(
                {(Path(result["project_dir"]) / source).read_bytes() for source in sources},
                {b"nested", b"flat"},
            )

    def test_index_has_no_remote_runtime_or_script_css_injection(self) -> None:
        from autodesign.editable_video_job import write_editable_video_project

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs_dir = root / "runs"
            source_run_dir = runs_dir / "source"
            editor_assets = root / "editor-assets"
            source_run_dir.mkdir(parents=True)
            editor_assets.mkdir(parents=True)
            artifact = _artifact()
            artifact["video_project"]["scenes"][0]["scene_id"] = "</script><script>alert(1)</script>\u2028\u2029&>"
            title = artifact["layers"][1]
            title["font_family"] = "url(https://evil.test/font)"
            title["effects"] = {"fill": "URL(https://evil.test/pixel)"}

            result = write_editable_video_project(
                artifact=artifact,
                runs_dir=runs_dir,
                editor_assets_dir=editor_assets,
                source_run_dir=source_run_dir,
                run_id="render-safe-html",
                run_dir=runs_dir / "render-safe-html",
                cancellation_token=CancellationToken.never("render-safe-html"),
            )

            index = (Path(result["project_dir"]) / "index.html").read_text()
            self.assertNotIn("http://", index.lower())
            self.assertNotIn("https://", index.lower())
            self.assertNotIn("url(", index.lower())
            self.assertNotIn("</script><script>alert(1)", index)
            self.assertNotIn("evil.test", index)
            self.assertIn("\\u003c/script\\u003e", index)
            self.assertIn("\\u2028", index)
            self.assertIn("\\u2029", index)

    def test_css_escape_cannot_reconstruct_remote_asset_url(self) -> None:
        from autodesign.editable_video_job import write_editable_video_project

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs_dir = root / "runs"
            source_run_dir = runs_dir / "source"
            editor_assets = root / "editor-assets"
            source_run_dir.mkdir(parents=True)
            editor_assets.mkdir()
            artifact = _artifact()
            artifact["layers"][0]["fill_color"] = r"\72l(\68ttps://evil.test/pixel)"
            artifact["layers"][1]["font_family"] = '"Times New Roman", Times, Georgia, serif'

            result = write_editable_video_project(
                artifact=artifact,
                runs_dir=runs_dir,
                editor_assets_dir=editor_assets,
                source_run_dir=source_run_dir,
                run_id="escaped-css",
                run_dir=runs_dir / "escaped-css",
                cancellation_token=CancellationToken.never("escaped-css"),
            )

            index = (Path(result["project_dir"]) / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("evil.test", index)
            self.assertNotIn(r"\72l", index)
            self.assertIn('font-family:&quot;Times New Roman&quot;, Times, Georgia, serif', index)
            self.assertIn(
                "default-src 'none'; img-src 'self' data:; media-src 'self' data:; connect-src 'none'",
                index,
            )

    def test_editable_video_cancel_during_promotion_leaves_no_final(self) -> None:
        from autodesign.editable_video_job import _render_editable_video

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir = root / "project"
            renders = project_dir / "renders"
            renders.mkdir(parents=True)
            run_dir = root / "run"
            run_dir.mkdir()
            token_event = threading.Event()
            token = CancellationToken(store=None, run_id="render", signal_event=token_event)
            final = renders / "editable-123.mp4"
            final.write_bytes(b"existing-final")

            def fake_render(request: object) -> SimpleNamespace:
                command = getattr(request, "command")
                output = Path(command[command.index("--output") + 1])
                (project_dir / output).write_bytes(b"new-final")
                return SimpleNamespace(
                    status="ok", reason="completed", returncode=0, timed_out=False,
                    elapsed_s=0.01, stdout="", stderr="",
                )

            real_replace = os.replace

            def cancel_after_replace(source: object, destination: object) -> None:
                real_replace(source, destination)
                if Path(destination) == final:
                    token_event.set()

            with mock.patch("autodesign.editable_video_job.run_external_author_process", fake_render), \
                 mock.patch("autodesign.editable_video_job.time.time_ns", return_value=123), \
                 mock.patch("autodesign.editable_video_job.os.replace", side_effect=cancel_after_replace):
                with self.assertRaises(RunCancelled):
                    _render_editable_video(
                        project_dir=project_dir,
                        run_dir=run_dir,
                        run_id="render",
                        fps=24,
                        token=token,
                        render_command=(sys.executable, "-c", "pass"),
                        timeout_s=1,
                    )

            self.assertEqual(final.read_bytes(), b"existing-final")

    def test_renderer_receives_minimal_environment_without_unrelated_secret(self) -> None:
        from autodesign.editable_video_job import run_editable_video_job

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs_dir = root / "runs"
            source_run_dir = runs_dir / "source"
            run_dir = runs_dir / "render-env"
            editor_assets = root / "editor-assets"
            editor_assets.mkdir(parents=True)
            source_run_dir.mkdir(parents=True)
            renderer = root / "renderer_env.py"
            renderer.write_text(
                """
import os, pathlib, sys
args = sys.argv[1:]
out = pathlib.Path(args[args.index('--output') + 1])
pathlib.Path('env-secret.txt').write_text(os.environ.get('AUTODESIGN_UNRELATED_SECRET', '<absent>'))
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(b'mp4')
""".strip(),
                encoding="utf-8",
            )

            with mock.patch.dict(
                "os.environ",
                {"AUTODESIGN_UNRELATED_SECRET": "must-not-leak"},
                clear=False,
            ):
                result = run_editable_video_job(
                    artifact=_artifact(),
                    runs_dir=runs_dir,
                    editor_assets_dir=editor_assets,
                    source_run_dir=source_run_dir,
                    run_id="render-env",
                    run_dir=run_dir,
                    cancellation_token=CancellationToken.never("render-env"),
                    render_command=(sys.executable, str(renderer)),
                )

            self.assertTrue(Path(result["mp4_path"]).is_file())
            self.assertEqual(
                set(result),
                {"run_id", "project_dir", "mp4_path", "fps", "render"},
            )
            self.assertEqual(
                (run_dir / "hyperframes-editable-demo" / "env-secret.txt").read_text(),
                "<absent>",
            )

    def test_cancelled_render_kills_descendants_and_never_publishes_mp4(self) -> None:
        from autodesign.editable_video_job import run_editable_video_job

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs_dir = root / "runs"
            source_run_dir = runs_dir / "source"
            run_dir = runs_dir / "render-cancel"
            editor_assets = root / "editor-assets"
            editor_assets.mkdir(parents=True)
            source_run_dir.mkdir(parents=True)
            renderer = root / "renderer.py"
            renderer.write_text(
                """
import pathlib, subprocess, sys, time
args = sys.argv[1:]
out = pathlib.Path(args[args.index('--output') + 1])
child = subprocess.Popen([sys.executable, '-c',
    \"import pathlib,time; time.sleep(1.2); pathlib.Path('descendant-finished').write_text('late')\"])
pathlib.Path('child.pid').write_text(str(child.pid))
time.sleep(1.4)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(b'late-mp4')
""".strip(),
                encoding="utf-8",
            )
            cancelled = threading.Event()
            token = CancellationToken(store=None, run_id="render-cancel", signal_event=cancelled)
            outcome: list[BaseException | dict[str, object]] = []

            def execute() -> None:
                try:
                    outcome.append(
                        run_editable_video_job(
                            artifact=_artifact(),
                            runs_dir=runs_dir,
                            editor_assets_dir=editor_assets,
                            source_run_dir=source_run_dir,
                            run_id="render-cancel",
                            run_dir=run_dir,
                            cancellation_token=token,
                            render_command=(sys.executable, str(renderer)),
                            render_timeout_s=10,
                        )
                    )
                except BaseException as exc:  # expected RunCancelled crosses threads
                    outcome.append(exc)

            worker = threading.Thread(target=execute)
            worker.start()
            child_pid_path = run_dir / "hyperframes-editable-demo" / "child.pid"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not child_pid_path.exists():
                time.sleep(0.02)
            self.assertTrue(child_pid_path.exists(), "render fixture did not spawn its descendant")
            child_pid = int(child_pid_path.read_text())
            cancelled.set()
            worker.join(timeout=8)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], RunCancelled)
            snapshot = ProcessLedger(run_dir).read()
            for record in snapshot.processes:
                self.assertFalse(process_is_alive(record.identity), record)
            with self.assertRaises(ProcessLookupError):
                process_identity(child_pid)
            self.assertFalse(any(run_dir.rglob("*.mp4")))
            cancelled_snapshot = _digest_tree(run_dir)
            time.sleep(1.5)
            self.assertFalse((run_dir / "hyperframes-editable-demo" / "descendant-finished").exists())
            self.assertFalse(any(run_dir.rglob("*.mp4")))
            self.assertEqual(_digest_tree(run_dir), cancelled_snapshot)


if __name__ == "__main__":
    unittest.main()
