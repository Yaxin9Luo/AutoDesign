from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import fitz

from autodesign.tools._contract import ToolContext
from autodesign.util.paper_source_sanity import (
    PaperSourceInputError,
    assert_valid_paper_source_pdf,
    inspect_paper_source_pdf,
)


ingest_module = importlib.import_module("autodesign.tools.ingest_document")


def _insert_panel(
    page: fitz.Page,
    *,
    x: float,
    y: float,
    width: float,
    heading: str,
    body: str,
) -> None:
    page.insert_text((x, y), heading, fontsize=24, fontname="helv")
    page.insert_textbox(
        fitz.Rect(x, y + 18, x + width, y + 175),
        body,
        fontsize=16,
        fontname="helv",
        lineheight=1.25,
    )


def _write_poster_pdf(path: Path) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=2304, height=1152)
    page.insert_text((70, 72), "PosterNet: Visual Evidence at Scale", fontsize=48)
    columns = (70.0, 825.0, 1580.0)
    headings = (
        ("1 Motivation", "The problem requires a compact visual summary."),
        ("2 Method", "Our method combines structured evidence and retrieval."),
        ("3 Data and Benchmark", "We evaluate on several representative datasets."),
        ("4 Results", "The approach improves accuracy and runtime."),
        ("5 Analysis", "Ablations isolate the contribution of each component."),
        ("6 Takeaways", "The central result is robust across settings."),
    )
    for index, (heading, body) in enumerate(headings):
        column = index % 3
        row = index // 3
        _insert_panel(
            page,
            x=columns[column],
            y=150 + row * 430,
            width=650,
            heading=heading,
            body=" ".join([body] * 8),
        )
    doc.set_metadata({"creator": "Chromium", "producer": "Skia/PDF m149"})
    doc.save(path)
    doc.close()
    return path


def _write_short_paper_pdf(
    path: Path,
    *,
    width: float,
    height: float,
    include_images: bool = False,
    include_abstract_references: bool = True,
    metadata: dict[str, str] | None = None,
) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    margin = 42.0
    gutter = 22.0
    column_width = (width - 2 * margin - gutter) / 2
    page.insert_text((margin, 42), "A Compact Theory of Reliable Systems", fontsize=19)
    page.insert_text((margin, 62), "Ada Researcher and Lin Scientist", fontsize=10)
    abstract = (
        "Abstract\nWe give a concise account of a reliable system and its assumptions.\n\n"
        if include_abstract_references
        else ""
    )
    references = (
        "References\n[1] A. Researcher. Reliable systems. 2025."
        if include_abstract_references
        else "Closing Note\nThe argument is complete without visual assets."
    )
    left = abstract + (
        "1 Introduction\n"
        "Short papers retain a continuous article reading order even when they "
        "use two columns. The argument develops through prose rather than panels.\n\n"
        "2 Method\n"
        "The method follows from three definitions and a deterministic proof."
    )
    right = (
        "3 Results\n"
        "The theorem bounds the error under the stated assumptions. The result "
        "does not require figures or object crops.\n\n"
        "4 Discussion\n"
        "The main limitation is the finite-sample assumption.\n\n"
        f"{references}"
    )
    page.insert_textbox(
        fitz.Rect(margin, 82, margin + column_width, height - margin),
        left,
        fontsize=10,
        lineheight=1.25,
    )
    page.insert_textbox(
        fitz.Rect(margin + column_width + gutter, 82, width - margin, height - margin),
        right,
        fontsize=10,
        lineheight=1.25,
    )
    if include_images:
        page.draw_rect(fitz.Rect(margin, height - 180, margin + 160, height - 80))
    if metadata:
        doc.set_metadata(metadata)
    doc.save(path)
    doc.close()
    return path


def _context(tmp_path: Path) -> ToolContext:
    layers_dir = tmp_path / "layers"
    layers_dir.mkdir(exist_ok=True)
    return ToolContext(
        settings=SimpleNamespace(),
        run_dir=tmp_path,
        layers_dir=layers_dir,
        run_id="paper-source-sanity-test",
    )


def test_generated_panelized_poster_is_hard_rejected(tmp_path: Path) -> None:
    pdf_path = _write_poster_pdf(tmp_path / "generated-poster.pdf")

    report = inspect_paper_source_pdf(pdf_path)

    assert report["single_page"] is True
    assert report["large_poster_canvas"] is True
    assert report["panelized_multi_region_layout"] is True
    assert report["hard_reject"] is True
    assert report["classification"] == "poster_like"
    assert report["page_count"] == 1
    assert report["layout"]["occupied_region_count"] >= 3
    assert report["layout"]["panel_heading_count"] >= 4


def test_assertion_error_carries_repair_contract(tmp_path: Path) -> None:
    pdf_path = _write_poster_pdf(tmp_path / "generated-poster.pdf")

    caught: ValueError | None = None
    try:
        assert_valid_paper_source_pdf(pdf_path)
    except ValueError as error:
        caught = error
    else:
        raise AssertionError("generated poster PDF was not rejected")

    assert caught is not None
    assert caught.issue_id == "paper_source_generated_poster_detected"
    assert caught.repair_route == "replace_paper_source"
    assert caught.report["hard_reject"] is True
    assert "generated poster PDF" in str(caught)


def test_real_one_page_short_papers_are_not_rejected(
    tmp_path: Path,
    name: str,
    width: float,
    height: float,
) -> None:
    pdf_path = _write_short_paper_pdf(
        tmp_path / name,
        width=width,
        height=height,
    )

    report = inspect_paper_source_pdf(pdf_path)

    assert report["single_page"] is True
    assert report["large_poster_canvas"] is False
    assert report["hard_reject"] is False
    assert_valid_paper_source_pdf(pdf_path)


def test_no_image_theory_paper_is_not_rejected(tmp_path: Path) -> None:
    pdf_path = _write_short_paper_pdf(
        tmp_path / "no-image-theory.pdf",
        width=612.0,
        height=792.0,
        include_images=False,
    )

    report = inspect_paper_source_pdf(pdf_path)

    assert report["content"]["image_count"] == 0
    assert report["hard_reject"] is False


def test_weak_signals_do_not_hard_reject_without_large_panelized_canvas(
    tmp_path: Path,
) -> None:
    pdf_path = _write_short_paper_pdf(
        tmp_path / "poster.pdf",
        width=595.0,
        height=842.0,
        include_abstract_references=False,
        metadata={"creator": "Chromium", "producer": "Skia/PDF m149"},
    )

    report = inspect_paper_source_pdf(pdf_path)

    assert report["metadata"]["filename_mentions_poster"] is True
    assert "Skia" in report["metadata"]["producer"]
    assert report["content"]["has_abstract"] is False
    assert report["content"]["has_references"] is False
    assert report["hard_reject"] is False


def test_ingest_rejects_required_poster_before_any_cache_lookup(
    tmp_path: Path,
) -> None:
    pdf_path = _write_poster_pdf(tmp_path / "generated-poster.pdf")
    ctx = _context(tmp_path)
    ctx.state["paper_source_sanity_required"] = True
    ctx.state["paper_visual_storyboard"] = {"selected_assets": []}

    with patch.object(
        ingest_module,
        "_cached_ingest_summary",
        side_effect=AssertionError("state cache lookup must not run"),
    ) as state_cache, patch.object(
        ingest_module,
        "_cached_pdf_ingest_summary",
        side_effect=AssertionError("persistent cache lookup must not run"),
    ) as pdf_cache:
        result = ingest_module.ingest_document(
            {"file_paths": [str(pdf_path)]},
            ctx=ctx,
        )

    state_cache.assert_not_called()
    pdf_cache.assert_not_called()
    assert result.status == "error"
    assert result.error_category == "validation"
    assert result.payload["issue_id"] == "paper_source_generated_poster_detected"
    assert result.payload["repair_route"] == "replace_paper_source"


def test_ingest_rejects_required_poster_before_reuse_lookup(tmp_path: Path) -> None:
    pdf_path = _write_poster_pdf(tmp_path / "generated-poster.pdf")
    ctx = _context(tmp_path)
    ctx.state["paper_source_sanity_required"] = True
    ctx.state["reuse_ingest_run"] = str(tmp_path / "old-run")

    with patch.object(
        ingest_module,
        "_reuse_ingest_run_if_requested",
        side_effect=AssertionError("reuse lookup must not run"),
    ) as reuse:
        result = ingest_module.ingest_document({"file_paths": [str(pdf_path)]}, ctx=ctx)

    reuse.assert_not_called()
    assert result.status == "error"
    assert result.payload["issue_id"] == "paper_source_generated_poster_detected"


def test_unreadable_pdf_raises_typed_input_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")

    caught: PaperSourceInputError | None = None
    try:
        inspect_paper_source_pdf(broken)
    except PaperSourceInputError as error:
        caught = error
    else:
        raise AssertionError("unreadable PDF was not rejected")

    assert caught is not None
    assert caught.issue_id == "paper_source_unreadable"
    assert caught.report["hard_reject"] is False
    assert caught.report["input_rejected"] is True


def test_common_poster_from_pdf_brief_enables_sanity_check() -> None:
    from autodesign.runner import _paper_source_sanity_required

    assert _paper_source_sanity_required(
        "Make a poster from the attached PDF",
        [Path("paper.pdf")],
        reference_poster=None,
    )


def test_large_three_column_short_article_is_not_mistaken_for_poster(tmp_path: Path) -> None:
    path = tmp_path / "large-three-column-article.pdf"
    doc = fitz.open()
    page = doc.new_page(width=1200, height=900)
    for column, x in enumerate((45.0, 445.0, 845.0), start=1):
        page.insert_text((x, 70), f"{column} Article Section", fontsize=11)
        page.insert_textbox(
            fitz.Rect(x, 90, x + 310, 360),
            "Continuous article prose follows the reading order. " * 20,
            fontsize=10,
            lineheight=1.25,
        )
        page.insert_text((x, 430), f"{column + 3} Further Analysis", fontsize=11)
        page.insert_textbox(
            fitz.Rect(x, 450, x + 310, 780),
            "The argument continues as dense academic prose. " * 20,
            fontsize=10,
            lineheight=1.25,
        )
    doc.save(path)
    doc.close()

    report = inspect_paper_source_pdf(path)

    assert report["single_page"] is True
    assert report["large_poster_canvas"] is True
    assert report["layout"]["median_font_size_pt"] < 12
    assert report["hard_reject"] is False


def test_article_flow_signal_protects_12pt_large_three_column_paper(tmp_path: Path) -> None:
    path = tmp_path / "large-article.pdf"
    doc = fitz.open()
    page = doc.new_page(width=1200, height=900)
    for column, x in enumerate((45.0, 445.0, 845.0), start=1):
        first_heading = "Abstract" if column == 1 else f"{column} Method"
        second_heading = "References" if column == 3 else f"{column + 3} Results"
        page.insert_text((x, 70), first_heading, fontsize=14)
        page.insert_textbox(
            fitz.Rect(x, 90, x + 310, 360),
            "Continuous paper prose follows a normal reading order. " * 16,
            fontsize=12,
            lineheight=1.2,
        )
        page.insert_text((x, 430), second_heading, fontsize=14)
        page.insert_textbox(
            fitz.Rect(x, 450, x + 310, 780),
            "The academic argument continues with citations and evidence. " * 16,
            fontsize=12,
            lineheight=1.2,
        )
    doc.save(path)
    doc.close()

    report = inspect_paper_source_pdf(path)

    assert report["panelized_multi_region_layout"] is True
    assert report["article_flow_signal"] is True
    assert report["hard_reject"] is False


def test_reuse_source_helper_recovers_recorded_original_pdf(tmp_path: Path) -> None:
    from autodesign.runner import _paper_source_attachments_from_reuse

    paper = _write_short_paper_pdf(tmp_path / "paper.pdf", width=612, height=792)
    source_run = tmp_path / "runs" / "source-run"
    source_run.mkdir(parents=True)
    (source_run / "resume_state.json").write_text(
        '{"attachments": ["' + str(paper) + '"]}',
        encoding="utf-8",
    )

    assert _paper_source_attachments_from_reuse(tmp_path, "source-run") == [paper]


def test_ingest_skips_authority_when_sanity_is_not_required(tmp_path: Path) -> None:
    pdf_path = _write_poster_pdf(tmp_path / "general-workflow-poster.pdf")
    ctx = _context(tmp_path)

    class CacheReached(RuntimeError):
        pass

    with patch.object(
        ingest_module,
        "_cached_ingest_summary",
        side_effect=CacheReached("general PDF workflow reached cache lookup"),
    ):
        try:
            ingest_module.ingest_document({"file_paths": [str(pdf_path)]}, ctx=ctx)
        except CacheReached:
            pass
        else:
            raise AssertionError("general PDF workflow did not reach cache lookup")


def test_empty_selected_assets_does_not_reject_valid_theory_paper(
    tmp_path: Path,
) -> None:
    pdf_path = _write_short_paper_pdf(
        tmp_path / "no-image-theory.pdf",
        width=612.0,
        height=792.0,
        include_images=False,
    )
    ctx = _context(tmp_path)
    ctx.state["paper_source_sanity_required"] = True
    ctx.state["paper_visual_storyboard"] = {"selected_assets": []}

    class CacheReached(RuntimeError):
        pass

    with patch.object(
        ingest_module,
        "_cached_ingest_summary",
        side_effect=CacheReached("valid theory paper reached cache lookup"),
    ):
        try:
            ingest_module.ingest_document({"file_paths": [str(pdf_path)]}, ctx=ctx)
        except CacheReached:
            pass
        else:
            raise AssertionError("valid theory paper did not reach cache lookup")


def test_unreadable_pdf_returns_structured_source_input_error(tmp_path: Path) -> None:
    pdf_path = tmp_path / "unreadable.pdf"
    pdf_path.write_bytes(b"not a pdf")
    ctx = _context(tmp_path)
    ctx.state["paper_source_sanity_required"] = True

    with patch.object(
        ingest_module,
        "_cached_ingest_summary",
        side_effect=AssertionError("cache lookup must not run for unreadable input"),
    ) as cache:
        result = ingest_module.ingest_document({"file_paths": [str(pdf_path)]}, ctx=ctx)

    cache.assert_not_called()
    assert result.status == "error"
    assert result.payload["issue_id"] == "paper_source_unreadable"


class PaperSourceSanityTests(unittest.TestCase):
    def _with_tmp(self, test, *args: object) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            test(Path(raw_tmp), *args)

    def test_generated_panelized_poster_is_hard_rejected(self) -> None:
        self._with_tmp(test_generated_panelized_poster_is_hard_rejected)

    def test_assertion_error_carries_repair_contract(self) -> None:
        self._with_tmp(test_assertion_error_carries_repair_contract)

    def test_real_one_page_short_papers_are_not_rejected(self) -> None:
        for name, width, height in (
            ("a4-one-page.pdf", 595.0, 842.0),
            ("landscape-short-paper.pdf", 842.0, 595.0),
        ):
            with self.subTest(name=name):
                self._with_tmp(
                    test_real_one_page_short_papers_are_not_rejected,
                    name,
                    width,
                    height,
                )

    def test_no_image_theory_paper_is_not_rejected(self) -> None:
        self._with_tmp(test_no_image_theory_paper_is_not_rejected)

    def test_weak_signals_do_not_hard_reject(self) -> None:
        self._with_tmp(
            test_weak_signals_do_not_hard_reject_without_large_panelized_canvas
        )

    def test_ingest_rejects_poster_before_cache_lookup(self) -> None:
        self._with_tmp(test_ingest_rejects_required_poster_before_any_cache_lookup)

    def test_ingest_rejects_poster_before_reuse_lookup(self) -> None:
        self._with_tmp(test_ingest_rejects_required_poster_before_reuse_lookup)

    def test_unreadable_pdf_raises_typed_input_error(self) -> None:
        self._with_tmp(test_unreadable_pdf_raises_typed_input_error)

    def test_common_poster_brief_enables_sanity_check(self) -> None:
        test_common_poster_from_pdf_brief_enables_sanity_check()

    def test_large_three_column_article_is_not_rejected(self) -> None:
        self._with_tmp(test_large_three_column_short_article_is_not_mistaken_for_poster)

    def test_article_flow_protects_large_three_column_paper(self) -> None:
        self._with_tmp(test_article_flow_signal_protects_12pt_large_three_column_paper)

    def test_reuse_source_recovers_original_pdf(self) -> None:
        self._with_tmp(test_reuse_source_helper_recovers_recorded_original_pdf)

    def test_ingest_skips_authority_when_not_required(self) -> None:
        self._with_tmp(test_ingest_skips_authority_when_sanity_is_not_required)

    def test_empty_selected_assets_accepts_theory_paper(self) -> None:
        self._with_tmp(test_empty_selected_assets_does_not_reject_valid_theory_paper)

    def test_unreadable_pdf_returns_structured_error(self) -> None:
        self._with_tmp(test_unreadable_pdf_returns_structured_source_input_error)
