# Paper Project Page Taste

Taste contract version: 1.0.0

Use this resource only for `paper_project_page`. It records design qualities derived from user labels, not a reusable template. Do not copy, fetch, trace, or embed reference screenshots, assets, page code, logos, or brand styling.

## Target

- Tell a complete research narrative from identity and thesis through method, source evidence, results or demos, resources, limitations when supported, and citation.
- Lead with real paper visuals and native evidence. Visual hierarchy should make the method and findings inspectable without opening the PDF.
- Use modular composition with varied evidence-led section structures, not a repeated card wall or one static academic template.
- Add at least one meaningful source-grounded interaction chosen from available affordances. Figure-rich papers should support focused figure inspection; result tables should support comparison or sorting when their values permit it.
- Use purposeful technical motion to clarify state, navigation, or comparison. Keep the desktop page keyboard-operable and fully understandable with reduced motion.
- Use a light academic editorial surface, exactly one primary accent, and 3-8 restrained inline SVG icons as functional cues. Give every icon-only control an accessible name.
- Keep all core content visible before JavaScript runs. Motion may enhance state changes but must have a `prefers-reduced-motion: reduce` fallback that preserves content and controls.
- Keep 3D off by default. Enable it only when the paper source or brief explicitly requires a 3D view, and do not make it necessary for understanding the evidence.

## Reject

- Old static project-page templates with thin navigation and undifferentiated stacked text.
- Text-only blogs, link directories, or pages that outsource the paper narrative to other documents.
- Abstract, video-only, or atmosphere-only shells that omit method and result evidence.
- Reject generic product marketing, oversized sales heroes, conversion CTA funnels, testimonials, pricing language, or feature card walls.
- Unrelated decorative imagery, invented claims, remote visual dependencies, or animation that does not explain the research.
- JavaScript-dependent reveal patterns, motion without reduced-motion behavior, decorative 3D, inaccessible icon controls, or multiple competing accent colors.

## Decision Rule

Choose composition and interaction from the source inventory. Omit unsupported modules quietly; never invent evidence to complete a pattern. Preserve local-only assets, editable native text and tables, source provenance, desktop readability, and reduced-motion access.
