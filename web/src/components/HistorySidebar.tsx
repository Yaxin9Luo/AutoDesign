import { useState } from "react";
import { useApp, useConversationList } from "@/lib/store";
import type { Conversation } from "@/lib/types";
import { translate, type UiLanguage } from "@/lib/i18n";
import { I } from "./icons";
import { ResizeHandle } from "./ResizeHandle";
import { LanguageMenu } from "./LanguageMenu";

export function HistorySidebar() {
  const conversations = useConversationList();
  const current_id = useApp((s) => s.current_conversation_id);
  const newConversation = useApp((s) => s.newConversation);
  const switchConversation = useApp((s) => s.switchConversation);
  const deleteConversation = useApp((s) => s.deleteConversation);
  const loadDemoDeck = useApp((s) => s.loadDemoDeck);
  const loadDemoLanding = useApp((s) => s.loadDemoLanding);
  const loadDemoPoster = useApp((s) => s.loadDemoPoster);
  const loadDemoSlides = useApp((s) => s.loadDemoSlides);
  const loadDemoVideo = useApp((s) => s.loadDemoVideo);
  const loadServerHistory = useApp((s) => s.loadServerHistory);
  const setSidebarWidth = useApp((s) => s.setSidebarWidth);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const [samplesOpen, setSamplesOpen] = useState(false);

  return (
    <aside className="app-panel relative flex h-full min-h-0 w-full shrink-0 flex-col border-r">
      <div className="flex h-12 shrink-0 items-center px-2.5">
        <button
          onClick={newConversation}
          className="group flex w-full items-center justify-between rounded-md border border-ink-300/70 bg-surface-raised/80 px-2.5 py-1.5 text-[12px] font-medium text-ink-900 shadow-soft transition hover:border-ink-400 hover:bg-white"
        >
          <span className="inline-flex items-center gap-2">
            <I.SparkleQuiet width={12} height={12} className="text-accent" />
            <span className="font-display" style={{ fontVariationSettings: '"opsz" 36' }}>
              {t("New chat")}
            </span>
          </span>
          <I.Plus width={12} height={12} className="text-ink-500 transition group-hover:text-accent-deep" />
        </button>
      </div>

      <div className="space-y-1.5 px-2.5 pb-2">
        <button
          onClick={() => void loadServerHistory()}
          className="flex w-full items-center justify-between rounded-md border border-ink-300/60 bg-vellum/70 px-2.5 py-1.5 text-[11px] font-medium text-ink-600 transition hover:border-accent/50 hover:bg-white hover:text-accent-deep"
          title={t("Import real generated runs from the local backend history")}
        >
          <span>{t("Import recent runs")}</span>
          <I.Refresh width={11} height={11} />
        </button>
        <div className="rounded-md border border-ink-300/55 bg-vellum/50">
          <button
            type="button"
            onClick={() => setSamplesOpen((v) => !v)}
            className="flex w-full items-center justify-between px-2.5 py-1.5 text-[10.5px] font-medium uppercase text-ink-500 transition hover:text-ink-900"
            aria-expanded={samplesOpen}
          >
            <span style={{ letterSpacing: "0.14em" }}>{t("Samples")}</span>
            <I.ChevronDown
              width={11}
              height={11}
              className="transition"
              style={{ transform: samplesOpen ? "rotate(180deg)" : undefined }}
            />
          </button>
          {samplesOpen && (
            <div className="space-y-1 border-t border-ink-300/45 px-1.5 py-1.5">
              <button
                onClick={loadDemoPoster}
                className="flex w-full items-center justify-between rounded-md border border-dashed border-ink-300/70 bg-paper/70 px-2.5 py-1.5 text-[11px] text-ink-500 transition hover:border-accent/60 hover:bg-white hover:text-accent-deep"
                title={t("Drop an editable poster into canvas — no agent call, no money")}
              >
                <span>{t("Try editable poster (no agent)")}</span>
                <I.Edit width={11} height={11} />
              </button>
              <button
                onClick={loadDemoSlides}
                className="flex w-full items-center justify-between rounded-md border border-dashed border-ink-300/70 bg-paper/70 px-2.5 py-1.5 text-[11px] text-ink-500 transition hover:border-accent/60 hover:bg-white hover:text-accent-deep"
                title={t("Drop editable slides into canvas — no agent call, no money")}
              >
                <span>{t("Try editable slides (no agent)")}</span>
                <I.Deck width={11} height={11} />
              </button>
              <button
                onClick={loadDemoLanding}
                className="flex w-full items-center justify-between rounded-md border border-dashed border-ink-300/70 bg-paper/70 px-2.5 py-1.5 text-[11px] text-ink-500 transition hover:border-accent/60 hover:bg-white hover:text-accent-deep"
                title={t("Drop an editable landing page into canvas — no agent call, no money")}
              >
                <span>{t("Try editable landing (no agent)")}</span>
                <I.Layout width={11} height={11} />
              </button>
              <button
                onClick={loadDemoVideo}
                className="flex w-full items-center justify-between rounded-md border border-dashed border-ink-300/70 bg-paper/70 px-2.5 py-1.5 text-[11px] text-ink-500 transition hover:border-accent/60 hover:bg-white hover:text-accent-deep"
                title={t("Drop an editable video project into canvas — no agent call, no money")}
              >
                <span>{t("Try editable video (no agent)")}</span>
                <I.Video width={11} height={11} />
              </button>
              <button
                onClick={loadDemoDeck}
                className="flex w-full items-center justify-between rounded-md border border-dashed border-ink-300/70 bg-paper/70 px-2.5 py-1.5 text-[11px] text-ink-500 transition hover:border-accent/60 hover:bg-white hover:text-accent-deep"
                title={t("Drop a fake 6-slide deck preview into canvas — no agent call, no money")}
              >
                <span>{t("Try deck preview (no agent)")}</span>
                <span className="text-[10px]">→</span>
              </button>
            </div>
          )}
        </div>
      </div>

      <div className="px-3 pb-2 pt-2">
        <span className="eyebrow-rule">{t("Recent")}</span>
      </div>

      <div className="flex-1 space-y-0.5 overflow-y-auto px-1.5 pb-3">
        {conversations.length === 0 ? (
          <div className="px-3 py-6 text-[12px] italic text-ink-500">
            {t("Nothing here yet.")}
          </div>
        ) : (
          conversations.map((c) => (
            <ConversationItem
              key={c.id}
              c={c}
              active={c.id === current_id}
              onClick={() => switchConversation(c.id)}
              onDelete={() => deleteConversation(c.id)}
            />
          ))
        )}
      </div>

      <div className="flex items-center justify-between border-t border-ink-300/50 px-3 py-2.5">
        <span className="font-display text-[11px] tracking-[0.02em] text-ink-500" style={{ fontVariationSettings: '"opsz" 36' }}>
          AutoDesign <span className="mx-1 text-ink-400">·</span>{" "}
          <span
            className="tabular text-[9.5px] uppercase text-ink-400"
            style={{ letterSpacing: "0.18em" }}
          >
            Beta 1.0
          </span>
        </span>
        <div className="flex items-center gap-1.5">
          <LanguageMenu compact />
          <SettingsButton />
        </div>
      </div>

      <ResizeHandle
        side="right"
        getCurrentSize={() => useApp.getState().history_sidebar_width}
        setSize={(px) => setSidebarWidth("history", px)}
      />
    </aside>
  );
}

function SettingsButton() {
  const open = useApp((s) => s.openSettings);
  const needs_setup = useApp((s) => s.backend_needs_setup);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  return (
    <button
      type="button"
      onClick={open}
      className="relative inline-flex items-center gap-1.5 rounded-md px-1.5 py-1 text-ink-500 transition hover:text-ink-900"
      title={t("API keys & settings")}
    >
      <I.Settings width={12} height={12} />
      <span className="text-[10px] uppercase" style={{ letterSpacing: "0.16em" }}>
        {t("Settings")}
      </span>
      {needs_setup && (
        <span
          className="absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-full bg-amber-600"
          aria-label={t("Needs setup")}
        />
      )}
    </button>
  );
}

function ConversationItem({
  c,
  active,
  onClick,
  onDelete,
}: {
  c: Conversation;
  active: boolean;
  onClick: () => void;
  onDelete: () => void;
}) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const subtitle = subtitleFor(c, language);
  return (
    <div
      onClick={onClick}
      role="button"
      className={`group relative flex cursor-pointer items-start gap-2 rounded-md px-3 py-2 transition ${
        active
          ? "border border-ink-300/65 bg-surface-raised shadow-soft"
          : "border border-transparent hover:border-ink-300/50 hover:bg-surface-raised/65"
      }`}
    >
      {/* Active rule — 1.5px ink-900 bar at left */}
      {active && (
        <span
          aria-hidden
          className="absolute inset-y-1.5 left-0.5 w-[2px] rounded-full bg-accent"
        />
      )}
      <div className="min-w-0 flex-1 leading-tight">
        <div
          className={`flex items-center gap-1.5 text-[13px] font-medium ${active ? "text-ink-900" : "text-ink-700"}`}
          title={c.title}
        >
          {c.pending && (
            <span
              aria-label={t("Run in progress")}
              title={t("Run in progress")}
              className="relative inline-flex h-1.5 w-1.5 shrink-0 rounded-full bg-accent"
            >
              <span className="absolute inset-0 animate-ping rounded-full bg-accent/60" />
            </span>
          )}
          <span className="truncate">{c.title || t("New chat")}</span>
        </div>
        <div className="mt-0.5 truncate text-[10.5px] uppercase text-ink-500" style={{ letterSpacing: "0.1em" }}>
          {subtitle}
        </div>
      </div>
      <button
        onClick={(e) => {
          e.stopPropagation();
          if (
            historyMessageCount(c) === 0 ||
            window.confirm(translate(language, "Delete conversation confirm", { title: c.title }))
          ) {
            onDelete();
          }
        }}
        className="opacity-0 transition group-hover:opacity-100"
        title={t("Delete conversation")}
      >
        <I.Trash width={12} height={12} />
      </button>
    </div>
  );
}

function subtitleFor(c: Conversation, language: UiLanguage): string {
  const artifactCount = Object.keys(c.artifacts).length;
  const msgCount = historyMessageCount(c);
  const time = relativeTime(c.updated_at, language);
  if (msgCount === 0) return time;
  if (artifactCount === 0) return `${msgCount} ${translate(language, "msg")} · ${time}`;
  return `${artifactCount} ${translate(language, artifactCount > 1 ? "artifacts" : "artifact")} · ${time}`;
}

function historyMessageCount(c: Conversation): number {
  return c.history_summary ? c.history_message_count ?? 0 : c.messages.length;
}

function relativeTime(ts: number, language: UiLanguage): string {
  const diff = Date.now() - ts;
  const m = Math.round(diff / 60000);
  if (m < 1) return translate(language, "just now");
  if (m < 60) return language === "zh" ? `${m} 分钟` : `${m}m`;
  const h = Math.round(m / 60);
  if (h < 24) return language === "zh" ? `${h} 小时` : `${h}h`;
  const d = Math.round(h / 24);
  if (d < 7) return language === "zh" ? `${d} 天` : `${d}d`;
  return new Date(ts).toLocaleDateString();
}
