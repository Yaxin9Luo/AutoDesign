# Poster Agent-First PDF Ingestion v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade only `autodesign-poster` from the released catalog-first workflow to an Agent-first, revisioned PDF workflow that lets the host Agent inspect the complete paper, register verified PDF-region crops, revise source selection and planning, and use a strictly read-only DOM audit while preserving the standalone Skill's deterministic safety.

**Architecture:** The shared portable core gains an explicitly opted-in run-format-v2 state/revision substrate while its default remains released run format 1. The Poster harness is the only caller that opts into v2. The host Agent owns PDF interpretation, visual importance, crop choice, semantic review, and HTML/CSS edits; scripts own immutable page rendering, deterministic cropping, hashing, append-only receipts, revision commits, recovery, measurement, and publication gates. A Poster-only browser worker measures the DOM and writes QA evidence but proves the artifact tree is byte-identical before and after the audit.

**Tech Stack:** Python 3.10+ standard library, Poppler CLI (`pdftotext`, `pdfinfo`, `pdftoppm`, `pdfimages`), exact-pinned Playwright/Chromium already bundled by the Skill, HTML/CSS, JSON/JSONL, Git, `unittest`, existing deterministic Skill packager/validator.

## Global Constraints

- Implement against current public branch `codex/poster-agent-first-v2`; do not merge `codex/agent-first-pdf-skills` wholesale. Commit `db028ff` is review evidence only.
- The approved design is [2026-08-17-poster-agent-first-v2-design.md](../specs/2026-08-17-poster-agent-first-v2-design.md). If code and this plan disagree with the design, stop and resolve the design mismatch before coding.
- Only Poster opts into run format 2. `autodesign-ppt`, `autodesign-webpage`, and `autodesign-video` must initialize and resume released run format 1 exactly as before.
- Keep artifact/delivery payload schema version 1 separate from run state version 2. Do not globally change the current `FORMAT_VERSION = 1` constant used by existing artifact contracts.
- Treat `RUN/input/source.pdf` or complete hash-bound page renders as the semantic surface. `pdfimages` output remains untrusted hints and can never become an eligibility whitelist.
- Do not accept arbitrary scratch images as paper evidence. Source assets must derive from the immutable source PDF/page manifest or be style-only reference images kept outside evidence.
- Do not add an API provider, server process, queue, SSE layer, hidden AutoDesign import, or model judge. The installed Skill must remain standalone.
- The DOM tool is read-only. Do not port auto-fit, font shrinking, panel expansion, element movement, CSS injection, or any other mutation path from the full Harness.
- Every CLI command prints one redacted JSON value to stdout and returns nonzero for blocked, failed, corrupt, or incomplete work. Persist only paths relative to the run.
- Reject symlinks, hardlinks, traversal, non-regular files, stale hashes, noncanonical transaction bytes, and conflicting revision parents before mutation. Failed operations must not write outside the run.
- Keep installed packages immutable: no cache, bytecode, run state, browser output, or temporary file may be written inside `agent_skills/autodesign-poster`.
- Use RED-GREEN-REFACTOR for every slice. Run the focused test before implementation, capture the intended failure, then make only that failure green.
- Commit after every task using the exact commit message listed. Each commit must include only the task's owned paths; inspect `git diff --cached --name-only` before committing.
- Do not publish a release, update News, create a PR, or merge in this plan. The endpoint is a reviewed, merge-ready release candidate with real acceptance evidence.

## Public Interfaces and Data Contracts

Keep these names stable throughout implementation:

```python
# shared v2 source vocabulary
SOURCE_KINDS = ("figure", "table", "diagram", "plot", "photo", "equation", "other")
SOURCE_IMPORTANCE = ("essential", "supporting")

# Poster-only policy in poster_harness.py
POSTER_SOURCE_ROLES = (
    "method", "overview", "method-overview",
    "result", "primary-result", "comparison",
    "context", "supporting",
)

# agent_skills/_shared/portable_png.py
def inspect_png(data: bytes) -> dict[str, int]: ...
def crop_png(data: bytes, box: tuple[int, int, int, int]) -> bytes: ...

# agent_skills/_shared/portable_core.py
RELEASED_RUN_FORMAT_VERSION = 1
AGENT_FIRST_RUN_FORMAT_VERSION = 2

def initialize_run(
    run_dir: Path | str,
    skill_root: Path | str,
    *,
    release_version: str,
    archive_sha256: str | None = None,
    run_format_version: int = RELEASED_RUN_FORMAT_VERSION,
) -> dict[str, Any]: ...

def inspect_run_format(run_dir: Path | str) -> int: ...
def diagnose_v1_run(run_dir: Path | str) -> dict[str, Any]: ...
def inspect_source(run_dir: Path | str) -> dict[str, Any]: ...
def crop_source(
    run_dir: Path | str,
    request: Mapping[str, Any],
    *,
    fail_at: str | None = None,
) -> dict[str, Any]: ...
def list_source_assets(run_dir: Path | str) -> dict[str, Any]: ...
def create_source_review_context(
    run_dir: Path | str,
    selection: Mapping[str, Any],
) -> dict[str, Any]: ...
def record_source_review(
    run_dir: Path | str,
    context_path: Path | str,
    review: Mapping[str, Any],
    *,
    fail_at: str | None = None,
) -> dict[str, Any]: ...
def save_plan_revision(
    run_dir: Path | str,
    plan: Mapping[str, Any],
    *,
    fail_at: str | None = None,
) -> dict[str, Any]: ...
def load_active_plan(run_dir: Path | str) -> dict[str, Any]: ...
def load_attempt_plan(run_dir: Path | str, attempt_id: str) -> dict[str, Any]: ...
def load_attempt_visual_catalog(
    run_dir: Path | str,
    attempt_id: str,
) -> dict[str, Any]: ...
def reopen_curation(
    run_dir: Path | str,
    request: Mapping[str, Any],
) -> dict[str, Any]: ...
```

Poster DOM audit interfaces:

```python
# agent_skills/autodesign-poster/scripts/poster_dom_audit.py
def browser_probe_script() -> str: ...
def evaluate_dom_snapshot(
    snapshot: Mapping[str, Any],
    *,
    canvas: Mapping[str, Any],
    print_size: Mapping[str, Any],
) -> dict[str, Any]: ...
def run_poster_dom_audit(
    run_dir: Path | str,
    attempt_id: str,
    *,
    cache_root: Path | None = None,
    allow_browser_install: bool = True,
) -> dict[str, Any]: ...
```

The v2 crop request must use this exact top-level schema:

```json
{
  "run_format_version": 2,
  "source_manifest_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "page_manifest_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "page": 7,
  "page_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "bbox": [0.12, 0.18, 0.84, 0.71],
  "kind": "figure",
  "poster_role": "method-overview",
  "supports_claims": ["claim-method-01"],
  "why_essential": "The paper's principal system diagram",
  "intended_reuse_limit": 1,
  "supersedes": []
}
```

The three repeated-letter hashes above are illustrative valid-length values; a real request copies the exact three hashes emitted by `inspect-source`. Coordinates are normalized `[left, top, right, bottom]`, top-left origin, inclusive of neither right nor bottom pixel. `page` is one-based. Canonical request bytes plus source/page hashes determine one stable `asset_id`; an identical request is idempotent. Shared core binds `poster_role` and `intended_reuse_limit` as opaque receipt metadata; only the Poster wrapper interprets their policy or enum, keeping the shared crop algorithm free of layout/design decisions.

---

### Task 0: Freeze the Baseline and Scope

**Files:**
- Read: `docs/superpowers/specs/2026-08-17-poster-agent-first-v2-design.md`
- Read: `agent_skills/_shared/portable_core.py`
- Read: `agent_skills/autodesign-poster/scripts/poster_harness.py`
- Read: `scripts/sync_agent_skill_core.py`
- Read: `tests/test_portable_skill_run_state.py`
- Read: `tests/test_autodesign_poster_skill.py`
- Review only: `git show db028ff -- agent_skills/_shared/portable_core.py agent_skills/_shared/portable_png.py tests/test_portable_skill_run_state.py`

- [ ] **Step 1: Confirm repository and branch**

Run:

```bash
cd /Users/yaxinluo/Desktop/AutoDesign-public-release-ready-20260813
git status --short --branch
git rev-parse --show-toplevel
git log -3 --oneline
```

Expected: branch `codex/poster-agent-first-v2`, clean tree, design commits `2be9010` and `4c90c64` in history.

- [ ] **Step 2: Capture the released baseline before edits**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_portable_skill_run_state \
  tests.test_autodesign_poster_skill \
  tests.test_autodesign_ppt_skill \
  tests.test_autodesign_webpage_skill \
  tests.test_autodesign_video_skill -v
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_agent_skills.py --root agent_skills
PYTHONDONTWRITEBYTECODE=1 python3 scripts/sync_agent_skill_core.py --check
git status --short
```

Expected: all current tests and validators pass, sync reports `checked 4`, and tests create no tracked or ignored bytecode under `agent_skills/`.

- [ ] **Step 3: Record candidate-code boundaries in the implementation notes**

The implementer must write these three facts into the first task's commit message body or local execution log:

1. `db028ff` is stale candidate code, not merge input.
2. Only pure crop/state/revision ideas may be ported after current-main tests are RED.
3. Product modules under `autodesign/` are unavailable at installed-Skill runtime.

Do not commit anything in Task 0.

---

### Task 1: Add the Deterministic PNG Crop Primitive

**Files:**
- Create: `agent_skills/_shared/portable_png.py`
- Create: `agent_skills/autodesign-poster/scripts/portable_png.py`
- Modify: `scripts/sync_agent_skill_core.py`
- Create: `tests/test_portable_png.py`
- Modify: `tests/test_portable_skill_run_state.py`
- Modify: `tests/test_portable_agent_skill_packages.py`

- [ ] **Step 1: Write RED tests for PNG filters, modes, safety, and sync**

In `tests/test_portable_png.py`, construct PNG bytes in the test with standard-library `struct` and `zlib`. Use five separate 4x3 RGBA fixtures—one fixture each for row filter 0, 1, 2, 3, and 4—with all three rows using that fixture's filter. Crop pixel box `(1, 0, 4, 2)` and assert the same literal 3x2 expected RGBA pixels for every filter.

Also assert:

- repeated crops are byte-identical;
- `inspect_png` reports width, height, bit depth, color type, channels, and row bytes;
- 8-bit non-interlaced grayscale, grayscale-alpha, RGB, and RGBA work;
- invalid CRC, truncated IDAT, trailing zlib bytes, unsupported bit depth, indexed color, interlace, invalid filter, and out-of-range/empty box fail;
- output is a valid non-interlaced PNG and does not retain untrusted metadata chunks;
- canonical and vendored Poster copies are byte-identical after sync;
- PPT/Webpage/Video do not gain an unused `portable_png.py` file.

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_portable_png -v
```

Expected RED: import/file-not-found failures because `portable_png.py` does not exist.

- [ ] **Step 2: Implement the minimum standard-library decoder/cropper**

Port only the reviewed pure PNG logic from `db028ff`. Decode filters 0–4, validate chunk CRCs and exact zlib consumption, crop decoded scanlines, and re-encode deterministic rows with filter 0. Do not add Pillow, NumPy, or image-semantic policy.

`inspect_png` must return:

```python
{
    "width": int,
    "height": int,
    "bit_depth": 8,
    "color_type": int,
    "channels": int,
    "row_bytes": int,
}
```

- [ ] **Step 3: Extend sync with a Poster-only source map**

In `scripts/sync_agent_skill_core.py`, keep the existing all-Skill `sources` map unchanged and add a `skill_specific_sources` map:

```python
skill_specific_sources = {
    "autodesign-poster": {
        Path("scripts/portable_png.py"): shared / "portable_png.py",
    },
}
```

Apply the same symlink/file/atomic-copy checks used by shared sources. Update the synthetic sync fixtures in `tests/test_portable_skill_run_state.py` to create the canonical PNG source and to verify drift/fail-closed behavior.

- [ ] **Step 4: Sync and run GREEN tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/sync_agent_skill_core.py
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_portable_png \
  tests.test_portable_skill_run_state \
  tests.test_portable_agent_skill_packages -v
PYTHONDONTWRITEBYTECODE=1 python3 scripts/sync_agent_skill_core.py --check
git diff --check
```

Expected: all pass; canonical and Poster copy match; other three packages remain unchanged by PNG sync.

- [ ] **Step 5: Review and commit**

Run:

```bash
git diff -- agent_skills/_shared/portable_png.py \
  agent_skills/autodesign-poster/scripts/portable_png.py \
  scripts/sync_agent_skill_core.py tests/test_portable_png.py \
  tests/test_portable_skill_run_state.py tests/test_portable_agent_skill_packages.py
git add agent_skills/_shared/portable_png.py \
  agent_skills/autodesign-poster/scripts/portable_png.py \
  scripts/sync_agent_skill_core.py tests/test_portable_png.py \
  tests/test_portable_skill_run_state.py tests/test_portable_agent_skill_packages.py
git diff --cached --check
git commit -m "feat(skills): add deterministic Poster crop primitive"
```

---

### Task 2: Add Opt-In Run Format 2 and PDF Source Derivations

**Files:**
- Modify: `agent_skills/_shared/portable_core.py`
- Modify after canonical GREEN: `agent_skills/autodesign-{poster,ppt,webpage,video}/scripts/_portable.py` via sync only
- Create: `tests/test_portable_source_curation_v2.py`
- Modify: `tests/test_portable_skill_run_state.py`

- [ ] **Step 1: Write RED compatibility and source-inspection tests**

Create `tests/test_portable_source_curation_v2.py` with helpers that build an isolated fake Skill root and fake Poppler command runner. Test:

- omitted `run_format_version` produces the exact released v1 directory/state contract;
- explicit `run_format_version=2` creates `source-assets/files`, `source-assets/receipts`, `source-reviews`, `curations`, `plans`, and `provenance/supersessions.jsonl` in addition to existing immutable source/page data;
- `inspect_run_format` reads only safe `run.json` metadata and rejects missing, bool, unknown, symlinked, or malformed values;
- mutating v2 APIs reject v1 before loading/executing the old Skill snapshot;
- `diagnose_v1_run` reports relative paths/state read-only and leaves every run byte unchanged;
- PDF preparation creates canonical `evidence/page-manifest.json` and `evidence/pdfimages-hints.json` from the copied `RUN/input/source.pdf`;
- `inspect_source` returns `input/source.pdf`, its hash, every page in numeric order, page dimensions/hashes, and hint trust=`untrusted`;
- text/Markdown sources remain supported in v2 as source-text-only mode, but `crop_source` rejects them and any no-visual decision requires later explicit `not_applicable` review evidence.

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_portable_source_curation_v2.PortableSourceCurationV2Tests.test_v2_initialization_is_explicit_and_v1_default_is_unchanged \
  tests.test_portable_source_curation_v2.PortableSourceCurationV2Tests.test_inspect_and_diagnose_are_fail_closed_and_read_only -v
```

Expected RED: missing v2 argument/constants/functions.

- [ ] **Step 2: Split run version from artifact schema version**

Keep current `FORMAT_VERSION = 1` for existing evidence/review/delivery payloads. Add `RELEASED_RUN_FORMAT_VERSION = 1` and `AGENT_FIRST_RUN_FORMAT_VERSION = 2`. Branch `initialize_run` only after validating the explicit argument. The v1 branch must preserve the current state keys, directories, and transition semantics; the v2 branch initializes revision pointers to `None` and starts at `state="initialized"`.

Do not change existing PPT/Webpage/Video callers.

- [ ] **Step 3: Add canonical page and hint manifests for v2 PDF preparation**

Refactor the already verified Poppler routing rather than duplicate it. For v2 PDF runs:

- all Poppler commands read the immutable copied input;
- page paths are `evidence/pages/page-0001.png`, `page-0002.png`, ...;
- `page-manifest.json` records source hash, renderer=`pdftoppm`, DPI=144, page box=`poppler_default`, rotation=0, dimensions, relative path, and hash;
- `pdfimages-hints.json` records extracted object path/hash/page/object number and `trust="untrusted_hint"`, `eligible=false`;
- successful preparation transitions v2 from `initialized` to `curating` without committing a catalog;
- retry removes only stale uncommitted page/hint outputs and never replaces a ready source.

- [ ] **Step 4: Write RED crop-registry tests**

Add tests using valid portable PNG page bytes for:

- a crop absent from all `pdfimages` hints succeeds;
- request exact-key validation and source/page-manifest/page hash binding;
- normalized-to-pixel conversion uses floor for left/top and ceil for right/bottom;
- identical canonical request returns the same asset/receipt and creates no duplicate event;
- changed bbox/role/claim/reuse creates a distinct append-only asset;
- invalid page/bbox/hash/claim and symlink/hardlink/containment escape fail without outside mutation;
- a noncanonical request file fails at the CLI boundary without dispatching the Mapping-level crop API;
- crash after staged asset write, after receipt write, and between promotion steps is either safely discarded or recovered idempotently;
- two concurrent identical/different crop requests serialize, never publish a partial pair, and never produce duplicate IDs/events;
- `list_source_assets` separates `extraction_hints` from `derived_assets`, and neither is eligible before source review.

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_portable_source_curation_v2.PortableSourceCurationV2Tests.test_agent_can_register_crop_missing_from_pdfimages_hints \
  tests.test_portable_source_curation_v2.PortableSourceCurationV2Tests.test_crop_registry_is_hash_bound_idempotent_and_append_only \
  tests.test_portable_source_curation_v2.PortableSourceCurationV2Tests.test_crop_registry_fails_closed_without_outside_mutation -v
```

Expected RED: crop APIs absent.

- [ ] **Step 5: Implement source inspection/crop/list APIs**

Use stable IDs formed as `src-` plus the first 24 hexadecimal characters of the operation hash. Store pixels in `source-assets/files/{asset_id}.png` and canonical receipts in `source-assets/receipts/{asset_id}.json`. The receipt must bind all fields from the approved design: source/page hashes, page geometry, renderer/DPI/page box/rotation, normalized and pixel bbox, semantic request, output hash, and receipt hash.

Add the reviewed cross-platform advisory `_run_lock` from the candidate branch: a persistent `.run.lock` regular file opened with no-follow/inode/link-count checks, POSIX `fcntl.flock`, and Windows one-byte `msvcrt.locking`. Hold it across read-CAS-stage-promote-state/event operations. Test same-process threads and separate processes. Use a sibling staging directory and exact-set verification before promotion. Never copy or import from outside the verified page registry.

Because the canonical core is vendored into all four Skills but `portable_png.py` is Poster-only, resolve the crop module lazily inside `crop_source`; do not import it at `_portable.py` module load. A v2 crop without the helper is an integrity error, while every v1 PPT/Webpage/Video import/help path remains independent of that file.

- [ ] **Step 6: Run shared and v1 regression suites, then sync**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_portable_source_curation_v2 \
  tests.test_portable_skill_run_state -v
PYTHONDONTWRITEBYTECODE=1 python3 scripts/sync_agent_skill_core.py
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_autodesign_ppt_skill \
  tests.test_autodesign_webpage_skill \
  tests.test_autodesign_video_skill -v
PYTHONDONTWRITEBYTECODE=1 python3 scripts/sync_agent_skill_core.py --check
git diff --check
```

Expected: v2 tests pass and all three non-Poster Skills remain released-v1 green.

- [ ] **Step 7: Review and commit**

Stage only the canonical core, four mechanically synced copies, and the two owned test files. Verify all five core hashes are byte-identical before committing.

```bash
shasum -a 256 agent_skills/_shared/portable_core.py \
  agent_skills/autodesign-{poster,ppt,webpage,video}/scripts/_portable.py
git add agent_skills/_shared/portable_core.py \
  agent_skills/autodesign-{poster,ppt,webpage,video}/scripts/_portable.py \
  tests/test_portable_source_curation_v2.py \
  tests/test_portable_skill_run_state.py
git diff --cached --check
git commit -m "feat(skills): add opt-in Agent-first source runs"
```

---

### Task 3: Add Fresh Source Review and Immutable Catalog Revisions

**Files:**
- Modify: `agent_skills/_shared/portable_core.py`
- Sync: `agent_skills/autodesign-{poster,ppt,webpage,video}/scripts/_portable.py`
- Modify: `tests/test_portable_source_curation_v2.py`
- Create: `agent_skills/autodesign-poster/references/agent-first-source.md`

- [ ] **Step 1: Write RED context-binding tests**

Define the source-review selection as exact keys:

```json
{
  "run_format_version": 2,
  "assets": [
    {
      "asset_id": "src-...",
      "roles": ["method-overview"],
      "max_reuse": 1,
      "importance": "essential"
    }
  ],
  "source_story": {
    "central_method": {
      "status": "covered",
      "asset_ids": ["src-..."],
      "evidence_ids": ["claim-method-01"],
      "rationale": "..."
    },
    "primary_result": {
      "status": "covered",
      "asset_ids": ["src-..."],
      "evidence_ids": ["claim-result-01"],
      "rationale": "..."
    }
  }
}
```

Test that the context binds exact source/page manifests, receipts, previews, selection, current catalog parent, rubric, and context hash. Reject partial preview sets, stale assets, unknown evidence, duplicate assets, invalid roles/reuse, and `not_applicable` without non-empty source-grounded rationale/evidence.

Run the focused test and confirm RED because source-review APIs are missing.

- [ ] **Step 2: Write RED review-schema and catalog-commit tests**

Require exact review keys:

```json
{
  "run_format_version": 2,
  "source_review_context_sha256": "...",
  "reviewer_kind": "fresh_subagent",
  "dimension_scores": {
    "importance": 4,
    "crop_completeness": 4,
    "caption_claim_match": 4,
    "label_axis_legend_readability": 4,
    "duplicate_or_ornamental_content": 4,
    "method_result_coverage": 4,
    "poster_area_fit": 4
  },
  "asset_findings": [],
  "coverage_findings": [],
  "blockers": [],
  "localized_repairs": [],
  "verdict": "pass",
  "complete": true
}
```

Allow `reviewer_kind` only `fresh_subagent` or `host_fresh_pass`. A pass requires all seven finite scores in `[4, 5]` and no blockers. A fail must contain at least one bound finding/blocker and keeps the run in curation.

Test that a passing review atomically creates exactly:

```text
curations/001/catalog.json
curations/001/review.json
curations/001/manifest.json
curations/001/COMMIT.json
```

and compare-and-set updates `active_curation_revision/hash`. Tamper, replay under a changed parent, partial write, unknown extra file, noncanonical JSON, and conflicting orphan revision must fail closed.

- [ ] **Step 3: Implement context/review/catalog APIs**

Adapt the candidate transaction pattern from `db028ff`, but rename paths and reviewer values to the approved design. Context directories are append-only `source-reviews/review-{operation_prefix}-{sequence}/`, where `sequence` is zero-padded to three digits. Review previews are copies of exact crop bytes inside that review directory; they are not new evidence assets. Shared core validates role strings structurally but does not own the Poster role enum; the Poster wrapper added in Task 5 validates every role against `POSTER_SOURCE_ROLES` before calling shared core.

Catalog assets are the only eligible Poster evidence and contain exact receipt hashes, roles, max reuse, importance, and `trust="reviewed"`. Do not calculate or enforce a numeric image count.

- [ ] **Step 4: Document only the source-curation contract**

Write `references/agent-first-source.md` with:

1. direct PDF inspection order;
2. full-page fallback and fresh vision-capable subagent fallback;
3. crop request JSON and coordinate convention;
4. source-review rubric and reviewer separation;
5. central method/primary result coverage;
6. `pdfimages` as hints only;
7. no fixed visual quota and no arbitrary evidence imports.

Do not yet rewrite `SKILL.md`; Task 7 integrates the final command sequence after CLI behavior is proven.

- [ ] **Step 5: Verify, sync, and commit**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_portable_source_curation_v2 -v
PYTHONDONTWRITEBYTECODE=1 python3 scripts/sync_agent_skill_core.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/sync_agent_skill_core.py --check
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_agent_skills.py --root agent_skills
git diff --check
```

Commit only canonical core, four synced copies, the v2 source-curation test, and `agent-first-source.md`.

```bash
git add agent_skills/_shared/portable_core.py \
  agent_skills/autodesign-{poster,ppt,webpage,video}/scripts/_portable.py \
  agent_skills/autodesign-poster/references/agent-first-source.md \
  tests/test_portable_source_curation_v2.py
git diff --cached --check
git commit -m "feat(skills): add reviewed source catalog revisions"
```

---

### Task 4: Add Plan Revisions, Attempt Snapshots, Repair Routing, and Recovery

**Files:**
- Modify: `agent_skills/_shared/portable_core.py`
- Sync: `agent_skills/autodesign-{poster,ppt,webpage,video}/scripts/_portable.py`
- Modify: `tests/test_portable_source_curation_v2.py`
- Create: `tests/test_autodesign_poster_agent_first_v2.py`

- [ ] **Step 1: Write RED immutable-plan and attempt-binding tests**

Test that `save_plan_revision` requires state `curated`, binds the active catalog revision/hash, and creates exactly `plans/NNN/{plan.json,manifest.json,COMMIT.json}`. The generic filename keeps shared core artifact-agnostic; the payload still declares `artifact_type="poster"`. A replay of identical canonical plan bytes is idempotent; different bytes create the next revision only after an authorized replan state.

Test `begin_attempt` snapshots:

- source-manifest hash;
- catalog revision/hash and `catalog-snapshot.json`;
- plan revision/hash and `plan-snapshot.json`;
- exact authorized asset IDs/hashes;
- parent attempt and supersession-ledger prefix hash.

Attempt 01 must continue validating against revision 1 after Attempt 02 uses revision 2. Source curation and planning must not increment `attempt_count`.

- [ ] **Step 2: Write RED repair-route tests**

Use the strict order:

```python
REPAIR_ROUTE_ORDER = {
    "layout_repair": 0,
    "content_replan": 1,
    "source_reingest": 2,
}
```

The Poster semantic review adds exact `repair_route` and `route_findings`. Each finding contains `finding_id`, `code`, `minimum_route`, `block_id`, and `message`. A failing review must choose a route at least as strong as every finding; escalation is valid, downgrade is invalid.

Use this Poster-owned minimum-route table; shared core knows only route ordering:

```python
POSTER_FINDING_MINIMUM_ROUTE = {
    "dom_overflow": "layout_repair",
    "dom_clipping": "layout_repair",
    "dom_overlap": "layout_repair",
    "dom_blank_band": "layout_repair",
    "typography": "layout_repair",
    "visual_balance": "layout_repair",
    "narrative_hierarchy": "content_replan",
    "claim_selection": "content_replan",
    "section_allocation": "content_replan",
    "evidence_area_mismatch": "content_replan",
    "key_visual_missing": "source_reingest",
    "wrong_visual": "source_reingest",
    "incomplete_crop": "source_reingest",
    "fragmentary_crop": "source_reingest",
    "unreadable_source_visual": "source_reingest",
    "caption_claim_mismatch": "source_reingest",
}
```

A passing review stores `repair_route: null` and no route findings/blockers. A failing review requires a non-null route and at least one bound route finding or blocker. Environment/runtime failures never enter this semantic code table.

Test:

- layout failure begins a new attempt using the same catalog/plan;
- content failure requires `reopen_curation`, keeps catalog, and commits a new plan before another attempt;
- missing/wrong/fragmentary/unreadable key visual requires source reingest, new source review, new catalog, then new plan;
- runtime/browser/Poppler/export failure preserves the active attempt and resumes with `retry_current_attempt` rather than consuming an attempt;
- the strongest simultaneous finding wins.

- [ ] **Step 3: Implement generic revision/recovery APIs in shared core**

Port the reviewed compare-and-set transaction pattern for `curations`, `plans`, and the append-only hash-chained `provenance/supersessions.jsonl`. Shared core validates generic route ordering and revision ancestry; Poster-specific finding-code-to-minimum-route mapping stays in `poster_harness.py` in Task 5.

For every transaction crash point—after staging write, after revision rename, after run pointer write, after review write—`resume_run` must either complete one unambiguous commit or delete incomplete staging. It must reject conflicting complete bytes, multiple orphan revisions, parent mismatch, ledger truncation, and ledger rewrite.

- [ ] **Step 4: Make v2 resume name exactly one next action**

Cover these stable values in a table-driven test:

```text
prepare_source
inspect_source
curate_source
source_review
plan
author
retry_current_attempt
dom_audit
validate
semantic_review
reopen_curation
finalize
complete
resolve_blocker
```

Keep the current v1 `resume_run` result mapping unchanged.

- [ ] **Step 5: Verify all crash windows and v1 regression**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_portable_source_curation_v2 \
  tests.test_autodesign_poster_agent_first_v2 \
  tests.test_portable_skill_run_state -v
PYTHONDONTWRITEBYTECODE=1 python3 scripts/sync_agent_skill_core.py
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_autodesign_ppt_skill \
  tests.test_autodesign_webpage_skill \
  tests.test_autodesign_video_skill -v
git diff --check
```

- [ ] **Step 6: Review and commit**

Commit only the shared core, four synced copies, and the two v2 test files:

```bash
git add agent_skills/_shared/portable_core.py \
  agent_skills/autodesign-{poster,ppt,webpage,video}/scripts/_portable.py \
  tests/test_portable_source_curation_v2.py \
  tests/test_autodesign_poster_agent_first_v2.py
git diff --cached --check
git commit -m "feat(skills): add revision-bound Poster attempts"
```

---

### Task 5: Integrate the Agent-First Lifecycle into Poster CLI and Validation

**Files:**
- Modify: `agent_skills/autodesign-poster/scripts/poster_harness.py`
- Modify: `tests/test_autodesign_poster_skill.py`
- Modify: `tests/test_autodesign_poster_agent_first_v2.py`

- [ ] **Step 1: Write RED CLI-contract tests**

Assert `--help` exposes:

```text
doctor init evidence inspect-source crop-source list-source-assets
source-review-context record-source-review plan begin-attempt dom-audit
validate review-context record-review reopen-curation finalize resume diagnose-v1
```

For v2, remove `bind-visuals` from the parser and help output. `diagnose-v1` is the only legacy-run command; do not keep two catalog transition paths.

Define arguments:

```text
inspect-source       --run-dir RUN
crop-source          --run-dir RUN --request REQUEST.json
list-source-assets   --run-dir RUN
source-review-context --run-dir RUN --selection SELECTION.json
record-source-review --run-dir RUN --context RUN-relative-context --review REVIEW.json
reopen-curation      --run-dir RUN --request REQUEST.json
dom-audit            --run-dir RUN --attempt NN [--cache-root PATH] [--offline-browser]
diagnose-v1          --run-dir RUN
```

Every command must emit one JSON object and return 2 for a valid blocked/failed state, 1 for contract/integrity/runtime exceptions, and 0 only for a completed successful command.

For `--request`, `--selection`, `--review`, `--plan`, and `reopen-curation --request`, read bytes first, parse once, and require equality with the shared canonical JSON serializer before dispatch. This is the CLI boundary that makes the noncanonical-request test in Task 2 enforceable; the Mapping-level Python APIs validate schemas and hashes but do not claim to remember original JSON whitespace.

- [ ] **Step 2: Make Poster initialization the sole v2 opt-in**

Change `initialize_poster_run` to call:

```python
core.initialize_run(
    run_dir,
    SKILL_ROOT,
    release_version=release_version,
    archive_sha256=archive_sha256,
    run_format_version=core.AGENT_FIRST_RUN_FORMAT_VERSION,
)
```

Retain `--reference` as style-only input. Reject `--asset` as v2 paper evidence with a clear instruction to derive a crop from the PDF. Keep non-PDF text/Markdown ingestion source-grounded but without crop capability.

- [ ] **Step 3: Replace catalog-floor planning with reviewed-catalog planning**

Remove `_visual_coverage_requirement` and the 5–8 deterministic target. `save_poster_plan` must call `core.save_plan_revision` and validate:

- every `visual_allocations[].visual_id` exists in the active reviewed catalog;
- allocation count per asset does not exceed `max_reuse`;
- role is permitted by both plan and catalog;
- central method and primary result are covered by catalog `source_story`, or have reviewed `not_applicable` rationale;
- zero visuals are allowed only when the reviewed catalog records both relevant categories as not applicable;
- original source evidence is retained when a native explanatory diagram/table is added.

Do not enforce 4–7 as a gate; mention it only as design guidance in documentation.

- [ ] **Step 4: Stage only attempt-authorized assets**

`begin_poster_attempt` must load the attempt-bound plan/catalog snapshots, copy only referenced reviewed assets to `attempts/NN/artifact/assets/`, and create an authoring context containing relative PDF/page/crop paths, receipt hashes, plan roles, reuse limits, source-flow guidance, and the exact next command. Reject unreferenced, missing, hash-mismatched, or unsupported asset types.

- [ ] **Step 5: Bind validation/review/finalize to attempt snapshots**

Change `validate_poster_attempt`, review-context creation, review recording, reopen, resume, and finalization to load `core.load_attempt_plan` and `core.load_attempt_visual_catalog`, never root mutable pointers. Revalidate Poster-specific repair-route findings at record, reopen, resume, and finalize so persisted tampering cannot downgrade a route.

Keep all released hard gates green: one physical PDF page, editable HTML/native tables, exact local-asset closure, source map, claims/numbers, identity-only header, typography, browser/network isolation, and exact final allowlist.

- [ ] **Step 6: Run focused lifecycle tests**

Add a complete synthetic lifecycle:

```text
init v2 -> prepare PDF -> inspect -> crop -> source context -> pass source review
-> plan revision 1 -> attempt 01 -> validate -> fail source fragment
-> reopen source_reingest -> replacement crop -> source review revision 2
-> plan revision 2 -> attempt 02 -> validate -> pass review -> finalize
```

Assert Attempt 01 bytes/snapshots remain unchanged and final contains only Attempt 02 reviewed artifacts.

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_autodesign_poster_agent_first_v2 \
  tests.test_autodesign_poster_skill -v
PYTHONDONTWRITEBYTECODE=1 python3 -m autodesign.smoke
git diff --check
```

- [ ] **Step 7: Review and commit**

```bash
git add agent_skills/autodesign-poster/scripts/poster_harness.py \
  tests/test_autodesign_poster_skill.py \
  tests/test_autodesign_poster_agent_first_v2.py
git diff --cached --check
git commit -m "feat(skills): integrate Poster Agent-first lifecycle"
```

---

### Task 6: Internalize a Strictly Read-Only Poster DOM Audit

**Files:**
- Create: `agent_skills/autodesign-poster/scripts/poster_dom_audit.py`
- Create: `tests/test_poster_dom_audit.py`
- Modify: `agent_skills/autodesign-poster/scripts/poster_harness.py`
- Modify: `tests/test_autodesign_poster_agent_first_v2.py`
- Review only: `autodesign/tools/paper_poster_renderer.py`
- Review only: `autodesign/util/poster_gate_audit.py`

- [ ] **Step 1: Inventory eligible Harness logic before copying**

Create a local review checklist mapping each adopted measurement to its source function/JS fragment. Eligible: TreeWalker text rectangles, computed visibility/typography, root/element overflow, overlap geometry, images and effective resolution, native table metrics, source-flow gutters/sibling structure, canvas parity, blank bands, panel fill, and boxiness signals.

Explicitly reject every function/path that changes style, position, dimensions, font size, content, or generated CSS. Do not import `autodesign` at runtime.

- [ ] **Step 2: Write RED pure-evaluator tests**

Use hand-authored snapshots to test stable findings for:

```text
poster-dom-root-overflow
poster-dom-text-clipping
poster-dom-text-overlap
poster-dom-viewport-escape
poster-dom-blank-band
poster-dom-sparse-oversized-panel
poster-dom-image-low-effective-resolution
poster-dom-table-overflow
poster-dom-table-text-small
poster-dom-source-flow-gutter
poster-dom-source-flow-sibling
poster-dom-screen-print-mismatch
poster-dom-template-boxiness
```

Each finding must contain `code`, `block_id`, `severity`, `geometry`, `message`, and `suggested_repair_route`. Pure evaluation must be deterministic and contain no write API.

- [ ] **Step 3: Implement the isolated browser probe**

`run_poster_dom_audit` must:

1. verify the v2 run, attempt context, plan, catalog, and `artifact/poster.html`;
2. hash every artifact file and record `artifact_tree_sha256_before`;
3. use existing `setup_browser.ensure_browser_runtime` and an internal isolated worker mode of `poster_dom_audit.py`;
4. deny network using the existing browser worker request policy/Chromium arguments;
5. capture screen and print snapshots, screenshot(s), and sanitized console/request errors;
6. evaluate both snapshots through `evaluate_dom_snapshot`;
7. write only `attempts/NN/qa/dom-audit.json` and `qa/previews/dom-*.png` atomically;
8. hash the artifact tree again, require equality, and report `artifact_unchanged=true`.

Do not call `element.style=`, `setAttribute` on the authored document, `page.evaluate` code that mutates layout, or any auto-fit helper.

- [ ] **Step 4: Write opt-in real Chromium RED/GREEN tests**

Under `AUTODESIGN_SKILL_REAL_BROWSER=1`, test:

- one valid dense editable Poster passes;
- clipping, overlap, internal blank band, low-resolution image, overflowing table, bad source-flow gutter, and screen/print mismatch each produce the expected finding;
- an audit of a deliberately writable artifact still leaves every artifact byte/hash unchanged;
- a symlinked QA output or hardlinked artifact fails without external mutation;
- remote requests/popups/workers are blocked and sanitized.

Use `AUTODESIGN_SKILL_BROWSER_CACHE` to reuse the exact-pinned runtime after one verified install.

- [ ] **Step 5: Make standalone `dom-audit` and final `validate` share one engine**

`validate_poster_attempt` must consume the same persisted/verified report or call `run_poster_dom_audit`; it must not duplicate thresholds or a second JS probe. DOM blockers join deterministic findings and route to `layout_repair` unless Poster policy raises them to a stronger route.

- [ ] **Step 6: Verify and commit**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_poster_dom_audit -v
AUTODESIGN_SKILL_REAL_BROWSER=1 \
AUTODESIGN_SKILL_BROWSER_CACHE="$HOME/.cache/autodesign-skills/browser" \
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_poster_dom_audit \
  tests.test_autodesign_poster_agent_first_v2 -v
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_agent_skills.py --root agent_skills
git diff --check
git add agent_skills/autodesign-poster/scripts/poster_dom_audit.py \
  agent_skills/autodesign-poster/scripts/poster_harness.py \
  tests/test_poster_dom_audit.py \
  tests/test_autodesign_poster_agent_first_v2.py
git diff --cached --check
git commit -m "feat(skills): add read-only Poster DOM audit"
```

Commit only the new DOM module/tests and the Poster harness/test integration.

---

### Task 7: Rewrite Poster Skill Instructions and Prove Portable Packaging

**Files:**
- Modify: `agent_skills/autodesign-poster/SKILL.md`
- Modify: `agent_skills/autodesign-poster/references/agent-first-source.md`
- Modify: `agent_skills/autodesign-poster/references/output-contract.md`
- Modify: `agent_skills/autodesign-poster/references/review-rubric.md`
- Modify: `tests/test_portable_agent_skill_packages.py`
- Modify: `tests/test_portable_agent_skill_packaging.py`
- Modify: `tests/test_autodesign_poster_agent_first_v2.py`

- [ ] **Step 1: Write RED documentation-contract tests**

Assert `SKILL.md` teaches this order without embedding the full contracts:

```text
doctor -> init -> inspect source PDF/pages -> crop/list -> fresh source review
-> immutable plan -> attempt -> read-only DOM audit -> validate
-> fresh artifact review -> route/revise -> finalize
```

Assert it says:

- PDF/page renders are primary;
- `pdfimages` are hints only;
- there is no mandatory image-count quota;
- Agent/subagent owns semantic selection;
- scripts never edit the Poster;
- fresh source review precedes plan;
- repair route may escalate but not downgrade;
- v1 diagnose is read-only;
- the full Harness remains stronger and the Skill does not replace it.

- [ ] **Step 2: Rewrite instructions with progressive disclosure**

Keep `SKILL.md` concise and command-oriented. Put crop/review JSON plus Poster-v2 source/provenance rules in `agent-first-source.md`, final HTML/PDF/DOM contracts in `output-contract.md`, and the fresh semantic rubric/routes in `review-rubric.md`. Leave the synchronized generic `source-grounding.md` unchanged in this task; modifying only the Poster copy would violate `sync_agent_skill_core.py --check`.

Use portable launcher discovery in the order `python3`, `python`, Windows `py -3`. Do not hardcode Attempt 01; always obtain the active attempt/revision from command output or `resume`.

- [ ] **Step 3: Verify a read-only installed package outside the repository**

Build a deterministic test release twice:

```bash
RELEASE_A="$(mktemp -d /tmp/autodesign-poster-v2-release-a.XXXXXX)"
RELEASE_B="$(mktemp -d /tmp/autodesign-poster-v2-release-b.XXXXXX)"
python3 -I scripts/package_agent_skills.py build \
  --source-root agent_skills \
  --output-dir "$RELEASE_A" \
  --version 0.2.0-rc1
python3 -I scripts/package_agent_skills.py build \
  --source-root agent_skills \
  --output-dir "$RELEASE_B" \
  --version 0.2.0-rc1
diff -qr "$RELEASE_A" "$RELEASE_B"
```

Install the Poster archive with the generated checksum into a fresh temporary host root using the release-local installer, chmod the installed tree read-only, and run `--help`, `doctor` without `--install-browser`, `init`, `inspect-source`, `resume`, and `diagnose-v1` from an unrelated working directory with `PYTHONPATH`/`PYTHONHOME` unset. Hash the installed tree before/after and require equality and zero `__pycache__`/`.pyc`.

```bash
INSTALL_ROOT="$(mktemp -d /tmp/autodesign-poster-v2-install.XXXXXX)"
python3 -I "$RELEASE_A/package_agent_skills.py" install \
  --archive "$RELEASE_A/autodesign-poster-0.2.0-rc1.zip" \
  --checksum "$RELEASE_A/autodesign-poster-0.2.0-rc1.zip.sha256" \
  --destination "$INSTALL_ROOT"
chmod -R a-w "$INSTALL_ROOT/autodesign-poster"
```

Create all mutable run/output/cache paths as siblings outside `$INSTALL_ROOT`; never relax permissions to make a test pass.

- [ ] **Step 4: Run the full package and cross-Skill regression matrix**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_portable_agent_skill_packages \
  tests.test_portable_agent_skill_packaging \
  tests.test_portable_skill_run_state \
  tests.test_portable_source_curation_v2 \
  tests.test_autodesign_poster_agent_first_v2 \
  tests.test_poster_dom_audit \
  tests.test_autodesign_poster_skill \
  tests.test_autodesign_ppt_skill \
  tests.test_autodesign_webpage_skill \
  tests.test_autodesign_video_skill -v
PYTHONDONTWRITEBYTECODE=1 python3 -m autodesign.smoke
PYTHONDONTWRITEBYTECODE=1 python3 scripts/sync_agent_skill_core.py --check
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_agent_skills.py --root agent_skills
git diff --check
```

- [ ] **Step 5: Review and commit**

```bash
git add agent_skills/autodesign-poster/SKILL.md \
  agent_skills/autodesign-poster/references/agent-first-source.md \
  agent_skills/autodesign-poster/references/output-contract.md \
  agent_skills/autodesign-poster/references/review-rubric.md \
  tests/test_portable_agent_skill_packages.py \
  tests/test_portable_agent_skill_packaging.py \
  tests/test_autodesign_poster_agent_first_v2.py
git diff --cached --check
git commit -m "docs(skills): teach Poster Agent-first workflow"
```

---

### Task 8: Run the Three-Paper Acceptance Matrix and Independent Review

**Files:**
- Create after acceptance: `docs/superpowers/reports/2026-08-17-poster-agent-first-v2-acceptance.md`
- Do not commit: `/Users/yaxinluo/.codex/artifacts/autodesign-poster-agent-first-v2-acceptance/**`

- [ ] **Step 1: Freeze the exact acceptance corpus**

Use these immutable inputs and verify hashes before any generation:

```text
/Users/yaxinluo/Documents/Codex/2026-07-28/zhe/work/overleaf-paper-6a1935d7660a456de5934efd-source-b78bbff/output/pdf/paper.pdf
  sha256 f466939ffcd693dfc951df40ad09bb3233b68e39312f3526a58aeda6824b0daa
  pages 28

/Users/yaxinluo/Desktop/Projects/Any2Poster/Paper2Poster/Paper2Poster-data/2020-denoising-diffusion-probabilistic-models/paper.pdf
  sha256 aee5e07a802e8dfd2a386374c94fd61d1d056cb7e1e0fec4f28e8120ff5d8505
  pages 25

/Users/yaxinluo/Desktop/Projects/OpenDesign/data/external_dataset/icml2024_underwater_sam/paper.pdf
  sha256 72a0900dcd83b30d281e9e670dacffdfe4a488a30dd0a096ec8fdfcb547a2363
  pages 15
```

Create `/Users/yaxinluo/.codex/artifacts/autodesign-poster-agent-first-v2-acceptance/acceptance-corpus.json` with each paper's ID, path, hash, page count, category, and a human-approved `must_have_visuals` list. Every must-have entry contains `page`, normalized `bbox`, `role`, and `rationale`.

**Blocking checkpoint:** render page contact sheets and obtain human approval of this manifest before running either Skill or Harness generation. Do not infer or revise must-have visuals after seeing outputs.

- [ ] **Step 2: Install the release candidate into an isolated Codex home**

Build/install the exact branch commit into:

```text
/Users/yaxinluo/.codex/artifacts/autodesign-poster-agent-first-v2-acceptance/codex-home/skills/autodesign-poster
```

Record branch commit, archive SHA-256, installer checksum, `codex --version`, browser runtime state/hash, Poppler versions, and source hashes in `environment.json`. Do not reuse the repository package directly.

Authenticate the isolated home interactively with `CODEX_HOME="$ACCEPTANCE_ROOT/codex-home" codex login`; do not copy, print, hash, or commit an existing auth file. Verify `codex login status` succeeds before any paid generation.

- [ ] **Step 3: Run one native Codex trajectory per paper**

For each paper, use an empty non-repository workspace and a separate run/output directory. Copy its immutable input to `$ACCEPTANCE_ROOT/work/$paper_id/paper.pdf`, verify the copy against the frozen corpus hash, then invoke Codex in workspace-write mode with the installed `$autodesign-poster` Skill. Instruct it to use that exact `paper.pdf`, complete the entire Agent-first lifecycle, and avoid AutoDesign server/product imports.

Use the reproducible CLI shape:

```bash
paper_id="autodesign"
CODEX_HOME="$ACCEPTANCE_ROOT/codex-home" \
codex exec --sandbox workspace-write --approve-for-me --skip-git-repo-check \
  --cd "$ACCEPTANCE_ROOT/work/$paper_id" --json \
  --output-last-message "$ACCEPTANCE_ROOT/work/$paper_id/last-message.txt" \
  'Use $autodesign-poster to turn paper.pdf into a dense, editable, source-grounded conference poster. Inspect the PDF directly, perform fresh source review, iterate through every required validation/review route, and finalize only a passing artifact.' \
  > "$ACCEPTANCE_ROOT/work/$paper_id/trajectory.jsonl"
```

Preserve the Skill run, crops/receipts, source reviews, catalog/plan revisions, attempts, QA, final HTML/PDF, and trajectory. A CLI success without all required artifacts is failure.

- [ ] **Step 4: Exercise source-reingest deliberately**

For the designated fragmented paper, first register a deliberately incomplete but source-derived crop and run source review. Require a failing finding that routes to `source_reingest`; then register the complete replacement region, commit catalog/plan revision 2, and complete Attempt 02. Verify Attempt 01 and all revision-1 bytes remain unchanged.

- [ ] **Step 5: Run one current DeepSeek Harness trajectory**

Upgrade the existing DSH installation with its official `dsh upgrade` command, record `dsh --version`, install the same checksum-verified Poster Skill into a fresh DSH discovery root, and run one representative paper with:

```bash
dsh -p 'Use the installed autodesign-poster Skill to turn the supplied PDF into a finalized source-grounded poster. Follow the Agent-first source-review and read-only DOM-audit lifecycle.'
```

Run from a clean workspace containing only the PDF and run/output directories. If current DSH cannot discover, execute, or visually inspect the installed Skill/PDF, record the exact blocker and do not claim DSH acceptance.

- [ ] **Step 6: Blindly compare Skill and full Harness outputs**

For each paper, pair the v2 Skill final with a current full AutoDesign Harness final generated from the same frozen PDF and Poster preset. Reuse an existing Harness final only when its run manifest proves the same source SHA-256 and current Harness commit; otherwise run the full Harness once and preserve its run ID, commit, logs, final HTML/PDF, and preview. Rename each pair A/B with a freshly randomized mapping and ask a fresh reviewer who does not know generator identity to score these seven dimensions from 1–5:

Use the current repository Harness command shape and capture its printed run ID:

```bash
mkdir -p "$ACCEPTANCE_ROOT/harness/$paper_id"
uv --cache-dir .uv-cache run python -m autodesign.cli run \
  --from-file "$paper_pdf" \
  --template cvpr-landscape \
  --designer-author external \
  --designer-author-harness codex \
  --designer-author-max-attempts 4 \
  'Create a dense, editable, source-grounded academic conference poster. Preserve the paper identity, central method, primary results, and original evidence.' \
  2>&1 | tee "$ACCEPTANCE_ROOT/harness/$paper_id/command.log"
```

Before treating the Harness command as success, require its final HTML, one-page PDF, preview, attempt history, and terminal event; a zero exit code alone is insufficient.

```text
evidence_selection
information_hierarchy
typography
visual_balance
professionalism
anti_template_quality
editability
```

Calculate per paper `Skill mean / Harness mean`; require median ratio across three papers `>= 0.75`. Independently require:

- zero invented claims, numbers, logos, or evidence;
- must-have visual recall `>= 0.80` per corpus and no major wrong visual;
- at least one correct complete PDF-region asset missing/fragmented in `pdfimages` hints;
- central method and primary result covered or reviewed not-applicable;
- one physical PDF page and editable HTML;
- no clipped/overlapping/unreadable body content;
- DOM audit before/after artifact hashes identical.

Any hard failure blocks merge regardless of mean score.

- [ ] **Step 7: Serve a local acceptance gallery**

Generate a static gallery under `$ACCEPTANCE_ROOT/gallery` containing source-page/crop receipts, revision timeline, Attempt 01/02 previews, final Skill/Harness A/B images, scores, and hard-gate results. Choose and record a free loopback port, then serve the nearest common root:

```bash
PORT="$(python3 - <<'PY'
import socket
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
PY
)"
python3 -m http.server "$PORT" --bind 127.0.0.1 \
  --directory "$ACCEPTANCE_ROOT"
```

In another shell, use `curl -fsS -o /dev/null -w '%{http_code}\n'` to require HTTP 200 for the gallery HTML plus representative CSS, poster preview, crop, and PDF URLs before sharing `http://127.0.0.1:$PORT/gallery/`.

- [ ] **Step 8: Write the acceptance report and request independent code review**

Create `docs/superpowers/reports/2026-08-17-poster-agent-first-v2-acceptance.md` with:

- exact branch commit/tree;
- test/package/install commands and outcomes;
- corpus hashes and frozen-manifest hash;
- per-paper must-have recall and hard gates;
- blind scores and median ratio;
- DSH result or explicit blocker;
- known limitations;
- gallery URL as local evidence only, not a durable public link.

Do not include credentials or machine-specific absolute output paths in the committed report; identify preserved local evidence by root hash and paper ID.

Ask an independent reviewer to inspect the complete branch diff against `main`, reproduce the focused deterministic suite, inspect all three rendered finals/contact sheets, and verify the report calculations. Fix every actionable finding with a new RED test and a scoped follow-up commit.

- [ ] **Step 9: Run final clean-tree verification and commit the report**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_portable_png \
  tests.test_portable_source_curation_v2 \
  tests.test_autodesign_poster_agent_first_v2 \
  tests.test_poster_dom_audit \
  tests.test_autodesign_poster_skill \
  tests.test_portable_skill_run_state \
  tests.test_portable_agent_skill_packages \
  tests.test_portable_agent_skill_packaging \
  tests.test_autodesign_ppt_skill \
  tests.test_autodesign_webpage_skill \
  tests.test_autodesign_video_skill -v
PYTHONDONTWRITEBYTECODE=1 python3 -m autodesign.smoke
PYTHONDONTWRITEBYTECODE=1 python3 scripts/sync_agent_skill_core.py --check
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_agent_skills.py --root agent_skills
git diff --check
git status --short
```

Force-add the ignored documentation report only after all acceptance gates pass:

```bash
git add -f docs/superpowers/reports/2026-08-17-poster-agent-first-v2-acceptance.md
git diff --cached --check
git commit -m "test(skills): document Poster v2 acceptance"
```

Expected final state: clean worktree, Poster v2 accepted, all three other Skills still v1 and green, no release/tag/PR created.

## Final Review Checklist

- [ ] Every changed production line traces to Agent-first source curation, revision safety, Poster integration, or read-only DOM measurement.
- [ ] No stale-branch merge commit or copied product-runtime dependency entered the package.
- [ ] Default `initialize_run` behavior remains v1; only Poster passes v2 explicitly.
- [ ] `pdfimages` hints never authorize an asset or plan.
- [ ] No deterministic visual-count floor remains.
- [ ] Catalog and plan revisions are immutable, canonical, parent-bound, and crash-recoverable.
- [ ] Attempts load their own snapshots, not active root pointers.
- [ ] Repair escalation/downgrade and runtime same-attempt behavior are tested.
- [ ] DOM audit and final validation use the same engine and prove artifact immutability.
- [ ] Installed package stays byte-identical/read-only in isolated lifecycle tests.
- [ ] Three-paper must-have recall, blind ratio, and hard gates pass before merge readiness.
- [ ] PPT, Webpage, and Video behavior remains released run format 1.
