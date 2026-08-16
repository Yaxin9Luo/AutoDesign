# Portable source-grounding contract

Use the bundled `_portable.py` harness for source preparation and validation.
Do not invent evidence IDs or treat a citation ID as proof by itself.

## Evidence first

1. Initialize a user-selected run directory. Initialization snapshots every
   bundled instruction, reference, and script used by the run.
2. Prepare the user's source. Text and Markdown receive stable section/line
   anchors. Fully verified PDF preparation requires `pdftotext`, `pdfinfo`,
   `pdftoppm`, and `pdfimages`; a missing or failing command is `blocked`.
3. Read `evidence/evidence.jsonl` and retrieve relevant entries lexically before
   drafting claims. Preserve the cited IDs in the artifact source map.
4. Validate claims before deterministic artifact QA. A direct quote must be a
   normalized substring of quote-safe cited evidence. Every visible number must
   occur in cited evidence or be the result of an explicit formula whose inputs
   occur there. Other claims need meaningful lexical overlap. Semantic review
   remains responsible for paraphrase fidelity.

## Visual evidence

`evidence/source_visuals.json` is the only visual catalog. Explicit attached
images begin eligible. PDF-extracted candidates remain `review_required` until
a fresh host-VLM or reviewer sidecar binds the visual to caption evidence with
adequate confidence and declares allowed content roles. Plans must respect each
visual's role allowlist and reuse limit.

Reference images are style-only. Never copy their text, logos, claims, figures,
tables, links, or scannable codes into a generated artifact.

## Review and delivery

Create semantic review context only after deterministic checks pass. The review
must echo the exact attempt ID, context hash, artifact hashes, complete preview
or frame set, source-manifest hash, and rubric hash. Reject stale, partial,
wrong-attempt, or incomplete reviews. Finalization stages one passing attempt,
atomically promotes it, never overwrites an existing delivery, and labels a
vision-unreviewed delivery `needs_visual_review` rather than verified.
