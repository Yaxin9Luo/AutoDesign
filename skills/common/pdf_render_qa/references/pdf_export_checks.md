# PDF export checks

Render the current export after meaningful layout changes. Inspect spacing, typography, headers/footers, tables, figure sharpness, page numbers, section transitions, clipped text, overlap, unreadable glyphs, black squares, missing captions, and placeholder tokens.

For paper posters, verify `poster.pdf` agrees with `preview.png` and `poster.html` in size, identity marks, source figures, and title-band layout. A fallback-produced PDF remains reviewable but must retain its fallback warning.

Repair at the HTML/CSS/page-size or source-metadata layer. Do not flatten editable text or tables just to conceal export defects. If a dependency is unavailable, report it and keep the PNG preview as the temporary review artifact.
