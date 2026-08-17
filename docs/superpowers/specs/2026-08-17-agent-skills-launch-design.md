# AutoDesign Agent Skills Launch Design

## Context

AutoDesign already has four standalone, independently installable Agent Skills:
Poster, PPT, Webpage, and Video. Their implementation and deterministic release
packaging are complete on the stable Skills branch. The unfinished Agent-first
PDF ingestion work is explicitly excluded from this launch.

## Goal

Publish a clear, honest installation front door for the four stable Skills so
people can install them in Codex, Claude Code, or DeepSeek Harness today.

## Scope

- Add one new root README News item dated 2026-08-17.
- Link that News item directly to `agent_skills/README.md`, never to a PR.
- Rewrite `agent_skills/README.md` as the dedicated launch and installation
  guide.
- Preserve every existing historical News item unchanged.
- Reuse the existing deterministic ZIP/checksum/installer release format.
- Verify real installations in clean Codex, Claude Code, and DeepSeek Harness
  discovery roots.

## Non-goals

- Do not include Agent-first PDF ingestion v2.
- Do not change any Poster, PPT, Webpage, or Video generation behavior.
- Do not redesign the root README or add new visual assets.
- Do not claim that the Skills currently match the full AutoDesign Harness.

## User experience

The dedicated README is ordered for first-time users:

1. What the four Skills create.
2. The quickest supported installation path.
3. Host-specific installation paths for Codex, Claude Code, and DeepSeek
   Harness.
4. First-run verification and example prompts.
5. Prerequisites per Skill.
6. An explicit Skills-versus-Harness boundary.
7. The roadmap and contribution invitation.
8. Maintainer-only validation and deterministic packaging commands.

The simplest path uses `gh skill install` against the exact nested Skill path.
The checksum-verified release-local installer remains the portable fallback and
the release artifact format.

## Product boundary

The guide must say plainly that the Skills are a portable convenience layer,
not a replacement for the full AutoDesign Harness. The full Harness provides
more orchestration, shared context management, quality gates, retry routing,
and integrated review/editing surfaces. Skill output depends more heavily on
the host Coding Agent and therefore varies more.

The 70–80% figure is a future target for the portable Skills, not a measured
current result. The guide welcomes contributions to extraction, planning,
layout, evaluation, and artifact-specific validation.

## Installation contract

- Codex user scope: `$HOME/.agents/skills`.
- Claude Code user scope: `$HOME/.claude/skills`.
- DeepSeek Harness: project `.dsh/skills` or `.agents/skills`, and user DSH or
  Agent Skills homes.
- Each installed directory contains a complete `SKILL.md`, scripts,
  references, assets, and license.
- Installer examples remain checksum-first, atomic, and non-overwriting.

## Verification

- Documentation contract tests must fail before the README changes and pass
  afterward.
- All four package validators and existing packaging tests must remain green.
- Two independent release builds must be byte-identical.
- All four Skills must install from release ZIP plus checksum into clean Codex,
  Claude Code, and DeepSeek Harness roots.
- Read-only installed packages must remain byte-identical after help/doctor/init
  smoke commands and must create no `__pycache__` or `.pyc` files.

## Primary references

- OpenAI Codex Skills: https://developers.openai.com/codex/skills
- Claude Code Skills: https://code.claude.com/docs/en/skills
- DeepSeek Harness Skills: https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/skills.md
- Agent Skills specification: https://agentskills.io/specification
- GitHub CLI Skill installation: https://cli.github.com/manual/gh_skill_install
