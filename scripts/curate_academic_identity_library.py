"""Offline curation runner for academic identity logo allowlist gaps.

The runner creates review artifacts only. It never mutates the tracked
allowlist; accepted logo candidates are written to proposed_allowlist.json and
allowlist.patch for human review.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import html
import json
import os
import re
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from autodesign.agents.identity_logo_agent import IdentityLogoAgent  # noqa: E402
from autodesign.config import (  # noqa: E402
    Settings,
    identity_logo_agent_command_for_harness,
    load_settings,
)
from autodesign.tools import ToolContext  # noqa: E402
from autodesign.util.academic_identity import append_identity_asset, refresh_identity_asset_metrics  # noqa: E402
from autodesign.util.academic_identity_search import (  # noqa: E402
    ALLOWLIST_PATH,
    FetchUrl,
    find_allowlist_rule,
    load_academic_identity_allowlist,
    resolve_academic_identity_assets,
)
from autodesign.util.io import atomic_write_json  # noqa: E402


TARGET_SET_ID = "high-frequency-ai-identity-2026-06"


def _unique_strings(items: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _rule(
    rule_id: str,
    entity_name: str,
    role: str,
    aliases: list[str],
    official_domains: list[str],
    *,
    homepages: list[str] | None = None,
    preferred_page_urls: list[str] | None = None,
    preferred_asset_urls: list[str] | None = None,
    allowed_asset_domains: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    pages = preferred_page_urls if preferred_page_urls is not None else homepages
    return {
        "id": rule_id,
        "entity_name": entity_name,
        "role": role,
        "aliases": _unique_strings([entity_name, *aliases]),
        "official_domains": _unique_strings(official_domains),
        "allowed_asset_domains": _unique_strings(allowed_asset_domains or []),
        "homepages": _unique_strings(homepages or []),
        "preferred_page_urls": _unique_strings(pages or []),
        "preferred_asset_urls": _unique_strings(preferred_asset_urls or []),
        "confidence": 0.82,
        "curation_status": "manual_review_needed",
        "tags": _unique_strings(["offline-curation", TARGET_SET_ID, *(tags or [])]),
    }


TARGET_RULES: list[dict[str, Any]] = [
    _rule("company-alibaba-group", "Alibaba Group", "company", ["Alibaba", "Alibaba DAMO Academy", "DAMO Academy"], ["alibabagroup.com", "alibaba.com", "damo.alibaba.com"], homepages=["https://www.alibabagroup.com/en-US/", "https://damo.alibaba.com/?language=en"], preferred_page_urls=["https://www.alibabagroup.com/en-US/about-alibaba", "https://damo.alibaba.com/?language=en"], tags=["china-ai-company"]),
    _rule("lab-tencent-ai-lab", "Tencent AI Lab", "lab", ["Tencent AI", "Tencent Artificial Intelligence Lab", "Tencent"], ["ai.tencent.com", "tencent.com"], homepages=["https://ai.tencent.com/ailab/en/index/"], tags=["china-ai-lab"]),
    _rule("lab-huawei-noahs-ark", "Huawei Noah's Ark Lab", "lab", ["Huawei Noah's Ark", "Noah's Ark Lab", "Huawei Noahs Ark Lab", "Huawei Noah's Ark Laboratory"], ["noahlab.com.hk", "huawei.com"], homepages=["https://www.noahlab.com.hk/"], tags=["china-ai-lab"]),
    _rule("lab-baidu-research", "Baidu Research", "lab", ["Baidu", "Baidu AI"], ["research.baidu.com", "baidu.com"], homepages=["https://research.baidu.com/"], tags=["china-ai-lab"]),
    _rule("company-bytedance", "ByteDance Research", "company", ["ByteDance", "Bytedance Research", "ByteDance AI Lab", "ByteDance Seed", "Seed"], ["bytedance.com", "seed.bytedance.com"], homepages=["https://seed.bytedance.com/en/", "https://www.bytedance.com/en/"], tags=["china-ai-company"]),
    _rule("company-deepseek", "DeepSeek", "company", ["DeepSeek AI", "Deepseek"], ["deepseek.com"], allowed_asset_domains=["cdn.deepseek.com"], homepages=["https://www.deepseek.com/en/", "https://www.deepseek.com/"], preferred_asset_urls=["https://cdn.deepseek.com/logo.png"], tags=["china-ai-company"]),
    _rule("company-zhipu-ai", "Zhipu AI", "company", ["Zhipu", "ZhipuAI", "Z.ai", "Z AI"], ["zhipuai.cn", "z.ai"], homepages=["https://www.zhipuai.cn/en/", "https://www.zhipuai.cn/"], preferred_asset_urls=["https://www.zhipuai.cn/logo-en.svg"], tags=["china-ai-company"]),
    _rule("company-moonshot-ai", "Moonshot AI", "company", ["Kimi", "Kimi AI", "Moonshot", "Kimi/Moonshot"], ["moonshot.cn", "moonshot.ai", "kimi.com"], homepages=["https://www.moonshot.cn/", "https://www.kimi.com/"], tags=["china-ai-company"]),
    _rule("company-minimax", "MiniMax", "company", ["MiniMax AI", "Minimax"], ["minimax.io", "minimaxi.com"], homepages=["https://www.minimax.io/"], tags=["china-ai-company"]),
    _rule("company-stepfun", "StepFun", "company", ["Step Fun", "StepFun AI", "Jieyue Xingchen"], ["stepfun.com"], homepages=["https://www.stepfun.com/"], tags=["china-ai-company"]),
    _rule("institution-epfl", "EPFL", "institution", ["Ecole Polytechnique Federale de Lausanne", "Swiss Federal Institute of Technology Lausanne"], ["epfl.ch"], homepages=["https://www.epfl.ch/"], preferred_page_urls=["https://www.epfl.ch/campus/services/communication/en/visual-identity/"], preferred_asset_urls=["https://www.epfl.ch/wp-content/themes/wp-theme-2018/assets/svg/epfl-logo.svg"], tags=["europe-ai-school"]),
    _rule("institution-technical-university-of-munich", "Technical University of Munich", "institution", ["TUM", "Technische Universitat Munchen", "Technical University Munich"], ["tum.de"], homepages=["https://www.tum.de/en/"], preferred_page_urls=["https://www.tum.de/en/about-tum/corporate-design"], preferred_asset_urls=["https://www.tum.de/typo3conf/ext/in2template/Resources/Public/Images/Backend/tum-logo.svg"], tags=["europe-ai-school"]),
    _rule("institution-imperial-college-london", "Imperial College London", "institution", ["Imperial College", "Imperial"], ["imperial.ac.uk"], homepages=["https://www.imperial.ac.uk/"], tags=["europe-ai-school"]),
    _rule("institution-ucl", "University College London", "institution", ["UCL", "University College London"], ["ucl.ac.uk"], allowed_asset_domains=["cdn.ucl.ac.uk"], homepages=["https://www.ucl.ac.uk/"], preferred_page_urls=["https://www.ucl.ac.uk/brand/essentials/logos"], preferred_asset_urls=["https://cdn.ucl.ac.uk/logos/ucl/ucl-logo--primary.svg"], tags=["europe-ai-school"]),
    _rule("institution-university-of-edinburgh", "University of Edinburgh", "institution", ["Edinburgh University", "Edinburgh"], ["ed.ac.uk"], homepages=["https://www.ed.ac.uk/"], preferred_page_urls=["https://www.ed.ac.uk/about/website/brand"], preferred_asset_urls=["https://www.ed.ac.uk/themes/upstream/wpp_theme/images/uoe-logo-centred-black.png"], tags=["europe-ai-school"]),
    _rule("institution-aalto-university", "Aalto University", "institution", ["Aalto"], ["aalto.fi", "aaltologo.fi"], homepages=["https://www.aalto.fi/en", "https://aaltologo.fi/"], preferred_asset_urls=["https://www.aalto.fi/themes/custom/aalto_base/images/aalto_logo.svg"], tags=["europe-ai-school"]),
    _rule("institution-mbzuai", "Mohamed bin Zayed University of Artificial Intelligence", "institution", ["MBZUAI", "Mohamed bin Zayed University of AI"], ["mbzuai.ac.ae"], allowed_asset_domains=["staticcdn.mbzuai.ac.ae"], homepages=["https://mbzuai.ac.ae/"], preferred_page_urls=["https://mbzuai.ac.ae/brand/"], tags=["middle-east-ai-school"]),
    _rule("institution-kaust", "King Abdullah University of Science and Technology", "institution", ["KAUST"], ["kaust.edu.sa"], allowed_asset_domains=["ipomedia.kaust.edu.sa"], homepages=["https://www.kaust.edu.sa/en"], preferred_page_urls=["https://www.kaust.edu.sa/en/about/brand"], preferred_asset_urls=["https://www.kaust.edu.sa/ResourcePackages/KAUSTMain/assets/dist/images/kaust-logo.svg"], tags=["middle-east-ai-school"]),
    _rule("institution-peking-university", "Peking University", "institution", ["PKU", "Beida"], ["pku.edu.cn"], homepages=["https://english.pku.edu.cn/"], tags=["existing-badge-gap"]),
    _rule("institution-shanghai-jiao-tong-university", "Shanghai Jiao Tong University", "institution", ["Shanghai Jiaotong University", "SJTU"], ["sjtu.edu.cn", "global.sjtu.edu.cn"], homepages=["https://en.sjtu.edu.cn/", "https://global.sjtu.edu.cn/en/"], preferred_asset_urls=["https://global.sjtu.edu.cn/en/assets/images/logo_white_130.png"], tags=["existing-badge-gap"]),
    _rule("institution-zhejiang-university", "Zhejiang University", "institution", ["ZJU"], ["zju.edu.cn"], homepages=["https://www.zju.edu.cn/english/"], tags=["existing-badge-gap"]),
    _rule("institution-nanjing-university", "Nanjing University", "institution", ["NJU"], ["nju.edu.cn", "nju.edu"], homepages=["https://www.nju.edu.cn/", "https://www.nju.edu/"], tags=["existing-badge-gap"]),
    _rule("institution-fudan-university", "Fudan University", "institution", ["Fudan"], ["fudan.edu.cn"], homepages=["https://www.fudan.edu.cn/en/"], tags=["existing-badge-gap"]),
    _rule("institution-ustc", "University of Science and Technology of China", "institution", ["USTC"], ["ustc.edu.cn"], homepages=["https://en.ustc.edu.cn/"], tags=["existing-badge-gap"]),
    _rule("institution-xidian-university", "Xidian University", "institution", ["Xidian", "XDU"], ["xidian.edu.cn"], homepages=["https://en.xidian.edu.cn/"], tags=["existing-badge-gap"]),
    _rule("institution-wuhan-university", "Wuhan University", "institution", ["WHU"], ["whu.edu.cn"], homepages=["https://en.whu.edu.cn/"], tags=["existing-badge-gap"]),
    _rule("institution-southeast-university", "Southeast University", "institution", ["SEU"], ["seu.edu.cn"], homepages=["https://english.seu.edu.cn/", "https://www.seu.edu.cn/english/"], tags=["existing-badge-gap"]),
    _rule("institution-chinese-academy-of-sciences", "Chinese Academy of Sciences", "institution", ["CAS"], ["cas.cn"], homepages=["https://english.cas.cn/"], preferred_asset_urls=["https://english.cas.cn/images/logo.png"], tags=["existing-badge-gap"]),
    _rule("lab-shanghai-ai-laboratory", "Shanghai AI Laboratory", "lab", ["Shanghai AI Lab", "SHLAB"], ["shlab.org.cn"], homepages=["https://www.shlab.org.cn/"], preferred_asset_urls=["https://www.shlab.org.cn/static/asset/img/share-logo.png"], tags=["existing-badge-gap"]),
    _rule("lab-baai", "Beijing Academy of Artificial Intelligence", "lab", ["BAAI"], ["baai.ac.cn"], homepages=["https://www.baai.ac.cn/english.html"], tags=["existing-badge-gap"]),
    _rule("lab-peng-cheng-laboratory", "Peng Cheng Laboratory", "lab", ["PCL"], ["pcl.ac.cn"], homepages=["https://www.pcl.ac.cn/"], tags=["existing-badge-gap"]),
    _rule("institution-eth-zurich", "ETH Zurich", "institution", ["ETH", "ETH Zuerich"], ["ethz.ch"], homepages=["https://ethz.ch/en.html"], preferred_page_urls=["https://ethz.ch/en/the-eth-zurich/portrait/corporate-design.html"], tags=["existing-badge-gap"]),
    _rule("institution-university-of-oxford", "University of Oxford", "institution", ["Oxford University", "Oxford"], ["ox.ac.uk", "brand.ox.ac.uk"], homepages=["https://www.ox.ac.uk/", "https://brand.ox.ac.uk/"], preferred_page_urls=["https://brand.ox.ac.uk/"], tags=["existing-badge-gap"]),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create offline review artifacts for academic identity logo allowlist curation.",
    )
    parser.add_argument("--target-set", default=TARGET_SET_ID, choices=(TARGET_SET_ID,))
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--identity-logo-agent", choices=("auto", "off", "required"), default="off")
    parser.add_argument("--identity-logo-agent-harness", choices=("custom", "codex", "claude", "deepseek", "opencode", "kimi", "mimo", "zcode"), default=None)
    parser.add_argument("--identity-logo-agent-cmd", default=None)
    parser.add_argument("--identity-logo-agent-model", default=None)
    parser.add_argument("--identity-logo-agent-timeout", type=int, default=None)
    parser.add_argument("--max-entities", type=int, default=None)
    parser.add_argument("--max-candidates", type=int, default=None)
    parser.add_argument(
        "--resolver-network",
        choices=("off", "on"),
        default="off",
        help="Default off runs resolver validation without network fetches; on downloads/materializes official assets.",
    )
    args = parser.parse_args(argv)

    out_dir = args.out_dir or (
        REPO_ROOT / "out" / "identity_logo_curation" / datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    result = run_curation(
        out_dir=out_dir,
        identity_logo_agent=args.identity_logo_agent,
        identity_logo_agent_harness=args.identity_logo_agent_harness,
        identity_logo_agent_cmd=args.identity_logo_agent_cmd,
        identity_logo_agent_model=args.identity_logo_agent_model,
        identity_logo_agent_timeout=args.identity_logo_agent_timeout,
        max_entities=args.max_entities,
        max_candidates=args.max_candidates,
        fetcher=None if args.resolver_network == "on" else _offline_fetcher,
    )
    print(f"academic_identity_curation: report={result['report_path']}")
    print(f"academic_identity_curation: proposed_allowlist={result['proposed_allowlist_path']}")
    print(f"academic_identity_curation: patch={result['patch_path']}")
    return 0


def run_curation(
    *,
    out_dir: Path,
    target_rules: list[dict[str, Any]] | None = None,
    identity_logo_agent: str = "off",
    identity_logo_agent_harness: str | None = None,
    identity_logo_agent_cmd: str | None = None,
    identity_logo_agent_model: str | None = None,
    identity_logo_agent_timeout: int | None = None,
    max_entities: int | None = None,
    max_candidates: int | None = None,
    fetcher: FetchUrl | None = None,
    base_allowlist_path: Path = ALLOWLIST_PATH,
) -> dict[str, Any]:
    """Run curation and write review artifacts without mutating the source allowlist."""

    out_dir.mkdir(parents=True, exist_ok=True)
    layers_dir = out_dir / "layers"
    layers_dir.mkdir(parents=True, exist_ok=True)
    seeds = [copy.deepcopy(item) for item in (target_rules or TARGET_RULES)]
    base_allowlist = load_academic_identity_allowlist(base_allowlist_path)
    temp_allowlist = _merge_target_rules(base_allowlist, seeds)
    temp_allowlist["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    temp_allowlist.setdefault("description", "Temporary academic identity curation allowlist.")
    temp_allowlist_path = out_dir / "temporary_allowlist.json"
    atomic_write_json(temp_allowlist_path, temp_allowlist)

    identity_assets = _build_identity_assets(seeds)
    rendered_layers: dict[str, dict[str, Any]] = {}
    resolved = _resolve_identity_assets(
        identity_assets=identity_assets,
        rendered_layers=rendered_layers,
        run_dir=out_dir,
        layers_dir=layers_dir,
        allowlist_path=temp_allowlist_path,
        fetcher=fetcher,
        target_rules=seeds,
    )

    settings = _settings_from_args(
        identity_logo_agent=identity_logo_agent,
        identity_logo_agent_harness=identity_logo_agent_harness,
        identity_logo_agent_cmd=identity_logo_agent_cmd,
        identity_logo_agent_model=identity_logo_agent_model,
        identity_logo_agent_timeout=identity_logo_agent_timeout,
        max_entities=max_entities,
        max_candidates=max_candidates,
    )
    agent_result: dict[str, Any] = {"status": "disabled", "resolver": "identity_logo_agent"}
    if settings.identity_logo_agent_mode != "off":
        ctx = ToolContext(settings=settings, run_dir=out_dir, layers_dir=layers_dir, run_id=out_dir.name)
        ctx.state["rendered_layers"] = rendered_layers
        resolved, agent_result = IdentityLogoAgent(settings).run(
            ctx=ctx,
            identity_assets=resolved,
            allowlist_path=temp_allowlist_path,
        )

    accepted = _accepted_candidates(resolved, target_rules=seeds)
    rejected = _rejected_candidates(resolved, seeds)
    proposed_allowlist = _apply_accepted_assets_to_allowlist(temp_allowlist, accepted)
    proposed_allowlist_path = out_dir / "proposed_allowlist.json"
    accepted_path = out_dir / "accepted_candidates.json"
    rejected_path = out_dir / "rejected_candidates.json"
    patch_path = out_dir / "allowlist.patch"
    report_path = out_dir / "index.html"

    atomic_write_json(proposed_allowlist_path, proposed_allowlist)
    atomic_write_json(accepted_path, {
        "version": 1,
        "target_set": TARGET_SET_ID,
        "draft_only": True,
        "accepted_count": len(accepted),
        "candidates": accepted,
    })
    atomic_write_json(rejected_path, {
        "version": 1,
        "target_set": TARGET_SET_ID,
        "draft_only": True,
        "rejected_count": len(rejected),
        "candidates": rejected,
    })
    atomic_write_json(out_dir / "rendered_layers.json", rendered_layers)
    atomic_write_json(out_dir / "academic_identity_assets.json", resolved)
    atomic_write_json(out_dir / "identity_logo_agent_result.json", agent_result)
    _write_patch(base_allowlist_path, proposed_allowlist_path, patch_path)
    _write_report(report_path, target_rules=seeds, accepted=accepted, rejected=rejected, identity_assets=resolved, agent_result=agent_result)
    return {
        "report_path": str(report_path),
        "proposed_allowlist_path": str(proposed_allowlist_path),
        "patch_path": str(patch_path),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
    }


def _settings_from_args(
    *,
    identity_logo_agent: str,
    identity_logo_agent_harness: str | None,
    identity_logo_agent_cmd: str | None,
    identity_logo_agent_model: str | None,
    identity_logo_agent_timeout: int | None,
    max_entities: int | None,
    max_candidates: int | None,
) -> Settings:
    try:
        settings = load_settings()
    except RuntimeError:
        settings = Settings(
            anthropic_api_key="identity-curation-stub",
            anthropic_base_url=None,
            gemini_api_key="",
            designer_model="identity-curation-stub",
            critic_model="identity-curation-stub",
            identity_logo_agent_mode="off",
            identity_logo_agent_harness=os.getenv("DESIGN_ANYTHING_IDENTITY_LOGO_AGENT_HARNESS", "codex").strip() or "codex",
            identity_logo_agent_model=os.getenv("DESIGN_ANYTHING_IDENTITY_LOGO_AGENT_MODEL", "").strip() or None,
            identity_logo_agent_cmd=os.getenv("DESIGN_ANYTHING_IDENTITY_LOGO_AGENT_CMD", "").strip(),
        )
    updates: dict[str, Any] = {"identity_logo_agent_mode": identity_logo_agent}
    if identity_logo_agent_harness is not None:
        updates["identity_logo_agent_harness"] = identity_logo_agent_harness
    if identity_logo_agent_model is not None:
        updates["identity_logo_agent_model"] = identity_logo_agent_model or None
    if identity_logo_agent_timeout is not None:
        updates["identity_logo_agent_timeout_s"] = identity_logo_agent_timeout
    if max_entities is not None:
        updates["identity_logo_agent_max_entities"] = max_entities
    if max_candidates is not None:
        updates["identity_logo_agent_max_candidates"] = max_candidates
    if identity_logo_agent_cmd is not None:
        updates["identity_logo_agent_cmd"] = identity_logo_agent_cmd
    settings = replace(settings, **updates)
    if (
        identity_logo_agent_cmd is None
        and settings.identity_logo_agent_mode != "off"
        and settings.identity_logo_agent_harness != "custom"
        and (not settings.identity_logo_agent_cmd or identity_logo_agent_harness is not None or identity_logo_agent_model is not None)
    ):
        settings = replace(
            settings,
            identity_logo_agent_cmd=identity_logo_agent_command_for_harness(
                settings.identity_logo_agent_harness,
                settings.identity_logo_agent_model,
            ),
        )
    return settings


def _merge_target_rules(base_allowlist: dict[str, Any], seeds: list[dict[str, Any]]) -> dict[str, Any]:
    merged = copy.deepcopy(base_allowlist)
    merged.setdefault("version", 1)
    rules = [rule for rule in (merged.get("rules") or []) if isinstance(rule, dict)]
    merged["rules"] = rules
    for seed in seeds:
        existing = _find_rule_for_seed(merged, seed)
        if existing is None:
            rules.append(copy.deepcopy(seed))
        else:
            _merge_rule_fields(existing, seed)
    return merged


def _find_rule_for_seed(allowlist: dict[str, Any], seed: dict[str, Any]) -> dict[str, Any] | None:
    seed_id = str(seed.get("id") or "")
    for rule in allowlist.get("rules") or []:
        if isinstance(rule, dict) and rule.get("id") == seed_id:
            return rule
    return find_allowlist_rule(seed.get("entity_name"), role=seed.get("role"), allowlist=allowlist)


def _merge_rule_fields(rule: dict[str, Any], seed: dict[str, Any]) -> None:
    for field in ("aliases", "official_domains", "allowed_asset_domains", "homepages", "preferred_page_urls", "preferred_asset_urls", "tags"):
        rule[field] = _unique_strings([*(rule.get(field) or []), *(seed.get(field) or [])])
    try:
        rule["confidence"] = max(float(rule.get("confidence") or 0), float(seed.get("confidence") or 0))
    except Exception:
        rule["confidence"] = rule.get("confidence") or seed.get("confidence") or 0.82
    rule.setdefault("curation_status", seed.get("curation_status") or "manual_review_needed")


def _build_identity_assets(rules: list[dict[str, Any]]) -> dict[str, Any]:
    entities: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    for rule in rules:
        name = str(rule.get("entity_name") or "").strip()
        role = str(rule.get("role") or "institution").strip() or "institution"
        if not name:
            continue
        entities.append({
            "entity_name": name,
            "role": role,
            "source": "offline_curation_target",
            "confidence": rule.get("confidence", 0.82),
            "placement_intent": "verified_affiliation",
            "required_to_place": role in {"venue", "institution", "lab", "company"},
        })
        assets.append({
            "asset_id": f"identity_badge_{_slug(name)}",
            "entity_name": name,
            "label": name,
            "role": role,
            "asset_type": "text_badge",
            "safe_to_place": True,
            "source": "offline_curation_target",
        })
    return {"kind": "academic_identity_assets", "version": 1, "target_set": TARGET_SET_ID, "entities": entities, "assets": assets}


def _resolve_identity_assets(
    *,
    identity_assets: dict[str, Any],
    rendered_layers: dict[str, dict[str, Any]],
    run_dir: Path,
    layers_dir: Path,
    allowlist_path: Path,
    fetcher: FetchUrl | None,
    target_rules: list[dict[str, Any]],
) -> dict[str, Any]:
    state = copy.deepcopy(identity_assets)
    search_results: list[dict[str, Any]] = []
    for start in range(0, len(target_rules), 10):
        batch_rules = target_rules[start:start + 10]
        batch_state = _build_identity_assets(batch_rules)
        resolved = resolve_academic_identity_assets(
            identity_assets=batch_state,
            rendered_layers=rendered_layers,
            run_dir=run_dir,
            layers_dir=layers_dir,
            brief="",
            allowlist_path=allowlist_path,
            fetcher=fetcher,
            enabled=True,
        )
        search = resolved.get("search") if isinstance(resolved.get("search"), dict) else {}
        search_results.extend([item for item in (search.get("results") or []) if isinstance(item, dict)])
        for asset in resolved.get("assets") or []:
            if isinstance(asset, dict) and asset.get("asset_type") == "image":
                state = append_identity_asset(state, copy.deepcopy(asset))
    state["search"] = _aggregate_search_results(search_results, allowlist_path=allowlist_path)
    state = refresh_identity_asset_metrics(state)
    metrics = dict(state.get("metrics") or {})
    status_counts = state["search"].get("status_counts") or {}
    metrics.update({
        "identity_search_result_count": len(search_results),
        "identity_search_resolved_count": status_counts.get("resolved", 0),
        "identity_search_status_counts": status_counts,
    })
    state["metrics"] = metrics
    return state


def _aggregate_search_results(results: list[dict[str, Any]], *, allowlist_path: Path) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    unresolved_required: list[dict[str, Any]] = []
    for item in results:
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status != "resolved" and item.get("required_to_place"):
            unresolved_required.append({
                "entity_name": item.get("entity_name"),
                "role": item.get("role"),
                "status": status,
            })
    payload: dict[str, Any] = {
        "enabled": True,
        "resolver": "academic_identity_search",
        "version": 1,
        "allowlist_path": str(allowlist_path),
        "results": results,
        "status_counts": status_counts,
    }
    if unresolved_required:
        payload["unresolved_required_entities"] = unresolved_required
    return payload


def _offline_fetcher(url: str, max_bytes: int) -> Any:
    raise RuntimeError(f"offline resolver mode refused network fetch: {url}")


def _accepted_candidates(
    identity_assets: dict[str, Any],
    *,
    target_rules: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    target_keys = {
        _entity_key(rule.get("entity_name"))
        for rule in (target_rules or [])
        if isinstance(rule, dict)
    }
    out: list[dict[str, Any]] = []
    for asset in identity_assets.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        if asset.get("asset_type") != "image" or not asset.get("safe_to_place"):
            continue
        if target_keys and _entity_key(asset.get("entity_name")) not in target_keys:
            continue
        out.append({
            "entity_name": asset.get("entity_name"),
            "role": asset.get("role"),
            "source_url": asset.get("source_url"),
            "discovered_from_url": asset.get("discovered_from_url"),
            "discovery_method": asset.get("discovery_method"),
            "allowlist_rule_id": asset.get("allowlist_rule_id"),
            "content_type": asset.get("content_type"),
            "content_sha256": asset.get("content_sha256") or asset.get("sha256"),
            "local_asset_path": asset.get("local_asset_path"),
            "rendered_layer_id": asset.get("rendered_layer_id"),
        })
    return out


def _rejected_candidates(identity_assets: dict[str, Any], target_rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    accepted_keys = {
        _entity_key(item.get("entity_name"))
        for item in _accepted_candidates(identity_assets, target_rules=target_rules)
        if item.get("entity_name")
    }
    target_keys = {
        _entity_key(rule.get("entity_name"))
        for rule in target_rules
        if isinstance(rule, dict)
    }
    search = identity_assets.get("search") if isinstance(identity_assets.get("search"), dict) else {}
    for result in search.get("results") or []:
        result_key = _entity_key(result.get("entity_name"))
        if (
            not isinstance(result, dict)
            or result_key not in target_keys
            or result_key in accepted_keys
        ):
            continue
        rejected = result.get("rejected") if isinstance(result.get("rejected"), list) else []
        if rejected:
            for item in rejected:
                if isinstance(item, dict):
                    rows.append({"entity_name": result.get("entity_name"), "role": result.get("role"), "status": result.get("status"), "url": item.get("url"), "reason": item.get("reason"), "discovery_method": item.get("discovery_method")})
        else:
            rows.append({"entity_name": result.get("entity_name"), "role": result.get("role"), "status": result.get("status"), "url": result.get("source_url"), "reason": result.get("reason") or result.get("status")})
    seen = {_entity_key(row.get("entity_name")) for row in rows}
    for rule in target_rules:
        key = _entity_key(rule.get("entity_name"))
        if key and key not in accepted_keys and key not in seen:
            rows.append({"entity_name": rule.get("entity_name"), "role": rule.get("role"), "status": "not_resolved", "reason": "no_verified_logo_asset"})
    return rows


def _apply_accepted_assets_to_allowlist(allowlist: dict[str, Any], accepted: list[dict[str, Any]]) -> dict[str, Any]:
    proposed = copy.deepcopy(allowlist)
    for candidate in accepted:
        url = str(candidate.get("source_url") or "").strip()
        if not url:
            continue
        rule = find_allowlist_rule(candidate.get("entity_name"), role=candidate.get("role"), allowlist=proposed)
        if not rule:
            continue
        rule["preferred_asset_urls"] = _unique_strings([*(rule.get("preferred_asset_urls") or []), url])
        host = (urlparse(url).hostname or "").lower()
        if host and not _host_in_domains(host, rule.get("official_domains") or []):
            rule["allowed_asset_domains"] = _unique_strings([*(rule.get("allowed_asset_domains") or []), host])
        rule["tags"] = _unique_strings([*(rule.get("tags") or []), "offline-curation-reviewed-candidate"])
    proposed["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return proposed


def _write_patch(original_path: Path, proposed_path: Path, patch_path: Path) -> None:
    with original_path.open("r", encoding="utf-8") as f:
        original_data = json.load(f)
    with proposed_path.open("r", encoding="utf-8") as f:
        proposed_data = json.load(f)
    original = _canonical_json(original_data).splitlines(keepends=True)
    proposed = _canonical_json(proposed_data).splitlines(keepends=True)
    diff = difflib.unified_diff(original, proposed, fromfile=str(original_path), tofile=str(proposed_path))
    patch_path.write_text("".join(diff), encoding="utf-8")


def _canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_report(
    report_path: Path,
    *,
    target_rules: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    identity_assets: dict[str, Any],
    agent_result: dict[str, Any],
) -> None:
    accepted_rows = "\n".join(_accepted_row(item, report_path.parent) for item in accepted)
    rejected_rows = "\n".join(
        "<tr>"
        f"<td>{_e(item.get('entity_name'))}</td><td>{_e(item.get('role'))}</td>"
        f"<td>{_e(item.get('status'))}</td><td>{_e(item.get('reason'))}</td><td>{_url(item.get('url'))}</td>"
        "</tr>"
        for item in rejected
    )
    target_rows = "\n".join(
        "<tr>"
        f"<td>{_e(rule.get('entity_name'))}</td><td>{_e(rule.get('role'))}</td>"
        f"<td>{_e(', '.join(rule.get('official_domains') or []))}</td>"
        f"<td>{_e(', '.join(rule.get('preferred_page_urls') or []))}</td>"
        "</tr>"
        for rule in target_rules
    )
    metrics = identity_assets.get("metrics") if isinstance(identity_assets.get("metrics"), dict) else {}
    report = f"""<!doctype html>
<meta charset="utf-8">
<title>Academic Identity Logo Curation</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 24px; color: #17202a; }}
h1 {{ font-size: 24px; margin: 0 0 16px; }}
h2 {{ font-size: 18px; margin: 28px 0 10px; }}
table {{ border-collapse: collapse; width: 100%; margin: 8px 0 18px; }}
td, th {{ border: 1px solid #d7dde5; padding: 8px; vertical-align: top; font-size: 13px; }}
th {{ background: #f4f6f8; text-align: left; }}
.summary {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }}
.metric {{ border: 1px solid #d7dde5; padding: 10px 12px; border-radius: 6px; background: #fff; }}
.preview {{ width: 160px; height: 80px; display: flex; align-items: center; justify-content: center; background: #f7f8fa; }}
.preview img {{ max-width: 150px; max-height: 70px; object-fit: contain; }}
.url {{ overflow-wrap: anywhere; }}
code {{ font-size: 12px; }}
</style>
<h1>Academic Identity Logo Curation</h1>
<div class="summary">
  <div class="metric"><strong>Target set</strong><br>{_e(TARGET_SET_ID)}</div>
  <div class="metric"><strong>Targets</strong><br>{len(target_rules)}</div>
  <div class="metric"><strong>Accepted</strong><br>{len(accepted)}</div>
  <div class="metric"><strong>Rejected / unresolved</strong><br>{len(rejected)}</div>
  <div class="metric"><strong>Agent</strong><br>{_e(agent_result.get('status'))}</div>
  <div class="metric"><strong>Resolver status</strong><br>{_e(metrics.get('identity_search_status_counts'))}</div>
</div>
<h2>Accepted Candidates</h2>
<table><tr><th>Preview</th><th>Entity</th><th>Role</th><th>Method</th><th>Source URL</th><th>Discovered From</th></tr>
{accepted_rows or '<tr><td colspan="6">No accepted logo assets.</td></tr>'}</table>
<h2>Rejected / Unresolved Candidates</h2>
<table><tr><th>Entity</th><th>Role</th><th>Status</th><th>Reason</th><th>URL</th></tr>
{rejected_rows or '<tr><td colspan="5">No rejected candidates.</td></tr>'}</table>
<h2>Targets</h2>
<table><tr><th>Entity</th><th>Role</th><th>Official Domains</th><th>Preferred Pages</th></tr>
{target_rows}</table>
<h2>Artifacts</h2>
<p><code>accepted_candidates.json</code>, <code>rejected_candidates.json</code>,
<code>proposed_allowlist.json</code>, and <code>allowlist.patch</code> are draft review artifacts.
The tracked allowlist was not modified.</p>
"""
    report_path.write_text(report, encoding="utf-8")


def _accepted_row(item: dict[str, Any], report_dir: Path) -> str:
    local = str(item.get("local_asset_path") or "")
    preview = ""
    if local:
        path = Path(local)
        try:
            rel = path.relative_to(report_dir)
            preview = f'<div class="preview"><img src="{html.escape(rel.as_posix())}"></div>'
        except Exception:
            preview = '<div class="preview">local asset</div>'
    return (
        "<tr>"
        f"<td>{preview}</td><td>{_e(item.get('entity_name'))}</td><td>{_e(item.get('role'))}</td>"
        f"<td>{_e(item.get('discovery_method'))}</td><td>{_url(item.get('source_url'))}</td>"
        f"<td>{_url(item.get('discovered_from_url'))}</td></tr>"
    )


def _host_in_domains(host: str, domains: list[Any]) -> bool:
    cleaned = host.lower().strip(".")
    for domain in domains:
        dom = str(domain or "").lower().strip(".")
        if dom and (cleaned == dom or cleaned.endswith("." + dom)):
            return True
    return False


def _entity_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _slug(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug or "identity"


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return f'<span class="url">{html.escape(text)}</span>'


if __name__ == "__main__":
    raise SystemExit(main())
