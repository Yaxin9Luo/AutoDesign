# Portable source-grounding contract

Use the bundled `_portable.py` harness for source preparation and validation.
Do not invent evidence IDs or treat a citation ID as proof by itself.

## Evidence first

1. Initialize a user-selected run directory. Initialization snapshots every
   bundled instruction, reference, and script used by the run, excluding
   generated interpreter caches. Resume always verifies the installed Skill
   root against that snapshot.
2. Prepare the user's source. Text and Markdown receive stable section/line
   anchors; Markdown evidence retains preambles and heading text. Fully verified
   PDF preparation copies the input first, then runs `pdftotext`, `pdfinfo`,
   `pdftoppm`, and `pdfimages` only against that immutable copy. A missing or
   failing command is `blocked`.
   Every rendered PDF page is exact-set and hash-bound in the source manifest.
   A blocked preparation may be retried after stale partial PDF outputs are
   cleared. A ready source is immutable; use a new run for another source.
3. Read `evidence/evidence.jsonl` and retrieve relevant entries lexically before
   drafting claims. Preserve the cited IDs in the artifact source map.
4. Validate claims before deterministic artifact QA. A direct quote must be a
   normalized substring of quote-safe cited evidence. Every visible number must
   occur in cited evidence or be the result of an explicit formula whose inputs
   occur there. Percent values remain distinct from unitless values; commas and
   scientific notation, leading decimals, and Unicode minus normalize without
   discarding units or signs. Every formula operand must be declared and
   source-grounded. Other claims, including CJK text, need meaningful lexical
   overlap. Semantic review remains responsible for paraphrase fidelity.

## Visual evidence

`evidence/source_visuals.json` is the only visual catalog. Explicit attached
images begin eligible. PDF-extracted candidates remain `review_required` until
a fresh host-VLM or reviewer sidecar binds the visual to caption evidence with
adequate confidence and declares allowed content roles. The sidecar repeats the
source-manifest, visual-catalog, visual-file, and caption-evidence hashes, so a
review from another source cannot authorize reuse. Confidence must be a finite
number from 0.8 through 1, excluding booleans. Bindings occur only while the
ready source is still initialized; repeated batches append to one exact-schema,
hash-bound authorization history. Plans must respect each visual's role
allowlist and reuse limit.

Reference images are style-only. Never copy their text, logos, claims, figures,
tables, links, or scannable codes into a generated artifact.

## Review and delivery

Write the attempt source map before deterministic checks. Create semantic
review context only after deterministic checks pass. The review
must echo the exact attempt ID, context hash, artifact hashes, complete preview
or frame set, source-manifest hash, source-map hash, and rubric hash. Reject
stale, partial, wrong-attempt, or incomplete reviews. Finalization exact-set
verifies reviewed artifact files, stages one passing attempt, atomically
promotes it, never overwrites an existing delivery, and labels a vision-
unreviewed delivery `needs_visual_review` rather than verified.
Source maps live under their attempt directory; finalization promotes only the
selected attempt's map and preserves all earlier attempt history.
