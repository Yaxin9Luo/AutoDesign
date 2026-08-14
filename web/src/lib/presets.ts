import type { ArtifactType } from "./types.ts";

export const PAPER_POSTER_TEMPLATE = "cvpr-landscape";

export const DENSE_PAPER_POSTER_PROMPT = `Generate a polished academic paper poster from the attached paper. Make it a dense conference-style editorial poster: compact paper identity header, three-column body, multiple normal-flow sections, strong section headers, source figures/tables with short local readouts, and enough paper-specific content to make every major section feel intentionally occupied. Use a real academic-poster layout, not a landing page. The first screen should be the poster itself.

Header requirements:
- Use a full-width top identity header area with the fixed treatment: a white/near-white header with a single top accent rule only; no bottom header rule or side outline.
- The header is limited to exactly these three visible paper-identity lines: paper title, author list, and school/institution/company names.
- Place those fields as three compact centered text rows only: title line, authors line, school/institution/company line.
- The school/institution/company line should contain only organization names grounded in the paper, rendered as plain text only. Do not invent missing organizations.
- Do not add a fourth header/meta/subtitle row or side identity rail. Do not put any other visible content in the header: no logos, image badges, icons, QR codes, venue/year text, conference names, arXiv/archive labels, citation/contact text, project/code/resource links, topic badges, method slogans, contribution bullets, benchmark claims, source figures/tables, or explanatory captions. If any of those fields are available, leave them out of the header; users can add them after export.

Poster structure and density:
- Use three balanced vertical columns below the header.
- Each column should contain multiple dense academic sections with numbered section headings where natural, such as Motivation, Method, Data/Benchmark, Results, Analysis, Limitations, Takeaways, and References/Code.
- Fill the poster like a real conference poster: high information density, little wasted whitespace, but still readable.
- Every major panel should feel filled by meaningful paper content, not by stretched whitespace. If a panel has empty space, first add concise paper-specific details, extra benchmark facts, mechanism notes, ablation takeaways, compact native rows, or short bullets from the paper. Only then adjust sizing or spacing.
- Do not simply enlarge figures, tables, cards, or text to fill space. Avoid large unused blank areas inside sections.
- If content does not fit, choose the right local repair visually: shorten prose, tighten spacing, reduce figure/table size, convert long prose to bullets, split or rebalance the section, or remove lower-value text. Do not over-compress unrelated sections that are already good.

Evidence and content requirements:
- Use real figures, tables, charts, and diagrams from the paper whenever they support the story.
- Every source figure/table should appear near a concise local explanation of what it proves.
- Reconstruct simple result tables, compact comparison tables, source-grounded bullets, and pipeline diagrams as native editable HTML/SVG/table elements when useful.
- All claims, numbers, benchmarks, dataset details, method descriptions, limitations, and takeaways must be grounded in the attached paper.
- If a number or claim cannot be verified from the paper, omit it or rewrite it qualitatively.
- Results sections should include more than headline numbers. Add benchmark context: competing baselines, task families, secondary metrics, and the claim those results support.
- Training, method, and analysis sections should use paper-specific details: stages, datasets, losses, sequence lengths, modules, ablation results, assumptions, or future-work claims where available.

Visual and typography requirements:
- Use a formal academic poster visual system with a restrained identity header treatment, filled color section title bands, compact body text, thin dividers, native tables, charts, and disciplined spacing.
- Use the fixed academic typography system: Times New Roman; title 56px/1.08/600; author and institution rows 28px; major section headings 36px/1.10/700; body/readout/table prose 24px; captions/labels 20px. Body text should stay regular weight except for short important terms, labels, or numbers.
- Avoid large blocks of bold body copy. Use italics sparingly or not at all.
- Prefer concise bullets for explanatory text when they improve scanability.
- Prefer real paper visuals over decorative imagery. Do not use generated or stock imagery as scientific evidence.
- Preserve readable source figures. Avoid ugly oversized white wrappers around images. Crop only obvious external white margins; never crop into axes, legends, labels, captions, diagrams, or meaningful visual content.
- If a figure has a left/right layout with blank space beside it, fill that same source-flow unit with source-backed readouts, native mini tables, or mechanism bullets. If there is not enough local content, use a stacked/full-width layout instead of leaving a half-empty lane.
- For dense or ugly source tables, do not blindly reproduce screenshots. Rebuild them as clean native HTML tables, compact comparison tables, concise visual interpretation, or source-grounded bullets using the table's actual paper facts.
- Use a restrained but polished academic color palette appropriate to the paper domain: neutral white/near-white background, high-contrast ink, and one disciplined accent color. The identity header uses the fixed white/near-white treatment with a single top accent rule only, while section headings use compact filled accent bands with white text. Keep panel interiors, native table cells, and ordinary readouts white or neutral. Source figure/table wrapper DOM boxes may exist for measurement, but their borders must be transparent with no visible outline or shadow. Avoid bottom header rules, filled title bands, four-sided outlines, mixing header styles, tinted panel bodies, pale-blue table zebra rows, heavy colored borders, and decorative gradients or stock-like backgrounds.

Do not:
- Do not create a web landing page, dashboard, marketing graphic, promotional poster, or grid of large repeated cards.
- Do not show process labels such as "source-backed", "authored HTML", "paper poster", "no generated evidence imagery", "ingested", "retrieved", "grounded", "evidence pack", or "pipeline".
- Do not display source ids, layout notes, planning notes, or internal instructions.
- Do not bake final text into images; keep text native and editable.

Use browser-native authored HTML/CSS for the poster layout. Let CSS grid/flex/normal document flow handle the three columns and section interiors; avoid absolute-positioned piles of independent text boxes. After rendering, visually inspect the full poster and iterate locally until it looks intentionally composed: no blank panel bottoms, clipped figures, overlapped text, unreadable tables, oversized wrappers, excessive bold/italic body text, or padded-looking sections.`;

export const PAPER_BUNDLE_PROMPT_VERSION = 1 as const;
export const VIDEO_ARTIFACT_DESCRIPTION = "MP4 · 5–10 min · narrated + subtitles";
export const VIDEO_SCENE_DURATION_MIN_S = 1;
export const VIDEO_SCENE_DURATION_MAX_S = 600;

export const PAPER_BUNDLE_PROMPTS_V1 = Object.freeze({
  poster: DENSE_PAPER_POSTER_PROMPT,
  deck: "Create a polished 16:9 academic conference slide deck in standalone HTML. Build a coherent research talk narrative from motivation through method, evidence, analysis, limitations, and takeaways; make substantive slides information-rich and use original paper figures, native tables, equations, and editable diagrams as visual evidence.",
  landing: "Create a polished interactive academic paper landing page in standalone HTML. Make the paper identity, method, evidence, and results immediately understandable; use many eligible original paper figures with local interpretations, restrained inline SVG icons, meaningful source-grounded interactions, responsive layout, and subtle motion with reduced-motion support.",
  video: "Create a rigorous 5–10 minute academic conference video, choosing the duration to match the paper's complexity, with English narration, English subtitles, and extensive use of original paper visuals.",
} satisfies Record<ArtifactType, string>);

export function shouldUsePaperPosterTemplate(
  artifact_type: string | null | undefined,
  attachments: Array<{ kind?: string; role?: string }>,
): boolean {
  return artifact_type === "poster" && attachments.some(
    (a) => a.kind === "pdf" && a.role !== "style_reference",
  );
}
