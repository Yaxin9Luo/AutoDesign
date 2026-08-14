"""Fixed paper-poster quality rubric.

This is the encoded-prior rubric the evaluation harness scores against. The
weights and dimensions mirror the mature generation-side contracts (gold floors,
density/synthesis priors, faithfulness, readability) rather than re-deriving a
new aesthetic. Human-alignment calibration is intentionally out of scope here:
the generation harness already absorbed those priors and this rubric reuses them.

Owners:
- ``deterministic`` dimensions are scored entirely by Python rule tools.
- ``subjective`` dimensions are scored directly by the per-dimension VLM judge.
- ``mixed`` dimensions have a deterministic component and a subjective component;
  the aggregator blends whatever components are available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


DimensionOwner = Literal["deterministic", "subjective", "mixed"]
@dataclass(frozen=True)
class RubricDimension:
    id: str
    weight: float
    owner: DimensionOwner
    summary: str
    tools: tuple[str, ...] = field(default_factory=tuple)

    @property
    def subjective(self) -> bool:
        return self.owner in {"subjective", "mixed"}


# Weighted dimensions sum to 100. ``render_integrity`` is a hard gate, not a
# weighted dimension: it caps the overall score instead of contributing to it.
DIMENSIONS: tuple[RubricDimension, ...] = (
    RubricDimension(
        id="source_faithfulness",
        weight=10.0,
        owner="mixed",
        summary="Claims, numbers, and entities are grounded in the paper; no hallucinated results.",
        tools=("numeric_grounding", "vlm_judge"),
    ),
    RubricDimension(
        id="paper_coverage",
        weight=10.0,
        owner="subjective",
        summary="The poster conveys the paper's core arc and must-cover points.",
        tools=("paper_brief", "vlm_judge"),
    ),
    RubricDimension(
        id="information_density_and_synthesis",
        weight=15.0,
        owner="deterministic",
        summary="Dense, well-synthesized panels; no hollow interiors, bottom dead space, long blank bands, or pasted paper-body screenshots.",
        tools=("density", "html_structure", "gold_floor", "paper_body_screenshot"),
    ),
    RubricDimension(
        id="visual_evidence_use",
        weight=10.0,
        owner="mixed",
        summary="Meaningful use of figures/tables with nearby readouts, not screenshot walls or raw paper-body crops.",
        tools=("html_structure", "render_audit", "crop", "paper_body_screenshot", "vlm_judge"),
    ),
    RubricDimension(
        id="basic_layout_integrity",
        weight=20.0,
        owner="deterministic",
        summary="Final rendered poster is not mechanically broken: usable size/aspect, readable text, no obvious export-edge damage.",
        tools=("basic_layout_integrity", "ocr", "density"),
    ),
    RubricDimension(
        id="layout_readability",
        weight=25.0,
        owner="mixed",
        summary="Clear hierarchy, readable text, no overflow/clipping/overlap.",
        tools=("density", "render_audit", "crop", "vlm_judge"),
    ),
    RubricDimension(
        id="professional_aesthetics",
        weight=10.0,
        owner="subjective",
        summary="Professional academic craft: restrained palette, consistent typography.",
        tools=("vlm_judge",),
    ),
)

# Hard gate dimension (not weighted). A P0 here caps the overall score.
GATE_DIMENSION_ID = "render_integrity"
GATE_CEILING_SCORE = 40.0
BODY_SCREENSHOT_GATE_CEILING_SCORE = 30.0
BODY_SCREENSHOT_CATASTROPHIC_GATE_CEILING_SCORE = 0.0
EMPTY_VISUAL_PLACEHOLDER_SEVERE_GATE_CEILING_SCORE = 10.0
EMPTY_VISUAL_PLACEHOLDER_CEILING_SCORE = 50.0
MULTI_PANEL_CROP_FAILURE_CEILING_SCORE = 50.0

# Severity weighting mirrors scripts/poster_autopilot_decision.SEVERITY_WEIGHT,
# kept local so the evaluator package does not import from scripts/.
SEVERITY_WEIGHT: dict[str, float] = {"P0": 5.0, "P1": 2.0, "P2": 0.5}

# Verdict thresholds over the 0..100 overall score.
PASS_THRESHOLD = 70.0
REVISE_THRESHOLD = 50.0

# --- Image-native density references -----------------------------------------
# The density dimension is scored purely from the rendered image so PNG/JPG/PDF/
# HTML inputs of the same poster get the same score (format normalization). All
# three signals are resolution-invariant ratios.
#
# Calibrated on the three human_effort_gold_v1 posters, measured with the exact
# eval-time methods (pixels + RapidOCR): nonwhite 0.42-0.46, OCR text-coverage
# 0.40-0.50, longest blank vertical run < 0.01. References sit below the gold
# actuals so a gold-quality poster scores ~1.0 on each component.
REF_NONWHITE_RATIO = 0.23          # legacy ink reference (kept for step visualizer)
BLANK_BAND_FLOOR = 0.08            # legacy 1-D blank band floor (kept for step visualizer)
BLANK_BAND_PENALTY_K = 0.5         # legacy blank penalty (kept for step visualizer)

# Density / layout are scored from an image-native CONTENT-occupancy grid (see
# evaluator/spatial.py), not raw non-white pixels. A cell counts as content only
# if it has real texture or OCR text, so colored panel backgrounds and dark voids
# no longer inflate the score, and large empty regions inside the layout (not just
# full-width blank bands) are penalized.
# NOTE: these scoring constants were calibrated by scripts/calibrate_density_layout.py
# over 100 stratified real posters + 9 corner cases — chosen as the most stable
# config (all corner ground-truth targets satisfied, good sample spread, low
# saturation, flat objective region).
REF_CONTENT_COVERAGE = 0.80        # full marks for content occupancy at/above this
REF_TEXT_COVERAGE_RATIO = 0.40     # full marks for OCR text richness at/above this
# Content occupancy is the density backbone. Text only *lifts* the score (via a
# max() against content) — it can never cap a content-full poster — so OCR
# failure (stylized/non-Latin/low-res text) does not tank a genuinely dense poster.
DENSITY_CONTENT_WEIGHT = 0.7       # content occupancy dominates the density base
DENSITY_TEXT_WEIGHT = 0.3          # text richness can only add, never subtract
EMPTY_RECT_CAP = 0.10              # a blank strip/void at/above this fraction is "fully bad"
DENSITY_VOID_PENALTY_K = 0.75      # multiplicative: up to -75% density for a max void
LAYOUT_VOID_CAP = 0.10             # layout void penalty saturates at this void fraction
LAYOUT_VOID_PENALTY_K = 0.8        # multiplicative: up to -80% layout for a max void


def dimension_by_id(dimension_id: str) -> RubricDimension | None:
    for dimension in DIMENSIONS:
        if dimension.id == dimension_id:
            return dimension
    return None


def subjective_dimension_ids() -> tuple[str, ...]:
    return tuple(d.id for d in DIMENSIONS if d.subjective)


def deterministic_dimension_ids() -> tuple[str, ...]:
    return tuple(d.id for d in DIMENSIONS if d.owner in {"deterministic", "mixed"})


def weight_total() -> float:
    return sum(d.weight for d in DIMENSIONS)
