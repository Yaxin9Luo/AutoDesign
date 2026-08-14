/**
 * Static catalog of providers + agents the UI knows about. Adding a new
 * provider here + a header line in `api_settings.ts.configHeaders` is
 * enough to expose it end-to-end — backend already routes through the
 * existing three SDK shells (anthropic / openai_compat / gemini).
 *
 * Model lists are intentionally curated: each provider exposes hundreds
 * to thousands of model ids, and we'd rather show a small, opinionated
 * list of the picks that have been shaken out on this codebase than
 * dump the full registry. Power users get a free-text "Custom…" entry
 * that round-trips any model id verbatim.
 */

import type { AgentId, ProviderId } from "./api_settings";

export interface ProviderSpec {
  id: ProviderId;
  /** Display label. */
  name: string;
  /** One-line description shown under the name in Settings. */
  blurb: string;
  /** Where to grab a key. Surfaced as a small "Get key →" link. */
  docs_url: string;
  /** Used by the masked-input placeholder. */
  key_placeholder: string;
  /** Custom-OpenAI providers also need a base URL. */
  needs_base_url?: boolean;
  base_url_placeholder?: string;
  /** Curated list of recommended models. The ":vision" suffix is a UI
   *  marker (stripped before sending) so the agent picker can flag a
   *  model as vision-capable. */
  models: string[];
}

export const PROVIDERS: readonly ProviderSpec[] = [
  {
    id: "openrouter",
    name: "OpenRouter",
    blurb: "Single key, hundreds of models. Recommended starting point.",
    docs_url: "https://openrouter.ai/keys",
    key_placeholder: "sk-or-v1-…",
    // Curated as of 2026-05. Grouped roughly by family in
    // BasePicker so the dropdown is scannable. Vision-capable
    // entries are also listed in `VISION_MODELS` below — Critic
    // / Ingest agents only show those.
    models: [
      // Anthropic
      "anthropic/claude-opus-4-8",
      "anthropic/claude-opus-4-7",
      "anthropic/claude-sonnet-4-6",
      "anthropic/claude-haiku-4-5",
      // OpenAI
      "openai/gpt-5.4-nano",
      "openai/gpt-5.5",
      "openai/gpt-5",
      "openai/gpt-5-mini",
      "openai/gpt-5-nano",
      "openai/gpt-5-image-mini",
      "openai/o4",
      "openai/o4-mini",
      // Google
      "google/gemini-3.1-pro-preview",
      "google/gemini-2.5-pro",
      "google/gemini-2.5-flash",
      "google/gemini-2.5-flash-image",
      // xAI
      "x-ai/grok-4",
      // Meta
      "meta-llama/llama-4-maverick",
      "meta-llama/llama-4-scout",
      // DeepSeek
      "deepseek/deepseek-v3.2",
      "deepseek/deepseek-v3.2-exp",
      "deepseek/deepseek-r1",
      // Moonshot
      "moonshotai/kimi-k2.6",
      "moonshotai/kimi-k2-thinking",
      // Qwen
      "qwen/qwen3-max",
      "qwen/qwen3-coder",
      "qwen/qwen-vl-max",
      // Z AI / GLM
      "z-ai/glm-4.6",
      "z-ai/glm-4.5-air",
      "z-ai/glm-4.5v",
      // ByteDance (image)
      "bytedance-seed/seedream-4.5",
    ],
  },
  {
    id: "anthropic",
    name: "Anthropic (Direct)",
    blurb: "First-party Claude API. Direct billing with Anthropic.",
    docs_url: "https://console.anthropic.com/settings/keys",
    key_placeholder: "sk-ant-…",
    models: [
      "claude-opus-4-8",
      "claude-opus-4-7",
      "claude-sonnet-4-6",
      "claude-haiku-4-5",
    ],
  },
  {
    id: "openai",
    name: "OpenAI (Direct)",
    blurb: "First-party OpenAI API. GPT-5 / o-series / image models.",
    docs_url: "https://platform.openai.com/api-keys",
    key_placeholder: "sk-proj-…",
    models: [
      "gpt-5.4-nano",
      "gpt-5.5",
      "gpt-5",
      "gpt-5-mini",
      "gpt-5-nano",
      "gpt-5-image-mini",
      "o4",
      "o4-mini",
    ],
  },
  {
    id: "gemini",
    name: "Google Gemini (Direct)",
    blurb: "First-party Gemini API. Used for image generation in this app.",
    docs_url: "https://aistudio.google.com/apikey",
    key_placeholder: "AIza…",
    models: [
      "gemini-3.1-pro-preview",
      "gemini-2.5-pro",
      "gemini-2.5-flash",
      "gemini-2.5-flash-image",
    ],
  },
  {
    id: "custom_openai",
    name: "Custom OpenAI-compatible",
    blurb: "Anything that speaks /v1/chat/completions — Together, "
         + "Fireworks, Moonshot direct, vLLM, llama.cpp, …",
    docs_url: "",
    key_placeholder: "your-api-key",
    needs_base_url: true,
    base_url_placeholder: "https://api.together.xyz/v1",
    models: ["gpt-5.5", "gpt-5.4-nano", "gpt-5.4"],
  },
];

/** Model ids that accept image inputs. The Critic + Ingest agents
 *  filter the dropdown to only these entries. Anything not in here is
 *  hidden when an agent's `vision: true` flag is set. */
export const VISION_MODELS: readonly string[] = [
  // OpenAI-compatible / internal
  "gpt-5.4-nano",
  "gpt-5.4",
  "gpt-5.5",
  // OpenRouter prefixed
  "anthropic/claude-opus-4-8",
  "anthropic/claude-opus-4-7",
  "anthropic/claude-sonnet-4-6",
  "anthropic/claude-haiku-4-5",
  "openai/gpt-5.4-nano",
  "openai/gpt-5.5",
  "openai/gpt-5",
  "openai/gpt-5-mini",
  "openai/o4",
  "google/gemini-3.1-pro-preview",
  "google/gemini-2.5-pro",
  "google/gemini-2.5-flash",
  "x-ai/grok-4",
  "qwen/qwen-vl-max",
  "z-ai/glm-4.5v",
  // Anthropic direct
  "claude-opus-4-7",
  "claude-opus-4-8",
  "claude-sonnet-4-6",
  "claude-haiku-4-5",
  // OpenAI direct
  "gpt-5",
  "gpt-5-mini",
  "o4",
  // Gemini direct
  "gemini-3.1-pro-preview",
  "gemini-2.5-pro",
  "gemini-2.5-flash",
];

/** Image-generation model ids. Surfaced when the agent is `image`
 *  (which only runs through the image_backend, not the LLM backend). */
export const IMAGE_MODELS: readonly string[] = [
  "google/gemini-2.5-flash-image",
  "openai/gpt-5-image-mini",
  "bytedance-seed/seedream-4.5",
  "gemini-2.5-flash-image",
  "gpt-5-image-mini",
];

export interface AgentSpec {
  id: AgentId;
  /** Display label. */
  label: string;
  /** One-line role description. */
  blurb: string;
  /** True if the agent must be vision-capable (consumes images). */
  vision: boolean;
}

export const AGENTS: readonly AgentSpec[] = [
  {
    id: "designer",
    label: "Designer",
    blurb: "Main API model for non-external design planning and tool calls.",
    vision: false,
  },
  {
    id: "enhancer",
    label: "Prompt Enhancer",
    blurb: "Refines the user brief before planning; fails open to the raw prompt.",
    vision: false,
  },
  {
    id: "claim_graph",
    label: "Claim Graph Extractor",
    blurb: "Extracts paper claims through API tool calls and quote validation.",
    vision: false,
  },
  {
    id: "deck_outline",
    label: "Deck Outline Designer",
    blurb: "Chooses slide count and outline after document ingest.",
    vision: false,
  },
  {
    id: "paper_memory",
    label: "Paper Memory Curator",
    blurb: "Builds validated source-backed evidence dossiers for paper posters.",
    vision: false,
  },
  {
    id: "ingest",
    label: "Document Ingest",
    blurb: "OCR, structure extraction, figure captions, and table parsing for attached documents.",
    vision: true,
  },
  {
    id: "critic",
    label: "Critic",
    blurb: "Reviews the rendered artifact. Must be vision-capable.",
    vision: true,
  },
  {
    id: "composer",
    label: "Video Composer",
    blurb: "Authors HyperFrames scene HTML for export_video. Only used "
         + "when generating video.",
    vision: false,
  },
  {
    id: "image",
    label: "Image Generator",
    blurb: "Backgrounds, brand assets, figure illustrations. "
         + "Image-generation model.",
    vision: false,
  },
  {
    id: "image_fallback",
    label: "Image Fallback",
    blurb: "Backup image-generation model used when the primary image endpoint is unavailable.",
    vision: false,
  },
];
