from __future__ import annotations

import tempfile
from pathlib import Path

from bs4 import BeautifulSoup

from scripts.web_server import _patch_html_for_apply_edits


def _soup(path: Path) -> BeautifulSoup:
    return BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "poster.html"
        dst = root / "patched.html"
        src.write_text(
            """<!doctype html>
<html><head><style>
.paper-poster{width:800px;height:520px}
.poster-columns{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}
</style></head><body>
<main class="paper-poster">
  <div class="poster-columns" data-block-id="columns">
    <section class="poster-column" data-block-id="col_a" data-column-id="left">
      <section class="poster-section" data-block-id="s1"><p data-block-id="body">Original</p></section>
      <section class="poster-section" data-block-id="s2"><h2>Second</h2></section>
    </section>
    <section class="poster-column" data-block-id="col_b" data-column-id="middle">
      <section class="poster-section" data-block-id="s3"><h2>Third</h2></section>
    </section>
    <section class="poster-column" data-block-id="col_c" data-column-id="right">
      <section class="poster-section" data-block-id="s4"><h2>Fourth</h2></section>
    </section>
  </div>
</main>
</body></html>""",
            encoding="utf-8",
        )

        _patch_html_for_apply_edits(
            src,
            dst,
            {
                "layers": {"body": {"text": "Changed"}},
                "layout": [
                    {"kind": "section_height", "section_id": "s2", "height_px": 140},
                    {"kind": "column_widths", "columns_id": "columns", "widths": [25, 50, 25]},
                    {
                        "kind": "section_order",
                        "columns": [
                            {"column_id": "left", "section_ids": ["s1"]},
                            {"column_id": "middle", "section_ids": ["s2", "s3"]},
                            {"column_id": "right", "section_ids": ["s4"]},
                        ],
                    },
                ],
            },
        )
        doc = _soup(dst)
        assert doc.find(attrs={"data-block-id": "body"}).get_text(strip=True) == "Changed"
        s2 = doc.find(attrs={"data-block-id": "s2"})
        assert "height:140px" in str(s2.get("style"))
        assert "min-height:140px" in str(s2.get("style"))
        columns_style = str(doc.find(attrs={"data-block-id": "columns"}).get("style"))
        assert "grid-template-columns:25% 50% 25%" in columns_style
        middle_ids = [
            child.get("data-block-id")
            for child in doc.find(attrs={"data-column-id": "middle"}).find_all(recursive=False)
        ]
        assert middle_ids == ["s2", "s3"]

        legacy_dst = root / "legacy.html"
        _patch_html_for_apply_edits(src, legacy_dst, {"body": {"text": "Legacy changed"}})
        legacy_doc = _soup(legacy_dst)
        assert legacy_doc.find(attrs={"data-block-id": "body"}).get_text(strip=True) == "Legacy changed"

    print("flow layout edit smoke passed")


if __name__ == "__main__":
    main()
