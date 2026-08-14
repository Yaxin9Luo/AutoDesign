# Paper2Deck Provenance

Use for academic paper decks together with `deck.html_ppt_general`, which supplies the visual substrate. Technical claims and evidence must remain traceable to the source.

## Stage: enhance

Expand the paper into a source-aware narrative without repeating thesis, mechanism, and takeaways as interchangeable summaries. Require a source-backed visual plan and speaker-note intent. Experiment setup, method, results, ablation/analysis, and limitations earn dedicated space only when the paper memory, evidence packs, or visual provenance supports them.

## Stage: plan

Ingest first; plan claims, source slots, and speaker notes before slide markup. Author editable slide frames with stable provenance metadata. Read `visual_policy`.

For the default 18-slide academic deck, use this evidence-conditioned arc:

1. Cover: paper identity only.
2. Outline: preview the talk chapters without presenting conclusions early.
3. Research problem and scope: define the question, not the answer twice.
4. Source-supported motivation.
5. Prior work and unresolved gap.
6. Contributions: separate the supported claims.
7. Method overview: one complete system-level method claim.
8. Mechanism detail: explain how the method works without restating slide 7.
9. Algorithm, objective, or architecture detail when evidenced.
10. Experiment setup or evaluation protocol when evidenced.
11. Primary results with a local reading of the strongest evidence.
12. Secondary or robustness results when evidenced.
13. Ablation or analysis when the source contains it.
14. Qualitative behavior when the source contains it.
15. Limitations, failure modes, or boundary conditions when evidenced.
16. Implications and synthesis without repeating the thesis.
17. Distinct takeaways.
18. Closing and discussion prompt.

Merge unsupported conditional slots into adjacent evidenced sections or replace them with other source-supported analysis. Never invent an experiment setup, ablation, limitation, number, formula, or causal claim to fill the arc.

Each planned slide carries chapter, communication job, assertion title, scope, layout family, evidence refs, and speaker-note intent. A full formal profile uses 20-26 slides and may add chapter checkpoints, architecture detail, datasets/metrics, implementation detail, and efficiency analysis. Keep `[Sources]` and `[Talk]` guidance in hidden speaker-note metadata.

## Stage: critique

Reject missing high-value source coverage, fabricated numbers, unbound claims, absent evidence quotes, unsupported narrative slots, repeated thesis/mechanism/takeaway copy, and method/results slides that omit available paper figures or tables. Check the independent unique-source, source-placement, and visual-unit-slide targets.

## Stage: repair

Restore source ids, distinct local interpretations, and editable visual units; enlarge evidence through layout changes; and rewrite unverifiable claims around exact source evidence. Read `visual_policy`.
