from __future__ import annotations

import importlib
import tempfile
import threading
import time
import unittest
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as real_wait
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fitz

from autodesign.run_control import CancellationToken, RunCancelled
from autodesign.tools._contract import ToolContext
from autodesign.util.pdf import PdfFigureCandidate, PdfTableCandidate


ingest_document = importlib.import_module("autodesign.tools.ingest_document")


class _RecordingThreadPoolExecutor(ThreadPoolExecutor):
    instances: list["_RecordingThreadPoolExecutor"] = []

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.submitted = 0
        self.submit_lock = threading.Lock()
        self.__class__.instances.append(self)

    def submit(self, fn, /, *args, **kwargs):
        with self.submit_lock:
            self.submitted += 1
        return super().submit(fn, *args, **kwargs)


class _FakePixmap:
    def __init__(self, on_save=None) -> None:
        self._on_save = on_save

    def save(self, path: str) -> None:
        Path(path).write_bytes(b"fake-png")
        if self._on_save is not None:
            self._on_save()


class _FakePage:
    def __init__(self, on_render=None, on_save=None) -> None:
        self._on_render = on_render
        self._on_save = on_save

    def get_pixmap(self, *, dpi: int):
        del dpi
        if self._on_render is not None:
            self._on_render()
        return _FakePixmap(self._on_save)


class _FakeDocument:
    def __init__(self, pages: list[_FakePage]) -> None:
        self._pages = pages

    def __len__(self) -> int:
        return len(self._pages)

    def __getitem__(self, index: int) -> _FakePage:
        return self._pages[index]


class _VectorDocument:
    def __init__(self, page) -> None:
        self.page = page

    def __len__(self) -> int:
        return 1

    def __getitem__(self, index: int):
        if index != 0:
            raise IndexError(index)
        return self.page

    def close(self) -> None:
        return None


class _CancelAfterPhaseToken:
    def __init__(self, phase_suffix: str) -> None:
        self.run_id = "ingest-cancellation-test"
        self.phase_suffix = phase_suffix
        self.cancelled = False

    def is_cancelled(self) -> bool:
        return self.cancelled

    def raise_if_cancelled(self, phase: str) -> None:
        if self.cancelled:
            raise RunCancelled(self.run_id, phase)
        if phase.endswith(self.phase_suffix):
            self.cancelled = True


class IngestCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.layers_dir = self.root / "layers"
        self.layers_dir.mkdir()
        _RecordingThreadPoolExecutor.instances.clear()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _context(self) -> tuple[ToolContext, threading.Event]:
        cancel_event = threading.Event()
        token = CancellationToken(
            store=None,
            run_id="ingest-cancellation-test",
            signal_event=cancel_event,
        )
        ctx = ToolContext(
            settings=SimpleNamespace(
                ingest_model="test-model",
                ingest_http_timeout=1.0,
                poster_harness_mode="production",
            ),
            run_dir=self.root,
            layers_dir=self.layers_dir,
            run_id="ingest-cancellation-test",
            cancellation_token=token,
        )
        return ctx, cancel_event

    def _figure_candidates(self, count: int) -> list[PdfFigureCandidate]:
        candidates = []
        for index in range(count):
            path = self.layers_dir / f"figure_{index}.png"
            path.write_bytes(b"fake-png")
            candidates.append(PdfFigureCandidate(
                page=index + 1,
                bbox_pt=None,
                path=path,
                width_px=320,
                height_px=200,
                strategy="raster",
                xref=index + 1,
            ))
        return candidates

    def _table_candidates(self, count: int) -> list[PdfTableCandidate]:
        candidates = []
        for index in range(count):
            path = self.layers_dir / f"table_{index}.png"
            path.write_bytes(b"fake-png")
            candidates.append(PdfTableCandidate(
                page=index + 1,
                bbox_pt=(10.0, 20.0, 300.0, 180.0),
                image_path=path,
                width_px=580,
                height_px=320,
                raw_cells=[["metric", "value"]],
                nrows=1,
                ncols=2,
            ))
        return candidates

    def _run_blocked_case(self, case: str) -> None:
        ctx, cancel_event = self._context()
        first_started = threading.Event()
        release_worker = threading.Event()
        caller_finished = threading.Event()
        starts: list[int] = []
        outcome: dict[str, object] = {}
        events: list[str] = []

        def block(index: int) -> None:
            starts.append(index)
            first_started.set()
            release_worker.wait(timeout=2.0)

        if case == "ocr":
            doc = _FakeDocument([_FakePage() for _ in range(5)])

            def blocked_vlm(**kwargs):
                del kwargs
                page_index = len(starts)
                block(page_index)
                return {"text": f"page {page_index}"}

            invoke = lambda: ingest_document._ocr_scanned_pdf(
                self.root / "paper.pdf", doc, ctx,
            )
            worker_patch = patch.object(
                ingest_document, "vlm_call_json", side_effect=blocked_vlm,
            )
        elif case == "caption":
            candidates = self._figure_candidates(5)
            manifest = {
                "figures": [
                    {"page": index + 1, "caption": f"Figure {index + 1}"}
                    for index in range(5)
                ],
            }

            def blocked_match(index, *_args):
                block(index)
                return {
                    "matched_idx": index,
                    "confidence": 0.9,
                    "is_real_figure": True,
                    "reason": "fixture",
                    "caption_text": f"Figure {index + 1}",
                    "short_caption": "fixture",
                    "sub_panels": [],
                }

            invoke = lambda: ingest_document._match_captions_parallel(
                candidates, manifest, ctx,
            )
            worker_patch = patch.object(
                ingest_document, "_match_one_caption", side_effect=blocked_match,
            )
        else:
            candidates = self._table_candidates(5)

            def blocked_parse(index, *_args):
                block(index)
                return {
                    "is_table": True,
                    "headers": ["metric", "value"],
                    "rows": [["accuracy", "90"]],
                    "title": f"Table {index + 1}",
                    "matched_idx": index,
                    "caption_text": f"Table {index + 1}",
                    "reason": "fixture",
                }

            invoke = lambda: ingest_document._parse_tables_parallel(
                candidates, {"tables": []}, ctx,
            )
            worker_patch = patch.object(
                ingest_document, "_parse_one_table", side_effect=blocked_parse,
            )

        def run() -> None:
            try:
                outcome["result"] = invoke()
            except BaseException as exc:
                outcome["exception"] = exc
            finally:
                caller_finished.set()

        def capture_log(event: str, **_payload) -> None:
            events.append(event)

        with patch.dict("os.environ", {"INGEST_VLM_PARALLELISM": "1"}), \
                patch.object(ingest_document, "log", side_effect=capture_log), \
                patch.object(
                    ingest_document,
                    "ThreadPoolExecutor",
                    _RecordingThreadPoolExecutor,
                ), \
                worker_patch:
            caller = threading.Thread(target=run, name=f"cancel-{case}")
            caller.start()
            try:
                self.assertTrue(first_started.wait(timeout=1.0))
                self.assertEqual(starts, [0], "only one bounded task may be in flight")
                cancel_event.set()
                self.assertTrue(
                    caller_finished.wait(timeout=0.5),
                    "the caller must poll cancellation without waiting for a blocked worker",
                )
                events_at_return = list(events)
                paths_at_return = {
                    path.name: (path.stat().st_size, path.stat().st_mtime_ns)
                    for path in self.layers_dir.iterdir()
                }
                self.assertIsInstance(outcome.get("exception"), RunCancelled)
                self.assertNotIn("result", outcome)
                self.assertEqual(starts, [0])
            finally:
                release_worker.set()
                caller.join(timeout=1.0)

            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if all(
                    not thread.is_alive()
                    for executor in _RecordingThreadPoolExecutor.instances
                    for thread in executor._threads
                ):
                    break
                threading.Event().wait(0.005)
            self.assertTrue(
                all(
                    not thread.is_alive()
                    for executor in _RecordingThreadPoolExecutor.instances
                    for thread in executor._threads
                ),
                "released executor workers must exit before late-mutation checks",
            )
            stable_deadline = time.monotonic() + 0.05
            while time.monotonic() < stable_deadline:
                self.assertEqual(starts, [0], "queued futures must never start after cancellation")
                self.assertEqual(events, events_at_return, "workers must not emit late logs")
                self.assertEqual(
                    {
                        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
                        for path in self.layers_dir.iterdir()
                    },
                    paths_at_return,
                    "workers must not perform late layer writes",
                )
                threading.Event().wait(0.005)
            self.assertFalse(any(event.endswith(".done") for event in events))
            self.assertFalse(any(event.endswith(".summary") for event in events))

    def test_all_ingest_pools_bound_work_and_stop_without_late_merges(self) -> None:
        for case in ("ocr", "caption", "table"):
            with self.subTest(case=case):
                self._run_blocked_case(case)

    def test_all_ingest_pools_reject_cancellation_before_any_submit(self) -> None:
        for case in ("ocr", "caption", "table"):
            with self.subTest(case=case):
                ctx, cancel_event = self._context()
                cancel_event.set()
                if case == "ocr":
                    worker_patch = patch.object(ingest_document, "vlm_call_json")
                    invoke = lambda: ingest_document._ocr_scanned_pdf(
                        self.root / "paper.pdf",
                        _FakeDocument([_FakePage()]),
                        ctx,
                    )
                elif case == "caption":
                    worker_patch = patch.object(ingest_document, "_match_one_caption")
                    invoke = lambda: ingest_document._match_captions_parallel(
                        self._figure_candidates(1),
                        {"figures": [{"page": 1, "caption": "Figure 1"}]},
                        ctx,
                    )
                else:
                    worker_patch = patch.object(ingest_document, "_parse_one_table")
                    invoke = lambda: ingest_document._parse_tables_parallel(
                        self._table_candidates(1),
                        {"tables": []},
                        ctx,
                    )
                with worker_patch as worker:
                    with self.assertRaises(RunCancelled):
                        invoke()
                worker.assert_not_called()

    def test_all_ingest_pools_never_submit_more_than_parallelism(self) -> None:
        for case in ("ocr", "caption", "table"):
            with self.subTest(case=case):
                _RecordingThreadPoolExecutor.instances.clear()
                ctx, cancel_event = self._context()
                release = threading.Event()
                two_started = threading.Event()
                starts: list[int] = []
                lock = threading.Lock()
                outcome: dict[str, object] = {}

                def block(index: int) -> None:
                    with lock:
                        starts.append(index)
                        if len(starts) == 2:
                            two_started.set()
                    release.wait(timeout=2.0)

                if case == "ocr":
                    def worker(**kwargs):
                        index = int(str(kwargs["user_text"]).split()[1]) - 1
                        block(index)
                        return {"text": str(index)}

                    worker_patch = patch.object(
                        ingest_document, "vlm_call_json", side_effect=worker,
                    )
                    invoke = lambda: ingest_document._ocr_scanned_pdf(
                        self.root / "paper.pdf",
                        _FakeDocument([_FakePage() for _ in range(6)]),
                        ctx,
                    )
                elif case == "caption":
                    def worker(index, *_args):
                        block(index)
                        return {
                            "matched_idx": index,
                            "confidence": 0.9,
                            "is_real_figure": True,
                            "reason": "fixture",
                            "caption_text": str(index),
                            "short_caption": str(index),
                            "sub_panels": [],
                        }

                    worker_patch = patch.object(
                        ingest_document, "_match_one_caption", side_effect=worker,
                    )
                    invoke = lambda: ingest_document._match_captions_parallel(
                        self._figure_candidates(6),
                        {"figures": [{"page": i + 1, "caption": str(i)} for i in range(6)]},
                        ctx,
                    )
                else:
                    def worker(index, *_args):
                        block(index)
                        return {"is_table": True, "headers": [], "rows": [], "reason": "fixture"}

                    worker_patch = patch.object(
                        ingest_document, "_parse_one_table", side_effect=worker,
                    )
                    invoke = lambda: ingest_document._parse_tables_parallel(
                        self._table_candidates(6), {"tables": []}, ctx,
                    )

                def run() -> None:
                    try:
                        outcome["result"] = invoke()
                    except BaseException as exc:
                        outcome["exception"] = exc

                with patch.dict("os.environ", {"INGEST_VLM_PARALLELISM": "2"}), \
                        patch.object(
                            ingest_document,
                            "ThreadPoolExecutor",
                            _RecordingThreadPoolExecutor,
                        ), worker_patch:
                    caller = threading.Thread(target=run)
                    caller.start()
                    try:
                        self.assertTrue(two_started.wait(timeout=1.0))
                        submitted_before_cancel = _RecordingThreadPoolExecutor.instances[0].submitted
                        cancel_event.set()
                        deadline = time.monotonic() + 0.5
                        while caller.is_alive() and time.monotonic() < deadline:
                            caller.join(timeout=0.005)
                    finally:
                        release.set()
                        caller.join(timeout=1.0)

                self.assertLessEqual(submitted_before_cancel, 2)
                self.assertEqual(sorted(starts), [0, 1])
                self.assertIsInstance(outcome.get("exception"), RunCancelled)

    def test_all_ingest_pools_check_after_completion_before_parent_merge(self) -> None:
        for case in ("ocr", "caption", "table"):
            with self.subTest(case=case):
                ctx, cancel_event = self._context()

                def cancel_when_completed(*args, **kwargs):
                    done, pending = real_wait(*args, **kwargs)
                    if done:
                        cancel_event.set()
                    return done, pending

                if case == "ocr":
                    worker_patch = patch.object(
                        ingest_document, "vlm_call_json", return_value={"text": "late"},
                    )
                    invoke = lambda: ingest_document._ocr_scanned_pdf(
                        self.root / "paper.pdf", _FakeDocument([_FakePage()]), ctx,
                    )
                elif case == "caption":
                    worker_patch = patch.object(
                        ingest_document,
                        "_match_one_caption",
                        return_value={
                            "matched_idx": 0,
                            "confidence": 0.9,
                            "is_real_figure": True,
                            "reason": "late",
                            "caption_text": "late",
                            "short_caption": "late",
                            "sub_panels": [],
                        },
                    )
                    invoke = lambda: ingest_document._match_captions_parallel(
                        self._figure_candidates(1),
                        {"figures": [{"page": 1, "caption": "Figure 1"}]},
                        ctx,
                    )
                else:
                    worker_patch = patch.object(
                        ingest_document,
                        "_parse_one_table",
                        return_value={"is_table": True, "headers": [], "rows": [], "reason": "late"},
                    )
                    invoke = lambda: ingest_document._parse_tables_parallel(
                        self._table_candidates(1), {"tables": []}, ctx,
                    )

                events: list[str] = []
                with patch.object(
                    ingest_document, "wait", side_effect=cancel_when_completed, create=True,
                ), patch.object(
                    ingest_document, "log", side_effect=lambda event, **_payload: events.append(event),
                ), worker_patch:
                    with self.assertRaises(RunCancelled):
                        invoke()
                self.assertFalse(any(event.endswith(".done") for event in events))
                self.assertFalse(any(event.endswith(".summary") for event in events))

    def test_ocr_render_and_save_boundaries_stop_the_page_prepass(self) -> None:
        for cancel_at in ("render", "save"):
            with self.subTest(cancel_at=cancel_at):
                ctx, cancel_event = self._context()
                saved: list[int] = []

                def make_page(index: int) -> _FakePage:
                    def on_render() -> None:
                        if index == 0 and cancel_at == "render":
                            cancel_event.set()

                    def on_save() -> None:
                        saved.append(index)
                        if index == 0 and cancel_at == "save":
                            cancel_event.set()

                    return _FakePage(on_render=on_render, on_save=on_save)

                doc = _FakeDocument([make_page(index) for index in range(3)])
                with patch.object(ingest_document, "vlm_call_json") as vlm:
                    with self.assertRaises(RunCancelled):
                        ingest_document._ocr_scanned_pdf(
                            self.root / "paper.pdf", doc, ctx,
                        )

                vlm.assert_not_called()
                if cancel_at == "render":
                    self.assertEqual(saved, [])
                    self.assertFalse((self.layers_dir / "ingest_ocr_page_001.png").exists())
                else:
                    self.assertEqual(saved, [0])
                    self.assertTrue((self.layers_dir / "ingest_ocr_page_001.png").exists())
                self.assertFalse((self.layers_dir / "ingest_ocr_page_002.png").exists())

    def test_registration_rejects_cancel_before_rename_or_state_merge(self) -> None:
        for case in ("figure", "table"):
            with self.subTest(case=case):
                ctx, cancel_event = self._context()
                pdf_path = self.root / f"{case}.pdf"
                pdf_path.write_bytes(b"not-a-real-pdf")
                cancel_event.set()
                if case == "figure":
                    candidate = self._figure_candidates(1)[0]
                    invoke = lambda: ingest_document._register_candidates(
                        candidates=[candidate],
                        matches={0: {
                            "is_real_figure": True,
                            "caption_text": "Figure 1",
                            "confidence": 0.9,
                        }},
                        ctx=ctx,
                        pdf_path=pdf_path,
                    )
                    final_path = self.layers_dir / "img_ingest_fig_01.png"
                    source_path = candidate.path
                else:
                    candidate = self._table_candidates(1)[0]
                    invoke = lambda: ingest_document._register_tables(
                        candidates=[candidate],
                        parsed={0: {
                            "is_table": True,
                            "headers": ["metric", "value"],
                            "rows": [["accuracy", "90"]],
                            "caption_text": "Table 1",
                        }},
                        ctx=ctx,
                        pdf_path=pdf_path,
                        manifest={"tables": []},
                    )
                    final_path = self.layers_dir / "img_ingest_table_01.png"
                    source_path = candidate.image_path

                with self.assertRaises(RunCancelled):
                    invoke()
                self.assertTrue(source_path.exists())
                self.assertFalse(final_path.exists())
                self.assertEqual(ctx.state["rendered_layers"], {})

    def test_vector_refine_render_and_save_have_cancellation_barriers(self) -> None:
        for cancel_at in ("render", "save"):
            with self.subTest(cancel_at=cancel_at):
                ctx, cancel_event = self._context()
                pdf_path = self.root / f"vector-{cancel_at}.pdf"
                pdf_path.write_bytes(b"not-a-real-pdf")
                source_path = self.layers_dir / f"vector-{cancel_at}.png"
                source_path.write_bytes(b"original")
                saved: list[str] = []

                class Pixmap:
                    width = 300
                    height = 180

                    def save(self, path: str) -> None:
                        Path(path).write_bytes(b"refined")
                        saved.append(path)
                        if cancel_at == "save":
                            cancel_event.set()

                class Page:
                    rect = fitz.Rect(0, 0, 600, 800)

                    def get_pixmap(self, *, clip, dpi: int):
                        del clip, dpi
                        if cancel_at == "render":
                            cancel_event.set()
                        return Pixmap()

                candidate = PdfFigureCandidate(
                    page=1,
                    bbox_pt=(20.0, 80.0, 320.0, 260.0),
                    path=source_path,
                    width_px=600,
                    height_px=360,
                    strategy="vector",
                    xref=None,
                )
                events: list[str] = []
                with patch.object(
                    ingest_document, "fitz",
                ) as fitz_module, patch.object(
                    ingest_document,
                    "_matched_caption_block",
                    return_value=fitz.Rect(20.0, 220.0, 320.0, 245.0),
                ), patch.object(
                    ingest_document, "_vector_upper_page_noise_trim_y", return_value=None,
                ), patch.object(
                    ingest_document,
                    "_vector_lower_text_boundary",
                    return_value=fitz.Rect(20.0, 220.0, 320.0, 245.0),
                ), patch.object(
                    ingest_document, "_caption_supports_horizontal_crop", return_value=False,
                ), patch.object(
                    ingest_document, "_refined_bbox_is_useful", return_value=True,
                ), patch.object(
                    ingest_document, "_pdf_crop_quality_flags", return_value=[],
                ), patch.object(
                    ingest_document, "log", side_effect=lambda event, **_payload: events.append(event),
                ):
                    fitz_module.open.return_value = _VectorDocument(Page())
                    fitz_module.Rect.side_effect = fitz.Rect
                    with self.assertRaises(RunCancelled):
                        ingest_document._register_candidates(
                            candidates=[candidate],
                            matches={0: {
                                "is_real_figure": True,
                                "caption_text": "Figure 1",
                                "confidence": 0.9,
                            }},
                            ctx=ctx,
                            pdf_path=pdf_path,
                        )

                if cancel_at == "render":
                    self.assertEqual(saved, [])
                    self.assertEqual(source_path.read_bytes(), b"original")
                else:
                    self.assertEqual(saved, [str(source_path)])
                    self.assertEqual(source_path.read_bytes(), b"refined")
                self.assertNotIn("ingest.pdf.vector_crop.refined", events)
                self.assertEqual(ctx.state["rendered_layers"], {})
                self.assertFalse((self.layers_dir / "img_ingest_fig_01.png").exists())

    def test_pdf_prepass_cancel_after_page_text_stops_all_native_writes(self) -> None:
        ctx, cancel_event = self._context()
        pdf_path = self.root / "native-prepass.pdf"
        pdf_path.write_bytes(b"not-a-real-pdf")
        fake_doc = _VectorDocument(SimpleNamespace())

        def extract_text(_doc):
            cancel_event.set()
            return ["paper body"]

        with patch.object(
            ingest_document, "page_count", return_value=1,
        ), patch.object(
            ingest_document.fitz, "open", return_value=fake_doc,
        ), patch.object(
            ingest_document, "detect_scanned_pdf", return_value=False,
        ), patch.object(
            ingest_document, "extract_page_text", side_effect=extract_text,
        ), patch.object(
            ingest_document, "extract_embedded_rasters",
        ) as extract_rasters, patch.object(
            ingest_document, "extract_vector_clusters",
        ) as extract_vectors, patch.object(
            ingest_document, "extract_table_candidates",
        ) as extract_tables, patch.object(
            ingest_document, "render_page_png",
        ) as render_cover, patch.object(
            ingest_document, "discover_captioned_visual_groups",
        ) as discover_groups, patch.object(
            ingest_document, "recover_caption_anchored_visuals",
        ) as recover_groups:
            with self.assertRaises(RunCancelled):
                ingest_document._ingest_pdf(pdf_path, ctx)

        extract_rasters.assert_not_called()
        extract_vectors.assert_not_called()
        extract_tables.assert_not_called()
        render_cover.assert_not_called()
        discover_groups.assert_not_called()
        recover_groups.assert_not_called()

    def test_per_item_broad_handlers_do_not_downgrade_run_cancelled(self) -> None:
        for case in ("ocr", "caption", "table"):
            with self.subTest(case=case):
                ctx, _cancel_event = self._context()
                expected = RunCancelled(ctx.run_id, f"{case}.worker")
                if case == "ocr":
                    worker_patch = patch.object(
                        ingest_document, "vlm_call_json", side_effect=expected,
                    )
                    invoke = lambda: ingest_document._ocr_scanned_pdf(
                        self.root / "paper.pdf", _FakeDocument([_FakePage()]), ctx,
                    )
                elif case == "caption":
                    worker_patch = patch.object(
                        ingest_document, "_match_one_caption", side_effect=expected,
                    )
                    invoke = lambda: ingest_document._match_captions_parallel(
                        self._figure_candidates(1),
                        {"figures": [{"page": 1, "caption": "Figure 1"}]},
                        ctx,
                    )
                else:
                    worker_patch = patch.object(
                        ingest_document, "_parse_one_table", side_effect=expected,
                    )
                    invoke = lambda: ingest_document._parse_tables_parallel(
                        self._table_candidates(1), {"tables": []}, ctx,
                    )
                with worker_patch:
                    with self.assertRaises(RunCancelled) as caught:
                        invoke()
                self.assertIs(caught.exception, expected)

    def test_noncancelled_pool_behavior_preserves_order_and_failure_degradation(self) -> None:
        ctx, _cancel_event = self._context()
        with patch.dict("os.environ", {"INGEST_VLM_PARALLELISM": "2"}):
            ocr_calls = 0
            ocr_lock = threading.Lock()

            def ocr_vlm(**kwargs):
                nonlocal ocr_calls
                with ocr_lock:
                    ocr_calls += 1
                index = int(str(kwargs["user_text"]).split()[1]) - 1
                if index == 0:
                    threading.Event().wait(0.02)
                if index == 1:
                    raise RuntimeError("ocr fixture failure")
                return {"text": f"page-{index}"}

            with patch.object(ingest_document, "vlm_call_json", side_effect=ocr_vlm):
                page_texts = ingest_document._ocr_scanned_pdf(
                    self.root / "paper.pdf",
                    _FakeDocument([_FakePage() for _ in range(3)]),
                    ctx,
                )
            self.assertEqual(page_texts, ["page-0", "", "page-2"])

            def caption_worker(index, *_args):
                if index == 1:
                    raise RuntimeError("caption fixture failure")
                return {
                    "matched_idx": index,
                    "confidence": 0.9,
                    "is_real_figure": True,
                    "reason": "fixture",
                    "caption_text": f"Figure {index + 1}",
                    "short_caption": str(index),
                    "sub_panels": [],
                }

            with patch.object(
                ingest_document, "_match_one_caption", side_effect=caption_worker,
            ):
                caption_results = ingest_document._match_captions_parallel(
                    self._figure_candidates(3),
                    {"figures": [{"page": i + 1, "caption": f"Figure {i + 1}"} for i in range(3)]},
                    ctx,
                )
            self.assertEqual(set(caption_results), {0, 1, 2})
            self.assertEqual(caption_results[0]["matched_idx"], 0)
            self.assertEqual(caption_results[1]["caption_association_method"], "unmatched")
            self.assertEqual(caption_results[2]["matched_idx"], 2)

            def table_worker(index, *_args):
                if index == 1:
                    raise RuntimeError("table fixture failure")
                return {"is_table": True, "headers": [str(index)], "rows": [[str(index)]]}

            with patch.object(
                ingest_document, "_parse_one_table", side_effect=table_worker,
            ):
                table_results = ingest_document._parse_tables_parallel(
                    self._table_candidates(3), {"tables": []}, ctx,
                )
            self.assertEqual(set(table_results), {0, 1, 2})
            self.assertTrue(table_results[0]["is_table"])
            self.assertFalse(table_results[1]["is_table"])
            self.assertIn("table fixture failure", table_results[1]["reason"])
            self.assertTrue(table_results[2]["is_table"])

    def test_each_ingest_vlm_call_receives_the_context_token(self) -> None:
        for case in ("structure", "ocr", "caption", "table"):
            with self.subTest(case=case):
                ctx, _cancel_event = self._context()
                received_tokens: list[object] = []

                def fake_vlm(**kwargs):
                    received_tokens.append(kwargs.get("cancellation_token"))
                    if case == "structure":
                        return {"title": "Fixture"}
                    if case == "ocr":
                        return {"text": "page text"}
                    if case == "caption":
                        return {
                            "matched_idx": 0,
                            "confidence": 0.9,
                            "is_real_figure": True,
                            "reason": "fixture",
                            "short_caption": "figure",
                            "sub_panels": [],
                        }
                    return {
                        "is_table": True,
                        "headers": ["metric", "value"],
                        "rows": [["accuracy", "90"]],
                        "title": "Table 1",
                        "matched_idx": 0,
                        "reason": "fixture",
                    }

                with patch.dict("os.environ", {"INGEST_VLM_PARALLELISM": "1"}), \
                        patch.object(ingest_document, "vlm_call_json", side_effect=fake_vlm):
                    if case == "structure":
                        ingest_document._extract_structure(
                            ["paper body"], None, ctx, self.root / "paper.pdf",
                        )
                    elif case == "ocr":
                        ingest_document._ocr_scanned_pdf(
                            self.root / "paper.pdf",
                            _FakeDocument([_FakePage()]),
                            ctx,
                        )
                    elif case == "caption":
                        ingest_document._match_captions_parallel(
                            self._figure_candidates(1),
                            {"figures": [{"page": 1, "caption": "Figure 1"}]},
                            ctx,
                        )
                    else:
                        ingest_document._parse_tables_parallel(
                            self._table_candidates(1),
                            {"tables": [{"page": 1, "caption": "Table 1"}]},
                            ctx,
                        )

                self.assertEqual(received_tokens, [ctx.cancellation_token])

    def test_ingest_agents_receive_the_context_token(self) -> None:
        ctx, _cancel_event = self._context()
        source = self.root / "paper.txt"
        source.write_text("fixture", encoding="utf-8")
        ctx.state["deck_plan"] = {"status": "draft"}

        with patch.object(
            ingest_document, "_ingest_markdown", return_value={
                "file": str(source),
                "type": "markdown",
                "registered_layer_ids": [],
                "raw_text": "fixture",
            },
        ), patch.object(
            ingest_document, "refine_canvas_plan_from_ingest", return_value=None,
        ), patch.object(
            ingest_document, "should_refine_deck_plan", return_value=True,
        ), patch.object(
            ingest_document, "_ensure_paper_memory_dossier", return_value={},
        ), patch.object(
            ingest_document, "build_paper_visual_storyboard", return_value={},
        ), patch.object(
            ingest_document, "should_auto_discover_paper_resources", return_value=False,
        ), patch.object(
            ingest_document, "_build_poster_content_brief", return_value={},
        ), patch.object(
            ingest_document, "build_poster_plan_contract", return_value={},
        ), patch.object(ingest_document, "DeckOutlineAgent") as deck_agent:
            deck_agent.return_value.plan.return_value = ctx.state["deck_plan"]
            result = ingest_document.ingest_document(
                {"file_paths": [str(source)]}, ctx=ctx,
            )

        self.assertEqual(result.status, "ok")
        self.assertIs(
            deck_agent.return_value.plan.call_args.kwargs.get("cancellation_token"),
            ctx.cancellation_token,
        )

        paper_memory = {
            "kind": "paper_memory",
            "cache_key": "fixture-cache-key",
            "chunk_count": 1,
        }
        with patch.object(
            ingest_document, "read_paper_memory_dossier_cache", return_value=None,
        ), patch.object(
            ingest_document, "PaperMemoryAgent",
        ) as paper_memory_agent:
            paper_memory_agent.return_value.build.return_value = {}
            dossier = ingest_document._ensure_paper_memory_dossier(
                ctx=ctx,
                paper_memory=paper_memory,
                paper_manifest={},
                paper_visual_provenance={},
                recommended_text_units={},
                recommended_figures={},
            )

        self.assertEqual(dossier, {})
        self.assertIs(
            paper_memory_agent.return_value.build.call_args.kwargs.get("cancellation_token"),
            ctx.cancellation_token,
        )

    def test_paper_memory_cancellation_after_build_blocks_cache_write(self) -> None:
        token = _CancelAfterPhaseToken("paper_memory_agent.after_build")
        ctx = ToolContext(
            settings=SimpleNamespace(
                paper_memory_model="test-model",
                enable_paper_memory_agent=True,
            ),
            run_dir=self.root,
            layers_dir=self.layers_dir,
            run_id=token.run_id,
            cancellation_token=token,
        )
        with patch.object(
            ingest_document, "read_paper_memory_dossier_cache", return_value=None,
        ), patch.object(
            ingest_document, "PaperMemoryAgent",
        ) as paper_memory_agent, patch.object(
            ingest_document, "write_paper_memory_dossier_cache",
        ) as write_cache:
            paper_memory_agent.return_value.build.return_value = {"sections": [{"title": "A"}]}
            with self.assertRaises(RunCancelled):
                ingest_document._ensure_paper_memory_dossier(
                    ctx=ctx,
                    paper_memory={
                        "kind": "paper_memory",
                        "cache_key": "fixture-cache-key",
                        "chunk_count": 1,
                    },
                    paper_manifest={},
                    paper_visual_provenance={},
                    recommended_text_units={},
                    recommended_figures={},
                )

        write_cache.assert_not_called()


if __name__ == "__main__":
    unittest.main()
