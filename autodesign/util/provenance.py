"""v2.7 Provenance validator for paper2deck.

Audits every body-text LayerNode for fabricated numbers. The 2026-04-25
longcat-next dogfood produced 9 fabricated bullets across slides 4/6/8/9
("PSNR 28.5 → 22.1 dB" — paper Table 6 actually shows 20.88/21.86/30.52/18.16;
"500K hours audio / 4.5T text tokens / 80GB vs 120GB / 12.5K tok/sec on
64×A100" — none of those numbers exist in the paper at all). The pattern:
when the planner can ground from an explicit benchmark table (slide 7
headline numbers were all real), it cites correctly. When the topic is
narrative/distributed (training scale, ablations, scaling laws), the
v2.5.3 "number + named rival" prompt rule backfires — the LLM meets the
rule by inventing plausible numbers.

This module is the stick. Prompt rules in v2.7 ask the planner to set
`evidence_quote` on every number-bearing LayerNode body; this validator
substring-matches that quote against `ctx.state["ingested"][i]["raw_text"]`
and either logs (`audit` mode) or replaces unverifiable numbers with `[?]`
markers in the rendered slide (`strict` mode, default for paper2deck).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator


# Match numeric tokens worth checking. Captures:
#   - integers >=3 digits (skip "v2", "1×" version/multiplier noise)
#   - decimals (any digit count)
#   - K/M/T/B/% suffixed forms (4.5T, 500K, 40%, 12.5K)
#   - optional unit suffix (dB, GB, MB, TB, hours, tok, FLOPs, ×, x)
#   - signed decimals (+5.2, -0.32) — common in deltas + scaling exponents
#
# We deliberately don't match bare 1- or 2-digit ints because they are
# almost always slide indices, list counters, version numbers, or section
# labels — too noisy to gate on.
_NUMERIC_RE = re.compile(
    r"(?<![\w.])"
    r"("
        r"[+\-]?"
        r"(?:"
            # number + unit (catches "80 GB", "12.5 hours", "100K tokens", "5×")
            r"\d+(?:\.\d+)?[KMTB]?\s*(?:dB|GB|MB|TB|kB|hours?|tok(?:ens?)?|FLOPs?|×|x)"
            # decimals without unit (28.5, 0.32, 70.6)
            r"|\d+\.\d+"
            # K/M/T/B suffix without unit (500K, 4.5T, 1.5B)
            r"|\d+(?:\.\d+)?[KMTB]"
            # percentage (40%, 12.5%)
            r"|\d+(?:\.\d+)?%"
            # bare integers >= 100 (skip slide indices, version numbers)
            r"|\d{3,}"
        r")"
    r")"
    r"(?!\w)",
    re.IGNORECASE,
)


# Strings whose numeric content is purely structural and doesn't require
# evidence: slide-N/M markers, version tags, section labels like
# "01 · MOTIVATION", page references like "p.12".
_SAFE_CONTEXT_RE = re.compile(
    r"^\s*(?:"
        r"\d+\s*/\s*\d+"                # 1/12
        r"|v?\d+(?:\.\d+)+"             # v2.6.1, 2.6
        r"|\d{1,2}\s*[·:]\s*[\w\s]+"    # "01 · MOTIVATION"
        r"|p\.?\s*\d+"                  # p.12
        r"|\d{1,2}"                     # bare 1-2 digit
    r")\s*$",
    re.IGNORECASE,
)


# Structural text has no paper claim to verify. All other text layers are
# audited so legacy layer graphs with arbitrary body-layer names stay covered.
_STRUCTURAL_TEXT_ROLES = frozenset({
    "title", "authors", "footer", "slide_number", "section_label", "badge",
})


@dataclass
class ProvenanceFailure:
    """One body bullet that failed evidence-quote substring match."""
    layer_id: str
    text: str
    numeric_tokens: list[str]
    evidence_quote: str | None
    reason: str  # "missing_quote" | "quote_not_in_source"


@dataclass
class ProvenanceReport:
    """Audit summary returned by `validate_provenance`. Caller decides
    whether to mutate the spec via `apply_strict_provenance`."""
    failures: list[ProvenanceFailure] = field(default_factory=list)
    n_text_layers_audited: int = 0
    n_layers_with_numbers: int = 0
    n_passed: int = 0
    sources: list[str] = field(default_factory=list)

    def has_failures(self) -> bool:
        return bool(self.failures)

    def to_dict(self) -> dict[str, Any]:
        return {
            "failures": [asdict(f) for f in self.failures],
            "n_audited": self.n_text_layers_audited,
            "n_with_numbers": self.n_layers_with_numbers,
            "n_passed": self.n_passed,
            "sources": self.sources,
        }


def _norm_ws(s: str) -> str:
    """Collapse runs of whitespace so substring matches survive PDF line
    wraps, double-spaces, and tab/space mixing."""
    return re.sub(r"\s+", " ", s or "").strip()


def _extract_numeric_tokens(text: str) -> list[str]:
    """Return numeric tokens from `text` worth checking. Returns empty
    list when `text` matches a `_SAFE_CONTEXT_RE` (purely structural)."""
    if not text:
        return []
    if _SAFE_CONTEXT_RE.match(text):
        return []
    return _NUMERIC_RE.findall(text)


def _iter_body_text_layers(spec: Any) -> Iterator[Any]:
    """Yield visible claim-bearing text from legacy layer-graph decks.

    The HTML-first deck path uses native HTML blocks; this compatibility
    validator handles any remaining layer-graph exports without relying on
    the removed file-backed PPTX template contract.
    """
    for slide in (getattr(spec, "layer_graph", None) or []):
        if getattr(slide, "kind", None) != "slide":
            continue
        for child in (getattr(slide, "children", None) or []):
            if getattr(child, "kind", None) != "text":
                continue
            role = (
                getattr(child, "role", None)
                or getattr(child, "name", None)
                or ""
            ).strip().lower().replace("-", "_").replace(" ", "_")
            if role not in _STRUCTURAL_TEXT_ROLES:
                yield child


def _collect_sources(ctx: Any) -> list[tuple[str, str]]:
    """Pull (title, normalized_raw_text) tuples from ctx.state["ingested"].
    Skips entries with no `raw_text` (e.g. image-only ingests)."""
    state = getattr(ctx, "state", None) or {}
    out: list[tuple[str, str]] = []
    for entry in (state.get("ingested") or []):
        raw = (entry or {}).get("raw_text") or ""
        if not raw:
            continue
        manifest = (entry or {}).get("manifest") or {}
        title = manifest.get("title") or entry.get("file") or "?"
        out.append((str(title), _norm_ws(raw)))
    return out


def validate_provenance(spec: Any, ctx: Any) -> ProvenanceReport:
    """Audit body-text bullets against ingested paper raw_text.

    Behavior:
      - No ingested source → empty report (validator is paper2deck-specific
        and a no-op on free-text decks).
      - For each body text layer: extract numeric tokens. If none, skip.
        If `evidence_quote` is missing → record failure (`missing_quote`).
        If `evidence_quote` is set but is not a normalized substring of any
        ingested raw_text → record failure (`quote_not_in_source`).
        Otherwise → pass.

    Caller consumes the report and decides logging / mutation policy.
    """
    sources_text = _collect_sources(ctx)
    report = ProvenanceReport(sources=[t for t, _ in sources_text])
    if not sources_text:
        return report

    for layer in _iter_body_text_layers(spec):
        report.n_text_layers_audited += 1
        text = (getattr(layer, "text", None) or "").strip()
        tokens = _extract_numeric_tokens(text)
        if not tokens:
            continue
        report.n_layers_with_numbers += 1

        quote = getattr(layer, "evidence_quote", None)
        if not quote or not quote.strip():
            report.failures.append(ProvenanceFailure(
                layer_id=str(getattr(layer, "layer_id", "?")),
                text=text,
                numeric_tokens=tokens,
                evidence_quote=None,
                reason="missing_quote",
            ))
            continue

        normq = _norm_ws(quote)
        if not any(normq in srct for _, srct in sources_text):
            report.failures.append(ProvenanceFailure(
                layer_id=str(getattr(layer, "layer_id", "?")),
                text=text,
                numeric_tokens=tokens,
                evidence_quote=quote,
                reason="quote_not_in_source",
            ))
            continue

        report.n_passed += 1
    return report


def validate_claim_graph_quotes(
    claim_graph: Any, ctx: Any,
) -> list[str]:
    """v2.8.0 — re-check `ClaimGraph.evidence[*].raw_quote` substring
    against the same paper raw_text the v2.7 body-bullet validator uses.

    The dedicated `util/claim_graph_validator.py` runs at extraction time
    (right after the sub-agent emits a ClaimGraph). This helper exists so
    the composite-stage validator can re-run the substring check in the
    same place it audits body bullets — useful when downstream code
    accidentally swaps `paper_raw_text` (e.g. apply-edits round-trip).

    Returns a list of error strings (empty = pass). When `claim_graph` is
    None or no source is available, returns []. Does NOT mutate the graph.
    """
    if claim_graph is None:
        return []
    sources_text = _collect_sources(ctx)
    if not sources_text:
        return []
    haystacks = [src for _, src in sources_text]

    errors: list[str] = []
    evidence = getattr(claim_graph, "evidence", None) or []
    for ev in evidence:
        quote = getattr(ev, "raw_quote", None) or ""
        normq = _norm_ws(quote)
        ev_id = getattr(ev, "id", "?")
        if not normq:
            errors.append(f"evidence {ev_id}: empty raw_quote")
            continue
        if not any(normq in h for h in haystacks):
            errors.append(
                f"evidence {ev_id}: raw_quote not in paper raw_text "
                f"(quote={quote!r:.120s})"
            )
    return errors


def apply_strict_provenance(spec: Any, report: ProvenanceReport) -> int:
    """Mutate failing bullets in-place: replace numeric tokens with `[?]`.

    Returns the count of layers actually mutated. Idempotent on layers
    whose text no longer matches `_NUMERIC_RE`.

    Educational by design: the rendered slide carries visible `[?]`
    markers so both presenter and audience see that the validator caught
    a fabrication. The planner can re-emit on the next revise iteration
    with proper `evidence_quote` to remove the markers.
    """
    if not report.has_failures():
        return 0
    failed_ids = {f.layer_id for f in report.failures}
    n_mut = 0
    for layer in _iter_body_text_layers(spec):
        lid = str(getattr(layer, "layer_id", "?"))
        if lid not in failed_ids:
            continue
        text = getattr(layer, "text", None) or ""
        new_text = _NUMERIC_RE.sub("[?]", text)
        if new_text != text:
            try:
                layer.text = new_text
            except (AttributeError, TypeError):
                # Pydantic frozen / proxy — best-effort skip.
                continue
            n_mut += 1
    return n_mut
