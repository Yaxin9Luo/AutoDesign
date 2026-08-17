# AutoDesign Agent Skills Launch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish an honest, directly installable launch page and release bundle for the four stable AutoDesign Agent Skills.

**Architecture:** Keep the four stable Skill packages unchanged. Add a thin public documentation layer in the root News table and `agent_skills/README.md`, protect it with exact documentation contract tests, then rebuild and install the existing deterministic release archives in isolated host roots.

**Tech Stack:** Markdown, Python `unittest`, the existing stdlib-only Skill validator/packager, GitHub CLI, Git.

## Global Constraints

- Ship the stable four-Skill implementation at commit `3f19b11a0c8c7f7d291ee7aa8a3d08a1063b74c4` plus launch-only documentation changes.
- Do not include Agent-first PDF ingestion v2 or any unrelated worktree changes.
- The new 2026-08-17 News link must point to `./agent_skills/README.md`, not a PR.
- Preserve all historical News rows unchanged.
- Describe 70–80% as a future target, never a current benchmark result.
- State that Skills do not replace the full AutoDesign Harness.
- Keep release installation checksum-first, atomic, and non-overwriting.

---

### Task 1: Lock the public documentation contract

**Files:**
- Modify: `tests/test_portable_agent_skill_packages.py`
- Test: `tests/test_portable_agent_skill_packages.py`

**Interfaces:**
- Consumes: `README.md` and `agent_skills/README.md` as UTF-8 Markdown.
- Produces: regression checks for the News link, four Skills, host install paths, quick-install commands, limitations, roadmap target, and contribution invitation.

- [ ] **Step 1: Write the failing tests**

```python
def test_news_links_directly_to_agent_skills_readme(self):
    news = ROOT_README.read_text(encoding="utf-8")
    self.assertRegex(news, r"2026-08-17.*\./agent_skills/README\.md")

def test_launch_guide_is_installable_and_honest(self):
    guide = SKILLS_README.read_text(encoding="utf-8")
    for skill in APPROVED_SKILLS:
        self.assertIn(skill, guide)
    for phrase in ("gh skill install", "70–80%", "does not replace", "Contributing"):
        self.assertIn(phrase, guide)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest tests.test_portable_agent_skill_packages -v`

Expected: the new launch contract tests fail because the News row and honest launch copy do not exist yet.

- [ ] **Step 3: Commit the RED test boundary**

```bash
git add tests/test_portable_agent_skill_packages.py
git commit -m "test(skills): define launch documentation contract"
```

### Task 2: Publish the root News entry and dedicated guide

**Files:**
- Modify: `README.md`
- Modify: `agent_skills/README.md`

**Interfaces:**
- Consumes: the stable package names and existing release-local installer CLI.
- Produces: one root discovery link and a standalone installation/readiness guide.

- [ ] **Step 1: Add the root News row**

Add exactly one row dated `2026-08-17` above the existing rows. Link it to
`./agent_skills/README.md`; do not modify the previous rows.

- [ ] **Step 2: Rewrite the dedicated README**

Include:

```markdown
## Quick install
## Install by Coding Agent
## First run
## Requirements
## Skills vs. the full AutoDesign Harness
## Roadmap and contributing
## Maintainer verification
```

Use exact `gh skill install` commands for the four nested package paths, plus
the existing checksum-verified ZIP installer fallback.

- [ ] **Step 3: Run the focused test and verify GREEN**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest tests.test_portable_agent_skill_packages -v`

Expected: all package and documentation contract tests pass.

- [ ] **Step 4: Commit the public launch docs**

```bash
git add README.md agent_skills/README.md
git commit -m "docs(skills): publish standalone install guide"
```

### Task 3: Validate packages and deterministic release output

**Files:**
- Verify: `agent_skills/**`
- Verify: `scripts/package_agent_skills.py`
- Verify: `scripts/validate_agent_skills.py`

**Interfaces:**
- Consumes: stable Skill packages plus the documentation-only commits.
- Produces: two byte-identical `v0.1.0` release directories.

- [ ] **Step 1: Run focused package and packaging tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest \
  tests.test_portable_agent_skill_packages \
  tests.test_portable_agent_skill_packaging -v
```

Expected: 17 or more tests pass with no failures.

- [ ] **Step 2: Run all four package validators**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/validate_agent_skills.py --root agent_skills`

Expected: Poster, PPT, Webpage, and Video each print `OK`.

- [ ] **Step 3: Build release A and B**

```bash
python3 -B scripts/package_agent_skills.py build --source-root agent_skills --output-dir /tmp/autodesign-skills-v0.1.0-a --version 0.1.0
python3 -B scripts/package_agent_skills.py build --source-root agent_skills --output-dir /tmp/autodesign-skills-v0.1.0-b --version 0.1.0
diff -qr /tmp/autodesign-skills-v0.1.0-a /tmp/autodesign-skills-v0.1.0-b
```

Expected: no diff; each release contains four ZIPs, four checksum sidecars,
`manifest.json`, and the two release-local tools.

### Task 4: Install and smoke-test every host path

**Files:**
- Verify only: `/tmp/autodesign-skills-v0.1.0-a/**`

**Interfaces:**
- Consumes: release-local installer, ZIPs, and SHA-256 sidecars.
- Produces: twelve clean installations: four Skills each in Codex, Claude Code, and DeepSeek Harness roots.

- [ ] **Step 1: Install all four packages into each host root**

Use the release-local installer with `python3 -I`, each ZIP, its checksum, and
fresh destinations under `/tmp` representing `~/.agents/skills`,
`~/.claude/skills`, and `~/.dsh/skills`.

- [ ] **Step 2: Make installed packages read-only and smoke them**

For every installation, run the package harness `--help`, then the documented
`doctor`/`init` path where supported. Record allowed missing-runtime doctor
statuses without treating them as package failures.

- [ ] **Step 3: Verify immutability**

Hash every installed file before and after the smoke matrix. Assert hashes are
unchanged, no package path is writable, and no `__pycache__` or `.pyc` exists.

### Task 5: Review, publish, and verify the launch

**Files:**
- Review: all branch changes against `origin/main`

**Interfaces:**
- Consumes: reviewed commits and verified release A.
- Produces: a GitHub PR, merged public branch, and `v0.1.0` release assets.

- [ ] **Step 1: Review the complete diff and link targets**

Run `git diff --check origin/main...HEAD`, inspect the changed-file list, and
verify that the new News row resolves to the dedicated README while historical
News rows are byte-identical.

- [ ] **Step 2: Push and create the PR**

Push `codex/skills-launch` to the public repository and create a PR describing
the four Skills, installation matrix, honest limitations, and verification.

- [ ] **Step 3: Merge only after checks pass**

Verify the PR checks and changed-file scope before merging into `main`.

- [ ] **Step 4: Publish release assets**

Create the `agent-skills-v0.1.0` GitHub release from the merged commit and
upload the 11 verified files from release A.

- [ ] **Step 5: Verify public links and downloads**

Open the public root README, dedicated guide, release manifest, one ZIP, and
one checksum URL; require HTTP 200 and verify the downloaded ZIP digest.
