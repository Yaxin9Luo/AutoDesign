"""Dedicated coding-agent stage for official academic identity logo discovery.

The agent only discovers candidate official logo URLs. AutoDesign still
validates, downloads, and exposes local identity assets to poster planners.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..util.academic_identity_search import resolve_designer_identity_logo_candidates
from ..util.io import atomic_write_json
from ..util.logging import log


_CANDIDATES_FILE = "identity_logo_candidates.json"
_CONTEXT_FILE = "identity_logo_context.json"
_PROMPT_FILE = "identity_logo_prompt.md"
_RESOLUTION_FILE = "identity_logo_candidate_resolution.json"


class IdentityLogoAgent:
    """Run a local coding-agent command to discover official logo candidates."""

    def __init__(self, settings: Any):
        self.settings = settings

    def run(
        self,
        *,
        ctx: Any,
        identity_assets: dict[str, Any],
        allowlist_path: Path | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        mode = str(getattr(self.settings, "identity_logo_agent_mode", "auto") or "auto").strip().lower()
        if mode == "off":
            return identity_assets, {"status": "disabled", "resolver": "identity_logo_agent"}

        targets = _identity_logo_targets(
            identity_assets,
            max_entities=int(getattr(self.settings, "identity_logo_agent_max_entities", 6) or 6),
        )
        if not targets:
            return identity_assets, {"status": "skipped_no_targets", "resolver": "identity_logo_agent"}

        command = str(getattr(self.settings, "identity_logo_agent_cmd", "") or "").strip()
        required = mode == "required"
        if not command:
            result = {
                "status": "error" if required else "skipped_no_command",
                "resolver": "identity_logo_agent",
                "required": required,
                "blocking": required,
                "reason": "missing_identity_logo_agent_cmd",
                "targets": targets,
            }
            _record_agent_result(identity_assets, result)
            return identity_assets, result

        agent_dir = ctx.run_dir / "identity_logo_agent"
        if agent_dir.exists():
            shutil.rmtree(agent_dir)
        agent_dir.mkdir(parents=True, exist_ok=True)
        context = {
            "kind": "identity_logo_agent_context",
            "version": 1,
            "targets": targets,
            "academic_identity_assets": _json_safe(identity_assets),
            "policy": {
                "official_sources_only": True,
                "candidate_urls_only": True,
                "final_html_must_use_local_assets": True,
                "prefer_svg_png_wordmark": True,
                "reject": [
                    "hero images",
                    "video or film thumbnails",
                    "og/social preview images",
                    "Wikipedia/Wikimedia/social/news mirrors",
                    "stock or generated images",
                ],
            },
        }
        atomic_write_json(agent_dir / _CONTEXT_FILE, context)
        prompt = _build_prompt(context)
        (agent_dir / _PROMPT_FILE).write_text(prompt, encoding="utf-8")

        timeout_s = max(1, int(getattr(self.settings, "identity_logo_agent_timeout_s", 240) or 240))
        log(
            "identity_logo_agent.start",
            mode=mode,
            attempt_dir=str(agent_dir),
            target_count=len(targets),
            timeout_s=timeout_s,
        )
        try:
            proc = subprocess.run(
                shlex.split(command),
                input=prompt,
                text=True,
                cwd=str(agent_dir),
                timeout=timeout_s,
                capture_output=True,
                check=False,
            )
        except subprocess.TimeoutExpired:
            result = {
                "status": "error" if required else "timeout",
                "resolver": "identity_logo_agent",
                "required": required,
                "blocking": required,
                "reason": "timeout",
                "targets": targets,
            }
            _record_agent_result(identity_assets, result)
            return identity_assets, result
        except OSError as exc:
            result = {
                "status": "error" if required else "start_failed",
                "resolver": "identity_logo_agent",
                "required": required,
                "blocking": required,
                "reason": f"start_failed:{type(exc).__name__}",
                "targets": targets,
            }
            _record_agent_result(identity_assets, result)
            return identity_assets, result

        log_payload = {
            "returncode": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-4000:],
            "stderr_tail": (proc.stderr or "")[-4000:],
        }
        atomic_write_json(agent_dir / "identity_logo_agent_log.json", log_payload)
        if proc.returncode != 0:
            result = {
                "status": "error" if required else "subprocess_failed",
                "resolver": "identity_logo_agent",
                "required": required,
                "blocking": required,
                "reason": f"returncode:{proc.returncode}",
                "targets": targets,
            }
            _record_agent_result(identity_assets, result)
            return identity_assets, result

        manifest = _read_candidates_manifest(agent_dir / _CANDIDATES_FILE, stdout=proc.stdout)
        candidates = manifest.get("candidates") if isinstance(manifest, dict) else []
        if not isinstance(candidates, list) or not candidates:
            result = {
                "status": "no_candidates",
                "resolver": "identity_logo_agent",
                "required": required,
                "targets": targets,
                "rejections": manifest.get("rejections") if isinstance(manifest, dict) else [],
            }
            _record_agent_result(identity_assets, result)
            return identity_assets, result

        rendered_layers = ctx.state.get("rendered_layers")
        if not isinstance(rendered_layers, dict):
            rendered_layers = {}
            ctx.state["rendered_layers"] = rendered_layers
        max_candidates = max(1, int(getattr(self.settings, "identity_logo_agent_max_candidates", 12) or 12))
        updated, resolution = resolve_designer_identity_logo_candidates(
            identity_assets=identity_assets,
            rendered_layers=rendered_layers,
            run_dir=ctx.run_dir,
            layers_dir=ctx.layers_dir,
            candidates=[item for item in candidates if isinstance(item, dict)][:max_candidates],
            allowlist_path=allowlist_path,
            source="identity_logo_agent",
            resolver_name="identity_logo_agent",
            state_key="identity_logo_agent_resolution",
        )
        resolution["agent_status"] = "resolved"
        resolution["candidate_manifest"] = {
            "candidate_count": len(candidates),
            "rejection_count": len(manifest.get("rejections") or []) if isinstance(manifest, dict) else 0,
        }
        updated["identity_logo_agent"] = {
            "status": "resolved",
            "resolver": "identity_logo_agent",
            "attempt_dir": str(agent_dir),
            "target_count": len(targets),
            "resolved_count": resolution.get("resolved_count", 0),
        }
        atomic_write_json(agent_dir / _RESOLUTION_FILE, resolution)
        atomic_write_json(agent_dir / "academic_identity_assets.json", updated)
        return updated, resolution


def _identity_logo_targets(identity_assets: dict[str, Any], *, max_entities: int) -> list[dict[str, Any]]:
    assets = [item for item in (identity_assets.get("assets") or []) if isinstance(item, dict)]
    image_keys = {
        _entity_key(item.get("entity_name"))
        for item in assets
        if item.get("asset_type") == "image" and item.get("safe_to_place")
    }
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entity in identity_assets.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("entity_name") or "").strip()
        role = str(entity.get("role") or "").strip().lower()
        if not name or role not in {"venue", "institution", "lab", "company", "society", "publisher", "project"}:
            continue
        if str(entity.get("placement_intent") or "").lower() in {"context_mention", "reference_only", "do_not_place"}:
            continue
        key = _entity_key(name)
        if not key or key in seen or key in image_keys:
            continue
        seen.add(key)
        targets.append({
            "entity_name": name,
            "role": role,
            "required_to_place": bool(entity.get("required_to_place") or role == "venue"),
            "primary_identity": bool(entity.get("primary_identity")),
            "placement_intent": entity.get("placement_intent") or "supporting_identity",
            "query_hint": f"{name} {role if role != 'venue' else 'conference'} official logo SVG PNG brand assets",
        })
        if len(targets) >= max_entities:
            break
    return targets


def _build_prompt(context: dict[str, Any]) -> str:
    return (
        "You are AutoDesign's identity-logo discovery agent.\n\n"
        "Work only in the current directory. Read identity_logo_context.json. "
        "Use web search only to identify official institution, company, lab, "
        "society, publisher, or conference logo assets suitable for an academic "
        "paper poster header.\n\n"
        "Write identity_logo_candidates.json with this shape:\n"
        "{\n"
        "  \"version\": 1,\n"
        "  \"candidates\": [\n"
        "    {\"entity_name\": \"...\", \"role\": \"institution|company|lab|venue\", "
        "\"source_url\": \"https://...svg-or-png\", \"discovered_from_url\": "
        "\"https://official-page\", \"confidence\": 0.0, \"required_to_place\": true, "
        "\"primary_identity\": false, \"notes\": \"why this is official and logo-like\"}\n"
        "  ],\n"
        "  \"rejections\": [\n"
        "    {\"entity_name\": \"...\", \"url\": \"https://...\", \"reason\": \"not a logo\"}\n"
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Prefer direct SVG or transparent PNG logo/wordmark assets from official brand pages.\n"
        "- A page being official is not enough; hero images, video thumbnails, social preview "
        "images, photographs, banners, and topic illustrations are not logos.\n"
        "- Do not use Wikipedia, Wikimedia, social media, news/blog mirrors, stock, or generated images.\n"
        "- Do not download or edit final poster files. Remote URLs are discovery/provenance only.\n"
        "- If uncertain, add a rejection and leave the entity without a candidate.\n\n"
        f"Context:\n```json\n{json.dumps(context, ensure_ascii=False, indent=2)}\n```\n"
    )


def _read_candidates_manifest(path: Path, *, stdout: str) -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    text = (stdout or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _record_agent_result(identity_assets: dict[str, Any], result: dict[str, Any]) -> None:
    identity_assets["identity_logo_agent"] = result


def _entity_key(value: Any) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return str(value)
