#!/usr/bin/env python3
"""Standalone evidence, authoring, QA, review, and delivery harness for decks."""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_SLIDE_COUNT = 18
MAX_REPAIR_ATTEMPTS = 3
RELEASE_VERSION = "0.1.0"
SLIDE_WIDTH = 1920
SLIDE_HEIGHT = 1080
_PLAN_SHA_FIELD = "ppt_plan_sha256"


class PptHarnessError(RuntimeError):
    """The standalone deck workflow cannot safely continue."""


def _external_output(path: Path | str) -> Path:
    target = Path(path).expanduser().resolve(strict=False)
    package = Path(__file__).resolve().parent.parent
    try:
        target.relative_to(package)
    except ValueError:
        return target
    raise PptHarnessError("generated output must stay outside the installed Skill")


def _load_sibling(name: str, filename: str):
    path = Path(__file__).resolve().with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PptHarnessError(f"could not load bundled runtime module: {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


portable = _load_sibling("autodesign_ppt_portable", "_portable.py")
browser_setup = _load_sibling("autodesign_ppt_browser_setup", "setup_browser.py")
ppt_setup = _load_sibling("autodesign_ppt_runtime_setup", "setup_ppt.py")
exporter = _load_sibling("autodesign_ppt_exporter", "export_pptx.py")


_COUNT_TOKEN = r"(?:[1-5]?\d|60|[零〇一二两三四五六七八九十]{1,3})"
_COUNT_LEFT_BOUNDARY = r"(?<![0-9零〇一二两三四五六七八九十百])"
_EXPLICIT_COUNT_PATTERNS = (
    re.compile(
        rf"{_COUNT_LEFT_BOUNDARY}(?P<count>{_COUNT_TOKEN})\s*(?:张\s*)?(?:幻灯片|ppt)(?![A-Za-z0-9_])",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_COUNT_LEFT_BOUNDARY}(?P<count>{_COUNT_TOKEN})\s*页\s*(?:的\s*)?(?:幻灯片|ppt|演示文稿|deck|presentation)(?![A-Za-z0-9_])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\d)(?P<count>[1-5]?\d|60)\s*-?\s*slides?(?![A-Za-z0-9_])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\d)(?P<count>[1-5]?\d|60)\s*-?\s*pages?\s+(?:conference\s+)?(?:deck|slides?|presentation|ppt)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:make|create|generate|prepare)\s+(?:it\s+)?"
        r"(?P<count>[1-5]?\d|60)\s*-?\s*pages?"
        r"(?=\s*(?:[.!?,;:]|$))",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:请\s*)?(?:生成|做成|制作|需要|想要|要|做|共)\s*"
        rf"(?P<count>{_COUNT_TOKEN})\s*页"
        rf"(?=\s*(?:[。！!，,.？?；;：:]|$))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:slides?\s*(?:count|total)?|(?:deck|presentation|ppt)\s*"
        r"(?:pages?|slides?)?\s*(?:count|total)?)\s*[:=]?\s*"
        r"(?P<count>[1-5]?\d|60)(?!\d)",
        re.IGNORECASE,
    ),
)

_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_ACADEMIC_ARC = (
    ("cover", "Opening", "State the paper identity and central thesis"),
    ("outline", "Roadmap", "Orient the audience to the research argument"),
    ("problem", "Problem", "Define the research problem and stakes"),
    ("motivation", "Motivation", "Show why the problem matters now"),
    ("prior-gap", "Prior work gap", "Identify the unresolved limitation"),
    ("contributions", "Contributions", "Make the paper's contributions explicit"),
    ("method-overview", "Method overview", "Explain the full method at a glance"),
    ("mechanism", "Core mechanism", "Explain the central design mechanism"),
    ("objective", "Technical formulation", "Ground the algorithm, objective, or architecture"),
    ("setup", "Experimental setup", "Establish data, baselines, and evaluation protocol"),
    ("primary-results", "Primary results", "Present the main quantitative finding"),
    ("robustness", "Robustness", "Test whether the finding holds across conditions"),
    ("ablation", "Ablation", "Isolate which components create the gain"),
    ("qualitative", "Qualitative evidence", "Show representative source-backed examples"),
    ("limitations", "Limitations", "State scope, failure modes, and uncertainty"),
    ("implications", "Implications", "Connect results to research practice"),
    ("takeaways", "Takeaways", "Compress the argument into memorable conclusions"),
    ("closing", "Closing", "End with the thesis and discussion prompt"),
)

_SUBSTITUTE_ROLE_DEFINITIONS = {
    "method-detail": (
        "method-detail",
        "Method detail",
        "Resolve one additional source-backed method detail",
    ),
    "architecture-detail": (
        "architecture-detail",
        "Architecture detail",
        "Explain one additional source-backed architecture decision",
    ),
    "implementation-detail": (
        "implementation-detail",
        "Implementation detail",
        "Explain source-backed implementation or training details",
    ),
    "results-deep-dive": (
        "results-deep-dive",
        "Results deep dive",
        "Interpret one additional source-backed result breakdown",
    ),
    "evidence-analysis": (
        "evidence-analysis",
        "Evidence analysis",
        "Explain one source-backed analysis without inventing an ablation",
    ),
    "case-analysis": (
        "case-analysis",
        "Case analysis",
        "Analyze one source-backed case without claiming absent qualitative evidence",
    ),
    "scope-and-boundaries": (
        "scope-and-boundaries",
        "Scope and boundaries",
        "State supported scope or assumptions without inventing limitations",
    ),
    "implications-detail": (
        "implications-detail",
        "Implications detail",
        "Develop one additional source-backed implication",
    ),
}

_CONDITIONAL_ROLE_SUBSTITUTIONS = {
    "objective": ("method-detail", "architecture-detail"),
    "setup": ("implementation-detail", "method-detail"),
    "robustness": ("results-deep-dive", "evidence-analysis"),
    "ablation": ("evidence-analysis", "results-deep-dive"),
    "qualitative": ("case-analysis", "evidence-analysis"),
    "limitations": ("scope-and-boundaries", "implications-detail"),
}

_NARRATIVE_BACKBONE = (
    "cover",
    "outline",
    "problem",
    "contributions",
    "method-overview",
    "primary-results",
    "takeaways",
    "closing",
)


_ROLE_DISTINCTIVE_CONCEPTS = {
    "cover": (
        ("title", "标题"),
        ("author", "作者"),
        ("affiliation", "机构"),
        ("thesis", "主旨"),
    ),
    "outline": (
        ("roadmap", "路线图"),
        ("outline", "大纲"),
        ("overview", "概览"),
    ),
    "problem": (
        ("problem", "问题"),
        ("challenge", "挑战"),
        ("unresolved", "未解决"),
        ("fail", "失败"),
    ),
    "motivation": (
        ("motivation", "动机"),
        ("significance", "意义"),
        ("important", "重要"),
        ("impact", "影响"),
    ),
    "prior-gap": (
        ("related work", "相关工作"),
        ("prior work", "先前工作"),
        ("gap", "差距"),
        ("shortcoming", "不足"),
    ),
    "contributions": (
        ("contribut", "贡献"),
        ("we propose", "我们提出"),
        ("we introduce", "我们引入"),
    ),
    "method-overview": (
        ("framework", "框架"),
        ("architecture", "架构"),
        ("pipeline", "流程"),
        ("approach", "方法"),
    ),
    "mechanism": (
        ("mechanism", "机制"),
        ("module", "模块"),
        ("algorithm", "算法"),
        ("procedure", "过程"),
    ),
    "objective": (
        ("objective", "目标函数"),
        ("loss", "损失"),
        ("equation", "公式"),
        ("theorem", "定理"),
    ),
    "setup": (
        ("experiment", "实验"),
        ("dataset", "数据集"),
        ("benchmark", "基准"),
        ("metric", "指标"),
    ),
    "primary-results": (
        ("performance", "性能"),
        ("accuracy", "准确率"),
        ("improvement", "提升"),
        ("score", "分数"),
    ),
    "robustness": (
        ("robust", "鲁棒"),
        ("generaliz", "泛化"),
        ("sensitivity", "敏感性"),
        ("variance", "方差"),
    ),
    "ablation": (
        ("ablation", "消融"),
        ("without", "去除"),
        ("remov", "移除"),
        ("variant", "变体"),
    ),
    "qualitative": (
        ("qualitative", "定性"),
        ("case study", "案例"),
        ("visualization", "可视化"),
        ("example", "示例"),
    ),
    "limitations": (
        ("limitation", "局限"),
        ("caveat", "限制条件"),
        ("uncertainty", "不确定"),
        ("failure mode", "失败模式"),
    ),
    "implications": (
        ("implication", "启示"),
        ("application", "应用"),
        ("practice", "实践"),
        ("deployment", "部署"),
    ),
    "takeaways": (
        ("takeaway", "要点"),
        ("summary", "总结"),
        ("key finding", "关键发现"),
    ),
    "closing": (
        ("discussion", "讨论"),
        ("future work", "未来工作"),
        ("conclusion", "结论"),
        ("closing", "结束"),
    ),
    "method-detail": (
        ("method detail", "方法细节"),
        ("component", "组件"),
        ("procedure", "步骤"),
        ("module", "模块"),
    ),
    "architecture-detail": (
        ("architecture detail", "架构细节"),
        ("layer", "层"),
        ("backbone", "骨干"),
        ("block", "模块块"),
    ),
    "implementation-detail": (
        ("implementation", "实现"),
        ("training detail", "训练细节"),
        ("hyperparameter", "超参数"),
        ("configuration", "配置"),
    ),
    "results-deep-dive": (
        ("secondary result", "补充结果"),
        ("breakdown", "分项"),
        ("per-category", "分类结果"),
        ("additional finding", "补充发现"),
    ),
    "evidence-analysis": (
        ("error analysis", "误差分析"),
        ("interpret", "解读"),
        ("trend", "趋势"),
        ("correlation", "相关性"),
    ),
    "case-analysis": (
        ("case analysis", "案例分析"),
        ("representative case", "代表案例"),
        ("failure example", "失败示例"),
    ),
    "scope-and-boundaries": (
        ("scope", "范围"),
        ("boundary", "边界"),
        ("assumption", "假设"),
        ("applicability", "适用性"),
    ),
    "implications-detail": (
        ("implication", "启示"),
        ("application", "应用"),
        ("deployment", "部署"),
        ("practice", "实践"),
    ),
    "evidence-deep-dive": (
        ("evidence", "证据"),
        ("finding", "发现"),
        ("figure", "图"),
        ("table", "表"),
    ),
}

_CONDITIONAL_ROLE_SIGNAL_RULES = {
    "ablation": (
        (("ablation", "消融"),),
        (
            (
                "with and without",
                "w/o",
                "component removal",
                "module removal",
                "remov",
                "去除",
                "移除",
            ),
            ("component", "module", "block", "组件", "模块"),
            (
                "compare",
                "comparison",
                "effect",
                "drop",
                "decreas",
                "increas",
                "reduc",
                "improv",
                "gain",
                "比较",
                "影响",
                "下降",
                "提升",
                "增益",
            ),
        ),
        (
            ("variant comparison", "compare variants", "变体比较", "变体对比"),
            (
                "effect",
                "drop",
                "decreas",
                "increas",
                "reduc",
                "improv",
                "gain",
                "影响",
                "下降",
                "提升",
                "增益",
            ),
        ),
    ),
    "robustness": (
        (("robustness", "robust", "鲁棒性", "鲁棒"),),
        (
            (
                "across dataset",
                "across condition",
                "distribution shift",
                "shifted dataset",
                "out-of-domain",
                "perturb",
                "noisy input",
                "sensitivity",
                "variance",
                "generalization",
                "跨数据集",
                "跨条件",
                "分布偏移",
                "域外",
                "扰动",
                "噪声",
                "敏感性",
                "方差",
                "泛化",
            ),
            (
                "stable",
                "consistent",
                "remain",
                "hold",
                "degrad",
                "drop",
                "decreas",
                "increas",
                "improv",
                "稳定",
                "一致",
                "保持",
                "下降",
                "提升",
            ),
        ),
    ),
    "qualitative": (
        (
            (
                "qualitative analysis",
                "qualitative evidence",
                "qualitative result",
                "qualitative evaluation",
                "qualitative example",
                "qualitative comparison",
                "定性分析",
                "定性证据",
                "定性结果",
                "定性评估",
                "定性示例",
                "定性对比",
            ),
        ),
        (
            (
                "case study",
                "case-study",
                "representative case",
                "failure example",
                "visual example",
                "visualization",
                "案例",
                "示例",
                "可视化",
            ),
            (
                "show",
                "illustrat",
                "demonstrat",
                "visualize",
                "behavior",
                "finding",
                "failure",
                "表明",
                "展示",
                "说明",
                "行为",
                "发现",
                "失败",
            ),
        ),
    ),
}
_SEMANTIC_MIN_CONCEPTS = 1
_SEMANTIC_MARGIN = 1

_CONDITIONAL_ROLE_ABSENCE_TERMS = {
    "ablation": (
        "ablation",
        "component removal",
        "module removal",
        "variant comparison",
        "remove a component",
        "remove the component",
        "remove a module",
        "remove the module",
        "消融",
        "组件移除",
        "模块移除",
        "变体比较",
    ),
    "robustness": (
        "robustness",
        "robust",
        "distribution shift",
        "shifted dataset",
        "sensitivity analysis",
        "鲁棒性",
        "鲁棒",
        "分布偏移",
        "敏感性分析",
    ),
    "qualitative": (
        "qualitative analysis",
        "qualitative evidence",
        "qualitative result",
        "qualitative evaluation",
        "qualitative example",
        "qualitative comparison",
        "case study",
        "representative case",
        "failure example",
        "visual example",
        "visualization",
        "定性分析",
        "定性证据",
        "定性结果",
        "定性评估",
        "定性示例",
        "定性对比",
        "案例分析",
        "代表案例",
        "失败示例",
        "可视化",
    ),
}

_NUMERIC_VALUE_PATTERN = r"(?<![\w.])\d+(?:\.\d+)?\s*%?"


def _parse_slide_count_token(token: str) -> int | None:
    if token.isascii() and token.isdigit():
        value = int(token)
        return value if 1 <= value <= 60 else None
    if "十" in token:
        if token.count("十") != 1:
            return None
        tens, ones = token.split("十", 1)
        if len(tens) > 1 or len(ones) > 1:
            return None
        tens_value = 1 if not tens else _CHINESE_DIGITS.get(tens)
        ones_value = 0 if not ones else _CHINESE_DIGITS.get(ones)
        if tens_value is None or ones_value is None or tens_value == 0:
            return None
        value = tens_value * 10 + ones_value
    elif len(token) == 1:
        value = _CHINESE_DIGITS.get(token, 0)
    else:
        return None
    return value if 1 <= value <= 60 else None


def _explicit_slide_count(brief: str) -> int | None:
    for pattern in _EXPLICIT_COUNT_PATTERNS:
        match = pattern.search(brief)
        if match:
            value = _parse_slide_count_token(match.group("count"))
            if value is not None:
                return value
    return None


def _arc_for_count(count: int) -> list[tuple[str, str, str]]:
    if count == len(_ACADEMIC_ARC):
        return list(_ACADEMIC_ARC)
    if count == 1:
        return [_ACADEMIC_ARC[0]]
    if count < len(_ACADEMIC_ARC):
        selected = {
            round(index * (len(_ACADEMIC_ARC) - 1) / (count - 1))
            for index in range(count)
        }
        while len(selected) < count:
            selected.add(next(index for index in range(len(_ACADEMIC_ARC)) if index not in selected))
        return [_ACADEMIC_ARC[index] for index in sorted(selected)]
    result = list(_ACADEMIC_ARC[:-2])
    for index in range(count - len(_ACADEMIC_ARC)):
        number = index + 1
        result.append(
            (
                f"evidence-deep-dive-{number}",
                f"Evidence deep dive {number}",
                "Explain one additional source-backed result or mechanism",
            )
        )
    result.extend(_ACADEMIC_ARC[-2:])
    return result


def _grapheme_clusters(text: str) -> list[str]:
    clusters: list[str] = []
    for character in text:
        is_regional_indicator = "\U0001f1e6" <= character <= "\U0001f1ff"
        previous_is_unpaired_regional_indicator = bool(
            clusters
            and is_regional_indicator
            and all("\U0001f1e6" <= item <= "\U0001f1ff" for item in clusters[-1])
            and len(clusters[-1]) % 2 == 1
        )
        is_modifier = unicodedata.category(character).startswith("M") or (
            "\ufe00" <= character <= "\ufe0f"
        ) or ("\U0001f3fb" <= character <= "\U0001f3ff")
        if clusters and (
            is_modifier
            or character == "\u200d"
            or clusters[-1].endswith("\u200d")
            or previous_is_unpaired_regional_indicator
        ):
            clusters[-1] += character
        else:
            clusters.append(character)
    return clusters


def _bounded_source_anchor(text: str) -> str:
    normalized = " ".join(
        unicodedata.normalize("NFKC", text).lstrip("# ").split()
    )
    if not normalized:
        return ""
    words = normalized.split()
    word_bounded = " ".join(words[:12])
    word_truncated = len(words) > 12
    clusters = _grapheme_clusters(word_bounded)
    limit = min(len(clusters), 72)
    sentence_end: int | None = None
    for index, cluster in enumerate(clusters[:limit]):
        final = cluster[-1]
        if final in "。！？；":
            sentence_end = index + 1
            break
        if final in ".!?;" and (
            index + 1 == len(clusters) or clusters[index + 1].isspace()
        ):
            sentence_end = index + 1
            break
    end = sentence_end if sentence_end is not None else limit
    anchor = "".join(clusters[:end]).strip()
    truncated = word_truncated or end < len(clusters)
    if truncated and sentence_end is None:
        anchor = anchor.rstrip(".,;:，；：") + "…"
    elif sentence_end is not None:
        anchor = anchor.rstrip("。！？；.!?;")
    return anchor


def _semantic_evidence_ref(
    role: str,
    _title: str,
    _communication_job: str,
    evidence: Sequence[Mapping[str, Any]],
) -> str:
    candidate = _semantic_evidence_candidate(role, evidence)
    if candidate is not None:
        return candidate
    raise PptHarnessError(
        f"could not semantically assign evidence for role {role}; provide --story-plan"
    )


def _semantic_role_key(role: str) -> str:
    if re.fullmatch(r"evidence-deep-dive-\d+", role):
        return "evidence-deep-dive"
    return role


def _conditional_role_declares_absence(role: str, normalized: str) -> bool:
    role_terms = _CONDITIONAL_ROLE_ABSENCE_TERMS.get(role, ())
    if not role_terms:
        return False
    role_pattern = "(?:" + "|".join(re.escape(term) for term in role_terms) + ")"
    english_patterns = (
        rf"\b(?:do|does|did|will|would|can|could|is|are|was|were|has|have|had)\s+"
        rf"(?:not|never)\b[^.!?]{{0,80}}{role_pattern}",
        rf"\bno\b[^.!?]{{0,50}}{role_pattern}",
        rf"{role_pattern}[^.!?]{{0,80}}\b(?:not|never|absent|missing|omitted|unavailable)\b",
        rf"{role_pattern}[^.!?]{{0,70}}\b"
        rf"(?:left|deferred|reserved|postponed|planned|considered)\b"
        rf"[^.!?]{{0,40}}\b(?:future work|future studies?|future evaluation)\b",
        rf"\b(?:defer(?:s|red)?|leave|leaves|left|reserv(?:e|es|ed)|"
        rf"postpon(?:e|es|ed)|plan(?:s|ned)?)\b"
        rf"[^.!?]{{0,50}}{role_pattern}[^.!?]{{0,50}}"
        rf"\b(?:future work|future studies?|future evaluation)\b",
        rf"\bwill\s+(?:evaluate|study|analy[sz]e|provide|conduct|perform)\b"
        rf"[^.!?]{{0,50}}{role_pattern}[^.!?]{{0,50}}\bfuture work\b",
    )
    if any(re.search(pattern, normalized) for pattern in english_patterns):
        return True
    chinese_patterns = (
        rf"(?:未|没有|不(?:进行|提供|评估|评价|报告|包含|涉及|开展|考虑))"
        rf"[^。！？]{{0,30}}{role_pattern}",
        rf"{role_pattern}[^。！？]{{0,40}}"
        rf"(?:未提供|未进行|没有|缺失|留待未来|未来工作)",
    )
    return any(re.search(pattern, normalized) for pattern in chinese_patterns)


def _has_numeric_observed_comparison(normalized: str) -> bool:
    if len(re.findall(_NUMERIC_VALUE_PATTERN, normalized)) < 2:
        return False
    return any(
        re.search(pattern, normalized)
        for pattern in (
            r"\bfrom\b.{0,80}\bto\b",
            r"\b(?:versus|vs\.?)\b",
            r"\bcompared\s+(?:with|to)\b",
            r"\bw/o\b.{0,100}\bfull\b",
            r"\bfull\b.{0,100}\bw/o\b",
            r"从[^。！？]{0,40}到",
            r"(?:相比|对比|比较)[^。！？]{0,80}",
        )
    )


def _conditional_role_signal_supported(
    role: str,
    normalized: str,
    rules: Sequence[Sequence[Sequence[str]]],
) -> bool:
    if _conditional_role_declares_absence(role, normalized):
        return False
    numeric_comparison = (
        role in {"ablation", "robustness"}
        and _has_numeric_observed_comparison(normalized)
    )
    for alternative in rules:
        group_matches = [
            any(_semantic_term_present(normalized, term) for term in group)
            for group in alternative
        ]
        if all(group_matches):
            return True
        if (
            numeric_comparison
            and len(group_matches) > 1
            and all(group_matches[:-1])
        ):
            return True
    return False


def _role_evidence_score(role: str, text: str) -> int:
    semantic_role = _semantic_role_key(role)
    concepts = _ROLE_DISTINCTIVE_CONCEPTS.get(semantic_role, ())
    normalized = " ".join(
        unicodedata.normalize("NFKC", text).casefold().split()
    )
    rules = _CONDITIONAL_ROLE_SIGNAL_RULES.get(semantic_role)
    if rules is not None and not _conditional_role_signal_supported(
        semantic_role,
        normalized,
        rules,
    ):
        return 0
    score = sum(
        any(_semantic_term_present(normalized, term) for term in alternatives)
        for alternatives in concepts
    )
    return max(score, 1) if rules is not None else score


def _semantic_evidence_candidate(
    role: str,
    evidence: Sequence[Mapping[str, Any]],
) -> str | None:
    scored: list[tuple[int, str]] = []
    for item in evidence:
        score = _role_evidence_score(role, str(item.get("text", "")))
        scored.append((score, str(item.get("id", ""))))
    scored.sort(key=lambda item: (-item[0], item[1]))
    top_score, top_id = scored[0] if scored else (0, "")
    runner_up = scored[1][0] if len(scored) > 1 else 0
    if (
        top_score >= _SEMANTIC_MIN_CONCEPTS
        and top_score - runner_up >= _SEMANTIC_MARGIN
    ):
        return top_id
    return None


def _semantic_term_present(normalized_text: str, term: str) -> bool:
    normalized_term = unicodedata.normalize("NFKC", term).casefold()
    if any(not character.isascii() for character in normalized_term):
        return normalized_term in normalized_text
    if " " in normalized_term:
        return normalized_term in normalized_text
    return re.search(
        rf"(?<![a-z0-9]){re.escape(normalized_term)}[a-z]*(?![a-z0-9])",
        normalized_text,
    ) is not None


def _role_definition(role: str) -> tuple[str, str, str]:
    for definition in _ACADEMIC_ARC:
        if definition[0] == role:
            return definition
    if role in _SUBSTITUTE_ROLE_DEFINITIONS:
        return _SUBSTITUTE_ROLE_DEFINITIONS[role]
    match = re.fullmatch(r"evidence-deep-dive-(\d+)", role)
    if match:
        number = int(match.group(1))
        return (
            role,
            f"Evidence deep dive {number}",
            "Explain one additional source-backed result or mechanism",
        )
    raise PptHarnessError(f"unknown deck role: {role}")


def _adaptive_arc_and_evidence(
    expected_arc: Sequence[tuple[str, str, str]],
    evidence: Sequence[Mapping[str, Any]],
) -> tuple[list[tuple[str, str, str]], list[list[str]]]:
    resolved_arc: list[tuple[str, str, str]] = []
    assignments: list[list[str]] = []
    used_roles: set[str] = set()
    for expected in expected_arc:
        expected_role = expected[0]
        candidate = _semantic_evidence_candidate(expected_role, evidence)
        resolved = expected
        if candidate is None:
            for substitute_role in _CONDITIONAL_ROLE_SUBSTITUTIONS.get(
                expected_role,
                (),
            ):
                if substitute_role in used_roles:
                    continue
                candidate = _semantic_evidence_candidate(substitute_role, evidence)
                if candidate is not None:
                    resolved = _role_definition(substitute_role)
                    break
        if candidate is None:
            raise PptHarnessError(
                f"could not semantically assign evidence for role {expected_role}; "
                "provide --story-plan with a source-backed role substitution"
            )
        if resolved[0] in used_roles:
            raise PptHarnessError(
                f"adaptive deck plan would duplicate role {resolved[0]}; provide --story-plan"
            )
        used_roles.add(resolved[0])
        resolved_arc.append(resolved)
        assignments.append([candidate])
    return resolved_arc, assignments


def _host_story_arc_and_evidence(
    story_plan: Mapping[str, Any],
    expected_arc: Sequence[tuple[str, str, str]],
    evidence: Sequence[Mapping[str, Any]],
) -> tuple[list[tuple[str, str, str]], list[list[str]]]:
    if set(story_plan) != {"format_version", "slides"} or story_plan.get(
        "format_version"
    ) != 1:
        raise PptHarnessError("story plan must use the exact version-1 schema")
    slides = story_plan.get("slides")
    if not isinstance(slides, list) or len(slides) != len(expected_arc):
        raise PptHarnessError("story plan slide count must match the requested deck")
    evidence_by_id = {
        str(item.get("id", "")): str(item.get("text", "")) for item in evidence
    }
    evidence_ids = set(evidence_by_id)
    resolved_arc: list[tuple[str, str, str]] = []
    assignments: list[list[str]] = []
    seen_roles: set[str] = set()
    for index, (entry, expected) in enumerate(zip(slides, expected_arc), start=1):
        if not isinstance(entry, Mapping) or set(entry) != {
            "slide_id",
            "role",
            "evidence_refs",
        }:
            raise PptHarnessError("story plan slides must use the exact role/evidence schema")
        expected_id = f"slide-{index:02d}"
        if entry.get("slide_id") != expected_id:
            raise PptHarnessError("story plan slide IDs must be contiguous and ordered")
        role = entry.get("role")
        if not isinstance(role, str):
            raise PptHarnessError("story plan roles must be strings")
        allowed_roles = {
            expected[0],
            *_CONDITIONAL_ROLE_SUBSTITUTIONS.get(expected[0], ()),
        }
        if role not in allowed_roles:
            raise PptHarnessError(
                f"story plan role {role} is not a supported substitution for "
                f"academic slot {expected[0]}"
            )
        if role in seen_roles:
            raise PptHarnessError(f"story plan role {role} is duplicated")
        seen_roles.add(role)
        refs = entry.get("evidence_refs")
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) for ref in refs)
            or len(refs) != len(set(refs))
        ):
            raise PptHarnessError("story plan evidence refs must be a non-empty unique list")
        unknown = sorted(set(refs) - evidence_ids)
        if unknown:
            raise PptHarnessError(
                f"story plan cites unknown evidence: {', '.join(unknown)}"
            )
        if max(
            (_role_evidence_score(role, evidence_by_id[ref]) for ref in refs),
            default=0,
        ) < _SEMANTIC_MIN_CONCEPTS:
            raise PptHarnessError(
                f"story plan role {role} is not supported by its cited evidence"
            )
        resolved_arc.append(_role_definition(role))
        assignments.append(list(refs))

    actual_roles = [definition[0] for definition in resolved_arc]
    required_backbone = [
        role
        for role in _NARRATIVE_BACKBONE
        if role in {definition[0] for definition in expected_arc}
    ]
    actual_backbone = [role for role in actual_roles if role in required_backbone]
    if actual_backbone != required_backbone:
        raise PptHarnessError(
            "story plan must preserve the ordered academic narrative backbone"
        )
    return resolved_arc, assignments


def build_deck_plan(
    brief: str,
    evidence_ids: Sequence[str],
    *,
    slide_count: int | None = None,
    evidence_texts: Mapping[str, str] | None = None,
    story_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the shared paper-deck plan: 18 by default, explicit requests win."""

    requested = slide_count if slide_count is not None else _explicit_slide_count(brief)
    count = requested if requested is not None else DEFAULT_SLIDE_COUNT
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 60:
        raise PptHarnessError("slide count must be an integer from 1 through 60")
    sources = [item for item in dict.fromkeys(evidence_ids) if re.fullmatch(r"ev-\d{3,}", item)]
    if not sources:
        raise PptHarnessError("deck planning requires at least one evidence ID")
    expected_arc = _arc_for_count(count)
    evidence = [
        {"id": source_id, "text": str((evidence_texts or {}).get(source_id, ""))}
        for source_id in sources
    ]
    if story_plan is not None:
        arc, evidence_assignments = _host_story_arc_and_evidence(
            story_plan,
            expected_arc,
            evidence,
        )
        assignment_source = "host_story_plan"
    else:
        arc, evidence_assignments = _adaptive_arc_and_evidence(
            expected_arc,
            evidence,
        )
        assignment_source = "semantic_default"
    slides: list[dict[str, Any]] = []
    for index, (role, title, communication_job) in enumerate(arc, start=1):
        planned_sources = evidence_assignments[index - 1]
        source_text = "\n".join(
            str((evidence_texts or {}).get(source_id, ""))
            for source_id in planned_sources
        )
        source_anchor = _bounded_source_anchor(source_text)
        assertion_title = (
            f"{title}: {source_anchor}" if source_anchor else title
        )
        talk = communication_job
        if source_anchor:
            talk = f"{communication_job}. Ground this slide in: {source_anchor}"
        slides.append(
            {
                "slide_id": f"slide-{index:02d}",
                "slide_index": index,
                "role": role,
                "chapter": "paper-talk",
                "communication_job": communication_job,
                "assertion_title": assertion_title,
                "evidence_refs": planned_sources,
                "speaker_note_intent": f"[Sources] {', '.join(planned_sources)} [Talk] {talk}.",
                "layout_family": "editorial-evidence",
            }
        )
    return {
        "format_version": 1,
        "artifact_type": "deck",
        "brief": brief,
        "slide_count": count,
        "count_source": "explicit_user" if requested is not None else "academic_default",
        "canvas": {"width": SLIDE_WIDTH, "height": SLIDE_HEIGHT},
        "evidence_assignment_source": assignment_source,
        "visual_allocations": [],
        "slides": slides,
    }


def _contained_dependency(root: Path, base: Path, reference: str) -> Path | None:
    try:
        return exporter.resolve_local_dependency(root, base, reference)
    except (OSError, exporter.PptContractError) as error:
        raise PptHarnessError(
            f"local deck dependency is missing or unsafe: {reference}: {error}"
        ) from error


def _dependency_closure(deck: Any) -> list[Path]:
    root = deck.html_path.parent.resolve(strict=True)
    pending: list[Path] = []
    for _tag, _attribute, reference in deck.resources:
        dependency = _contained_dependency(root, root, reference)
        if dependency is not None:
            pending.append(dependency)
    resolved: set[Path] = set()
    css_url = re.compile(
        r"url\(\s*(?:(['\"])(.*?)\1|([^)'\"\s]+))\s*\)", re.IGNORECASE
    )
    css_import = re.compile(r"@import\s+(['\"])(.*?)\1", re.IGNORECASE)
    while pending:
        path = pending.pop()
        if path in resolved:
            continue
        resolved.add(path)
        if path.suffix.lower() != ".css":
            continue
        try:
            css = path.read_text(encoding="utf-8")
        except UnicodeError as error:
            raise PptHarnessError(f"CSS dependency is not UTF-8: {path.name}") from error
        references = [
            (match.group(2) or match.group(3)) for match in css_url.finditer(css)
        ]
        references.extend(match.group(2) for match in css_import.finditer(css))
        for reference in references:
            nested = _contained_dependency(root, path.parent, reference)
            if nested is not None:
                pending.append(nested)
    return sorted(resolved)


def _copy_dependencies(deck: Any, destination_root: Path) -> None:
    root = deck.html_path.parent.resolve(strict=True)
    for path in _dependency_closure(deck):
        relative = path.relative_to(root)
        target = destination_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def validate_deck_against_plan(
    deck: Any, plan: Mapping[str, Any]
) -> dict[str, Any]:
    planned_slides = plan.get("slides")
    if not isinstance(planned_slides, list):
        return {
            "name": "deck_plan",
            "passed": False,
            "issues": ["deck plan has no ordered slide list"],
        }
    issues: list[str] = []
    if len(deck.slides) != len(planned_slides):
        issues.append(
            f"planned {len(planned_slides)} slides but authored {len(deck.slides)}"
        )
    for index, (slide, planned) in enumerate(
        zip(deck.slides, planned_slides), start=1
    ):
        if not isinstance(planned, Mapping):
            issues.append(f"slide {index}: plan entry is not an object")
            continue
        expected = {
            "id": planned.get("slide_id"),
            "data-slide-id": planned.get("slide_id"),
            "data-slide-index": str(planned.get("slide_index")),
            "data-slide-role": planned.get("role"),
            "data-section": planned.get("chapter"),
            "data-assertion-title": planned.get("assertion_title"),
        }
        actual = {
            "id": slide.attrs.get("id"),
            "data-slide-id": slide.attrs.get("data-slide-id"),
            "data-slide-index": slide.attrs.get("data-slide-index"),
            "data-slide-role": slide.attrs.get("data-slide-role"),
            "data-section": slide.attrs.get("data-section"),
            "data-assertion-title": slide.attrs.get("data-assertion-title"),
        }
        for field, expected_value in expected.items():
            if actual[field] != expected_value:
                issues.append(
                    f"slide {index}: {field} differs from the immutable plan"
                )
        planned_sources = [str(item) for item in planned.get("evidence_refs", [])]
        authored_sources = [
            item
            for item in re.split(
                r"[\s,;]+", slide.attrs.get("data-source-ids", "").strip()
            )
            if item
        ]
        if authored_sources != planned_sources:
            issues.append(f"slide {index}: evidence refs differ from the immutable plan")
        assertion_elements = [
            element
            for element in slide.elements
            if element.kind == "text" and element.tag == "h1"
        ]
        if len(assertion_elements) != 1 or (
            assertion_elements
            and assertion_elements[0].text
            != str(planned.get("assertion_title", "")).strip()
        ):
            issues.append(
                f"slide {index}: visible H1 differs from the immutable assertion title"
            )
        for element_index, element in enumerate(slide.elements, start=1):
            if element.kind not in {"text", "table"}:
                continue
            element_sources = [
                item
                for item in re.split(
                    r"[\s,;]+", element.attrs.get("data-source-ids", "").strip()
                )
                if item
            ]
            if element_sources != planned_sources:
                issues.append(
                    f"slide {index}: native {element.kind} {element_index} sources differ from the immutable plan"
                )
        parsed_notes = exporter.parse_speaker_notes(
            slide.attrs.get("data-speaker-notes", "")
        )
        if parsed_notes is None or parsed_notes[0] != planned_sources:
            issues.append(
                f"slide {index}: speaker-note refs differ from the immutable plan"
            )
        expected_note = " ".join(
            str(planned.get("speaker_note_intent", "")).split()
        )
        authored_note = " ".join(
            slide.attrs.get("data-speaker-notes", "").split()
        )
        if authored_note != expected_note:
            issues.append(
                f"slide {index}: speaker-note intent differs from the immutable plan"
            )
    return {"name": "deck_plan", "passed": not issues, "issues": issues}


def _verify_attempt_plan_snapshot(run_dir: Path, attempt: str) -> Path:
    plan_path = run_dir / "plan.json"
    snapshot = (
        run_dir / "attempts" / attempt / "artifact" / "provenance" / "plan.json"
    )
    if (
        snapshot.is_symlink()
        or not snapshot.is_file()
        or snapshot.lstat().st_nlink > 1
        or snapshot.read_bytes() != plan_path.read_bytes()
    ):
        raise PptHarnessError("attempt plan snapshot differs from the bound plan")
    return snapshot


def _artifact_delivery_paths(
    artifact_root: Path,
    deck: Any,
    *,
    require_notes: bool = True,
    require_outputs: bool,
) -> list[str]:
    expected = {
        Path("deck.html"),
        Path("deck.pdf"),
        Path("deck.pptx"),
        Path("notes.json"),
        Path("provenance/plan.json"),
    }
    if artifact_root.is_symlink() or not artifact_root.is_dir():
        raise PptHarnessError("artifact root is missing or unsafe")
    root = artifact_root.resolve(strict=True)
    for dependency in _dependency_closure(deck):
        expected.add(dependency.relative_to(root))
    allowed_directories = {
        parent
        for relative in expected
        for parent in relative.parents
        if parent != Path(".")
    }
    actual_files: list[Path] = []
    for path in sorted(artifact_root.rglob("*")):
        relative = path.relative_to(artifact_root)
        if path.is_symlink():
            raise PptHarnessError(f"artifact contains a symlink: {relative}")
        status = path.lstat()
        if path.is_dir():
            if relative not in allowed_directories:
                raise PptHarnessError(f"unexpected artifact directory: {relative}")
            continue
        if relative not in expected:
            raise PptHarnessError(f"unexpected artifact file: {relative}")
        if not path.is_file() or status.st_nlink > 1:
            raise PptHarnessError(f"artifact contains a hardlink or unsafe file: {relative}")
        actual_files.append(path)
    required = {Path("deck.html"), Path("provenance/plan.json")}
    if require_notes:
        required.add(Path("notes.json"))
    if require_outputs:
        required.update({Path("deck.pdf"), Path("deck.pptx")})
    missing = sorted(str(path) for path in required if not (artifact_root / path).is_file())
    if missing:
        raise PptHarnessError(
            "required delivery artifact is missing: " + ", ".join(missing)
        )
    return [
        f"artifact/{path.relative_to(artifact_root).as_posix()}"
        for path in actual_files
    ]


def create_slide_audit_variants(
    html_path: Path | str,
    output_dir: Path | str,
    expected_slide_count: int,
) -> list[Path]:
    """Create one immutable local-asset-closed browser target per slide."""

    source = Path(html_path).expanduser().resolve(strict=True)
    validation = exporter.validate_deck_html(source, expected_slide_count=expected_slide_count)
    if not validation["passed"]:
        codes = ", ".join(sorted({issue["code"] for issue in validation["issues"]}))
        raise PptHarnessError(f"deck HTML contract failed before browser QA: {codes}")
    root = _external_output(output_dir)
    if root.exists() and any(root.iterdir()):
        raise PptHarnessError("slide audit output directory must be empty")
    root.mkdir(parents=True, exist_ok=True)
    raw = source.read_text(encoding="utf-8")
    deck = exporter.parse_deck_html(source)
    variants: list[Path] = []
    for index in range(1, expected_slide_count + 1):
        slide_id = f"slide-{index:02d}"
        slide_root = root / slide_id
        slide_root.mkdir()
        _copy_dependencies(deck, slide_root)
        isolation = (
            "<style data-autodesign-slide-audit>"
            "html,body{width:1920px!important;height:1080px!important;overflow:hidden!important;"
            "margin:0!important;background:#fff!important}"
            ".deck-slide{display:none!important;margin:0!important}"
            f"#{slide_id}{{display:block!important}}"
            "</style>"
        )
        closing_head = re.search(r"</head\s*>", raw, re.IGNORECASE)
        if closing_head is None:
            raise PptHarnessError("deck HTML requires a head element")
        variant = raw[: closing_head.start()] + isolation + raw[closing_head.start() :]
        target = slide_root / "index.html"
        target.write_text(variant, encoding="utf-8")
        variants.append(target)
    return variants


def validate_computed_slide_canvases(
    measurements: Sequence[Mapping[str, Any]], expected_slide_count: int
) -> dict[str, Any]:
    """Fail unless every authored slide root computes to the canonical canvas."""

    issues: list[str] = []
    expected_ids = [
        f"slide-{index:02d}" for index in range(1, expected_slide_count + 1)
    ]
    actual_ids = [str(item.get("slide_id", "")) for item in measurements]
    if actual_ids != expected_ids:
        issues.append("computed slide roots are missing, duplicated, or out of order")
    for index, item in enumerate(measurements, start=1):
        slide_id = str(item.get("slide_id") or f"slide-at-{index}")
        expected = {
            "computed_width": SLIDE_WIDTH,
            "computed_height": SLIDE_HEIGHT,
            "offset_width": SLIDE_WIDTH,
            "offset_height": SLIDE_HEIGHT,
            "rect_width": SLIDE_WIDTH,
            "rect_height": SLIDE_HEIGHT,
        }
        for field, target in expected.items():
            value = item.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or abs(float(value) - target) > 0.5
            ):
                issues.append(
                    f"{slide_id}: actual {field} must be {target}, found {value!r}"
                )
    return {"name": "computed_slide_canvas", "passed": not issues, "issues": issues}


def _measure_computed_slide_canvases(html_path: Path, runtime: Any) -> list[dict[str, Any]]:
    script = r'''
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

source = Path(sys.argv[1]).resolve(strict=True).as_uri()
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=[
        '--disable-background-networking', '--disable-component-update',
        '--disable-default-apps', '--disable-domain-reliability', '--disable-sync',
        '--metrics-recording-only', '--no-first-run', '--no-default-browser-check',
        '--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE localhost',
    ])
    page = browser.new_page(viewport={'width': 1920, 'height': 1080})
    page.route('http://**/*', lambda route: route.abort('blockedbyclient'))
    page.route('https://**/*', lambda route: route.abort('blockedbyclient'))
    page.goto(source, wait_until='load', timeout=30000)
    measurements = page.evaluate("""() => [...document.querySelectorAll('.deck-slide')].map(slide => {
      const style = getComputedStyle(slide);
      const rect = slide.getBoundingClientRect();
      return {
        slide_id: slide.id,
        computed_width: Number.parseFloat(style.width),
        computed_height: Number.parseFloat(style.height),
        offset_width: slide.offsetWidth,
        offset_height: slide.offsetHeight,
        rect_width: rect.width,
        rect_height: rect.height,
      };
    })""")
    print(json.dumps(measurements, sort_keys=True))
    browser.close()
'''
    environment = browser_setup.isolated_environment(
        browsers_path=runtime.browsers_path,
        allow_network_configuration=False,
    )
    result = subprocess.run(
        [str(runtime.python_executable), "-B", "-I", "-c", script, str(html_path)],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no browser output").strip()[-1000:]
        raise PptHarnessError(f"browser could not inspect computed slide canvases: {detail}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise PptHarnessError("browser returned unreadable computed slide canvases") from error
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise PptHarnessError("browser returned an invalid computed slide canvas report")
    return value


def write_contact_sheet_html(
    preview_paths: Sequence[Path | str], output_path: Path | str
) -> Path:
    """Create a self-contained visual review sheet without modifying previews."""

    previews = [Path(path).expanduser().resolve(strict=True) for path in preview_paths]
    if not previews:
        raise PptHarnessError("contact sheet requires at least one slide preview")
    output = _external_output(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    assets = output.parent / "assets"
    assets.mkdir(exist_ok=True)
    cards: list[str] = []
    for index, preview in enumerate(previews, start=1):
        target = assets / f"slide-{index:02d}.png"
        shutil.copyfile(preview, target)
        cards.append(
            f'<figure><img src="assets/{target.name}" alt="Slide {index} preview">'
            f"<figcaption>Slide {index}</figcaption></figure>"
        )
    html = "".join(
        (
            "<!doctype html><html><head><meta charset=\"utf-8\">",
            "<title>Deck contact sheet</title><style>",
            "*{box-sizing:border-box}body{margin:0;padding:32px;background:#ecebe6;color:#171717;",
            "font:16px Arial,Helvetica,sans-serif}main{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));",
            "gap:24px}figure{margin:0;background:#fff;border:1px solid #cbc8bf;padding:10px}",
            "img{display:block;width:100%;aspect-ratio:16/9;object-fit:contain;background:#fff}",
            "figcaption{margin-top:8px;font-weight:700}</style></head><body><main>",
            *cards,
            "</main></body></html>",
        )
    )
    output.write_text(html, encoding="utf-8")
    return output


def _browser_pdf(
    html_path: Path,
    output_path: Path,
    runtime: Any,
    *,
    timeout_seconds: int = 180,
) -> None:
    output_path = _external_output(output_path)
    script = """
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
source = Path(sys.argv[1]).resolve(strict=True).as_uri()
target = Path(sys.argv[2]).resolve(strict=False)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=[
        '--disable-background-networking', '--disable-component-update',
        '--disable-default-apps', '--disable-domain-reliability', '--disable-sync',
        '--metrics-recording-only', '--no-first-run', '--no-default-browser-check',
        '--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE localhost',
    ])
    page = browser.new_page(viewport={'width': 1920, 'height': 1080})
    page.route('http://**/*', lambda route: route.abort('blockedbyclient'))
    page.route('https://**/*', lambda route: route.abort('blockedbyclient'))
    page.goto(source, wait_until='load', timeout=30000)
    page.wait_for_timeout(250)
    page.pdf(path=str(target), width='1920px', height='1080px',
             margin={'top':'0','right':'0','bottom':'0','left':'0'},
             print_background=True, prefer_css_page_size=False)
    browser.close()
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp-{uuid.uuid4().hex}")
    env = browser_setup.isolated_environment(
        browsers_path=runtime.browsers_path,
        allow_network_configuration=False,
    )
    try:
        result = subprocess.run(
            [str(runtime.python_executable), "-I", "-c", script, str(html_path), str(temporary)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
        if result.returncode != 0 or not temporary.is_file():
            detail = (result.stderr or result.stdout or "no browser output").strip()[-1000:]
            raise PptHarnessError(f"browser could not export deck PDF: {detail}")
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)


def _run_exporter(
    runtime: Any,
    command: Sequence[str],
    *,
    timeout_seconds: int = 240,
    allowed_returncodes: Sequence[int] = (0,),
) -> int:
    result = subprocess.run(
        [str(runtime.python_executable), "-B", "-I", str(Path(exporter.__file__).resolve()), *command],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if result.returncode not in allowed_returncodes:
        detail = (result.stderr or result.stdout or "no exporter output").strip()[-1200:]
        raise PptHarnessError(f"editable PPTX runtime failed: {detail}")
    return result.returncode


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path = _external_output(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(value), handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_qa_directory(attempt_root: Path | str) -> Path:
    """Replace only scratch QA from an interrupted, uncommitted validation."""

    attempt = Path(attempt_root).expanduser().resolve(strict=True)
    deterministic = attempt / "qa" / "deterministic.json"
    target = attempt / "qa" / "deck"
    if deterministic.exists():
        raise PptHarnessError("reviewed deterministic QA is immutable")
    if target.exists() or target.is_symlink():
        if target.is_symlink():
            raise PptHarnessError("QA scratch directory must not be a symlink")
        quarantine = target.with_name(f".{target.name}.interrupted-{uuid.uuid4().hex}")
        os.replace(target, quarantine)
        if quarantine.is_dir():
            shutil.rmtree(quarantine)
        else:
            quarantine.unlink()
    target.mkdir(parents=True)
    return target


def _optional_office_comparison(
    pptx_path: Path,
    previews_dir: Path,
    qa_dir: Path,
    count: int,
    ppt_runtime: Any,
) -> dict[str, Any]:
    office = exporter.render_pptx_with_libreoffice(pptx_path, qa_dir / "office")
    if not office.get("performed"):
        return office
    office["passed"] = office.get("page_count") == count
    if not office["passed"]:
        return office
    rasterizer = shutil.which("pdftoppm")
    if not rasterizer:
        return {
            **office,
            "passed": False,
            "comparison_performed": False,
            "reason": "pdftoppm unavailable",
        }
    rendered = qa_dir / "office" / "rendered"
    rendered.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [rasterizer, "-png", "-r", "144", str(office["pdf"]), str(rendered / "slide")],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise PptHarnessError("pdftoppm could not rasterize the reopened PPTX")
    report_path = qa_dir / "pptx-render-comparison.json"
    _run_exporter(
        ppt_runtime,
        [
            "compare",
            "--canonical-dir",
            str(previews_dir),
            "--rendered-dir",
            str(rendered),
            "--expected-slide-count",
            str(count),
            "--report",
            str(report_path),
        ],
        allowed_returncodes=(0, 2),
    )
    comparison = json.loads(report_path.read_text(encoding="utf-8"))
    return {**office, **comparison, "comparison_performed": True}


def render_and_validate_deck(
    html_path: Path | str,
    *,
    expected_slide_count: int,
    qa_dir: Path | str,
    browser_cache: Path | str | None = None,
    ppt_cache: Path | str | None = None,
    offline_browser: bool = False,
    offline_ppt: bool = False,
) -> dict[str, Any]:
    """Run every deterministic gate and create PDF/PPTX delivery artifacts."""

    html = Path(html_path).expanduser().resolve(strict=True)
    qa = _external_output(qa_dir)
    if qa.is_symlink():
        raise PptHarnessError("QA directory must not be a symlink")
    qa.mkdir(parents=True, exist_ok=True)
    contract = exporter.validate_deck_html(html, expected_slide_count=expected_slide_count)
    if not contract["passed"]:
        return {
            "format_version": 1,
            "passed": False,
            "html_contract": contract,
            "checks": [{"name": "html_contract", "passed": False}],
        }

    browser_runtime = browser_setup.ensure_browser_runtime(
        cache_root=Path(browser_cache) if browser_cache is not None else None,
        allow_install=not offline_browser,
    )
    computed_canvas = validate_computed_slide_canvases(
        _measure_computed_slide_canvases(html, browser_runtime),
        expected_slide_count,
    )
    if not computed_canvas["passed"]:
        return {
            "format_version": 1,
            "passed": False,
            "html_contract": contract,
            "computed_slide_canvas": computed_canvas,
            "checks": [
                {"name": "html_contract", "passed": True},
                computed_canvas,
            ],
        }
    ppt_runtime = ppt_setup.ensure_ppt_runtime(
        cache_root=Path(ppt_cache) if ppt_cache is not None else None,
        allow_install=not offline_ppt,
    )
    variants = create_slide_audit_variants(html, qa / "variants", expected_slide_count)
    previews = qa / "previews"
    previews.mkdir(exist_ok=True)
    browser_reports: dict[str, Any] = {}
    preview_paths: list[Path] = []
    for index, variant in enumerate(variants, start=1):
        slide_id = f"slide-{index:02d}"
        output = variant.parent / "audit"
        report = browser_setup.audit_local_html(
            variant,
            workspace_root=variant.parent,
            output_dir=output,
            viewports=[f"{slide_id}:1920x1080"],
            runtime=browser_runtime,
            allow_install=False,
        )
        browser_reports[slide_id] = report
        source_preview = output / f"{slide_id}.png"
        if not source_preview.is_file():
            raise PptHarnessError(f"browser audit omitted preview for {slide_id}")
        preview = previews / f"{slide_id}.png"
        shutil.copyfile(source_preview, preview)
        preview_paths.append(preview)

    contact_html = write_contact_sheet_html(preview_paths, qa / "contact" / "index.html")
    contact_report = browser_setup.audit_local_html(
        contact_html,
        workspace_root=contact_html.parent,
        output_dir=qa / "contact" / "audit",
        viewports=["contact-sheet:1440x900"],
        runtime=browser_runtime,
        allow_install=False,
    )
    contact_sheet = qa / "contact-sheet.png"
    shutil.copyfile(qa / "contact" / "audit" / "contact-sheet.png", contact_sheet)

    pdf_path = html.parent / "deck.pdf"
    _browser_pdf(html, pdf_path, browser_runtime)
    page_count = exporter.pdf_page_count(pdf_path)
    pptx_path = html.parent / "deck.pptx"
    pptx_report_path = qa / "pptx-validation.json"
    _run_exporter(
        ppt_runtime,
        [
            "export",
            "--html",
            str(html),
            "--output",
            str(pptx_path),
            "--report",
            str(pptx_report_path),
            "--preview-dir",
            str(previews),
            "--background-dir",
            str(qa / "backgrounds"),
        ],
    )
    pptx_validation = json.loads(pptx_report_path.read_text(encoding="utf-8"))
    office = _optional_office_comparison(
        pptx_path, previews, qa, expected_slide_count, ppt_runtime
    )
    browser_pass = all(report.get("passed") is True for report in browser_reports.values())
    office_pass = not office.get("performed") or (
        office.get("page_count") == expected_slide_count
        and office.get("comparison_performed") is True
        and office.get("passed") is True
    )
    checks = [
        {"name": "html_contract", "passed": bool(contract["passed"])},
        computed_canvas,
        {"name": "per_slide_browser_qa", "passed": browser_pass},
        {"name": "contact_sheet_browser_qa", "passed": contact_report.get("passed") is True},
        {"name": "pdf_page_count", "passed": page_count == expected_slide_count, "actual": page_count},
        {"name": "editable_pptx_reopen", "passed": pptx_validation.get("passed") is True},
        {"name": "rendered_pptx_comparison", "passed": office_pass, "performed": bool(office.get("performed"))},
    ]
    result = {
        "format_version": 1,
        "passed": all(check["passed"] is True for check in checks),
        "checks": checks,
        "html_contract": contract,
        "computed_slide_canvas": computed_canvas,
        "browser_reports": browser_reports,
        "contact_sheet_report": contact_report,
        "preview_paths": [str(path) for path in [*preview_paths, contact_sheet]],
        "contact_sheet": str(contact_sheet),
        "pdf_path": str(pdf_path),
        "pdf_page_count": page_count,
        "pptx_path": str(pptx_path),
        "pptx_validation": pptx_validation,
        "rendered_pptx_comparison": office,
    }
    _atomic_json(qa / "deck-validation.json", result)
    return result


REVIEW_RUBRIC = {
    "format_version": 1,
    "dimensions": [
        "source_fidelity",
        "narrative_coherence",
        "visual_hierarchy",
        "typography_legibility",
        "layout_composition",
        "evidence_communication",
        "speaker_notes",
    ],
    "minimum_score": 4,
    "instructions": (
        "A fresh host VLM or fresh subagent must inspect the contact sheet and every slide preview; "
        "report localized repairs and never infer quality from HTML alone."
    ),
}


def _passing_review_score_error(review: Mapping[str, Any]) -> str | None:
    if review.get("verdict") != "pass":
        return None
    scores = review.get("dimension_scores")
    if not isinstance(scores, dict) or set(scores) != set(REVIEW_RUBRIC["dimensions"]):
        return "passing review must score every bound rubric dimension"
    if any(
        not isinstance(score, (int, float))
        or isinstance(score, bool)
        or not math.isfinite(float(score))
        or not REVIEW_RUBRIC["minimum_score"] <= score <= 5
        for score in scores.values()
    ):
        return "passing review requires every bound dimension score to be at least 4"
    return None


def _verify_persisted_review_minimum(run_dir: Path, state: Mapping[str, Any]) -> None:
    attempt = state.get("active_attempt")
    if not isinstance(attempt, str):
        return
    review_path = run_dir / "attempts" / attempt / "qa" / "semantic-review.json"
    if not review_path.exists() and not review_path.is_symlink():
        return
    if (
        review_path.is_symlink()
        or not review_path.is_file()
        or review_path.lstat().st_nlink != 1
    ):
        raise PptHarnessError("persisted semantic review is linked or unsafe")
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PptHarnessError("persisted semantic review is unreadable") from error
    if not isinstance(review, dict):
        raise PptHarnessError("persisted semantic review is invalid")
    score_error = _passing_review_score_error(review)
    if score_error is not None:
        raise PptHarnessError(score_error)


def _verify_final_delivery_links(run_dir: Path) -> None:
    final = run_dir / "final"
    if not final.exists() and not final.is_symlink():
        return
    if final.is_symlink() or not final.is_dir():
        raise PptHarnessError("final delivery directory is linked or unsafe")
    for current, directories, filenames in os.walk(final, followlinks=False):
        current_path = Path(current)
        for name in directories:
            if (current_path / name).is_symlink():
                raise PptHarnessError("final delivery contains a symlink")
        for name in filenames:
            path = current_path / name
            status = path.lstat()
            if path.is_symlink() or not path.is_file() or status.st_nlink != 1:
                relative = path.relative_to(final).as_posix()
                raise PptHarnessError(
                    f"final delivery file has an unsafe link count or hardlink: {relative}"
                )


def _skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _run_state(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


def _plan_bytes(plan: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(plan), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _plan_binding(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "format_version": 1,
        "plan_sha256": hashlib.sha256(_plan_bytes(plan)).hexdigest(),
    }


def _verify_plan_binding(run_dir: Path, state: Mapping[str, Any]) -> dict[str, Any] | None:
    plan_path = run_dir / "plan.json"
    binding_path = run_dir / "plan-binding.json"
    state_digest = state.get(_PLAN_SHA_FIELD)
    if binding_path.is_symlink() or (
        binding_path.exists()
        and (not binding_path.is_file() or binding_path.lstat().st_nlink > 1)
    ):
        raise PptHarnessError("plan hash binding is linked or unsafe")
    if not plan_path.exists() and not binding_path.exists():
        if state.get("state") != "initialized" or state_digest is not None:
            raise PptHarnessError("planned run is missing its plan binding")
        return None
    if not plan_path.exists():
        if state.get("state") == "initialized" and binding_path.is_file():
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            if state_digest is not None and binding.get("plan_sha256") != state_digest:
                raise PptHarnessError("plan hash binding changed")
            return None
        raise PptHarnessError("plan binding exists without its plan")
    if plan_path.is_symlink() or plan_path.lstat().st_nlink > 1:
        raise PptHarnessError("plan file is linked or unsafe")
    if not binding_path.is_file():
        raise PptHarnessError("plan hash binding is missing")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    if binding != _plan_binding(plan) or state_digest != binding["plan_sha256"]:
        raise PptHarnessError("plan hash binding changed")
    return plan


def _bind_plan_before_save(run_dir: Path, plan: Mapping[str, Any]) -> None:
    binding_path = run_dir / "plan-binding.json"
    expected = _plan_binding(plan)
    state = _run_state(run_dir)
    state_digest = state.get(_PLAN_SHA_FIELD)
    if state_digest is not None and state_digest != expected["plan_sha256"]:
        raise PptHarnessError("refusing to replace a different plan hash binding")
    if binding_path.exists():
        if binding_path.is_symlink() or binding_path.lstat().st_nlink > 1:
            raise PptHarnessError("plan hash binding is linked or unsafe")
        existing = json.loads(binding_path.read_text(encoding="utf-8"))
        if existing != expected:
            raise PptHarnessError("refusing to replace a different plan hash binding")
    else:
        portable.atomic_write_json(binding_path, expected)
    if state_digest is None:
        state[_PLAN_SHA_FIELD] = expected["plan_sha256"]
        portable.atomic_write_json(run_dir / "run.json", state)


def _snapshot_plan_for_attempt(run_dir: Path, attempt: str) -> Path:
    plan_path = run_dir / "plan.json"
    plan = _verify_plan_binding(run_dir, _run_state(run_dir))
    if plan is None:
        raise PptHarnessError("attempt requires a bound plan")
    destination = (
        run_dir / "attempts" / attempt / "artifact" / "provenance" / "plan.json"
    )
    if destination.exists():
        if (
            destination.is_symlink()
            or not destination.is_file()
            or destination.lstat().st_nlink > 1
            or destination.read_bytes() != plan_path.read_bytes()
        ):
            raise PptHarnessError("attempt plan snapshot differs from the bound plan")
        return destination
    portable.atomic_write_bytes(destination, plan_path.read_bytes())
    return destination


def _resume(run_dir: Path) -> dict[str, Any]:
    state = portable.resume_run(run_dir, skill_root=_skill_root())
    _verify_plan_binding(run_dir, state)
    _verify_persisted_review_minimum(run_dir, state)
    _verify_final_delivery_links(run_dir)
    return state


def _active_attempt(run_dir: Path) -> str:
    state = _run_state(run_dir)
    attempt = state.get("active_attempt")
    if not isinstance(attempt, str):
        raise PptHarnessError("run has no active attempt")
    return attempt


def _begin_attempt(run_dir: Path) -> str:
    state = _run_state(run_dir)
    if int(state.get("attempt_count", 0)) >= MAX_REPAIR_ATTEMPTS:
        raise PptHarnessError(f"bounded repair limit reached ({MAX_REPAIR_ATTEMPTS} attempts)")
    return portable.begin_attempt(run_dir)


def _command_init(args: argparse.Namespace) -> dict[str, Any]:
    portable.initialize_run(
        args.run_dir,
        _skill_root(),
        release_version=RELEASE_VERSION,
        archive_sha256=args.archive_sha256,
    )
    manifest = portable.prepare_source(
        args.run_dir,
        args.source,
        extra_assets=args.extra_asset,
        reference_images=args.reference_image,
    )
    return {
        "passed": manifest.get("status") == "ready",
        "source_manifest": manifest,
        "resume": _resume(args.run_dir),
    }


def _command_plan(args: argparse.Namespace) -> dict[str, Any]:
    _resume(args.run_dir)
    evidence = portable.load_evidence(args.run_dir)
    story_plan_path = getattr(args, "story_plan", None)
    story_plan: Mapping[str, Any] | None = None
    if story_plan_path is not None:
        value = json.loads(story_plan_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise PptHarnessError("story plan must be a JSON object")
        story_plan = value
    plan = build_deck_plan(
        args.brief,
        [item["id"] for item in evidence],
        slide_count=args.slide_count,
        evidence_texts={str(item["id"]): str(item.get("text", "")) for item in evidence},
        story_plan=story_plan,
    )
    if args.visual_allocations is not None:
        allocations = json.loads(args.visual_allocations.read_text(encoding="utf-8"))
        if not isinstance(allocations, list) or any(not isinstance(item, dict) for item in allocations):
            raise PptHarnessError("visual allocations must be a JSON list of objects")
        slide_ids = {item["slide_id"] for item in plan["slides"]}
        for allocation in allocations:
            if allocation.get("slide_id") not in slide_ids:
                raise PptHarnessError("visual allocation targets an unknown slide")
        visual_check = portable.validate_visual_plan(args.run_dir, allocations)
        if not visual_check["valid"]:
            raise PptHarnessError("visual allocation exceeds an allowed reuse limit")
        plan["visual_allocations"] = allocations
    clean_plan = portable.redact_secrets(plan)
    _bind_plan_before_save(args.run_dir, clean_plan)
    saved = portable.save_plan(args.run_dir, clean_plan)
    _verify_plan_binding(args.run_dir, _run_state(args.run_dir))
    return saved


def _command_evidence(args: argparse.Namespace) -> dict[str, Any]:
    _resume(args.run_dir)
    evidence = portable.load_evidence(args.run_dir)
    return {
        "query": args.query,
        "results": portable.lexical_retrieve(evidence, args.query, limit=args.limit),
    }


def _command_visuals(args: argparse.Namespace) -> dict[str, Any]:
    _resume(args.run_dir)
    return json.loads((args.run_dir / "evidence" / "source_visuals.json").read_text(encoding="utf-8"))


def _command_bind_visuals(args: argparse.Namespace) -> dict[str, Any]:
    _resume(args.run_dir)
    review = json.loads(args.review.read_text(encoding="utf-8"))
    result = portable.bind_host_vlm_visuals(args.run_dir, review)
    return {"source_visuals": result, "resume": _resume(args.run_dir)}


def _command_begin(args: argparse.Namespace) -> dict[str, Any]:
    _resume(args.run_dir)
    attempt = _begin_attempt(args.run_dir)
    _snapshot_plan_for_attempt(args.run_dir, attempt)
    artifact = args.run_dir / "attempts" / attempt / "artifact" / "deck.html"
    return {"attempt_id": attempt, "author_target": str(artifact)}


def _command_stage_visual(args: argparse.Namespace) -> dict[str, Any]:
    _resume(args.run_dir)
    attempt = args.attempt or _active_attempt(args.run_dir)
    plan = json.loads((args.run_dir / "plan.json").read_text(encoding="utf-8"))
    allocation = next(
        (
            item
            for item in plan.get("visual_allocations", [])
            if item.get("visual_id") == args.visual_id
        ),
        None,
    )
    if allocation is None:
        raise PptHarnessError("visual must be approved in the immutable deck plan before staging")
    catalog = json.loads(
        (args.run_dir / "evidence" / "source_visuals.json").read_text(encoding="utf-8")
    )
    visual = next(
        (item for item in catalog.get("visuals", []) if item.get("id") == args.visual_id),
        None,
    )
    if not isinstance(visual, dict) or visual.get("eligibility") != "eligible":
        raise PptHarnessError("visual is unknown or not content-eligible")
    relative = visual.get("path")
    if not isinstance(relative, str):
        raise PptHarnessError("visual has no staged evidence path")
    source = portable.safe_path(args.run_dir / "evidence", relative, must_exist=True)
    if portable.sha256_file(source) != visual.get("sha256"):
        raise PptHarnessError("visual bytes differ from the evidence catalog")
    if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}:
        raise PptHarnessError("native PPTX export requires a supported raster image")
    target = (
        args.run_dir
        / "attempts"
        / attempt
        / "artifact"
        / "assets"
        / f"{args.visual_id}{source.suffix.lower()}"
    )
    portable.atomic_write_bytes(target, source.read_bytes())
    return {
        "attempt_id": attempt,
        "visual_id": args.visual_id,
        "slide_id": allocation.get("slide_id"),
        "artifact_src": f"assets/{target.name}",
        "sha256": portable.sha256_file(target),
    }


def _run_visual_gate(run_dir: Path, deck: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    allocations = plan.get("visual_allocations", [])
    if not isinstance(allocations, list):
        return {"name": "visual_provenance", "passed": False, "issues": ["invalid plan allocations"]}
    by_id: dict[str, list[dict[str, Any]]] = {}
    for item in allocations:
        if isinstance(item, dict) and isinstance(item.get("visual_id"), str):
            by_id.setdefault(item["visual_id"], []).append(item)
    catalog = json.loads((run_dir / "evidence" / "source_visuals.json").read_text(encoding="utf-8"))
    catalog_by_id = {
        item.get("id"): item for item in catalog.get("visuals", []) if isinstance(item, dict)
    }
    used: Counter[tuple[str, str]] = Counter()
    issues: list[str] = []
    for slide in deck.slides:
        for element in slide.elements:
            if element.kind != "image":
                continue
            source_ids = [
                item
                for item in re.split(r"[\s,;]+", element.attrs.get("data-source-ids", "").strip())
                if item
            ]
            matches = [item for item in source_ids if item in by_id]
            if len(matches) != 1:
                issues.append(f"{slide.slide_id}: image must name exactly one planned visual ID")
                continue
            visual_id = matches[0]
            visual_allocations = by_id[visual_id]
            visual = catalog_by_id.get(visual_id)
            if not any(item.get("slide_id") == slide.slide_id for item in visual_allocations):
                issues.append(f"{slide.slide_id}: visual {visual_id} is allocated to another slide")
            if not isinstance(visual, dict) or visual.get("eligibility") != "eligible":
                issues.append(f"{slide.slide_id}: visual {visual_id} is not content-eligible")
                continue
            source_path = element.attrs.get("src", "")
            try:
                staged = _contained_dependency(
                    deck.html_path.parent.resolve(strict=True),
                    deck.html_path.parent.resolve(strict=True),
                    source_path,
                )
            except (OSError, PptHarnessError):
                staged = None
            if staged is None:
                issues.append(f"{slide.slide_id}: staged visual {visual_id} is missing")
            elif portable.sha256_file(staged) != visual.get("sha256"):
                issues.append(f"{slide.slide_id}: staged visual {visual_id} has the wrong hash")
            used[(visual_id, slide.slide_id)] += 1
    planned: Counter[tuple[str, str]] = Counter(
        (visual_id, str(item.get("slide_id", "")))
        for visual_id, items in by_id.items()
        for item in items
    )
    missing = sorted((planned - used).elements())
    issues.extend(
        f"planned visual {visual_id} is absent from {slide_id}"
        for visual_id, slide_id in missing
    )
    extra = sorted((used - planned).elements())
    issues.extend(
        f"visual {visual_id} is used more often than planned on {slide_id}"
        for visual_id, slide_id in extra
    )
    return {"name": "visual_provenance", "passed": not issues, "issues": issues}


def _command_validate(args: argparse.Namespace) -> dict[str, Any]:
    _resume(args.run_dir)
    attempt = args.attempt or _active_attempt(args.run_dir)
    attempt_root = args.run_dir / "attempts" / attempt
    qa_root = prepare_qa_directory(attempt_root)
    artifact_root = attempt_root / "artifact"
    html = artifact_root / "deck.html"
    _verify_attempt_plan_snapshot(args.run_dir, attempt)
    plan = _verify_plan_binding(args.run_dir, _run_state(args.run_dir))
    if plan is None:
        raise PptHarnessError("validation requires a bound plan")
    deck = exporter.parse_deck_html(html)
    plan_gate = validate_deck_against_plan(deck, plan)
    _artifact_delivery_paths(
        artifact_root, deck, require_notes=False, require_outputs=False
    )
    notes_path = artifact_root / "notes.json"
    _atomic_json(notes_path, {"format_version": 1, "slides": exporter.notes_from_deck(deck)})
    portable.write_source_map(args.run_dir, attempt, exporter.claims_from_deck(deck))
    visual_gate = _run_visual_gate(args.run_dir, deck, plan)
    if plan_gate["passed"] and visual_gate["passed"]:
        result = render_and_validate_deck(
            html,
            expected_slide_count=int(plan["slide_count"]),
            qa_dir=qa_root,
            browser_cache=args.browser_cache,
            ppt_cache=args.ppt_cache,
            offline_browser=args.offline_browser,
            offline_ppt=args.offline_ppt,
        )
        result["checks"] = [plan_gate, visual_gate, *result.get("checks", [])]
        result["passed"] = (
            plan_gate["passed"]
            and visual_gate["passed"]
            and bool(result.get("passed"))
        )
        _atomic_json(qa_root / "deck-validation.json", result)
    else:
        result = {
            "format_version": 1,
            "passed": False,
            "checks": [plan_gate, visual_gate],
            "preview_paths": [],
        }
        _atomic_json(qa_root / "deck-validation.json", result)
    artifact_paths = _artifact_delivery_paths(
        artifact_root, deck, require_outputs=bool(result.get("passed"))
    )
    preview_paths: dict[str, str] = {}
    for path_text in result.get("preview_paths", []):
        path = Path(path_text).resolve(strict=True)
        frame = "contact-sheet" if path.name == "contact-sheet.png" else path.stem
        preview_paths[frame] = path.relative_to(
            attempt_root.resolve(strict=True)
        ).as_posix()
    portable.record_deterministic_result(
        args.run_dir,
        attempt,
        passed=bool(result.get("passed")),
        checks=result.get("checks", []),
        artifact_paths=artifact_paths,
        preview_paths=preview_paths,
    )
    return {"attempt_id": attempt, **result, "resume": _resume(args.run_dir)}


def _command_review_context(args: argparse.Namespace) -> dict[str, Any]:
    _resume(args.run_dir)
    attempt = args.attempt or _active_attempt(args.run_dir)
    return portable.create_review_context(args.run_dir, attempt, rubric=REVIEW_RUBRIC)


def _command_record_review(args: argparse.Namespace) -> dict[str, Any]:
    _resume(args.run_dir)
    attempt = args.attempt or _active_attempt(args.run_dir)
    review = json.loads(args.review.read_text(encoding="utf-8"))
    score_error = _passing_review_score_error(review)
    if score_error is not None:
        raise PptHarnessError(score_error)
    portable.record_semantic_review(args.run_dir, attempt, review)
    return _resume(args.run_dir)


def _command_finalize(args: argparse.Namespace) -> dict[str, Any]:
    _resume(args.run_dir)
    attempt = args.attempt or _active_attempt(args.run_dir)
    manifest = portable.finalize_attempt(args.run_dir, attempt)
    return {"delivery_manifest": manifest, "resume": _resume(args.run_dir)}


def _command_resume(args: argparse.Namespace) -> dict[str, Any]:
    return _resume(args.run_dir)


def _command_doctor(args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {
        "format_version": 1,
        "python": sys.version.split()[0],
        "pdf_tools": {
            name: bool(shutil.which(name)) for name in ("pdfinfo", "pdftoppm", "pdftotext", "pdfimages")
        },
        "office_renderer": shutil.which("soffice") or shutil.which("libreoffice"),
    }
    try:
        browser = browser_setup.ensure_browser_runtime(
            cache_root=args.browser_cache,
            allow_install=not args.offline,
        )
        result["browser_runtime"] = {"ready": True, "cache_dir": str(browser.cache_dir)}
    except Exception as error:
        result["browser_runtime"] = {"ready": False, "error": str(error)}
    try:
        ppt = ppt_setup.ensure_ppt_runtime(cache_root=args.ppt_cache, allow_install=not args.offline)
        result["ppt_runtime"] = {"ready": True, "cache_dir": str(ppt.cache_dir)}
    except Exception as error:
        result["ppt_runtime"] = {"ready": False, "error": str(error)}
    result["passed"] = (
        all(result["pdf_tools"].values())
        and result["browser_runtime"]["ready"]
        and result["ppt_runtime"]["ready"]
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--run-dir", type=Path, required=True)
    initialize.add_argument("--source", type=Path, required=True)
    initialize.add_argument("--extra-asset", type=Path, action="append", default=[])
    initialize.add_argument("--reference-image", type=Path, action="append", default=[])
    initialize.add_argument("--archive-sha256")
    initialize.set_defaults(handler=_command_init)
    plan = commands.add_parser("plan")
    plan.add_argument("--run-dir", type=Path, required=True)
    plan.add_argument("--brief", required=True)
    plan.add_argument("--slide-count", type=int)
    plan.add_argument("--story-plan", type=Path)
    plan.add_argument("--visual-allocations", type=Path)
    plan.set_defaults(handler=_command_plan)
    evidence = commands.add_parser("evidence")
    evidence.add_argument("--run-dir", type=Path, required=True)
    evidence.add_argument("--query", required=True)
    evidence.add_argument("--limit", type=int, default=8)
    evidence.set_defaults(handler=_command_evidence)
    visuals = commands.add_parser("visuals")
    visuals.add_argument("--run-dir", type=Path, required=True)
    visuals.set_defaults(handler=_command_visuals)
    bind = commands.add_parser("bind-visuals")
    bind.add_argument("--run-dir", type=Path, required=True)
    bind.add_argument("--review", type=Path, required=True)
    bind.set_defaults(handler=_command_bind_visuals)
    begin = commands.add_parser("begin")
    begin.add_argument("--run-dir", type=Path, required=True)
    begin.set_defaults(handler=_command_begin)
    stage = commands.add_parser("stage-visual")
    stage.add_argument("--run-dir", type=Path, required=True)
    stage.add_argument("--attempt")
    stage.add_argument("--visual-id", required=True)
    stage.set_defaults(handler=_command_stage_visual)
    validate = commands.add_parser("validate")
    validate.add_argument("--run-dir", type=Path, required=True)
    validate.add_argument("--attempt")
    validate.add_argument("--browser-cache", type=Path)
    validate.add_argument("--ppt-cache", type=Path)
    validate.add_argument("--offline-browser", action="store_true")
    validate.add_argument("--offline-ppt", action="store_true")
    validate.set_defaults(handler=_command_validate)
    context = commands.add_parser("review-context")
    context.add_argument("--run-dir", type=Path, required=True)
    context.add_argument("--attempt")
    context.set_defaults(handler=_command_review_context)
    record = commands.add_parser("record-review")
    record.add_argument("--run-dir", type=Path, required=True)
    record.add_argument("--attempt")
    record.add_argument("--review", type=Path, required=True)
    record.set_defaults(handler=_command_record_review)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--run-dir", type=Path, required=True)
    finalize.add_argument("--attempt")
    finalize.set_defaults(handler=_command_finalize)
    resume = commands.add_parser("resume")
    resume.add_argument("--run-dir", type=Path, required=True)
    resume.set_defaults(handler=_command_resume)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--browser-cache", type=Path)
    doctor.add_argument("--ppt-cache", type=Path)
    doctor.add_argument("--offline", action="store_true")
    doctor.set_defaults(handler=_command_doctor)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = args.handler(args)
        print(json.dumps(portable.redact_secrets(payload), ensure_ascii=True, indent=2, sort_keys=True))
        return 0 if payload.get("passed", True) is not False else 2
    except (
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        portable.PortableError,
        PptHarnessError,
    ) as error:
        print(f"ERROR: {portable.redact_secrets(str(error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
