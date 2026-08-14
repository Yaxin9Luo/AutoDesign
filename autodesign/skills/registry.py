"""AutoDesign runtime skill registry and selector."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..config import Settings
from ..schema import ArtifactType
from ..util.logging import log


_CONTROL_SEP = "\n\n---\n\n"
_CONTROL_PREFIXES = ("Attached files:", "Template:", "Canvas Plan:", "Deck Plan:")
_STAGES = {"enhance", "plan", "critique", "repair"}
_MAX_RESOURCE_CHARS = 12_000
_CONTENT_SOURCE_SUFFIXES = {".pdf", ".docx", ".pptx", ".ppt", ".md", ".markdown", ".txt"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_SOURCE_IMAGE_CUES = (
    "screenshot",
    "screenshots",
    "source image",
    "source material",
    "materials",
    "reference document",
    "reference material",
    "analyze this image",
    "from this image",
    "from this screenshot",
    "截图",
    "资料图",
    "素材图",
    "来源图",
    "参考文档",
    "参考资料",
)
_NON_SOURCE_SINGLE_IMAGE_CUES = (
    "logo",
    "brand asset",
    "brand reference",
    "style reference",
    "visual reference",
    "moodboard",
    "icon",
    "watermark",
    "logo图",
    "标志",
    "品牌图",
    "风格参考",
)
_PAPER_CUES = (
    "arxiv",
    "paper",
    "research paper",
    "conference",
    "academic",
    "论文",
    "学术",
    "会议论文",
    "paper2deck",
)
_REPORT_CUES = (
    "report",
    "financial report",
    "annual report",
    "industry research",
    "whitepaper",
    "business pdf",
    "business",
    "quarterly",
    "earnings",
    "revenue",
    "sales report",
    "executive update",
    "company profile",
    "case study",
    "财报",
    "报告",
    "行业调研",
    "白皮书",
    "企业介绍",
)
_BEAUTIFY_CUES = (
    "beautify",
    "redesign",
    "revamp",
    "polish",
    "美化",
    "改版",
    "润色",
    "优化ppt",
    "润色ppt",
)


class SkillResource(BaseModel):
    """A v2 on-demand resource declared by a runtime skill pack."""

    model_config = ConfigDict(extra="forbid")

    id: str
    path: str
    description: str
    stages: list[str]
    when_to_read: str
    media_type: str

    @field_validator("id", "path", "description", "when_to_read", "media_type")
    @classmethod
    def _validate_nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("v2 resource text fields must be non-empty")
        return value

    @field_validator("stages")
    @classmethod
    def _validate_stages(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("v2 resource stages must be non-empty")
        bad = [stage for stage in value if stage not in _STAGES]
        if bad:
            raise ValueError(f"unknown resource stages: {bad}")
        return value


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    manifest_version: int = 1
    id: str
    version: str = "0.1.0"
    description: str = ""
    applies_to: list[str] = Field(default_factory=list)
    stages: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)
    priority: int = 50
    enabled_by_default: bool = True
    source: dict[str, Any] = Field(default_factory=dict)
    assets: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    resources: list[SkillResource] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _validate_v2_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        try:
            manifest_version = int(value.get("manifest_version", 1))
        except (TypeError, ValueError):
            return value
        if manifest_version != 2:
            return value
        if "description" not in value:
            raise ValueError("v2 manifest requires description")
        allowed = {
            "manifest_version", "id", "version", "description", "applies_to",
            "stages", "triggers", "priority", "enabled_by_default", "source",
            "assets", "outputs", "resources",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"unknown v2 manifest fields: {unknown}")
        return value

    @model_validator(mode="after")
    def _validate_v2_contract(self) -> "SkillManifest":
        if self.manifest_version == 1:
            return self
        if self.manifest_version != 2:
            raise ValueError("manifest_version must be 1 or 2")
        if not self.id.strip():
            raise ValueError("v2 skill id must be non-empty")
        if not self.version.strip():
            raise ValueError("v2 version must be non-empty")
        if not self.description.strip():
            raise ValueError("v2 description must be non-empty")
        if len(self.description) > 160:
            raise ValueError("v2 description must be at most 160 characters")
        resource_ids = [resource.id for resource in self.resources]
        if len(resource_ids) != len(set(resource_ids)):
            raise ValueError("v2 resource ids must be unique")
        declared_stages = set(self.stages)
        out_of_scope = sorted({
            stage
            for resource in self.resources
            for stage in resource.stages
            if stage not in declared_stages
        })
        if out_of_scope:
            raise ValueError(
                f"v2 resource stages must be declared by the skill: {out_of_scope}"
            )
        return self

    @field_validator("stages")
    @classmethod
    def _validate_stages(cls, value: list[str]) -> list[str]:
        # Preserve the historical validation behavior for v1 packs.
        bad = [stage for stage in value if stage not in _STAGES]
        if bad:
            raise ValueError(f"unknown skill stages: {bad}")
        return value


class SkillPack(BaseModel):
    manifest: SkillManifest
    root: Path
    markdown: str = ""
    content_hash: str = ""
    resource_hashes: dict[str, str] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def id(self) -> str:
        return self.manifest.id

    def render(self, stage: str) -> str:
        stage = (stage or "").strip().lower()
        if stage not in _STAGES:
            return ""
        if self.manifest.stages and stage not in self.manifest.stages:
            return ""

        section = _extract_stage_section(self.markdown, stage)
        if not section:
            return ""

        parts = [f"### {self.manifest.id} v{self.manifest.version}"]
        if self.manifest.manifest_version == 2:
            if self.manifest.description:
                parts.append(self.manifest.description)
            parts.append(section.strip())
            catalog = self._resource_catalog(stage)
            if catalog:
                parts.extend(["#### Runtime resources", catalog])
            return "\n\n".join(parts).strip()

        parts.append(section.strip())
        assets = _render_json_assets(self.root, self.manifest.assets)
        if assets and stage in {"enhance", "plan"}:
            parts.append("#### Skill assets")
            parts.append(assets)
        return "\n\n".join(parts).strip()

    def read_resource(self, resource_id: str, stage: str) -> str | None:
        if self.manifest.manifest_version != 2:
            return None
        stage = (stage or "").strip().lower()
        resource = self.resource(resource_id)
        if (
            resource is None
            or (self.manifest.stages and stage not in self.manifest.stages)
            or stage not in resource.stages
        ):
            return None
        expected_hash = self.resource_hashes.get(resource.id)
        if not expected_hash:
            return None
        try:
            if not self.verify_integrity():
                return None
            data = _read_resource_bytes(self.root, resource.path)
            if sha256(data).hexdigest() != expected_hash:
                return None
            text = data.decode("utf-8")
            _validate_resource_content(resource, text)
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        return text

    def verify_integrity(self) -> bool:
        if self.manifest.manifest_version != 2 or not self.content_hash:
            return False
        try:
            skill_bytes = (self.root / "SKILL.md").read_bytes()
            if _content_hash(self.manifest, skill_bytes, self.root) != self.content_hash:
                return False
            for resource in self.manifest.resources:
                expected = self.resource_hashes.get(resource.id)
                if not expected:
                    return False
                if sha256(_read_resource_bytes(self.root, resource.path)).hexdigest() != expected:
                    return False
        except (OSError, UnicodeDecodeError, ValueError):
            return False
        return True

    def resource(self, resource_id: str) -> SkillResource | None:
        return next((item for item in self.manifest.resources if item.id == resource_id), None)

    def _resource_catalog(self, stage: str) -> str:
        entries = [item for item in self.manifest.resources if stage in item.stages]
        return "\n".join(
            f"- `{item.id}` ({item.media_type}): {item.description} "
            f"Read when: {item.when_to_read}"
            for item in entries
        )

    def runtime_summary(self) -> dict[str, Any]:
        return {
            "id": self.manifest.id,
            "manifest_version": self.manifest.manifest_version,
            "version": self.manifest.version,
            "description": self.manifest.description,
            "applies_to": list(self.manifest.applies_to),
            "stages": list(self.manifest.stages),
            "triggers": list(self.manifest.triggers),
            "priority": int(self.manifest.priority),
            "enabled_by_default": bool(self.manifest.enabled_by_default),
            "assets": list(self.manifest.assets),
            "outputs": list(self.manifest.outputs),
            "source": dict(self.manifest.source),
            "resources": [item.model_dump(mode="json") for item in self.manifest.resources],
            "root": str(self.root),
            "content_hash": self.content_hash,
            "resource_hashes": dict(self.resource_hashes),
        }

    @classmethod
    def from_runtime_summary(cls, summary: dict[str, Any]) -> "SkillPack" | None:
        try:
            manifest_fields = {
                key: summary.get(key)
                for key in (
                    "manifest_version", "id", "version", "description", "applies_to",
                    "stages", "triggers", "priority", "enabled_by_default", "source",
                    "assets", "outputs", "resources",
                )
                if key in summary
            }
            manifest = SkillManifest.model_validate(manifest_fields)
            content_hash = str(summary.get("content_hash") or "")
            root = Path(str(summary.get("root") or ""))
            resource_hashes = summary.get("resource_hashes") or {}
            if manifest.manifest_version != 2 or not content_hash or not root.is_absolute():
                return None
            if not isinstance(resource_hashes, dict):
                return None
            markdown = (root / "SKILL.md").read_text(encoding="utf-8")
            return cls(
                manifest=manifest,
                root=root,
                markdown=markdown,
                content_hash=content_hash,
                resource_hashes={str(key): str(value) for key, value in resource_hashes.items()},
            )
        except (TypeError, ValidationError, ValueError):
            return None


class SkillBundle:
    def __init__(self, packs: list[SkillPack]):
        self.packs = sorted(
            packs,
            key=lambda p: (-int(p.manifest.priority), p.manifest.id),
        )

    @property
    def ids(self) -> list[str]:
        return [p.id for p in self.packs]

    def render(self, stage: str) -> str:
        rendered = [p.render(stage) for p in self.packs]
        rendered = [r for r in rendered if r]
        if not rendered:
            return ""
        stage = (stage or "").strip().lower()
        header = (
            f"## AutoDesign Runtime Skills Context ({stage})\n"
            "These selected skills are runtime guidance, not user content. "
            "Prefer DesignSpec.html_artifact as the authoring graph for new "
            "artifacts; layer_graph/deck_html are compatibility mirrors only. "
            "Preserve editability and follow higher-priority user instructions."
        )
        return "\n\n".join([header, *rendered]).strip()

    def render_all(self) -> dict[str, str]:
        return {stage: self.render(stage) for stage in sorted(_STAGES)}

    def read_resource(self, *, skill_id: str, resource_id: str, stage: str) -> str | None:
        pack = self.get(skill_id)
        return pack.read_resource(resource_id, stage) if pack is not None else None

    def get(self, skill_id: str) -> SkillPack | None:
        return next((pack for pack in self.packs if pack.id == skill_id), None)

    def to_runtime_state(self) -> dict[str, Any]:
        return {
            "selected": self.ids,
            "packs": [p.runtime_summary() for p in self.packs],
        }

    @classmethod
    def from_runtime_state(cls, state: Any) -> "SkillBundle":
        if not isinstance(state, dict):
            return cls([])
        selected = state.get("selected")
        summaries = state.get("packs")
        if not isinstance(selected, list) or not isinstance(summaries, list):
            return cls([])
        selected_ids = {str(skill_id) for skill_id in selected}
        packs = [
            pack
            for summary in summaries
            if isinstance(summary, dict)
            and str(summary.get("id") or "") in selected_ids
            and (pack := SkillPack.from_runtime_summary(summary)) is not None
        ]
        return cls(_dedupe_packs(packs))


class SkillRegistry:
    def __init__(self, packs: list[SkillPack]):
        self.packs = sorted(
            packs,
            key=lambda p: (-int(p.manifest.priority), p.manifest.id),
        )

    @classmethod
    def load(cls, root: Path) -> "SkillRegistry":
        packs: list[SkillPack] = []
        if not root.exists():
            return cls([])
        for manifest_path in sorted(root.rglob("skill.json")):
            try:
                manifest = SkillManifest.model_validate_json(
                    manifest_path.read_text(encoding="utf-8")
                )
            except (ValidationError, OSError, ValueError) as e:
                log("skills.load_error", path=str(manifest_path), error=str(e))
                continue
            skill_root = manifest_path.parent
            md_path = skill_root / "SKILL.md"
            try:
                if manifest.manifest_version == 1:
                    log("skills.deprecated_manifest_v1", path=str(manifest_path), skill_id=manifest.id)
                    markdown = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
                    packs.append(SkillPack(manifest=manifest, root=skill_root, markdown=markdown))
                    continue
                markdown = md_path.read_text(encoding="utf-8")
                resource_hashes = _validate_v2_pack(manifest, skill_root)
                packs.append(SkillPack(
                    manifest=manifest,
                    root=skill_root,
                    markdown=markdown,
                    content_hash=_content_hash(manifest, md_path.read_bytes(), skill_root),
                    resource_hashes=resource_hashes,
                ))
            except (OSError, UnicodeDecodeError, ValueError) as e:
                log("skills.load_error", path=str(manifest_path), error=str(e))
        return cls(packs)

    def get(self, skill_id: str) -> SkillPack | None:
        return next((pack for pack in self.packs if pack.id == skill_id), None)

    def select(
        self,
        *,
        brief: str,
        attachments: list[Path],
        artifact_hint: str | ArtifactType | None,
    ) -> SkillBundle:
        artifact = _normalize_artifact_hint(artifact_hint, brief)
        selection_text = _selection_text(brief, attachments)
        brief_lc = (brief or "").lower()
        has_content_source = _has_content_source_attachment(attachments, selection_text)
        deck_primary_id = _deck_primary_skill_id(
            artifact=artifact,
            selection_text=selection_text,
            attachments=attachments,
        )

        selected: list[SkillPack] = []
        for pack in self.packs:
            m = pack.manifest
            if not m.enabled_by_default:
                continue
            if not _applies_to(m.applies_to, artifact):
                continue

            if m.id == "common.source_analysis_flow":
                if has_content_source:
                    selected.append(pack)
                continue
            if m.id == "common.pdf_visual_curation":
                if has_content_source and _has_pdf_attachment(attachments):
                    selected.append(pack)
                continue
            if m.id == "common.pdf_render_qa":
                if _has_pdf_attachment(attachments):
                    selected.append(pack)
                continue
            if m.id == "common.export_qa":
                selected.append(pack)
                continue
            if m.id == "common.playwright_browser_qa":
                if artifact in {"poster", "deck", "landing"}:
                    selected.append(pack)
                continue
            if m.id == "deck.paper2deck_provenance":
                if deck_primary_id == m.id:
                    selected.append(pack)
                continue
            if m.id == "deck.report2deck_general":
                if deck_primary_id == m.id:
                    selected.append(pack)
                continue
            if m.id == "deck.ppt_beautify":
                if deck_primary_id == m.id:
                    selected.append(pack)
                continue
            if m.id == "deck.html_ppt_general":
                if deck_primary_id in {
                    "deck.paper2deck_provenance",
                    "deck.report2deck_general",
                    "deck.html_ppt_general",
                }:
                    selected.append(pack)
                continue
            if m.id == "poster.visual_recipe":
                if artifact == "poster":
                    selected.append(pack)
                continue
            if m.id == "poster.table_craft":
                if artifact == "poster" and (
                    _has_pdf_attachment(attachments)
                    or any(cue in selection_text for cue in _PAPER_CUES)
                    or _trigger_match(m.triggers, brief_lc)
                ):
                    selected.append(pack)
                continue
            if m.id == "landing.visual_recipe":
                if artifact == "landing":
                    selected.append(pack)
                continue
            if m.id == "video.conference_video":
                if artifact == "video":
                    selected.append(pack)
                continue

            if _trigger_match(m.triggers, brief_lc):
                selected.append(pack)

        return SkillBundle(_dedupe_packs(selected))


def load_builtin_skills(settings: Settings) -> SkillRegistry:
    return SkillRegistry.load(settings.skills_dir)


def select_skills(
    brief: str,
    attachments: list[Path],
    artifact_hint: str | ArtifactType | None,
    settings: Settings,
) -> SkillBundle:
    if not getattr(settings, "enable_skills", True):
        log("skills.skipped", reason="disabled in settings")
        return SkillBundle([])
    registry = load_builtin_skills(settings)
    bundle = registry.select(
        brief=brief,
        attachments=attachments,
        artifact_hint=artifact_hint,
    )
    log("skills.selected", ids=bundle.ids)
    return bundle


def inject_skill_context(brief: str, context: str) -> str:
    """Insert skill context after leading runner control prologues.

    `Attached files:` / `Template:` must stay at byte 0 because the planner
    and enhancer use those literal prefixes as control signals.
    """
    context = (context or "").strip()
    if not context:
        return brief
    insert_at = _leading_control_end(brief)
    block = context + _CONTROL_SEP
    if insert_at <= 0:
        return block + brief
    return brief[:insert_at] + block + brief[insert_at:]


def _leading_control_end(text: str) -> int:
    pos = 0
    while True:
        remaining = text[pos:]
        if not remaining.startswith(_CONTROL_PREFIXES):
            return pos
        sep_idx = remaining.find(_CONTROL_SEP)
        if sep_idx < 0:
            return pos
        pos += sep_idx + len(_CONTROL_SEP)


def _normalize_artifact_hint(
    artifact_hint: str | ArtifactType | None,
    brief: str,
) -> str:
    if isinstance(artifact_hint, ArtifactType):
        return artifact_hint.value
    raw = (artifact_hint or "").strip().lower()
    if raw in {"poster", "deck", "landing", "video"}:
        return raw
    text = (brief or "").lower()
    if any(t in text for t in ("artifact_type: poster", "type: poster", "poster", "海报")):
        return "poster"
    if any(t in text for t in ("type: deck", "deck", "slides", "slide deck", "ppt", "pptx", "powerpoint", "keynote", "演示", "幻灯片")):
        return "deck"
    if any(t in text for t in ("type: landing", "landing", "web page", "website", "网页", "网站", "着陆页")):
        return "landing"
    if any(t in text for t in ("type: video", "video", "mp4", "animated", "视频", "动画")):
        return "video"
    return "poster"


def _applies_to(applies_to: list[str], artifact: str) -> bool:
    values = {v.lower() for v in applies_to}
    return not values or "all" in values or artifact in values


def _selection_text(brief: str, attachments: list[Path]) -> str:
    filenames = " ".join(p.name for p in attachments)
    return f"{brief or ''} {filenames}".lower()


def _has_content_source_attachment(attachments: list[Path], selection_text: str) -> bool:
    suffixes = [p.suffix.lower() for p in attachments]
    if any(s in _CONTENT_SOURCE_SUFFIXES for s in suffixes):
        return True
    image_count = sum(1 for s in suffixes if s in _IMAGE_SUFFIXES)
    if image_count >= 2:
        return True
    if image_count == 1:
        if _has_cue(selection_text, _NON_SOURCE_SINGLE_IMAGE_CUES):
            return False
        return _has_cue(selection_text, _SOURCE_IMAGE_CUES)
    return False


def _has_suffix(attachments: list[Path], suffixes: set[str]) -> bool:
    return any(p.suffix.lower() in suffixes for p in attachments)


def _has_pdf_attachment(attachments: list[Path]) -> bool:
    return _has_suffix(attachments, {".pdf"})


def _has_cue(text: str, cues: tuple[str, ...]) -> bool:
    for cue in cues:
        cue_lc = cue.lower()
        if not cue_lc:
            continue
        # ASCII word-like cues should not match inside longer words; this keeps
        # "whitepaper" from routing to the academic paper skill via "paper".
        if re.fullmatch(r"[a-z0-9][a-z0-9 \-_/]*", cue_lc):
            if re.search(rf"(?<![a-z0-9]){re.escape(cue_lc)}(?![a-z0-9])", text):
                return True
            continue
        if cue_lc in text:
            return True
    return False


def _deck_primary_skill_id(
    *,
    artifact: str,
    selection_text: str,
    attachments: list[Path],
) -> str | None:
    if artifact != "deck":
        return None
    has_pdf = _has_suffix(attachments, {".pdf"})
    has_ppt = _has_suffix(attachments, {".ppt", ".pptx"})
    has_content_source = _has_content_source_attachment(attachments, selection_text)

    if has_ppt or _has_cue(selection_text, _BEAUTIFY_CUES):
        return "deck.ppt_beautify"
    if _has_cue(selection_text, _REPORT_CUES):
        return "deck.report2deck_general"
    if _has_cue(selection_text, _PAPER_CUES) or has_pdf:
        return "deck.paper2deck_provenance"
    if has_content_source:
        return "deck.report2deck_general"
    return "deck.html_ppt_general"


def _trigger_match(triggers: list[str], brief_lc: str) -> bool:
    if not triggers:
        return True
    return any(t.lower() in brief_lc for t in triggers)


def _dedupe_packs(packs: list[SkillPack]) -> list[SkillPack]:
    seen: set[str] = set()
    out: list[SkillPack] = []
    for pack in packs:
        if pack.id in seen:
            continue
        seen.add(pack.id)
        out.append(pack)
    return out


def _extract_stage_section(markdown: str, stage: str) -> str:
    if not markdown:
        return ""
    pattern = re.compile(
        rf"^## Stage:\s*{re.escape(stage)}\s*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(markdown)
    if not match:
        return ""
    start = match.end()
    next_match = re.search(r"^## Stage:", markdown[start:], re.MULTILINE)
    end = start + next_match.start() if next_match else len(markdown)
    return markdown[start:end].strip()


def _validate_v2_pack(manifest: SkillManifest, root: Path) -> dict[str, str]:
    markdown = (root / "SKILL.md").read_text(encoding="utf-8")
    _validate_v2_stage_headings(markdown, expected_stages=manifest.stages)
    hashes: dict[str, str] = {}
    for resource in manifest.resources:
        data = _read_resource_bytes(root, resource.path)
        text = data.decode("utf-8")
        _validate_resource_content(resource, text)
        hashes[resource.id] = sha256(data).hexdigest()
    return hashes


def _validate_v2_stage_headings(markdown: str, *, expected_stages: list[str]) -> None:
    headings = re.findall(r"^## Stage:\s*(.*?)\s*$", markdown, re.MULTILINE)
    for raw_stage in headings:
        if raw_stage not in _STAGES:
            raise ValueError(f"invalid v2 stage heading: {raw_stage!r}")
    if len(headings) != len(set(headings)):
        raise ValueError("v2 stage headings must be unique")
    if set(headings) != set(expected_stages):
        raise ValueError(
            "v2 stage headings must exactly match manifest stages: "
            f"headings={headings}, manifest={expected_stages}"
        )


def _read_resource_bytes(root: Path, relative_path: str) -> bytes:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe resource path: {relative_path}")
    root_resolved = root.resolve(strict=True)
    resolved = (root / path).resolve(strict=True)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as e:
        raise ValueError(f"resource escapes skill pack: {relative_path}") from e
    if not resolved.is_file():
        raise ValueError(f"resource is not a file: {relative_path}")
    return resolved.read_bytes()


def _validate_resource_content(resource: SkillResource, text: str) -> None:
    if len(text) > _MAX_RESOURCE_CHARS:
        raise ValueError(
            f"resource {resource.id!r} exceeds {_MAX_RESOURCE_CHARS} characters"
        )
    if resource.media_type.lower().split(";", 1)[0].strip() == "application/json":
        try:
            json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"resource {resource.id!r} is invalid JSON") from e


def _content_hash(manifest: SkillManifest, skill_bytes: bytes, root: Path) -> str:
    canonical_manifest = manifest.model_dump(mode="json")
    canonical_manifest["resources"] = sorted(
        canonical_manifest["resources"], key=lambda resource: resource["id"]
    )
    digest = sha256()
    digest.update(json.dumps(
        canonical_manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8"))
    digest.update(skill_bytes)
    for resource in sorted(manifest.resources, key=lambda item: item.id):
        digest.update(resource.id.encode("utf-8"))
        digest.update(_read_resource_bytes(root, resource.path))
    return digest.hexdigest()


def _render_json_assets(root: Path, assets: list[str]) -> str:
    rendered: list[str] = []
    for rel in assets:
        path = root / rel
        if path.suffix.lower() != ".json" or not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(text) > 6000:
            text = text[:6000].rstrip() + "\n...<truncated>"
        rendered.append(f"`{rel}`:\n```json\n{text}\n```")
    return "\n\n".join(rendered)
