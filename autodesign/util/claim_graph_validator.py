"""v2.8.0 ClaimGraph validator.

Two checks, both pure functions:

1. **Substring provenance** — every `EvidenceNode.raw_quote` must appear
   verbatim in `paper_raw_text` (whitespace-collapsed). Echoes the v2.7
   provenance rule for body bullets — fabricated quotes drop the whole
   graph back to None and the runner degrades to v2.7.3 behavior.
2. **Reference integrity** — `MechanismNode.resolves` must reference
   existing tension ids; `ImplicationNode.derives_from` must reference
   existing mechanism or evidence ids. Dangling refs make the planner's
   downstream `covers` field meaningless.

`validate_claim_graph(graph, paper_raw_text)` returns a list of human-
readable error strings (empty list = pass). The runner logs the list and
sets `Brief.claim_graph = None` on any failure.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..schema import ClaimGraph


_WS_RE = re.compile(r"\s+")


def _norm_ws(s: str) -> str:
    """Collapse runs of whitespace so substring matches survive PDF line
    wraps and double-spaces. Mirrors `provenance._norm_ws` so the two
    validators behave identically on the same input."""
    return _WS_RE.sub(" ", s or "").strip()


def validate_claim_graph(
    graph: "ClaimGraph", paper_raw_text: str,
) -> list[str]:
    """Return a list of error strings; empty list means the graph passes.

    Errors fall into three buckets:
      - "evidence E5: raw_quote not in paper_raw_text"
      - "mechanism M2: resolves unknown tension id 'T9'"
      - "implication I3: derives_from unknown id 'X1'"

    The function does NOT mutate the graph and does NOT raise on bad input
    — empty paper_raw_text simply makes every evidence node fail the
    substring check, which the runner will treat as a degraded extract.
    """
    errors: list[str] = []
    haystack = _norm_ws(paper_raw_text or "")

    tension_ids = {t.id for t in graph.tensions}
    mechanism_ids = {m.id for m in graph.mechanisms}
    evidence_ids = {e.id for e in graph.evidence}
    valid_implication_refs = mechanism_ids | evidence_ids

    for ev in graph.evidence:
        quote = _norm_ws(ev.raw_quote or "")
        if not quote:
            errors.append(f"evidence {ev.id}: empty raw_quote")
            continue
        if quote not in haystack:
            errors.append(
                f"evidence {ev.id}: raw_quote not in paper_raw_text "
                f"(quote={ev.raw_quote!r:.120s})"
            )
        for mech_id in ev.supports:
            if mech_id not in mechanism_ids:
                errors.append(
                    f"evidence {ev.id}: supports unknown mechanism id "
                    f"{mech_id!r}"
                )

    for mech in graph.mechanisms:
        for tid in mech.resolves:
            if tid not in tension_ids:
                errors.append(
                    f"mechanism {mech.id}: resolves unknown tension id "
                    f"{tid!r}"
                )

    for impl in graph.implications:
        for ref in impl.derives_from:
            if ref not in valid_implication_refs:
                errors.append(
                    f"implication {impl.id}: derives_from unknown id "
                    f"{ref!r} (must reference a mechanism or evidence)"
                )

    return errors
