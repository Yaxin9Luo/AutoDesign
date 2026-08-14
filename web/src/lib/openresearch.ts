import type { OpenResearchArtifactState } from "./types";

export interface OpenResearchSubmitOptions {
  org_id?: string;
  paper_id?: string;
  paper_url?: string;
  repo_full_name?: string;
  agent_prompt?: string;
}

export function openResearchResultHref(
  state?: OpenResearchArtifactState | null,
): string | null {
  return (
    state?.latest_report_url ||
    state?.gui_submitter_session_url ||
    state?.project_url ||
    state?.result_url ||
    null
  );
}

export function openResearchStatusLabel(
  state?: OpenResearchArtifactState | null,
): string {
  if (state?.status === "running") return "Submitting";
  if (state?.status === "submitted") {
    if (state.latest_report_url) return "Report ready";
    if (state.gui_submitter_status === "submitted") return "Submitted";
    return "Project ready";
  }
  if (state?.status === "error") return "Submit failed";
  return "OpenResearch";
}

export function openResearchStatusMessage(
  state?: OpenResearchArtifactState | null,
): string {
  const message = (
    state?.error ||
    state?.gui_submitter_error ||
    state?.gui_submitter_reason ||
    ""
  );
  if (message === "submitter_disabled") {
    return "OpenResearch submitter is disabled. Open Settings > OpenResearch and enable it.";
  }
  if (message === "missing_submitter_cmd") {
    return "OpenResearch submitter command is missing. Open Settings > OpenResearch; auto-detect Codex or set an Advanced submitter command.";
  }
  return message;
}

export function openResearchNeedsPaperId(
  state?: OpenResearchArtifactState | null,
): boolean {
  const message = openResearchStatusMessage(state).toLowerCase();
  return state?.status === "error" && /paper id|arxiv|paper url/.test(message);
}

export function openResearchSubmitOptionsFromPaperInput(
  input: string,
): OpenResearchSubmitOptions {
  const value = input.trim();
  if (!value) return {};
  if (/^https?:\/\//i.test(value)) return { paper_url: value };
  return { paper_id: value };
}
