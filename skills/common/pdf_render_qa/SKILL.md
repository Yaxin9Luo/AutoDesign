# PDF Render QA

Use for source or exported PDFs. Treat rendered pages as layout evidence; text extraction alone is insufficient.

## Stage: enhance

Preserve page, figure/table, caption, and crop provenance in the brief. Require rendered-page inspection rather than relying on extracted text alone.

## Stage: plan

Plan from rendered pages and selected crops; choose physical page size early. Keep review files under `tmp/pdfs/`. Read `pdf_render_commands` for supported tools.

## Stage: critique

Render the current export and inspect clipping, tables, figures, glyphs, headers, page transitions, and physical page size. Treat visible defects as export findings.

## Stage: repair

Fix HTML/CSS and source metadata before rasterization, then re-render. Report unavailable dependencies exactly. Read `pdf_export_checks`.
