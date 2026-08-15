import { useEffect, useRef, useState } from "react";
import { useApp } from "@/lib/store";
import {
  type AgentId,
  type ApiConfig,
  type CodingHarness,
  type PipelineModelConfig,
  clearConfig,
  maskKey,
  readConfig,
  saveConfig,
} from "@/lib/api_settings";
import {
  PROVIDERS,
  type ProviderSpec,
} from "@/lib/catalog";
import { customOpenAIBaseUrlError } from "@/lib/settings_validation";
import {
  cancelHarnessLogin,
  fetchHarnessAuthStatus,
  harnessLoginEventsUrl,
  startHarnessLogin,
  testCodingAgent,
  type CodingAgentSmokeResponse,
  type HarnessAuthStatus,
  type HarnessLoginState,
} from "@/lib/api";
import { translate } from "@/lib/i18n";
import { I } from "./icons";

type Tab = "providers" | "harnesses" | "openresearch";

type AuthStatusCheck = {
  phase: "idle" | "checking" | "ready" | "error";
  result?: HarnessAuthStatus;
  error?: string;
};

const CODING_HARNESSES: Array<{
  id: Extract<CodingHarness, "codex" | "claude" | "deepseek" | "opencode" | "pi">;
  label: string;
  detail: string;
}> = [
  {
    id: "codex",
    label: "Codex",
    detail: "Uses a local Codex CLI reachable from the AutoDesign backend.",
  },
  {
    id: "claude",
    label: "Claude Code",
    detail: "Uses the claude CLI with bypass permissions.",
  },
  {
    id: "deepseek",
    label: "DeepSeek Harness",
    detail: "Uses the official DSH CLI in released headless mode.",
  },
  {
    id: "opencode",
    label: "OpenCode",
    detail: "Uses the repo OpenCode wrapper.",
  },
  {
    id: "pi",
    label: "Pi",
    detail: "Uses the Pi coding agent CLI in non-interactive mode.",
  },
];

function normalizeCodingHarness(value: string | undefined): CodingHarness {
  return CODING_HARNESSES.find((h) => h.id === value)?.id ?? "codex";
}

const CLEARED_API_MODEL_AGENTS: AgentId[] = [
  "designer",
  "enhancer",
  "claim_graph",
  "deck_outline",
  "paper_memory",
  "composer",
];

function syncConfigToCodingAgent(cfg: ApiConfig): ApiConfig {
  const current = cfg.harnesses ?? {};
  const harness = normalizeCodingHarness(current.code_editor ?? current.designer_author);
  const harnessModel = (
    current.code_editor_model
      ?? current.designer_author_model
      ?? ""
  ).trim();
  const models = { ...cfg.models };
  for (const agentId of CLEARED_API_MODEL_AGENTS) {
    delete models[agentId];
  }
  return {
    ...cfg,
    models,
    harnesses: {
      ...current,
      designer_author: harness,
      code_editor: harness,
      designer_author_model: harnessModel || undefined,
      code_editor_model: harnessModel || undefined,
    },
  };
}

/** Right-side drawer for providers, coding agent, and submission integrations. */
export function SettingsDrawer() {
  const open = useApp((s) => s.settings_open);
  const close = useApp((s) => s.closeSettings);
  const reload = useApp((s) => s.loadBackendInfo);
  const backendInfo = useApp((s) => s.backend_info);
  const language = useApp((s) => s.ui_language);
  const t = (text: string, params?: Record<string, string | number>) => translate(language, text, params);

  // Local copy of the persisted config so the user can edit fields and
  // either Save or Cancel without committing every keystroke. Initialized
  // each time the drawer opens.
  const [draft, setDraft] = useState<ApiConfig>({
    keys: {},
    bases: {},
    models: {},
    pipeline_models: {},
    harnesses: {},
    openresearch: {},
  });
  const [tab, setTab] = useState<Tab>("providers");
  const [savedTick, setSavedTick] = useState(0);

  useEffect(() => {
    if (!open) return;
    setTab("providers");
    setDraft(readConfig());
  }, [open]);

  // Esc-to-close — keeps the keyboard-only flow first-class. Must run
  // BEFORE the early return so hook count stays stable across renders.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, close]);

  if (!open) return null;

  if (backendInfo?.demo_mode) {
    const demo = backendInfo.demo;
    return (
      <>
        <div
          aria-hidden
          onClick={close}
          className="fixed inset-0 z-40 bg-ink-900/25 backdrop-blur-sm animate-fadeIn"
        />
        <aside
          role="dialog"
          aria-label={t("Settings")}
          className="fixed right-0 top-0 z-50 flex h-full w-full flex-col border-l border-ink-300/60 bg-paper shadow-raised animate-slideInRight sm:w-[480px]"
        >
          <header className="flex h-16 shrink-0 items-center justify-between border-b border-ink-300/60 px-6">
            <div className="flex items-baseline gap-3">
              <span className="eyebrow">Demo</span>
              <h2
                className="font-display text-[20px] text-ink-900"
                style={{ fontVariationSettings: '"opsz" 72, "SOFT" 30', letterSpacing: 0 }}
              >
                {t("Settings locked")}
              </h2>
            </div>
            <button
              onClick={close}
              className="inline-flex items-center gap-2 rounded-md px-2 py-1 text-ink-500 transition hover:text-ink-900"
              aria-label={t("Close (Esc)")}
              title={t("Close (Esc)")}
            >
              <span className="hidden eyebrow sm:inline">Esc</span>
              <I.Close width={14} height={14} />
            </button>
          </header>
          <div className="flex-1 overflow-y-auto px-6 py-6">
            <div className="rounded-md border border-ink-300/70 bg-surface-raised p-4">
              <div className="flex items-start gap-3">
                <div className="mt-0.5 rounded-sm border border-ink-300/70 bg-paper p-2 text-ink-700">
                  <I.Lock width={16} height={16} />
                </div>
                <div>
                  <p className="text-[13px] leading-relaxed text-ink-800">
                    {t("This public demo uses server-side credentials and fixed poster settings.")}
                  </p>
                  <dl className="mt-4 grid grid-cols-2 gap-3 text-[11.5px]">
                    <div>
                      <dt className="eyebrow text-ink-400">{t("Artifact")}</dt>
                      <dd className="mt-1 text-ink-800">{t("Poster")}</dd>
                    </div>
                    <div>
                      <dt className="eyebrow text-ink-400">{t("Template")}</dt>
                      <dd className="mt-1 text-ink-800">{demo?.template ?? "cvpr-landscape"}</dd>
                    </div>
                    <div>
                      <dt className="eyebrow text-ink-400">{t("Daily runs")}</dt>
                      <dd className="mt-1 text-ink-800">{demo?.daily_limit ?? 3}</dd>
                    </div>
                    <div>
                      <dt className="eyebrow text-ink-400">{t("Concurrency")}</dt>
                      <dd className="mt-1 text-ink-800">{demo?.concurrency ?? 1}</dd>
                    </div>
                  </dl>
                </div>
              </div>
            </div>
          </div>
          <footer className="flex shrink-0 justify-end border-t border-ink-300/60 bg-surface-raised px-6 py-3.5">
            <button
              onClick={close}
              className="inline-flex items-center gap-1.5 rounded-sm bg-ink-900 px-3.5 py-2 text-[10px] font-medium uppercase text-ink-50 transition hover:bg-ink-700"
              style={{ letterSpacing: "0.18em" }}
            >
              {t("Close")}
              <I.Check width={11} height={11} />
            </button>
          </footer>
        </aside>
      </>
    );
  }

  const onSave = () => {
    if (customOpenAIBaseUrlError(draft.bases.custom_openai, draft.keys.custom_openai)) {
      setTab("providers");
      return;
    }
    const synced = syncConfigToCodingAgent(draft);
    saveConfig(synced);
    setDraft(synced);
    setSavedTick((t) => t + 1);
    void reload();
    window.setTimeout(close, 600);
  };

  const onClear = () => {
    if (!window.confirm(t("Forget all saved providers, coding agent, and OpenResearch settings?"))) return;
    clearConfig();
    setDraft({ keys: {}, bases: {}, models: {}, pipeline_models: {}, harnesses: {}, openresearch: {} });
    setSavedTick((t) => t + 1);
  };

  return (
    <>
      <div
        aria-hidden
        onClick={close}
        className="fixed inset-0 z-40 bg-ink-900/25 backdrop-blur-sm animate-fadeIn"
      />
      <aside
        role="dialog"
        aria-label={t("Settings")}
        className="fixed right-0 top-0 z-50 flex h-full w-full flex-col border-l border-ink-300/60 bg-paper shadow-raised animate-slideInRight sm:w-[480px]"
      >
        <header className="flex h-16 shrink-0 items-center justify-between border-b border-ink-300/60 px-6">
          <div className="flex items-baseline gap-3">
            <span className="eyebrow">{t("Configuration")}</span>
            <h2
              className="font-display text-[20px] text-ink-900"
              style={{ fontVariationSettings: '"opsz" 72, "SOFT" 30', letterSpacing: 0 }}
            >
              {t("Settings")}
            </h2>
          </div>
          <button
            onClick={close}
            className="inline-flex items-center gap-2 rounded-md px-2 py-1 text-ink-500 transition hover:text-ink-900"
            aria-label={t("Close (Esc)")}
            title={t("Close (Esc)")}
          >
            <span className="hidden eyebrow sm:inline">Esc</span>
            <I.Close width={14} height={14} />
          </button>
        </header>

        <div className="relative flex shrink-0 items-center border-b border-ink-300/60 px-3">
          <TabBtn active={tab === "providers"} onClick={() => setTab("providers")}>
            <span className="tabular mr-1.5 text-[9px] text-ink-400">01</span>
            {t("Providers")}
          </TabBtn>
          <TabBtn active={tab === "harnesses"} onClick={() => setTab("harnesses")}>
            <span className="tabular mr-1.5 text-[9px] text-ink-400">02</span>
            {t("Coding Agent")}
          </TabBtn>
          <TabBtn active={tab === "openresearch"} onClick={() => setTab("openresearch")}>
            <span className="tabular mr-1.5 text-[9px] text-ink-400">03</span>
            OpenResearch
          </TabBtn>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-6">
          {tab === "providers" ? (
            <ProvidersTab draft={draft} setDraft={setDraft} />
          ) : tab === "harnesses" ? (
            <HarnessesTab draft={draft} setDraft={setDraft} />
          ) : (
            <OpenResearchTab draft={draft} setDraft={setDraft} />
          )}
        </div>

        <footer className="flex shrink-0 items-center justify-between gap-2 border-t border-ink-300/60 bg-surface-raised px-6 py-3.5">
          <button
            onClick={onClear}
            className="rounded-md px-2.5 py-1.5 text-[10px] font-medium uppercase text-ink-500 transition hover:text-ink-900"
            style={{ letterSpacing: "0.16em" }}
          >
            {t("Clear all")}
          </button>
          <div className="flex items-center gap-2">
            {savedTick > 0 && (
              <span className="eyebrow text-accent">{t("Saved")}</span>
            )}
            <button
              onClick={close}
              className="rounded-md px-3 py-1.5 text-[11px] font-medium text-ink-500 transition hover:text-ink-900"
            >
              {t("Cancel")}
            </button>
            <button
              onClick={onSave}
              className="inline-flex items-center gap-1.5 rounded-sm bg-ink-900 px-3.5 py-2 text-[10px] font-medium uppercase text-ink-50 transition hover:bg-ink-700"
              style={{ letterSpacing: "0.18em" }}
            >
              {t("Save")}
              <I.Check width={11} height={11} />
            </button>
          </div>
        </footer>
      </aside>
    </>
  );
}

function TabBtn({
  active, onClick, children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button onClick={onClick} className={`tab-btn ${active ? "tab-btn-active" : ""}`}>
      {children}
    </button>
  );
}

// ============================================================================
// Providers tab
// ============================================================================

function ProvidersTab({
  draft, setDraft,
}: {
  draft: ApiConfig;
  setDraft: (next: ApiConfig) => void;
}) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string, params?: Record<string, string | number>) => translate(language, text, params);
  return (
    <div>
      <p className="mb-5 text-[12.5px] leading-relaxed text-ink-700">
        {t("Keys live in this browser's localStorage and travel as HTTP headers on each request — the agent backend never writes them to disk.")} <span className="text-ink-500">{t("You only need ONE provider configured to start; OpenRouter is the easiest.")}</span>
      </p>
      <div className="space-y-4">
        {PROVIDERS.map((p) => (
          <ProviderCard key={p.id} spec={p} draft={draft} setDraft={setDraft} />
        ))}
        <PipelineModelsCard draft={draft} setDraft={setDraft} />
      </div>
      <p className="mt-7 border-t border-ink-300/60 pt-4 text-[10.5px] leading-relaxed text-ink-500">
        {t("Anyone with browser access can read localStorage. For local dev this matches the prior .env path. Don't ship this UI to a public host without HTTPS + per-user authentication.")}
      </p>
    </div>
  );
}

function PipelineModelsCard({
  draft, setDraft,
}: {
  draft: ApiConfig;
  setDraft: (next: ApiConfig) => void;
}) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const pm = draft.pipeline_models ?? {};
  const setPM = (patch: Partial<PipelineModelConfig>) =>
    setDraft({ ...draft, pipeline_models: { ...pm, ...patch } });
  const inputCls = "mt-2 h-9 w-full rounded-md border border-ink-300 bg-paper px-3 font-mono text-[11px] text-ink-900 outline-none transition placeholder:text-ink-400 focus:border-accent";
  const noteCls = "mt-1 text-[10.5px] leading-relaxed text-ink-500";
  return (
    <div className="rounded-md border border-ink-300/70 bg-surface-raised p-4">
      <div
        className="font-display text-[16px] text-ink-900"
        style={{ fontVariationSettings: '"opsz" 36' }}
      >
        {t("Pipeline models")}
      </div>
      <p className="mt-1 text-[11.5px] leading-relaxed text-ink-500">
        {t("Optional overrides for helper agents, never the coding agent. Leave text or vision blank to use the backend default: gpt-5.4-nano.")}
      </p>
      <div className="mt-3.5 space-y-3">
        <div>
          <label className="field-label block">{t("Text model")}</label>
          <input
            type="text"
            value={pm.text ?? ""}
            onChange={(e) => setPM({ text: e.target.value })}
            placeholder="gpt-5.4-nano"
            spellCheck={false}
            autoComplete="off"
            className={inputCls}
          />
          <p className={noteCls}>{t("enhancer · claim graph · outline · paper memory · composer")}</p>
        </div>
        <div>
          <label className="field-label block">{t("Vision model")}</label>
          <input
            type="text"
            value={pm.vision ?? ""}
            onChange={(e) => setPM({ vision: e.target.value })}
            placeholder="gpt-5.4-nano"
            spellCheck={false}
            autoComplete="off"
            className={inputCls}
          />
          <p className={noteCls}>{t("ingest · critic — must be able to read images")}</p>
        </div>
        <div>
          <label className="field-label block">{t("Image model")}</label>
          <input
            type="text"
            value={pm.image ?? ""}
            onChange={(e) => setPM({ image: e.target.value })}
            placeholder="openai/gpt-5-image-mini"
            spellCheck={false}
            autoComplete="off"
            className={inputCls}
          />
          <p className={noteCls}>{t("image generation · default openai/gpt-5-image-mini")}</p>
        </div>
      </div>
    </div>
  );
}

function ProviderCard({
  spec, draft, setDraft,
}: {
  spec: ProviderSpec;
  draft: ApiConfig;
  setDraft: (next: ApiConfig) => void;
}) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const key = draft.keys[spec.id] ?? "";
  const base = spec.id === "custom_openai" ? (draft.bases.custom_openai ?? "")
              : spec.id === "anthropic" ? (draft.bases.anthropic ?? "")
              : "";
  const setKey = (v: string) => setDraft({
    ...draft,
    keys: { ...draft.keys, [spec.id]: v },
  });
  const setBase = (v: string) => {
    if (spec.id === "custom_openai") {
      setDraft({ ...draft, bases: { ...draft.bases, custom_openai: v } });
    } else if (spec.id === "anthropic") {
      setDraft({ ...draft, bases: { ...draft.bases, anthropic: v } });
    }
  };
  const baseUrlError = spec.id === "custom_openai"
    ? customOpenAIBaseUrlError(base, key)
    : null;

  return (
    <div className="rounded-md border border-ink-300/70 bg-surface-raised p-4 transition hover:border-ink-300">
      <div className="flex items-baseline justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2.5">
            <span
              className="font-display text-[16px] text-ink-900"
              style={{ fontVariationSettings: '"opsz" 36' }}
            >
              {spec.name}
            </span>
            {key && (
              <span className="inline-flex items-center gap-1 text-accent">
                <span className="h-1 w-1 rounded-full bg-accent" />
                <span className="eyebrow text-accent-deep">{t("Connected")}</span>
              </span>
            )}
          </div>
          <p className="mt-1 text-[11.5px] leading-relaxed text-ink-500">
            {t(spec.blurb)}
          </p>
        </div>
        {spec.docs_url && (
          <a
            href={spec.docs_url}
            target="_blank"
            rel="noreferrer"
            className="group shrink-0 inline-flex items-center gap-1 text-[10px] font-medium uppercase text-accent-deep transition"
            style={{ letterSpacing: "0.16em" }}
          >
            {t("Get key")}
            <I.ArrowRight width={10} height={10} className="transition group-hover:translate-x-0.5" />
          </a>
        )}
      </div>
      <div className="mt-3.5">
        <KeyField
          label={t("API key")}
          value={key}
          onChange={setKey}
          placeholder={spec.key_placeholder}
        />
      </div>
      {spec.needs_base_url && (
        <div className="mt-3">
          <label className="field-label block">{t("Base URL")}</label>
          <input
            type="text"
            value={base}
            onChange={(e) => setBase(e.target.value)}
            placeholder={spec.base_url_placeholder}
            spellCheck={false}
            autoComplete="off"
            aria-invalid={baseUrlError ? true : undefined}
            aria-describedby={baseUrlError ? "custom-openai-base-url-error" : undefined}
            className={`mt-1.5 h-9 w-full rounded-md border bg-surface-raised px-3 font-mono text-[12px] text-ink-900 outline-none transition focus:border-accent ${
              baseUrlError ? "border-amber-700/70" : "border-ink-300"
            }`}
          />
          {baseUrlError && (
            <p id="custom-openai-base-url-error" role="alert" className="mt-1.5 text-[10.5px] leading-relaxed text-amber-900">
              {baseUrlError === "required"
                ? t("A Base URL is required when a Custom OpenAI-compatible API key is set.")
                : t("Enter a complete http:// or https:// URL with a host. Localhost and private network hosts are supported.")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function KeyField({
  label, value, onChange, placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
}) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  // Switch between masked-display and editable raw input.
  const [editing, setEditing] = useState(value === "");
  const [show, setShow] = useState(false);
  useEffect(() => {
    if (value === "") setEditing(true);
  }, [value]);
  const masked = !editing;

  return (
    <>
      <label className="field-label block">{label}</label>
      <div className="mt-1.5 flex items-center gap-1.5">
        {masked ? (
          <div className="flex h-9 flex-1 items-center rounded-md border border-ink-300 bg-vellum px-3 font-mono text-[12px] text-ink-700">
            {maskKey(value)}
          </div>
        ) : (
          <input
            type={show ? "text" : "password"}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder={placeholder}
            spellCheck={false}
            autoComplete="off"
            className="h-9 flex-1 rounded-md border border-ink-300 bg-surface-raised px-3 font-mono text-[12px] text-ink-900 outline-none transition focus:border-accent"
          />
        )}
        <button
          type="button"
          onClick={() => {
            if (masked) setEditing(true);
            else setShow(!show);
          }}
          className="rounded-md p-2 text-ink-500 transition hover:text-ink-900"
          title={masked ? t("Edit") : show ? t("Hide") : t("Reveal")}
        >
          {masked ? <I.Edit width={13} height={13} /> :
            show ? <I.EyeOff width={13} height={13} /> : <I.Eye width={13} height={13} />}
        </button>
      </div>
    </>
  );
}

// ============================================================================
// Coding Harness tab
// ============================================================================

function HarnessesTab({
  draft, setDraft,
}: {
  draft: ApiConfig;
  setDraft: (next: ApiConfig) => void;
}) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string, params?: Record<string, string | number>) => translate(language, text, params);
  const reload = useApp((s) => s.loadBackendInfo);
  const paperProfile = useApp((s) => s.backend_info?.backend_profile?.paper_poster);
  const codeEditorProfile = useApp((s) => s.backend_info?.backend_profile?.code_editor);
  const capabilities = useApp((s) => s.backend_info?.backend_profile?.harness_capabilities);
  const environment = useApp((s) => s.backend_info?.backend_profile?.environment);
  const [smoke, setSmoke] = useState<{
    status: "idle" | "running" | "done";
    result?: CodingAgentSmokeResponse;
    error?: string;
  }>({ status: "idle" });
  const [authStatus, setAuthStatus] = useState<AuthStatusCheck>({ phase: "idle" });
  const [authCheckTick, setAuthCheckTick] = useState(0);
  const [login, setLogin] = useState<{
    status: "idle" | "running";
    state?: HarnessLoginState;
    error?: string;
  }>({ status: "idle" });
  const [keyVisible, setKeyVisible] = useState(false);
  const loginEsRef = useRef<EventSource | null>(null);
  const cfg = draft.harnesses ?? {};
  const selectedHarness = normalizeCodingHarness(
    cfg.code_editor
      ?? cfg.designer_author
      ?? codeEditorProfile?.harness
      ?? paperProfile?.designer_author_harness,
  );
  const selectedCapability = capabilities?.[selectedHarness];
  const selectedSurface = selectedCapability?.surfaces?.code_editor
    ?? selectedCapability?.surfaces?.designer_author;
  const selectedModel = cfg.code_editor_model
    ?? cfg.designer_author_model
    ?? "";
  const derivedModel = selectedModel.trim() || t(defaultHarnessModelLabel(selectedHarness));
  const profileAvailability = codeEditorProfile?.harness === selectedHarness
    ? codeEditorProfile.available
    : undefined;
  const commandState = authStatus.phase === "ready" && authStatus.result?.available === false
    ? "missing"
    : typeof profileAvailability === "boolean"
        ? (profileAvailability ? "detected" : "missing")
        : typeof selectedCapability?.available === "boolean"
          ? (selectedCapability.available ? "detected" : "missing")
          : "checking";
  const commandAvailable = commandState === "detected";
  const commandLabel = commandState === "detected"
    ? t("Command detected")
    : commandState === "missing"
      ? t("Command missing")
      : t("Checking command");
  const commandNextAction = commandState === "missing"
    ? missingCommandNextAction(selectedHarness, t)
    : "";
  const smokeLabel = smoke.status === "running"
    ? t("Checking")
    : smoke.error
      ? t("Status error")
      : smoke.result?.ok
        ? t("Passed")
        : smoke.result?.status === "missing_command"
          ? t("Command missing")
          : smoke.result
            ? t("Failed")
            : t("Not run");
  const smokeHarness = normalizeCodingHarness(smoke.result?.harness ?? selectedHarness);
  const smokeModel = displayHarnessModel(smokeHarness, smoke.result?.model, t);
  const rawSmokeLogExcerpt = smoke.result && !smoke.result.ok
    ? cleanSmokeLogExcerpt([smoke.result.stdout_excerpt, smoke.result.stderr_excerpt].filter(Boolean).join("\n"))
    : "";
  const smokeLogExcerpt = smoke.result && !smoke.result.ok && smoke.result.status !== "timeout" && !smoke.result.timed_out
    ? rawSmokeLogExcerpt
    : "";
  const smokeDetail = smoke.result
    ? formatSmokeDetail(smoke.result, smokeModel, rawSmokeLogExcerpt, t)
    : smoke.status === "running"
      ? t("Testing coding agent with model: {model}", { model: derivedModel })
      : smoke.error
        ? t("Smoke status error: {error}", { error: smoke.error })
        : commandState === "missing"
          ? commandNextAction
          : commandState === "checking"
            ? t("Checking command availability before a smoke test can run.")
            : codeEditorProfile?.auth_message
              ?? t("Next: run a smoke test to verify the CLI can execute non-interactively.");
  const authLabel = commandState === "missing"
    ? t("Command missing")
    : authStatus.phase === "checking" || authStatus.phase === "idle"
      ? t("Checking account")
      : authStatus.phase === "error"
        ? t("Account status error")
        : authStatus.result?.logged_in
          ? (authStatus.result.account
              ? t("Connected as {account}", { account: authStatus.result.account })
              : t("Connected"))
          : t("Signed out");
  const authNextAction = commandState === "missing"
    ? missingCommandNextAction(selectedHarness, t)
    : authStatus.phase === "error"
      ? t("Next: retry the status check. If it keeps failing, run this CLI once in Terminal and retry.")
      : authStatus.phase === "ready" && !authStatus.result?.logged_in
        ? t("Next: connect an account or use a Harness API key, then run the smoke test.")
        : "";
  const canTest = commandAvailable && smoke.status !== "running";
  const canConnect = commandAvailable
    && authStatus.phase === "ready"
    && authStatus.result?.available !== false;

  const setHarness = (next: CodingHarness) => {
    const isCurrentHarness = next === selectedHarness;
    const nextModel = isCurrentHarness ? selectedModel : "";
    if (!isCurrentHarness) {
      setSmoke({ status: "idle" });
      setAuthStatus({ phase: next === "claude" || next === "codex" ? "checking" : "idle" });
    }
    setDraft({
      ...draft,
      harnesses: {
        ...cfg,
        designer_author: next,
        code_editor: next,
        designer_author_model: nextModel,
        code_editor_model: nextModel,
      },
    });
  };

  const setHarnessModel = (model: string) => {
    setSmoke({ status: "idle" });
    setDraft({
      ...draft,
      harnesses: {
        ...cfg,
        designer_author: selectedHarness,
        code_editor: selectedHarness,
        designer_author_model: model,
        code_editor_model: model,
      },
    });
  };

  const runSmoke = async () => {
    if (!commandAvailable || smoke.status === "running") return;
    const synced = syncConfigToCodingAgent(draft);
    setDraft(synced);
    setSmoke({ status: "running" });
    try {
      const result = await testCodingAgent(synced, { timeout_s: 60 });
      setSmoke({ status: "done", result });
      void reload();
    } catch (err) {
      setSmoke({
        status: "done",
        error: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const supportsAccount = selectedHarness === "claude" || selectedHarness === "codex";
  const supportsHarnessKey = supportsAccount || selectedHarness === "deepseek";

  useEffect(() => {
    if (!supportsAccount) {
      setAuthStatus({ phase: "idle" });
      return;
    }
    let alive = true;
    setAuthStatus({ phase: "checking" });
    void fetchHarnessAuthStatus(selectedHarness)
      .then((result) => { if (alive) setAuthStatus({ phase: "ready", result }); })
      .catch((err) => {
        if (alive) {
          setAuthStatus({
            phase: "error",
            error: err instanceof Error ? err.message : String(err),
          });
        }
      });
    return () => { alive = false; };
  }, [selectedHarness, supportsAccount, authCheckTick]);

  useEffect(() => () => { loginEsRef.current?.close(); }, []);

  const setHarnessKey = (val: string) => {
    setDraft({ ...draft, harnesses: { ...cfg, harness_api_key: val.trim() || undefined } });
  };

  const startLogin = async () => {
    if (!canConnect) return;
    setLogin({ status: "running" });
    try {
      const started = await startHarnessLogin(selectedHarness);
      setLogin({ status: "running", state: started });
      loginEsRef.current?.close();
      const es = new EventSource(harnessLoginEventsUrl(started.login_id));
      loginEsRef.current = es;
      es.onmessage = (ev) => {
        let st: HarnessLoginState;
        try {
          st = JSON.parse(ev.data) as HarnessLoginState;
        } catch {
          return;
        }
        const terminal = st.status === "success" || st.status === "failed" || st.status === "cancelled";
        setLogin({ status: terminal ? "idle" : "running", state: st });
        if (terminal) {
          es.close();
          if (loginEsRef.current === es) loginEsRef.current = null;
          setAuthCheckTick((tick) => tick + 1);
          void reload();
        }
      };
      es.onerror = () => {
        es.close();
        if (loginEsRef.current === es) loginEsRef.current = null;
        setLogin((cur) => (cur.status === "running"
          ? { status: "idle", state: cur.state, error: t("Login stream disconnected.") }
          : cur));
      };
    } catch (err) {
      setLogin({ status: "idle", error: err instanceof Error ? err.message : String(err) });
    }
  };

  const cancelLogin = async () => {
    const id = login.state?.login_id;
    loginEsRef.current?.close();
    loginEsRef.current = null;
    if (id) {
      try {
        await cancelHarnessLogin(id);
      } catch {
        /* ignore — best effort */
      }
    }
    setLogin({ status: "idle" });
  };

  return (
    <div>
      <p className="mb-5 text-[12.5px] leading-relaxed text-ink-700">
        {t("Pick one local coding agent. AutoDesign uses it for the first poster draft, later revisions, Edit Area changes, and agent-converted exports.")} <span className="text-ink-500">{t("The model version is passed to that coding agent command line, while API prep agents keep backend defaults.")}</span>
      </p>

      <div className="grid grid-cols-2 gap-3">
        {CODING_HARNESSES.map((h) => {
          const active = h.id === selectedHarness;
          const cap = capabilities?.[h.id];
          const cardStatus = cap?.available === false
            ? t("Needs setup")
            : cap?.available === true
              ? t("Detected")
              : t("Checking");
          return (
            <button
              key={h.id}
              type="button"
              onClick={() => setHarness(h.id)}
              className={`rounded-md border p-3 text-left transition ${
                active
                  ? "border-accent bg-accent/8 shadow-soft"
                  : "border-ink-300/70 bg-surface-raised hover:border-ink-400"
              }`}
            >
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div
                    className="font-display text-[15px] text-ink-900"
                    style={{ fontVariationSettings: '"opsz" 36' }}
                  >
                    {h.label}
                  </div>
                  <p className="mt-1 text-[10.5px] leading-relaxed text-ink-500">
                    {t(harnessCardDescription(h.id))}
                  </p>
                </div>
                <span
                  className={`rounded-sm border px-1.5 py-px text-[9px] font-medium uppercase ${
                    cap?.available === false ? "border-amber-700/35 text-amber-900" : "border-ink-300/70 text-ink-500"
                  }`}
                  style={{ letterSpacing: "0.14em" }}
                >
                  {cardStatus}
                </span>
              </div>
            </button>
          );
        })}
      </div>

      {selectedHarness === "opencode" || selectedHarness === "pi" ? (
        <label className="mt-5 block rounded-md border border-ink-300/70 bg-surface-raised p-4">
          <span className="eyebrow text-ink-400">
            {t(selectedHarness === "pi" ? "Pi model" : "OpenCode model")}
          </span>
          <input
            value={selectedModel}
            onChange={(e) => setHarnessModel(e.target.value)}
            placeholder={selectedHarness === "pi"
              ? "openai/gpt-5.5"
              : "qwen/qwen3-coder"}
            spellCheck={false}
            autoComplete="off"
            className="mt-2 h-9 w-full rounded-md border border-ink-300 bg-paper px-3 font-mono text-[11px] text-ink-900 outline-none transition placeholder:text-ink-400 focus:border-accent"
          />
          <p className="mt-2 text-[11px] leading-relaxed text-ink-500">
            {t(selectedHarness === "pi"
              ? "Leave it blank to use Pi's configured default model, or enter a provider/model id."
              : "Leave it blank to use OpenCode's CLI default, or enter a provider/model id.")}
          </p>
        </label>
      ) : (
        <label className="mt-5 block rounded-md border border-ink-300/70 bg-surface-raised p-4">
          <span className="eyebrow text-ink-400">{t("Model version")}</span>
          <select
            value={selectedModel}
            onChange={(e) => setHarnessModel(e.target.value)}
            className="mt-2 h-9 w-full rounded-md border border-ink-300 bg-paper px-3 font-mono text-[11px] text-ink-900 outline-none transition focus:border-accent"
          >
            <option value="">{t(defaultHarnessModelLabel(selectedHarness))}</option>
            {modelOptionsForHarness(selectedHarness).map((model) => (
              <option key={model} value={model}>{model}</option>
            ))}
          </select>
          <p className="mt-2 text-[11px] leading-relaxed text-ink-500">
            {t("Leave blank to use the coding agent's own default model.")}
          </p>
        </label>
      )}

      {supportsHarnessKey && (
        <label className="mt-3 block rounded-md border border-ink-300/70 bg-surface-raised p-4">
          <span className="eyebrow text-ink-400">{t("Harness API key")}</span>
          <div className="mt-2 flex items-center gap-2">
            <input
              type={keyVisible ? "text" : "password"}
              value={cfg.harness_api_key ?? ""}
              onChange={(e) => setHarnessKey(e.target.value)}
              placeholder={selectedHarness === "claude" ? "sk-ant-…" : "sk-…"}
              spellCheck={false}
              autoComplete="off"
              className="h-9 w-full rounded-md border border-ink-300 bg-paper px-3 font-mono text-[11px] text-ink-900 outline-none transition placeholder:text-ink-400 focus:border-accent"
            />
            <button
              type="button"
              onClick={() => setKeyVisible((v) => !v)}
              className="shrink-0 rounded-sm border border-ink-300 px-2 py-1.5 text-[9.5px] font-medium uppercase text-ink-600 transition hover:border-accent hover:text-accent-deep"
              style={{ letterSpacing: "0.12em" }}
            >
              {keyVisible ? t("Hide") : t("Show")}
            </button>
          </div>
          <p className="mt-2 text-[11px] leading-relaxed text-ink-500">
            {selectedHarness === "deepseek"
              ? t("Optional. Passed to DeepSeek Harness as DEEPSEEK_API_KEY. Leave blank to use the backend environment.")
              : t("Optional. Pay-per-token API key for the coding agent CLI (claude → ANTHROPIC_API_KEY, codex → OPENAI_API_KEY). Leave blank to use a connected account below.")}
          </p>
        </label>
      )}

      <div className="mt-5 rounded-md border border-ink-300/70 bg-vellum px-3.5 py-3 text-[11.5px] leading-relaxed text-ink-600">
        <div className="grid grid-cols-2 gap-3">
          <div>
            <div className="eyebrow text-ink-400">{t("Synced surfaces")}</div>
            <p className="mt-1 text-ink-800">{t("First draft, revision, export")}</p>
          </div>
          <div>
            <div className="eyebrow text-ink-400">{t("Derived model")}</div>
            <p className="mt-1 truncate font-mono text-[11px] text-ink-800" title={derivedModel}>{derivedModel}</p>
          </div>
          <div>
            <div className="eyebrow text-ink-400">{t("Command")}</div>
            <p className="mt-1 truncate font-mono text-[11px] text-ink-800" title={selectedSurface?.cmd || codeEditorProfile?.cmd || paperProfile?.designer_author_cmd || ""}>
              {selectedSurface?.cmd || codeEditorProfile?.cmd || paperProfile?.designer_author_cmd || t("Resolved after saving.")}
            </p>
          </div>
          <div>
            <div className="eyebrow text-ink-400">{t("Availability")}</div>
            <p className={`mt-1 text-ink-800 ${commandAvailable ? "" : "text-amber-900"}`}>
              {commandLabel}
            </p>
          </div>
        </div>
        <div className="mt-3 border-t border-ink-300/60 pt-3">
          <div className="eyebrow text-ink-400">{t("Local runtime")}</div>
          <div className="mt-2 grid grid-cols-2 gap-3">
            <div>
              <p className="text-[10px] uppercase tracking-[0.12em] text-ink-400">
                {t("Coding agent CLI")}
              </p>
              <p className={`mt-1 text-[11px] ${environment?.coding_agent.ready === false ? "text-amber-900" : "text-ink-800"}`}>
                {environment
                  ? (environment.coding_agent.ready ? t("Ready") : t("Needs setup"))
                  : t("Checking")}
                {environment?.coding_agent.version ? ` · ${environment.coding_agent.version}` : ""}
              </p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-[0.12em] text-ink-400">
                {t("Video runtime")}
              </p>
              <p className={`mt-1 text-[11px] ${environment?.video.ready === false ? "text-amber-900" : "text-ink-800"}`}>
                {environment
                  ? (environment.video.ready ? t("Ready") : t("Needs setup"))
                  : t("Checking")}
                {environment?.video.hyperframes.version
                  ? ` · HyperFrames ${environment.video.hyperframes.version}`
                  : ""}
              </p>
            </div>
          </div>
          {environment?.video.ready === false && (
            <p className="mt-2 text-[10.5px] leading-relaxed text-amber-900">
              {t("Missing: {items}", {
                items: environment.video.missing.join(", "),
              })} {environment.video.repair}
            </p>
          )}
        </div>
        <div className="mt-3 border-t border-ink-300/60 pt-3">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0 flex-1">
              <div className="eyebrow text-ink-400">{t("Smoke test")}</div>
              <p className="mt-1 text-[11px] leading-relaxed text-ink-500">
                {smokeLabel} · {smokeDetail}
              </p>
            </div>
            <button
              type="button"
              onClick={runSmoke}
              disabled={!canTest}
              title={commandState === "missing" ? commandNextAction : undefined}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-sm border border-ink-300 bg-surface-raised px-2.5 py-1.5 text-[9.5px] font-medium uppercase text-ink-700 transition hover:border-accent hover:text-accent-deep disabled:cursor-not-allowed disabled:opacity-50"
              style={{ letterSpacing: "0.14em" }}
            >
              {smoke.status === "running" ? (
                <span className="h-2 w-2 animate-spin rounded-full border border-ink-300 border-t-accent" />
              ) : (
                <I.Check width={10} height={10} />
              )}
              {smoke.status === "running"
                ? t("Testing")
                : commandState === "missing"
                  ? t("Install CLI first")
                  : commandState === "checking"
                    ? t("Checking command")
                    : t("Test coding agent")}
            </button>
          </div>
          {smokeLogExcerpt && (
            <pre className="mt-2 max-h-24 overflow-auto rounded-sm border border-ink-300/60 bg-paper p-2 font-mono text-[10px] leading-relaxed text-ink-600">
              {smokeLogExcerpt}
            </pre>
          )}
        </div>
        {supportsAccount && (
          <div className="mt-3 border-t border-ink-300/60 pt-3">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="eyebrow text-ink-400">{t("Account")}</div>
                <p className={`mt-1 truncate text-[11px] ${authStatus.phase === "error" || commandState === "missing" ? "text-amber-900" : "text-ink-800"}`} title={authStatus.phase === "ready" ? authStatus.result?.account ?? "" : ""}>
                  {authLabel}
                </p>
              </div>
              {login.status === "running" ? (
                <button
                  type="button"
                  onClick={cancelLogin}
                  className="inline-flex shrink-0 items-center gap-1.5 rounded-sm border border-ink-300 bg-surface-raised px-2.5 py-1.5 text-[9.5px] font-medium uppercase text-ink-700 transition hover:border-accent hover:text-accent-deep"
                  style={{ letterSpacing: "0.14em" }}
                >
                  <span className="h-2 w-2 animate-spin rounded-full border border-ink-300 border-t-accent" />
                  {t("Cancel")}
                </button>
              ) : authStatus.phase === "error" ? (
                <button
                  type="button"
                  onClick={() => setAuthCheckTick((tick) => tick + 1)}
                  className="inline-flex shrink-0 items-center gap-1.5 rounded-sm border border-ink-300 bg-surface-raised px-2.5 py-1.5 text-[9.5px] font-medium uppercase text-ink-700 transition hover:border-accent hover:text-accent-deep"
                  style={{ letterSpacing: "0.14em" }}
                >
                  {t("Retry status")}
                </button>
              ) : (
                <button
                  type="button"
                  onClick={startLogin}
                  disabled={!canConnect}
                  title={commandState === "missing" ? commandNextAction : undefined}
                  className="inline-flex shrink-0 items-center gap-1.5 rounded-sm border border-ink-300 bg-surface-raised px-2.5 py-1.5 text-[9.5px] font-medium uppercase text-ink-700 transition hover:border-accent hover:text-accent-deep disabled:cursor-not-allowed disabled:opacity-50"
                  style={{ letterSpacing: "0.14em" }}
                >
                  {commandState === "missing"
                    ? t("Install CLI first")
                    : authStatus.phase === "ready" && authStatus.result?.logged_in
                      ? t("Reconnect")
                      : t("Connect account")}
                </button>
              )}
            </div>
            {login.status === "running" && login.state?.url && (
              <p className="mt-2 text-[11px] leading-relaxed text-ink-600">
                {t("A browser window should open. If not, open this URL to finish signing in:")}{" "}
                <a href={login.state.url} target="_blank" rel="noreferrer" className="break-all text-accent-deep underline">
                  {login.state.url}
                </a>
              </p>
            )}
            {login.status === "running" && !login.state?.url && (
              <p className="mt-2 text-[11px] leading-relaxed text-ink-500">
                {login.state?.message || t("Starting login…")}
              </p>
            )}
            {login.error && (
              <p className="mt-2 text-[11px] leading-relaxed text-amber-900">{login.error}</p>
            )}
            {authNextAction && (
              <p className="mt-2 text-[11px] leading-relaxed text-ink-500">{authNextAction}</p>
            )}
          </div>
        )}
        {selectedCapability?.notes && (
          <p className="mt-3 border-t border-ink-300/60 pt-3 text-[11px] text-ink-500">
            {t(selectedCapability.notes)}
          </p>
        )}
      </div>
    </div>
  );
}

function defaultHarnessModelLabel(harness: CodingHarness): string {
  switch (harness) {
    case "codex":
      return "Codex default GPT";
    case "claude":
      return "Claude Code default";
    case "deepseek":
      return "DeepSeek Harness default";
    case "opencode":
      return "OpenCode CLI default";
    case "kimi":
      return "Default model: Kimi";
    case "mimo":
      return "Default model: Mimo";
    case "pi":
      return "Pi CLI default";
    case "zcode":
      return "Default model: GLM";
  }
}

function harnessCardDescription(harness: CodingHarness): string {
  switch (harness) {
    case "codex":
      return "Codex with selectable GPT model";
    case "claude":
      return "Claude Code with selectable Claude model";
    case "deepseek":
      return "Official DeepSeek Harness with selectable DeepSeek model";
    case "opencode":
      return "OpenCode with custom model id";
    case "kimi":
      return "Default model: Kimi";
    case "mimo":
      return "Default model: Mimo";
    case "pi":
      return "Pi coding agent with optional provider/model selection";
    case "zcode":
      return "Default model: GLM";
  }
}

function modelOptionsForHarness(harness: CodingHarness): string[] {
  switch (harness) {
    case "codex":
      return ["gpt-5.4", "gpt-5.4-nano", "gpt-5.5", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"];
    case "claude":
      return ["claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"];
    case "deepseek":
      return ["deepseek-v4-flash", "deepseek-v4-pro"];
    default:
      return [];
  }
}

function displayHarnessModel(
  harness: CodingHarness,
  model: string | null | undefined,
  t: (text: string, params?: Record<string, string | number>) => string,
): string {
  return model?.trim() || t(defaultHarnessModelLabel(harness));
}

function formatSmokeDetail(
  result: CodingAgentSmokeResponse,
  model: string,
  logExcerpt: string,
  t: (text: string, params?: Record<string, string | number>) => string,
): string {
  if (result.ok) {
    return t("Test passed. Model: {model}", { model });
  }
  const reason = smokeFailureReason(result, logExcerpt, t);
  const solution = smokeFailureSolution(result, logExcerpt, t);
  return solution
    ? t("Test failed. Model: {model}. {reason} {solution}", { model, reason, solution })
    : t("Test failed. Model: {model}. {reason}", { model, reason });
}

function smokeFailureReason(
  result: CodingAgentSmokeResponse,
  logExcerpt: string,
  t: (text: string, params?: Record<string, string | number>) => string,
): string {
  if (result.status === "missing_command") return t("Command was not found.");
  const networkReason = smokeNetworkReason(logExcerpt, t);
  if (networkReason) return networkReason;
  if (result.status === "timeout" || result.timed_out || result.reason === "timeout") {
    return t("Timed out after {seconds}s before the CLI wrote the smoke-test file.", {
      seconds: result.timeout_s,
    });
  }
  const conciseLog = conciseSmokeLog(logExcerpt);
  if (conciseLog) {
    return t("CLI returned: {message}", { message: conciseLog });
  }
  if (result.reason === "missing_smoke_output") {
    return t("The CLI exited without writing the smoke-test file.");
  }
  if (result.reason === "marker_json_not_ok" || result.reason.startsWith("invalid_marker_json")) {
    return t("The CLI wrote an invalid smoke-test result.");
  }
  if (result.reason.startsWith("command_start_error")) {
    return t("The command could not be started.");
  }
  if (result.reason.startsWith("command_parse_error")) {
    return t("The configured command could not be parsed.");
  }
  return result.reason || t("Unknown failure.");
}

function smokeFailureSolution(
  result: CodingAgentSmokeResponse,
  logExcerpt: string,
  t: (text: string, params?: Record<string, string | number>) => string,
): string {
  const harness = normalizeCodingHarness(result.harness);
  const lowerLog = logExcerpt.toLowerCase();
  if (isSmokeNetworkFailure(lowerLog)) {
    return t("Next: check network/VPN/proxy access for this CLI, open it once in Terminal, then retry.");
  }
  if (result.status === "missing_command") {
    return missingCommandNextAction(harness, t);
  }
  if (lowerLog.includes("model not found") || lowerLog.includes("providermodelnotfounderror")) {
    if (harness === "opencode") {
      return t("Next: use an OpenCode model id in provider/model format, or leave the model blank to use the OpenCode default.");
    }
    return t("Next: leave the model blank or choose a model supported by this CLI account.");
  }
  if (lowerLog.includes("api error: 400") || lowerLog.includes("请求格式有误")) {
    if (harness === "claude") {
      return t("Next: leave the Claude model blank or pick a model supported by your Claude Code CLI, then retry.");
    }
    return t("Next: check the selected model and CLI login, then retry.");
  }
  if (lowerLog.includes("login") || lowerLog.includes("auth") || lowerLog.includes("unauthorized")) {
    return t("Next: open the CLI once in Terminal, finish login, then retry this test.");
  }
  if (result.status === "timeout" || result.timed_out || result.reason === "timeout") {
    return t("Next: open the CLI once in Terminal to clear login or permission prompts, then retry.");
  }
  if (result.reason === "missing_smoke_output") {
    return t("Next: check the CLI output below, fix login/model issues, then retry.");
  }
  return "";
}

function missingCommandNextAction(
  harness: CodingHarness,
  t: (text: string, params?: Record<string, string | number>) => string,
): string {
  switch (harness) {
    case "codex":
      return t("Next: run codex --version in the same shell/runtime that starts AutoDesign. If it only works elsewhere, add it to the backend PATH or set AUTODESIGN_CODEX_BIN and restart.");
    case "claude":
      return t("Next: install Claude Code and put claude on PATH.");
    case "opencode":
      return t("Next: install OpenCode and put opencode on PATH.");
    case "deepseek":
      return t("Next: install or upgrade DeepSeek Harness with npm install -g @deepseek-ai/dsh@latest, then restart AutoDesign.");
    case "pi":
      return t("Next: install Pi and put pi on PATH.");
    default:
      return t("Next: install this CLI and put it on PATH.");
  }
}

function smokeNetworkReason(
  logExcerpt: string,
  t: (text: string, params?: Record<string, string | number>) => string,
): string {
  const lowerLog = logExcerpt.toLowerCase();
  if (!isSmokeNetworkFailure(lowerLog)) return "";
  return t("The CLI could not reach its model service: {message}", {
    message: conciseSmokeLog(logExcerpt),
  });
}

function isSmokeNetworkFailure(lowerLog: string): boolean {
  return lowerLog.includes("tls handshake")
    || lowerLog.includes("failed to connect to websocket")
    || lowerLog.includes("stream disconnected")
    || lowerLog.includes("connection error")
    || lowerLog.includes("connection refused")
    || lowerLog.includes("network error");
}

function conciseSmokeLog(text: string): string {
  const singleLine = text.replace(/\s+/g, " ").trim();
  if (!singleLine) return "";
  return singleLine.length <= 180 ? singleLine : `${singleLine.slice(0, 177)}...`;
}

function cleanSmokeLogExcerpt(text: string): string {
  return text
    .replace(/\x1b\[[0-9;]*m/g, "")
    .split("\n")
    .filter((line) => {
      if (line.includes("codex_core_skills::loader: ignoring interface.icon_")) return false;
      if (line.includes("codex_core_plugins::manifest: ignoring interface.defaultPrompt")) return false;
      return true;
    })
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

// ============================================================================
// OpenResearch tab
// ============================================================================

function OpenResearchTab({
  draft, setDraft,
}: {
  draft: ApiConfig;
  setDraft: (next: ApiConfig) => void;
}) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const profile = useApp((s) => s.backend_info?.backend_profile?.openresearch);
  const cfg = draft.openresearch ?? {};
  const enabled = cfg.enabled ?? profile?.submitter === "custom";
  const commandAvailable = profile?.submitter_cmd_available ?? true;
  const hasOrg = !!cfg.org_id?.trim() || !!profile?.org_id_configured;
  const sourceLabel = openResearchSourceLabel(profile?.submitter_cmd_source);
  const status = !enabled
    ? "Off"
    : !commandAvailable
      ? "Needs command"
      : !hasOrg
        ? "Needs org"
        : "Ready";

  const patch = (next: Partial<ApiConfig["openresearch"]>) => {
    setDraft({
      ...draft,
      openresearch: { ...cfg, ...next },
    });
  };

  const setEnabled = (next: boolean) => {
    patch({
      enabled: next,
      submitter_timeout_s: next
        ? cfg.submitter_timeout_s || String(profile?.submitter_timeout_s || 900)
        : cfg.submitter_timeout_s,
    });
  };

  return (
    <div>
      <p className="mb-5 text-[12.5px] leading-relaxed text-ink-700">
        {t("Submit generated paper posters to OpenResearch from the artifact toolbar. The default submitter uses the ChatGPT/Codex app bundle or codex on PATH.")}
      </p>

      <div className="mb-4 rounded-md border border-ink-300/70 bg-surface-raised p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <span
                className="font-display text-[16px] text-ink-900"
                style={{ fontVariationSettings: '"opsz" 36' }}
              >
                {t("OpenResearch submitter")}
              </span>
              <span
                className={`rounded-sm border px-1.5 py-px text-[9px] font-medium uppercase ${
                  status === "Ready"
                    ? "border-accent/40 text-accent-deep"
                    : "border-ink-300/70 text-ink-500"
                }`}
                style={{ letterSpacing: "0.18em" }}
              >
                {t(status)}
              </span>
            </div>
            <p className="mt-1 text-[11.5px] leading-relaxed text-ink-500">
              {enabled
                ? commandAvailable
                  ? `${t("Command source")}: ${sourceLabel}.`
                  : profile?.submitter_cmd_message || t("Configure an external submitter command.")
                : t("Disabled for this browser.")}
            </p>
          </div>
          <label className="flex shrink-0 cursor-pointer items-center gap-2 text-[11px] font-medium uppercase text-ink-700" style={{ letterSpacing: "0.14em" }}>
            <input
              type="checkbox"
              checked={!!enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              className="h-4 w-4 accent-ink-900"
            />
            {t("Enable")}
          </label>
        </div>
      </div>

      <div className="space-y-4">
        <TextField
          label={t("Org ID")}
          value={cfg.org_id ?? ""}
          onChange={(v) => patch({ org_id: v })}
          placeholder="019eeea4-824f-7d72-acfe-37c58958ae9b"
        />
        <TextField
          label={t("Default repo")}
          value={cfg.repo_full_name ?? ""}
          onChange={(v) => patch({ repo_full_name: v })}
          placeholder="owner/repo (optional)"
        />
        <KeyField
          label={t("API token")}
          value={cfg.token ?? ""}
          onChange={(v) => patch({ token: v })}
          placeholder={t("optional; used to refresh reports")}
        />
      </div>

      <details className="mt-5 rounded-md border border-ink-300/70 bg-vellum px-3.5 py-3">
        <summary className="cursor-pointer text-[10px] font-medium uppercase text-ink-600" style={{ letterSpacing: "0.16em" }}>
          {t("Advanced")}
        </summary>
        <div className="mt-4 space-y-4">
          <TextField
            label={t("Submitter command")}
            value={cfg.submitter_cmd ?? ""}
            onChange={(v) => patch({ submitter_cmd: v })}
            placeholder={t("leave blank for auto-detect")}
            mono
          />
          <TextField
            label={t("Submitter timeout seconds")}
            value={cfg.submitter_timeout_s ?? ""}
            onChange={(v) => patch({ submitter_timeout_s: v.replace(/[^\d]/g, "") })}
            placeholder="900"
            mono
          />
          <TextField
            label={t("API URL")}
            value={cfg.api_url ?? ""}
            onChange={(v) => patch({ api_url: v })}
            placeholder={profile?.api_url || "https://api.openresearch.sh"}
            mono
          />
        </div>
      </details>
    </div>
  );
}

function TextField({
  label, value, onChange, placeholder, mono = false,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  mono?: boolean;
}) {
  return (
    <div>
      <label className="field-label block">{label}</label>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        spellCheck={false}
        autoComplete="off"
        className={`mt-1.5 h-9 w-full rounded-md border border-ink-300 bg-surface-raised px-3 text-[12px] text-ink-900 outline-none transition focus:border-accent ${
          mono ? "font-mono" : ""
        }`}
      />
    </div>
  );
}

function openResearchSourceLabel(source: string | undefined): string {
  switch (source) {
    case "configured":
      return "configured command";
    case "codex_app":
    case "app_bundle":
      return "ChatGPT/Codex app";
    case "path":
      return "codex on PATH";
    case "missing":
      return "missing";
    default:
      return "auto";
  }
}
