"""CLI entry: `autodesign [chat|run|apply-edits] ...`.

Subcommands:
  chat                     (default) launch conversational REPL
  run "<brief>"            one-shot: generate a single artifact from one brief
  apply-edits <html>       round-trip an edited poster HTML → new PSD/SVG/HTML/PNG

Examples:
  autodesign                             # starts chat shell
  autodesign chat                        # same
  autodesign chat --resume <sid>         # resume existing session
  autodesign run "a 3:4 poster for X"    # one-shot, old behavior
  autodesign apply-edits ~/poster.edited.html  # re-render from edits
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from .config import load_settings
from .runner import PipelineRunner
from .util.run_paths import resolve_run_dir


def _terminal_status_exit_code(terminal_status: str | None) -> int:
    """Map a completed run outcome to a shell-friendly process status."""
    return 0 if str(terminal_status or "").strip().lower() in {"pass", "ok"} else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="autodesign",
        description=(
            "AutoDesign — open-source conversational design agent. "
            "Generates editable posters, slide decks, and landing pages "
            "via chat-driven LLM orchestration."
        ),
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="<command>")

    # chat (default)
    chat_p = subparsers.add_parser(
        "chat",
        help="launch conversational REPL (default if no subcommand given)",
    )
    chat_p.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="resume an existing session from sessions/<SESSION_ID>.json",
    )

    # run (one-shot)
    run_p = subparsers.add_parser(
        "run",
        help="one-shot: generate a single artifact from one brief",
    )
    run_p.add_argument(
        "brief",
        nargs="?",
        default=None,
        help="Design brief, e.g. '国宝回家 公益项目主视觉海报，竖版 3:4'. "
             "Optional when --resume-run is used.",
    )
    run_p.add_argument(
        "--from-file",
        metavar="PATH",
        action="append",
        default=[],
        help=("Attach a source document to this brief (PDF / Markdown / "
              "image). Repeatable. Designer will call `ingest_document` "
              "on these files FIRST, then use the extracted structure "
              "(title / sections / figures) to drive the DesignSpec."),
    )
    run_p.add_argument(
        "--reference-poster",
        metavar="PATH",
        default=None,
        help=("Apply visual style from a separate reference poster "
              "(HTML / PDF / PNG / JPEG / WebP / PPTX). Its content and "
              "assets are never treated as paper evidence."),
    )
    run_p.add_argument(
        "--reference-page",
        metavar="N",
        type=int,
        default=1,
        help="1-based page/slide to use for PDF or PPTX references (default: 1).",
    )
    run_p.add_argument(
        "--template",
        metavar="NAME",
        default=None,
        help=("Poster canvas preset — e.g. 'conference-poster-portrait' "
              "(2172×3072 @150dpi), 'neurips-portrait', 'cvpr-landscape', "
              "'icml-portrait', 'a0-portrait', 'a0-landscape'. When set, "
              "the resolved canvas (w_px / h_px / dpi / aspect_ratio) is "
              "injected into the brief prologue so the designer uses it."),
    )
    run_p.add_argument(
        "--skip-enhancer",
        action="store_true",
        default=False,
        help=("Skip the v2.4 Prompt Enhancer pre-planner stage. "
              "Default: ON (Opus 4.7 rewrites raw briefs into "
              "structured multi-section briefs before designer.start). "
              "Pass this flag to feed the raw brief to the planner "
              "verbatim — useful for power users with hand-crafted "
              "briefs or for A/B comparisons."),
    )
    run_p.add_argument(
        "--no-claim-graph",
        action="store_true",
        default=False,
        help=("Skip the v2.8.0 ClaimGraph extractor stage (runs "
              "between enhancer and planner whenever a PDF is "
              "attached). Default: ON for paper inputs. Pass this "
              "flag to fall back to the v2.7.3 chapter-order "
              "behavior — useful for A/B comparisons or when the "
              "extractor regresses on a particular paper."),
    )
    run_p.add_argument(
        "--from-run",
        metavar="RUN_ID_OR_PATH",
        default=None,
        help=(
            "Reuse ingest artifacts (paper memory, storyboard, visual "
            "provenance, poster content brief) from an existing "
            "out/runs/<run_id> directory and skip PDF parsing. --from-file "
            "becomes optional when this is set. Ideal for iterating on "
            "designer output without re-running ingest."
        ),
    )
    run_p.add_argument(
        "--resume-run",
        metavar="RUN_ID_OR_PATH",
        default=None,
        help=(
            "Resume an existing failed run by appending new attempt_NN "
            "directories under the artifact-specific author root. Skips "
            "ingest, enhancer, canvas plan, and claim graph; the resumed "
            "run reuses the persisted run_brief.json / resume_state.json. "
            "--designer-author-max-attempts is treated as an INCREMENTAL "
            "budget (add N more attempts on top of what already ran)."
        ),
    )
    run_p.add_argument(
        "--reuse-ingest-run",
        metavar="RUN_ID_OR_PATH",
        default=None,
        help=argparse.SUPPRESS,
    )
    run_p.add_argument(
        "--designer-author",
        "--planner-author",
        dest="designer_author",
        choices=("internal", "external"),
        default=None,
        help=(
            "Designer author mode. Default/env: internal. "
            "Use external to run AUTODESIGN_DESIGNER_AUTHOR_CMD or "
            "--designer-author-cmd through the artifact-specific Poster, Landing, "
            "Slides, or Video coding-agent harness."
        ),
    )
    run_p.add_argument(
        "--designer-author-harness",
        "--planner-author-harness",
        dest="designer_author_harness",
        choices=("custom", "codex", "claude", "claude-code", "cloud-code", "deepseek", "deepseek-harness", "dsh", "opencode", "kimi", "mimo", "pi", "zcode"),
        default=None,
        help=(
            "Preset command for --designer-author external. custom keeps "
            "--designer-author-cmd / AUTODESIGN_DESIGNER_AUTHOR_CMD behavior; "
            "deepseek, opencode, kimi, mimo, pi, and zcode adapt their CLI semantics to the "
            "designer-author stdin/file contract."
        ),
    )
    run_p.add_argument(
        "--designer-author-cmd",
        "--planner-author-cmd",
        dest="designer_author_cmd",
        metavar="CMD",
        default=None,
        help=(
            "Local command for --designer-author external, e.g. "
            "'codex exec --model gpt-5-codex'. The active artifact harness prompt "
            "is sent on stdin."
        ),
    )
    run_p.add_argument(
        "--designer-author-model",
        "--planner-author-model",
        dest="designer_author_model",
        metavar="MODEL",
        default=None,
        help="Optional model hint passed to the external designer author prompt.",
    )
    run_p.add_argument(
        "--designer-author-timeout",
        "--planner-author-timeout",
        dest="designer_author_timeout",
        metavar="SECONDS",
        type=int,
        default=None,
        help="Timeout for the external designer author subprocess.",
    )
    run_p.add_argument(
        "--designer-author-max-attempts",
        "--planner-author-max-attempts",
        dest="designer_author_max_attempts",
        metavar="N",
        type=int,
        default=None,
        help="Safety budget for the external designer-author repair-until-pass loop.",
    )
    run_p.add_argument(
        "--identity-logo-agent",
        choices=("auto", "off", "required"),
        default=None,
        help=(
            "Deprecated no-op kept for compatibility. Paper-poster ingest no "
            "longer runs automatic heading-logo discovery."
        ),
    )
    run_p.add_argument(
        "--identity-logo-agent-harness",
        choices=("custom", "codex", "claude", "claude-code", "cloud-code", "deepseek", "deepseek-harness", "dsh", "opencode", "kimi", "mimo", "pi", "zcode"),
        default=None,
        help="Deprecated no-op compatibility flag for the removed identity logo stage.",
    )
    run_p.add_argument(
        "--identity-logo-agent-cmd",
        metavar="CMD",
        default=None,
        help="Deprecated no-op compatibility flag for the removed identity logo stage.",
    )
    run_p.add_argument(
        "--identity-logo-agent-model",
        metavar="MODEL",
        default=None,
        help="Deprecated no-op compatibility flag for the removed identity logo stage.",
    )
    run_p.add_argument(
        "--identity-logo-agent-timeout",
        metavar="SECONDS",
        type=int,
        default=None,
        help="Deprecated no-op compatibility flag for the removed identity logo stage.",
    )

    # apply-edits (round-trip edited HTML)
    ae_p = subparsers.add_parser(
        "apply-edits",
        help=("round-trip an edited poster HTML back into a fresh "
              "PSD/SVG/HTML/preview set"),
    )
    ae_p.add_argument(
        "html",
        help="Path to the edited HTML (e.g. ~/Downloads/poster.edited.html)",
    )
    ae_p.add_argument(
        "-o", "--out-dir",
        metavar="PATH",
        help=("Output run directory (default: out/runs/<new-run-id>). "
              "The new run is always self-contained."),
    )

    args = parser.parse_args(argv)

    # Default subcommand = chat
    if args.subcommand is None or args.subcommand == "chat":
        from .chat import run_chat
        resume_id = getattr(args, "resume", None)
        return run_chat(resume_id=resume_id)

    if args.subcommand == "run":
        return _run_oneshot(
            args.brief,
            getattr(args, "from_file", []) or [],
            reference_poster=getattr(args, "reference_poster", None),
            reference_page=getattr(args, "reference_page", 1),
            template=getattr(args, "template", None),
            skip_enhancer=getattr(args, "skip_enhancer", False),
            no_claim_graph=getattr(args, "no_claim_graph", False),
            reuse_ingest_run=getattr(args, "reuse_ingest_run", None),
            from_run=getattr(args, "from_run", None),
            resume_run=getattr(args, "resume_run", None),
            designer_author=getattr(args, "designer_author", None),
            designer_author_harness=getattr(args, "designer_author_harness", None),
            designer_author_cmd=getattr(args, "designer_author_cmd", None),
            designer_author_model=getattr(args, "designer_author_model", None),
            designer_author_timeout=getattr(args, "designer_author_timeout", None),
            designer_author_max_attempts=getattr(args, "designer_author_max_attempts", None),
            identity_logo_agent=getattr(args, "identity_logo_agent", None),
            identity_logo_agent_harness=getattr(args, "identity_logo_agent_harness", None),
            identity_logo_agent_cmd=getattr(args, "identity_logo_agent_cmd", None),
            identity_logo_agent_model=getattr(args, "identity_logo_agent_model", None),
            identity_logo_agent_timeout=getattr(args, "identity_logo_agent_timeout", None),
        )

    if args.subcommand == "apply-edits":
        return _run_apply_edits(args.html, args.out_dir)

    parser.print_help()
    return 1


def _run_oneshot(brief: str | None, from_file: list[str],
                 *, reference_poster: str | None = None,
                 reference_page: int = 1,
                 template: str | None = None,
                 skip_enhancer: bool = False,
                 no_claim_graph: bool = False,
                 reuse_ingest_run: str | None = None,
                 from_run: str | None = None,
                 resume_run: str | None = None,
                 designer_author: str | None = None,
                 designer_author_harness: str | None = None,
                 designer_author_cmd: str | None = None,
                 designer_author_model: str | None = None,
                 designer_author_timeout: int | None = None,
                 designer_author_max_attempts: int | None = None,
                 identity_logo_agent: str | None = None,
                 identity_logo_agent_harness: str | None = None,
                 identity_logo_agent_cmd: str | None = None,
                 identity_logo_agent_model: str | None = None,
                 identity_logo_agent_timeout: int | None = None) -> int:
    """One-shot mode — single brief (+ optional attachments) → single artifact."""
    del (
        identity_logo_agent,
        identity_logo_agent_harness,
        identity_logo_agent_cmd,
        identity_logo_agent_model,
        identity_logo_agent_timeout,
    )

    # --resume-run is mutually exclusive with every other input flag: it
    # rehydrates raw_user_brief / attachments / template from the resumed
    # run's persisted state files. Any of these on the command line would
    # silently be ignored or actively conflict.
    if resume_run:
        conflicts: list[str] = []
        if brief:
            conflicts.append("brief")
        if from_file:
            conflicts.append("--from-file")
        if reference_poster:
            conflicts.append("--reference-poster")
        if reference_page != 1:
            conflicts.append("--reference-page")
        if from_run:
            conflicts.append("--from-run")
        if reuse_ingest_run:
            conflicts.append("--reuse-ingest-run")
        if template:
            conflicts.append("--template")
        if skip_enhancer:
            conflicts.append("--skip-enhancer")
        if no_claim_graph:
            conflicts.append("--no-claim-graph")
        if conflicts:
            print(
                f"  error: --resume-run is mutually exclusive with: {', '.join(conflicts)}",
                file=sys.stderr,
            )
            return 2
    elif from_run and reuse_ingest_run:
        print(
            "  error: --from-run and --reuse-ingest-run point at the same "
            "mechanism; pass only one",
            file=sys.stderr,
        )
        return 2
    else:
        if not brief:
            print(
                "  error: brief is required (unless --resume-run is set)",
                file=sys.stderr,
            )
            return 2

    # --from-run is the first-class name for --reuse-ingest-run.
    if from_run and not reuse_ingest_run:
        reuse_ingest_run = from_run

    # v1.1: resolve --from-file attachments into Path objects and validate early.
    # In --from-run / --reuse-ingest-run / --resume-run modes, --from-file is
    # optional: attachments are either replaced by the reused ingest or restored
    # from the resumed run's resume_state.json.
    attachments: list[Path] = []
    for fp_str in from_file:
        p = Path(fp_str).expanduser().resolve()
        if not p.exists():
            print(f"  error: attachment not found: {p}", file=sys.stderr)
            return 2
        if not p.is_file():
            print(f"  error: attachment not a file: {p}", file=sys.stderr)
            return 2
        attachments.append(p)

    reference_path: Path | None = None
    if reference_poster:
        from .util.reference_poster import SUPPORTED_REFERENCE_POSTER_SUFFIXES

        reference_path = Path(reference_poster).expanduser().resolve()
        if not reference_path.exists() or not reference_path.is_file():
            print(f"  error: reference poster not found: {reference_path}", file=sys.stderr)
            return 2
        if reference_path.suffix.lower() not in SUPPORTED_REFERENCE_POSTER_SUFFIXES:
            supported = ", ".join(sorted(SUPPORTED_REFERENCE_POSTER_SUFFIXES))
            print(
                f"  error: unsupported reference poster format "
                f"{reference_path.suffix or '(none)'}; supported: {supported}",
                file=sys.stderr,
            )
            return 2
    if reference_page < 1:
        print("  error: --reference-page must be 1 or greater", file=sys.stderr)
        return 2
    if reference_path is None and reference_page != 1:
        print("  error: --reference-page requires --reference-poster", file=sys.stderr)
        return 2

    # v2.3 — validate template name early so a typo fails before any API cost.
    if template is not None:
        from .config import resolve_template, available_templates
        if resolve_template(template) is None:
            names = " / ".join(available_templates())
            print(f"  error: unknown template {template!r}. "
                  f"Available: {names}", file=sys.stderr)
            return 2

    settings = load_settings()
    settings_updates: dict[str, object] = {}
    if designer_author is not None:
        settings_updates["designer_author_mode"] = designer_author
    if designer_author_harness is not None:
        settings_updates["designer_author_harness"] = designer_author_harness
    if designer_author_cmd is not None:
        settings_updates["designer_author_cmd"] = designer_author_cmd
    if designer_author_model is not None:
        settings_updates["designer_author_model"] = designer_author_model or None
    if designer_author_timeout is not None:
        settings_updates["designer_author_timeout_s"] = designer_author_timeout
    if designer_author_max_attempts is not None:
        settings_updates["designer_author_max_attempts"] = designer_author_max_attempts
        settings_updates["authoring_max_attempts_override"] = designer_author_max_attempts
    if settings_updates:
        settings = replace(settings, **settings_updates)
        if (
            designer_author_cmd is None
            and settings.designer_author_harness != "custom"
            and (designer_author_harness is not None or designer_author_model is not None)
        ):
            from .config import designer_author_command_for_harness
            settings = replace(
                settings,
                designer_author_cmd=designer_author_command_for_harness(
                    settings.designer_author_harness,
                    settings.designer_author_model,
                ),
            )
    if reference_path is not None and settings.designer_author_mode != "external":
        print(
            "  error: --reference-poster requires --designer-author external",
            file=sys.stderr,
        )
        return 2
    if reuse_ingest_run:
        reuse_path = resolve_run_dir(settings.out_dir, reuse_ingest_run)
        if not reuse_path.exists() or not reuse_path.is_dir():
            print(f"  error: reuse ingest run not found: {reuse_path}", file=sys.stderr)
            return 2
    if resume_run:
        resume_path = resolve_run_dir(settings.out_dir, resume_run)
        if not resume_path.exists() or not resume_path.is_dir():
            print(f"  error: resume run not found: {resume_path}", file=sys.stderr)
            return 2
    runner = PipelineRunner(settings)
    try:
        result = runner.run(brief or "", attachments=attachments,
                            template=template,
                            skip_enhancer=skip_enhancer,
                            no_claim_graph=no_claim_graph,
                            reuse_ingest_run=reuse_ingest_run,
                            resume_run=resume_run,
                            reference_poster=reference_path,
                            reference_page_index=reference_page - 1)
    except ValueError as exc:
        if getattr(exc, "issue_id", "") not in {
            "paper_source_generated_poster_detected",
            "paper_source_unreadable",
            "paper_source_sanity_unverifiable",
            "video_runtime_unavailable",
        }:
            raise
        print(f"  error: {exc}", file=sys.stderr)
        return 2

    run_dir = Path(result.run_dir)
    final_dir = run_dir / "final"
    _write_cli_events(settings.out_dir, result.run_id, brief or "", attachments, result)

    print()
    if attachments:
        print(f"  Ingested:        {', '.join(str(p.name) for p in attachments)}")
    if reference_path:
        print(f"  Reference style: {reference_path.name} (page {reference_page})")
    print(f"  Run dir:         {run_dir}")
    for fname in ("preview.png", "poster.psd", "poster.svg",
                  "poster.html", "index.html", "deck.html", "deck.pdf", "deck.pptx"):
        fp = final_dir / fname
        if fp.exists():
            print(f"  {fname:14s}:  {fp}")
    print(f"  Layers:          {result.n_layers}  "
          f"|  Critiques: {result.n_critiques}")
    print(f"  Terminal:        {result.terminal_status}  "
          f"|  Critic: {result.critic_verdict or 'none'}"
          + (f" ({result.critic_score:.2f})" if result.critic_score is not None else ""))
    print(f"  Wall time:       {result.wall_s}s")
    return _terminal_status_exit_code(result.terminal_status)


def _run_apply_edits(html_path: str, out_dir: str | None) -> int:
    """Round-trip edited HTML → new render_dir."""
    from .apply_edits import apply_edits
    from .util.design_events import append_design_event, layer_edit_events
    from .util.layer_parse import parse_html_layers

    src = Path(html_path).expanduser().resolve()
    if not src.exists():
        print(f"  error: edited HTML not found: {src}", file=sys.stderr)
        return 2
    out = Path(out_dir).expanduser().resolve() if out_dir else None

    settings = load_settings()

    # Snapshot layers before editing so we can diff afterward.
    try:
        before_layers = parse_html_layers(src)
    except Exception:  # noqa: BLE001
        before_layers = []

    try:
        result = apply_edits(
            src, settings=settings, out_dir=out,
        )
    except (ValueError, RuntimeError, FileNotFoundError) as e:
        print(f"  error: {e}", file=sys.stderr)
        return 2

    print()
    print(f"  Source HTML:     {src}")
    print(f"  New run_id:      {result.run_id}")
    run_dir = Path(result.run_dir)
    print(f"  Run dir:         {run_dir}")
    final_dir = run_dir / "final"
    for fname in ("preview.png", "poster.psd", "poster.svg",
                  "poster.html", "index.html"):
        fp = final_dir / fname
        if fp.exists():
            print(f"  {fname:14s}:  {fp}")
    print(f"  Layers restored: {len(result.restored_layer_ids)}"
          + (f"  |  skipped: {len(result.skipped)} ({', '.join(result.skipped)})"
             if result.skipped else ""))

    # --- Design-memory events ------------------------------------------------
    # Mirror what /api/edits/apply in web_server.py does: diff before/after
    # layers and write layer.edited + edits.applied into the design session.
    # conversation_id ties this edit back to the original generation run so
    # the memory extractor can correlate brief → artifact → user-edits.
    after_html = final_dir / src.name
    if not after_html.exists():
        # Fallback: landing uses index.html, poster uses poster.html.
        alt = "index.html" if result.artifact_type == "landing" else "poster.html"
        after_html = final_dir / alt
    try:
        after_layers = parse_html_layers(after_html) if after_html.exists() else []
    except Exception:  # noqa: BLE001
        after_layers = []

    parent = result.parent_run_id or result.run_id
    conversation_id = f"cli_{parent}"
    edit_diffs = layer_edit_events(before_layers, after_layers)
    out_dir_path = settings.out_dir
    for diff in edit_diffs:
        append_design_event(
            out_dir_path, conversation_id, "layer.edited",
            run_id=result.run_id,
            artifact_id=f"art_{result.run_id}",
            data={
                **diff,
                "parent_run_id": result.parent_run_id,
                "artifact_type": result.artifact_type,
            },
        )
    append_design_event(
        out_dir_path, conversation_id, "edits.applied",
        run_id=result.run_id,
        artifact_id=f"art_{result.run_id}",
        data={
            "parent_run_id": result.parent_run_id,
            "artifact_type": result.artifact_type,
            "n_layer_diffs": len(edit_diffs),
            "restored_layer_ids": result.restored_layer_ids,
            "skipped": result.skipped,
        },
    )
    # -------------------------------------------------------------------------
    return 0


def _write_cli_events(out_dir: Path, run_id: str, brief: str,
                      attachments: list[Path], result) -> None:
    from .util.design_events import append_design_event, attachment_event_data

    conversation_id = f"cli_{run_id}"
    append_design_event(
        out_dir, conversation_id, "message.user_submitted",
        run_id=run_id,
        data={"brief": brief, "artifact_type": result.artifact_type},
    )
    for p in attachments:
        append_design_event(
            out_dir, conversation_id, "attachment.added",
            run_id=run_id,
            data=attachment_event_data(p),
        )
    failed = result.terminal_status in {"fail", "max_turns", "abort"}
    event = "artifact.generation_failed" if failed else "artifact.generated"
    append_design_event(
        out_dir, conversation_id, event,
        run_id=run_id,
        artifact_id=f"art_{run_id}",
        data={
            "artifact_type": result.artifact_type,
            "terminal_status": result.terminal_status,
            "critic_verdict": result.critic_verdict,
            "critic_score": result.critic_score,
            "n_layers": result.n_layers,
            "n_critiques": result.n_critiques,
            "wall_s": result.wall_s,
            "canvas_plan": getattr(result, "canvas_plan", {}),
            "deck_plan": getattr(result, "deck_plan", {}),
        },
    )
    if not failed and result.style_snapshot:
        append_design_event(
            out_dir, conversation_id, "artifact.style_snapshot",
            run_id=run_id,
            artifact_id=f"art_{run_id}",
            data=result.style_snapshot,
        )


if __name__ == "__main__":
    sys.exit(main())
