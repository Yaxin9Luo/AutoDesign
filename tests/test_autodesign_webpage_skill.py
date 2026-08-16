from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "agent_skills" / "autodesign-webpage"
HARNESS_PATH = SKILL_ROOT / "scripts" / "webpage_harness.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("autodesign_webpage_harness", HARNESS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load webpage harness")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_text() -> str:
    return """# Atlas: Evidence-Aware Research Communication
Atlas is authored by Ada Researcher and Ben Scientist. Atlas turns one source paper into an evidence-aware research page.

## Abstract
Atlas keeps scientific claims connected to stable evidence anchors while preserving editable native text and local visuals.

## Method
The method has three stages: plan the research story, compose source-bound sections, and validate the rendered page.

## Results
On the synthetic evaluation, Atlas improved evidence coverage from 62% to 91% while keeping every claim traceable.

## Limitations
The synthetic evaluation is small, browser coverage is limited to Chromium, and the fixture does not establish generalization.

## Resources
The paper resource is https://example.org/atlas-paper and no code or dataset URL is provided.

## Citation
Citation metadata, venue, affiliation, date, code URL, dataset URL, and license are not provided in this source fixture.
"""


def _svg() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" width="900" height="360" viewBox="0 0 900 360">
<rect width="900" height="360" fill="#f5f0e8"/><path d="M120 180H780" stroke="#26314d" stroke-width="8"/>
<circle cx="160" cy="180" r="70" fill="#d35b38"/><circle cx="450" cy="180" r="70" fill="#e9d7b8"/>
<circle cx="740" cy="180" r="70" fill="#26314d"/></svg>"""


def _claims() -> list[dict[str, object]]:
    return [
        {
            "id": "claim-title",
            "text": "Atlas: Evidence-Aware Research Communication",
            "source_ids": ["ev-001"],
        },
        {
            "id": "claim-thesis",
            "text": "Atlas turns one source paper into an evidence-aware research page.",
            "source_ids": ["ev-001"],
        },
        {
            "id": "claim-abstract",
            "text": "Atlas keeps scientific claims connected to stable evidence anchors.",
            "source_ids": ["ev-002"],
        },
        {
            "id": "claim-method",
            "text": "The method plans the research story, composes source-bound sections, and validates the rendered page.",
            "source_ids": ["ev-003"],
        },
        {
            "id": "claim-results",
            "text": "Atlas improved evidence coverage from 62% to 91% on the synthetic evaluation.",
            "source_ids": ["ev-004"],
        },
        {
            "id": "claim-limitations",
            "text": "The synthetic evaluation is small and browser coverage is limited to Chromium.",
            "source_ids": ["ev-005"],
        },
        {
            "id": "claim-resource",
            "text": "The paper resource is https://example.org/atlas-paper.",
            "source_ids": ["ev-006"],
        },
    ]


def _plan() -> dict[str, object]:
    return {
        "format_version": 1,
        "artifact_type": "research_webpage",
        "brief": "Create an editorial research project page for a technical audience.",
        "title_claim_id": "claim-title",
        "thesis_claim_id": "claim-thesis",
        "sections": [
            {"id": "identity", "role": "identity", "claim_ids": ["claim-title", "claim-thesis"]},
            {"id": "abstract", "role": "abstract", "claim_ids": ["claim-abstract"]},
            {"id": "method", "role": "method", "claim_ids": ["claim-method"]},
            {"id": "evidence", "role": "evidence", "claim_ids": ["claim-method"]},
            {"id": "results", "role": "results", "claim_ids": ["claim-results"]},
            {"id": "limitations", "role": "limitations", "claim_ids": ["claim-limitations"]},
            {"id": "resources", "role": "resources", "claim_ids": ["claim-resource"]},
            {"id": "citation", "role": "citation", "claim_ids": []},
        ],
        "visual_allocations": [{"visual_id": "vis-001", "role": "overview"}],
        "interactions": [
            {
                "id": "inspect-method",
                "kind": "inspect",
                "claim_ids": ["claim-method"],
                "visual_ids": ["vis-001"],
                "control_id": "inspect-method-control",
                "target_id": "method-figure",
                "state_attribute": "aria-pressed",
            }
        ],
        "resource_links": [
            {
                "label": "Paper",
                "url": "https://example.org/atlas-paper",
                "source_ids": ["ev-006"],
            }
        ],
        "missing_metadata": [
            "affiliations",
            "venue",
            "date",
            "code_url",
            "data_url",
            "citation",
            "license",
        ],
        "max_attempts": 4,
    }


def _valid_html(asset_path: str = "assets/vis-001.svg") -> str:
    filler = (
        "The page keeps native text, evidence identifiers, and an inspectable narrative close "
        "to the supporting source. Each section states one job and avoids decorative repetition. "
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Atlas: Evidence-Aware Research Communication</title>
  <style>
    :root {{ --ink:#182033; --paper:#f7f3ea; --accent:#c65332; }}
    * {{ box-sizing:border-box; }} html {{ scroll-behavior:smooth; }}
    body {{ margin:0; color:var(--ink); background:var(--paper); font:17px/1.6 Arial,sans-serif; }}
    a:focus-visible, button:focus-visible {{ outline:3px solid var(--accent); outline-offset:4px; }}
    nav, main, footer {{ width:min(1120px,calc(100% - 32px)); margin:auto; }}
    nav {{ display:flex; gap:18px; padding:18px 0; flex-wrap:wrap; }}
    section, header {{ padding:64px 0; border-top:1px solid #bbb; }}
    header {{ min-height:70vh; display:grid; align-content:center; }}
    h1 {{ font:700 clamp(44px,7vw,76px)/1.05 Georgia,serif; max-width:15ch; }}
    h2 {{ font:600 clamp(30px,4vw,44px)/1.1 Georgia,serif; }}
    figure {{ margin:28px 0; padding:20px; border:2px solid transparent; }}
    figure.is-inspected {{ border-color:var(--accent); }} figure img {{ width:100%; height:auto; display:block; }}
    button {{ padding:12px 16px; border:1px solid currentColor; background:transparent; color:inherit; }}
    .resource-list {{ display:flex; gap:12px; flex-wrap:wrap; }}
    [data-icon] {{ width:20px; height:20px; vertical-align:middle; }}
    @media (max-width:720px) {{ header {{ min-height:auto; }} section,header {{ padding:42px 0; }} }}
    @media (prefers-reduced-motion:reduce) {{ *,*::before,*::after {{ animation:none!important; transition:none!important; scroll-behavior:auto!important; }} }}
  </style>
</head>
<body>
<a href="#main" class="skip-link">Skip to research content</a>
<nav aria-label="Research sections">
  <a href="#method"><svg data-icon aria-hidden="true" viewBox="0 0 20 20"><path d="M2 10h16"/></svg>Method</a>
  <a href="#results"><svg data-icon aria-hidden="true" viewBox="0 0 20 20"><path d="M2 16L8 8l4 4 6-9"/></svg>Results</a>
  <a href="#resources"><svg data-icon aria-hidden="true" viewBox="0 0 20 20"><circle cx="10" cy="10" r="7"/></svg>Resources</a>
</nav>
<main id="main">
  <header id="identity" data-section-role="identity">
    <p>Research project page</p>
    <h1 data-claim-id="claim-title">Atlas: Evidence-Aware Research Communication</h1>
    <p data-thesis-claim-id="claim-thesis" data-claim-id="claim-thesis">Atlas turns one source paper into an evidence-aware research page.</p>
  </header>
  <section id="abstract" data-section-role="abstract"><h2>Abstract</h2><p data-claim-id="claim-abstract">Atlas keeps scientific claims connected to stable evidence anchors.</p><p>{filler}</p></section>
  <section id="method" data-section-role="method"><h2>Method</h2><p data-claim-id="claim-method">The method plans the research story, composes source-bound sections, and validates the rendered page.</p>
    <button id="inspect-method-control" type="button" data-interaction-id="inspect-method" aria-controls="method-figure" aria-pressed="false">Inspect the source method figure</button>
    <figure id="method-figure" data-source-id="vis-001"><img src="{asset_path}" data-source-id="vis-001" alt="Three-stage Atlas method pipeline"><figcaption>Source method overview linked to claim-method.</figcaption></figure>
    <p>{filler}</p></section>
  <section id="evidence" data-section-role="evidence"><h2>Evidence map</h2><p data-claim-id="claim-method">The method plans the research story, composes source-bound sections, and validates the rendered page.</p><p>{filler}</p></section>
  <section id="results" data-section-role="results"><h2>Results</h2><p data-claim-id="claim-results">Atlas improved evidence coverage from 62% to 91% on the synthetic evaluation.</p><table><caption>Source-backed evidence coverage</caption><thead><tr><th>System</th><th>Coverage</th></tr></thead><tbody><tr><td>Baseline</td><td>62%</td></tr><tr><td>Atlas</td><td>91%</td></tr></tbody></table><p>{filler}</p></section>
  <section id="limitations" data-section-role="limitations"><h2>Limitations</h2><p data-claim-id="claim-limitations">The synthetic evaluation is small and browser coverage is limited to Chromium.</p><p>{filler}</p></section>
  <section id="resources" data-section-role="resources"><h2>Resources</h2><p data-claim-id="claim-resource">The paper resource is https://example.org/atlas-paper.</p><div class="resource-list"><a data-resource-link="Paper" href="https://example.org/atlas-paper" rel="noopener">Paper</a></div>
    <p data-missing-metadata="code_url">Code URL was not provided in the source.</p><p data-missing-metadata="data_url">Dataset URL was not provided in the source.</p><p data-missing-metadata="license">License was not provided in the source.</p></section>
  <section id="citation" data-section-role="citation"><h2>Citation</h2><p data-missing-metadata="citation">Citation metadata was not provided in the source.</p><p data-missing-metadata="affiliations">Affiliations were not provided in the source.</p><p data-missing-metadata="venue">Venue was not provided in the source.</p><p data-missing-metadata="date">Publication date was not provided in the source.</p></section>
</main>
<footer><p>Source-grounded local research artifact.</p></footer>
<script>
  const control=document.getElementById('inspect-method-control');
  control.addEventListener('click',()=>{{
    const selected=control.getAttribute('aria-pressed')==='true';
    control.setAttribute('aria-pressed',String(!selected));
    document.getElementById('method-figure').classList.toggle('is-inspected',!selected);
  }});
</script>
</body></html>"""


class WebpageSkillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.harness = _load_harness()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "paper.md"
        self.source.write_text(_source_text(), encoding="utf-8")
        self.visual = self.root / "pipeline.svg"
        self.visual.write_text(_svg(), encoding="utf-8")
        self.run = self.root / "run"
        self.harness.initialize_webpage_run(
            self.run,
            self.source,
            extra_assets=[self.visual],
            release_version="0.1.0-test",
            install_browser=False,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _author_valid_attempt(self) -> str:
        self.harness.save_webpage_plan(self.run, _plan())
        attempt_id = self.harness.begin_webpage_attempt(self.run)
        staged = self.harness.stage_visual(self.run, attempt_id, "vis-001")
        artifact = self.run / "attempts" / attempt_id / "artifact"
        (artifact / "index.html").write_text(_valid_html(staged), encoding="utf-8")
        self.harness.write_webpage_source_map(self.run, attempt_id, _claims())
        return attempt_id

    def test_copied_skill_runs_from_unrelated_cwd_without_mutating_install(self) -> None:
        installed = self.root / "installed" / "autodesign-webpage"
        shutil.copytree(SKILL_ROOT, installed)
        workspace = self.root / "unrelated-workspace"
        workspace.mkdir()
        run = workspace / "run"
        before = {
            path.relative_to(installed).as_posix(): path.read_bytes()
            for path in installed.rglob("*")
            if path.is_file()
        }
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        completed = subprocess.run(
            [
                sys.executable,
                str(installed / "scripts" / "webpage_harness.py"),
                "init",
                "--run-dir",
                str(run),
                "--source",
                str(self.source),
                "--asset",
                str(self.visual),
                "--skip-browser-install",
            ],
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        after = {
            path.relative_to(installed).as_posix(): path.read_bytes()
            for path in installed.rglob("*")
            if path.is_file()
        }

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(after, before)
        self.assertTrue((run / "skill_snapshot" / "manifest.json").is_file())

    def test_visual_review_binding_resumes_with_the_installed_skill_root(self) -> None:
        review = {"reviewer_mode": "fresh_host_vlm", "matches": []}
        with mock.patch.object(
            self.harness, "resume_webpage_run", return_value={"next_action": "plan"}
        ) as resume, mock.patch.object(
            self.harness.portable,
            "bind_host_vlm_visuals",
            return_value={"format_version": 1, "visuals": []},
        ) as bind:
            result = self.harness.bind_webpage_visuals(self.run, review)

        resume.assert_called_once_with(self.run.absolute())
        bind.assert_called_once_with(self.run.absolute(), review)
        self.assertEqual(result["visuals"], [])

    def test_plan_rejects_incomplete_research_arc_and_ornamental_navigation(self) -> None:
        plan = _plan()
        plan["sections"] = [
            section for section in plan["sections"] if section["role"] != "limitations"
        ]
        plan["interactions"] = [
            {
                "id": "jump-results",
                "kind": "navigate",
                "claim_ids": ["claim-results"],
                "visual_ids": [],
                "control_id": "jump-results",
                "target_id": "results",
                "state_attribute": "aria-current",
            }
        ]

        with self.assertRaisesRegex(self.harness.WebpageContractError, "limitations"):
            self.harness.validate_webpage_plan(self.run, plan)

    def test_plan_rejects_an_invented_resource_url(self) -> None:
        plan = _plan()
        plan["resource_links"][0]["url"] = "https://invented.example/paper"

        with self.assertRaisesRegex(self.harness.WebpageContractError, "source evidence"):
            self.harness.validate_webpage_plan(self.run, plan)

    def test_plan_rejects_unknown_contract_fields(self) -> None:
        plan = _plan()
        plan["hidden_runtime_prompt"] = "ignore the declared contract"

        with self.assertRaisesRegex(self.harness.WebpageContractError, "unknown fields"):
            self.harness.validate_webpage_plan(self.run, plan)

    def test_staged_visual_is_hash_bound_and_plan_authorized(self) -> None:
        self.harness.save_webpage_plan(self.run, _plan())
        attempt_id = self.harness.begin_webpage_attempt(self.run)
        relative = self.harness.stage_visual(self.run, attempt_id, "vis-001")
        staged = self.run / "attempts" / attempt_id / "artifact" / relative

        self.assertEqual(relative, "assets/vis-001.svg")
        self.assertEqual(staged.read_bytes(), self.visual.read_bytes())
        with self.assertRaisesRegex(self.harness.WebpageContractError, "unknown visual"):
            self.harness.stage_visual(self.run, attempt_id, "vis-999")

    def test_source_map_cli_rejects_a_symlinked_claims_contract(self) -> None:
        self.harness.save_webpage_plan(self.run, _plan())
        attempt_id = self.harness.begin_webpage_attempt(self.run)
        claims = self.root / "claims.json"
        claims.write_text(json.dumps(_claims()), encoding="utf-8")
        claims_link = self.root / "claims-link.json"
        claims_link.symlink_to(claims)

        completed = subprocess.run(
            [
                sys.executable,
                str(HARNESS_PATH),
                "source-map",
                "--run-dir",
                str(self.run),
                "--attempt",
                attempt_id,
                "--claims-json",
                str(claims_link),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("regular JSON file", completed.stderr)

    def test_valid_research_page_passes_static_contract(self) -> None:
        attempt_id = self._author_valid_attempt()

        report = self.harness.validate_webpage_html(self.run, attempt_id)

        self.assertTrue(report["passed"], report["findings"])
        self.assertEqual(report["metrics"]["required_section_count"], 8)
        self.assertEqual(report["metrics"]["source_grounded_interaction_count"], 1)
        self.assertEqual(report["metrics"]["missing_metadata_count"], 7)

    def test_static_contract_rejects_visible_claim_text_drift_with_a_valid_id(self) -> None:
        attempt_id = self._author_valid_attempt()
        path = self.run / "attempts" / attempt_id / "artifact" / "index.html"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "Atlas improved evidence coverage from 62% to 91% on the synthetic evaluation.",
                "Atlas achieved perfect evidence coverage on every benchmark.",
                1,
            ),
            encoding="utf-8",
        )

        report = self.harness.validate_webpage_html(self.run, attempt_id)

        self.assertIn(
            "claim_text_mismatch",
            {finding["code"] for finding in report["findings"]},
        )

    def test_static_contract_rejects_delayed_script_navigation_before_browser(self) -> None:
        attempt_id = self._author_valid_attempt()
        path = self.run / "attempts" / attempt_id / "artifact" / "index.html"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "</script>",
                "setTimeout(() => { window.location.href = 'https://egress.example/late'; }, 2000);</script>",
                1,
            ),
            encoding="utf-8",
        )

        report = self.harness.validate_webpage_html(self.run, attempt_id)

        self.assertIn(
            "dynamic_navigation_script",
            {finding["code"] for finding in report["findings"]},
        )

    def test_static_contract_rejects_markup_navigation_egress(self) -> None:
        attempt_id = self._author_valid_attempt()
        path = self.run / "attempts" / attempt_id / "artifact" / "index.html"
        html = path.read_text(encoding="utf-8").replace(
            "</head>",
            '<meta http-equiv="refresh" content="2;url=https://egress.example/late"></head>',
            1,
        ).replace(
            "</footer>",
            '<form action="https://egress.example/submit"><button>Send</button></form></footer>',
            1,
        )
        path.write_text(html, encoding="utf-8")

        report = self.harness.validate_webpage_html(self.run, attempt_id)

        self.assertIn(
            "dynamic_navigation_markup",
            {finding["code"] for finding in report["findings"]},
        )

    def test_static_contract_forbids_additional_html_sidecars(self) -> None:
        attempt_id = self._author_valid_attempt()
        artifact = self.run / "attempts" / attempt_id / "artifact"
        (artifact / "supplement.html").write_text(
            '<!doctype html><script src="https://egress.example/payload.js"></script>',
            encoding="utf-8",
        )
        index = artifact / "index.html"
        index.write_text(
            index.read_text(encoding="utf-8").replace(
                "</footer>", '<a href="supplement.html">Supplement</a></footer>', 1
            ),
            encoding="utf-8",
        )

        report = self.harness.validate_webpage_html(self.run, attempt_id)

        self.assertIn(
            "html_sidecar_forbidden",
            {finding["code"] for finding in report["findings"]},
        )

    def test_skill_commands_use_the_attempt_id_returned_by_begin(self) -> None:
        instructions = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertNotIn("--attempt 01", instructions)
        self.assertNotIn("attempts/01", instructions)
        self.assertIn('ATTEMPT_ID="$(python3 "$HARNESS" begin', instructions)
        self.assertIn('--attempt "$ATTEMPT_ID"', instructions)
        self.assertIn('["active_attempt"]', instructions)

    def test_static_contract_requires_the_planned_section_ids(self) -> None:
        attempt_id = self._author_valid_attempt()
        path = self.run / "attempts" / attempt_id / "artifact" / "index.html"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                '<section id="results" data-section-role="results">',
                '<section id="benchmark" data-section-role="results">',
            ),
            encoding="utf-8",
        )

        report = self.harness.validate_webpage_html(self.run, attempt_id)

        self.assertIn(
            "section_id_mismatch",
            {finding["code"] for finding in report["findings"]},
        )

    def test_static_contract_rejects_remote_assets_and_invented_links(self) -> None:
        attempt_id = self._author_valid_attempt()
        path = self.run / "attempts" / attempt_id / "artifact" / "index.html"
        html = path.read_text(encoding="utf-8").replace(
            "</head>", '<script src="https://cdn.example/app.js"></script></head>'
        ).replace(
            "</footer>", '<a href="https://invented.example/demo">Demo</a></footer>'
        )
        path.write_text(html, encoding="utf-8")

        report = self.harness.validate_webpage_html(self.run, attempt_id)
        codes = {finding["code"] for finding in report["findings"]}

        self.assertIn("remote_asset", codes)
        self.assertIn("invented_resource_link", codes)

    def test_static_contract_rejects_remote_assets_in_inline_styles(self) -> None:
        attempt_id = self._author_valid_attempt()
        path = self.run / "attempts" / attempt_id / "artifact" / "index.html"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                '<header id="identity"',
                '<header style="background-image:url(https://cdn.example/hero.png)" id="identity"',
                1,
            ),
            encoding="utf-8",
        )

        report = self.harness.validate_webpage_html(self.run, attempt_id)

        self.assertIn(
            "remote_asset", {finding["code"] for finding in report["findings"]}
        )

    def test_static_contract_closes_link_track_and_inline_svg_dependencies(self) -> None:
        attempt_id = self._author_valid_attempt()
        path = self.run / "attempts" / attempt_id / "artifact" / "index.html"
        html = path.read_text(encoding="utf-8").replace(
            "</head>",
            '<link rel="icon" href="https://cdn.example/icon.svg"></head>',
        ).replace(
            "</footer>",
            '<video><track src="https://cdn.example/captions.vtt"></video>'
            '<svg><image href="https://cdn.example/figure.png"></image></svg></footer>',
        )
        path.write_text(html, encoding="utf-8")

        report = self.harness.validate_webpage_html(self.run, attempt_id)
        rejected = {
            finding.get("value")
            for finding in report["findings"]
            if finding["code"] == "remote_asset"
        }

        self.assertEqual(
            rejected,
            {
                "https://cdn.example/icon.svg",
                "https://cdn.example/captions.vtt",
                "https://cdn.example/figure.png",
            },
        )

    def test_static_contract_rejects_javascript_hidden_core_content(self) -> None:
        attempt_id = self._author_valid_attempt()
        path = self.run / "attempts" / attempt_id / "artifact" / "index.html"
        html = path.read_text(encoding="utf-8").replace(
            "section, header {",
            ".reveal { opacity:0; } section, header {",
        ).replace(
            '<section id="results"', '<section class="reveal" id="results"'
        ).replace(
            "const control=", "const watcher=new IntersectionObserver(()=>{}); const control="
        )
        path.write_text(html, encoding="utf-8")

        report = self.harness.validate_webpage_html(self.run, attempt_id)

        self.assertIn(
            "javascript_reveal_dependency",
            {finding["code"] for finding in report["findings"]},
        )

    def test_static_contract_rejects_motion_without_effective_reduced_motion(self) -> None:
        attempt_id = self._author_valid_attempt()
        path = self.run / "attempts" / attempt_id / "artifact" / "index.html"
        html = path.read_text(encoding="utf-8").replace(
            "@media (prefers-reduced-motion:reduce)",
            "@media (prefers-color-scheme:dark)",
        )
        path.write_text(html, encoding="utf-8")

        report = self.harness.validate_webpage_html(self.run, attempt_id)

        self.assertIn(
            "motion_without_reduced_motion",
            {finding["code"] for finding in report["findings"]},
        )

    def test_static_contract_rejects_unbound_or_non_keyboard_interaction(self) -> None:
        attempt_id = self._author_valid_attempt()
        path = self.run / "attempts" / attempt_id / "artifact" / "index.html"
        html = path.read_text(encoding="utf-8").replace(
            '<button id="inspect-method-control" type="button" data-interaction-id="inspect-method" aria-controls="method-figure" aria-pressed="false">Inspect the source method figure</button>',
            '<div id="inspect-method-control" data-interaction-id="inspect-method" aria-controls="method-figure" aria-pressed="false">Inspect the source method figure</div>',
        )
        path.write_text(html, encoding="utf-8")

        report = self.harness.validate_webpage_html(self.run, attempt_id)
        codes = {finding["code"] for finding in report["findings"]}

        self.assertIn("interaction_not_keyboard_native", codes)

    def test_static_contract_rejects_broken_fragments_missing_alt_and_icon_names(self) -> None:
        attempt_id = self._author_valid_attempt()
        path = self.run / "attempts" / attempt_id / "artifact" / "index.html"
        html = path.read_text(encoding="utf-8").replace(
            '<a href="#method"><svg data-icon aria-hidden="true" viewBox="0 0 20 20"><path d="M2 10h16"/></svg>Method</a>',
            '<a href="#does-not-exist"><svg data-icon viewBox="0 0 20 20"><path d="M2 10h16"/></svg></a>',
        ).replace(
            'alt="Three-stage Atlas method pipeline"', 'alt=""'
        )
        path.write_text(html, encoding="utf-8")

        report = self.harness.validate_webpage_html(self.run, attempt_id)
        codes = {finding["code"] for finding in report["findings"]}

        self.assertIn("broken_internal_link", codes)
        self.assertIn("image_missing_alt", codes)
        self.assertIn("icon_control_missing_name", codes)

    def test_static_contract_rejects_ai_slop_and_generic_marketing_copy(self) -> None:
        attempt_id = self._author_valid_attempt()
        path = self.run / "attempts" / attempt_id / "artifact" / "index.html"
        html = path.read_text(encoding="utf-8").replace(
            "background:var(--paper)",
            "background:linear-gradient(90deg,#fff,#eee)",
        ).replace(
            "Source-grounded local research artifact.",
            "Unlock the power of research. Book a demo and start your free trial.",
        )
        path.write_text(html, encoding="utf-8")

        report = self.harness.validate_webpage_html(self.run, attempt_id)
        codes = {finding["code"] for finding in report["findings"]}

        self.assertIn("decorative_gradient", codes)
        self.assertIn("generic_marketing_copy", codes)

    def test_static_contract_rejects_source_visual_hash_drift(self) -> None:
        attempt_id = self._author_valid_attempt()
        staged = self.run / "attempts" / attempt_id / "artifact" / "assets" / "vis-001.svg"
        staged.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")

        report = self.harness.validate_webpage_html(self.run, attempt_id)

        self.assertIn(
            "source_visual_hash_mismatch",
            {finding["code"] for finding in report["findings"]},
        )

    def test_static_contract_rejects_source_visual_hash_drift_in_srcset(self) -> None:
        attempt_id = self._author_valid_attempt()
        artifact = self.run / "attempts" / attempt_id / "artifact"
        (artifact / "assets" / "drifted.svg").write_text(
            "<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8"
        )
        path = artifact / "index.html"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'src="assets/vis-001.svg"',
                'src="assets/vis-001.svg" srcset="assets/drifted.svg 2x"',
                1,
            ),
            encoding="utf-8",
        )

        report = self.harness.validate_webpage_html(self.run, attempt_id)

        self.assertIn(
            "source_visual_hash_mismatch",
            {finding["code"] for finding in report["findings"]},
        )

    def test_static_contract_rejects_source_visual_reuse_outside_the_plan(self) -> None:
        attempt_id = self._author_valid_attempt()
        path = self.run / "attempts" / attempt_id / "artifact" / "index.html"
        html = path.read_text(encoding="utf-8").replace(
            "</figure>",
            '<img src="assets/vis-001.svg" data-source-id="vis-001" alt="Duplicated method pipeline"></figure>',
            1,
        )
        path.write_text(html, encoding="utf-8")

        report = self.harness.validate_webpage_html(self.run, attempt_id)

        self.assertIn(
            "source_visual_reuse_mismatch",
            {finding["code"] for finding in report["findings"]},
        )

    def test_full_lifecycle_hash_binds_browser_checks_and_final_delivery(self) -> None:
        attempt_id = self._author_valid_attempt()

        def fake_browser(html_path, *, workspace_root, output_dir, **_kwargs):
            self.assertEqual(html_path, workspace_root / "artifact" / "index.html")
            self.assertEqual(output_dir, workspace_root / "qa" / "previews")
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "desktop.png").write_bytes(b"desktop-png")
            (output_dir / "mobile.png").write_bytes(b"mobile-png")
            return {
                "format_version": 1,
                "html": "index.html",
                "viewports": {
                    "desktop": {"passed": True, "screenshot": "desktop.png"},
                    "mobile": {"passed": True, "screenshot": "mobile.png"},
                },
                "blocked_requests": [],
                "missing_local_assets": [],
                "direct_network_attempts": [],
                "passed": True,
            }

        def fake_interactions(**_kwargs):
            return {
                "format_version": 1,
                "passed": True,
                "checks": {
                    "no_javascript_core_visible": True,
                    "keyboard_interactions": True,
                    "reduced_motion_effective": True,
                    "internal_links_resolve": True,
                },
                "interactions": [{"id": "inspect-method", "passed": True}],
            }

        report = self.harness.validate_webpage_attempt(
            self.run,
            attempt_id,
            browser_audit=fake_browser,
            interaction_audit=fake_interactions,
            allow_browser_install=False,
        )
        context = self.harness.create_webpage_review_context(self.run, attempt_id)
        self.assertEqual(context["rubric"]["brief"], _plan()["brief"])
        review = {
            "format_version": 1,
            "attempt_id": attempt_id,
            "review_context_sha256": context["context_sha256"],
            "artifact_hashes": context["artifact_hashes"],
            "preview_hashes": context["preview_hashes"],
            "reviewed_frame_ids": sorted(context["preview_hashes"]),
            "source_manifest_sha256": context["source_manifest_sha256"],
            "source_map_sha256": context["source_map_sha256"],
            "rubric_sha256": context["rubric_sha256"],
            "reviewer_mode": "fresh_host_vlm",
            "dimension_scores": {
                dimension: 4 for dimension in context["rubric"]["dimensions"]
            },
            "blockers": [],
            "localized_repairs": [],
            "verdict": "pass",
            "complete": True,
        }
        self.harness.record_webpage_review(self.run, attempt_id, review)
        manifest = self.harness.finalize_webpage_attempt(self.run, attempt_id)

        self.assertTrue(report["passed"])
        self.assertEqual(set(context["preview_paths"]), {"desktop", "mobile"})
        self.assertEqual(manifest["verification_status"], "verified")
        self.assertTrue((self.run / "final" / "index.html").is_file())
        self.assertTrue((self.run / "final" / "browser-audit.json").is_file())
        self.assertTrue((self.run / "final" / "interaction-audit.json").is_file())
        resumed = self.harness.resume_webpage_run(self.run)
        self.assertEqual(resumed["next_action"], "complete")

    def test_passing_review_requires_publication_quality_scores(self) -> None:
        attempt_id = self._author_valid_attempt()

        def fake_browser(_html, *, output_dir, **_kwargs):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "desktop.png").write_bytes(b"desktop")
            (output_dir / "mobile.png").write_bytes(b"mobile")
            return {
                "format_version": 1,
                "viewports": {
                    "desktop": {"passed": True, "screenshot": "desktop.png"},
                    "mobile": {"passed": True, "screenshot": "mobile.png"},
                },
                "passed": True,
            }

        self.harness.validate_webpage_attempt(
            self.run,
            attempt_id,
            browser_audit=fake_browser,
            interaction_audit=lambda **_kwargs: {"format_version": 1, "passed": True, "checks": {}},
            allow_browser_install=False,
        )
        context = self.harness.create_webpage_review_context(self.run, attempt_id)
        review = {
            "format_version": 1,
            "attempt_id": attempt_id,
            "review_context_sha256": context["context_sha256"],
            "artifact_hashes": context["artifact_hashes"],
            "preview_hashes": context["preview_hashes"],
            "reviewed_frame_ids": sorted(context["preview_hashes"]),
            "source_manifest_sha256": context["source_manifest_sha256"],
            "source_map_sha256": context["source_map_sha256"],
            "rubric_sha256": context["rubric_sha256"],
            "reviewer_mode": "fresh_subagent",
            "dimension_scores": {
                dimension: 2 for dimension in context["rubric"]["dimensions"]
            },
            "blockers": [],
            "localized_repairs": [],
            "verdict": "pass",
            "complete": True,
        }

        with self.assertRaisesRegex(self.harness.WebpageContractError, "quality threshold"):
            self.harness.record_webpage_review(self.run, attempt_id, review)


@unittest.skipUnless(
    os.environ.get("AUTODESIGN_SKILL_REAL_BROWSER") == "1",
    "set AUTODESIGN_SKILL_REAL_BROWSER=1 with an explicit verified browser cache",
)
class WebpageSkillRealBrowserTest(unittest.TestCase):
    @staticmethod
    def _browser_cache() -> Path:
        cache = os.environ.get("AUTODESIGN_SKILL_BROWSER_CACHE", "").strip()
        if not cache:
            raise unittest.SkipTest("AUTODESIGN_SKILL_BROWSER_CACHE must be explicit")
        return Path(cache)

    @staticmethod
    def _author_browser_attempt(root: Path, harness, transform) -> tuple[Path, str, Path]:
        source = root / "paper.md"
        source.write_text(_source_text(), encoding="utf-8")
        visual = root / "pipeline.svg"
        visual.write_text(_svg(), encoding="utf-8")
        run = root / "run"
        harness.initialize_webpage_run(
            run, source, extra_assets=[visual], install_browser=False
        )
        harness.save_webpage_plan(run, _plan())
        attempt = harness.begin_webpage_attempt(run)
        staged = harness.stage_visual(run, attempt, "vis-001")
        artifact = run / "attempts" / attempt / "artifact"
        (artifact / "index.html").write_text(
            transform(_valid_html(staged)), encoding="utf-8"
        )
        harness.write_webpage_source_map(run, attempt, _claims())
        return run, attempt, artifact

    def test_real_browser_checks_desktop_mobile_keyboard_no_js_and_reduced_motion(self) -> None:
        cache = self._browser_cache()
        harness = _load_harness()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "paper.md"
            source.write_text(_source_text(), encoding="utf-8")
            visual = root / "pipeline.svg"
            visual.write_text(_svg(), encoding="utf-8")
            run = root / "run"
            harness.initialize_webpage_run(
                run,
                source,
                extra_assets=[visual],
                release_version="0.1.0-real-browser",
                install_browser=False,
            )
            harness.save_webpage_plan(run, _plan())
            attempt = harness.begin_webpage_attempt(run)
            staged = harness.stage_visual(run, attempt, "vis-001")
            artifact = run / "attempts" / attempt / "artifact"
            (artifact / "index.html").write_text(_valid_html(staged), encoding="utf-8")
            harness.write_webpage_source_map(run, attempt, _claims())

            report = harness.validate_webpage_attempt(
                run,
                attempt,
                browser_cache=cache,
                allow_browser_install=False,
            )

            self.assertTrue(report["passed"], report)
            self.assertTrue((run / "attempts" / attempt / "qa" / "previews" / "desktop.png").is_file())
            self.assertTrue((run / "attempts" / attempt / "qa" / "previews" / "mobile.png").is_file())
            interaction = json.loads(
                (artifact / "interaction-audit.json").read_text(encoding="utf-8")
            )
            self.assertTrue(interaction["checks"]["keyboard_interactions"])
            self.assertTrue(interaction["checks"]["no_javascript_core_visible"])
            self.assertTrue(interaction["checks"]["reduced_motion_effective"])
            self.assertTrue(interaction["checks"]["internal_links_resolve"])
            self.assertTrue(interaction["checks"]["observable_interaction_effects"])
            self.assertTrue(interaction["checks"]["mobile_interaction_available"])
            self.assertTrue(interaction["checks"]["desktop_identity_thesis_above_fold"])
            self.assertTrue(interaction["checks"]["focus_indicators_visible"])

    def test_browser_rejects_aria_only_interaction_without_visible_target_change(self) -> None:
        cache = self._browser_cache()
        harness = _load_harness()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run, attempt, artifact = self._author_browser_attempt(
                root,
                harness,
                lambda html: html.replace(
                    "document.getElementById('method-figure').classList.toggle('is-inspected',!selected);",
                    "document.getElementById('method-figure').setAttribute('aria-label','Inspected');",
                ),
            )

            report = harness.validate_webpage_attempt(
                run, attempt, browser_cache=cache, allow_browser_install=False
            )

            self.assertFalse(report["passed"])
            audit = json.loads(
                (artifact / "interaction-audit.json").read_text(encoding="utf-8")
            )
            self.assertFalse(audit["checks"]["observable_interaction_effects"])
            self.assertFalse(audit["interactions"][0]["target_changed"])

    def test_browser_does_not_count_focus_only_target_styling_as_activation(self) -> None:
        cache = self._browser_cache()
        harness = _load_harness()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def focus_only_change(html: str) -> str:
                return html.replace(
                    "figure.is-inspected { border-color:var(--accent); }",
                    "#inspect-method-control:focus + #method-figure { border-color:var(--accent); }",
                    1,
                ).replace(
                    "document.getElementById('method-figure').classList.toggle('is-inspected',!selected);",
                    "document.getElementById('method-figure').setAttribute('aria-label','Inspected');",
                    1,
                )

            run, attempt, artifact = self._author_browser_attempt(
                root, harness, focus_only_change
            )

            report = harness.validate_webpage_attempt(
                run, attempt, browser_cache=cache, allow_browser_install=False
            )

            self.assertFalse(report["passed"])
            audit = json.loads(
                (artifact / "interaction-audit.json").read_text(encoding="utf-8")
            )
            self.assertFalse(audit["checks"]["observable_interaction_effects"])
            self.assertFalse(audit["interactions"][0]["target_changed"])

    def test_browser_requires_a_usable_mobile_interaction_control(self) -> None:
        cache = self._browser_cache()
        harness = _load_harness()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run, attempt, artifact = self._author_browser_attempt(
                root,
                harness,
                lambda html: html.replace(
                    "@media (max-width:720px) {",
                    "@media (max-width:720px) { #inspect-method-control { display:none!important; }",
                    1,
                ),
            )

            report = harness.validate_webpage_attempt(
                run, attempt, browser_cache=cache, allow_browser_install=False
            )

            self.assertFalse(report["passed"])
            audit = json.loads(
                (artifact / "interaction-audit.json").read_text(encoding="utf-8")
            )
            self.assertFalse(audit["checks"]["mobile_interaction_available"])

    def test_browser_requires_the_mobile_interaction_to_work(self) -> None:
        cache = self._browser_cache()
        harness = _load_harness()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run, attempt, artifact = self._author_browser_attempt(
                root,
                harness,
                lambda html: html.replace(
                    "@media (max-width:720px) {",
                    "@media (max-width:720px) { figure.is-inspected { border-color:transparent!important; }",
                    1,
                ),
            )

            report = harness.validate_webpage_attempt(
                run, attempt, browser_cache=cache, allow_browser_install=False
            )

            self.assertFalse(report["passed"])
            audit = json.loads(
                (artifact / "interaction-audit.json").read_text(encoding="utf-8")
            )
            self.assertFalse(audit["checks"]["mobile_interaction_available"])

    def test_browser_requires_identity_and_thesis_in_first_desktop_viewport(self) -> None:
        cache = self._browser_cache()
        harness = _load_harness()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run, attempt, artifact = self._author_browser_attempt(
                root,
                harness,
                lambda html: html.replace(
                    "header { min-height:70vh;",
                    "header { margin-top:1200px; min-height:70vh;",
                    1,
                ),
            )

            report = harness.validate_webpage_attempt(
                run, attempt, browser_cache=cache, allow_browser_install=False
            )

            self.assertFalse(report["passed"])
            audit = json.loads(
                (artifact / "interaction-audit.json").read_text(encoding="utf-8")
            )
            self.assertFalse(audit["checks"]["desktop_identity_thesis_above_fold"])

    def test_browser_rejects_a_sliver_of_identity_at_the_viewport_edge(self) -> None:
        cache = self._browser_cache()
        harness = _load_harness()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run, attempt, artifact = self._author_browser_attempt(
                root,
                harness,
                lambda html: html.replace(
                    "h1 {",
                    "h1, [data-thesis-claim-id] { position:fixed; top:990px; margin:0; } h1 {",
                    1,
                ),
            )

            report = harness.validate_webpage_attempt(
                run, attempt, browser_cache=cache, allow_browser_install=False
            )

            self.assertFalse(report["passed"])
            audit = json.loads(
                (artifact / "interaction-audit.json").read_text(encoding="utf-8")
            )
            self.assertFalse(audit["checks"]["desktop_identity_thesis_above_fold"])

    def test_browser_requires_a_visibly_distinct_keyboard_focus_indicator(self) -> None:
        cache = self._browser_cache()
        harness = _load_harness()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run, attempt, artifact = self._author_browser_attempt(
                root,
                harness,
                lambda html: html.replace(
                    "outline:3px solid var(--accent); outline-offset:4px;",
                    "outline:none;",
                    1,
                ),
            )

            report = harness.validate_webpage_attempt(
                run, attempt, browser_cache=cache, allow_browser_install=False
            )

            self.assertFalse(report["passed"])
            audit = json.loads(
                (artifact / "interaction-audit.json").read_text(encoding="utf-8")
            )
            self.assertFalse(audit["checks"]["focus_indicators_visible"])

    def test_browser_observes_a_two_second_delayed_egress_attempt(self) -> None:
        cache = self._browser_cache()
        harness = _load_harness()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "artifact"
            artifact.mkdir()
            html_path = artifact / "index.html"
            html_path.write_text(
                """<!doctype html><html lang="en"><body>
                <button id="control" aria-controls="target" aria-pressed="false">Inspect</button>
                <p id="target">Evidence</p>
                <script>
                const c=document.getElementById('control');
                c.addEventListener('click',()=>{c.setAttribute('aria-pressed','true');document.getElementById('target').style.border='3px solid red';});
                setTimeout(()=>{window.location.href='https://egress.example/late';},2000);
                </script></body></html>""",
                encoding="utf-8",
            )
            runtime = harness.setup_browser.ensure_browser_runtime(
                cache_root=cache, allow_install=False
            )

            report = harness._run_interaction_audit(
                html_path=html_path,
                workspace_root=artifact,
                output_dir=root / "qa",
                interactions=[
                    {
                        "id": "inspect",
                        "control_id": "control",
                        "target_id": "target",
                        "state_attribute": "aria-pressed",
                    }
                ],
                content_contract={
                    "title_claim_id": "title",
                    "thesis_claim_id": "thesis",
                    "sections": [],
                    "visual_allocations": [],
                    "missing_metadata": [],
                },
                runtime=runtime,
                browser_cache=cache,
                allow_install=False,
            )

            self.assertFalse(report["passed"])
            self.assertFalse(report["checks"]["no_network_attempts"])
            self.assertGreaterEqual(report["blocked_request_count"], 1)

    def test_no_javascript_claim_check_uses_canonical_unicode_text(self) -> None:
        cache = self._browser_cache()
        harness = _load_harness()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "artifact"
            (artifact / "assets").mkdir(parents=True)
            (artifact / "assets" / "vis-001.svg").write_text(
                _svg(), encoding="utf-8"
            )
            html_path = artifact / "index.html"
            html_path.write_text(
                _valid_html().replace(
                    '<h1 data-claim-id="claim-title">Atlas: Evidence-Aware Research Communication</h1>',
                    '<h1 data-claim-id="claim-title">Cafe\u0301: Evidence-Aware Research Communication</h1>',
                    1,
                ),
                encoding="utf-8",
            )
            contract = _plan()
            source_claims = _claims()
            source_claims[0] = {
                **source_claims[0],
                "text": "Caf\u00e9: Evidence-Aware Research Communication",
            }
            contract["source_claims"] = source_claims
            runtime = harness.setup_browser.ensure_browser_runtime(
                cache_root=cache, allow_install=False
            )

            report = harness._run_interaction_audit(
                html_path=html_path,
                workspace_root=artifact,
                output_dir=root / "qa",
                interactions=contract["interactions"],
                content_contract=contract,
                runtime=runtime,
                browser_cache=cache,
                allow_install=False,
            )

            self.assertTrue(report["passed"], report)
            self.assertTrue(report["checks"]["no_javascript_core_visible"])

    def test_no_javascript_gate_rejects_hidden_source_text_with_css_replacement(self) -> None:
        cache = self._browser_cache()
        harness = _load_harness()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def hide_bound_text(html: str) -> str:
                return html.replace(
                    "section, header {",
                    ".source-hidden { display:none; } .invented::before { content:'Invented result'; } section, header {",
                    1,
                ).replace(
                    '<p data-claim-id="claim-results">Atlas improved evidence coverage from 62% to 91% on the synthetic evaluation.</p>',
                    '<p class="invented" data-claim-id="claim-results"><span class="source-hidden">Atlas improved evidence coverage from 62% to 91% on the synthetic evaluation.</span></p>',
                    1,
                )

            run, attempt, artifact = self._author_browser_attempt(
                root, harness, hide_bound_text
            )

            report = harness.validate_webpage_attempt(
                run, attempt, browser_cache=cache, allow_browser_install=False
            )

            self.assertFalse(report["passed"])
            interaction = json.loads(
                (artifact / "interaction-audit.json").read_text(encoding="utf-8")
            )
            self.assertFalse(interaction["checks"]["no_javascript_core_visible"])

    def test_no_javascript_gate_rejects_css_hidden_claim_evidence(self) -> None:
        cache = self._browser_cache()
        harness = _load_harness()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "paper.md"
            source.write_text(_source_text(), encoding="utf-8")
            visual = root / "pipeline.svg"
            visual.write_text(_svg(), encoding="utf-8")
            run = root / "run"
            harness.initialize_webpage_run(
                run,
                source,
                extra_assets=[visual],
                release_version="0.1.0-real-browser-hidden-claim",
                install_browser=False,
            )
            harness.save_webpage_plan(run, _plan())
            attempt = harness.begin_webpage_attempt(run)
            staged = harness.stage_visual(run, attempt, "vis-001")
            artifact = run / "attempts" / attempt / "artifact"
            html = _valid_html(staged).replace(
                "section, header {",
                ".hidden-source-claim { display:none; } section, header {",
            ).replace(
                '<p data-claim-id="claim-results">',
                '<p class="hidden-source-claim" data-claim-id="claim-results">',
            )
            (artifact / "index.html").write_text(html, encoding="utf-8")
            harness.write_webpage_source_map(run, attempt, _claims())

            report = harness.validate_webpage_attempt(
                run,
                attempt,
                browser_cache=cache,
                allow_browser_install=False,
            )

            self.assertFalse(report["passed"])
            interaction = json.loads(
                (artifact / "interaction-audit.json").read_text(encoding="utf-8")
            )
            self.assertFalse(interaction["checks"]["no_javascript_core_visible"])


if __name__ == "__main__":
    unittest.main()
