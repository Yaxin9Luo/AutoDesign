# PDF render commands

Render PDF pages to PNGs for layout inspection. Prefer `pdftoppm` when available; otherwise use repository PyMuPDF helpers. Use `pdfplumber` or `pypdf` only for quick text checks, never as proof of layout fidelity. Keep intermediate review files in `tmp/pdfs/` and final artifacts in the run output.

When dependencies are missing, report them rather than skipping QA:

```bash
uv pip install reportlab pdfplumber pypdf
brew install poppler
pdftoppm -png "$INPUT_PDF" "$OUTPUT_PREFIX"
```

Preserve page, figure/table, caption, crop, and physical page-size provenance. Use ASCII hyphens in PDF-facing text.
