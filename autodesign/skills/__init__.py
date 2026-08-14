"""Runtime skill packs for AutoDesign.

These are product-side skills consumed by the AutoDesign pipeline. They are
not Codex local skills: the selected packs become compact context for the
enhancer, planner, critic, and repair pass.
"""

from __future__ import annotations

from .registry import (
    SkillBundle,
    SkillManifest,
    SkillPack,
    SkillRegistry,
    inject_skill_context,
    load_builtin_skills,
    select_skills,
)

__all__ = [
    "SkillBundle",
    "SkillManifest",
    "SkillPack",
    "SkillRegistry",
    "inject_skill_context",
    "load_builtin_skills",
    "select_skills",
]
