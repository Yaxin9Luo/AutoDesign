from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from autodesign.agents.external_code_editor import CodeEditorResult
from autodesign.run_control import RunCancelled
from autodesign.util.browser_render import BrowserRenderResult
from scripts.web_server import _run_poster_code_edit_sync
from tests.test_run_worker_protocol import _settings


class _CancellationProbe:
    def __init__(self) -> None:
        self.cancelled = False

    def raise_if_cancelled(self, phase: str) -> None:
        if self.cancelled:
            raise RunCancelled("edit", phase)


def _write_poster(path: Path, *, asset_refs: tuple[str, ...] = ()) -> None:
    images = "".join(f'<img src="{value}">' for value in asset_refs)
    path.write_text(
        '<main class="paper-poster" style="width:1600px;height:900px">'
        f"{images}</main>",
        encoding="utf-8",
    )


class PosterCodeEditPromotionTests(unittest.TestCase):
    def test_same_source_and_derived_run_is_rejected_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            settings = _settings(root)
            runs_dir = settings.out_dir / "runs"
            source_run_dir = runs_dir / "same"
            source_final_dir = source_run_dir / "final"
            source_final_dir.mkdir(parents=True)
            source_poster = source_final_dir / "poster.html"
            source_poster.write_text("SOURCE", encoding="utf-8")
            original_mtime = source_poster.stat().st_mtime_ns

            with patch(
                "autodesign.poster_code_edit.ExternalCodeEditor.run"
            ) as editor, self.assertRaisesRegex(ValueError, "must differ"):
                _run_poster_code_edit_sync(
                    run_id="same",
                    source_run_id="same",
                    source_run_dir=source_run_dir,
                    source_poster_path=source_poster,
                    artifact={"artifact_id": "art_same"},
                    instruction="edit",
                    conversation_history=[],
                    selection_context=None,
                    required_color_system={},
                    settings=settings,
                )

            editor.assert_not_called()
            self.assertEqual(source_poster.read_text(encoding="utf-8"), "SOURCE")
            self.assertEqual(source_poster.stat().st_mtime_ns, original_mtime)

    def test_cancellation_during_asset_promotion_does_not_start_next_copy(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            settings = _settings(root)
            runs_dir = settings.out_dir / "runs"
            source_run_dir = runs_dir / "source"
            source_final_dir = source_run_dir / "final"
            source_final_dir.mkdir(parents=True)
            source_poster = source_final_dir / "poster.html"
            _write_poster(source_poster)

            attempt_dir = runs_dir / "edit" / "code_editor" / "attempt_01"
            assets_dir = attempt_dir / "assets"
            assets_dir.mkdir(parents=True)
            (assets_dir / "a.png").write_bytes(b"a")
            (assets_dir / "b.png").write_bytes(b"b")
            edited_poster = attempt_dir / "poster.html"
            _write_poster(
                edited_poster,
                asset_refs=("assets/a.png", "assets/b.png"),
            )
            editor_result = CodeEditorResult(
                attempt_dir=attempt_dir,
                poster_path=edited_poster,
            )
            token = _CancellationProbe()
            real_copy2 = shutil.copy2

            def copy_and_cancel(source: Path, target: Path, *args: object, **kwargs: object):
                copied = real_copy2(source, target, *args, **kwargs)
                if Path(source).name == "a.png":
                    token.cancelled = True
                return copied

            with patch(
                "autodesign.poster_code_edit.ExternalCodeEditor.run",
                return_value=editor_result,
            ), patch(
                "autodesign.poster_code_edit.shutil.copy2",
                side_effect=copy_and_cancel,
            ), self.assertRaises(RunCancelled):
                _run_poster_code_edit_sync(
                    run_id="edit",
                    source_run_id="source",
                    source_run_dir=source_run_dir,
                    source_poster_path=source_poster,
                    artifact={"artifact_id": "art_source"},
                    instruction="tighten spacing",
                    conversation_history=[],
                    selection_context=None,
                    required_color_system={},
                    settings=settings,
                    cancellation_token=token,
                )

            final_dir = runs_dir / "edit" / "final"
            self.assertTrue((final_dir / "assets" / "a.png").is_file())
            self.assertFalse((final_dir / "assets" / "b.png").exists())
            self.assertFalse((final_dir / "code_editor_revision_manifest.json").exists())

    def test_cancellation_during_recursive_source_copy_stops_remaining_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            settings = _settings(root)
            runs_dir = settings.out_dir / "runs"
            source_run_dir = runs_dir / "source"
            source_final_dir = source_run_dir / "final"
            layers_dir = source_final_dir / "layers"
            layers_dir.mkdir(parents=True)
            (layers_dir / "a.png").write_bytes(b"a")
            (layers_dir / "b.png").write_bytes(b"b")
            source_poster = source_final_dir / "poster.html"
            _write_poster(source_poster)

            attempt_dir = runs_dir / "edit" / "code_editor" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            edited_poster = attempt_dir / "poster.html"
            _write_poster(edited_poster)
            editor_result = CodeEditorResult(
                attempt_dir=attempt_dir,
                poster_path=edited_poster,
            )
            token = _CancellationProbe()
            real_copy2 = shutil.copy2

            def copy_and_cancel(source: Path, target: Path, *args: object, **kwargs: object):
                copied = real_copy2(source, target, *args, **kwargs)
                if Path(source) == layers_dir / "a.png":
                    token.cancelled = True
                return copied

            with patch(
                "autodesign.poster_code_edit.ExternalCodeEditor.run",
                return_value=editor_result,
            ), patch(
                "autodesign.poster_code_edit.shutil.copy2",
                side_effect=copy_and_cancel,
            ), self.assertRaises(RunCancelled):
                _run_poster_code_edit_sync(
                    run_id="edit",
                    source_run_id="source",
                    source_run_dir=source_run_dir,
                    source_poster_path=source_poster,
                    artifact={"artifact_id": "art_source"},
                    instruction="tighten spacing",
                    conversation_history=[],
                    selection_context=None,
                    required_color_system={},
                    settings=settings,
                    cancellation_token=token,
                )

            final_dir = runs_dir / "edit" / "final"
            self.assertTrue((final_dir / "layers" / "a.png").is_file())
            self.assertFalse((final_dir / "layers" / "b.png").exists())
            self.assertFalse((final_dir / "poster.html").exists())
            self.assertFalse((final_dir / "code_editor_revision_manifest.json").exists())

    def test_revision_context_ancestry_uses_explicit_runs_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            settings = _settings(root)
            runs_dir = settings.out_dir / "runs"
            grandparent_dir = runs_dir / "grandparent"
            grandparent_dir.mkdir(parents=True)
            source_run_dir = runs_dir / "source"
            source_final_dir = source_run_dir / "final"
            source_final_dir.mkdir(parents=True)
            (source_final_dir / "code_editor_revision_manifest.json").write_text(
                json.dumps({"parent_run_id": "grandparent"}),
                encoding="utf-8",
            )
            source_poster = source_final_dir / "poster.html"
            _write_poster(source_poster)

            attempt_dir = runs_dir / "edit" / "code_editor" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            edited_poster = attempt_dir / "poster.html"
            _write_poster(edited_poster)
            editor_result = CodeEditorResult(
                attempt_dir=attempt_dir,
                poster_path=edited_poster,
            )
            seen_context: list[Path] = []

            def edit(**kwargs: object) -> CodeEditorResult:
                seen_context.extend(kwargs["context_run_dirs"])  # type: ignore[arg-type]
                return editor_result

            def screenshot(_source: Path, target: Path, **_kwargs: object) -> BrowserRenderResult:
                target.write_bytes(b"preview")
                return BrowserRenderResult(
                    backend="test",
                    width_px=1600,
                    height_px=900,
                )

            with patch(
                "autodesign.poster_code_edit.ExternalCodeEditor.run",
                side_effect=edit,
            ), patch(
                "autodesign.poster_code_edit.screenshot_html",
                side_effect=screenshot,
            ):
                _run_poster_code_edit_sync(
                    run_id="edit",
                    source_run_id="source",
                    source_run_dir=source_run_dir,
                    source_poster_path=source_poster,
                    artifact={"artifact_id": "art_source"},
                    instruction="tighten spacing",
                    conversation_history=[],
                    selection_context=None,
                    required_color_system={},
                    settings=settings,
                )

            self.assertEqual(
                seen_context,
                [source_run_dir, grandparent_dir],
            )

    def test_legacy_wrapper_builds_complete_final_and_selection_summary(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            settings = _settings(root)
            runs_dir = settings.out_dir / "runs"
            source_run_dir = runs_dir / "source"
            source_final_dir = source_run_dir / "final"
            source_final_dir.mkdir(parents=True)
            source_poster = source_final_dir / "poster.html"
            _write_poster(source_poster)

            attempt_dir = runs_dir / "edit" / "code_editor" / "attempt_01"
            asset_dir = attempt_dir / "assets"
            asset_dir.mkdir(parents=True)
            (asset_dir / "figure.png").write_bytes(b"figure")
            edited_poster = attempt_dir / "poster.html"
            _write_poster(edited_poster, asset_refs=("assets/figure.png",))
            editor_result = CodeEditorResult(
                attempt_dir=attempt_dir,
                poster_path=edited_poster,
                validation_summary={"ok": True},
            )

            def screenshot(_source: Path, target: Path, **_kwargs: object) -> BrowserRenderResult:
                target.write_bytes(b"preview")
                return BrowserRenderResult(
                    backend="test",
                    width_px=1600,
                    height_px=900,
                )

            with patch(
                "autodesign.poster_code_edit.ExternalCodeEditor.run",
                return_value=editor_result,
            ), patch(
                "autodesign.poster_code_edit.screenshot_html",
                side_effect=screenshot,
            ):
                result = _run_poster_code_edit_sync(
                    run_id="edit",
                    source_run_id="source",
                    source_run_dir=source_run_dir,
                    source_poster_path=source_poster,
                    artifact={"artifact_id": "art_source"},
                    instruction="tighten spacing",
                    conversation_history=[],
                    selection_context={"block_id": "results"},
                    required_color_system={},
                    settings=settings,
                )

            final_dir = runs_dir / "edit" / "final"
            manifest = json.loads(
                (final_dir / "code_editor_revision_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue((final_dir / "poster.html").is_file())
            self.assertTrue((final_dir / "preview.png").is_file())
            self.assertEqual((final_dir / "assets" / "figure.png").read_bytes(), b"figure")
            self.assertEqual(result["promoted_assets"], ["assets/figure.png"])
            self.assertEqual(
                result["selection_context_summary"],
                {"block_id": "results"},
            )
            self.assertEqual(
                manifest["selection_context_summary"],
                {"block_id": "results"},
            )


if __name__ == "__main__":
    unittest.main()
