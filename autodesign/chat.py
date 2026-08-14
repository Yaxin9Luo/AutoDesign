"""Conversational CLI shell — `autodesign chat` entry point.

Multi-turn REPL over AutoDesign: user types a brief (or follow-up like
"make the title bigger"), agent runs full DesignerLoop, artifacts land in
`out/runs/<run_id>/`, session state persists to `sessions/<id>.json`.

Slash commands (v1.0 subset):
  :help                  show commands
  :save [id]             save session to disk (default: current session_id)
  :load <id>             replace current session with loaded one
  :new                   start fresh session (prompts to save current)
  :list                  list recent sessions (most recent first)
  :history               show message history
  :tokens                show cumulative runtime stats
  :edit                  (v1.0 #5 stub) explains why natural language is preferred
  :export [path]         copy all artifacts + session to path/
  :exit  /  :quit  /  :q exit (prompts save)

Anything not starting with `:` is a user brief — goes to the planner as
the next turn. Prior artifacts in the session are summarized as context
so the planner can tell "revise existing" from "make something new."
"""

from __future__ import annotations

import json
import shutil
import sys
import textwrap
import traceback
from datetime import datetime
from pathlib import Path

from .config import Settings, load_settings
from .runner import PipelineRunner
from .schema import ArtifactType, RunResult
from .session import (
    ArtifactRef,
    ChatSession,
    load_session,
    list_sessions,
    new_session_id,
    save_session,
)
from .util.design_events import append_design_event, attachment_event_data
from .util.layer_parse import parse_html_layers

# Enable arrow-key history / line editing on Unix; harmless import on macOS/Linux.
try:
    import readline  # noqa: F401
except ImportError:
    pass


BANNER = (
    "AutoDesign v0.1 — open-source conversational design agent\n"
    "Describe what you want to make, or type :help\n"
)


# --- Public entry ---------------------------------------------------------


def run_chat(resume_id: str | None = None) -> int:
    """Main chat REPL. Returns exit code."""
    try:
        settings = load_settings()
    except RuntimeError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    sessions_dir = settings.repo_root / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    if resume_id:
        try:
            session = load_session(sessions_dir, resume_id)
            print(f"\n  resumed session {session.session_id}  "
                  f"({len(session.artifacts)} prior artifact(s))\n")
        except FileNotFoundError:
            print(f"  session not found: {resume_id}", file=sys.stderr)
            return 2
    else:
        session = ChatSession(session_id=new_session_id())
        print(f"\n{BANNER}"
              f"  session: {session.session_id}\n"
              f"  sessions dir: {sessions_dir}\n")

    state = {"session": session, "sessions_dir": sessions_dir,
             "settings": settings, "dirty": False}

    try:
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                print()
                _handle_exit(state)
                return 0
            except KeyboardInterrupt:
                print("\n  (use :exit to quit, or Ctrl-D)")
                continue

            if not line:
                continue

            if line.startswith(":"):
                should_continue = _dispatch_slash(line, state)
                if not should_continue:
                    return 0
                continue

            _handle_brief(line, state)
    except Exception as e:
        print(f"\n  fatal chat error: {e}", file=sys.stderr)
        traceback.print_exc()
        # best-effort save before exit so work isn't lost
        try:
            save_session(state["session"], state["sessions_dir"])
            print(f"  emergency-saved session {state['session'].session_id}",
                  file=sys.stderr)
        except Exception:
            pass
        return 1


# --- Brief handling (the actual work) -------------------------------------


def _handle_brief(brief: str, state: dict) -> None:
    session: ChatSession = state["session"]
    settings: Settings = state["settings"]

    # Build contextual brief: include a compact summary of prior artifacts so
    # planner can tell "revise existing" from "make new artifact."
    contextual_brief = _build_contextual_brief(brief, session)

    # v1.1: consume any pending_attachments (queued via :attach) for THIS turn.
    attachments: list[Path] = []
    for fp_str in session.pending_attachments:
        p = Path(fp_str).expanduser()
        if not p.is_absolute():
            p = p.resolve()
        if p.exists() and p.is_file():
            attachments.append(p)
        else:
            print(f"  warning: pending attachment not found, skipping: {fp_str}")
    # Clear queue — attachments are one-shot per turn.
    session.pending_attachments = []

    session.append_user(brief)

    print(f"\n  [generating — {settings.designer_model}, may take 1-5 min"
          + (f", ingesting {len(attachments)} file(s)" if attachments else "")
          + "]\n")
    start = datetime.now()

    try:
        result = PipelineRunner(settings).run(
            contextual_brief, attachments=attachments or None,
        )
    except Exception as e:
        print(f"  generation failed: {e}", file=sys.stderr)
        session.append_system(f"[error] {e}")
        state["dirty"] = True
        save_session(session, state["sessions_dir"])  # save even on failure
        return

    ref = _result_to_ref(result)
    _write_chat_events(settings.out_dir, session.session_id, brief, attachments, result, ref)
    session.artifacts.append(ref)
    session.current_artifact_type = ref.artifact_type
    session.append_assistant(
        _assistant_summary(result, ref),
        artifact_id=f"art_{result.run_id}",
    )
    state["dirty"] = False  # we auto-save after each turn
    save_session(session, state["sessions_dir"])

    elapsed = (datetime.now() - start).total_seconds()
    _display_turn_result(result, ref, elapsed, session)


def _build_contextual_brief(user_text: str, session: ChatSession) -> str:
    """Prepend session context for the planner when prior artifacts exist.

    HTML artifacts contribute parsed editable layer context; other artifact
    types contribute a compact product summary.
    """
    if not session.artifacts:
        return user_text

    latest = session.artifacts[-1]
    prior_layers = _load_prior_layers(latest)

    parts: list[str] = [
        "## Prior artifact in this chat session",
        f"- run_id: {latest.run_id}",
        f"- artifact_type: {latest.artifact_type.value}",
        f"- layers: {latest.n_layers}",
        f"- critic: {latest.verdict} ({latest.score})",
        f"- preview: {latest.preview_path}",
    ]
    if prior_layers:
        parts.extend([
            "",
            "### Prior editable HTML layers",
            "```json",
            json.dumps(prior_layers, ensure_ascii=False, indent=2),
            "```",
        ])
    else:
        parts.append(
            "- (no editable HTML layer manifest is available; use the artifact "
            "summary above as context only)"
        )
    parts.extend([
        "",
        "## Decision: revision or new artifact?",
        "The user's next request may be:",
        "  (a) a REVISION to the prior artifact (e.g. 'make title bigger', "
        "'try a red palette', 'move the stamp'). Reuse the prior artifact "
        "style/content where possible and keep existing layer_id values for "
        "layers you're revising, DO NOT invent a fresh concept from scratch.",
        "  (b) a NEW artifact — the user introduces a new subject, type, or "
        "canvas (e.g. 'now a landing page for this', 'make a poster for "
        "DIFFERENT project X'). Call switch_artifact_type first, then "
        "propose_design_spec with a new canvas/mood/palette.",
        "",
        "When in doubt, prefer REVISION — ambiguous short commands ('bigger', "
        "'darker', 'center it') almost always mean the prior artifact.",
        "",
        "## User's next request",
        user_text,
    ])
    return "\n".join(parts)


def _load_prior_layers(ref: ArtifactRef) -> list[dict] | None:
    if not ref.html_path:
        return None
    path = Path(ref.html_path)
    if not path.exists():
        return None
    try:
        return parse_html_layers(path)
    except Exception:
        return None


# --- Slash command dispatch -----------------------------------------------


def _dispatch_slash(line: str, state: dict) -> bool:
    """Returns True to continue REPL, False to exit."""
    parts = line[1:].split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    handlers = {
        "help":    _cmd_help,
        "h":       _cmd_help,
        "?":       _cmd_help,
        "save":    _cmd_save,
        "load":    _cmd_load,
        "new":     _cmd_new,
        "list":    _cmd_list,
        "ls":      _cmd_list,
        "history": _cmd_history,
        "tokens":  _cmd_tokens,
        "edit":    _cmd_edit,
        "attach":  _cmd_attach,
        "detach":  _cmd_detach,
        "export":  _cmd_export,
        "exit":    _cmd_exit,
        "quit":    _cmd_exit,
        "q":       _cmd_exit,
    }
    handler = handlers.get(cmd)
    if handler is None:
        print(f"  unknown command :{cmd}. Type :help.")
        return True
    return handler(arg, state)


# --- Slash command handlers -----------------------------------------------


def _cmd_help(arg: str, state: dict) -> bool:
    print(textwrap.dedent("""
        Commands:
          :help / :h / :?       show this
          :save [id]            save session (default: current session_id)
          :load <id>            replace current session with loaded one
          :new                  start fresh session (auto-saves current)
          :list / :ls           list recent sessions
          :history              show message history
          :tokens               cumulative runtime stats for this session
          :edit                 (stub) explains why natural-language edits are preferred
          :attach <path>        queue a PDF / MD / image for the NEXT turn (v1.1 paper2any)
          :detach               clear queued attachments
          :export [path]        copy artifacts + session to path (default: ~/Desktop/<id>)
          :exit / :quit / :q    exit (auto-saves)

        Anything not starting with ':' is a brief — sent to the agent
        as the next design turn. Prior artifacts in this session get
        summarized as context so the agent can revise vs create fresh.
    """).strip())
    return True


def _cmd_save(arg: str, state: dict) -> bool:
    session: ChatSession = state["session"]
    if arg:
        session.session_id = arg
    path = save_session(session, state["sessions_dir"])
    state["dirty"] = False
    print(f"  saved: {path}")
    return True


def _cmd_load(arg: str, state: dict) -> bool:
    if not arg:
        print("  usage: :load <session_id>")
        return True
    try:
        new = load_session(state["sessions_dir"], arg)
    except FileNotFoundError:
        print(f"  session not found: {arg}")
        return True
    if state["dirty"]:
        save_session(state["session"], state["sessions_dir"])
        print(f"  (auto-saved previous session {state['session'].session_id})")
    state["session"] = new
    print(f"  loaded {new.session_id}  ({len(new.artifacts)} artifact(s))")
    return True


def _cmd_new(arg: str, state: dict) -> bool:
    # Auto-save current session
    save_session(state["session"], state["sessions_dir"])
    print(f"  (auto-saved previous session {state['session'].session_id})")
    state["session"] = ChatSession(session_id=new_session_id())
    state["dirty"] = False
    print(f"  new session: {state['session'].session_id}")
    return True


def _cmd_list(arg: str, state: dict) -> bool:
    items = list_sessions(state["sessions_dir"])
    if not items:
        print("  (no sessions in this dir)")
        return True
    print(f"  recent sessions ({len(items)}):")
    for sid, updated, n_traj in items:
        marker = "*" if sid == state["session"].session_id else " "
        print(f"    {marker} {sid}  {updated.strftime('%Y-%m-%d %H:%M')}  {n_traj} artifact(s)")
    return True


def _cmd_history(arg: str, state: dict) -> bool:
    session: ChatSession = state["session"]
    if not session.message_history:
        print("  (no messages yet)")
        return True
    print(f"  session: {session.session_id}")
    for i, msg in enumerate(session.message_history, 1):
        stamp = msg.timestamp.strftime("%H:%M:%S")
        tag = f"[{msg.role}]"
        body = msg.content if len(msg.content) <= 200 else msg.content[:197] + "..."
        prefix = f"    {i:3d} {stamp} {tag:12s}"
        print(f"{prefix} {body}")
        if msg.artifact_id:
            print(f"        {' ' * 21}→ artifact {msg.artifact_id}")
    return True


def _cmd_tokens(arg: str, state: dict) -> bool:
    session: ChatSession = state["session"]
    if not session.artifacts:
        print("  no artifacts generated yet")
        return True
    print(f"  session: {session.session_id}")
    print(f"  artifacts:   {len(session.artifacts)}")
    print(f"  total wall:  {session.total_wall_s()}s")
    print(f"  per-artifact:")
    for i, t in enumerate(session.artifacts, 1):
        print(f"    [{i}] {t.artifact_type.value}  "
              f"{t.n_layers} layers  "
              f"{t.verdict}({t.score})  "
              f"{t.wall_s}s  "
              f"run_id={t.run_id}")
    return True


def _cmd_edit(arg: str, state: dict) -> bool:
    """Conversational edits go through the planner, not a slash shortcut.

    v1.0 #5 ships the `edit_layer` tool (planner-callable) but NOT a
    functional `:edit` slash. Rationale: natural language ("make the title
    bigger", "try red") gives the planner richer intent + leverages
    typography/color judgment the LLM already has. A slash with KV syntax
    (`:edit layer_001 font_size_px=280`) would be narrower UX for the same
    capability, and would require reconstructing a full ToolContext from the
    prior artifact on every edit — fragile plumbing for a worse experience.
    This handler exists to route the user towards the better path.
    """
    session: ChatSession = state["session"]
    if not session.artifacts:
        print("  no artifact yet in this session — describe what you want to make first.")
        return True
    latest = session.artifacts[-1]
    print(textwrap.dedent(f"""
          :edit is not a functional slash in v1.0 — use natural language instead.
          The planner picks up 'make the title bigger', 'try red', 'move the
          stamp down 40px', 'bolder shadow' etc. directly and will call the
          `edit_layer` tool under the hood for targeted text-layer tweaks.

          Latest artifact:
            run_id:       {latest.run_id}
            type:         {latest.artifact_type.value}
            layers:       {latest.n_layers}
            preview:      {latest.preview_path}

          Examples you can just type:
            make the title bigger and add a red drop shadow
            change the subtitle to '让流失海外的中华文物踏上归途'
            move the stamp to the top-left corner
            darker palette, keep the mood but more dramatic contrast
    """).strip())
    return True


def _cmd_attach(arg: str, state: dict) -> bool:
    """Queue a file for the next non-slash turn (v1.1 paper2any).

    Planner sees an injected prologue "Attached files: [...]" and calls
    `ingest_document` first; the extracted structure / figures drive the
    DesignSpec. Attachments are cleared after the next turn runs.
    """
    session: ChatSession = state["session"]
    if not arg:
        if session.pending_attachments:
            print("  queued for next turn:")
            for p in session.pending_attachments:
                print(f"    - {p}")
        else:
            print("  no attachments queued. :attach <path> to queue one.")
        return True

    fp = Path(arg).expanduser()
    if not fp.is_absolute():
        fp = fp.resolve()
    if not fp.exists() or not fp.is_file():
        print(f"  not found: {fp}")
        return True
    ext = fp.suffix.lower()
    supported = {".pdf", ".md", ".markdown", ".txt", ".png", ".jpg", ".jpeg", ".webp"}
    if ext not in supported:
        print(f"  unsupported type {ext!r}. Supported: {', '.join(sorted(supported))}")
        return True

    session.pending_attachments.append(str(fp))
    size_kb = fp.stat().st_size // 1024
    print(f"  ✓ queued: {fp.name} ({size_kb} KB). "
          f"Will be ingested on the next non-slash turn.")
    save_session(session, state["sessions_dir"])
    return True


def _cmd_detach(arg: str, state: dict) -> bool:
    session: ChatSession = state["session"]
    if not session.pending_attachments:
        print("  no attachments queued.")
        return True
    n = len(session.pending_attachments)
    session.pending_attachments = []
    save_session(session, state["sessions_dir"])
    print(f"  cleared {n} queued attachment(s).")
    return True


def _cmd_export(arg: str, state: dict) -> bool:
    session: ChatSession = state["session"]
    if not session.artifacts:
        print("  nothing to export — no artifacts in this session")
        return True
    dest = Path(arg).expanduser() if arg else Path.home() / "Desktop" / session.session_id
    dest.mkdir(parents=True, exist_ok=True)

    # Save a fresh copy of the session JSON into dest
    session_path_new = dest / f"{session.session_id}.json"
    with open(session_path_new, "w", encoding="utf-8") as f:
        payload = session.model_dump(mode="json")
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    print(f"  session → {session_path_new}")

    # Copy each artifact's run_dir content
    copied = 0
    for ref in session.artifacts:
        src_run_dir = Path(ref.run_dir)
        if not src_run_dir.exists():
            print(f"  (skipped missing run dir: {src_run_dir})")
            continue
        dst_dir = dest / ref.run_id
        if dst_dir.exists():
            shutil.rmtree(dst_dir)
        shutil.copytree(src_run_dir, dst_dir)
        copied += 1
    print(f"  copied {copied} artifact(s) → {dest}")
    return True


def _cmd_exit(arg: str, state: dict) -> bool:
    _handle_exit(state)
    return False


def _handle_exit(state: dict) -> None:
    session: ChatSession = state["session"]
    path = save_session(session, state["sessions_dir"])
    summary = (
        f"  saved {path.name}  "
        f"({len(session.artifacts)} artifact(s), "
        f"{session.total_wall_s()}s)"
    )
    print(summary)
    print("  bye.")


# --- Presentation ---------------------------------------------------------


def _result_to_ref(result: RunResult) -> ArtifactRef:
    """Derive a session-level ArtifactRef from the runtime run result."""
    try:
        artifact_type_enum = ArtifactType(result.artifact_type)
    except ValueError:
        artifact_type_enum = ArtifactType.POSTER

    run_dir = Path(result.run_dir)
    final_dir = run_dir / "final"

    def _maybe(name: str) -> str | None:
        p = final_dir / name
        if not p.exists():
            return None
        try:
            return str(p.resolve())
        except OSError:
            return str(p)

    return ArtifactRef(
        run_id=result.run_id,
        artifact_type=artifact_type_enum,
        created_at=_run_id_to_datetime(result.run_id) or datetime.now(),
        run_dir=str(run_dir),
        preview_path=_maybe("preview.png"),
        psd_path=_maybe("poster.psd"),
        svg_path=_maybe("poster.svg"),
        html_path=_maybe("poster.html") or _maybe("index.html") or _maybe("deck.html"),
        pdf_path=_maybe("deck.pdf"),
        pptx_path=_maybe("deck.pptx"),
        n_layers=result.n_layers,
        verdict=result.critic_verdict,
        score=result.critic_score,
        wall_s=float(result.wall_s),
    )


def _run_id_to_datetime(run_id: str) -> datetime | None:
    """run_id format is `YYYYMMDD-HHMMSS-<short>` (see util/ids.py)."""
    try:
        prefix = run_id.split("-", 2)
        if len(prefix) < 2:
            return None
        return datetime.strptime(f"{prefix[0]}-{prefix[1]}", "%Y%m%d-%H%M%S")
    except (ValueError, IndexError):
        return None


def _write_chat_events(
    out_dir: Path,
    session_id: str,
    brief: str,
    attachments: list[Path],
    result: RunResult,
    ref: ArtifactRef,
) -> None:
    append_design_event(
        out_dir,
        session_id,
        "message.user_submitted",
        run_id=result.run_id,
        data={"brief": brief},
    )
    for path in attachments:
        append_design_event(
            out_dir,
            session_id,
            "attachment.added",
            run_id=result.run_id,
            data=attachment_event_data(path),
        )
    event = "artifact.generated"
    if result.terminal_status in {"fail", "max_turns", "abort"}:
        event = "artifact.generation_failed"
    append_design_event(
        out_dir,
        session_id,
        event,
        run_id=result.run_id,
        artifact_id=f"art_{result.run_id}",
        data={
            "artifact_type": ref.artifact_type.value,
            "terminal_status": result.terminal_status,
            "critic_verdict": result.critic_verdict,
            "critic_score": result.critic_score,
            "n_layers": result.n_layers,
            "finalize_notes": result.finalize_notes,
            "paths": {
                "run_dir": ref.run_dir,
                "preview": ref.preview_path,
                "html": ref.html_path,
                "pdf": ref.pdf_path,
                "svg": ref.svg_path,
                "psd": ref.psd_path,
                "pptx": ref.pptx_path,
            },
        },
    )


def _assistant_summary(result: RunResult, ref: ArtifactRef) -> str:
    """User-facing one-line summary (goes into message_history as content)."""
    verdict_str = f"{ref.verdict}({ref.score:.2f})" if ref.verdict else "no critique"
    return (
        f"produced {ref.artifact_type.value} · {ref.n_layers} layers · "
        f"{verdict_str} · {ref.wall_s}s · "
        f"status={result.terminal_status} · run_id={ref.run_id}"
    )


def _display_turn_result(result: RunResult, ref: ArtifactRef,
                         elapsed: float, session: ChatSession) -> None:
    verdict_str = f"{ref.verdict} ({ref.score:.2f})" if ref.verdict else "no critique"
    status_icon = "✓" if result.terminal_status in {"pass", "revise"} else "!"
    print(f"\n  {status_icon} {ref.artifact_type.value} generated")
    print(f"    layers:     {ref.n_layers}")
    print(f"    critique:   {verdict_str}  (terminal: {result.terminal_status})")
    print(f"    timing:     {ref.wall_s}s wall, {elapsed:.1f}s elapsed")
    print(f"    run dir:    {ref.run_dir}")
    if ref.preview_path:
        print(f"    preview:    {ref.preview_path}")
    if ref.psd_path:
        print(f"    PSD:        {ref.psd_path}")
    if ref.svg_path:
        print(f"    SVG:        {ref.svg_path}")
    if ref.html_path:
        print(f"    HTML:       {ref.html_path}")
    if ref.pdf_path:
        print(f"    PDF:        {ref.pdf_path}")
    if ref.pptx_path:
        print(f"    PPTX:       {ref.pptx_path}")
    print(f"  session total: {len(session.artifacts)} artifact(s), "
          f"{session.total_wall_s()}s")
    print()
