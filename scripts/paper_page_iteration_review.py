#!/usr/bin/env python3
"""Build the multi-agent review packet for a paper project page batch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from autodesign.util.paper_page_diagnostics import (
    DEFAULT_REFERENCE_URLS,
    build_paper_page_iteration_review,
    write_iteration_review,
)


def _pages_from_batch_summary(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    pages: list[str] = []
    for result in data.get("results") or []:
        if not isinstance(result, dict):
            continue
        html_path = str(result.get("html_path") or "").strip()
        if html_path:
            pages.append(html_path)
    return pages


def _pages_from_root(path: Path) -> list[str]:
    if path.is_file() and path.name == "index.html":
        return [str(path)]
    return [str(item) for item in sorted(path.glob("*/index.html"))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--page", action="append", default=[], help="Generated index.html to review.")
    parser.add_argument("--page-root", action="append", default=[], help="Directory containing */index.html pages.")
    parser.add_argument("--batch-summary", default=None, help="paper_page_batch_loop batch_summary.json.")
    parser.add_argument("--reference", action="append", default=[])
    parser.add_argument("--reference-manifest", default=None, help="Optional paper_page_reference_harness manifest.json.")
    parser.add_argument("--label", default="paper_page_iteration")
    parser.add_argument("--out", default="out/diagnostics/paper_page_iteration_review")
    args = parser.parse_args(argv)

    pages: list[str] = list(args.page)
    if args.batch_summary:
        pages.extend(_pages_from_batch_summary(Path(args.batch_summary)))
    for root in args.page_root:
        pages.extend(_pages_from_root(Path(root)))
    pages = [page for page in dict.fromkeys(pages) if Path(page).exists()]
    if not pages:
        print("no existing pages found; pass --page, --page-root, or --batch-summary", file=sys.stderr)
        return 2

    references = args.reference or DEFAULT_REFERENCE_URLS
    report = build_paper_page_iteration_review(
        pages,
        references=references,
        reference_manifest=args.reference_manifest,
        label=args.label,
    )
    paths = write_iteration_review(report, args.out)
    print(json.dumps(paths, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
