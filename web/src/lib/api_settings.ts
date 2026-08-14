/**
 * Unified per-browser API config: provider keys + base URLs + per-agent
 * model picks. Stored under `autodesign.config.v2`. Migrates legacy
 * `designanything.config.v2` and `designanything.api_keys.v1` values on read.
 *
 * Sole owner of the wire-format that maps to the FastAPI shim's
 * `_HEADER_TO_ENV` table — see scripts/web_server.py. Adding a new
 * provider or agent is two lines (catalog.ts entry + a header below).
 */

const STORAGE_KEY = "autodesign.config.v2";
const LEGACY_STORAGE_KEY = "designanything.config.v2";
const LEGACY_V1_KEY = "designanything.api_keys.v1";
const DEMO_USER_KEY = "autodesign.demo_user.v1";
const LEGACY_DEMO_USER_KEY = "designanything.demo_user.v1";
const DEMO_MODE_KEY = "autodesign.demo_mode.v1";
const LEGACY_DEMO_MODE_KEY = "designanything.demo_mode.v1";
const OPENAI_DIRECT_BASE_URL = "https://api.openai.com/v1";

export type ProviderId =
  | "openrouter"
  | "anthropic"
  | "openai"
  | "gemini"
  | "custom_openai";

export type AgentId =
  | "designer"
  | "enhancer"
  | "claim_graph"
  | "deck_outline"
  | "paper_memory"
  | "ingest"
  | "critic"
  | "composer"
  | "image"
  | "image_fallback";

export type CodingHarness =
  | "codex"
  | "claude"
  | "opencode"
  | "kimi"
  | "mimo"
  | "pi"
  | "zcode";

/** Per-agent model pick. `provider === undefined` (or `"auto"` from the
 *  UI) means "let the backend route based on which keys are
 *  configured" — equivalent to NOT sending an X-Provider-* header. */
export interface AgentChoice {
  provider?: ProviderId;
  /** Free-text — most providers expose hundreds of models; the catalog
   *  surfaces a curated few but power users paste exotic ids verbatim. */
  model?: string;
}

export interface OpenResearchConfig {
  enabled?: boolean;
  org_id?: string;
  repo_full_name?: string;
  token?: string;
  api_url?: string;
  submitter_cmd?: string;
  submitter_timeout_s?: string;
}

export interface HarnessConfig {
  designer_author?: CodingHarness;
  designer_author_model?: string;
  code_editor?: CodingHarness;
  code_editor_model?: string;
  /** Optional explicit API key for the harness CLI subprocess (claude →
   *  ANTHROPIC_API_KEY, codex → OPENAI_API_KEY). Distinct from the pipeline
   *  provider keys — only authenticates the coding-agent CLI. */
  harness_api_key?: string;
}

/** Coarse per-group model overrides for helper pipeline roles. One value each
 *  covers many agents, so users don't fill a model per agent. Empty text /
 *  vision use the backend defaults and never inherit the coding-agent model.
 *  Empty image → gpt-5-image-mini. */
export interface PipelineModelConfig {
  /** Text roles: enhancer/claim_graph/deck_outline/paper_memory/composer. */
  text?: string;
  /** Vision roles: ingest + critic. */
  vision?: string;
  /** Image generation. */
  image?: string;
}

export interface ApiConfig {
  /** Per-provider credentials. */
  keys: Partial<Record<ProviderId, string>>;
  /** Optional base URLs. Only relevant for `custom_openai` (always
   *  needed) and the rare Anthropic-via-proxy case. */
  bases: {
    custom_openai?: string;
    anthropic?: string;
  };
  /** Per-agent { provider, model }. Empty entries fall back to the
   *  backend's default for that role. */
  models: Partial<Record<AgentId, AgentChoice>>;
  /** Coarse text / vision / image model overrides for the pipeline. */
  pipeline_models: PipelineModelConfig;
  /** Browser-local coding harness picks for HTML authoring and revisions. */
  harnesses: HarnessConfig;
  /** Browser-local OpenResearch GUI submitter config. */
  openresearch: OpenResearchConfig;
}

interface PersistedShape {
  v: 2;
  config: ApiConfig;
}

const empty = (): ApiConfig => ({
  keys: {},
  bases: {},
  models: {},
  pipeline_models: {},
  harnesses: {},
  openresearch: {},
});

export function readConfig(): ApiConfig {
  const current = readV2Config(STORAGE_KEY);
  if (current) return current;

  const legacyV2 = readV2Config(LEGACY_STORAGE_KEY);
  if (legacyV2) {
    try {
      saveConfig(legacyV2);
      window.localStorage.removeItem(LEGACY_STORAGE_KEY);
    } catch {
      // Keep reading the legacy value if quota blocks migration.
    }
    return legacyV2;
  }

  // Migrate v1 — only had OpenRouter + Gemini keys.
  try {
    const legacy = window.localStorage.getItem(LEGACY_V1_KEY);
    if (legacy) {
      const parsed = JSON.parse(legacy) as {
        v?: number;
        keys?: { openrouter?: string; gemini?: string };
      };
      if (parsed?.v === 1 && parsed.keys) {
        const next: ApiConfig = empty();
        if (parsed.keys.openrouter) next.keys.openrouter = parsed.keys.openrouter;
        if (parsed.keys.gemini) next.keys.gemini = parsed.keys.gemini;
        // Persist under the v2 key so the next read is a hot path.
        try {
          saveConfig(next);
          window.localStorage.removeItem(LEGACY_V1_KEY);
        } catch {
          // Keep reading the legacy value if quota blocks migration.
        }
        return next;
      }
    }
  } catch {
    // fall through to empty
  }
  return empty();
}

function readV2Config(key: string): ApiConfig | null {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PersistedShape;
    if (parsed?.v === 2 && parsed.config) return normalizeConfig(parsed.config);
  } catch {
    // Ignore malformed browser-local settings and continue through fallbacks.
  }
  return null;
}

export function saveConfig(next: ApiConfig): void {
  // Strip empty strings so we don't pollute headers with blanks.
  const cleaned: ApiConfig = {
    keys: prune(next.keys),
    bases: prune(next.bases),
    models: {},
    pipeline_models: prunePipelineModels(next.pipeline_models ?? {}),
    harnesses: pruneHarnesses(next.harnesses ?? {}),
    openresearch: pruneOpenResearch(next.openresearch ?? {}),
  };
  for (const [k, v] of Object.entries(next.models)) {
    if (!isAgentId(k)) continue;
    if (!v) continue;
    const provider = v.provider && v.provider !== ("auto" as ProviderId)
      ? v.provider : undefined;
    const model = v.model?.trim() || undefined;
    if (!provider && !model) continue;
    cleaned.models[k as AgentId] = {
      ...(provider ? { provider } : {}),
      ...(model ? { model } : {}),
    };
  }
  const payload: PersistedShape = { v: 2, config: cleaned };
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
}

export function clearConfig(): void {
  window.localStorage.removeItem(STORAGE_KEY);
  window.localStorage.removeItem(LEGACY_STORAGE_KEY);
  window.localStorage.removeItem(LEGACY_V1_KEY);
}

function prune<T extends Record<string, string | undefined>>(
  obj: T
): T {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(obj)) {
    if (v && v.trim()) out[k] = v.trim();
  }
  return out as T;
}

function normalizeConfig(raw: Partial<ApiConfig>): ApiConfig {
  const incoming = raw.models as (Partial<Record<AgentId | "planner" | string, AgentChoice>> | undefined);
  const models: Partial<Record<AgentId, AgentChoice>> = {};
  for (const [k, v] of Object.entries(incoming ?? {})) {
    if (isAgentId(k)) models[k] = v;
  }
  if (!models.designer && incoming?.planner) {
    models.designer = incoming.planner;
  }
  return {
    keys: raw.keys ?? {},
    bases: raw.bases ?? {},
    models,
    pipeline_models: prunePipelineModels(raw.pipeline_models ?? {}),
    harnesses: pruneHarnesses(raw.harnesses ?? {}),
    openresearch: raw.openresearch ?? {},
  };
}

function prunePipelineModels(raw: Partial<PipelineModelConfig>): PipelineModelConfig {
  const out: PipelineModelConfig = {};
  if (raw.text?.trim()) out.text = raw.text.trim();
  if (raw.vision?.trim()) out.vision = raw.vision.trim();
  if (raw.image?.trim()) out.image = raw.image.trim();
  return out;
}

const CODING_HARNESSES = new Set<CodingHarness>([
  "codex",
  "claude",
  "opencode",
  "kimi",
  "mimo",
  "pi",
  "zcode",
]);

function pruneHarnesses(raw: Partial<HarnessConfig>): HarnessConfig {
  const out: HarnessConfig = {};
  if (raw.designer_author && CODING_HARNESSES.has(raw.designer_author)) {
    out.designer_author = raw.designer_author;
  }
  if (raw.designer_author_model?.trim()) {
    out.designer_author_model = raw.designer_author_model.trim();
  }
  if (raw.code_editor && CODING_HARNESSES.has(raw.code_editor)) {
    out.code_editor = raw.code_editor;
  }
  if (raw.code_editor_model?.trim()) {
    out.code_editor_model = raw.code_editor_model.trim();
  }
  if (raw.harness_api_key?.trim()) {
    out.harness_api_key = raw.harness_api_key.trim();
  }
  return out;
}

function pruneOpenResearch(raw: OpenResearchConfig): OpenResearchConfig {
  const out: OpenResearchConfig = {};
  if (raw.enabled !== undefined) out.enabled = !!raw.enabled;
  const org_id = raw.org_id?.trim();
  const repo_full_name = raw.repo_full_name?.trim();
  const token = raw.token?.trim();
  const api_url = raw.api_url?.trim();
  const submitter_cmd = raw.submitter_cmd?.trim();
  const submitter_timeout_s = raw.submitter_timeout_s?.trim();
  if (org_id) out.org_id = org_id;
  if (repo_full_name) out.repo_full_name = repo_full_name;
  if (token) out.token = token;
  if (api_url) out.api_url = api_url;
  if (submitter_cmd) out.submitter_cmd = submitter_cmd;
  if (submitter_timeout_s) out.submitter_timeout_s = submitter_timeout_s;
  return out;
}

/** Convert config to the HTTP headers the FastAPI shim consumes. */
export function configHeaders(cfg: ApiConfig): Record<string, string> {
  const h: Record<string, string> = {};
  if (cfg.keys.openrouter) h["X-OpenRouter-Key"] = cfg.keys.openrouter;
  if (cfg.keys.anthropic) h["X-Anthropic-Key"] = cfg.keys.anthropic;
  if (cfg.keys.openai) h["X-OpenAI-Key"] = cfg.keys.openai;
  if (cfg.keys.gemini) h["X-Gemini-Key"] = cfg.keys.gemini;
  if (cfg.keys.custom_openai) h["X-OpenAI-Key"] = cfg.keys.custom_openai;
  if (cfg.bases.custom_openai) h["X-Custom-OpenAI-Base"] = cfg.bases.custom_openai;
  else if (cfg.keys.openai && !cfg.keys.custom_openai) {
    h["X-Custom-OpenAI-Base"] = OPENAI_DIRECT_BASE_URL;
  }
  if (cfg.bases.anthropic) h["X-Anthropic-Base"] = cfg.bases.anthropic;

  const H = cfg.harnesses ?? {};
  if (H.designer_author) h["X-Designer-Author-Harness"] = H.designer_author;
  if (H.designer_author_model) h["X-Designer-Author-Model"] = H.designer_author_model;
  if (H.harness_api_key) h["X-Designer-Author-Key"] = H.harness_api_key;
  if (H.code_editor) h["X-Code-Editor-Harness"] = H.code_editor;
  if (H.code_editor_model) h["X-Code-Editor-Model"] = H.code_editor_model;

  const O = cfg.openresearch;
  if (O.enabled === true) h["X-OpenResearch-Submitter"] = "custom";
  else if (O.enabled === false) h["X-OpenResearch-Submitter"] = "off";
  if (O.org_id) h["X-OpenResearch-Org-Id"] = O.org_id;
  if (O.repo_full_name) h["X-OpenResearch-Repo"] = O.repo_full_name;
  if (O.token) h["X-OpenResearch-Token"] = O.token;
  if (O.api_url) h["X-OpenResearch-Api-Url"] = O.api_url;
  if (O.submitter_cmd) h["X-OpenResearch-Submitter-Cmd"] = O.submitter_cmd;
  if (O.submitter_timeout_s) {
    h["X-OpenResearch-Submitter-Timeout"] = O.submitter_timeout_s;
  }

  const M = cfg.models;
  const designerModel = wireModelForAgent("designer", M.designer);
  const enhancerModel = wireModelForAgent("enhancer", M.enhancer);
  const claimGraphModel = wireModelForAgent("claim_graph", M.claim_graph);
  const deckOutlineModel = wireModelForAgent("deck_outline", M.deck_outline);
  const paperMemoryModel = wireModelForAgent("paper_memory", M.paper_memory);
  const ingestModel = wireModelForAgent("ingest", M.ingest);
  const criticModel = wireModelForAgent("critic", M.critic);
  const composerModel = wireModelForAgent("composer", M.composer);
  const imageModel = wireModelForAgent("image", M.image);
  const imageFallbackModel = wireModelForAgent("image_fallback", M.image_fallback);
  if (designerModel) h["X-Model-Designer"] = designerModel;
  if (enhancerModel) h["X-Model-Enhancer"] = enhancerModel;
  if (claimGraphModel) h["X-Model-Claim-Graph"] = claimGraphModel;
  if (deckOutlineModel) h["X-Model-Deck-Outline"] = deckOutlineModel;
  if (paperMemoryModel) h["X-Model-Paper-Memory"] = paperMemoryModel;
  if (ingestModel) h["X-Model-Ingest"] = ingestModel;
  if (criticModel) h["X-Model-Critic"] = criticModel;
  if (composerModel) h["X-Model-Composer"] = composerModel;
  if (imageModel) h["X-Model-Image"] = imageModel;
  if (imageFallbackModel) h["X-Model-Image-Fallback"] = imageFallbackModel;

  // Coarse helper-model groups (text / vision / image). Empty text/vision
  // fields send no header so the backend's gpt-5.4-nano defaults apply. They
  // intentionally never override Designer or inherit the coding harness.
  const PM = cfg.pipeline_models ?? {};
  const textModel = PM.text?.trim();
  const visionModel = PM.vision?.trim();
  const pipelineImageModel = PM.image?.trim() || "openai/gpt-5-image-mini";
  if (textModel) {
    h["X-Model-Enhancer"] = textModel;
    h["X-Model-Claim-Graph"] = textModel;
    h["X-Model-Deck-Outline"] = textModel;
    h["X-Model-Paper-Memory"] = textModel;
    h["X-Model-Composer"] = textModel;
  }
  if (visionModel) {
    h["X-Model-Ingest"] = visionModel;
    h["X-Model-Critic"] = visionModel;
  }
  if (pipelineImageModel) h["X-Model-Image"] = pipelineImageModel;

  // Provider override is only sent when the user picked a *direct*
  // provider for an agent — auto/openrouter just lets the backend's
  // existing routing logic resolve.
  for (const [agent, pick] of Object.entries(M)) {
    if (!isAgentId(agent)) continue;
    if (agent === "image" || agent === "image_fallback") continue;
    if (agent === "ingest") continue;
    if (!pick?.provider || pick.provider === "openrouter") continue;
    const tag = providerToBackendTag(pick.provider);
    if (!tag) continue;
    const headerName = `X-Provider-${capAgent(agent as AgentId)}`;
    h[headerName] = tag;
  }
  return h;
}

function demoUserId(): string {
  const existing = readMigratedValue(DEMO_USER_KEY, LEGACY_DEMO_USER_KEY);
  if (existing) return existing;
  const generated = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `demo_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
  window.localStorage.setItem(DEMO_USER_KEY, generated);
  return generated;
}

export function currentDemoUserScope(): string {
  return demoUserId();
}

function demoHeaders(): Record<string, string> {
  const id = demoUserId();
  document.cookie = [
    `autodesign_demo_user=${encodeURIComponent(id)}`,
    "Path=/",
    "SameSite=Lax",
    "Max-Age=31536000",
  ].join("; ");
  return { "X-Demo-User": id };
}

export function setDemoMode(enabled: boolean): void {
  window.localStorage.setItem(DEMO_MODE_KEY, enabled ? "1" : "0");
  window.localStorage.removeItem(LEGACY_DEMO_MODE_KEY);
}

function isDemoMode(): boolean {
  return readMigratedValue(DEMO_MODE_KEY, LEGACY_DEMO_MODE_KEY) === "1";
}

function readMigratedValue(key: string, legacyKey: string): string | null {
  const current = window.localStorage.getItem(key);
  if (current !== null) return current;
  const legacy = window.localStorage.getItem(legacyKey);
  if (legacy === null) return null;
  try {
    window.localStorage.setItem(key, legacy);
    window.localStorage.removeItem(legacyKey);
  } catch {
    // Return the legacy value if quota blocks migration.
  }
  return legacy;
}

/** Map a provider id to the backend tag the runner understands.
 *  See `_parse_provider` in autodesign/config.py. */
function providerToBackendTag(p: ProviderId): string | null {
  switch (p) {
    case "anthropic": return "anthropic";
    case "openai":
    case "custom_openai": return "openai_compat";
    case "openrouter": return "auto";        // auto is fine — backend picks
    case "gemini": return null;              // gemini routing isn't per-agent
  }
}

function normalizeModelId(model: string | undefined): string | undefined {
  const cleaned = model?.trim();
  if (!cleaned) return undefined;
  switch (cleaned) {
    case "gemini-3-pro":
    case "gemini-3-pro-preview":
    case "gemini-3.1-pro":
      return "gemini-3.1-pro-preview";
    case "google/gemini-3-pro":
    case "google/gemini-3-pro-preview":
    case "google/gemini-3.1-pro":
      return "google/gemini-3.1-pro-preview";
    default:
      return cleaned;
  }
}

function wireModelForAgent(agent: AgentId, pick: AgentChoice | undefined): string | undefined {
  const model = normalizeModelId(pick?.model);
  if (!model) return undefined;
  // Direct Gemini LLM routing is not implemented; only the image backend can
  // call first-party Gemini. Salvage old non-image picks by sending the
  // equivalent OpenRouter id when the user/backend has OpenRouter creds.
  if (pick?.provider === "gemini" && !isImageAgent(agent)) {
    return model.startsWith("gemini-") ? `google/${model}` : model;
  }
  return model;
}

function isImageAgent(agent: AgentId): boolean {
  return agent === "image" || agent === "image_fallback";
}

function isAgentId(value: string): value is AgentId {
  return value === "designer"
    || value === "enhancer"
    || value === "claim_graph"
    || value === "deck_outline"
    || value === "paper_memory"
    || value === "ingest"
    || value === "critic"
    || value === "composer"
    || value === "image"
    || value === "image_fallback";
}

function capAgent(a: AgentId): string {
  const map: Record<AgentId, string> = {
    designer: "Designer",
    enhancer: "Enhancer",
    claim_graph: "Claim-Graph",
    deck_outline: "Deck-Outline",
    paper_memory: "Paper-Memory",
    ingest: "Ingest",
    critic: "Critic",
    composer: "Composer",
    image: "Image",
    image_fallback: "Image-Fallback",
  };
  return map[a];
}

/** "sk-or-v1-1234abcd5678..." → "sk-or-v1-1234…cd5678" for display. */
export function maskKey(key: string | undefined): string {
  if (!key) return "";
  if (key.length <= 12) return "••••";
  return `${key.slice(0, 8)}…${key.slice(-6)}`;
}

// ---------- Legacy named exports (Pass 7 callers) -----------------------------

/** @deprecated Use `readConfig().keys`. Kept so the empty-hero footer
 *  doesn't have to import the larger `readConfig`. */
export function readKeys(): { openrouter?: string; gemini?: string } {
  const c = readConfig();
  return { openrouter: c.keys.openrouter, gemini: c.keys.gemini };
}

/** Headers shim — what the old `keys.ts` exported. Kept as the single
 *  pass-through point for `api.ts`. */
export function keyHeaders(): Record<string, string> {
  if (isDemoMode()) return demoHeaders();
  return { ...configHeaders(readConfig()), ...demoHeaders() };
}
