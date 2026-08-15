"""Fast dogfood harness for academic identity logo discovery.

This intentionally stops before poster planning/rendering. It extracts a small
paper identity context, resolves deterministic allowlist logos, optionally runs
IdentityLogoAgent for missing logos, and writes a contact-sheet report.
"""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import shutil
import sys
import urllib.request
from dataclasses import replace
from datetime import datetime
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
from autodesign.util.academic_identity import build_academic_identity_assets  # noqa: E402
from autodesign.util.academic_identity_search import resolve_academic_identity_assets  # noqa: E402
from autodesign.util.io import atomic_write_json  # noqa: E402


_VENUE_RE = re.compile(
    r"\b(NeurIPS|NIPS|ICML|ICLR|CVPR|ICCV|ECCV|ACL|EMNLP|AAAI|KDD|CHI|SIGGRAPH)(?:\s+20\d{2})?\b",
    flags=re.IGNORECASE,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run only paper identity extraction + logo discovery, then write an HTML logo report.",
    )
    parser.add_argument("sources", nargs="+", help="Local PDF/DOCX/TXT/MD path or HTTPS URL.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory. Default: out/identity_logo_dogfood/<timestamp>.",
    )
    parser.add_argument("--max-pages", type=int, default=2, help="PDF pages to read for identity text.")
    parser.add_argument(
        "--identity-logo-agent",
        choices=("auto", "off", "required"),
        default=None,
        help="Override DESIGN_ANYTHING_IDENTITY_LOGO_AGENT for this dogfood run.",
    )
    parser.add_argument(
        "--identity-logo-agent-harness",
        choices=("custom", "codex", "claude", "deepseek", "opencode", "kimi", "mimo", "zcode"),
        default=None,
        help="Coding-agent harness used for missing logos.",
    )
    parser.add_argument("--identity-logo-agent-cmd", default=None, help="Explicit custom agent command.")
    parser.add_argument("--identity-logo-agent-model", default=None, help="Optional agent model hint.")
    parser.add_argument("--identity-logo-agent-timeout", type=int, default=None, help="Agent timeout in seconds.")
    parser.add_argument("--max-entities", type=int, default=None, help="Max entities passed to the agent.")
    parser.add_argument("--max-candidates", type=int, default=None, help="Max candidate URLs materialized.")
    args = parser.parse_args(argv)

    out_dir = args.out_dir or (
        REPO_ROOT / "out" / "identity_logo_dogfood" / datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    layers_dir = out_dir / "layers"
    layers_dir.mkdir(parents=True, exist_ok=True)

    settings = _settings_from_args(args)
    ctx = ToolContext(settings=settings, run_dir=out_dir, layers_dir=layers_dir, run_id=out_dir.name)
    ctx.state["raw_user_brief"] = "Academic paper poster identity-logo dogfood run."

    rendered_layers: dict[str, dict[str, Any]] = {}
    summaries: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []
    source_dir = out_dir / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    for index, source in enumerate(args.sources, start=1):
        local_path = _materialize_source(source, source_dir, index=index)
        text = _extract_text(local_path, max_pages=max(1, int(args.max_pages or 1)))
        manifest = _manifest_from_text(text, source_name=local_path.name)
        summaries.append({
            "source_file": str(local_path),
            "manifest": manifest,
            "raw_text": text,
        })
        source_records.append({
            "input": source,
            "local_path": str(local_path),
            "title": manifest.get("title"),
            "venue": manifest.get("venue"),
            "affiliations": manifest.get("affiliations"),
            "text_chars": len(text),
        })

    identity_brief = "\n".join(
        " ".join(str(item.get(key) or "") for key in ("title", "venue")).strip()
        for item in source_records
    )
    identity_assets = build_academic_identity_assets(
        summaries=summaries,
        rendered_layers=rendered_layers,
        brief=identity_brief,
    )
    if identity_assets:
        identity_assets = resolve_academic_identity_assets(
            identity_assets=identity_assets,
            rendered_layers=rendered_layers,
            run_dir=out_dir,
            layers_dir=layers_dir,
            brief=identity_brief,
        )
        identity_assets, agent_result = IdentityLogoAgent(settings).run(
            ctx=ctx,
            identity_assets=identity_assets,
        )
    else:
        agent_result = {"status": "skipped_no_identity_entities", "resolver": "identity_logo_agent"}

    ctx.state["rendered_layers"] = rendered_layers
    ctx.state["academic_identity_assets"] = identity_assets
    atomic_write_json(out_dir / "sources.json", {"sources": source_records})
    atomic_write_json(out_dir / "academic_identity_assets.json", identity_assets)
    atomic_write_json(out_dir / "rendered_layers.json", rendered_layers)
    atomic_write_json(out_dir / "identity_logo_agent_result.json", agent_result)
    report_path = _write_report(out_dir, source_records, identity_assets, rendered_layers, agent_result)

    print(f"identity_logo_dogfood: report={report_path}")
    print(f"identity_logo_dogfood: identity_assets={out_dir / 'academic_identity_assets.json'}")
    return 0


def _settings_from_args(args: argparse.Namespace) -> Settings:
    try:
        settings = load_settings()
    except RuntimeError:
        settings = Settings(
            anthropic_api_key="identity-dogfood-stub",
            anthropic_base_url=None,
            gemini_api_key="",
            designer_model="identity-dogfood-stub",
            critic_model="identity-dogfood-stub",
            identity_logo_agent_mode=os.getenv("DESIGN_ANYTHING_IDENTITY_LOGO_AGENT", "auto").strip() or "auto",
            identity_logo_agent_harness=os.getenv(
                "DESIGN_ANYTHING_IDENTITY_LOGO_AGENT_HARNESS",
                "codex",
            ).strip() or "codex",
            identity_logo_agent_model=os.getenv("DESIGN_ANYTHING_IDENTITY_LOGO_AGENT_MODEL", "").strip() or None,
            identity_logo_agent_cmd=os.getenv("DESIGN_ANYTHING_IDENTITY_LOGO_AGENT_CMD", "").strip(),
        )
    updates: dict[str, Any] = {}
    if args.identity_logo_agent is not None:
        updates["identity_logo_agent_mode"] = args.identity_logo_agent
    if args.identity_logo_agent_harness is not None:
        updates["identity_logo_agent_harness"] = args.identity_logo_agent_harness
    if args.identity_logo_agent_model is not None:
        updates["identity_logo_agent_model"] = args.identity_logo_agent_model or None
    if args.identity_logo_agent_timeout is not None:
        updates["identity_logo_agent_timeout_s"] = args.identity_logo_agent_timeout
    if args.max_entities is not None:
        updates["identity_logo_agent_max_entities"] = args.max_entities
    if args.max_candidates is not None:
        updates["identity_logo_agent_max_candidates"] = args.max_candidates
    if args.identity_logo_agent_cmd is not None:
        updates["identity_logo_agent_cmd"] = args.identity_logo_agent_cmd
    settings = replace(settings, **updates) if updates else settings
    if (
        args.identity_logo_agent_cmd is None
        and settings.identity_logo_agent_harness != "custom"
        and (
            not settings.identity_logo_agent_cmd
            or args.identity_logo_agent_harness is not None
            or args.identity_logo_agent_model is not None
        )
    ):
        settings = replace(
            settings,
            identity_logo_agent_cmd=identity_logo_agent_command_for_harness(
                settings.identity_logo_agent_harness,
                settings.identity_logo_agent_model,
            ),
        )
    return settings


def _materialize_source(source: str, source_dir: Path, *, index: int) -> Path:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        req = urllib.request.Request(source, headers={"User-Agent": "AutoDesignIdentityLogoDogfood/1.0"})
        with urllib.request.urlopen(req, timeout=45) as response:  # noqa: S310 - user-requested dogfood URL fetch.
            data = response.read()
            content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip()
        suffix = Path(parsed.path).suffix.lower()
        if suffix not in {".pdf", ".docx", ".txt", ".md"}:
            suffix = mimetypes.guess_extension(content_type) or ".pdf"
        dest = source_dir / f"source_{index:02d}{suffix}"
        dest.write_bytes(data)
        return dest
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    dest = source_dir / path.name
    if path.resolve() != dest.resolve():
        shutil.copy2(path, dest)
    return dest


def _extract_text(path: Path, *, max_pages: int) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import fitz  # pymupdf

        chunks: list[str] = []
        with fitz.open(path) as doc:
            for page_index in range(min(max_pages, doc.page_count)):
                chunks.append(doc.load_page(page_index).get_text("text"))
        return "\n".join(chunks)
    if suffix == ".docx":
        from docx import Document

        doc = Document(str(path))
        return "\n".join(paragraph.text for paragraph in doc.paragraphs)
    return path.read_text(encoding="utf-8", errors="ignore")


def _manifest_from_text(text: str, *, source_name: str) -> dict[str, Any]:
    lines = [_clean_line(line) for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    title = _guess_title(lines) or Path(source_name).stem
    venue = _guess_venue(text)
    affiliations = _guess_affiliation_lines(lines)
    return {
        "title": title,
        "venue": venue,
        "affiliations": affiliations,
    }


def _guess_title(lines: list[str]) -> str:
    for index, line in enumerate(lines[:30]):
        lower = line.lower()
        if len(line) < 12 or "arxiv" in lower or "abstract" == lower:
            continue
        if "@" in line or re.search(r"\b(university|institute|research|lab|department)\b", lower):
            continue
        title_lines = [line]
        for next_line in lines[index + 1:index + 3]:
            if _looks_like_author_or_affiliation(next_line):
                break
            next_lower = next_line.lower()
            if len(next_line) < 8 or next_lower in {"abstract", "introduction"} or "@" in next_line:
                break
            title_lines.append(next_line)
        return " ".join(title_lines)[:220]
    return ""


def _looks_like_author_or_affiliation(line: str) -> bool:
    lower = str(line or "").lower()
    if re.search(r"\b(university|institute|school|college|laboratory|lab|research|department)\b", lower):
        return True
    if _looks_like_numbered_affiliation_line(line):
        return True
    if re.search(r"\b[A-Z]\.\s+[A-Z][a-z]+", str(line or "")):
        return True
    if re.search(r"\d\s*[⋆*†]?$", str(line or "")):
        return True
    return False


def _looks_like_numbered_affiliation_line(line: str) -> bool:
    match = re.match(r"^\d+\s*(.+)$", str(line or "").strip())
    if not match:
        return False
    label = match.group(1).strip()
    if not (3 <= len(label) <= 90):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z&.-]*", label)
    if not words or len(words) > 8:
        return False
    return all(word[:1].isupper() or word.isupper() for word in words)


def _guess_venue(text: str) -> str:
    match = _VENUE_RE.search(text or "")
    return match.group(0).strip() if match else ""


def _guess_affiliation_lines(lines: list[str]) -> list[str]:
    kept: list[str] = []
    for line in lines[:90]:
        lower = line.lower()
        if any(token in lower for token in (
            "university",
            "institute",
            "school",
            "college",
            "laboratory",
            "lab",
            "research",
            "department",
        )) or _looks_like_numbered_affiliation_line(line):
            kept.append(line[:300])
        if len(kept) >= 8:
            break
    return kept


def _clean_line(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _write_report(
    out_dir: Path,
    sources: list[dict[str, Any]],
    identity_assets: dict[str, Any],
    rendered_layers: dict[str, dict[str, Any]],
    agent_result: dict[str, Any],
) -> Path:
    assets = [item for item in (identity_assets.get("assets") or []) if isinstance(item, dict)]
    entities = [item for item in (identity_assets.get("entities") or []) if isinstance(item, dict)]
    search = identity_assets.get("search") if isinstance(identity_assets.get("search"), dict) else {}
    agent = identity_assets.get("identity_logo_agent") if isinstance(identity_assets.get("identity_logo_agent"), dict) else agent_result
    cards = "\n".join(_asset_card(asset, rendered_layers, out_dir) for asset in assets)
    entity_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(entity.get('entity_name') or ''))}</td>"
        f"<td>{html.escape(str(entity.get('role') or ''))}</td>"
        f"<td>{html.escape(str(entity.get('source') or ''))}</td>"
        f"<td>{html.escape(str(entity.get('placement_intent') or ''))}</td>"
        "</tr>"
        for entity in entities
    )
    source_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(source.get('local_path') or source.get('input') or ''))}</td>"
        f"<td>{html.escape(str(source.get('title') or ''))}</td>"
        f"<td>{html.escape(str(source.get('venue') or ''))}</td>"
        f"<td>{html.escape(', '.join(str(x) for x in source.get('affiliations') or []))}</td>"
        "</tr>"
        for source in sources
    )
    report = f"""<!doctype html>
<meta charset="utf-8">
<title>Identity Logo Dogfood Report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 24px; color: #17202a; }}
h1 {{ font-size: 24px; margin: 0 0 16px; }}
h2 {{ font-size: 18px; margin: 24px 0 10px; }}
table {{ border-collapse: collapse; width: 100%; margin: 8px 0 18px; }}
td, th {{ border: 1px solid #d7dde5; padding: 8px; vertical-align: top; font-size: 13px; }}
th {{ background: #f4f6f8; text-align: left; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }}
.card {{ border: 1px solid #d7dde5; border-radius: 8px; padding: 12px; background: #fff; }}
.preview {{ height: 120px; display: flex; align-items: center; justify-content: center; background: #f7f8fa; border: 1px solid #e4e7ec; margin-bottom: 10px; }}
.preview img {{ max-width: 220px; max-height: 100px; object-fit: contain; }}
.badge {{ display: inline-block; padding: 8px 10px; border: 1px solid #aab3c2; border-radius: 4px; font-weight: 600; background: #fff; }}
.meta {{ font-size: 12px; color: #526071; overflow-wrap: anywhere; }}
code {{ font-size: 12px; }}
</style>
<h1>Identity Logo Dogfood Report</h1>
<p class="meta">Output: <code>{html.escape(str(out_dir))}</code></p>
<h2>Sources</h2>
<table><thead><tr><th>Local path</th><th>Title</th><th>Venue</th><th>Affiliation-like lines</th></tr></thead><tbody>{source_rows}</tbody></table>
<h2>Entities</h2>
<table><thead><tr><th>Name</th><th>Role</th><th>Source</th><th>Placement</th></tr></thead><tbody>{entity_rows}</tbody></table>
<h2>Assets</h2>
<div class="grid">{cards or '<p>No identity assets found.</p>'}</div>
<h2>Resolver Status</h2>
<pre>{html.escape(json.dumps({'search': search, 'agent': agent}, ensure_ascii=False, indent=2, default=str))}</pre>
"""
    path = out_dir / "index.html"
    path.write_text(report, encoding="utf-8")
    return path


def _asset_card(asset: dict[str, Any], rendered_layers: dict[str, dict[str, Any]], out_dir: Path) -> str:
    local = asset.get("local_asset_path")
    layer = rendered_layers.get(str(asset.get("rendered_layer_id") or ""), {})
    src = local or layer.get("src_path")
    if src:
        src_path = Path(str(src))
        abs_src_path = src_path if src_path.is_absolute() else (REPO_ROOT / src_path).resolve()
        if abs_src_path.exists():
            try:
                src = abs_src_path.relative_to(out_dir.resolve())
            except ValueError:
                src = abs_src_path
        preview = f'<img src="{html.escape(str(src))}" alt="">'
    else:
        preview = f'<span class="badge">{html.escape(str(asset.get("label") or asset.get("entity_name") or ""))}</span>'
    return (
        '<section class="card">'
        f'<div class="preview">{preview}</div>'
        f'<strong>{html.escape(str(asset.get("entity_name") or ""))}</strong>'
        f'<div class="meta">type={html.escape(str(asset.get("asset_type") or ""))}; '
        f'role={html.escape(str(asset.get("role") or ""))}; '
        f'source={html.escape(str(asset.get("source") or ""))}; '
        f'safe={html.escape(str(asset.get("safe_to_place") or False))}</div>'
        f'<div class="meta">asset_id={html.escape(str(asset.get("asset_id") or ""))}</div>'
        f'<div class="meta">local={html.escape(str(asset.get("local_asset_path") or ""))}</div>'
        f'<div class="meta">remote={html.escape(str(asset.get("source_url") or ""))}</div>'
        '</section>'
    )


if __name__ == "__main__":
    raise SystemExit(main())
