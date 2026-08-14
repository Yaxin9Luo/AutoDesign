/**
 * Reduces the SSE event stream from `/api/runs/{id}/events` into a
 * UI-shaped progress object. The reducer is pure — replays well, easy
 * to unit-test, no React.
 *
 * Shape rationale: a long generation (5–35 min for video) needs three
 * orthogonal signals to feel alive:
 *   1. **Phase pipeline** — high-level step bar (enhancer → claim →
 *      designer → render → critic → video). User sees where we are.
 *   2. **Current sub-step** — what tool is firing right now. User
 *      sees movement inside the slow phase.
 *   3. **Activity tail** — last N events with relative timestamps.
 *      User can spot weirdness ("we keep retrying generate_image").
 *
 * Plus elapsed time + token/image counters for quantitative feedback.
 */

export type StepStatus = "pending" | "in_progress" | "done" | "skipped" | "warning";

/** All phases the UI knows about. The catalog is ordered roughly by
 *  fire time. Any event named outside this set still flows into the
 *  activity tail; only the pipeline visualization is limited. */
export type PhaseId =
  | "enhance"
  | "claim_graph"
  | "plan"
  | "render"
  | "review"
  | "video_compose"
  | "video_render";

export type ProgressMode = "generate" | "poster_code_edit" | "video_render" | string;

export interface PhaseDef {
  id: PhaseId;
  label: string;
  /** Hidden by default unless an event for it ever fires. Lets the
   *  pipeline visualization shrink for a no-PDF / no-video run. */
  conditional: boolean;
}

export const PHASES: readonly PhaseDef[] = [
  { id: "enhance", label: "Refine brief", conditional: true },
  { id: "claim_graph", label: "Read paper", conditional: true },
  { id: "plan", label: "Plan & compose", conditional: false },
  { id: "render", label: "Render artifact", conditional: false },
  { id: "review", label: "Review", conditional: false },
  { id: "video_compose", label: "Author video scenes", conditional: true },
  { id: "video_render", label: "Render video", conditional: true },
];

export const POSTER_CODE_EDIT_PHASES: readonly PhaseDef[] = [
  { id: "plan", label: "Prepare revision", conditional: false },
  { id: "render", label: "Validate poster", conditional: false },
  { id: "review", label: "Save revision", conditional: false },
];

export interface ActivityEvent {
  ts: number;        // epoch ms
  raw_event: string; // e.g. "generate_image.start"
  label: string;     // human-friendly
  detail?: string;   // optional secondary line (e.g. prompt snippet)
  body?: string;      // longer agent/status excerpt, already truncated server-side
  preview_url?: string;
  html_url?: string;
  attempt?: number;
  max_attempts?: number;
  category: "phase" | "tool" | "warning" | "error" | "info";
}

export interface AttemptPreview {
  ts: number;
  attempt: number;
  max_attempts?: number;
  preview_url: string;
  html_url?: string;
  detail?: string;
}

export interface RuntimeAlert {
  status_code?: number;
  title: string;
  message: string;
  hint: string;
  technical_detail: string;
}

export interface RunProgress {
  run_id: string;
  mode?: ProgressMode | null;
  started_at: number;
  /** Map of every phase the run has touched. Conditional phases stay
   *  out of this map until they're hit, so the pipeline draws shorter. */
  phases: Partial<Record<PhaseId, StepStatus>>;
  /** Current "leaf" tool — what the designer most recently invoked. */
  current_step?: string;
  current_step_started_at?: number;
  /** Capped tail of the most recent N events. */
  recent: ActivityEvent[];
  attempt_previews: AttemptPreview[];
  /** Most recent provider/runtime failure that needs prominent user attention. */
  runtime_alert?: RuntimeAlert;
  /** Quantitative counters surfaced as small badges in the card. */
  counts: {
    /** Designer.turn count = tool calls so far. */
    tool_calls: number;
    attempts: number;
    max_attempts?: number;
    images_done: number;
    images_started: number;
    warnings: number;
    /** Cumulative tokens (input + output). Updated when a `*.tokens`
     *  payload arrives — currently emitted by enhancer + critic +
     *  designer.done. */
    tokens?: number;
  };
  /** Terminal flag for the SSE consumer. */
  phase: "queued" | "running" | "cancelling" | "done" | "error";
  /** True only while the cancellation POST itself is outstanding. */
  cancel_request_in_flight?: boolean;
  /** One-liner shown as the card title's status. */
  label: string;
}

const TAIL_LIMIT = 24;

const AUTHOR_ATTEMPT_LABELS: Record<string, { artifact: string; step: string }> = {
  "designer_author.attempt_start": {
    artifact: "poster",
    step: "External poster author",
  },
  "slides_author.attempt_start": {
    artifact: "slides",
    step: "External slides author",
  },
  "landing_author.attempt_start": {
    artifact: "landing page",
    step: "External landing author",
  },
  "video_author.attempt_start": {
    artifact: "video",
    step: "External video author",
  },
};

export function initialProgress(run_id: string, mode?: ProgressMode | null): RunProgress {
  const now = Date.now();
  if (mode === "poster_code_edit") {
    return {
      run_id,
      mode,
      started_at: now,
      phases: { plan: "in_progress" },
      current_step: "Revision request queued",
      current_step_started_at: now,
      recent: [
        {
          ts: now,
          raw_event: "code_editor.requested",
          label: "Poster revision requested",
          category: "phase",
        },
      ],
      attempt_previews: [],
      counts: { tool_calls: 0, attempts: 0, images_done: 0, images_started: 0, warnings: 0 },
      phase: "running",
      label: "Preparing poster revision…",
    };
  }
  return {
    run_id,
    mode,
    started_at: now,
    phases: {},
    recent: [],
    attempt_previews: [],
    counts: { tool_calls: 0, attempts: 0, images_done: 0, images_started: 0, warnings: 0 },
    phase: "queued",
    label: "Starting up…",
  };
}

// ---------- helpers ----------

function setPhase(
  p: RunProgress, id: PhaseId, status: StepStatus
): RunProgress {
  return { ...p, phases: { ...p.phases, [id]: status } };
}

function pushEvent(
  p: RunProgress, ev: ActivityEvent
): RunProgress {
  const next = [ev, ...p.recent];
  if (next.length > TAIL_LIMIT) next.length = TAIL_LIMIT;
  return { ...p, recent: next };
}

function pushAttemptPreview(
  p: RunProgress, preview: AttemptPreview
): RunProgress {
  const existing = p.attempt_previews.filter((item) => item.attempt !== preview.attempt);
  return { ...p, attempt_previews: [...existing, preview].sort((a, b) => a.attempt - b.attempt) };
}

function setStep(
  p: RunProgress, name: string, ts: number
): RunProgress {
  return { ...p, current_step: name, current_step_started_at: ts };
}

function clearStep(p: RunProgress): RunProgress {
  return { ...p, current_step: undefined, current_step_started_at: undefined };
}

function recordAttempt(
  progress: RunProgress,
  attempt: number | undefined,
  maxAttempts: number | undefined,
): RunProgress {
  return {
    ...progress,
    counts: {
      ...progress.counts,
      attempts: Math.max(progress.counts.attempts, attempt ?? 0),
      ...(typeof maxAttempts === "number"
        ? {
            max_attempts: Math.max(
              progress.counts.max_attempts ?? 0,
              maxAttempts,
            ),
          }
        : {}),
    },
  };
}

// ---------- the reducer ----------

/**
 * Apply one SSE event to the current progress. The full backend payload
 * is passed in (not just event name) so we can extract details like
 * `prompt`, `model`, `layer_id` for the activity tail.
 */
export function applyEvent(
  prev: RunProgress, payload: Record<string, unknown>
): RunProgress {
  const event = String(payload.event ?? "");
  if (!event) return prev;
  const ts = Date.now();
  const base = event.startsWith("code_editor.") || event === "web.code_editor.error"
    ? { ...prev, mode: "poster_code_edit" }
    : prev;

  // ---- TERMINAL ----
  if (event === "run.done") {
    let next = setPhase(base, "review",
      base.phases.review === "in_progress" ? "done" : base.phases.review ?? "done");
    if (base.mode === "poster_code_edit") {
      next = setPhase(next, "plan", "done");
      next = setPhase(next, "render", "done");
    }
    if (base.phases.video_render === "in_progress") {
      next = setPhase(next, "video_render", "done");
    }
    return {
      ...clearStep(next),
      phase: "done",
      label: "Done.",
    };
  }
  if (event === "apply.done") {
    return { ...clearStep(base), phase: "done", label: "Edits applied." };
  }
  if (event === "openresearch.done") {
    return { ...clearStep(base), phase: "done", label: "OpenResearch submitted." };
  }
  if (event === "run.error") {
    return {
      ...clearStep(base),
      phase: "error",
      label: "Run failed.",
    };
  }
  if (event === "run.cancelled") {
    return {
      ...clearStep(base),
      phase: "error",
      label: "Run cancelled.",
    };
  }
  if (event === "openresearch.error") {
    return {
      ...clearStep(base),
      phase: "error",
      label: "OpenResearch failed.",
    };
  }

  // ---- PHASE TRANSITIONS ----
  // Any non-terminal event implies the run is now running, even if we
  // missed `run.start` due to the SSE-subscribe race (the client opens
  // the EventSource a few ms after the runner thread is scheduled —
  // the very first log can land on the floor).
  let next = base.phase === "queued"
    ? { ...base, phase: "running" as const }
    : base;
  const authorAttemptLabels = AUTHOR_ATTEMPT_LABELS[event];
  if (event === "run.start") {
    next = { ...next, label: "Starting up…", phase: "running" };
  } else if (event === "prompt.enhance.request") {
    next = setPhase(next, "enhance", "in_progress");
    next = { ...next, label: "Refining your brief…" };
    next = setStep(next, "Prompt enhancer", ts);
  } else if (event === "prompt.enhance.done") {
    next = setPhase(next, "enhance", "done");
    next = clearStep(next);
  } else if (event === "prompt.enhance.skipped") {
    next = setPhase(next, "enhance", "skipped");
  } else if (event === "prompt.enhance.error") {
    next = setPhase(next, "enhance", "warning");
    next = { ...next, counts: { ...next.counts, warnings: next.counts.warnings + 1 } };
  } else if (event === "claim_graph.start") {
    next = setPhase(next, "claim_graph", "in_progress");
    next = { ...next, label: "Reading the paper…" };
    next = setStep(next, "Claim graph extractor", ts);
  } else if (event === "claim_graph.done") {
    next = setPhase(next, "claim_graph", "done");
    next = clearStep(next);
  } else if (event === "claim_graph.skipped") {
    next = setPhase(next, "claim_graph", "skipped");
  } else if (event.startsWith("claim_graph.")) {
    // .degraded / .invalid / .failsafe — surface as a warning row but
    // don't fail the phase (extractor falls back to chapter ordering).
    if (next.phases.claim_graph === "in_progress") {
      next = setPhase(next, "claim_graph", "warning");
    }
    next = { ...next, counts: { ...next.counts, warnings: next.counts.warnings + 1 } };
  } else if (event === "deck_outline.start") {
    next = setPhase(next, "plan", "in_progress");
    next = { ...next, label: "Planning deck outline…" };
    next = setStep(next, "Deck outline designer", ts);
  } else if (event === "deck_outline.done") {
    next = setPhase(next, "plan", "in_progress");
    next = clearStep(next);
  } else if (event.startsWith("deck_outline.")) {
    next = { ...next, counts: { ...next.counts, warnings: next.counts.warnings + 1 } };
  } else if (event === "paper_memory_agent.start") {
    next = setPhase(next, "plan", "in_progress");
    next = { ...next, label: "Curating paper memory…" };
    next = setStep(next, "Paper memory curator", ts);
  } else if (event === "paper_memory_agent.turn_output") {
    const turn = numberField(payload, "turn");
    next = setPhase(next, "plan", "in_progress");
    next = setStep(next, turn ? `Paper memory turn ${turn}` : "Paper memory model output", ts);
  } else if (event === "paper_memory_agent.tool_call") {
    next = setPhase(next, "plan", "in_progress");
    next = setStep(next, `Paper memory · ${stringField(payload, "tool") ?? "tool call"}`, ts);
  } else if (event === "paper_memory_agent.done") {
    next = setPhase(next, "plan", "in_progress");
    next = clearStep(next);
  } else if (
    event === "paper_memory_agent.degraded"
    || event === "paper_memory_agent.api_error"
    || event === "paper_memory_agent.failed"
  ) {
    next = { ...next, counts: { ...next.counts, warnings: next.counts.warnings + 1 } };
  } else if (event.startsWith("paper_memory_agent.")) {
    next = setPhase(next, "plan", "in_progress");
  } else if (event === "designer_author.start") {
    next = setPhase(next, "plan", "in_progress");
    next = { ...next, label: "Preparing external poster author…" };
    next = setStep(next, "Staging paper evidence", ts);
  } else if (authorAttemptLabels) {
    const attempt = numberField(payload, "attempt");
    const maxAttempts = numberField(payload, "max_attempts");
    next = setPhase(next, "plan", "in_progress");
    next = {
      ...next,
      label: attempt
        ? `Authoring ${authorAttemptLabels.artifact} attempt ${attempt}${maxAttempts ? `/${maxAttempts}` : ""}…`
        : `Authoring ${authorAttemptLabels.artifact} attempt…`,
    };
    next = recordAttempt(next, attempt, maxAttempts);
    next = setStep(next, authorAttemptLabels.step, ts);
  } else if (event === "designer_author.wait") {
    next = setPhase(next, "plan", "in_progress");
    next = setStep(next, "External author is still running", ts);
  } else if (event === "designer_author.agent_output") {
    const attempt = numberField(payload, "attempt");
    next = {
      ...next,
      counts: { ...next.counts, attempts: Math.max(next.counts.attempts, attempt ?? 0) },
    };
    next = setStep(next, attempt ? `Reviewing attempt ${attempt} output` : "Reviewing author output", ts);
  } else if (event === "designer_author.attempt_preview") {
    const attempt = numberField(payload, "attempt");
    const maxAttempts = numberField(payload, "max_attempts");
    const previewRel = stringField(payload, "preview_relative_path");
    next = setPhase(next, "render", "in_progress");
    next = {
      ...next,
      label: attempt ? `Previewing poster attempt ${attempt}…` : "Previewing poster attempt…",
    };
    next = recordAttempt(next, attempt, maxAttempts);
    if (attempt && previewRel) {
      next = pushAttemptPreview(next, {
        ts,
        attempt,
        max_attempts: maxAttempts,
        preview_url: runFileUrl(payload, previewRel),
        html_url: stringField(payload, "html_relative_path")
          ? runFileUrl(payload, stringField(payload, "html_relative_path")!)
          : undefined,
        detail: stringField(payload, "preview_backend"),
      });
    }
  } else if (event === "designer_author.direct_final_validation_block") {
    next = setPhase(next, "render", "warning");
    next = {
      ...next,
      label: "Validation asked for a repair…",
      counts: { ...next.counts, warnings: next.counts.warnings + 1 },
    };
    next = setStep(next, "Poster validation feedback", ts);
  } else if (event === "designer_author.direct_final_validation_pass") {
    next = setPhase(next, "render", "in_progress");
    next = { ...next, label: "Poster attempt passed preflight…" };
    next = setStep(next, "Rendering preview", ts);
  } else if (event === "designer_author.retry") {
    next = setPhase(next, "plan", "in_progress");
    next = {
      ...next,
      label: "Repairing poster attempt…",
      counts: { ...next.counts, warnings: next.counts.warnings + 1 },
    };
    next = setStep(next, "Preparing repair prompt", ts);
  } else if (
    event === "designer_author.direct_final"
    || event === "designer_author.direct_final_soft_accept"
    || event === "designer_author.best_candidate_fallback"
    || event === "designer_author.best_available_artifact_fallback"
    || event === "designer_author.best_candidate_fallback_final"
    || event === "designer_author.best_available_artifact_fallback_final"
  ) {
    next = setPhase(next, "plan", "done");
    next = setPhase(next, "render", "done");
    next = setPhase(next, "review", "in_progress");
    next = { ...next, label: "Saving poster artifact…" };
    next = clearStep(next);
  } else if (event === "designer_author.attempt_preview_error") {
    next = { ...next, counts: { ...next.counts, warnings: next.counts.warnings + 1 } };
  } else if (
    event === "code_editor.requested"
    || event === "code_editor.start"
    || event === "code_editor.prepare"
  ) {
    next = setPhase(next, "plan", "in_progress");
    next = { ...next, label: "Preparing poster revision…" };
    next = setStep(next, "Preparing poster revision", ts);
  } else if (event === "code_editor.attempt_start") {
    const attempt = numberField(payload, "attempt");
    next = setPhase(next, "plan", "in_progress");
    next = { ...next, label: "Running code editor…" };
    next = {
      ...next,
      counts: { ...next.counts, attempts: Math.max(next.counts.attempts, attempt ?? 0) },
    };
    next = setStep(next, "External code editor", ts);
  } else if (event === "code_editor.agent_output") {
    const attempt = numberField(payload, "attempt");
    next = {
      ...next,
      counts: { ...next.counts, attempts: Math.max(next.counts.attempts, attempt ?? 0) },
    };
    next = setStep(next, attempt ? `Reviewing edit attempt ${attempt} output` : "Reviewing code editor output", ts);
  } else if (event === "code_editor.validate_preview") {
    next = setPhase(next, "plan", "done");
    next = setPhase(next, "render", "in_progress");
    next = { ...next, label: "Validating revised poster…" };
    next = setStep(next, "Poster preview capture", ts);
  } else if (event === "code_editor.attempt_ok") {
    next = setPhase(next, "plan", "done");
    next = setPhase(next, "render", "in_progress");
    next = clearStep(next);
  } else if (event === "code_editor.attempt_rejected" || event === "web.code_editor.error") {
    next = { ...next, counts: { ...next.counts, warnings: next.counts.warnings + 1 } };
  } else if (event === "artifact_export.requested" || event === "artifact_export.prepare") {
    next = setPhase(next, "plan", "in_progress");
    next = { ...next, label: "Preparing PowerPoint export…" };
    next = setStep(next, "Preparing export context", ts);
  } else if (event === "artifact_export.attempt_start") {
    const attempt = numberField(payload, "attempt");
    next = setPhase(next, "plan", "in_progress");
    next = { ...next, label: "Running export agent…" };
    next = {
      ...next,
      counts: { ...next.counts, attempts: Math.max(next.counts.attempts, attempt ?? 0) },
    };
    next = setStep(next, "Converting to PPTX", ts);
  } else if (event === "artifact_export.agent_output") {
    const attempt = numberField(payload, "attempt");
    next = {
      ...next,
      counts: { ...next.counts, attempts: Math.max(next.counts.attempts, attempt ?? 0) },
    };
    next = setStep(next, attempt ? `Reviewing export attempt ${attempt} output` : "Reviewing export output", ts);
  } else if (event === "artifact_export.attempt_ok") {
    next = setPhase(next, "plan", "done");
    next = setPhase(next, "render", "in_progress");
    next = { ...next, label: "Saving PowerPoint export…" };
    next = clearStep(next);
  } else if (event === "artifact_export.attempt_rejected" || event === "artifact_export.error") {
    next = { ...next, counts: { ...next.counts, warnings: next.counts.warnings + 1 } };
  } else if (event === "artifact_export.done") {
    next = setPhase(next, "plan", "done");
    next = setPhase(next, "render", "done");
    next = setPhase(next, "review", "done");
    next = { ...next, label: "PowerPoint export ready." };
    next = clearStep(next);
  } else if (event === "designer.start" || event === "planner.start") {
    next = setPhase(next, "plan", "in_progress");
    next = { ...next, label: "Planning the design…" };
  } else if (event === "designer.turn" || event === "planner.turn") {
    const turn = typeof payload.turn === "number" ? payload.turn : 0;
    next = {
      ...next,
      counts: { ...next.counts, tool_calls: turn },
    };
  } else if (event === "designer.api_error" || event === "planner.api_error") {
    next = { ...next, counts: { ...next.counts, warnings: next.counts.warnings + 1 } };
  } else if (event.startsWith("composite.") && event.endsWith(".start")) {
    next = setPhase(next, "render", "in_progress");
    next = { ...next, label: "Rendering the artifact…" };
    next = setStep(next, "Compositing layers", ts);
  } else if (
    event === "composite.done"
    || event === "composite.landing.done"
    || event === "composite.deck.done"
  ) {
    // composite.done is also the implicit "plan complete" signal — the
    // designer only finalizes after a successful composite.
    next = setPhase(next, "plan", "done");
    next = setPhase(next, "render", "done");
    next = clearStep(next);
  } else if (event.endsWith("_warning") && event.startsWith("composite.")) {
    next = { ...next, counts: { ...next.counts, warnings: next.counts.warnings + 1 } };
  } else if (event === "critic.start") {
    next = setPhase(next, "review", "in_progress");
    next = { ...next, label: "Reviewing the result…" };
    next = setStep(next, "Vision critic", ts);
  } else if (event === "critic.done") {
    next = setPhase(next, "review", "done");
    next = clearStep(next);
  } else if (event.startsWith("hyperframes.compose")) {
    next = setPhase(next, "video_compose",
      event.endsWith(".done") ? "done" : "in_progress");
    if (!event.endsWith(".done")) {
      next = { ...next, label: "Authoring video scenes…" };
      next = setStep(next, "Video composer", ts);
    } else {
      next = clearStep(next);
    }
  } else if (event === "export_video.render.start") {
    next = setPhase(next, "video_render", "in_progress");
    next = { ...next, label: "Rendering MP4…" };
    next = setStep(next, "HyperFrames render", ts);
  } else if (event === "export_video.render.done") {
    next = setPhase(next, "video_render", "done");
    next = clearStep(next);
  } else if (event === "export_video.render.error" || event === "export_video.render.timeout") {
    next = setPhase(next, "video_render", "warning");
    next = { ...next, counts: { ...next.counts, warnings: next.counts.warnings + 1 } };
  }

  // ---- TOOL ACTIVITY (sub-steps inside the designer phase) ----
  if (event.startsWith("ingest.") && event.endsWith(".start")) {
    next = setStep(next, "Ingesting document", ts);
  } else if (event === "ingest.pdf.ocr.start") {
    next = setStep(next, "OCRing PDF pages", ts);
  } else if (event === "generate_image.start") {
    next = setStep(next, "Generating image", ts);
    next = {
      ...next,
      counts: { ...next.counts, images_started: next.counts.images_started + 1 },
    };
  } else if (event === "generate_image.done" || event === "generate_image.ok") {
    next = {
      ...next,
      counts: { ...next.counts, images_done: next.counts.images_done + 1 },
    };
    if (next.current_step === "Generating image") next = clearStep(next);
  } else if (event === "generate_image.fail" || event === "generate_image.error") {
    next = { ...next, counts: { ...next.counts, warnings: next.counts.warnings + 1 } };
    if (next.current_step === "Generating image") next = clearStep(next);
  } else if (event === "propose_design_spec.start") {
    next = setStep(next, "Proposing design spec", ts);
  } else if (event === "propose_design_spec.done") {
    if (next.current_step === "Proposing design spec") next = clearStep(next);
  } else if (event === "render_text_layer.start") {
    next = setStep(next, "Rendering text layer", ts);
  }

  // Token accounting — opportunistic, only fires when payload has it.
  if (typeof payload.input_tokens === "number" && typeof payload.output_tokens === "number") {
    const t = (next.counts.tokens ?? 0) + payload.input_tokens + payload.output_tokens;
    next = { ...next, counts: { ...next.counts, tokens: t } };
  }

  if (isRuntimeApiErrorEvent(event)) {
    const rawError = stringField(payload, "error") ?? stringField(payload, "msg");
    if (rawError) {
      next = { ...next, runtime_alert: classifyRuntimeAlert(rawError) };
    }
  }

  // ---- ACTIVITY TAIL ----
  // We don't tail every micro-event — too noisy. Filter to events with
  // user-meaningful detail.
  const interesting =
    event.endsWith(".start")
    || event.endsWith(".done")
    || event.endsWith(".ok")
    || event.endsWith(".fail")
    || event.endsWith(".error")
    || event.endsWith(".api_error")
    || event.endsWith(".timeout")
    || event.endsWith("_warning")
    || event.endsWith(".skipped")
    || event.endsWith(".degraded")
    || event === "designer.turn"
    || event === "claim_graph.invalid"
    || event === "code_editor.agent_output"
    || (
      event.startsWith("designer_author.")
      && event !== "designer_author.wait"
      && event !== "designer_author.identical_repair_wait"
    );
  if (interesting) {
    let activity = formatActivityEvent(event, payload, ts);
    if (
      event === "paper_memory_agent.degraded"
      && stringField(payload, "reason") === "no_valid_dossier"
      && next.runtime_alert
    ) {
      activity = {
        ...activity,
        detail: "Paper memory was skipped because the upstream model request failed.",
      };
    }
    next = pushEvent(next, activity);
  }

  if (base.phase === "cancelling") {
    next = { ...next, phase: "cancelling", label: base.label };
  }

  return next;
}

// ---------- event → human-readable activity row ----------

/** Produce the {label, detail, category} bag for the activity tail. */
function formatActivityEvent(
  event: string, payload: Record<string, unknown>, ts: number
): ActivityEvent {
  // Default: pretty-print the event family.
  let label = humanizeEventName(event);
  let detail: string | undefined;
  let body: string | undefined;
  let preview_url: string | undefined;
  let html_url: string | undefined;
  let attempt: number | undefined;
  let max_attempts: number | undefined;
  let category: ActivityEvent["category"] = "tool";

  // Specific overrides — these are the ones with payload fields worth
  // surfacing.
  if (event === "designer.turn" || event === "planner.turn") {
    label = `Designer turn ${payload.turn ?? "?"}`;
    detail = typeof payload.n_messages === "number" ? `${payload.n_messages} messages` : undefined;
    category = "phase";
  } else if (event === "paper_memory_agent.turn_output") {
    label = `Paper memory model output · turn ${numberField(payload, "turn") ?? "?"}`;
    detail = [
      stringField(payload, "stop_reason"),
      tokenDetail(payload),
    ].filter(Boolean).join(" · ");
    const text = stringField(payload, "text_excerpt");
    const toolCalls = arrayField(payload, "tool_calls")
      .map((item) => {
        if (!isRecord(item)) return "";
        const name = stringField(item, "name");
        const summary = stringField(item, "summary");
        return [name, summary].filter(Boolean).join(": ");
      })
      .filter(Boolean);
    const bodyParts = [
      text ? text : "",
      toolCalls.length ? `Tool calls:\n${toolCalls.map((line) => `- ${line}`).join("\n")}` : "",
    ].filter(Boolean);
    if (bodyParts.length > 0) body = bodyParts.join("\n\n");
    category = "info";
  } else if (event === "paper_memory_agent.tool_call") {
    const tool = stringField(payload, "tool") ?? "tool";
    label = tool === "lookup_paper_memory"
      ? "Looking up paper evidence"
      : tool === "report_paper_memory_dossier"
        ? "Submitting paper memory dossier"
        : `Paper memory tool · ${tool}`;
    detail = payload.is_error ? "needs retry" : undefined;
    const summary = stringField(payload, "summary");
    const result = stringField(payload, "result_summary");
    body = [
      summary ? `Request: ${summary}` : "",
      result ? `Result: ${result}` : "",
    ].filter(Boolean).join("\n");
    category = payload.is_error ? "warning" : "info";
  } else if (event === "paper_memory_agent.done") {
    label = "Paper memory dossier ready";
    detail = [
      typeof payload.sections === "number" ? `${payload.sections} sections` : "",
      tokenDetail(payload),
    ].filter(Boolean).join(" · ");
    const sections = arrayField(payload, "section_summaries")
      .map((item) => {
        if (!isRecord(item)) return "";
        const title = stringField(item, "title") ?? "Section";
        const claim = stringField(item, "claim");
        const copy = stringField(item, "poster_copy");
        return [
          `${title}: ${claim ?? ""}`.trim(),
          copy ? `  Poster copy: ${copy}` : "",
        ].filter(Boolean).join("\n");
      })
      .filter(Boolean);
    if (sections.length > 0) body = sections.map((line) => `- ${line}`).join("\n");
    category = "phase";
  } else if (event === "paper_memory_agent.degraded") {
    label = "Paper memory curator degraded";
    detail = stringField(payload, "reason") ?? tokenDetail(payload);
    body = stringField(payload, "last_text_excerpt");
    category = "warning";
  } else if (event === "paper_memory_agent.api_error") {
    label = `Paper memory API error · turn ${numberField(payload, "turn") ?? "?"}`;
    const rawError = stringField(payload, "error");
    detail = rawError ? providerMessage(rawError) : undefined;
    category = "warning";
  } else if (AUTHOR_ATTEMPT_LABELS[event]) {
    attempt = numberField(payload, "attempt");
    max_attempts = numberField(payload, "max_attempts");
    const artifact = AUTHOR_ATTEMPT_LABELS[event].artifact;
    label = `${artifact.charAt(0).toUpperCase()}${artifact.slice(1)} attempt ${attempt ?? "?"} started`;
    detail = [
      max_attempts ? `${max_attempts} max` : "",
      payload.repair ? "repair pass" : "initial draft",
    ].filter(Boolean).join(" · ");
    category = "phase";
  } else if (
    event === "designer_author.agent_output"
    || event === "code_editor.agent_output"
    || event === "artifact_export.agent_output"
  ) {
    attempt = numberField(payload, "attempt");
    max_attempts = numberField(payload, "max_attempts");
    label = event.startsWith("artifact_export.")
      ? `Export agent output${attempt ? ` · attempt ${attempt}` : ""}`
      : event.startsWith("code_editor.")
      ? `Code editor output${attempt ? ` · attempt ${attempt}` : ""}`
      : `Author output${attempt ? ` · attempt ${attempt}` : ""}`;
    const elapsed = numberField(payload, "elapsed_s");
    detail = [
      stringField(payload, "status"),
      stringField(payload, "reason"),
      typeof elapsed === "number" ? `${elapsed.toFixed(1)}s` : "",
    ].filter(Boolean).join(" · ");
    const done = stringField(payload, "done_summary");
    const stdout = stringField(payload, "stdout_excerpt");
    const stderr = stringField(payload, "stderr_excerpt");
    const bodyParts = [
      done ? `Summary: ${done}` : "",
      stdout ? stdout : "",
      stderr ? `stderr: ${stderr}` : "",
    ].filter(Boolean);
    if (bodyParts.length > 0) body = bodyParts.join("\n\n");
    category = stringField(payload, "status") === "error" ? "warning" : "info";
  } else if (event === "designer_author.attempt_preview") {
    attempt = numberField(payload, "attempt");
    max_attempts = numberField(payload, "max_attempts");
    label = `Poster attempt ${attempt ?? "?"} preview`;
    detail = [
      max_attempts ? `${attempt ?? "?"}/${max_attempts}` : "",
      stringField(payload, "preview_backend"),
    ].filter(Boolean).join(" · ");
    const previewRel = stringField(payload, "preview_relative_path");
    if (previewRel) preview_url = runFileUrl(payload, previewRel);
    const htmlRel = stringField(payload, "html_relative_path");
    if (htmlRel) html_url = runFileUrl(payload, htmlRel);
    category = "info";
  } else if (event === "designer_author.direct_final_validation_block") {
    const issue = stringField(payload, "issue_id");
    label = "Poster validation requested repair";
    detail = issue ?? `${numberField(payload, "issue_count") ?? 0} issues`;
    category = "warning";
  } else if (event === "designer_author.retry") {
    label = `Repairing after attempt ${numberField(payload, "attempt") ?? "?"}`;
    detail = [
      stringField(payload, "stage"),
      stringField(payload, "issue_id"),
    ].filter(Boolean).join(" · ");
    category = "warning";
  } else if (event === "designer_author.direct_final_validation_pass") {
    label = `Attempt ${numberField(payload, "attempt") ?? "?"} passed preflight`;
    category = "phase";
  } else if (event === "designer_author.direct_final") {
    label = `Poster promoted from attempt ${numberField(payload, "attempt") ?? "?"}`;
    detail = stringField(payload, "preview_backend");
    category = "phase";
  } else if (
    event === "designer_author.best_candidate_fallback_final"
    || event === "designer_author.best_available_artifact_fallback_final"
  ) {
    label = `Poster fallback promoted from attempt ${numberField(payload, "attempt") ?? "?"}`;
    detail = [
      stringField(payload, "candidate_id"),
      stringField(payload, "source_issue_id"),
      stringField(payload, "acceptance_reason"),
    ].filter(Boolean).join(" · ");
    category = "phase";
  } else if (event === "designer_author.attempt_preview_error") {
    label = `Attempt ${numberField(payload, "attempt") ?? "?"} preview unavailable`;
    detail = stringField(payload, "error");
    category = "warning";
  } else if (event.startsWith("designer_author.")) {
    attempt = numberField(payload, "attempt");
    if (attempt) detail = `attempt ${attempt}`;
  } else if (event.startsWith("generate_image.")) {
    const prompt = typeof payload.prompt === "string" ? payload.prompt : undefined;
    if (prompt) detail = `"${truncate(prompt, 70)}"`;
    if (event.endsWith(".fail") || event.endsWith(".error")) category = "warning";
  } else if (event.startsWith("ingest.")) {
    const file = typeof payload.path === "string"
      ? String(payload.path).split("/").pop()
      : typeof payload.filename === "string" ? payload.filename : undefined;
    if (file) detail = file;
  } else if (event === "render_text_layer.start" || event === "render_text_layer.done") {
    const id = payload.layer_id;
    if (typeof id === "string") detail = id;
  } else if (event === "critic.done") {
    if (typeof payload.reward === "number") {
      detail = `reward ${payload.reward.toFixed(2)}`;
    }
    category = "phase";
  } else if (event.endsWith("_warning")) {
    category = "warning";
    label = humanizeEventName(event);
    if (typeof payload.layer_id === "string") detail = payload.layer_id;
    else if (typeof payload.reason === "string") detail = String(payload.reason);
  } else if (isRuntimeApiErrorEvent(event)) {
    category = "warning";
    const rawError = stringField(payload, "error") ?? stringField(payload, "msg");
    if (rawError) detail = providerMessage(rawError);
  } else if (event.endsWith(".error") || event.endsWith(".fail") || event.endsWith(".timeout")) {
    category = "warning";
    if (typeof payload.error === "string") detail = String(payload.error).slice(0, 80);
  } else if (event.endsWith(".start") || event.endsWith(".done") || event.endsWith(".ok")) {
    category = "tool";
  }

  return {
    ts,
    raw_event: event,
    label,
    detail,
    body,
    preview_url,
    html_url,
    attempt,
    max_attempts,
    category,
  };
}

/** "generate_image.fail" → "Generate image failed". A small bag of
 *  manual overrides; the fallback path replaces underscores with
 *  spaces and capitalizes. */
function humanizeEventName(event: string): string {
  const overrides: Record<string, string> = {
    "run.start": "Run started",
    "run.done": "Run complete",
    "prompt.enhance.request": "Refining brief",
    "prompt.enhance.done": "Brief refined",
    "prompt.enhance.skipped": "Brief refinement skipped",
    "claim_graph.start": "Reading paper",
    "claim_graph.done": "Paper read",
    "claim_graph.invalid": "Claim graph invalid (degraded)",
    "claim_graph.degraded": "Claim graph degraded",
    "claim_graph.skipped": "Claim graph skipped",
    "deck_outline.start": "Planning deck outline",
    "deck_outline.done": "Deck outline planned",
    "paper_memory_agent.start": "Curating paper memory",
    "paper_memory_agent.turn_output": "Paper memory model output",
    "paper_memory_agent.tool_call": "Paper memory tool call",
    "paper_memory_agent.done": "Paper memory curated",
    "paper_memory_agent.degraded": "Paper memory curator degraded",
    "paper_memory_agent.api_error": "Paper memory API error",
    "designer_author.start": "Preparing external author",
    "designer_author.attempt_start": "Poster attempt started",
    "slides_author.attempt_start": "Slides attempt started",
    "landing_author.attempt_start": "Landing page attempt started",
    "video_author.attempt_start": "Video attempt started",
    "designer_author.agent_output": "External author output",
    "designer_author.attempt_preview": "Poster attempt preview",
    "designer_author.direct_final_validation_block": "Poster validation blocked",
    "designer_author.direct_final_validation_pass": "Poster validation passed",
    "designer_author.retry": "Repairing poster",
    "designer_author.direct_final": "Poster promoted",
    "designer_author.attempt_preview_error": "Attempt preview unavailable",
    "code_editor.requested": "Poster revision requested",
    "code_editor.start": "Preparing poster revision",
    "code_editor.prepare": "Revision context staged",
    "code_editor.agent_output": "Code editor output",
    "code_editor.attempt_start": "Running code editor",
    "code_editor.attempt_ok": "Code editor output accepted",
    "code_editor.attempt_rejected": "Code editor output rejected",
    "code_editor.validate_preview": "Validating revised poster",
    "artifact_export.requested": "PowerPoint export requested",
    "artifact_export.prepare": "Preparing PowerPoint export",
    "artifact_export.attempt_start": "Running export agent",
    "artifact_export.agent_output": "Export agent output",
    "artifact_export.attempt_ok": "Export output accepted",
    "artifact_export.attempt_rejected": "Export output rejected",
    "artifact_export.done": "PowerPoint export ready",
    "designer.start": "Designer started",
    "designer.turn": "Designer turn",
    "designer.api_error": "Designer API error",
    "planner.start": "Designer started",
    "planner.turn": "Designer turn",
    "planner.api_error": "Designer API error",
    "ingest.pdf.ocr.start": "OCR started",
    "ingest.pdf.ocr.done": "OCR complete",
    "ingest.done": "Ingest complete",
    "generate_image.start": "Generating image",
    "generate_image.done": "Image done",
    "generate_image.ok": "Image done",
    "generate_image.fail": "Image failed",
    "propose_design_spec.start": "Proposing spec",
    "propose_design_spec.done": "Spec proposed",
    "render_text_layer.start": "Rendering text",
    "render_text_layer.done": "Text rendered",
    "composite.start": "Compositing",
    "composite.done": "Composited",
    "composite.landing.done": "Landing composited",
    "composite.deck.done": "Deck composited",
    "composite.text_overlap_warning": "Text overlap",
    "composite.bbox_aspect_warning": "Bbox aspect odd",
    "composite.callout_orphan_warning": "Callout orphan",
    "composite.closing_stub_warning": "Closing stub",
    "critic.start": "Critic started",
    "critic.done": "Critic done",
    "hyperframes.compose.done": "Video script done",
    "export_video.render.start": "Rendering MP4",
    "export_video.render.done": "MP4 rendered",
    "export_video.render.error": "MP4 render error",
    "export_video.render.timeout": "MP4 render timeout",
  };
  if (overrides[event]) return overrides[event];
  // Fallback: turn "foo_bar.baz_qux" → "Foo bar · baz qux"
  return event
    .replace(/_/g, " ")
    .replace(/\./g, " · ")
    .replace(/^\w/, (c) => c.toUpperCase());
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : `${s.slice(0, n - 1)}…`;
}

function isRuntimeApiErrorEvent(event: string): boolean {
  return event === "prompt.enhance.error" || event.endsWith(".api_error");
}

function classifyRuntimeAlert(rawError: string): RuntimeAlert {
  const statusCode = httpStatusCode(rawError);
  const lower = rawError.toLowerCase();
  let title = "Model provider request failed";
  let hint = "Check your provider settings and network, then retry.";

  if (statusCode === 401) {
    title = "Provider authentication failed";
    hint = "Check the provider API key and account access, then retry.";
  } else if (statusCode === 403) {
    title = "Model unavailable from the current network or region";
    hint = "Check your VPN, network, account access, or provider route, then retry.";
  } else if (statusCode === 429) {
    title = "Provider rate limit reached";
    hint = "Wait briefly or switch provider or model, then retry.";
  } else if (statusCode != null && statusCode >= 500) {
    title = "Provider service is temporarily unavailable";
    hint = "Wait briefly or switch provider, then retry.";
  } else if (lower.includes("timeout") || lower.includes("timed out")) {
    title = "Provider request timed out";
    hint = "Check the network or provider status, then retry.";
  }

  return {
    ...(statusCode != null ? { status_code: statusCode } : {}),
    title,
    message: providerMessage(rawError),
    hint,
    technical_detail: rawError,
  };
}

function httpStatusCode(rawError: string): number | undefined {
  const match = rawError.match(
    /(?:error\s+code\s*:\s*|["']code["']\s*:\s*)([1-5]\d{2})/i,
  );
  if (!match) return undefined;
  const value = Number(match[1]);
  return Number.isFinite(value) ? value : undefined;
}

function providerMessage(rawError: string): string {
  const nested = rawError.match(/["']message["']\s*:\s*["']([^"']+)["']/i);
  if (nested?.[1]) return nested[1].trim();
  return rawError.trim();
}

function stringField(payload: Record<string, unknown>, key: string): string | undefined {
  const value = payload[key];
  return typeof value === "string" && value.trim() ? value : undefined;
}

function arrayField(payload: Record<string, unknown>, key: string): unknown[] {
  const value = payload[key];
  return Array.isArray(value) ? value : [];
}

function numberField(payload: Record<string, unknown>, key: string): number | undefined {
  const value = payload[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function tokenDetail(payload: Record<string, unknown>): string | undefined {
  const input = numberField(payload, "input_tokens");
  const output = numberField(payload, "output_tokens");
  const total = (input ?? 0) + (output ?? 0);
  return total > 0 ? `${total.toLocaleString()} tokens` : undefined;
}

function runFileUrl(payload: Record<string, unknown>, relPath: string): string {
  const runId = stringField(payload, "run_id") ?? "";
  const cleanRun = encodeURIComponent(runId);
  const cleanRel = relPath
    .split("/")
    .filter(Boolean)
    .map((part) => encodeURIComponent(part))
    .join("/");
  return `/api/files/runs/${cleanRun}/${cleanRel}`;
}
