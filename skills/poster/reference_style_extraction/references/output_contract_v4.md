# Output contract v4

Produce `reference_style_analysis.json`, `reference_style_blueprint.html`,
`reference_style_agent_review.json`, and `reference_style_agent_done.json`.
Use the exact target canvas from `reference_source_metadata.json`.

## Analysis JSON

Write one JSON object with this shape. Replace every placeholder with measured
values from `reference.png`; do not copy the example array lengths.

```json
{
  "version": 4,
  "transfer_mode": "reference_first_reconstruction",
  "summary": "short visual description",
  "palette": {
    "background": "#RRGGBB", "ink": "#RRGGBB", "primary": "#RRGGBB",
    "secondary": "#RRGGBB", "accent": "#RRGGBB",
    "header_text": "#RRGGBB", "section_heading_text": "#RRGGBB",
    "additional_roles": {"optional_repeated_accent_role": "#RRGGBB"}
  },
  "header_treatment": {
    "mode": "open_white|top_rule_white|tinted_open|filled_band|subtle_outline|split_identity",
    "alignment": "left|center",
    "composition": "full_width_identity|left_identity_cluster|centered_identity",
    "background_role": "background|secondary|primary",
    "title_color_role": "primary|ink|header_text",
    "rule_placement": "none|top|bottom", "rule_color_role": "primary|ink",
    "rule_width_px": 0
  },
  "lead_band": {
    "present": true, "placement": "below_identity",
    "background_role": "primary|secondary|accent",
    "text_color_role": "on_primary|ink", "alignment": "left|center",
    "height_px": 0, "text_size_px": 0
  },
  "section_heading_treatment": {
    "mode": "filled_band|outlined_band|underline|text_only",
    "text_color_role": "primary|ink|on_primary",
    "fill_role": "background|secondary|primary", "border_role": "primary|ink",
    "border_width_px": 0, "corner_style": "square|rounded|capsule",
    "rule_color_role": "primary|ink", "rule_width_px": 0
  },
  "section_structure": {
    "inter_section_dividers": "none|hairline|strong",
    "outer_border": "none|hairline",
    "vertical_accent_rules": "none|sparse|frequent"
  },
  "body_region_structure": {
    "layout_mode": "equal_regions|weighted_regions|freeform_regions",
    "region_count": 3,
    "major_section_count": 3,
    "major_sections_per_region": [1, 1, 1],
    "regions": [
      {
        "region_id": "region_1",
        "region_role": "column|footer_band|side_callout|hero_region|stacked_region|full_width_band",
        "section_count": 1,
        "reading_order": 1
      }
    ],
    "subsection_treatment": "inline_colored_label|small_heading|none"
  },
  "surfaces": {
    "panel_fill": "white|near_white|transparent",
    "border_style": "none|hairline",
    "corner_style": "square|subtle", "shadow_style": "none|subtle"
  },
  "spacing": {
    "outer_margin_px": 0, "column_gap_px": 0,
    "section_gap_px": 0, "panel_padding_px": 0
  },
  "layout_rhythm": {
    "region_proportions": [1, 1, 1],
    "density": "dense|balanced",
    "region_boxes": [
      {"region_id": "region_1", "x_pct": 0, "y_pct": 0, "w_pct": 33, "h_pct": 100}
    ]
  },
  "chrome_treatment": {
    "present": false, "placement": "none|gutters|section_edges",
    "density": "none|sparse|frequent", "crossing_policy": "never_cross_content"
  },
  "typography_style": {
    "display_family_category": "sans_serif|serif",
    "body_family_category": "sans_serif|serif",
    "family_category": "sans_serif|serif",
    "title_weight": 700, "identity_weight": 500,
    "section_heading_weight": 700, "body_weight": 400,
    "title_size_px": 72, "identity_size_px": 26,
    "section_heading_size_px": 34, "body_size_px": 24, "caption_size_px": 20
  },
  "table_treatment": {
    "observed": false,
    "rule_style": "none|minimal|booktabs|hairline_grid",
    "header_fill": "none|light|primary"
  },
  "formula_treatment": {"frame": "none|hairline|box", "background": "none|light"},
  "figure_treatment": {"frame": "none|hairline", "caption_alignment": "left|center"},
  "do_not_copy": ["reference content", "reference logos", "reference figures"]
}
```

The body has two to six tight, non-overlapping macro regions. A detached bottom
question, Terminus, summary strip, or side callout is its own region. Collapse
space reserved for removed logos or QR codes instead of preserving an empty
asset zone.

## Blueprint HTML

Write a style-only `.reference-style-blueprint` root with one
`data-style-role="identity-header"`, an optional `lead-band`, one
`body-regions` container, and two to six `body-region` elements. Every region
must use matching `data-region-id` and `data-region-role` values from the JSON.
Group vertically aligned panels sharing one reading track as a `stacked_region`.

Use `{{PAPER_TITLE}}`, `{{AUTHORS}}`, and `{{INSTITUTIONS}}` exactly once inside
the identity header. Other visible text may use only `{{TARGET_PAPER_SUMMARY}}`,
`{{SECTION_TITLE}}`, `{{TARGET_PAPER_CONTENT}}`, `{{TARGET_PAPER_FIGURE}}`, and
`{{TARGET_PAPER_TABLE}}`. Do not include copied wording, names, logos, QR codes,
icons, scientific content, `img`, `svg`, `canvas`, `script`, `link`, `iframe`,
remote URLs, or data URLs.

Keep every top-level section below the header inside exactly one body region.
If a region has one major heading, use one `section` and represent internal
topics with `subsection-heading` or inline labels. Put body-spanning decoration
only in one root-level `data-style-role="chrome-layer"` behind content. Header
decoration remains inside the identity header. Never implement large geometry
with section or column pseudo-elements, and never cross content.

Put all styling in one inline `<style>` block. Use only six-digit colors from
the analysis palette, including declared `additional_roles`. Treat a visible
rectangle as a figure/table frame only when it is authored chrome, not the
raster edge of an embedded image. Do not infer table or formula rules when the
reference does not show them.

## Render-bound review

Render the exact final blueprint at the target canvas and compare it with
`reference.png`. Then compute the blueprint file SHA-256 and write:

```json
{
  "status": "ok",
  "rendered_blueprint_inspected": true,
  "header_matches_reference": true,
  "body_region_geometry_matches_reference": true,
  "chrome_avoids_content": true,
  "blueprint_sha256": "sha256 of final reference_style_blueprint.html"
}
```

Finally write `reference_style_agent_done.json` as `{"status":"ok"}`. The
reference is read-only: never transcribe, summarize, paraphrase, or reuse its
text, claims, links, logos, QR codes, icons, figures, tables, or other assets.
