# Landing layout treatments

The first viewport is the usable page, not a preface. Plan hero/fold, proof, feature rhythm, workflow or comparison, and CTA/footer as semantic sections with slots and grouped blocks.

Classify the subtype before planning. Developer/SaaS pages stay utilitarian and scannable; product launches lead with the product; waitlists keep the offer and signup path direct. Use `section_map` for the matching section order.

Paper project pages use a source-dependent narrative drawn from `hero`, `resources`, `abstract`, `framework`, `key_findings`, `demos`, `benchmarks`, `ablations`, `limitations`, and `citation_footer`. Omit or merge a module when the source genuinely lacks that evidence. Show the real title, authors/affiliations when known, a source-backed thesis, and only discovered resource links. The result must read as a complete research narrative, not a generic marketing funnel.

After ingest, run paper-resource discovery before authoring. Render real arXiv/PDF, GitHub, Hugging Face, demo, blog, model-weight, social, hardware/interface, and BibTeX links as compact horizontal chips only when discovered. Missing links are quiet notes, never fake buttons.

Use a web type scale: 44-64px hero title, 28-36px section headings, 16-18px body, and 12-14px captions/code. Repeated same-size text blocks are a hierarchy failure.

Render actual source images, not text that merely names figure ids. Put at least one source visual in the hero/framework viewport when available; four or more source visuals are a normal publishable target when method, result, ablation, qualitative, or demo evidence exists. Prefer the paper's architecture/pipeline figure in framework and source-backed native tables or exact numeric summaries in benchmarks.

Select interaction from source affordances. Enable focused inspection for source figures, including keyboard close and previous/next navigation when multiple figures exist. Make native result tables sortable only where cell values permit deterministic comparison. Active navigation and reading progress should orient the reader; they do not substitute for a source-grounded interaction.

Use academic-light surfaces, restrained borders, editorial whitespace, and exactly one paper-specific primary accent. Use 3-8 restrained inline SVG icons for functional cues, and give every icon-only control an accessible name. Avoid dark default styling, gradients, oversized marketing heroes, repeated card walls, decorative motion, and unrelated imagery. Keep the desktop composition readable without hiding evidence. Core content must be visible without JavaScript reveal. Under reduced motion, remove smooth scrolling, animation, and transitions while keeping every section and control usable. Keep 3D disabled unless the source or brief explicitly opts in to an evidence-grounded 3D view.

Treat visual-reference feedback as a native section revision. Repair section order, slots, groups, type hierarchy, interaction semantics, and spacing before local decoration; preserve source visual ids, tables, citation text, and accessibility-friendly text.
