#!/usr/bin/env python3
"""Smoke test for the external poster code-editor process contract."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from autodesign.agents.external_code_editor import ExternalCodeEditor
from autodesign.config import REPO_ROOT
from autodesign.util.academic_palette import require_academic_color_system


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="autodesign-code-editor-") as tmp:
        root = Path(tmp)
        required_color_system = require_academic_color_system("plum_sage")
        palette_id = str(required_color_system["palette_id"])
        palette_declarations = ";".join(
            f"{name}:{value}"
            for name, value in required_color_system["css_variables"].items()
        )
        source_run = root / "runs" / "source_run"
        source_final = source_run / "final"
        layers = source_final / "layers"
        layers.mkdir(parents=True)
        (layers / "fake.png").write_bytes(b"fake image bytes")
        source_poster = source_final / "poster.html"
        source_poster.write_text(
            """<!doctype html>
<html><body>
<main class="paper-poster" style="width:1200px;height:600px">
  <section data-block-id="results"><h2>Results</h2><img src="layers/fake.png"><p>Original result.</p></section>
</main>
</body></html>
""",
            encoding="utf-8",
        )
        (source_run / "paper_memory.md").write_text("Grounded result: pass@1 improves.", encoding="utf-8")
        (source_run / "paper_visual_provenance.json").write_text("{}", encoding="utf-8")

        fake = root / "fake_code_editor.py"
        fake.write_text(
            "\n".join(
                [
                    "import json, pathlib, sys",
                    "cwd = pathlib.Path.cwd()",
                    "prompt = sys.stdin.read()",
                    "assert 'paper_poster_revision_skill.md' in prompt",
                    "assert 'selection_context.json' in prompt",
                    "assert (cwd / 'current_poster.html').exists()",
                    "assert (cwd / 'paper_poster_revision_skill.md').exists()",
                    "assert (cwd / 'selection_context.json').exists()",
                    "manifest = json.loads((cwd / 'source_manifest.json').read_text())",
                    "assert manifest['has_selection_context'] is True",
                    "assert manifest['selection_context_summary']['block_id'] == 'results'",
                    "if (cwd / 'validation_feedback.json').exists():",
                    "    html = " + repr(
                        "<!doctype html><html><body>"
                        f"<main class=\"paper-poster\" data-palette-id=\"{palette_id}\" "
                        f"style=\"{palette_declarations};width:1200px;height:600px\">"
                        "<section data-block-id=\"results\"><h2>Results</h2>"
                        "<img src=\"layers/fake.png\"><p>Revised result: pass@1 improves while keeping "
                        "the source figure local and editable. This extra grounded copy makes the smoke "
                        "artifact large enough to look like a real poster section.</p></section></main></body></html>"
                    ),
                    "else:",
                    "    html = '<!doctype html><html><body><main class=\"paper-poster\"><img src=\"https://example.com/bad.png\"><p>Bad remote image.</p></main></body></html>'",
                    "(cwd / 'poster.html').write_text(html)",
                    "(cwd / 'code_editor_done.json').write_text(json.dumps({'status': 'done'}))",
                ]
            ),
            encoding="utf-8",
        )

        settings = SimpleNamespace(
            code_editor_cmd=f"{sys.executable} {fake}",
            code_editor_harness="custom",
            code_editor_timeout_s=10,
            code_editor_max_attempts=2,
            skills_dir=REPO_ROOT / "skills",
        )
        run_dir = root / "runs" / "revision_run"
        result = ExternalCodeEditor(settings).run(
            source_poster_path=source_poster,
            source_final_dir=source_final,
            run_dir=run_dir,
            parent_run_id="source_run",
            instruction="Tighten the Results section.",
            conversation_history=[{"role": "user", "text": "Tighten Results."}],
            required_color_system=required_color_system,
            selection_context={
                "kind": "element",
                "rect": {"x": 12, "y": 24, "w": 340, "h": 180},
                "selector": '[data-block-id="results"]',
                "block_id": "results",
                "text_excerpt": "Results Original result.",
                "html_excerpt": '<section data-block-id="results"><h2>Results</h2></section>',
                "nearby_headings": ["Results"],
            },
            context_run_dirs=[source_run],
        )
        assert result.poster_path.exists()
        assert len(result.attempts) == 2
        assert result.validation_summary["ok"] is True
        first = result.attempts[0].validation
        assert any("remote asset" in err for err in first["errors"])
        assert (result.attempt_dir / "paper_memory.md").exists()
        assert (result.attempt_dir / "paper_poster_revision_skill.md").exists()
        assert (result.attempt_dir / "selection_context.json").exists()

        missing = SimpleNamespace(code_editor_cmd="", code_editor_harness="custom")
        try:
            ExternalCodeEditor(missing).run(
                source_poster_path=source_poster,
                source_final_dir=source_final,
                run_dir=root / "runs" / "missing",
                parent_run_id="source_run",
                instruction="Edit.",
                conversation_history=[],
                context_run_dirs=[source_run],
                required_color_system=required_color_system,
            )
        except Exception as exc:  # noqa: BLE001
            assert getattr(exc, "reason", "") == "missing_code_editor_cmd"
        else:
            raise AssertionError("missing command should fail")

    print("code editor fake smoke passed")


if __name__ == "__main__":
    main()
