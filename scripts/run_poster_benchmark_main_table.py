#!/usr/bin/env python3
"""Run the 5-discipline poster benchmark for multiple poster systems.

This is a benchmark runner, not a new evaluator. It reuses the production
evaluation pieces:

- deterministic pre-pass: compute_deterministic_report
- subjective/mixed dimensions: direct single-dimension VLM judge
- final arithmetic: aggregate_final

Outputs are cached per candidate so long runs can be resumed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html as H
import json
import math
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from autodesign.evaluator.poster_rubric import (  # noqa: E402
    DIMENSIONS,
    GATE_CEILING_SCORE,
    PASS_THRESHOLD,
    REVISE_THRESHOLD,
)
from autodesign.eval_protocol import (  # noqa: E402
    EVAL_PROTOCOL,
    EVALUATOR_FINGERPRINT,
    VLM_PROMPT_FINGERPRINT,
    combine_fingerprints,
    fingerprint_python_symbols,
    structured_fingerprint,
)
from autodesign.evaluator.batch_style_homogeneity import (  # noqa: E402
    BATCH_STYLE_FINGERPRINT,
    MIN_BATCH_SIZE as BATCH_STYLE_MIN_BATCH_SIZE,
    evaluate_batch_style_homogeneity,
)
from autodesign.evaluator.benchmark_calibration_report import (  # noqa: E402
    build_anonymous_system_contact_sheet,
    build_same_paper_comparison_section,
    render_system_explainability_fields,
)
from autodesign.evaluator.quality_rubric import (  # noqa: E402
    academic_poster_aesthetics_cap_policy,
    aggregate_final,
    compute_deterministic_report,
    layout_coupled_cap_policy,
    poster_scale_legibility_cap_policy,
    presentation_viability_cap_policy,
)
from autodesign.evaluator.quality_schema import RubricDimensionScore  # noqa: E402
from autodesign.evaluator.tools import (  # noqa: E402
    DEFAULT_BENCHMARK_JUDGE_MODEL,
    _format_grounding,
    tool_vlm_judge,
)
from autodesign.evaluator.vlm_benchmark import _extract_paper_text, _safe_name  # noqa: E402
from autodesign.util.io import atomic_write_json  # noqa: E402


DISCIPLINES = [
    "ai_ml_existing_20",
    "biomed_health",
    "climate_earth_environment",
    "economics_policy",
    "physics_astronomy",
]

DISCIPLINE_LABELS = {
    "all": "全部",
    "ai_ml_existing_20": "AI/ML",
    "biomed_health": "生物医学与健康",
    "climate_earth_environment": "气候、地球与环境",
    "economics_policy": "经济与政策",
    "physics_astronomy": "物理与天文",
}

SYSTEM_LABELS = {
    "designanything": "AutoDesign",
    "opendesign_posterbench": "OpenDesign",
    "autodesign": "AutoDesign",
    "claude_design": "Claude Design",
    "codex_native": "Codex Native",
    "codex_posterly": "Codex Posterly",
    "codex_posterskill": "Codex PosterSkill",
    "codex_pptxposterskill": "Codex PPTX PosterSkill",
}

SYSTEM_ORDER = [
    "designanything",
    "opendesign_posterbench",
    "autodesign",
    "claude_design",
    "codex_native",
    "codex_posterly",
    "codex_posterskill",
    "codex_pptxposterskill",
]

DIM_LABELS = {
    "source_faithfulness": "Source Faithfulness",
    "paper_coverage": "Paper Coverage",
    "information_density_and_synthesis": "Information Density",
    "visual_evidence_use": "Visual Evidence Use",
    "basic_layout_integrity": "Basic Layout Integrity",
    "layout_readability": "Layout Readability",
    "professional_aesthetics": "Professional Aesthetics",
}

VLM_DIMS = [
    "paper_coverage",
    "source_faithfulness",
    "visual_evidence_use",
    "layout_readability",
    "professional_aesthetics",
]

ALL_DIMS = [d.id for d in DIMENSIONS]
FORCEABLE_VLM_DIMS = {
    "visual_evidence_use",
    "layout_readability",
    "professional_aesthetics",
}
_LEGACY_DETERMINISTIC_EQUIVALENT_RUBRIC_VERSIONS = {"0.1.20"}
_LEGACY_FINAL_EQUIVALENT_RUBRIC_VERSIONS = {"0.1.19", "0.1.20"}
_LEGACY_VLM_EQUIVALENT_RUBRIC_VERSIONS = {"0.1.19", "0.1.20"}
_LEGACY_UNCHANGED_TEXT_DIMENSION_RUBRIC_VERSIONS = {
    "0.1.14",
    "0.1.15",
    "0.1.16",
    "0.1.17",
    "0.1.18",
}
_DIMENSION_CAP_FINDING_IDS = {
    "layout-coupled-score-cap",
    "academic-poster-aesthetics-density-cap",
    "poster-scale-legibility-cap",
}
_PRESENTATION_VIABILITY_CAP_FINDING_ID = "presentation-viability-score-cap"
_REMOVED_SINGLE_DIMENSION_CAP_FINDING_ID = "judge-confirmed-serious-visual-defect"
_LEGACY_EVALUATOR_FINGERPRINTS = {
    # AutoDesign package rename only; evaluator implementation is unchanged.
    "sha256:138f5cedc0ef5361ef0f8cb550c602d4c0e78f6f56a1d645816a1dcb311117f2",
    "sha256:94f125533f8c888370d7733842c3a97836ea2aa1f2a7eec8cf24278c3e88f9d5",
}
_LEGACY_BENCHMARK_EVALUATOR_FINGERPRINTS = {
    # AutoDesign package rename only; benchmark aggregation is unchanged.
    "sha256:67aa0c9ba0096c5082e0b176bbfe1cf70ba2372a832bcca1ad55b6e6a028620e",
}
_LEGACY_VLM_PROMPT_FINGERPRINTS = {
    "sha256:8e6fd9f87dc5f777e7c2defe1031111fe59ac12e2c372ce85c9d1a16e3c326d1",
}
BENCHMARK_EVALUATOR_FINGERPRINT = combine_fingerprints(
    EVALUATOR_FINGERPRINT,
    fingerprint_python_symbols(
        Path(__file__),
        ["_complete_final_report", "_reaggregate_final_report"],
        namespace=f"{EVAL_PROTOCOL}:benchmark-scoring-code",
    ),
    namespace=f"{EVAL_PROTOCOL}:benchmark-evaluator",
)


def _is_retired_single_dimension_cap(finding: Any) -> bool:
    if not isinstance(finding, dict) or finding.get("id") != _REMOVED_SINGLE_DIMENSION_CAP_FINDING_ID:
        return False
    evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
    serious_dimensions = evidence.get("serious_dimensions")
    return not isinstance(serious_dimensions, list) or len(serious_dimensions) < 2


def _parse_force_vlm_dims(raw: str) -> set[str]:
    dims = {item.strip() for item in str(raw or "").split(",") if item.strip()}
    unsupported = sorted(dims - FORCEABLE_VLM_DIMS)
    if unsupported:
        raise argparse.ArgumentTypeError(
            "--force-vlm-dims only accepts: "
            + ", ".join(sorted(FORCEABLE_VLM_DIMS))
            + f"; unsupported: {', '.join(unsupported)}"
        )
    return dims


def _legacy_compatible_vlm_rubric_versions(dim: str) -> set[str]:
    versions = set(_LEGACY_VLM_EQUIVALENT_RUBRIC_VERSIONS)
    if dim in {"source_faithfulness", "paper_coverage"}:
        versions.update(_LEGACY_UNCHANGED_TEXT_DIMENSION_RUBRIC_VERSIONS)
    return versions


class _VLMCallLimiter:
    def __init__(self, delay_s: float) -> None:
        self.delay_s = max(0.0, float(delay_s))
        self._lock = threading.Lock()
        self._last_call_started_at: float | None = None

    def wait(self) -> None:
        if self.delay_s <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if self._last_call_started_at is not None:
                remaining = self.delay_s - (now - self._last_call_started_at)
                if remaining > 0:
                    time.sleep(remaining)
            self._last_call_started_at = time.monotonic()

OPENDESIGN_POSTERBENCH_DISCIPLINE_MAP = {
    "ai": "ai_ml_existing_20",
    "biomed-health": "biomed_health",
    "climate-earth-environment": "climate_earth_environment",
    "economics-policy": "economics_policy",
    "physics-astronomy": "physics_astronomy",
}


CLAUDE_CASE_MAP = {
    "ai_ml_existing_20": {
        "ceconv": "nips2023_color_equivariant_cnn",
        "clip": "2021-learning-transferable-visual-models-from-natural-language-supervision",
        "crop": "iclr2022_crop_rl_cert",
        "ddpm": "2020-denoising-diffusion-probabilistic-models",
        "demo": "neurips2024_demo_motion",
        "ds-1000": "icml2023_ds1000",
        "icl transformers": "icml2024_cot_transformers",
        "lcbm": "iclr2024_lcbm",
        "longcat-next": "arxiv2026_longcat_next",
        "mask r-cnn": "2017-mask-r-cnn",
        "nerf": "2020-nerf-representing-scenes-as-neural-radiance-fields-for-view-synthesis",
        "patchrot": "nips2022_patchrot",
        "sam 2": "sam2",
        "tores": "icml2024_imvc",
        "transformer": "2017-attention-is-all-you-need",
        "usis-sam": "icml2024_underwater_sam",
        "vit": "vit",
        "videogui": "neurips2024_videogui",
        "vript": "neurips2024_vript",
        "ivideogpt": "neurips2024_ivideogpt",
    },
    "biomed_health": {
        "2016 esc heart failure": "2016-2016-esc-guidelines-for-the-diagnosis-and-treatment-of-acute-and-chronic-heart-failure",
        "af guidelines": "2020-2020-esc-guidelines-for-the-diagnosis-and-management-of-atrial-fibrillation-developed",
        "adipose macrophage": "2003-obesity-is-associated-with-macrophage-accumulation-in-adipose-tissue",
        "cms colorectal cancer": "2015-the-consensus-molecular-subtypes-of-colorectal-cancer",
        "eid global trends": "2008-global-trends-in-emerging-infectious-diseases",
        "eln 2022 aml": "2022-diagnosis-and-management-of-aml-in-adults-2022-recommendations-from-an-international-expert-pane",
        "gbd 2010 yld": "2012-years-lived-with-disability-ylds-for-1160-sequelae-of-289-diseases-and-injuries-1990-2",
        "gbd 2016 yld": "2017-global-regional-and-national-incidence-prevalence-and-years-lived-with-disability-for",
        "gbd 2019": "2020-global-burden-of-369-diseases-and-injuries-in-204-countries-and-territories-1990-2019",
        "global obesity": "2014-global-regional-and-national-prevalence-of-overweight-and-obesity-in-children-and-adul",
        "luad molecular profiling": "2014-comprehensive-molecular-profiling-of-lung-adenocarcinoma",
        "lung sqcc": "2012-comprehensive-genomic-characterization-of-squamous-cell-lung-cancers",
        "mcp-counter": "2016-estimating-the-population-abundance-of-tissue-infiltrating-immune-and-stromal-cell-populations-u",
        "mimic-iii": "2016-mimic-iii-a-freely-accessible-critical-care-database",
        "sarcopenia ewgsop2": "2018-sarcopenia-revised-european-consensus-on-definition-and-diagnosis",
        "stupp 2005 gbm": "2005-radiotherapy-plus-concomitant-and-adjuvant-temozolomide-for-glioblastoma",
        "tcga colorectal cancer": "2012-comprehensive-molecular-characterization-of-human-colon-and-rectal-cancer",
        "tmb landscape": "2017-analysis-of-100-000-human-cancer-genomes-reveals-the-landscape-of-tumor-mutational-burden",
        "ccrcc": "2013-comprehensive-molecular-characterization-of-clear-cell-renal-cell-carcinoma",
        "proc": "2011-proc-an-open-source-package-for-r-and-s-to-analyze-and-compare-roc-curves",
    },
    "climate_earth_environment": {
        "biochar": "2010-sustainable-biochar-to-mitigate-global-climate-change",
        "c4mip": "2006-climate-carbon-cycle-feedback-analysis-results-from-the-c4mip-model-intercomparison",
        "chelsa": "2017-climatologies-at-high-resolution-for-the-earth-s-land-surface-areas",
        "cnrm-cm5.1": "2012-the-cnrm-cm5-1-global-climate-model-description-and-basic-evaluation",
        "cru ts v4": "2020-version-4-of-the-cru-ts-monthly-high-resolution-gridded-multivariate-climate-dataset",
        "forest integrity": "2020-anthropogenic-modification-of-forests-means-only-40-of-remaining-forests-have-high-ecosystem-int",
        "gfed3 fire emissions": "2010-global-fire-emissions-and-the-contribution-of-deforestation-savanna-forest-agricultura",
        "gfed4s fire emissions": "2017-global-fire-emissions-estimates-during-1997-2016",
        "global carbon budget 2020": "2020-global-carbon-budget-2020",
        "global carbon budget 2023": "2023-global-carbon-budget-2023",
        "greening": "2016-greening-of-the-earth-and-its-drivers",
        "habitat fragmentation": "2015-habitat-fragmentation-and-its-lasting-impact-on-earth-s-ecosystems",
        "ipsl-cm5": "2013-climate-change-projections-using-the-ipsl-cm5-earth-system-model-from-cmip3-to-cmip5",
        "mena": "2012-molecular-ecological-network-analyses",
        "miroc-esm 2010": "2011-miroc-esm-2010-model-description-and-basic-results-of-cmip5-20c3m-experiments",
        "np limitation": "2007-global-analysis-of-nitrogen-and-phosphorus-limitation-of-primary-producers-in-freshwat",
        "rcp ghg": "2011-the-rcp-greenhouse-gas-concentrations-and-their-extensions-from-1765-to-2300",
        "ssp ghg": "2020-the-shared-socio-economic-pathway-ssp-greenhouse-gas-concentrations-and-their-extensions-to-2500",
        "scenariomip": "2016-the-scenario-model-intercomparison-project-scenariomip-for-cmip6",
        "tropospheric ozone": "2015-tropospheric-ozone-and-its-precursors-from-the-urban-to-the-global-scale-from-air-quality-to-sho",
    },
    "economics_policy": {
        "class compromise": "2000-working-class-power-capitalist-class-interests-and-class-compromise",
        "currency crashes": "1996-currency-crashes-in-emerging-markets-an-empirical-treatment",
        "detecting discrimination": "1998-detecting-discrimination",
        "economic complexity": "2009-the-building-blocks-of-economic-complexity",
        "financial markets hierarchy": "1999-hierarchical-structure-in-financial-markets",
        "graduating in a recession": "2012-the-short-and-long-term-career-effects-of-graduating-in-a-recession",
        "household spending covid-19": "2020-how-does-household-spending-respond-to-an-epidemic-consumption-during-the-2020-covid-19-pandemic",
        "importing political polarization": "2020-importing-political-polarization-the-electoral-consequences-of-rising-trade-exposure",
        "new economy": "2000-does-the-new-economy-measure-up-to-the-great-inventions-of-the-past",
        "ppp debate": "2004-the-purchasing-power-parity-debate",
        "rare disasters": "2006-rare-disasters-and-asset-markets-in-the-twentieth-century",
        "superstar firms": "2020-the-fall-of-the-labor-share-and-the-rise-of-superstar-firms",
        "teacher value-added": "2014-measuring-the-impacts-of-teachers-ii-teacher-value-added-and-student-outcomes-in-adulthood",
        "the superiority of economists": "2015-the-superiority-of-economists",
        "top 1 percent": "2013-the-top-1-percent-in-international-and-historical-perspective",
        "transportation costs": "2007-transportation-costs-and-international-trade-in-the-second-era-of-globalization",
        "unequal we stand": "2009-unequal-we-stand-an-empirical-analysis-of-economic-inequality-in-the-united-states-1967-2006",
        "well-being": "2020-well-being-is-more-than-happiness-and-life-satisfaction-a-multidimensional-analysis-of-21-countr",
        "why do the poor live in cities": "2007-why-do-the-poor-live-in-cities-the-role-of-public-transportation",
        "world urbanization": "2005-world-urbanization-prospects",
    },
    "physics_astronomy": {
        "atlas higgs discovery": "2012-observation-of-a-new-particle-in-the-search-for-the-standard-model-higgs-boson-with-the-atlas-de",
        "accelerating universe": "1998-observational-evidence-from-supernovae-for-an-accelerating-universe-and-a-cosmological",
        "bicep2 b-mode": "2014-detection-of-b-mode-polarization-at-degree-angular-scales-by-bicep2",
        "cms higgs discovery": "2012-observation-of-a-new-boson-at-a-mass-of-125-gev-with-the-cms-experiment",
        "community structure": "2004-finding-and-evaluating-community-structure-in-networks",
        "complex networks": "2003-the-structure-and-function-of-complex-networks",
        "daya bay": "2012-first-measurement-of-theta13-from-daya-bay",
        "dust maps": "1998-maps-of-dust-infrared-emission-for-use-in-estimation-of-reddening-and-cosmic-microwave",
        "gw150914": "2016-observation-of-gravitational-waves-from-a-binary-black-hole-merger",
        "gwtc-1": "2019-gwtc-1-a-gravitational-wave-transient-catalog-of-compact-binary-mergers-observed-by-ligo-and-vir",
        "illustris project": "2014-the-illustris-simulation-the-evolution-of-galaxy-populations-across-cosmic-time",
        "lofar": "2013-lofar-the-low-frequency-array",
        "louvain method": "2008-fast-unfolding-of-communities-in-large-networks",
        "m87 black hole": "2019-first-m87-event-horizon-telescope-results-i-the-shadow-of-the-supermassive-black-hole",
        "planck 2018 cosmology": "2018-planck-2018-results-vi-cosmological-parameters",
        "quantum espresso": "2009-quantum-espresso-a-modular-and-open-source-software-project-for-quantum-simulations-of",
        "statistics networks": "2002-statistical-mechanics-of-complex-networks",
        "supernova cosmology": "1999-measurements-of-and-from-42-high-redshift-supernovae",
        "topological insulators": "2011-topological-insulators-and-superconductors",
    },
}

CLAUDE_EXACT_STEM_CASE_MAP = {
    "physics_astronomy": {
        "Topological Insulators Poster": "2011-topological-insulators-and-superconductors",
        "Topological Insulators Poster (standalone)": "2010-colloquium-topological-insulators",
    },
}


@dataclass(frozen=True)
class CandidateJob:
    system: str
    discipline: str
    case: str
    paper: Path
    artifact: Path | None
    source_name: str
    status: str = "ready"
    note: str = ""


def _clean_claude_key(stem: str) -> str:
    key = stem.lower()
    for token in (" poster", "(standalone)", "- standalone"):
        key = key.replace(token, "")
    return " ".join(key.replace("_", " ").strip(" -_").split())


def _cases_for_discipline(paper_root: Path, discipline: str) -> list[str]:
    return sorted(p.parent.name for p in (paper_root / discipline).glob("*/paper.pdf"))


def _discover_jobs(
    *,
    paper_root: Path,
    design_root: Path,
    opendesign_posterbench_root: Path,
    autodesign_links_root: Path,
    claude_root: Path,
    codex_native_root: Path,
    codex_posterly_root: Path,
    codex_posterskill_root: Path,
    codex_pptxposterskill_root: Path,
    systems: set[str],
) -> tuple[list[CandidateJob], list[dict[str, Any]]]:
    jobs: list[CandidateJob] = []
    mapping_rows: list[dict[str, Any]] = []
    opendesign_index = _opendesign_posterbench_index(opendesign_posterbench_root)
    autodesign_index = _autodesign_links_index(autodesign_links_root)

    def add_case_dir_system(system: str, root: Path, artifact_rel: Path, missing_note: str, cases: list[str], discipline: str) -> None:
        if system not in systems:
            return
        for case in cases:
            artifact = root / discipline / case / artifact_rel
            status = "ready" if artifact.exists() else "missing_artifact"
            job = CandidateJob(
                system=system,
                discipline=discipline,
                case=case,
                paper=paper_root / discipline / case / "paper.pdf",
                artifact=artifact if artifact.exists() else None,
                source_name=str(artifact),
                status=status,
                note="" if artifact.exists() else missing_note,
            )
            jobs.append(job)
            mapping_rows.append(_mapping_row(job, "direct case directory"))

    for discipline in DISCIPLINES:
        cases = _cases_for_discipline(paper_root, discipline)
        case_set = set(cases)
        if "designanything" in systems:
            for case in cases:
                artifact = design_root / discipline / case / "preview.png"
                status = "ready" if artifact.exists() else "missing_artifact"
                job = CandidateJob(
                    system="designanything",
                    discipline=discipline,
                    case=case,
                    paper=paper_root / discipline / case / "paper.pdf",
                    artifact=artifact if artifact.exists() else None,
                    source_name=str(artifact),
                    status=status,
                    note="" if artifact.exists() else "AutoDesign preview.png missing",
                )
                jobs.append(job)
                mapping_rows.append(_mapping_row(job, "direct case directory"))

        if "opendesign_posterbench" in systems:
            for case in cases:
                artifact_dir = opendesign_index.get((discipline, _case_key(case)))
                artifact = artifact_dir / "poster.png" if artifact_dir else None
                status = "ready" if artifact and artifact.exists() else "missing_artifact"
                job = CandidateJob(
                    system="opendesign_posterbench",
                    discipline=discipline,
                    case=case,
                    paper=paper_root / discipline / case / "paper.pdf",
                    artifact=artifact if artifact and artifact.exists() else None,
                    source_name=str(artifact_dir / "poster.html") if artifact_dir else "",
                    status=status,
                    note="" if status == "ready" else "OpenDesign PosterBench poster.png missing",
                )
                jobs.append(job)
                mapping_rows.append(_mapping_row(job, "opendesign posterbench artifact directory"))

        if "autodesign" in systems:
            for case in cases:
                indexed = autodesign_index.get((discipline, _case_key(case)))
                artifact = indexed[0] if indexed else None
                source = indexed[1] if indexed else None
                status = "ready" if artifact and artifact.exists() else "missing_artifact"
                job = CandidateJob(
                    system="autodesign",
                    discipline=discipline,
                    case=case,
                    paper=paper_root / discipline / case / "paper.pdf",
                    artifact=artifact if artifact and artifact.exists() else None,
                    source_name=str(source or artifact or ""),
                    status=status,
                    note="" if status == "ready" else "AutoDesign preview.png missing",
                )
                jobs.append(job)
                mapping_rows.append(_mapping_row(job, "autodesign links preview"))

        add_case_dir_system(
            "codex_native",
            codex_native_root,
            Path("poster.png"),
            "Codex native poster.png missing",
            cases,
            discipline,
        )
        add_case_dir_system(
            "codex_posterly",
            codex_posterly_root,
            Path("poster.png"),
            "Codex posterly poster.png missing",
            cases,
            discipline,
        )
        add_case_dir_system(
            "codex_posterskill",
            codex_posterskill_root,
            Path("poster") / "poster.png",
            "Codex posterskill poster/poster.png missing",
            cases,
            discipline,
        )
        add_case_dir_system(
            "codex_pptxposterskill",
            codex_pptxposterskill_root,
            Path("poster.png"),
            "Codex PPTX posterskill poster.png missing",
            cases,
            discipline,
        )

        if "claude_design" in systems:
            used_cases: set[str] = set()
            pngs = sorted((claude_root / discipline).glob("*.png"))
            for png in pngs:
                key = _clean_claude_key(png.stem)
                case = (
                    CLAUDE_EXACT_STEM_CASE_MAP.get(discipline, {}).get(png.stem)
                    or CLAUDE_CASE_MAP.get(discipline, {}).get(key)
                )
                if not case:
                    job = CandidateJob(
                        system="claude_design",
                        discipline=discipline,
                        case="",
                        paper=Path(),
                        artifact=png,
                        source_name=png.name,
                        status="unmapped",
                        note=f"No Claude mapping for key: {key}",
                    )
                    jobs.append(job)
                    mapping_rows.append(_mapping_row(job, "unmapped"))
                    continue
                status = "ready"
                note = ""
                if case not in case_set:
                    status = "bad_mapping"
                    note = f"Mapped case not found in paper corpus: {case}"
                if case in used_cases:
                    status = "duplicate_mapping"
                    note = f"Duplicate Claude mapping for case: {case}"
                used_cases.add(case)
                job = CandidateJob(
                    system="claude_design",
                    discipline=discipline,
                    case=case,
                    paper=paper_root / discipline / case / "paper.pdf",
                    artifact=png,
                    source_name=png.name,
                    status=status,
                    note=note,
                )
                jobs.append(job)
                mapping_rows.append(_mapping_row(job, f"claude key: {key}"))
            for case in cases:
                if case not in used_cases:
                    job = CandidateJob(
                        system="claude_design",
                        discipline=discipline,
                        case=case,
                        paper=paper_root / discipline / case / "paper.pdf",
                        artifact=None,
                        source_name="",
                        status="missing_artifact",
                        note="No Claude PNG mapped to this benchmark case",
                    )
                    jobs.append(job)
                    mapping_rows.append(_mapping_row(job, "missing mapped png"))
    return jobs, mapping_rows


def _opendesign_posterbench_index(root: Path) -> dict[tuple[str, str], Path]:
    artifacts_root = root / "artifacts" if (root / "artifacts").is_dir() else root
    index: dict[tuple[str, str], Path] = {}
    if not artifacts_root.is_dir():
        return index
    for artifact_dir in sorted(p for p in artifacts_root.iterdir() if p.is_dir()):
        parsed = _parse_opendesign_artifact_dir(artifact_dir.name)
        if not parsed:
            continue
        discipline, case_slug = parsed
        index[(discipline, _case_key(case_slug))] = artifact_dir
    return index


def _autodesign_links_index(root: Path) -> dict[tuple[str, str], tuple[Path, Path | None]]:
    index: dict[tuple[str, str], tuple[Path, Path | None]] = {}
    if not root.is_dir():
        return index
    suffix = "-preview.png"
    for preview in sorted(root.glob(f"*{suffix}")):
        stem = preview.name[:-len(suffix)]
        parsed = _parse_opendesign_artifact_dir(stem)
        if not parsed:
            continue
        discipline, case_slug = parsed
        poster_html = root / f"{stem}-poster.html"
        index[(discipline, _case_key(case_slug))] = (preview, poster_html if poster_html.exists() else None)
    return index


def _parse_opendesign_artifact_dir(name: str) -> tuple[str, str] | None:
    parts = name.split("-")
    if len(parts) < 3 or not parts[0].isdigit():
        return None
    for raw_discipline, discipline in OPENDESIGN_POSTERBENCH_DISCIPLINE_MAP.items():
        prefix = f"{parts[0]}-{raw_discipline}-"
        if name.startswith(prefix):
            return discipline, name[len(prefix):]
    return None


def _case_key(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _mapping_row(job: CandidateJob, mapping_method: str) -> dict[str, Any]:
    return {
        "system": job.system,
        "system_label": SYSTEM_LABELS.get(job.system, job.system),
        "discipline": job.discipline,
        "discipline_label": DISCIPLINE_LABELS.get(job.discipline, job.discipline),
        "case": job.case,
        "paper": str(job.paper) if job.paper else "",
        "artifact": str(job.artifact) if job.artifact else "",
        "source_name": job.source_name,
        "status": job.status,
        "mapping_method": mapping_method,
        "note": job.note,
    }


def _paper_text(job: CandidateJob, paper_cache_dir: Path, *, paper_sha256: str) -> str:
    cache = paper_cache_dir / job.discipline / f"{job.case}.txt"
    metadata_path = cache.with_suffix(".meta.json")
    metadata = _load_json(metadata_path)
    if cache.exists() and metadata and metadata.get("paper_sha256") == paper_sha256:
        return cache.read_text(encoding="utf-8", errors="replace")
    cache.parent.mkdir(parents=True, exist_ok=True)
    text = _extract_paper_text(job.paper)
    cache.write_text(text, encoding="utf-8")
    atomic_write_json(metadata_path, {"paper_sha256": paper_sha256})
    return text


def _paper_brief(
    job: CandidateJob,
    text: str,
    brief_dir: Path,
    *,
    paper_sha256: str,
) -> dict[str, Any]:
    path = brief_dir / job.discipline / f"{job.case}.json"
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("paper_sha256") == paper_sha256:
            cached.pop("paper_path", None)
            return cached
    cleaned = " ".join(text.split())
    head = cleaned[:5000]
    tail = cleaned[-1800:] if len(cleaned) > 7000 else ""
    brief = {
        "case_slug": job.case,
        "discipline": job.discipline,
        "paper_sha256": paper_sha256,
        "paper_excerpt_head": head,
        "paper_excerpt_tail": tail,
        "note": "Compact local text digest for direct benchmark VLM judge; not a full paper-brief agent call.",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, brief)
    return brief


def _judge_image(src: Path, cdir: Path) -> Path:
    dst = cdir / "judge_input.jpg"
    metadata_path = cdir / "judge_input.meta.json"
    source_sha256 = _file_sha256(src)
    metadata = _load_json(metadata_path)
    if (
        dst.exists()
        and dst.stat().st_size > 0
        and metadata
        and metadata.get("source_sha256") == source_sha256
    ):
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as image:
        image = image.convert("RGB")
        image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
        image.save(dst, format="JPEG", quality=88, optimize=True)
    atomic_write_json(metadata_path, {
        "source": str(src),
        "source_sha256": source_sha256,
        "judge_input_sha256": _file_sha256(dst),
    })
    return dst


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _detector_preflight() -> dict[str, Any]:
    checks: dict[str, Any] = {}
    try:
        import rapidocr_onnxruntime  # type: ignore  # noqa: F401
        checks["rapidocr_onnxruntime"] = {"available": True}
    except Exception as exc:  # noqa: BLE001
        checks["rapidocr_onnxruntime"] = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        import cv2  # type: ignore  # noqa: F401
        checks["cv2"] = {"available": True}
    except Exception as exc:  # noqa: BLE001
        checks["cv2"] = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    ok = all(item.get("available") for item in checks.values())
    return {
        "status": "ok" if ok else "degraded",
        "checks": checks,
        "install_hint": "Run benchmark commands with: uv --cache-dir .uv-cache run --extra ocr ...",
    }


def _matches_evaluator_fingerprint(data: dict[str, Any] | None) -> bool:
    if not isinstance(data, dict):
        return False
    source_fingerprint = data.get("evaluator_fingerprint")
    if source_fingerprint is not None:
        return (
            data.get("eval_protocol") == EVAL_PROTOCOL
            and source_fingerprint in {
                EVALUATOR_FINGERPRINT,
                *_LEGACY_EVALUATOR_FINGERPRINTS,
            }
        )
    return data.get("rubric_version") in _LEGACY_DETERMINISTIC_EQUIVALENT_RUBRIC_VERSIONS


def _valid_vlm_result(
    data: dict[str, Any] | None,
    *,
    judge_model: str | None = None,
    legacy_rubric_versions: set[str] | None = None,
    dimension: str | None = None,
    expected_input_fingerprint: str | None = None,
) -> bool:
    if not isinstance(data, dict):
        return False
    current_prompt = (
        data.get("eval_protocol") == EVAL_PROTOCOL
        and data.get("vlm_prompt_fingerprint") == VLM_PROMPT_FINGERPRINT
    )
    supplied_prompt_fingerprint = data.get("vlm_prompt_fingerprint")
    if supplied_prompt_fingerprint is not None:
        legacy_prompt = supplied_prompt_fingerprint in _LEGACY_VLM_PROMPT_FINGERPRINTS
    else:
        legacy_prompt = data.get("rubric_version") in (legacy_rubric_versions or set())
    if not current_prompt and not legacy_prompt:
        return False
    if current_prompt and expected_input_fingerprint is not None:
        if data.get("vlm_input_fingerprint") != expected_input_fingerprint:
            return False
    if current_prompt and dimension is not None and data.get("dimension") != dimension:
        return False
    if judge_model is not None and data.get("model") != judge_model:
        return False
    if data.get("status") != "ok":
        return False
    try:
        score = float(data.get("score_0_10"))
    except (TypeError, ValueError):
        return False
    return math.isfinite(score) and 0.0 <= score <= 10.0


def _complete_final_report(
    data: dict[str, Any] | None,
    *,
    artifact_sha256: str | None = None,
    paper_sha256: str | None = None,
    judge_model: str | None = None,
) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("eval_protocol") != EVAL_PROTOCOL:
        return False
    if data.get("evaluator_fingerprint") not in {
        BENCHMARK_EVALUATOR_FINGERPRINT,
        *_LEGACY_BENCHMARK_EVALUATOR_FINGERPRINTS,
    }:
        return False
    if data.get("reaggregation_status") == "degraded":
        return False
    source_fingerprint = data.get("source_evaluator_fingerprint")
    legacy_source = data.get("legacy_source_rubric_version")
    compatible_sources = {
        EVALUATOR_FINGERPRINT,
        BENCHMARK_EVALUATOR_FINGERPRINT,
        *_LEGACY_EVALUATOR_FINGERPRINTS,
        *_LEGACY_BENCHMARK_EVALUATOR_FINGERPRINTS,
    }
    if source_fingerprint is not None:
        if source_fingerprint not in compatible_sources:
            return False
    elif legacy_source not in _LEGACY_FINAL_EQUIVALENT_RUBRIC_VERSIONS:
        return False
    if not _compatible_final_vlm_prompt(data.get("vlm_prompt_fingerprint")):
        return False
    if artifact_sha256 is not None and data.get("artifact_sha256") != artifact_sha256:
        return False
    if paper_sha256 is not None and data.get("paper_sha256") != paper_sha256:
        return False
    if judge_model is not None and data.get("judge_model") != judge_model:
        return False
    if any(_is_retired_single_dimension_cap(finding) for finding in data.get("findings", []) or []):
        return False
    dims: dict[str, Any] = {}
    for dim in data.get("dimensions", []) or []:
        if isinstance(dim, dict):
            dims[str(dim.get("id"))] = dim.get("score_0_10")
    for dim in ALL_DIMS:
        try:
            score = float(dims.get(dim))
        except (TypeError, ValueError):
            return False
        if not math.isfinite(score) or score < 0.0 or score > 10.0:
            return False
    try:
        overall = float(data.get("overall_score_0_100"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(overall) or overall < 0.0 or overall > 100.0:
        return False
    expected_overall = sum(
        dim.weight * float(dims[dim.id]) / 10.0
        for dim in DIMENSIONS
    )
    viability_policy = _presentation_viability_policy(dims)
    viability_findings = [
        finding
        for finding in data.get("findings", []) or []
        if isinstance(finding, dict)
        and finding.get("id") == _PRESENTATION_VIABILITY_CAP_FINDING_ID
    ]
    if viability_policy is None:
        if viability_findings:
            return False
    else:
        if len(viability_findings) != 1:
            return False
        evidence = viability_findings[0].get("evidence")
        if not isinstance(evidence, dict):
            return False
        cached_ceiling = _finite_float(evidence.get("score_ceiling"))
        expected_ceiling = _finite_float(viability_policy.get("score_ceiling"))
        if cached_ceiling is None or expected_ceiling is None or abs(cached_ceiling - expected_ceiling) > 0.01:
            return False
    ceiling_candidates: list[float] = []
    if data.get("gate_triggered"):
        gate_ceiling = _finite_float(data.get("gate_ceiling"))
        if gate_ceiling is None:
            return False
        ceiling_candidates.append(gate_ceiling)
    recognized_ceiling_keys = {
        "layout-coupled-score-cap": "overall_ceiling",
        _PRESENTATION_VIABILITY_CAP_FINDING_ID: "score_ceiling",
        "deterministic-major-visual-failure": "score_ceiling",
        "judge-confirmed-major-visual-failure": "score_ceiling",
        "judge-confirmed-serious-visual-defect": "score_ceiling",
    }
    for finding in data.get("findings", []) or []:
        if not isinstance(finding, dict):
            continue
        ceiling_key = recognized_ceiling_keys.get(str(finding.get("id") or ""))
        if ceiling_key is None:
            continue
        evidence = finding.get("evidence")
        if not isinstance(evidence, dict):
            continue
        ceiling = _finite_float(evidence.get(ceiling_key))
        if ceiling is not None:
            ceiling_candidates.append(ceiling)
    expected_final = min([expected_overall, *ceiling_candidates])
    if abs(overall - expected_final) > 0.05:
        return False
    return True


def _compatible_final_vlm_prompt(value: Any) -> bool:
    if value in {VLM_PROMPT_FINGERPRINT, *_LEGACY_VLM_PROMPT_FINGERPRINTS}:
        return True
    prefix = "legacy-rubric:"
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and value.removeprefix(prefix) in _LEGACY_VLM_EQUIVALENT_RUBRIC_VERSIONS
    )


def _run_vlm_dim(
    *,
    dim: str,
    image: Path,
    brief: dict[str, Any],
    deterministic: dict[str, Any],
    cdir: Path,
    model: str | None,
    force: bool,
    retries: int,
    call_limiter: _VLMCallLimiter | None = None,
) -> dict[str, Any]:
    judge_model = model or DEFAULT_BENCHMARK_JUDGE_MODEL
    path = cdir / "vlm" / f"{dim}.json"
    grounding = None
    if dim in {"visual_evidence_use", "layout_readability", "professional_aesthetics"}:
        grounding = dict(deterministic.get("metric_bundles", {}) or {})
        dimension_components = deterministic.get("dimension_components", {}) or {}
        if dimension_components:
            grounding["dimension_components"] = dimension_components
    input_fingerprint = structured_fingerprint(
        {
            "eval_protocol": EVAL_PROTOCOL,
            "vlm_prompt_fingerprint": VLM_PROMPT_FINGERPRINT,
            "dimension": dim,
            "judge_model": judge_model,
            "image_sha256": _file_sha256(image),
            "paper_brief": brief,
            "grounding_text": _format_grounding(dim, grounding),
        },
        namespace=f"{EVAL_PROTOCOL}:vlm-input",
    )
    if not force:
        cached = _load_json(path)
        if _valid_vlm_result(
            cached,
            judge_model=judge_model,
            legacy_rubric_versions=_legacy_compatible_vlm_rubric_versions(dim),
            dimension=dim,
            expected_input_fingerprint=input_fingerprint,
        ):
            return cached  # type: ignore[return-value]
    path.parent.mkdir(parents=True, exist_ok=True)
    last: dict[str, Any] | None = None
    max_attempts = max(1, retries)
    for attempt in range(1, max_attempts + 1):
        if call_limiter is not None:
            call_limiter.wait()
        try:
            result = tool_vlm_judge(
                dimension=dim,
                image=image,
                paper_brief=brief,
                grounding=grounding,
                model=judge_model,
            )
        except Exception as exc:  # noqa: BLE001
            result = {
                "tool": "vlm_judge",
                "dimension": dim,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "attempt": attempt,
            }
        result.pop("rubric_version", None)
        result["eval_protocol"] = EVAL_PROTOCOL
        result["vlm_prompt_fingerprint"] = VLM_PROMPT_FINGERPRINT
        result["vlm_input_fingerprint"] = input_fingerprint
        last = result
        atomic_write_json(path, result)
        if _valid_vlm_result(
            result,
            judge_model=judge_model,
            legacy_rubric_versions=_legacy_compatible_vlm_rubric_versions(dim),
            dimension=dim,
            expected_input_fingerprint=input_fingerprint,
        ):
            return result
        if attempt < max_attempts:
            time.sleep(min(8, 1.5 * attempt))
    assert last is not None
    return last


def _score_job(
    job: CandidateJob,
    *,
    out_dir: Path,
    model: str | None,
    force: bool,
    force_vlm: bool,
    force_vlm_dims: set[str] | None = None,
    retries: int,
    vlm_call_limiter: _VLMCallLimiter | None = None,
) -> dict[str, Any]:
    judge_model = model or DEFAULT_BENCHMARK_JUDGE_MODEL
    base = {
        "system": job.system,
        "system_label": SYSTEM_LABELS.get(job.system, job.system),
        "discipline": job.discipline,
        "discipline_label": DISCIPLINE_LABELS.get(job.discipline, job.discipline),
        "case": job.case,
        "artifact": str(job.artifact) if job.artifact else "",
        "paper": str(job.paper) if job.paper else "",
        "candidate_name": f"{SYSTEM_LABELS.get(job.system, job.system)}::{job.discipline}/{job.case}",
        "status": job.status,
        "note": job.note,
    }
    if job.status != "ready" or not job.artifact:
        return {**base, "overall": None, "verdict": "missing", "dimensions": {}, "dimension_status": {}}

    cdir = out_dir / "candidates" / job.system / job.discipline / _safe_name(job.case)
    final_path = cdir / "poster_quality_report.json"
    artifact_sha256 = _file_sha256(job.artifact)
    paper_sha256 = _file_sha256(job.paper)
    cached_final = _load_json(final_path) if final_path.exists() else None
    artifact_changed = not cached_final or cached_final.get("artifact_sha256") != artifact_sha256
    selected_force_vlm_dims = set(force_vlm_dims or ())
    if final_path.exists() and not force and not force_vlm and not selected_force_vlm_dims:
        if _complete_final_report(
            cached_final,
            artifact_sha256=artifact_sha256,
            paper_sha256=paper_sha256,
            judge_model=judge_model,
        ):
            if cached_final.get("evaluator_fingerprint") in _LEGACY_BENCHMARK_EVALUATOR_FINGERPRINTS:
                cached_final = _reaggregate_final_report(cached_final)
                atomic_write_json(final_path, cached_final)
                return _record_from_final(job, cached_final, "reaggregated")
            return _record_from_final(job, cached_final, "cached")

    paper_text = _paper_text(
        job,
        out_dir / "paper_text_cache",
        paper_sha256=paper_sha256,
    )
    brief = _paper_brief(
        job,
        paper_text,
        out_dir / "paper_brief_cache",
        paper_sha256=paper_sha256,
    )

    det_path = cdir / "deterministic" / "deterministic_report.json"
    deterministic = _load_json(det_path) if not force and not artifact_changed else None
    if not _matches_evaluator_fingerprint(deterministic):
        deterministic = None
    if not deterministic:
        deterministic = compute_deterministic_report(
            paper=job.paper,
            candidate_artifact=job.artifact,
            out_dir=cdir / "deterministic",
            case_slug=job.case,
        )

    image = _judge_image(job.artifact, cdir)
    dim_scores: dict[str, dict[str, Any]] = {}
    for dim in VLM_DIMS:
        result = _run_vlm_dim(
            dim=dim,
            image=image,
            brief=brief,
            deterministic=deterministic,
            cdir=cdir,
            model=model,
            force=force_vlm or artifact_changed or dim in selected_force_vlm_dims,
            retries=retries,
            call_limiter=vlm_call_limiter,
        )
        if _valid_vlm_result(
            result,
            judge_model=judge_model,
            legacy_rubric_versions=_legacy_compatible_vlm_rubric_versions(dim),
        ):
            dim_scores[dim] = {
                "score_0_10": result.get("score_0_10"),
                "rationale": result.get("rationale", ""),
                "visible_evidence": result.get("visible_evidence", []),
                "defects_found": result.get("defects_found", []),
                "judge_confidence": result.get("judge_confidence"),
            }
    judge_report = {"dimension_scores": dim_scores}
    final = aggregate_final(
        deterministic,
        judge_report,
        mode="benchmark",
        candidate_name=base["candidate_name"],
        artifact=job.artifact,
        paper=job.paper,
        deterministic_path=str(det_path),
        judge_path=str(cdir / "vlm"),
    ).to_dict()
    final["source_evaluator_fingerprint"] = final.get("evaluator_fingerprint")
    final["evaluator_fingerprint"] = BENCHMARK_EVALUATOR_FINGERPRINT
    final["vlm_prompt_fingerprint"] = VLM_PROMPT_FINGERPRINT
    final["artifact_sha256"] = artifact_sha256
    final["paper_sha256"] = paper_sha256
    final["judge_input_sha256"] = _file_sha256(image)
    final["judge_model"] = judge_model
    atomic_write_json(final_path, final)
    return _record_from_final(job, final, "scored")


def _reaggregate_job(job: CandidateJob, *, out_dir: Path) -> dict[str, Any]:
    base = {
        "system": job.system,
        "system_label": SYSTEM_LABELS.get(job.system, job.system),
        "discipline": job.discipline,
        "discipline_label": DISCIPLINE_LABELS.get(job.discipline, job.discipline),
        "case": job.case,
        "artifact": str(job.artifact) if job.artifact else "",
        "paper": str(job.paper) if job.paper else "",
        "candidate_name": f"{SYSTEM_LABELS.get(job.system, job.system)}::{job.discipline}/{job.case}",
        "status": job.status,
        "note": job.note,
    }
    if job.status != "ready" or not job.artifact:
        return {**base, "overall": None, "verdict": "missing", "dimensions": {}, "dimension_status": {}}

    cdir = out_dir / "candidates" / job.system / job.discipline / _safe_name(job.case)
    final_path = cdir / "poster_quality_report.json"
    final = _load_json(final_path)
    if not final:
        return {
            **base,
            "overall": None,
            "verdict": "missing",
            "dimensions": {},
            "dimension_status": {},
            "note": f"missing cached poster_quality_report.json: {final_path}",
        }
    reaggregated = _reaggregate_final_report(final)
    expected_artifact_sha256 = _file_sha256(job.artifact)
    expected_paper_sha256 = _file_sha256(job.paper)
    source_integrity_errors: list[str] = []
    if final.get("artifact_sha256") != expected_artifact_sha256:
        source_integrity_errors.append("artifact_sha256_mismatch_or_missing")
    if final.get("paper_sha256") != expected_paper_sha256:
        source_integrity_errors.append("paper_sha256_mismatch_or_missing")
    if source_integrity_errors:
        reaggregated["reaggregation_status"] = "degraded"
        reaggregated["source_integrity"] = {
            "status": "degraded",
            "errors": source_integrity_errors,
            "expected_artifact_sha256": expected_artifact_sha256,
            "expected_paper_sha256": expected_paper_sha256,
            "cached_artifact_sha256": final.get("artifact_sha256"),
            "cached_paper_sha256": final.get("paper_sha256"),
        }
    if not _complete_final_report(
        reaggregated,
        artifact_sha256=expected_artifact_sha256,
        paper_sha256=expected_paper_sha256,
    ):
        reaggregated["reaggregation_status"] = "degraded"
        source_integrity = reaggregated.setdefault("source_integrity", {})
        source_integrity["status"] = "degraded"
        errors = source_integrity.setdefault("errors", [])
        if "reaggregated_report_incomplete_or_incompatible" not in errors:
            errors.append("reaggregated_report_incomplete_or_incompatible")
    atomic_write_json(final_path, reaggregated)
    status = (
        "reaggregated"
        if reaggregated.get("reaggregation_status") == "ok"
        else "reaggregated_degraded"
    )
    return _record_from_final(job, reaggregated, status)


def _reaggregate_final_report(final: dict[str, Any]) -> dict[str, Any]:
    legacy_source_rubric_version = str(
        final.get("legacy_source_rubric_version")
        or final.get("source_rubric_version")
        or final.get("rubric_version")
        or ""
    )
    if "source_evaluator_fingerprint" in final:
        source_evaluator_fingerprint = final.get("source_evaluator_fingerprint")
    else:
        source_evaluator_fingerprint = final.get("evaluator_fingerprint")
    if source_evaluator_fingerprint is not None:
        source_is_compatible = source_evaluator_fingerprint in {
            EVALUATOR_FINGERPRINT,
            BENCHMARK_EVALUATOR_FINGERPRINT,
            *_LEGACY_EVALUATOR_FINGERPRINTS,
            *_LEGACY_BENCHMARK_EVALUATOR_FINGERPRINTS,
        }
    else:
        source_is_compatible = (
            legacy_source_rubric_version in _LEGACY_FINAL_EQUIVALENT_RUBRIC_VERSIONS
        )
    previous_cap_original_scores: dict[str, float] = {}
    for finding in final.get("findings", []) or []:
        if not isinstance(finding, dict) or finding.get("id") not in _DIMENSION_CAP_FINDING_IDS:
            continue
        evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
        for item in evidence.get("capped_dimensions", []) or []:
            if not isinstance(item, dict) or not item.get("dimension"):
                continue
            original = _finite_float(item.get("original_score_0_10"))
            if original is not None:
                dim_id = str(item["dimension"])
                previous_cap_original_scores[dim_id] = max(
                    previous_cap_original_scores.get(dim_id, original),
                    original,
                )
    by_id = {
        str(dim.get("id")): dim
        for dim in final.get("dimensions", []) or []
        if isinstance(dim, dict) and dim.get("id")
    }
    findings = [
        dict(finding)
        for finding in final.get("findings", []) or []
        if isinstance(finding, dict)
        and finding.get("id") not in {
            *_DIMENSION_CAP_FINDING_IDS,
            _PRESENTATION_VIABILITY_CAP_FINDING_ID,
        }
        and not _is_retired_single_dimension_cap(finding)
    ]
    basic_layout_score = _finite_float((by_id.get("basic_layout_integrity") or {}).get("score_0_10"))
    layout_cap_policy = layout_coupled_cap_policy(
        basic_layout_score=basic_layout_score,
        findings=findings,
    )
    information_density_score = _finite_float(
        (by_id.get("information_density_and_synthesis") or {}).get("score_0_10")
    )
    aesthetics_cap_policy = academic_poster_aesthetics_cap_policy(
        information_density_score=information_density_score,
    )
    basic_layout_metrics = (by_id.get("basic_layout_integrity") or {}).get("metrics") or {}
    legibility_cap_policy = poster_scale_legibility_cap_policy(
        median_body_text_height_ref_px=_finite_float(
            basic_layout_metrics.get("median_body_text_height_ref_px")
            if isinstance(basic_layout_metrics, dict)
            else None
        ),
    )
    layout_dimension_caps = layout_cap_policy.get("dimension_caps", {})
    aesthetics_dimension_caps = aesthetics_cap_policy.get("dimension_caps", {})
    legibility_dimension_caps = legibility_cap_policy.get("dimension_caps", {})
    dimension_caps = _merge_dimension_caps(
        layout_dimension_caps,
        aesthetics_dimension_caps,
        legibility_dimension_caps,
    )
    capped_dimensions: list[dict[str, Any]] = []

    scored_weight = 0.0
    weighted = 0.0
    new_dims: list[dict[str, Any]] = []
    for dim in DIMENSIONS:
        old = dict(by_id.get(dim.id) or {})
        score = _finite_float(old.get("score_0_10"))
        prior_original = previous_cap_original_scores.get(dim.id)
        if prior_original is not None:
            score = prior_original
        cap = _finite_float(dimension_caps.get(dim.id) if isinstance(dimension_caps, dict) else None)
        cap_sources = _cap_sources_for_dimension(
            dim.id,
            score,
            layout_dimension_caps,
            aesthetics_dimension_caps,
            legibility_dimension_caps,
        )
        if score is not None and cap is not None and score > cap:
            capped_dimensions.append({
                "dimension": dim.id,
                "original_score_0_10": score,
                "capped_score_0_10": cap,
                "cap_sources": cap_sources,
            })
            score = cap
        norm = round(score / 10.0, 4) if score is not None else None
        if norm is not None:
            scored_weight += dim.weight
            weighted += dim.weight * norm
        metrics = dict(old.get("metrics") or {})
        metrics.pop("layout_coupled_cap", None)
        metrics.pop("academic_poster_aesthetics_cap", None)
        metrics.pop("poster_scale_legibility_cap", None)
        if any(item["dimension"] == dim.id and "layout" in item.get("cap_sources", []) for item in capped_dimensions):
            metrics["layout_coupled_cap"] = {
                "score_ceiling": _finite_float(layout_dimension_caps.get(dim.id) if isinstance(layout_dimension_caps, dict) else None),
                "original_score_0_10": next(
                    item["original_score_0_10"]
                    for item in capped_dimensions
                    if item["dimension"] == dim.id
                ),
                "basic_layout_score": basic_layout_score,
                "triggered_rules": layout_cap_policy.get("triggered_rules", []),
            }
        if any(item["dimension"] == dim.id and "academic_aesthetics" in item.get("cap_sources", []) for item in capped_dimensions):
            metrics["academic_poster_aesthetics_cap"] = {
                "score_ceiling": _finite_float(aesthetics_dimension_caps.get(dim.id) if isinstance(aesthetics_dimension_caps, dict) else None),
                "original_score_0_10": next(
                    item["original_score_0_10"]
                    for item in capped_dimensions
                    if item["dimension"] == dim.id
                ),
                "information_density_score": information_density_score,
                "triggered_rules": aesthetics_cap_policy.get("triggered_rules", []),
            }
        if any(item["dimension"] == dim.id and "poster_scale_legibility" in item.get("cap_sources", []) for item in capped_dimensions):
            metrics["poster_scale_legibility_cap"] = {
                "score_ceiling": _finite_float(
                    legibility_dimension_caps.get(dim.id)
                    if isinstance(legibility_dimension_caps, dict)
                    else None
                ),
                "original_score_0_10": next(
                    item["original_score_0_10"]
                    for item in capped_dimensions
                    if item["dimension"] == dim.id
                ),
                "median_body_text_height_ref_px": legibility_cap_policy.get(
                    "median_body_text_height_ref_px"
                ),
                "triggered_rules": legibility_cap_policy.get("triggered_rules", []),
            }
        old.update({
            "id": dim.id,
            "weight": dim.weight,
            "owner": dim.owner,
            "score_0_10": score,
            "normalized": norm,
            "status": "warning" if any(item["dimension"] == dim.id for item in capped_dimensions) else old.get("status") or ("ok" if score is not None else "needs_judge"),
            "metrics": metrics,
        })
        new_dims.append(old)

    overall = round(100.0 * weighted / scored_weight, 2) if scored_weight > 0 else None
    layout_overall_ceiling = _finite_float(layout_cap_policy.get("overall_ceiling"))
    if overall is not None and layout_overall_ceiling is not None:
        overall = min(overall, layout_overall_ceiling)
    layout_capped_dimensions = [
        item for item in capped_dimensions
        if "layout" in item.get("cap_sources", [])
    ]
    aesthetics_capped_dimensions = [
        item for item in capped_dimensions
        if "academic_aesthetics" in item.get("cap_sources", [])
    ]
    legibility_capped_dimensions = [
        item for item in capped_dimensions
        if "poster_scale_legibility" in item.get("cap_sources", [])
    ]
    if layout_capped_dimensions or layout_overall_ceiling is not None:
        findings.append({
            "id": "layout-coupled-score-cap",
            "severity": "P1" if layout_overall_ceiling is not None else "P2",
            "message": "Basic layout damage caps related visual-quality scores.",
            "dimension": "basic_layout_integrity",
            "evidence": {
                **layout_cap_policy,
                "capped_dimensions": layout_capped_dimensions,
            },
        })
    if aesthetics_capped_dimensions:
        findings.append({
            "id": "academic-poster-aesthetics-density-cap",
            "severity": "P2",
            "message": "Low information density caps human academic-poster aesthetics.",
            "dimension": "professional_aesthetics",
            "evidence": {
                **aesthetics_cap_policy,
                "capped_dimensions": aesthetics_capped_dimensions,
            },
        })
    if legibility_capped_dimensions:
        findings.append({
            "id": "poster-scale-legibility-cap",
            "severity": "P2",
            "message": "Poster-scale body text limits the layout-readability score.",
            "dimension": "layout_readability",
            "evidence": {
                **legibility_cap_policy,
                "capped_dimensions": legibility_capped_dimensions,
            },
        })
    presentation_viability_cap = _presentation_viability_policy({
        str(dim.get("id")): dim.get("score_0_10")
        for dim in new_dims
    })
    if overall is not None and presentation_viability_cap is not None:
        overall = min(overall, float(presentation_viability_cap["score_ceiling"]))
        findings.append({
            "id": _PRESENTATION_VIABILITY_CAP_FINDING_ID,
            "severity": "P1",
            "message": "Low presentation viability caps the overall poster score.",
            "dimension": "layout_readability",
            "evidence": presentation_viability_cap,
        })
    for finding in findings:
        if finding.get("id") not in {
            "deterministic-major-visual-failure",
            "judge-confirmed-major-visual-failure",
            "judge-confirmed-serious-visual-defect",
        }:
            continue
        evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
        stricter_ceiling = _finite_float(evidence.get("score_ceiling"))
        if overall is not None and stricter_ceiling is not None:
            overall = min(overall, stricter_ceiling)
    gate_triggered = bool(final.get("gate_triggered"))
    configured_ceiling = _finite_float(final.get("gate_ceiling"))
    ceiling = GATE_CEILING_SCORE if configured_ceiling is None else configured_ceiling
    if overall is not None and gate_triggered:
        overall = min(overall, ceiling)

    out = dict(final)
    out.pop("rubric_version", None)
    out.pop("source_rubric_version", None)
    out.update({
        "eval_protocol": EVAL_PROTOCOL,
        "evaluator_fingerprint": BENCHMARK_EVALUATOR_FINGERPRINT,
        "source_evaluator_fingerprint": source_evaluator_fingerprint,
        "vlm_prompt_fingerprint": (
            final.get("vlm_prompt_fingerprint")
            or (f"legacy-rubric:{legacy_source_rubric_version}" if legacy_source_rubric_version else None)
        ),
        "reaggregation_status": "ok" if source_is_compatible else "degraded",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_score_0_100": overall,
        "gate_ceiling": ceiling if gate_triggered else None,
        "gate_triggered": gate_triggered,
        "verdict": _benchmark_verdict(overall, gate_triggered),
        "dimensions": new_dims,
        "findings": findings,
        "finding_counts": _finding_counts_dict(findings),
    })
    if legacy_source_rubric_version:
        out["legacy_source_rubric_version"] = legacy_source_rubric_version
    else:
        out.pop("legacy_source_rubric_version", None)
    return out


def _merge_dimension_caps(*caps_list: Any) -> dict[str, float]:
    merged: dict[str, float] = {}
    for caps in caps_list:
        if not isinstance(caps, dict):
            continue
        for dim_id, raw_cap in caps.items():
            cap = _finite_float(raw_cap)
            if cap is None:
                continue
            merged[str(dim_id)] = min(merged.get(str(dim_id), cap), cap)
    return merged


def _cap_sources_for_dimension(
    dim_id: str,
    score: float | None,
    layout_dimension_caps: Any,
    aesthetics_dimension_caps: Any,
    legibility_dimension_caps: Any,
) -> list[str]:
    if score is None:
        return []
    sources: list[str] = []
    layout_cap = _finite_float(layout_dimension_caps.get(dim_id) if isinstance(layout_dimension_caps, dict) else None)
    aesthetics_cap = _finite_float(aesthetics_dimension_caps.get(dim_id) if isinstance(aesthetics_dimension_caps, dict) else None)
    legibility_cap = _finite_float(legibility_dimension_caps.get(dim_id) if isinstance(legibility_dimension_caps, dict) else None)
    if layout_cap is not None and score > layout_cap:
        sources.append("layout")
    if aesthetics_cap is not None and score > aesthetics_cap:
        sources.append("academic_aesthetics")
    if legibility_cap is not None and score > legibility_cap:
        sources.append("poster_scale_legibility")
    return sources


def _finding_counts_dict(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"P0": 0, "P1": 0, "P2": 0, "total": len(findings)}
    for finding in findings:
        severity = str(finding.get("severity") or "P2").upper()
        if severity not in {"P0", "P1", "P2"}:
            severity = "P2"
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def _finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _presentation_viability_policy(scores: dict[str, Any]) -> dict[str, Any] | None:
    dimensions = [
        RubricDimensionScore(
            id=dimension.id,
            weight=dimension.weight,
            owner=dimension.owner,
            score_0_10=_finite_float(scores.get(dimension.id)),
        )
        for dimension in DIMENSIONS
    ]
    return presentation_viability_cap_policy(dimensions)


def _presentation_viability_record_fields(scores: dict[str, Any]) -> dict[str, Any]:
    viability_inputs = {
        dim_id: _finite_float(scores.get(dim_id))
        for dim_id in (
            "information_density_and_synthesis",
            "layout_readability",
            "professional_aesthetics",
        )
    }
    if any(score is None for score in viability_inputs.values()):
        viability = None
    else:
        viability = round(
            0.50 * float(viability_inputs["information_density_and_synthesis"])
            + 0.25 * float(viability_inputs["layout_readability"])
            + 0.25 * float(viability_inputs["professional_aesthetics"]),
            4,
        )
    policy = _presentation_viability_policy(scores)
    return {
        "presentation_viability": viability,
        "presentation_viability_triggered": policy is not None,
        "presentation_viability_ceiling": (
            policy.get("score_ceiling") if policy is not None else None
        ),
        "presentation_viability_weak_dimensions": sorted(
            dim_id
            for dim_id, score in viability_inputs.items()
            if score is not None and score < 6.0
        ),
    }


def _benchmark_verdict(overall: float | None, gate_triggered: bool) -> str:
    if overall is None:
        return "incomplete"
    if overall >= PASS_THRESHOLD and not gate_triggered:
        return "pass"
    if overall >= REVISE_THRESHOLD:
        return "revise"
    return "fail"


def _preserve_stricter_verdict(existing: Any, recomputed: str) -> str:
    rank = {"fail": 0, "revise": 1, "pass": 2}
    current = str(existing or "").lower()
    if current not in rank or recomputed not in rank:
        return recomputed
    return current if rank[current] < rank[recomputed] else recomputed


def _record_from_final(job: CandidateJob, final: dict[str, Any], status: str) -> dict[str, Any]:
    dims = {}
    dim_status = {}
    dim_source = {}
    dim_metrics: dict[str, dict[str, Any]] = {}
    for dim in final.get("dimensions", []) or []:
        if not isinstance(dim, dict):
            continue
        dims[str(dim.get("id"))] = dim.get("score_0_10")
        dim_status[str(dim.get("id"))] = dim.get("status")
        dim_source[str(dim.get("id"))] = dim.get("source")
        dim_metrics[str(dim.get("id"))] = (
            dict(dim.get("metrics") or {})
            if isinstance(dim.get("metrics"), dict)
            else {}
        )
    missing = [d for d in ALL_DIMS if dims.get(d) is None]
    visual_metrics = dim_metrics.get("visual_evidence_use", {})
    readability_metrics = dim_metrics.get("layout_readability", {})
    legibility_cap = readability_metrics.get("poster_scale_legibility_cap") or {}
    trusted_layout = _trusted_layout_p1_summary(final.get("findings", []) or [])
    deterministic_report_path = str(final.get("deterministic_report_path") or "")
    candidate_dir = (
        Path(deterministic_report_path).parent.parent
        if deterministic_report_path
        else None
    )
    judge_input = candidate_dir / "judge_input.png" if candidate_dir else None
    presentation_viability = _presentation_viability_record_fields(dims)
    record_status = "incomplete" if missing else status
    officially_eligible = (
        not missing
        and record_status in {"scored", "cached", "reaggregated"}
        and final.get("reaggregation_status") != "degraded"
        and final.get("eval_protocol") == EVAL_PROTOCOL
        and final.get("evaluator_fingerprint") == BENCHMARK_EVALUATOR_FINGERPRINT
        and _compatible_final_vlm_prompt(final.get("vlm_prompt_fingerprint"))
        and (
            final.get("source_evaluator_fingerprint") is not None
            or final.get("legacy_source_rubric_version") is not None
        )
    )
    diagnostic_overall = final.get("overall_score_0_100")
    return {
        "system": job.system,
        "system_label": SYSTEM_LABELS.get(job.system, job.system),
        "discipline": job.discipline,
        "discipline_label": DISCIPLINE_LABELS.get(job.discipline, job.discipline),
        "case": job.case,
        "artifact": str(job.artifact) if job.artifact else "",
        "paper": str(job.paper) if job.paper else "",
        "artifact_sha256": final.get("artifact_sha256"),
        "paper_sha256": final.get("paper_sha256"),
        "candidate_name": final.get("candidate_name") or f"{job.system}::{job.discipline}/{job.case}",
        "status": record_status,
        "note": f"missing dimensions: {', '.join(missing)}" if missing else job.note,
        "overall": diagnostic_overall if officially_eligible else None,
        "diagnostic_overall": diagnostic_overall,
        "officially_eligible": officially_eligible,
        "verdict": final.get("verdict"),
        "gate_triggered": final.get("gate_triggered"),
        "dimensions": dims,
        "dimension_status": dim_status,
        "dimension_source": dim_source,
        "raw_professional_aesthetics": dims.get("professional_aesthetics"),
        "adjusted_professional_aesthetics": dims.get("professional_aesthetics"),
        "evidence_group_count": visual_metrics.get("evidence_group_count"),
        "evidence_area_ratio": visual_metrics.get("evidence_group_area_ratio"),
        "legibility_cap": legibility_cap.get("score_ceiling") if isinstance(legibility_cap, dict) else None,
        "trusted_layout_p1_source": trusted_layout.get("source"),
        "trusted_layout_p1_confidence": trusted_layout.get("confidence"),
        **presentation_viability,
        "batch_style_artifact": str(judge_input) if judge_input and judge_input.exists() else str(job.artifact or ""),
        "report_path": str(candidate_dir / "poster_quality_report.json") if candidate_dir else "",
        "eval_protocol": final.get("eval_protocol"),
        "evaluator_fingerprint": final.get("evaluator_fingerprint"),
        "source_evaluator_fingerprint": final.get("source_evaluator_fingerprint"),
        "legacy_source_rubric_version": final.get("legacy_source_rubric_version"),
        "vlm_prompt_fingerprint": final.get("vlm_prompt_fingerprint"),
        "judge_model": final.get("judge_model"),
        "reaggregation_status": final.get("reaggregation_status"),
    }


def _trusted_layout_p1_summary(findings: list[Any]) -> dict[str, Any]:
    for finding in findings:
        if not isinstance(finding, dict) or str(finding.get("severity") or "").upper() != "P1":
            continue
        evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
        if evidence.get("trusted_p1") is not True:
            continue
        return {
            "source": evidence.get("boundary_source") or evidence.get("source") or finding.get("id"),
            "confidence": evidence.get("confidence") or "high",
        }
    return {}


def _apply_batch_style_result(
    record: dict[str, Any],
    batch_result: dict[str, Any],
) -> dict[str, Any]:
    """Return a benchmark-only record with the batch aesthetics adjustment."""
    adjusted = dict(record)
    status = str(batch_result.get("status") or "degraded")
    adjusted.update({
        "batch_style_status": status,
        "batch_style_fingerprint": batch_result.get("batch_style_fingerprint"),
        "batch_style_judge_model": batch_result.get("judge_model"),
        "batch_style_cache_status": batch_result.get("cache_status"),
        "batch_style_source": batch_result.get("source"),
        "batch_style_explanation": str(batch_result.get("explanation") or ""),
    })
    if not _is_official_record(record):
        return adjusted
    if status not in {"ok", "not_applicable"}:
        adjusted["diagnostic_overall"] = (
            record.get("diagnostic_overall")
            if record.get("diagnostic_overall") is not None
            else record.get("overall")
        )
        adjusted["overall"] = None
        adjusted["officially_eligible"] = False
        adjusted["homogeneity_adjustment"] = 0.0
        adjusted["status"] = "batch_style_degraded"
        explanation = str(batch_result.get("explanation") or "").strip()
        adjusted["note"] = "; ".join(
            value
            for value in (str(record.get("note") or "").strip(), explanation)
            if value
        )
        return adjusted
    if status == "not_applicable":
        adjusted["homogeneity_adjustment"] = 0.0
        return adjusted
    dimensions = dict(record.get("dimensions") or {})
    raw_aesthetics = _finite_float(dimensions.get("professional_aesthetics"))
    adjustment = (
        _finite_float(batch_result.get("adjustment_points")) or 0.0
        if status == "ok"
        else 0.0
    )
    adjusted_aesthetics = raw_aesthetics
    if raw_aesthetics is not None:
        adjusted_aesthetics = round(max(0.0, min(10.0, raw_aesthetics + adjustment)), 2)
        dimensions["professional_aesthetics"] = adjusted_aesthetics

    weighted = 0.0
    scored_weight = 0.0
    for dim in DIMENSIONS:
        score = _finite_float(dimensions.get(dim.id))
        if score is None:
            continue
        weighted += dim.weight * score / 10.0
        scored_weight += dim.weight
    recomputed_overall = (
        round(100.0 * weighted / scored_weight, 2)
        if scored_weight > 0
        else None
    )
    prior_overall = _finite_float(record.get("overall"))
    if recomputed_overall is not None and prior_overall is not None:
        recomputed_overall = min(prior_overall, recomputed_overall)

    adjusted.update({
        "dimensions": dimensions,
        "overall": recomputed_overall,
        "verdict": _preserve_stricter_verdict(
            record.get("verdict"),
            _benchmark_verdict(
                recomputed_overall,
                bool(record.get("gate_triggered")),
            ),
        ),
        "raw_professional_aesthetics": raw_aesthetics,
        "adjusted_professional_aesthetics": adjusted_aesthetics,
        "style_adaptability": _finite_float(
            batch_result.get("style_adaptability_score_0_10")
        ),
        "homogeneity_adjustment": adjustment,
    })
    return adjusted


def _apply_batch_style_homogeneity(
    records: list[dict[str, Any]],
    *,
    out_dir: Path,
    judge_model: str,
    reaggregate_only: bool,
    force_judge: bool,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Evaluate and apply one anonymous batch adjustment per benchmark system."""
    by_system: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if not _is_official_record(record):
            continue
        by_system.setdefault(str(record.get("system") or "unknown"), []).append(record)

    backend: Any | None = None
    backend_error = ""
    if not reaggregate_only and any(
        len(items) >= BATCH_STYLE_MIN_BATCH_SIZE for items in by_system.values()
    ):
        try:
            from autodesign.config import load_settings
            from autodesign.llm_backend import make_backend

            backend = make_backend(load_settings(), judge_model, role="critic")
        except Exception as exc:  # noqa: BLE001
            backend_error = f"{type(exc).__name__}: {exc}"

    results: dict[str, dict[str, Any]] = {}
    for system, system_records in sorted(by_system.items()):
        system_out = out_dir / "batch_style" / _safe_name(system)
        if len(system_records) < BATCH_STYLE_MIN_BATCH_SIZE:
            results[system] = {
                "status": "not_applicable",
                "eval_protocol": EVAL_PROTOCOL,
                "batch_style_fingerprint": BATCH_STYLE_FINGERPRINT,
                "judge_model": judge_model,
                "artifact_count": len(system_records),
                "adjustment_points": 0.0,
                "explanation": (
                    f"Batch style requires at least {BATCH_STYLE_MIN_BATCH_SIZE} posters."
                ),
            }
            continue
        artifacts = [
            Path(str(record.get("batch_style_artifact") or record.get("artifact")))
            for record in system_records
            if record.get("batch_style_artifact") or record.get("artifact")
        ]
        missing_artifacts = [str(path) for path in artifacts if not path.exists()]
        if len(artifacts) != len(system_records) or missing_artifacts:
            results[system] = {
                "status": "degraded",
                "eval_protocol": EVAL_PROTOCOL,
                "batch_style_fingerprint": BATCH_STYLE_FINGERPRINT,
                "judge_model": judge_model,
                "artifact_count": len(artifacts) - len(missing_artifacts),
                "expected_artifact_count": len(system_records),
                "adjustment_points": 0.0,
                "explanation": "Batch style input is incomplete; the system is not publishable.",
                "missing_artifacts": missing_artifacts,
            }
            continue
        result = evaluate_batch_style_homogeneity(
            artifacts,
            out_dir=system_out,
            judge_model=judge_model,
            judge_backend=backend,
            cache_path=system_out / "batch_style_homogeneity.json",
            force_judge=force_judge and not reaggregate_only,
        )
        result = dict(result)
        if result.get("status") != "ok" and reaggregate_only:
            result["status"] = "degraded"
            result["explanation"] = (
                "Reaggregate-only mode had no valid batch-style cache; no adjustment applied. "
                + str(result.get("explanation") or "")
            ).strip()
        if result.get("status") != "ok" and backend_error:
            result["status"] = "degraded"
            result["backend_error"] = backend_error
        results[system] = result

    adjusted = [
        _apply_batch_style_result(record, results[str(record.get("system") or "unknown")])
        if (
            str(record.get("system") or "unknown") in results
            and _is_official_record(record)
        )
        else record
        for record in records
    ]
    return adjusted, results


def _mean(values: list[float]) -> float | None:
    clean = [value for raw in values if (value := _finite_float(raw)) is not None]
    return round(statistics.mean(clean), 2) if clean else None


def _aggregate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    systems = [s for s in SYSTEM_ORDER if any(r.get("system") == s for r in records)]
    systems.extend(
        sorted({str(r.get("system")) for r in records if r.get("system") and r.get("system") not in systems})
    )
    for system in systems:
        sys_records = [r for r in records if r.get("system") == system and _is_official_record(r)]
        rows.append(_aggregate_one(system, "all", sys_records))
        for discipline in DISCIPLINES:
            rows.append(_aggregate_one(system, discipline, [r for r in sys_records if r.get("discipline") == discipline]))
    return rows


def _aggregate_one(system: str, discipline: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    records = [record for record in records if _is_official_record(record)]
    trusted_p1_count = sum(
        1
        for record in records
        if str(record.get("trusted_layout_p1_confidence") or "").lower()
        in {"high", "trusted"}
    )
    viability_trigger_count = sum(
        1 for record in records if record.get("presentation_viability_triggered") is True
    )
    row = {
        "system": system,
        "system_label": SYSTEM_LABELS.get(system, system),
        "discipline": discipline,
        "discipline_label": DISCIPLINE_LABELS.get(discipline, discipline),
        "n": len(records),
        "overall": _mean([r.get("overall") for r in records]),
        "raw_professional_aesthetics": _mean([r.get("raw_professional_aesthetics") for r in records]),
        "adjusted_professional_aesthetics": _mean([r.get("adjusted_professional_aesthetics") for r in records]),
        "style_adaptability": _mean([r.get("style_adaptability") for r in records]),
        "homogeneity_adjustment": _mean([r.get("homogeneity_adjustment") for r in records]),
        "evidence_group_count": _mean([r.get("evidence_group_count") for r in records]),
        "evidence_area_ratio": _mean([r.get("evidence_area_ratio") for r in records]),
        "legibility_cap": _mean([r.get("legibility_cap") for r in records]),
        "trusted_layout_p1_source": ", ".join(sorted({
            str(r.get("trusted_layout_p1_source"))
            for r in records
            if r.get("trusted_layout_p1_source")
        })),
        "trusted_layout_p1_count": trusted_p1_count,
        "trusted_layout_p1_rate": round(
            trusted_p1_count / len(records),
            4,
        ) if records else 0.0,
        "presentation_viability_mean": _mean([
            r.get("presentation_viability") for r in records
        ]),
        "presentation_viability_trigger_count": viability_trigger_count,
        "presentation_viability_trigger_rate": round(
            viability_trigger_count / len(records),
            4,
        ) if records else 0.0,
        "presentation_viability_ceiling": _mean([
            r.get("presentation_viability_ceiling")
            for r in records
            if r.get("presentation_viability_triggered") is True
        ]),
        "presentation_viability_weak_dimensions": sorted({
            str(dim_id)
            for record in records
            for dim_id in record.get("presentation_viability_weak_dimensions", []) or []
        }),
    }
    for dim in ALL_DIMS:
        row[dim] = _mean([(r.get("dimensions") or {}).get(dim) for r in records])
    return row


def _is_official_record(record: dict[str, Any]) -> bool:
    if record.get("overall") is None:
        return False
    if "officially_eligible" in record:
        return record.get("officially_eligible") is True
    return record.get("status") in {None, "scored", "cached", "reaggregated"}


def _write_scores_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in sorted(records, key=lambda r: (r.get("system", ""), r.get("discipline", ""), r.get("case", ""))):
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_scores_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = [
        "system_label", "discipline_label", "case", "overall", "diagnostic_overall",
        "officially_eligible", "verdict", "status", "artifact", "paper",
        "artifact_sha256", "paper_sha256",
        "eval_protocol", "evaluator_fingerprint", "source_evaluator_fingerprint",
        "legacy_source_rubric_version", "vlm_prompt_fingerprint", "judge_model",
        "reaggregation_status", "batch_style_fingerprint", "batch_style_judge_model",
        "batch_style_cache_status", "batch_style_source",
        "raw_professional_aesthetics", "adjusted_professional_aesthetics", "style_adaptability",
        "homogeneity_adjustment", "batch_style_status", "evidence_group_count", "evidence_area_ratio",
        "legibility_cap", "trusted_layout_p1_source", "trusted_layout_p1_confidence",
        "presentation_viability", "presentation_viability_triggered",
        "presentation_viability_ceiling", "presentation_viability_weak_dimensions",
        *ALL_DIMS,
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for record in sorted(records, key=lambda r: (r.get("system", ""), r.get("discipline", ""), r.get("case", ""))):
            row = {k: record.get(k) for k in fields}
            for dim in ALL_DIMS:
                row[dim] = (record.get("dimensions") or {}).get(dim)
            writer.writerow(row)


def _write_mapping_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "system_label", "discipline_label", "case", "status", "source_name", "artifact",
        "paper", "mapping_method", "note",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return H.escape(str(value))


def _system_row_class(system: str | None) -> str:
    safe = "".join(ch if ch.isalnum() else "-" for ch in str(system or "").lower()).strip("-")
    return f"system-{safe}" if safe else "system-unknown"


def _sort_value(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return math.inf
    return out if math.isfinite(out) else math.inf


def _descending_sort_value(value: Any) -> float:
    out = _finite_float(value)
    return -out if out is not None else math.inf


def _discipline_sort_value(discipline: Any) -> int:
    if discipline == "all":
        return -1
    try:
        return DISCIPLINES.index(str(discipline))
    except ValueError:
        return len(DISCIPLINES)


def _sort_rows_by_overall(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    system_overall = {
        row.get("system"): row.get("overall")
        for row in rows
        if row.get("discipline") == "all"
    }
    return sorted(
        rows,
        key=lambda row: (
            _sort_value(system_overall.get(row.get("system"))),
            str(row.get("system_label") or row.get("system") or ""),
            _discipline_sort_value(row.get("discipline")),
        ),
    )


def _main_table(rows: list[dict[str, Any]]) -> str:
    headers = [
        "系统", "学科", "n", "Overall<br><span>0-100</span>",
        *[f"{H.escape(DIM_LABELS[d])}<br><span>0-10</span>" for d in ALL_DIMS],
    ]
    out = ["<table class='main-table'><thead><tr>"]
    out.extend(f"<th>{h}</th>" for h in headers)
    out.append("</tr></thead><tbody>")
    for row in _sort_rows_by_overall(rows):
        cls = f"{_system_row_class(row.get('system'))} {'overall-row' if row['discipline'] == 'all' else ''}".strip()
        out.append(f"<tr class='{cls}'>")
        out.append(f"<td><b>{H.escape(row['system_label'])}</b></td>")
        out.append(f"<td>{H.escape(row['discipline_label'])}</td>")
        out.append(f"<td class='num'>{row['n']}</td>")
        out.append(f"<td class='num'><b>{_fmt(row['overall'])}</b></td>")
        for dim in ALL_DIMS:
            out.append(f"<td class='num'>{_fmt(row.get(dim))}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _overall_only_table(rows: list[dict[str, Any]]) -> str:
    overall_rows = sorted(
        [row for row in rows if row.get("discipline") == "all"],
        key=lambda row: (
            _descending_sort_value(row.get("overall")),
            str(row.get("system_label") or row.get("system") or ""),
        ),
    )
    out = ["<table class='main-table overall-only-table'><thead><tr>"]
    headers = [
        "系统", "n", "Overall<br><span>0-100</span>",
        *[f"{H.escape(DIM_LABELS[dim])}<br><span>0-10</span>" for dim in ALL_DIMS],
    ]
    for header in headers:
        out.append(f"<th>{header}</th>")
    out.append("</tr></thead><tbody>")
    for row in overall_rows:
        cls = f"{_system_row_class(row.get('system'))} overall-row"
        out.append(f"<tr class='{cls}'>")
        out.append(f"<td><b>{H.escape(row['system_label'])}</b></td>")
        out.append(f"<td class='num'>{row['n']}</td>")
        out.append(f"<td class='num'><b>{_fmt(row['overall'])}</b></td>")
        for dim in ALL_DIMS:
            out.append(f"<td class='num'>{_fmt(row.get(dim))}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _delta_table(rows: list[dict[str, Any]]) -> str:
    by_key = {(r["system"], r["discipline"]): r for r in rows}
    competitors = [
        s for s in SYSTEM_ORDER
        if s != "designanything" and any(r.get("system") == s for r in rows)
    ]
    competitors.extend(
        sorted({str(r.get("system")) for r in rows if r.get("system") not in {"designanything", *competitors}})
    )
    out = ["<table><thead><tr><th>对比</th><th>学科</th><th>Overall Δ</th>"]
    out.extend(f"<th>{H.escape(DIM_LABELS[d])} Δ</th>" for d in ALL_DIMS)
    out.append("</tr></thead><tbody>")
    for competitor in competitors:
        for discipline in ["all", *DISCIPLINES]:
            da = by_key.get(("designanything", discipline), {})
            other = by_key.get((competitor, discipline), {})
            out.append("<tr>")
            out.append(
                f"<td>AutoDesign - {H.escape(SYSTEM_LABELS.get(competitor, competitor))}</td>"
            )
            out.append(f"<td>{H.escape(DISCIPLINE_LABELS.get(discipline, discipline))}</td>")
            out.append(f"<td class='num'><b>{_delta(da.get('overall'), other.get('overall'))}</b></td>")
            for dim in ALL_DIMS:
                out.append(f"<td class='num'>{_delta(da.get(dim), other.get(dim))}</td>")
            out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _delta(a: Any, b: Any) -> str:
    try:
        value = float(a) - float(b)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}"


def _case_rows(records: list[dict[str, Any]]) -> str:
    rows = []
    for record in sorted(records, key=lambda r: (r.get("discipline", ""), r.get("case", ""), r.get("system", ""))):
        dims = record.get("dimensions") or {}
        rows.append(f"<tr class='{_system_row_class(record.get('system'))}'>")
        rows.append(f"<td>{H.escape(record.get('discipline_label') or '')}</td>")
        rows.append(f"<td><code>{H.escape(record.get('case') or '')}</code></td>")
        rows.append(f"<td>{H.escape(record.get('system_label') or '')}</td>")
        rows.append(f"<td class='num'><b>{_fmt(record.get('overall'))}</b></td>")
        rows.append(f"<td>{H.escape(str(record.get('verdict') or ''))}</td>")
        rows.append(f"<td>{H.escape(str(record.get('status') or ''))}</td>")
        for dim in ALL_DIMS:
            rows.append(f"<td class='num'>{_fmt(dims.get(dim))}</td>")
        rows.append("</tr>")
    return "".join(rows)


def _write_html_report(
    path: Path,
    rows: list[dict[str, Any]],
    records: list[dict[str, Any]],
    mapping_rows: list[dict[str, Any]],
    wall_s: float,
    *,
    batch_style_results: dict[str, dict[str, Any]] | None = None,
    contact_sheets: dict[str, dict[str, Any]] | None = None,
    comparison_review: dict[str, Any] | None = None,
) -> None:
    scored = [r for r in records if _is_official_record(r)]
    missing = [r for r in records if not _is_official_record(r)]
    generated = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    delta_section = ""
    if any(row.get("system") == "designanything" for row in rows):
        delta_section = f"<h2>AutoDesign Delta</h2>\n{_delta_table(rows)}"
    missing_items = "".join(
        f"<li><b>{H.escape(str(r.get('system_label')))}</b> / {H.escape(str(r.get('discipline_label')))} / "
        f"<code>{H.escape(str(r.get('case')))}</code>: {H.escape(str(r.get('note') or r.get('status')))}</li>"
        for r in missing
    ) or "<li>无</li>"
    overall_rows = [row for row in rows if row.get("discipline") == "all"]
    explainability = render_system_explainability_fields(overall_rows)
    batch_style_results = batch_style_results or {}
    batch_notes = "".join(
        "<li>"
        f"<b>{H.escape(SYSTEM_LABELS.get(system, system))}</b>: "
        f"status={H.escape(str(result.get('status') or 'unknown'))}; "
        f"score={_fmt(result.get('style_adaptability_score_0_10'))}; "
        f"adjustment={_fmt(result.get('adjustment_points'))}; "
        f"{H.escape(str(result.get('explanation') or ''))}"
        "</li>"
        for system, result in sorted(batch_style_results.items())
    ) or "<li>No batch style result was available.</li>"
    contact_cards: list[str] = []
    for system, result in sorted((contact_sheets or {}).items()):
        image_path = Path(str(result.get("image_path") or ""))
        try:
            image_src = image_path.relative_to(path.parent).as_posix()
        except (ValueError, OSError):
            image_src = image_path.as_posix()
        contact_cards.append(
            "<figure class='contact-sheet'>"
            f"<figcaption>{H.escape(SYSTEM_LABELS.get(system, system))} "
            f"({int(result.get('items_rendered') or 0)} posters)</figcaption>"
            f"<a href='{H.escape(image_src)}'><img src='{H.escape(image_src)}' "
            f"alt='{H.escape(SYSTEM_LABELS.get(system, system))} anonymous contact sheet'></a>"
            "</figure>"
        )
    comparison_section = str((comparison_review or {}).get("html_section") or "")
    doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Poster Benchmark Main Table</title>
<style>
body{{font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;color:#1f2937;margin:0;background:#f7f8fb}}
main{{max-width:1480px;margin:0 auto;padding:28px 24px 64px}}
h1{{font-size:26px;margin:0 0 6px}}
h2{{font-size:19px;margin:28px 0 10px}}
p{{margin:6px 0}}
.sub{{color:#667085}}
.note{{background:#fff7e6;border-left:4px solid #d78b00;border-radius:0 8px 8px 0;padding:11px 14px;margin:14px 0}}
.kpis{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}
.kpi{{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:10px 14px;min-width:130px}}
.kpi .v{{font-weight:700;font-size:21px}}
.kpi .l{{font-size:12px;color:#667085}}
table{{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e5e7eb;margin:8px 0 18px;font-size:12px}}
th,td{{border:1px solid #e5e7eb;padding:6px 7px;vertical-align:middle}}
th{{background:#f0f3f7;text-align:left;position:sticky;top:0;z-index:1}}
th span{{font-weight:400;color:#667085}}
.main-table th,.main-table td{{white-space:nowrap}}
.overall-only-table{{max-width:none}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.system-designanything td{{background:#edf7f2}}
.system-opendesign-posterbench td{{background:#eef7ec}}
.system-autodesign td{{background:#f3f8e7}}
.system-claude-design td{{background:#fff4e8}}
.system-codex-native td{{background:#eef4ff}}
.system-codex-posterly td{{background:#f5efff}}
.system-codex-posterskill td{{background:#fff0f5}}
.system-codex-pptxposterskill td{{background:#eefaf8}}
.overall-row.system-designanything td{{background:#dff0e7}}
.overall-row.system-opendesign-posterbench td{{background:#dcefd8}}
.overall-row.system-autodesign td{{background:#e4f0c8}}
.overall-row.system-claude-design td{{background:#ffe9ce}}
.overall-row.system-codex-native td{{background:#dfeaff}}
.overall-row.system-codex-posterly td{{background:#ebe0ff}}
.overall-row.system-codex-posterskill td{{background:#ffe0ec}}
.overall-row.system-codex-pptxposterskill td{{background:#dcf2ee}}
tbody tr:hover td{{background:#fffbe8}}
code{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px}}
.small{{font-size:12px;color:#667085}}
.scroll{{overflow:auto;border:1px solid #e5e7eb;background:#fff}}
.scroll table{{border:0;margin:0}}
.contact-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}}
.contact-sheet{{margin:0;background:#fff;border:1px solid #e5e7eb;padding:10px}}
.contact-sheet figcaption{{font-weight:700;margin-bottom:8px}}
.contact-sheet img,.same-paper-comparison img{{display:block;width:100%;height:auto}}
ul{{margin-top:8px}}
</style>
</head>
<body><main>
<h1>Poster Benchmark Main Table</h1>
<p class="sub">5 个学科 poster benchmark；Overall 为 0-100，7 个 metric 为 0-10。生成时间：{H.escape(generated)}。</p>
<div class="note">
评分口径：deterministic 维度走当前 Python 规则；source/layout/visual evidence 与 paper coverage/professional aesthetics 走当前单维 VLM judge；
最终分数由 <code>aggregate_final</code> 按现有权重计算。各系统按实际可评分 poster 计入 n。
</div>
<div class="kpis">
  <div class="kpi"><div class="v">{len(scored)}</div><div class="l">已评分 poster</div></div>
  <div class="kpi"><div class="v">{len(records)}</div><div class="l">发现候选记录</div></div>
  <div class="kpi"><div class="v">{len(missing)}</div><div class="l">missing / incomplete</div></div>
  <div class="kpi"><div class="v">{round(wall_s / 60, 1)}m</div><div class="l">本次 wall time</div></div>
</div>
<h2>Overall Main Table</h2>
{_overall_only_table(rows)}
<h2>Detailed Main Table</h2>
{_main_table(rows)}
{delta_section}
<h2>Batch Style Adaptability</h2>
<p class="small">This benchmark-only modifier evaluates anonymous cross-paper layout adaptability. It does not change standalone poster reports.</p>
{explainability['html']}
<ul>{batch_notes}</ul>
<h2>Anonymous System Contact Sheets</h2>
<div class="contact-grid">{''.join(contact_cards) or '<p class="small">No contact sheets available.</p>'}</div>
{comparison_section}
<h2>Missing / Incomplete</h2>
<ul>{missing_items}</ul>
<h2>Per-case Scores</h2>
<div class="scroll"><table>
<thead><tr><th>学科</th><th>case</th><th>系统</th><th>Overall</th><th>verdict</th><th>status</th>"""
    doc += "".join(f"<th>{H.escape(DIM_LABELS[d])}</th>" for d in ALL_DIMS)
    doc += f"""</tr></thead><tbody>{_case_rows(records)}</tbody></table></div>
<p class="small">Files: <code>scores.jsonl</code>, <code>scores.csv</code>, <code>case_mapping.csv</code>, <code>benchmark_summary.json</code>.</p>
</main></body></html>"""
    path.write_text(doc, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-root", type=Path, default=_REPO / "eval/EvaData")
    parser.add_argument("--design-root", type=Path, default=_REPO / "DesignAnything_Poster")
    parser.add_argument("--opendesign-posterbench-root", type=Path, default=_REPO / "eval/EvaData/OpenDesign_PosterBench")
    parser.add_argument("--autodesign-links-root", type=Path, default=_REPO / "eval/EvaData/AutoDesign_PosterBench/links")
    parser.add_argument("--claude-root", type=Path, default=_REPO / "eval/EvaData/Claude_Design_Poster")
    parser.add_argument("--codex-native-root", type=Path, default=_REPO / "eval/EvaData/Codex_native_Poster")
    parser.add_argument("--codex-posterly-root", type=Path, default=_REPO / "eval/EvaData/Codex_posterly_Poster")
    parser.add_argument("--codex-posterskill-root", type=Path, default=_REPO / "eval/EvaData/Codex_posterskill_Posters")
    parser.add_argument("--codex-pptxposterskill-root", type=Path, default=_REPO / "eval/EvaData/Codex_pptxposterskill_Poster")
    parser.add_argument("--out-dir", type=Path, default=_REPO / "out/eval/report/poster_benchmark_main_table")
    parser.add_argument(
        "--systems",
        default="designanything,claude_design,codex_native,codex_posterly,codex_posterskill,codex_pptxposterskill",
        help="Comma-separated systems to run.",
    )
    parser.add_argument(
        "--system-label",
        action="append",
        default=[],
        metavar="KEY=LABEL",
        help="Override a system display label; repeatable. Useful when reusing an input slot for another system.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Limit ready jobs for smoke tests.")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--model", default=None)
    parser.add_argument("--force", action="store_true", help="Recompute deterministic and final reports.")
    parser.add_argument("--force-vlm", action="store_true", help="Re-run VLM judge calls even when cached.")
    parser.add_argument(
        "--force-vlm-dims",
        type=_parse_force_vlm_dims,
        default=set(),
        metavar="DIM[,DIM...]",
        help=(
            "Re-run only selected recalibrated VLM dimensions: "
            "visual_evidence_use, layout_readability, professional_aesthetics."
        ),
    )
    parser.add_argument(
        "--reaggregate-only",
        action="store_true",
        help="Reuse existing per-case reports and only recompute overall scores/tables with current rubric weights.",
    )
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument(
        "--vlm-call-delay",
        type=float,
        default=0.0,
        help="Sleep this many seconds between benchmark VLM judge dimension calls.",
    )
    parser.add_argument("--dry-map", action="store_true", help="Only discover mappings and write case_mapping.csv.")
    parser.add_argument(
        "--allow-degraded-detectors",
        action="store_true",
        help="Allow benchmark scoring when optional OCR/CV detector dependencies are unavailable. Not for official benchmark runs.",
    )
    args = parser.parse_args()
    for raw in args.system_label:
        key, separator, label = raw.partition("=")
        key = key.strip()
        label = label.strip()
        if not separator or not key or not label:
            parser.error(f"invalid --system-label {raw!r}; expected KEY=LABEL")
        SYSTEM_LABELS[key] = label
    return args


def _force_batch_style_judge(args: argparse.Namespace) -> bool:
    """Only the all-VLM force flag invalidates a valid batch-style cache."""
    return bool(args.force_vlm)


def main() -> int:
    args = parse_args()
    systems = {s.strip() for s in args.systems.split(",") if s.strip()}
    args.out_dir.mkdir(parents=True, exist_ok=True)
    jobs, mapping_rows = _discover_jobs(
        paper_root=args.paper_root,
        design_root=args.design_root,
        opendesign_posterbench_root=args.opendesign_posterbench_root,
        autodesign_links_root=args.autodesign_links_root,
        claude_root=args.claude_root,
        codex_native_root=args.codex_native_root,
        codex_posterly_root=args.codex_posterly_root,
        codex_posterskill_root=args.codex_posterskill_root,
        codex_pptxposterskill_root=args.codex_pptxposterskill_root,
        systems=systems,
    )
    _write_mapping_csv(args.out_dir / "case_mapping.csv", mapping_rows)
    ready = [job for job in jobs if job.status == "ready"]
    if args.limit:
        ready = ready[: args.limit]
    print(f"discovered {len(jobs)} records; ready to score {len(ready)}; out={args.out_dir}", flush=True)
    if args.dry_map:
        print(f"wrote mapping: {args.out_dir / 'case_mapping.csv'}", flush=True)
        return 0
    preflight = _detector_preflight()
    atomic_write_json(args.out_dir / "detector_preflight.json", preflight)
    if preflight.get("status") != "ok" and not args.allow_degraded_detectors and not args.reaggregate_only:
        print("detector preflight failed for official benchmark scoring", file=sys.stderr)
        print(json.dumps(preflight, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    t0 = time.monotonic()
    vlm_call_limiter = _VLMCallLimiter(args.vlm_call_delay)
    records: list[dict[str, Any]] = []
    skipped = [job for job in jobs if job.status != "ready"]
    for job in skipped:
        if args.reaggregate_only:
            records.append(_reaggregate_job(job, out_dir=args.out_dir))
        else:
            records.append(_score_job(
                job,
                out_dir=args.out_dir,
                model=args.model,
                force=False,
                force_vlm=False,
                force_vlm_dims=set(),
                retries=args.retries,
                vlm_call_limiter=vlm_call_limiter,
            ))

    def run(job: CandidateJob) -> dict[str, Any]:
        if args.reaggregate_only:
            return _reaggregate_job(job, out_dir=args.out_dir)
        return _score_job(
            job,
            out_dir=args.out_dir,
            model=args.model,
            force=args.force,
            force_vlm=args.force_vlm,
            force_vlm_dims=args.force_vlm_dims,
            retries=args.retries,
            vlm_call_limiter=vlm_call_limiter,
        )

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = [ex.submit(run, job) for job in ready]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                record = fut.result()
            except Exception as exc:  # noqa: BLE001
                record = {
                    "system": "unknown",
                    "system_label": "unknown",
                    "discipline": "",
                    "discipline_label": "",
                    "case": "",
                    "status": "error",
                    "note": f"{type(exc).__name__}: {exc}",
                    "overall": None,
                    "dimensions": {},
                }
            records.append(record)
            if i % 5 == 0 or i == len(ready):
                print(
                    f"[{i}/{len(ready)}] {record.get('system_label')} "
                    f"{record.get('discipline')}/{record.get('case')} overall={record.get('overall')} "
                    f"status={record.get('status')}",
                    flush=True,
                )

    records, batch_style_results = _apply_batch_style_homogeneity(
        records,
        out_dir=args.out_dir,
        judge_model=args.model or DEFAULT_BENCHMARK_JUDGE_MODEL,
        reaggregate_only=args.reaggregate_only,
        force_judge=_force_batch_style_judge(args),
    )
    rows = _aggregate(records)
    contact_sheets: dict[str, dict[str, Any]] = {}
    for system in sorted({str(record.get("system")) for record in records if record.get("system")}):
        system_records = [
            {
                **record,
                "artifact": record.get("batch_style_artifact") or record.get("artifact"),
            }
            for record in records
            if record.get("system") == system and _is_official_record(record)
        ]
        if not system_records:
            continue
        contact_sheets[system] = build_anonymous_system_contact_sheet(
            system_records,
            args.out_dir / "contact_sheets" / f"{_safe_name(system)}_100.png",
        )
    comparison_review = build_same_paper_comparison_section(
        [
            {
                **record,
                "artifact": record.get("batch_style_artifact") or record.get("artifact"),
            }
            for record in records
            if _is_official_record(record)
        ],
        args.out_dir,
        max_papers=30,
    )
    wall = time.monotonic() - t0
    _write_scores_jsonl(args.out_dir / "scores.jsonl", records)
    _write_scores_csv(args.out_dir / "scores.csv", records)
    vlm_prompt_fingerprints = sorted({
        str(record.get("vlm_prompt_fingerprint"))
        for record in records
        if record.get("vlm_prompt_fingerprint") and _is_official_record(record)
    })
    summary = {
        "eval_protocol": EVAL_PROTOCOL,
        "evaluator_fingerprint": BENCHMARK_EVALUATOR_FINGERPRINT,
        "vlm_prompt_fingerprint": (
            vlm_prompt_fingerprints[0] if len(vlm_prompt_fingerprints) == 1 else None
        ),
        "vlm_prompt_fingerprints": vlm_prompt_fingerprints,
        "batch_style_fingerprint": BATCH_STYLE_FINGERPRINT,
        "judge_models": sorted({
            str(record.get("judge_model"))
            for record in records
            if record.get("judge_model") and _is_official_record(record)
        }),
        "source_evaluator_fingerprints": sorted({
            str(record.get("source_evaluator_fingerprint"))
            for record in records
            if record.get("source_evaluator_fingerprint") and _is_official_record(record)
        }),
        "rows": rows,
        "records": records,
        "mapping": mapping_rows,
        "detector_preflight": preflight,
        "batch_style_homogeneity": batch_style_results,
        "contact_sheets": contact_sheets,
        "same_paper_comparison": comparison_review,
        "wall_seconds": round(wall, 2),
        "ready_scored": len([r for r in records if _is_official_record(r)]),
        "record_count": len(records),
    }
    atomic_write_json(args.out_dir / "benchmark_summary.json", summary)
    _write_html_report(
        args.out_dir / "benchmark_main_table_zh.html",
        rows,
        records,
        mapping_rows,
        wall,
        batch_style_results=batch_style_results,
        contact_sheets=contact_sheets,
        comparison_review=comparison_review,
    )
    print(f"wrote {args.out_dir / 'benchmark_main_table_zh.html'}", flush=True)
    print(f"scored {summary['ready_scored']}/{len(records)} records in {round(wall)}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
