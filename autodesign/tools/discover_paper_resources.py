"""Discover external resources for paper project pages.

The page pipeline needs more than PDF text: project pages normally expose
code, arXiv/PDF, demos, model weights, Hugging Face assets, blogs, and
sometimes hardware/API docs. This tool keeps discovery conservative:
source-extracted URLs rank first, public API search results are labeled by
source, and failures degrade to an empty manifest instead of fabricated links.
"""

from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from ._contract import ToolContext, obs_ok
from ..util.io import atomic_write_json
from ..util.logging import log


_USER_AGENT = "AutoDesign-paper-resource-search/1.0"
_URL_RE = re.compile(
    r"(?:(?:https?://|www\.)[^\s<>'\"\]\)]+|"
    r"(?:github\.com|huggingface\.co|arxiv\.org|x\.com|twitter\.com)/[^\s<>'\"\]\)]+)",
    re.IGNORECASE,
)
_ARXIV_ID_RE = re.compile(
    r"\barxiv\s*(?:\:|id\s*)?\s*([0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?)",
    re.IGNORECASE,
)
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9-]{2,}\b")
_TRAILING = ".,;:!?)]}>"
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "is", "of", "on", "or", "the", "to", "with", "via",
    "learning", "model", "models", "paper", "towards", "using",
}
_RESOURCE_TYPE_PRIORITY = {
    "project": 1,
    "arxiv": 2,
    "pdf": 3,
    "github": 4,
    "huggingface_model": 5,
    "huggingface_dataset": 6,
    "huggingface_space": 7,
    "demo": 8,
    "blog": 9,
    "hardware": 10,
    "twitter": 11,
    "weights": 12,
    "resource": 20,
}
_THIRD_PARTY_SEARCH_THRESHOLD = 0.9


def discover_paper_resources(args: dict[str, Any], *, ctx: ToolContext):
    """Tool entry point exposed to the designer."""

    max_results = _int_arg(args.get("max_results"), 12)
    search_web = bool(args.get("search_web", True))
    manifest = discover_paper_resources_for_context(
        ctx,
        title=str(args.get("title") or "").strip() or None,
        authors=list(args.get("authors") or []) if isinstance(args.get("authors"), list) else None,
        max_results=max_results,
        search_web=search_web,
    )
    return obs_ok(_compact_for_tool(manifest))


def discover_paper_resources_for_context(
    ctx: ToolContext,
    *,
    title: str | None = None,
    authors: list[Any] | None = None,
    max_results: int = 12,
    search_web: bool = True,
) -> dict[str, Any]:
    """Build and persist a resource manifest from ctx.state paper ingest."""

    summaries = _paper_summaries(ctx)
    manifest_title, manifest_authors = _title_authors_from_summaries(summaries)
    title = title or manifest_title
    authors = authors or manifest_authors
    raw_text = "\n".join(str(s.get("raw_text") or "") for s in summaries if isinstance(s, dict))
    context_text = "\n".join([
        title or "",
        " ".join(str(a) for a in (authors or [])),
        raw_text[:120_000],
    ])

    warnings: list[str] = []
    resources: list[dict[str, Any]] = []
    resources.extend(_resources_from_text(context_text))
    resources.extend(_pdf_companions_for_arxiv(resources))

    if search_web and _web_search_enabled():
        queries = _query_terms(title or "", authors or [], context_text)
        if not any(r.get("type") == "arxiv" and r.get("source") == "paper_text" for r in resources):
            resources.extend(_search_arxiv(title or "", warnings=warnings))
        paper_text_types = {
            str(r.get("type") or "")
            for r in resources
            if str(r.get("source") or "") == "paper_text"
        }
        if "github" not in paper_text_types:
            resources.extend(_search_github(queries, title or "", warnings=warnings))
        if not any(t.startswith("huggingface_") for t in paper_text_types):
            resources.extend(_search_huggingface(queries, title or "", warnings=warnings))

    resources = _dedupe_and_rank(resources, max_results=max_results)
    resource_chips = [_resource_chip(r) for r in resources if r.get("href")]
    recall_audit = _resource_recall_audit(
        resources,
        resource_chips,
        queries=_query_terms(title or "", authors or [], context_text)[:8],
        warnings=warnings,
    )
    result = {
        "kind": "paper_resource_manifest",
        "version": 1,
        "title": title,
        "authors": [str(a) for a in (authors or [])][:20],
        "resource_count": len(resources),
        "resources": resources,
        "resource_chips": resource_chips,
        "queries": recall_audit.get("queries") or [],
        "resource_recall_audit": recall_audit,
        "warnings": warnings[:12],
        "generated_at": int(time.time()),
    }
    ctx.state["paper_resources"] = result
    ctx.state["paper_resource_recall_audit"] = recall_audit
    try:
        atomic_write_json(ctx.run_dir / "paper_resource_manifest.json", result)
        atomic_write_json(ctx.run_dir / "paper_resource_recall_audit.json", recall_audit)
    except OSError:
        pass
    log(
        "paper_resources.discovered",
        count=len(resources),
        chips=len(resource_chips),
        title=(title or "")[:120],
        warnings=len(warnings),
    )
    return result


def _resource_recall_audit(
    resources: list[dict[str, Any]],
    resource_chips: list[dict[str, Any]],
    *,
    queries: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for item in resources:
        typ = str(item.get("type") or "resource")
        src = str(item.get("source") or "unknown")
        by_type[typ] = by_type.get(typ, 0) + 1
        by_source[src] = by_source.get(src, 0) + 1
    core_types = {"arxiv", "pdf", "github", "huggingface_model", "huggingface_dataset", "huggingface_space", "project"}
    present_core = {typ for typ in by_type if typ in core_types}
    return {
        "kind": "paper_resource_recall_audit",
        "version": 1,
        "resource_count": len(resources),
        "resource_chip_count": len(resource_chips),
        "queries": queries,
        "coverage_by_type": by_type,
        "coverage_by_source": by_source,
        "paper_text_url_count": by_source.get("paper_text", 0),
        "searched_external_sources": any(source in by_source for source in ("arxiv_api", "github_api", "huggingface_api")),
        "missing_core_resource_types": sorted(core_types - present_core),
        "verified_renderable_resource_count": len([
            item for item in resources
            if item.get("href") and float(item.get("confidence") or 0) >= 0.78
        ]),
        "warnings": warnings[:12],
    }


def should_auto_discover_paper_resources(ctx: ToolContext) -> bool:
    raw = os.getenv("PAPER_RESOURCE_SEARCH", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    brief = " ".join(str(ctx.state.get(k) or "") for k in ("raw_user_brief", "run_brief")).lower()
    return any(marker in brief for marker in (
        "paper project page",
        "paper page",
        "paper-to-page",
        "paper to page",
        "project page",
        "website",
        "web page",
        "webpage",
        "网页",
        "项目页",
        "论文页面",
    ))


def _paper_summaries(ctx: ToolContext) -> list[dict[str, Any]]:
    ingested = ctx.state.get("ingested")
    if not isinstance(ingested, list):
        return []
    return [s for s in ingested if isinstance(s, dict) and s.get("type") == "pdf"]


def _title_authors_from_summaries(summaries: list[dict[str, Any]]) -> tuple[str | None, list[Any]]:
    for summary in summaries:
        manifest = summary.get("manifest") if isinstance(summary.get("manifest"), dict) else {}
        title = str(manifest.get("title") or "").strip()
        authors = manifest.get("authors") if isinstance(manifest.get("authors"), list) else []
        if title:
            return title, authors
    return None, []


def _resources_from_text(text: str) -> list[dict[str, Any]]:
    text = _repair_pdf_url_whitespace(text or "")
    resources: list[dict[str, Any]] = []
    for match in _URL_RE.finditer(text or ""):
        href = _normalize_url(match.group(0))
        if not href:
            continue
        resources.append(_resource_from_href(
            href,
            source="paper_text",
            confidence=0.95,
            evidence=_line_excerpt(text, match.start()),
        ))
    for match in _ARXIV_ID_RE.finditer(text or ""):
        arxiv_id = match.group(1).rstrip(".")
        resources.append(_resource_from_href(
            f"https://arxiv.org/abs/{arxiv_id}",
            source="paper_text",
            confidence=0.95,
            evidence=_line_excerpt(text, match.start()),
        ))
    return resources


def _repair_pdf_url_whitespace(text: str) -> str:
    text = re.sub(
        r"((?:https?://)?(?:github\.com|huggingface\.co|arxiv\.org)/)\s+",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"((?:https?://)?(?:github\.com|huggingface\.co)/[^/\s]+)/\s+",
        r"\1/",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _resource_from_href(
    href: str,
    *,
    source: str,
    confidence: float,
    title: str | None = None,
    evidence: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kind = _resource_type(href, title or "")
    return {
        "type": kind,
        "label": _label_for_type(kind),
        "href": href,
        "title": title or _label_for_type(kind),
        "icon": _icon_for_type(kind),
        "source": source,
        "confidence": round(float(confidence), 3),
        "evidence": evidence or "",
        "metadata": metadata or {},
    }


def _normalize_url(value: str) -> str:
    raw = (value or "").strip().strip(_TRAILING)
    if not raw:
        return ""
    if raw.startswith("www."):
        raw = "https://" + raw
    if raw.lower().startswith(("github.com/", "huggingface.co/", "arxiv.org/", "x.com/", "twitter.com/")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if parsed.netloc.lower() in {"github.com", "huggingface.co", "arxiv.org"}:
        useful_path = parsed.path.strip("/")
        if not useful_path:
            return ""
    return raw


def _resource_type(href: str, title: str) -> str:
    parsed = urlparse(href)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    blob = f"{href} {title}".lower()
    if "hardware" in blob or "interface" in blob or "sdk" in blob or "device" in blob:
        return "hardware"
    if host == "github.com":
        return "github"
    if host.endswith("github.io"):
        return "project"
    if host == "huggingface.co":
        if path.startswith("/spaces/"):
            return "huggingface_space"
        if path.startswith("/datasets/"):
            return "huggingface_dataset"
        return "huggingface_model"
    if host == "arxiv.org":
        return "pdf" if path.startswith("/pdf/") else "arxiv"
    if host in {"x.com", "twitter.com"}:
        return "twitter"
    if any(key in blob for key in ("demo", "gradio", "space", "colab")):
        return "demo"
    if any(key in blob for key in ("weight", "checkpoint", "ckpt", "model-card")):
        return "weights"
    if any(key in host for key in ("medium.com", "blog", "openai.com")) or "/blog" in path:
        return "blog"
    return "resource"


def _label_for_type(kind: str) -> str:
    return {
        "project": "Project",
        "arxiv": "arXiv",
        "pdf": "PDF",
        "github": "GitHub",
        "huggingface_model": "Hugging Face",
        "huggingface_dataset": "HF Dataset",
        "huggingface_space": "Demo",
        "demo": "Demo",
        "blog": "Blog",
        "hardware": "Hardware",
        "twitter": "X/Twitter",
        "weights": "Weights",
    }.get(kind, "Link")


def _icon_for_type(kind: str) -> str:
    return {
        "project": "Web",
        "arxiv": "arXiv",
        "pdf": "PDF",
        "github": "GH",
        "huggingface_model": "HF",
        "huggingface_dataset": "HF",
        "huggingface_space": "HF",
        "demo": "Demo",
        "blog": "Blog",
        "hardware": "HW",
        "twitter": "X",
        "weights": "Wts",
    }.get(kind, "Link")


def _resource_chip(resource: dict[str, Any]) -> dict[str, Any]:
    kind = str(resource.get("type") or "resource")
    return {
        "kind": "text",
        "role": "cta",
        "text": str(resource.get("label") or _label_for_type(kind)),
        "href": str(resource.get("href") or ""),
        "title": str(resource.get("title") or ""),
        "resource_type": kind,
        "icon": str(resource.get("icon") or _icon_for_type(kind)),
        "source": str(resource.get("source") or ""),
        "confidence": resource.get("confidence"),
    }


def _query_terms(title: str, authors: list[Any], context_text: str) -> list[str]:
    terms: list[str] = []
    clean_title = _clean_title(title)
    if clean_title:
        terms.append(clean_title)
    acronyms = _acronyms(context_text)
    for acronym in acronyms[:4]:
        if acronym.lower() not in clean_title.lower():
            terms.append(acronym)
    if clean_title and authors:
        first_author = str(authors[0]).split()[-1] if str(authors[0]).strip() else ""
        if first_author:
            terms.append(f"{clean_title} {first_author}")
    return _unique_preserve_order(terms)[:8]


def _clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip(" .\n\t"))


def _acronyms(text: str) -> list[str]:
    bad = {"PDF", "HTML", "URL", "API", "GPU", "CPU", "RGB", "NLP", "LLM"}
    seen: dict[str, int] = {}
    for value in _ACRONYM_RE.findall(text or ""):
        if value in bad or len(value) > 24:
            continue
        seen[value] = seen.get(value, 0) + 1
    return [k for k, _ in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))]


def _search_github(queries: list[str], title: str, *, warnings: list[str]) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    for query in queries[:5]:
        url = "https://api.github.com/search/repositories?" + urlencode({
            "q": f"{query} paper",
            "per_page": "5",
        })
        data = _fetch_json(url, warnings=warnings, source="github")
        if not isinstance(data, dict):
            continue
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            href = str(item.get("html_url") or "")
            if not href:
                continue
            score = _repo_score(item, title, query)
            if score < _THIRD_PARTY_SEARCH_THRESHOLD:
                continue
            if not _allow_third_party_search() and not _github_search_is_official(item, title):
                continue
            resources.append(_resource_from_href(
                href,
                source="github_search",
                confidence=min(0.88, score),
                title=str(item.get("full_name") or item.get("name") or "GitHub"),
                metadata={
                    "stars": item.get("stargazers_count"),
                    "description": item.get("description"),
                    "query": query,
                },
            ))
    return resources


def _search_huggingface(queries: list[str], title: str, *, warnings: list[str]) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    endpoints = (
        ("models", "https://huggingface.co/api/models"),
        ("datasets", "https://huggingface.co/api/datasets"),
        ("spaces", "https://huggingface.co/api/spaces"),
    )
    for query in queries[:5]:
        for group, base in endpoints:
            url = base + "?" + urlencode({"search": query, "limit": "5"})
            data = _fetch_json(url, warnings=warnings, source=f"huggingface_{group}")
            if not isinstance(data, list):
                continue
            for item in data:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id") or "")
                if not item_id:
                    continue
                score = _hf_score(item, title, query)
                if score < _THIRD_PARTY_SEARCH_THRESHOLD:
                    continue
                if not _allow_third_party_search() and not _hf_search_is_official(item, title):
                    continue
                prefix = {"models": "", "datasets": "datasets/", "spaces": "spaces/"}[group]
                resources.append(_resource_from_href(
                    f"https://huggingface.co/{prefix}{item_id}",
                    source=f"huggingface_{group}_search",
                    confidence=min(0.88, score),
                    title=item_id,
                    metadata={
                        "likes": item.get("likes"),
                        "downloads": item.get("downloads"),
                        "tags": list(item.get("tags") or [])[:10],
                        "query": query,
                    },
                ))
    return resources


def _search_arxiv(title: str, *, warnings: list[str]) -> list[dict[str, Any]]:
    clean = _clean_title(title)
    if not clean:
        return []
    url = "https://export.arxiv.org/api/query?" + urlencode({
        "search_query": f'ti:"{clean}"',
        "start": "0",
        "max_results": "3",
    })
    raw = _fetch_text(url, warnings=warnings, source="arxiv")
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        warnings.append(f"arxiv_parse_failed:{exc}")
        return []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    out: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns):
        found_title = " ".join((entry.findtext("atom:title", default="", namespaces=ns) or "").split())
        entry_id = str(entry.findtext("atom:id", default="", namespaces=ns) or "")
        if not entry_id:
            continue
        if _token_overlap(found_title, clean) < 0.82:
            continue
        out.append(_resource_from_href(
            entry_id,
            source="arxiv_search",
            confidence=0.82,
            title=found_title or "arXiv",
        ))
        arxiv_id = entry_id.rstrip("/").split("/")[-1]
        if arxiv_id:
            out.append(_resource_from_href(
                f"https://arxiv.org/pdf/{arxiv_id}",
                source="arxiv_search",
                confidence=0.78,
                title=f"{found_title or 'Paper'} PDF",
            ))
    return out


def _repo_score(item: dict[str, Any], title: str, query: str) -> float:
    text = " ".join(str(item.get(k) or "") for k in ("full_name", "name", "description"))
    topics = item.get("topics") if isinstance(item.get("topics"), list) else []
    text = f"{text} {' '.join(str(t) for t in topics)}"
    overlap = max(_token_overlap(text, title), _token_overlap(text, query))
    stars = int(item.get("stargazers_count") or 0)
    star_bonus = min(0.16, stars / 5000)
    exact_bonus = 0.12 if query.lower() in text.lower() else 0.0
    return min(1.0, overlap + star_bonus + exact_bonus)


def _hf_score(item: dict[str, Any], title: str, query: str) -> float:
    text = " ".join([
        str(item.get("id") or ""),
        " ".join(str(t) for t in (item.get("tags") or [])[:20]),
        str(item.get("pipeline_tag") or ""),
        str(item.get("library_name") or ""),
    ])
    overlap = max(_token_overlap(text, title), _token_overlap(text, query))
    popularity = min(0.12, (int(item.get("likes") or 0) + int(item.get("downloads") or 0) / 100000) / 250)
    exact_bonus = 0.18 if query.lower() in text.lower() else 0.0
    return min(1.0, overlap + popularity + exact_bonus)


def _github_search_is_official(item: dict[str, Any], title: str) -> bool:
    clean_title = _clean_title(title).lower()
    if not clean_title:
        return False
    text = " ".join(str(item.get(k) or "") for k in ("full_name", "name", "description")).lower()
    return "official" in text and clean_title in text


def _hf_search_is_official(item: dict[str, Any], title: str) -> bool:
    clean_title = _clean_title(title).lower()
    if not clean_title:
        return False
    text = " ".join([
        str(item.get("id") or ""),
        " ".join(str(t) for t in (item.get("tags") or [])[:20]),
    ]).lower()
    return "official" in text and clean_title in text


def _allow_third_party_search() -> bool:
    raw = os.getenv("PAPER_RESOURCE_ALLOW_THIRD_PARTY", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _token_overlap(a: str, b: str) -> float:
    left = set(_tokens(a))
    right = set(_tokens(b))
    if not left or not right:
        return 0.0
    return len(left & right) / max(3, min(len(left), len(right)))


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9-]{2,}", (text or "").lower()):
        if token in _STOPWORDS:
            continue
        out.append(token)
    return out


def _dedupe_and_rank(resources: list[dict[str, Any]], *, max_results: int) -> list[dict[str, Any]]:
    by_href: dict[str, dict[str, Any]] = {}
    for item in resources:
        href = str(item.get("href") or "").strip()
        if not href:
            continue
        key = _canonical_href(href)
        current = by_href.get(key)
        if current is None or _resource_rank(item) < _resource_rank(current):
            by_href[key] = item
    ranked = sorted(by_href.values(), key=_resource_rank)
    return ranked[:max(1, max_results)]


def _pdf_companions_for_arxiv(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    companions: list[dict[str, Any]] = []
    for item in resources:
        if item.get("type") != "arxiv":
            continue
        href = str(item.get("href") or "")
        parsed = urlparse(href)
        if parsed.netloc.lower() != "arxiv.org" or not parsed.path.startswith("/abs/"):
            continue
        arxiv_id = parsed.path.rsplit("/", 1)[-1]
        if not arxiv_id:
            continue
        companions.append(_resource_from_href(
            f"https://arxiv.org/pdf/{arxiv_id}",
            source=str(item.get("source") or "paper_text"),
            confidence=min(float(item.get("confidence") or 0.9), 0.9),
            title="Paper PDF",
            evidence=str(item.get("evidence") or ""),
        ))
    return companions


def _resource_rank(item: dict[str, Any]) -> tuple[int, float, str]:
    source = str(item.get("source") or "")
    source_rank = 0 if source == "paper_text" else 1
    type_rank = _RESOURCE_TYPE_PRIORITY.get(str(item.get("type") or ""), 20)
    confidence = float(item.get("confidence") or 0)
    return (source_rank * 100 + type_rank, -confidence, str(item.get("href") or ""))


def _canonical_href(href: str) -> str:
    parsed = urlparse(href)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{host}{path}"


def _fetch_json(url: str, *, warnings: list[str], source: str) -> Any:
    raw = _fetch_text(url, warnings=warnings, source=source)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        warnings.append(f"{source}_json_failed:{exc}")
        return None


def _fetch_text(url: str, *, warnings: list[str], source: str) -> str:
    timeout = float(os.getenv("PAPER_RESOURCE_SEARCH_TIMEOUT_SECONDS", "8") or 8)
    try:
        req = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json, text/xml;q=0.9, */*;q=0.8"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read(750_000).decode("utf-8", "replace")
    except HTTPError as exc:
        warnings.append(f"{source}_http_{exc.code}")
    except (URLError, TimeoutError, OSError) as exc:
        warnings.append(f"{source}_fetch_failed:{type(exc).__name__}")
    return ""


def _web_search_enabled() -> bool:
    raw = os.getenv("PAPER_RESOURCE_SEARCH", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _compact_for_tool(manifest: dict[str, Any]) -> dict[str, Any]:
    resources = [
        {
            "type": r.get("type"),
            "label": r.get("label"),
            "href": r.get("href"),
            "title": r.get("title"),
            "icon": r.get("icon"),
            "source": r.get("source"),
            "confidence": r.get("confidence"),
            "evidence": r.get("evidence"),
        }
        for r in manifest.get("resources") or []
    ]
    return {
        "resource_count": manifest.get("resource_count"),
        "resources": resources,
        "resource_chips": manifest.get("resource_chips") or [],
        "resource_recall_audit": manifest.get("resource_recall_audit") or {},
        "manifest_path": "paper_resource_manifest.json",
        "recall_audit_path": "paper_resource_recall_audit.json",
        "warnings": manifest.get("warnings") or [],
    }


def _line_excerpt(text: str, pos: int, *, radius: int = 160) -> str:
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(value.strip())
    return out


def _int_arg(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default
