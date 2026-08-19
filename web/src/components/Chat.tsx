import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  artifactTypeForArtifact,
  effectiveArtifactType,
  useApp,
  useArtifactById,
  useCurrentConversation,
  useMessages,
} from "@/lib/store";
import type {
  Artifact,
  ArtifactType,
  Attachment,
  Message,
  PosterAreaSelectionItem,
  PosterSelectionSummary,
  PosterCanvasPreset,
  PosterPalette,
} from "@/lib/types";
import { nextId } from "@/lib/mock";
import { readKeys } from "@/lib/keys";
import { readConfig } from "@/lib/api_settings";
import { customOpenAIBaseUrlError } from "@/lib/settings_validation";
import {
  DENSE_PAPER_POSTER_PROMPT,
  VIDEO_ARTIFACT_DESCRIPTION,
} from "@/lib/presets";
import { paperBundleBlocksPptxExport } from "@/lib/paper_bundle";
import { translate } from "@/lib/i18n";
import {
  authoringBudgetFor,
  readAuthoringBudgets,
  saveAuthoringBudgets,
  type AuthoringBudgets,
} from "@/lib/authoring_budget";
import {
  artifactMessage,
  artifactValidationState,
} from "@/lib/artifact_status";
import { canSubmitPosterCanvasSelection } from "@/lib/poster_canvas_state";
import {
  openResearchNeedsPaperId,
  openResearchResultHref,
  openResearchStatusLabel,
  openResearchStatusMessage,
  openResearchSubmitOptionsFromPaperInput,
} from "@/lib/openresearch";
import { I } from "./icons";
import { ProgressCard } from "./ProgressCard";
import { FailureCard } from "./FailureCard";
import { ResizeHandle } from "./ResizeHandle";
import { ArtifactDownloadMenu } from "./ArtifactDownloadMenu";
import { PaperBundleCard } from "./PaperBundleCard";
import { LanguageMenu } from "./LanguageMenu";
import { PalettePicker } from "./PalettePicker";
import { CanvasPicker } from "./CanvasPicker";
import { AuthoringBudgetControl } from "./AuthoringBudgetControl";
import { AttemptInspector } from "./AttemptInspector";
import {
  ReferenceStyleControl,
  type ReferencePosterPreview,
} from "./ReferenceStyleControl";
import {
  attachmentsForReferencePosterSubmission,
  defaultPaperPosterBrief,
  inputForReferencePosterChoice,
  isReferenceDraftConversation,
  isReferenceStyleControlEligible,
  partitionReferenceAttachments,
  referencePosterValidationMessage,
  replaceStyleReferenceAttachment,
  validateWebReferencePosterFile,
  WEB_REFERENCE_POSTER_ACCEPT,
} from "@/lib/reference_poster";

interface ChatProps {
  /** "full" = chat mode (mode A), "rail" = narrow column inside Canvas (mode B) */
  variant: "full" | "rail";
}

const TEMPLATE_LABEL: Record<ArtifactType, string> = {
  poster: "Poster",
  landing: "Landing",
  deck: "Deck",
  video: "Video",
};

const INTENT_PLACEHOLDER: Record<ArtifactType, string> = {
  poster: "Attach a paper PDF, or describe the poster content and style…",
  landing: "Describe your landing page — product, audience, CTA…",
  deck: "Describe your slide deck — topic, audience, key points…",
  video: "Describe your video — paper to summarize, tone, length…",
};

const QUICK_ACTIONS: Array<{
  type: ArtifactType;
  label: string;
  desc: string;
  icon: (p: any) => JSX.Element;
}> = [
  {
    type: "poster",
    label: "Poster",
    desc: "PDF in · editable poster out",
    icon: I.Poster,
  },
  {
    type: "landing",
    label: "Landing",
    desc: "Web hero · responsive HTML",
    icon: I.Layout,
  },
  {
    type: "deck",
    label: "Deck",
    desc: "HTML · editable slides",
    icon: I.Deck,
  },
  {
    type: "video",
    label: "Video",
    desc: VIDEO_ARTIFACT_DESCRIPTION,
    icon: I.Video,
  },
];

function artifactPreviewAspectRatio(art: Artifact, artType: ArtifactType): number {
  const fallback =
    artType === "landing"
      ? 16 / 10
      : artType === "deck" || artType === "video"
        ? 16 / 9
        : 3 / 4;
  const raw = art.canvas?.w && art.canvas?.h ? art.canvas.w / art.canvas.h : fallback;
  if (!Number.isFinite(raw) || raw <= 0) return fallback;
  return Math.min(2.2, Math.max(0.75, raw));
}

function resolveComposerArtifactType({
  intent,
  hasPaperPdf,
  activeArtifact,
  brief,
}: {
  intent: ArtifactType | null;
  hasPaperPdf: boolean;
  activeArtifact: Artifact | undefined;
  brief: string;
}): ArtifactType {
  if (intent) return intent;
  if (hasPaperPdf) return "poster";
  if (activeArtifact) return artifactTypeForArtifact(activeArtifact);
  return effectiveArtifactType(null, brief);
}

function hasConfiguredBrowserProvider(): boolean {
  const { keys, bases } = readConfig();
  if (keys.openrouter?.trim() || keys.anthropic?.trim() || keys.openai?.trim()) return true;

  const customOpenAIBaseUrl = bases.custom_openai?.trim();
  return Boolean(
    keys.custom_openai?.trim()
    && customOpenAIBaseUrl
    && !customOpenAIBaseUrlError(customOpenAIBaseUrl),
  );
}

function visibleComposerInputAfterPaperPdfChoice(input: string): string {
  return input === DENSE_PAPER_POSTER_PROMPT ? "" : input;
}

export function Chat({ variant }: ChatProps) {
  const messages = useMessages();
  // Per-conversation pending — only this conversation's Send is
  // disabled, other conversations can fire in parallel.
  const conv = useCurrentConversation();
  const pending = !!conv?.pending;
  const intent_type = useApp((s) => s.intent_type);
  const current_conversation_id = useApp((s) => s.current_conversation_id);
  const setIntent = useApp((s) => s.setIntent);
  const sendMessage = useApp((s) => s.sendMessage);
  const enterCanvas = useApp((s) => s.enterCanvas);
  const enterChat = useApp((s) => s.enterChat);
  const mode = useApp((s) => s.mode);
  const toggleHistory = useApp((s) => s.toggleHistorySidebar);
  const setSidebarWidth = useApp((s) => s.setSidebarWidth);
  const areaItems = useApp((s) => s.area_revision_items);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const posterPalettes = useApp((s) => s.poster_palettes);
  const posterPalettesStatus = useApp((s) => s.poster_palettes_status);
  const posterPalettesError = useApp((s) => s.poster_palettes_error);
  const loadPosterPalettes = useApp((s) => s.loadPosterPalettes);
  const setPosterPalette = useApp((s) => s.setPosterPalette);
  const posterCanvasPresets = useApp((s) => s.poster_canvas_presets);
  const posterCanvasPresetsStatus = useApp((s) => s.poster_canvas_presets_status);
  const posterCanvasPresetsError = useApp((s) => s.poster_canvas_presets_error);
  const loadPosterCanvasPresets = useApp((s) => s.loadPosterCanvasPresets);
  const setPosterCanvasPreset = useApp((s) => s.setPosterCanvasPreset);
  const clearCanvasValidationError = useApp((s) => s.clearCanvasValidationError);
  const canvasValidationError = useApp(
    (s) => s.canvas_validation_errors[current_conversation_id] ?? null,
  );
  const startPaperBundle = useApp((s) => s.startPaperBundle);
  const backendNeedsSetup = useApp((s) => s.backend_needs_setup);
  const openSettings = useApp((s) => s.openSettings);

  const [input, setInput] = useState("");
  const [authoringBudgets, setAuthoringBudgets] = useState(readAuthoringBudgets);
  const [pending_attachments, setAttachments] = useState<Attachment[]>([]);
  const [referencePreview, setReferencePreview] = useState<ReferencePosterPreview | null>(null);
  const [referenceError, setReferenceError] = useState<string | null>(null);
  const referenceSelectionTokenRef = useRef(0);
  const referenceObjectUrlsRef = useRef(new Set<string>());
  const referencePreviewUrlRef = useRef<string | null>(null);
  const referenceDraftConversationIdRef = useRef<string | null>(current_conversation_id);
  const [paletteOpenRequest, setPaletteOpenRequest] = useState(0);
  const [paletteInvalid, setPaletteInvalid] = useState(false);
  const [canvasOpenRequest, setCanvasOpenRequest] = useState(0);
  const [canvasInvalid, setCanvasInvalid] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const attachmentRoleRef = useRef<Attachment["role"]>("content");
  const paperBundlePickerIntentRef = useRef(false);
  const paperBundlePickerConversationIdRef = useRef<string | null>(null);
  const paperBundleStartingRef = useRef(new Set<string>());
  const [paperBundleStartingConversationIds, setPaperBundleStartingConversationIds] = useState<
    Record<string, true>
  >({});
  const [paperBundlePickerErrors, setPaperBundlePickerErrors] = useState<Record<string, string>>({});
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const paperBundleStarting = !!paperBundleStartingConversationIds[current_conversation_id];
  const paperBundlePickerError = paperBundlePickerErrors[current_conversation_id] ?? null;

  const clearPaperBundleStarting = useCallback((conversationId: string) => {
    paperBundleStartingRef.current.delete(conversationId);
    setPaperBundleStartingConversationIds((current) => {
      if (!current[conversationId]) return current;
      const next = { ...current };
      delete next[conversationId];
      return next;
    });
  }, []);

  const clearPaperBundlePickerError = useCallback((conversationId: string) => {
    setPaperBundlePickerErrors((current) => {
      if (!current[conversationId]) return current;
      const next = { ...current };
      delete next[conversationId];
      return next;
    });
  }, []);

  const updateAuthoringBudgets = useCallback((budgets: AuthoringBudgets) => {
    saveAuthoringBudgets(budgets);
    setAuthoringBudgets(budgets);
  }, []);

  const revokeReferenceObjectUrl = useCallback((url: string) => {
    if (!referenceObjectUrlsRef.current.delete(url)) return;
    URL.revokeObjectURL(url);
    if (referencePreviewUrlRef.current === url) {
      referencePreviewUrlRef.current = null;
    }
  }, []);

  const revokeAllReferenceObjectUrls = useCallback(() => {
    for (const url of [...referenceObjectUrlsRef.current]) {
      revokeReferenceObjectUrl(url);
    }
  }, [revokeReferenceObjectUrl]);

  const revokePendingReferenceObjectUrls = useCallback(() => {
    for (const url of [...referenceObjectUrlsRef.current]) {
      if (url !== referencePreviewUrlRef.current) {
        revokeReferenceObjectUrl(url);
      }
    }
  }, [revokeReferenceObjectUrl]);

  const resetFileInput = useCallback(() => {
    if (fileRef.current) {
      fileRef.current.value = "";
      fileRef.current.accept = "";
      fileRef.current.multiple = true;
    }
    attachmentRoleRef.current = "content";
    paperBundlePickerIntentRef.current = false;
    paperBundlePickerConversationIdRef.current = null;
  }, []);

  const setFileInputRef = useCallback((node: HTMLInputElement | null) => {
    if (fileRef.current) {
      fileRef.current.removeEventListener("cancel", resetFileInput);
    }
    fileRef.current = node;
    if (node) {
      node.addEventListener("cancel", resetFileInput);
    }
  }, [resetFileInput]);

  const openAttachmentPicker = useCallback((accept?: string, role: Attachment["role"] = "content") => {
    if (!fileRef.current || paperBundleStartingRef.current.has(current_conversation_id)) return;
    paperBundlePickerIntentRef.current = false;
    paperBundlePickerConversationIdRef.current = null;
    attachmentRoleRef.current = role;
    fileRef.current.accept = accept ?? "";
    fileRef.current.multiple = true;
    fileRef.current.click();
  }, [current_conversation_id]);

  const attachPaperBundle = useCallback(() => {
    if (
      !fileRef.current
      || paperBundleStartingRef.current.has(current_conversation_id)
      || pending
    ) return;
    clearPaperBundlePickerError(current_conversation_id);
    paperBundlePickerIntentRef.current = true;
    paperBundlePickerConversationIdRef.current = current_conversation_id;
    attachmentRoleRef.current = "content";
    fileRef.current.value = "";
    fileRef.current.accept = ".pdf,application/pdf";
    fileRef.current.multiple = false;
    fileRef.current.click();
  }, [clearPaperBundlePickerError, current_conversation_id, pending]);

  const attachPaperPdf = useCallback(() => {
    setIntent("poster");
    setInput(visibleComposerInputAfterPaperPdfChoice);
    openAttachmentPicker(".pdf,application/pdf", "content");
  }, [
    openAttachmentPicker,
    setIntent,
  ]);

  const attachReferencePoster = useCallback(() => {
    setIntent("poster");
    setInput((current) => (
      inputForReferencePosterChoice(current, DENSE_PAPER_POSTER_PROMPT)
    ));
    openAttachmentPicker(WEB_REFERENCE_POSTER_ACCEPT, "style_reference");
  }, [openAttachmentPicker, setIntent]);

  useEffect(() => () => {
    referenceSelectionTokenRef.current += 1;
    revokeAllReferenceObjectUrls();
  }, [revokeAllReferenceObjectUrls]);

  const paperBundleRunning = conv?.paper_bundle?.kind === "parent"
    && Object.values(conv.paper_bundle.tasks).some(
      (task) => task.status === "pending" || task.status === "running",
    );

  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    scroller.scrollTo({
      top: paperBundleRunning ? 0 : scroller.scrollHeight,
      behavior: "smooth",
    });
  }, [messages.length, paperBundleRunning, pending]);

  useEffect(() => {
    if (!paperBundleStarting || conv?.paper_bundle?.kind !== "parent") return;
    clearPaperBundleStarting(current_conversation_id);
  }, [clearPaperBundleStarting, conv?.paper_bundle, current_conversation_id, paperBundleStarting]);

  // Reset draft when switching conversations.
  useEffect(() => {
    referenceSelectionTokenRef.current += 1;
    revokeAllReferenceObjectUrls();
    setInput("");
    setAttachments([]);
    setReferencePreview(null);
    setReferenceError(null);
    setPaletteInvalid(false);
    referenceDraftConversationIdRef.current = current_conversation_id;
    if (!paperBundlePickerIntentRef.current) {
      resetFileInput();
    }
  }, [current_conversation_id, resetFileInput, revokeAllReferenceObjectUrls]);

  const activeArtifact = conv?.active_artifact_id
    ? conv.artifacts[conv.active_artifact_id]
    : undefined;
  const draftBelongsToCurrentConversation = isReferenceDraftConversation(
    referenceDraftConversationIdRef.current,
    current_conversation_id,
  );
  const currentPendingAttachments = draftBelongsToCurrentConversation
    ? pending_attachments
    : [];
  const currentReferencePreview = draftBelongsToCurrentConversation
    ? referencePreview
    : null;
  const currentReferenceError = draftBelongsToCurrentConversation
    ? referenceError
    : null;
  const {
    content: pendingContentAttachments,
    reference: pendingReferenceAttachment,
  } = partitionReferenceAttachments(currentPendingAttachments);
  const hasPaperPdf = pendingContentAttachments.some((item) => item.kind === "pdf")
    || messages.some((message) => (
      message.role === "user"
      && partitionReferenceAttachments(message.attachments ?? []).content.some(
        (item) => item.kind === "pdf",
      )
    ));
  const canUseAreaNotes = !!(
    currentPendingAttachments.length === 0
    && activeArtifact
    && artifactTypeForArtifact(activeArtifact) === "poster"
    && activeArtifact.native_format === "html"
    && activeArtifact.native_file_url
  );
  const areaInstructionFallback = canUseAreaNotes
    ? selectedAreaInstructionBrief(areaItems)
    : "";
  const composerBrief = input.trim()
    || areaInstructionFallback
    || defaultPaperPosterBrief(
      hasPaperPdf,
      !intent_type || intent_type === "poster",
      Boolean(pendingReferenceAttachment),
      DENSE_PAPER_POSTER_PROMPT,
    );
  const resolvedComposerArtifactType = resolveComposerArtifactType({
    intent: intent_type,
    hasPaperPdf,
    activeArtifact,
    brief: composerBrief,
  });
  const posterContext = resolvedComposerArtifactType === "poster";
  const selectedPaletteId = conv?.poster_palette_id?.trim() || null;
  const selectedPaletteIsCanonical = !!(
    posterPalettesStatus === "ready"
    && selectedPaletteId
    && posterPalettes.some((palette) => palette.id === selectedPaletteId)
  );
  const selectedCanvasPresetId = conv?.poster_canvas_preset_id ?? "auto";
  const selectedCanvasPresetIsCanonical = canSubmitPosterCanvasSelection(
    posterCanvasPresetsStatus,
    posterCanvasPresets,
    selectedCanvasPresetId,
  );
  const canvasComposerProps = {
    canvasPresets: posterCanvasPresets,
    canvasPresetStatus: posterCanvasPresetsStatus,
    canvasPresetError: posterCanvasPresetsError,
    selectedCanvasPresetId,
    canvasOpenRequest,
    canvasInvalid,
    canvasValidationError: canvasValidationError?.message ?? null,
    onSelectCanvasPreset: (presetId: string) => {
      setPosterCanvasPreset(presetId);
      setCanvasInvalid(false);
    },
    onRetryCanvasPresets: () => { void loadPosterCanvasPresets(); },
    onClearCanvasValidationError: () => clearCanvasValidationError(),
  };

  useEffect(() => {
    if (posterContext && posterPalettesStatus === "idle") {
      void loadPosterPalettes();
    }
  }, [loadPosterPalettes, posterContext, posterPalettesStatus]);

  useEffect(() => {
    if (posterContext && posterCanvasPresetsStatus === "idle") {
      void loadPosterCanvasPresets();
    }
  }, [loadPosterCanvasPresets, posterCanvasPresetsStatus, posterContext]);

  useEffect(() => {
    if (paletteInvalid && selectedPaletteIsCanonical) {
      setPaletteInvalid(false);
    }
  }, [paletteInvalid, selectedPaletteIsCanonical]);

  useEffect(() => {
    if (canvasInvalid && selectedCanvasPresetIsCanonical) {
      setCanvasInvalid(false);
    }
  }, [canvasInvalid, selectedCanvasPresetIsCanonical]);

  useEffect(() => {
    if (!canvasValidationError || input !== "") return;
    setInput(canvasValidationError.brief);
  }, [canvasValidationError, input]);

  useEffect(() => {
    if (
      hasPaperPdf
      && referenceError === "A paper PDF is required when using a reference style."
    ) {
      setReferenceError(null);
    }
  }, [hasPaperPdf, referenceError]);

  const onAttach = (files: FileList | null) => {
    if (paperBundlePickerIntentRef.current) {
      const parentConversationId = paperBundlePickerConversationIdRef.current;
      const parentConversation = parentConversationId
        ? useApp.getState().conversations[parentConversationId]
        : undefined;
      if (
        !parentConversationId
        || paperBundleStartingRef.current.has(parentConversationId)
        || parentConversation?.pending
      ) {
        resetFileInput();
        return;
      }
      const selectedFiles = files ? Array.from(files) : [];
      const file = selectedFiles[0];
      const isPdf = !!file && (
        file.type === "application/pdf" || /\.pdf$/i.test(file.name)
      );
      if (selectedFiles.length !== 1 || !isPdf) {
        setPaperBundlePickerErrors((current) => ({
          ...current,
          [parentConversationId]: "Choose one PDF file to start Paper All-in-One.",
        }));
        resetFileInput();
        return;
      }
      clearPaperBundlePickerError(parentConversationId);
      paperBundleStartingRef.current.add(parentConversationId);
      setPaperBundleStartingConversationIds((current) => ({
        ...current,
        [parentConversationId]: true,
      }));
      void startPaperBundle(file, parentConversationId)
        .then(() => {
          clearPaperBundleStarting(parentConversationId);
        })
        .catch((error) => {
          clearPaperBundleStarting(parentConversationId);
          setPaperBundlePickerErrors((current) => ({
            ...current,
            [parentConversationId]: error instanceof Error
              ? error.message
              : "Paper All-in-One could not be started.",
          }));
        });
      resetFileInput();
      return;
    }
    if (!files) return;
    const role = attachmentRoleRef.current ?? "content";
    if (role === "style_reference") {
      const file = files[0];
      if (!file) {
        resetFileInput();
        return;
      }

      const selectionToken = ++referenceSelectionTokenRef.current;
      const selectionConversationId = current_conversation_id;
      revokePendingReferenceObjectUrls();
      const validation = validateWebReferencePosterFile(file);
      if (!validation.ok) {
        setReferenceError(referencePosterValidationMessage(validation.code));
        resetFileInput();
        return;
      }

      setReferenceError(null);
      const objectUrl = URL.createObjectURL(file);
      referenceObjectUrlsRef.current.add(objectUrl);
      const image = new Image();
      image.onload = () => {
        if (selectionToken !== referenceSelectionTokenRef.current) {
          revokeReferenceObjectUrl(objectUrl);
          return;
        }
        const width = image.naturalWidth;
        const height = image.naturalHeight;
        if (width <= 0 || height <= 0) {
          revokeReferenceObjectUrl(objectUrl);
          setReferenceError("The selected poster image could not be read.");
          return;
        }

        const previousUrl = referencePreviewUrlRef.current;
        if (previousUrl) revokeReferenceObjectUrl(previousUrl);
        referencePreviewUrlRef.current = objectUrl;
        referenceDraftConversationIdRef.current = selectionConversationId;
        setReferencePreview({ url: objectUrl, width, height });
        setReferenceError(null);
        setAttachments((current) => replaceStyleReferenceAttachment(current, {
          id: nextId("att"),
          name: file.name,
          size: file.size,
          role: "style_reference",
          kind: "image",
          file,
        }));
      };
      image.onerror = () => {
        revokeReferenceObjectUrl(objectUrl);
        if (selectionToken === referenceSelectionTokenRef.current) {
          setReferenceError("The selected poster image could not be read.");
        }
      };
      image.src = objectUrl;
      resetFileInput();
      return;
    }

    const selectedFiles = Array.from(files);
    const next: Attachment[] = selectedFiles.map((f) => ({
      id: nextId("att"),
      name: f.name,
      size: f.size,
      role,
      kind: /\.pdf$/i.test(f.name)
        ? "pdf"
        : /\.(png|jpg|jpeg|webp)$/i.test(f.name)
          ? "image"
          : /\.(docx?|pptx?|md|txt)$/i.test(f.name)
            ? "doc"
            : "other",
      // Keep the File handle so the multipart upload can stream the bytes.
      file: f,
    }));
    const hasPdf = next.some((a) => a.kind === "pdf" && a.role !== "style_reference");
    if (hasPdf && (!intent_type || intent_type === "poster")) {
      setIntent("poster");
      setInput(visibleComposerInputAfterPaperPdfChoice);
    }
    setAttachments((current) => [...current, ...next]);
    resetFileInput();
  };

  const removeReferencePoster = useCallback(() => {
    referenceSelectionTokenRef.current += 1;
    revokeAllReferenceObjectUrls();
    setReferencePreview(null);
    setReferenceError(null);
    setAttachments((current) => (
      partitionReferenceAttachments(current).content
    ));
  }, [revokeAllReferenceObjectUrls]);

  useEffect(() => {
    if (!posterContext && pendingReferenceAttachment) {
      removeReferencePoster();
    }
  }, [pendingReferenceAttachment, posterContext, removeReferencePoster]);

  const removePendingAttachment = (id: string) => {
    if (pendingReferenceAttachment?.id === id) {
      removeReferencePoster();
      return;
    }
    setAttachments((current) => current.filter((item) => item.id !== id));
  };

  const onSend = () => {
    if (paperBundleStartingRef.current.has(current_conversation_id) || pending) return;
    const submissionAttachments = attachmentsForReferencePosterSubmission(
      currentPendingAttachments,
      resolvedComposerArtifactType,
    );
    if (
      !composerBrief &&
      submissionAttachments.length === 0 &&
      !intent_type
    ) {
      return;
    }
    if (pendingReferenceAttachment && !hasPaperPdf) {
      setReferenceError("A paper PDF is required when using a reference style.");
      return;
    }
    if (
      resolvedComposerArtifactType === "poster"
      && backendNeedsSetup
      && !hasConfiguredBrowserProvider()
    ) {
      setPaletteInvalid(false);
      openSettings();
      return;
    }
    if (resolvedComposerArtifactType === "poster" && !selectedPaletteIsCanonical) {
      setPaletteInvalid(true);
      setPaletteOpenRequest((value) => value + 1);
      return;
    }
    if (resolvedComposerArtifactType === "poster" && !selectedCanvasPresetIsCanonical) {
      setCanvasInvalid(true);
      setCanvasOpenRequest((value) => value + 1);
      return;
    }
    setPaletteInvalid(false);
    setCanvasInvalid(false);
    if (!intent_type) {
      setIntent(resolvedComposerArtifactType);
    }
    sendMessage(composerBrief, submissionAttachments, {
      authoring_max_attempts: authoringBudgetFor(
        authoringBudgets,
        resolvedComposerArtifactType,
      ),
    });
    referenceSelectionTokenRef.current += 1;
    revokeAllReferenceObjectUrls();
    setInput("");
    setAttachments([]);
    setReferencePreview(null);
    setReferenceError(null);
    resetFileInput();
  };

  const placeholder =
    mode === "chat"
      ? intent_type
        ? t(INTENT_PLACEHOLDER[intent_type])
        : t("Attach a paper PDF, or describe what you want to make")
      : t("Refine the design, ask for variations…");

  const hasConversation =
    messages.length > 0
    || Object.keys(conv?.artifacts ?? {}).length > 0
    || conv?.paper_bundle?.kind === "parent";

  if (variant === "full") {
    return (
      <div className="canvas-grid-bg relative flex h-full min-h-0 w-full flex-col">
        {/* Soft ambient halo — barely-there spotlight at the top of the
         *  page. Reads as "natural light on paper" rather than the loud
         *  radial gradient AI-style hero. */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 top-0 h-[520px]"
          style={{
            background:
              "radial-gradient(ellipse 900px 380px at 50% -10%, oklch(1 0 0 / 0.72) 0%, oklch(0.982 0.005 88 / 0) 70%)",
          }}
        />
        <TopBar />

        {!hasConversation ? (
          <EmptyHero
            input={input}
            setInput={setInput}
            attachments={currentPendingAttachments}
            removeAttachment={removePendingAttachment}
            placeholder={placeholder}
            onSend={onSend}
            onAttachClick={() => openAttachmentPicker()}
            disabled={pending || paperBundleStarting}
            locked={paperBundleStarting}
            intent={intent_type}
            onSetIntent={setIntent}
            onAttachPaperPdf={attachPaperPdf}
            onAttachPaperBundle={attachPaperBundle}
            paperBundleStarting={paperBundleStarting}
            paperBundlePickerError={paperBundlePickerError}
            onAttachStyleReference={attachReferencePoster}
            reference={pendingReferenceAttachment}
            referencePreview={currentReferencePreview}
            referenceError={currentReferenceError}
            hasPaperPdf={hasPaperPdf}
            onRemoveReference={removeReferencePoster}
            posterContext={posterContext}
            palettes={posterPalettes}
            paletteStatus={posterPalettesStatus}
            paletteError={posterPalettesError}
            selectedPaletteId={selectedPaletteId}
            paletteOpenRequest={paletteOpenRequest}
            paletteInvalid={paletteInvalid}
            {...canvasComposerProps}
            authoringBudgets={authoringBudgets}
            onAuthoringBudgetsChange={updateAuthoringBudgets}
            onSelectPalette={(paletteId) => {
              setPosterPalette(paletteId);
              setPaletteInvalid(false);
            }}
            onRetryPalettes={() => void loadPosterPalettes()}
          />
        ) : (
          <div
            data-chat-workspace="true"
            className="relative flex min-h-0 flex-1 overflow-hidden"
          >
            <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
              <div ref={scrollerRef} className="min-h-0 flex-1 overflow-y-auto">
                <div className="mx-auto w-full max-w-[820px] px-8 py-8">
                  <div className="space-y-5">
                    {conv?.paper_bundle?.kind === "parent" && (
                      <PaperBundleCard bundle={conv.paper_bundle} />
                    )}
                    {messages.map((m) => (
                      <MessageBubble
                        key={m.id}
                        m={m}
                        onOpenCanvas={enterCanvas}
                      />
                    ))}
                  </div>
                </div>
              </div>
              <div className="shrink-0 border-t border-ink-300/45 bg-paper/70 backdrop-blur-md">
                <div className="mx-auto w-full max-w-[820px] px-8 py-4">
                  <PaperBundleStartupFeedback
                    starting={paperBundleStarting}
                    error={paperBundlePickerError}
                    className="mb-3"
                  />
                  <Composer
                    input={input}
                    setInput={setInput}
                    attachments={currentPendingAttachments}
                    removeAttachment={removePendingAttachment}
                    placeholder={placeholder}
                    onSend={onSend}
                    onAttachClick={() => openAttachmentPicker()}
                    onAttachStyleReference={attachReferencePoster}
                    reference={pendingReferenceAttachment}
                    referencePreview={currentReferencePreview}
                    referenceError={currentReferenceError}
                    hasPaperPdf={hasPaperPdf}
                    onRemoveReference={removeReferencePoster}
                    onAttachPaperPdf={attachPaperPdf}
                    posterContext={posterContext}
                    palettes={posterPalettes}
                    paletteStatus={posterPalettesStatus}
                    paletteError={posterPalettesError}
                    selectedPaletteId={selectedPaletteId}
                    paletteOpenRequest={paletteOpenRequest}
                    paletteInvalid={paletteInvalid}
                    {...canvasComposerProps}
                    authoringBudgets={authoringBudgets}
                    onAuthoringBudgetsChange={updateAuthoringBudgets}
                    onSelectPalette={(paletteId) => {
                      setPosterPalette(paletteId);
                      setPaletteInvalid(false);
                    }}
                    onRetryPalettes={() => void loadPosterPalettes()}
                    disabled={pending || paperBundleStarting}
                    locked={paperBundleStarting}
                    intent={intent_type}
                    onSetIntent={setIntent}
                  />
                </div>
              </div>
            </div>
            <AttemptInspector variant="rail" />
          </div>
        )}

        <input
          ref={setFileInputRef}
          type="file"
          multiple
          disabled={paperBundleStarting}
          className="hidden"
          onChange={(e) => onAttach(e.target.files)}
        />
      </div>
    );
  }

  // rail variant
  return (
    <div className="app-panel relative flex h-full min-h-0 w-full flex-col border-r">
      <div className="flex h-11 items-center justify-between border-b border-ink-300/55 px-3">
        <div className="flex items-center gap-1.5">
          <button
            onClick={toggleHistory}
            className="icon-btn"
            title={t("Toggle history")}
          >
            <I.PanelLeft width={14} height={14} />
          </button>
          <span className="mx-0.5 h-3.5 w-px bg-ink-300/80" />
          <button
            onClick={enterChat}
            className="group flex items-center gap-1.5 rounded-md px-1.5 py-1 text-[12px] text-ink-500 transition hover:text-ink-900"
          >
            <I.ArrowLeft width={13} height={13} />
            <span className="font-display" style={{ fontVariationSettings: '"opsz" 36' }}>
              {t("Back")}
            </span>
          </button>
        </div>
        <span className="eyebrow">{t("Conversation")}</span>
      </div>
      <div ref={scrollerRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-5">
        <div className="space-y-5">
          {conv?.paper_bundle?.kind === "parent" && (
            <PaperBundleCard bundle={conv.paper_bundle} compact />
          )}
          {messages.map((m) => (
            <MessageBubble key={m.id} m={m} compact onOpenCanvas={enterCanvas} />
          ))}
        </div>
      </div>
      <div className="border-t border-ink-300/55 p-3">
        <PaperBundleStartupFeedback
          starting={paperBundleStarting}
          error={paperBundlePickerError}
          className="mb-3"
        />
        <Composer
          compact
          input={input}
          setInput={setInput}
          attachments={currentPendingAttachments}
          removeAttachment={removePendingAttachment}
          placeholder={placeholder}
          onSend={onSend}
          onAttachClick={() => openAttachmentPicker()}
          onAttachStyleReference={attachReferencePoster}
          reference={pendingReferenceAttachment}
          referencePreview={currentReferencePreview}
          referenceError={currentReferenceError}
          hasPaperPdf={hasPaperPdf}
          onRemoveReference={removeReferencePoster}
          onAttachPaperPdf={attachPaperPdf}
          posterContext={posterContext}
          palettes={posterPalettes}
          paletteStatus={posterPalettesStatus}
          paletteError={posterPalettesError}
          selectedPaletteId={selectedPaletteId}
          paletteOpenRequest={paletteOpenRequest}
          paletteInvalid={paletteInvalid}
          {...canvasComposerProps}
          authoringBudgets={authoringBudgets}
          onAuthoringBudgetsChange={updateAuthoringBudgets}
          onSelectPalette={(paletteId) => {
            setPosterPalette(paletteId);
            setPaletteInvalid(false);
          }}
          onRetryPalettes={() => void loadPosterPalettes()}
          disabled={pending || paperBundleStarting}
          locked={paperBundleStarting}
          intent={intent_type}
          onSetIntent={setIntent}
        />
        <input
          ref={setFileInputRef}
          type="file"
          multiple
          disabled={paperBundleStarting}
          className="hidden"
          onChange={(e) => onAttach(e.target.files)}
        />
      </div>
      <ResizeHandle
        side="right"
        getCurrentSize={() => useApp.getState().chat_rail_width}
        setSize={(px) => setSidebarWidth("chat_rail", px)}
      />
    </div>
  );
}

function TopBar() {
  const toggleHistory = useApp((s) => s.toggleHistorySidebar);
  const history_open = useApp((s) => s.history_sidebar_open);
  const openSettings = useApp((s) => s.openSettings);
  const needs_setup = useApp((s) => s.backend_needs_setup);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  return (
    <div className="relative z-20 flex h-12 w-full shrink-0 items-center justify-between border-b border-ink-300/50 bg-paper/70 px-4 backdrop-blur-md">
      <div className="flex items-center gap-3">
        <button
          onClick={toggleHistory}
          className="icon-btn"
          title={history_open ? t("Hide history") : t("Show history")}
        >
          <I.PanelLeft />
        </button>
        <span className="mx-1 h-4 w-px bg-ink-300/80" />
        <div className="flex items-center gap-2.5">
          <I.SparkleQuiet width={14} height={14} className="text-accent" />
          <div className="flex items-baseline gap-2.5 leading-none">
            <span
              className="font-display text-[17px] font-medium text-ink-900"
              style={{ fontVariationSettings: '"opsz" 72, "SOFT" 30', letterSpacing: 0 }}
            >
              AutoDesign
            </span>
            <span className="hidden h-3 w-px bg-ink-300 sm:inline-block" />
            <span className="hidden eyebrow sm:inline">{t("Design workbench")}</span>
          </div>
        </div>
      </div>
      <div className="flex items-center gap-3">
        <LanguageMenu />
        <button
          onClick={openSettings}
          className="relative inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-ink-500 transition hover:text-ink-900"
          title={t("API keys & settings")}
        >
          <I.Settings width={13} height={13} />
          <span className="eyebrow hidden sm:inline">{t("Settings")}</span>
          {needs_setup && (
            <span className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-amber-600" />
          )}
        </button>
        <span className="tabular text-[10px] font-medium uppercase text-ink-500" style={{ letterSpacing: "0.18em" }}>
          Beta 1.0
        </span>
      </div>
    </div>
  );
}

interface EmptyHeroProps extends Omit<ComposerProps, "onSetIntent"> {
  intent: ArtifactType | null;
  onSetIntent: (t: ArtifactType | null) => void;
  onAttachPaperPdf: () => void;
  onAttachPaperBundle: () => void;
  paperBundleStarting: boolean;
  paperBundlePickerError: string | null;
}

function EmptyHero({
  intent,
  onSetIntent,
  onAttachPaperPdf,
  onAttachPaperBundle,
  paperBundleStarting,
  paperBundlePickerError,
  ...composer
}: EmptyHeroProps) {
  const demoMode = useApp((s) => !!s.backend_info?.demo_mode);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  return (
    <div className="relative z-10 flex flex-1 flex-col overflow-y-auto px-5 py-6 md:px-8 md:py-8">
      <div className="mx-auto flex w-full max-w-[760px] flex-1 flex-col justify-center">
        <div className="pb-5 text-center">
          <span className="eyebrow-rule justify-center">{t("AutoDesign workbench")}</span>
          <h1 className="mt-4 font-display text-[30px] leading-tight text-ink-900 md:text-[34px]" style={{ fontVariationSettings: '"opsz" 72' }}>
            {t("What will you design today?")}
          </h1>
        </div>

        <button
          type="button"
          onClick={onAttachPaperPdf}
          disabled={paperBundleStarting}
          className="mb-4 flex items-center justify-between gap-4 rounded-lg border border-accent-deep/20 bg-accent px-4 py-3 text-left text-white shadow-page transition hover:bg-accent-deep disabled:cursor-not-allowed disabled:opacity-50"
          title={t("Attach a paper PDF and start the Paper2Poster preset")}
        >
          <span className="flex min-w-0 items-center gap-3">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-white/30 bg-white/12">
              <I.File width={18} height={18} />
            </span>
            <span className="min-w-0">
              <span className="block text-[14px] font-semibold">
                {t("One-button Paper2Poster")}
              </span>
              <span className="block truncate text-[12px] text-white/82">
                {t("Attach PDF · CVPR landscape · dense editable poster")}
              </span>
            </span>
          </span>
          <I.ArrowRight width={15} height={15} className="shrink-0 text-white/88" />
        </button>

        {!demoMode && (
          <button
            type="button"
            onClick={onAttachPaperBundle}
            disabled={paperBundleStarting}
            aria-busy={paperBundleStarting}
            className="group mb-4 w-full rounded-md border border-accent/30 bg-surface-raised/92 px-4 py-3.5 text-left shadow-soft transition hover:border-accent/55 hover:bg-white hover:shadow-page disabled:cursor-wait disabled:opacity-60"
            title={t("Attach one paper PDF and create a complete communication suite")}
          >
            <span className="flex min-w-0 items-start gap-3">
              <span className="mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-accent/25 bg-accent-soft/45 text-accent-deep">
                <I.Asterism width={19} height={19} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block font-display text-[17px] font-semibold leading-tight text-ink-900" style={{ fontVariationSettings: '"opsz" 48' }}>
                  {t(paperBundleStarting
                    ? "Starting Paper All-in-One..."
                    : "One paper. A complete communication suite.")}
                </span>
                <span className="mt-1 block text-[12px] leading-relaxed text-ink-500">
                  {t(paperBundleStarting
                    ? "Checking backend and poster palette..."
                    : "Website, slides, poster, and narrated video with English subtitles from one PDF.")}
                </span>
              </span>
              {paperBundleStarting
                ? <I.Refresh width={15} height={15} className="mt-1 shrink-0 animate-spin text-ink-500" />
                : <I.ArrowRight width={15} height={15} className="mt-1 shrink-0 text-ink-500 transition group-hover:translate-x-0.5 group-hover:text-accent-deep" />}
            </span>
            <span className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 border-t border-ink-300/55 pt-3 sm:grid-cols-4">
              <PaperBundleCapability icon={I.Layout} label={t("Website for promotion")} />
              <PaperBundleCapability icon={I.Deck} label={t("Slides for talks")} />
              <PaperBundleCapability icon={I.Poster} label={t("Poster for conferences")} />
              <PaperBundleCapability icon={I.Video} label={t("Video with narration and subtitles")} />
            </span>
          </button>
        )}

        <PaperBundleStartupFeedback
          starting={paperBundleStarting}
          error={paperBundlePickerError}
          className="mb-4"
        />

        <div>
          <Composer
            {...composer}
            intent={intent}
            onSetIntent={onSetIntent}
            onAttachPaperPdf={onAttachPaperPdf}
          />
        </div>

        <ArtifactTemplateCards
          intent={intent}
          onSelect={onSetIntent}
          demoMode={demoMode}
          disabled={paperBundleStarting}
        />

        <BackendHealthFooter />
      </div>
    </div>
  );
}

function PaperBundleCapability({
  icon: Icon,
  label,
}: {
  icon: (p: any) => JSX.Element;
  label: string;
}) {
  return (
    <span className="flex min-w-0 items-center gap-1.5 text-[10.5px] font-medium leading-tight text-ink-600">
      <Icon width={13} height={13} className="shrink-0 text-accent-deep" />
      <span>{label}</span>
    </span>
  );
}

function PaperBundleStartupFeedback({
  starting,
  error,
  className = "",
}: {
  starting: boolean;
  error: string | null;
  className?: string;
}) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  if (!starting && !error) return null;
  return (
    <div
      className={`${className} rounded-sm border px-3 py-2 text-[12px] ${
        error
          ? "border-red-800/25 bg-red-50 text-red-900"
          : "border-accent/30 bg-accent-soft/35 text-accent-deep"
      }`}
      role={error ? "alert" : "status"}
      aria-live={error ? "assertive" : "polite"}
      aria-atomic="true"
    >
      {error ? t(error) : t("Checking backend and poster palette...")}
    </div>
  );
}

function ArtifactTemplateCards({
  intent,
  onSelect,
  demoMode,
  disabled,
}: {
  intent: ArtifactType | null;
  onSelect: (type: ArtifactType) => void;
  demoMode: boolean;
  disabled: boolean;
}) {
  const cards = demoMode
    ? QUICK_ACTIONS.filter((item) => item.type === "poster")
    : QUICK_ACTIONS;
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  return (
    <div className="mt-6 text-center">
      <div className="mb-3 text-[13px] font-semibold text-ink-600">
        {t("Start with an artifact...")}
      </div>
      <div className="grid grid-cols-2 gap-3 sm:flex sm:justify-center sm:gap-0">
        {cards.map((card, idx) => (
          <ArtifactTemplateCard
            key={card.type}
            card={card}
            selected={intent === card.type}
            tiltClass={TEMPLATE_TILT_CLASSES[idx] ?? ""}
            onClick={() => onSelect(card.type)}
            disabled={disabled}
          />
        ))}
      </div>
    </div>
  );
}

const TEMPLATE_TILT_CLASSES = [
  "sm:-rotate-3 sm:translate-y-2",
  "sm:-rotate-1",
  "sm:rotate-1 sm:translate-y-1",
  "sm:rotate-3 sm:translate-y-2",
];

function ArtifactTemplateCard({
  card,
  selected,
  tiltClass,
  onClick,
  disabled,
}: {
  card: (typeof QUICK_ACTIONS)[number];
  selected: boolean;
  tiltClass: string;
  onClick: () => void;
  disabled: boolean;
}) {
  const Icon = card.icon;
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`group relative flex h-[148px] w-full flex-col rounded-xl border bg-surface-raised/92 p-2.5 text-left shadow-raised transition hover:z-10 hover:-translate-y-1 hover:border-accent/55 hover:bg-white hover:shadow-page disabled:cursor-not-allowed disabled:opacity-50 sm:mx-[-3px] sm:w-[164px] ${tiltClass} ${
        selected
          ? "z-20 border-accent/70 ring-2 ring-accent/75 ring-offset-4 ring-offset-paper"
          : "border-ink-300/70"
      }`}
      title={translate(language, "Select artifact template", { label: t(card.label) })}
    >
      <div className="flex h-[82px] items-center justify-center rounded-lg border border-ink-200/75 bg-vellum/80">
        <TemplatePreviewIcon type={card.type} Icon={Icon} selected={selected} />
      </div>
      <div className="flex flex-1 flex-col justify-center px-1.5 pt-2 text-center">
        <div className="font-display text-[17px] font-semibold leading-tight text-ink-900" style={{ fontVariationSettings: '"opsz" 48' }}>
          {t(card.label)}
        </div>
        <div className="mt-1 truncate text-[11px] text-ink-500">
          {t(card.desc)}
        </div>
      </div>
    </button>
  );
}

function TemplatePreviewIcon({
  type,
  Icon,
  selected,
}: {
  type: ArtifactType;
  Icon: (p: any) => JSX.Element;
  selected: boolean;
}) {
  const accent = selected ? "text-accent-deep" : "text-ink-500";
  return (
    <div className="relative flex h-14 w-20 items-center justify-center">
      <div className="absolute inset-x-1 top-2 h-9 rounded-md border border-ink-300/70 bg-paper shadow-soft" />
      {type === "poster" && (
        <div className="absolute left-5 top-5 grid h-4 w-10 grid-cols-3 gap-1">
          <span className="rounded-sm bg-accent/35" />
          <span className="rounded-sm bg-ink-300/65" />
          <span className="rounded-sm bg-ink-300/50" />
        </div>
      )}
      {type === "landing" && (
        <div className="absolute left-5 top-6 h-2.5 w-10 rounded-full bg-accent/30" />
      )}
      {type === "deck" && (
        <div className="absolute right-3 top-4 h-8 w-12 rounded-md border border-ink-300/60 bg-paper shadow-soft" />
      )}
      {type === "video" && (
        <div className="absolute bottom-3 left-5 h-1 w-10 rounded-full bg-ink-300/70">
          <span className="block h-1 w-5 rounded-full bg-accent/45" />
        </div>
      )}
      <Icon width={24} height={24} className={`relative ${accent}`} />
    </div>
  );
}

// Pretty labels for each agent role, in the order they fire during a
// run. Roles unknown to this map (added later on the backend) still get
// listed using their raw key — the UI degrades gracefully.
const AGENT_LABELS: Array<[string, string]> = [
  ["enhancer", "Prompt Enhancer"],
  ["claim_graph", "Claim Graph"],
  ["designer", "Designer"],
  ["ingest", "Document Ingest"],
  ["deck_outline", "Deck Outline"],
  ["paper_memory", "Paper Memory"],
  ["code_editor", "Code Editor"],
  ["critic", "Critic"],
  ["composer", "Video Composer"],
  ["image", "Image Generator"],
  ["image_fallback", "Image (fallback)"],
];

function BackendHealthFooter() {
  const info = useApp((s) => s.backend_info);
  const needs_setup = useApp((s) => s.backend_needs_setup);
  const openSettings = useApp((s) => s.openSettings);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const [expanded, setExpanded] = useState(false);
  // Local read of stored keys so the CTA flips immediately when a key
  // is saved (avoids waiting for /api/health to re-resolve).
  const [haveLocalKey, setHaveLocalKey] = useState(false);
  useEffect(() => {
    setHaveLocalKey(!!readKeys().openrouter);
    const onStorage = () => setHaveLocalKey(!!readKeys().openrouter);
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [info, needs_setup]);

  if (!info) {
    return (
      <div className="mt-8 flex items-center gap-2 text-[11px] italic text-ink-500">
        <span className="h-1 w-1 animate-pulse rounded-full bg-ink-400" />
        {t("Connecting to agent backend…")}
      </div>
    );
  }
  if (needs_setup && !haveLocalKey) {
    return (
      <button
        type="button"
        onClick={openSettings}
        className="group mt-8 inline-flex items-center gap-2 rounded-sm border border-amber-700/40 bg-amber-50/60 px-3.5 py-2 text-[11.5px] font-medium text-amber-900 transition hover:border-amber-700/70 hover:bg-amber-50"
      >
        <I.Settings width={12} height={12} />
        <span>{t("Set up your API key to start")}</span>
        <I.ArrowRight width={11} height={11} className="transition group-hover:translate-x-0.5" />
      </button>
    );
  }

  // Distinct model count = sense of "how varied is this pipeline"
  const models = Object.fromEntries(
    Object.entries(info.models ?? {}).filter(([id]) => id !== "planner")
  );
  const distinct = new Set(Object.values(models)).size;
  const agentCount = Object.keys(models).length;
  const paperProfile = info.backend_profile?.paper_poster;
  const codeEditorProfile = info.backend_profile?.code_editor;
  const paperAuthorCmdMissing = paperProfile?.designer_author_cmd_available === false;
  const codeEditorCmdMissing = codeEditorProfile?.available === false;

  return (
    <div className="mt-10 max-w-xl">
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        className="group inline-flex items-center gap-2 text-ink-500 transition hover:text-ink-900"
      >
        <span className="h-1 w-1 rounded-full bg-accent" />
        <span className="eyebrow">
          <span className="tabular">{agentCount}</span> {t("Agents")}
          <span className="mx-1.5 text-ink-300">/</span>
          <span className="tabular">{distinct}</span> {t("Models")}
        </span>
        <I.ChevronDown
          width={10}
          height={10}
          className="transition"
          style={{ transform: expanded ? "rotate(180deg)" : undefined }}
        />
      </button>
      {expanded && (
        <div className="mt-4 grid w-full max-w-[520px] grid-cols-1 divide-y divide-ink-200/70 overflow-hidden rounded-sm border border-ink-300/70 bg-vellum">
          {paperProfile && (
            <>
              <ProfileRow label={t("Paper Poster")} value={paperProfile.template} />
              <ProfileRow
                label={t("Author")}
                value={`${paperProfile.designer_author} / ${paperProfile.designer_author_harness}`}
              />
              <ProfileRow
                label={t("Author Cmd")}
                value={
                  paperAuthorCmdMissing
                    ? t("Missing")
                    : shortCommandName(paperProfile.designer_author_cmd)
                }
                title={
                  paperAuthorCmdMissing
                    ? paperProfile.designer_author_cmd_message
                    : [
                        paperProfile.designer_author_cmd_source,
                        paperProfile.designer_author_cmd,
                      ].filter(Boolean).join(" · ")
                }
              />
            </>
          )}
          {codeEditorProfile && (
            <>
              <ProfileRow
                label={t("Code Editor")}
                value={
                  codeEditorCmdMissing
                    ? t("Missing")
                    : `${codeEditorProfile.harness} / ${shortCommandName(codeEditorProfile.cmd)}`
                }
                title={
                  codeEditorCmdMissing
                    ? codeEditorProfile.message
                    : [
                        codeEditorProfile.cmd_source,
                        codeEditorProfile.cmd,
                      ].filter(Boolean).join(" · ")
                }
              />
              <ProfileRow
                label={t("Code Editor Auth")}
                value={
                  codeEditorCmdMissing
                    ? t("Unavailable")
                    : codeEditorProfile.auth_status === "verified"
                      ? t("Verified")
                      : t("Auth not verified")
                }
                title={codeEditorProfile.auth_message}
              />
            </>
          )}
          {AGENT_LABELS.filter(([id]) => id in models).map(([id, label]) => (
            <ProfileRow
              key={id}
              label={t(label)}
              value={shortModelName(models[id])}
              title={models[id]}
            />
          ))}
        </div>
      )}
      {!expanded && (
        <div className="mt-2.5 space-y-1 text-[12px] italic text-ink-500">
          {paperProfile && (
            <div>
              {t("Paper Poster")}: {paperProfile.template} · {paperAuthorCmdMissing ? t("author command missing") : `${paperProfile.designer_author} ${t("author")}`} · revision {codeEditorCmdMissing ? t("code editor missing") : (codeEditorProfile?.harness ?? "codex")}
            </div>
          )}
          <div>
            {t("Runs take 30 s – 5 min · attach a PDF for paper-to-design workflows")}
          </div>
        </div>
      )}
    </div>
  );
}

function ProfileRow({
  label,
  value,
  title,
}: {
  label: string;
  value: string;
  title?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 px-3.5 py-2">
      <span className="eyebrow">{label}</span>
      <span className="tabular truncate font-mono text-[11px] text-ink-700" title={title ?? value}>
        {value}
      </span>
    </div>
  );
}

function shortModelName(id: string): string {
  const slash = id.lastIndexOf("/");
  return slash >= 0 ? id.slice(slash + 1) : id;
}

function shortCommandName(cmd: string): string {
  const normalized = cmd
    .replace("/Applications/ChatGPT.app/Contents/Resources/codex", "codex")
    .replace("/Applications/Codex.app/Contents/Resources/codex", "codex")
    .trim();
  return normalized.length > 58 ? `${normalized.slice(0, 55)}...` : normalized;
}


/** Tiny "context: N turns / M artifacts" chip at the top of the
 *  composer when there's prior conversation memory the agent will see.
 *  Hidden on the first turn (zero context to advertise). Hover surfaces
 *  the actual list as a tooltip so power users can verify what gets
 *  sent server-side. */
function MemoryHint({ compact }: { compact?: boolean }) {
  const conv = useCurrentConversation();
  const newConversation = useApp((s) => s.newConversation);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  if (!conv) return null;
  const userTurns = conv.messages.filter(
    (m) => m.role === "user" && !!m.text.trim(),
  ).length;
  const artifactCount = Object.keys(conv.artifacts).length;
  if (userTurns === 0 && artifactCount === 0) return null;

  // Tooltip-style detail — small list of artifact names so power users
  // can sanity-check what the designer will see.
  const artifactNames = Object.values(conv.artifacts)
    .slice(-3)
    .map((a) => `${a.artifact_type}: ${a.name}`)
    .join("\n");
  const title = artifactNames
    ? `Sending the last ${Math.min(userTurns, 6)} turn(s) and ${artifactCount} artifact(s) `
      + `as context to the agent. Use "New chat" to start fresh.\n\n${artifactNames}`
    : `Sending the last ${Math.min(userTurns, 6)} turn(s) as context to the agent.`;

  return (
    <div
      className={`flex items-center justify-between gap-3 border-b border-ink-300/50 bg-vellum/60 px-3 ${compact ? "py-1" : "py-1.5"}`}
      title={title}
    >
      <div
        className="flex min-w-0 items-center gap-1.5 text-[10.5px] uppercase text-ink-500"
        style={{ letterSpacing: "0.14em" }}
      >
        <I.SparkleQuiet width={11} height={11} className="text-accent" />
        <span className="tabular">
          {userTurns} {t(userTurns === 1 ? "turn" : "turns")}
          {artifactCount > 0 && (
            <> · {artifactCount} {t(artifactCount === 1 ? "artifact" : "artifacts")}</>
          )} {t("in context")}
        </span>
      </div>
      <button
        type="button"
        onClick={() => {
          if (window.confirm(t("Start a new chat? The agent will lose memory of this thread."))) {
            newConversation();
          }
        }}
        className="text-[10px] uppercase text-ink-500 transition hover:text-ink-900"
        style={{ letterSpacing: "0.14em" }}
        title={t("Start fresh — agent forgets prior turns")}
      >
        {t("New chat")} ↗
      </button>
    </div>
  );
}

/** Live progress card sourced from the backend's SSE event stream.
 *  See `ProgressCard.tsx` for the visual structure. */
function StreamingPlaceholder({ compact }: { compact?: boolean }) {
  return <ProgressCard compact={compact} />;
}

function MessageBubble({
  m,
  compact,
  onOpenCanvas,
}: {
  m: Message;
  compact?: boolean;
  onOpenCanvas: (id?: string) => void;
}) {
  const isUser = m.role === "user";
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  return (
    <div className={`flex animate-riseIn ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`max-w-[90%] ${compact ? "max-w-full" : ""}`}>
        {!isUser && !compact && (
          <div className="mb-2 flex items-center gap-2">
            <I.SparkleQuiet width={11} height={11} className="text-accent" />
            <span className="eyebrow">{t("Run output")}</span>
          </div>
        )}
        {m.attachments && m.attachments.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {m.attachments.map((a) => (
              <AttachmentChip key={a.id} a={a} />
            ))}
          </div>
        )}

        {m.status === "streaming" ? (
          <StreamingPlaceholder compact={compact} />
        ) : isUser ? (
          <div
            className={`inline-block rounded-md border border-ink-300/65 bg-surface-raised/88 px-4 py-2.5 text-[14px] text-ink-900 shadow-soft ${
              compact ? "px-3 py-2 text-[13px]" : ""
            }`}
          >
            <p className="whitespace-pre-wrap leading-relaxed">{m.text}</p>
            {m.selection_summary && (
              <SelectionSummaryInline summary={m.selection_summary} compact={compact} />
            )}
          </div>
        ) : (m.status === "error" || m.failure) && !m.artifact_id ? (
          <FailureCard m={m} compact={compact} />
        ) : (
          <div className={`run-output-panel ${compact ? "run-output-panel-compact" : ""}`}>
            {m.text && (
              <p
                className={`whitespace-pre-wrap leading-[1.55] text-ink-700 ${
                  compact ? "text-[12.5px]" : "text-[13.5px]"
                }`}
              >
                {m.text}
              </p>
            )}
            {m.download_url && (
              <a
                href={m.download_url}
                download={m.download_filename}
                className="mt-3 inline-flex items-center gap-1.5 rounded-sm border border-ink-300 bg-paper px-3 py-1.5 text-[11px] font-medium uppercase text-ink-700 transition hover:border-ink-500 hover:text-ink-900"
                style={{ letterSpacing: "0.14em" }}
              >
                <I.File width={12} height={12} />
                {m.download_filename || t("Download export")}
              </a>
            )}
            {m.artifact_id && (
              <ArtifactCard
                artifact_id={m.artifact_id}
                compact={compact}
                onOpen={() => onOpenCanvas(m.artifact_id)}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function ArtifactCard({
  artifact_id,
  compact,
  onOpen,
}: {
  artifact_id: string;
  compact?: boolean;
  onOpen: () => void;
}) {
  const art = useArtifactById(artifact_id);
  const conv = useCurrentConversation();
  const paperBundlePptxExportDisabled = useApp((s) => {
    const current = s.conversations[s.current_conversation_id];
    const parent = current?.paper_bundle?.kind === "parent"
      ? current
      : current?.paper_bundle?.kind === "child"
        ? s.conversations[current.paper_bundle.parent_conversation_id]
        : undefined;
    return parent?.paper_bundle?.kind === "parent"
      && paperBundleBlocksPptxExport(parent.paper_bundle);
  });
  const submitOpenResearchProject = useApp((s) => s.submitOpenResearchProject);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const [paperIdDraft, setPaperIdDraft] = useState("");
  if (!art) return null;
  const artType = artifactTypeForArtifact(art);
  const producingMessage = artifactMessage(conv, artifact_id);
  const validation = artifactValidationState(art, producingMessage);
  const previewAspect = artifactPreviewAspectRatio(art, artType);
  const isNative = !!art.native_file_url;
  const layerCount = Array.isArray(art.layers) ? art.layers.length : 0;
  const subline = isNative
    ? `${t(TEMPLATE_LABEL[artType])} · ${art.native_format?.toUpperCase()}`
    : `${t(TEMPLATE_LABEL[artType])} · ${layerCount} ${t("layers")}`;
  const research = art.openresearch;
  const showResearch = artType === "poster";
  const resultHref = openResearchResultHref(research);
  const reportLabel = t(openResearchStatusLabel(research));
  const researchMessage = openResearchStatusMessage(research);
  const needsPaperId = openResearchNeedsPaperId(research);
  const paperIdValue = paperIdDraft.trim();
  const submitPaperIdRetry = () => {
    if (!paperIdValue || research?.status === "running") return;
    void submitOpenResearchProject(
      artifact_id,
      openResearchSubmitOptionsFromPaperInput(paperIdValue),
    );
  };
  return (
    <div
      className={`mt-4 overflow-hidden rounded-md border border-ink-300/70 bg-paper shadow-page ${compact ? "mt-3" : ""}`}
    >
      <div
        className="relative w-full"
        style={{
          aspectRatio: String(previewAspect),
          background: art.canvas.background ?? "#fff",
        }}
      >
        <ArtifactThumbnail artifact_id={artifact_id} />
      </div>
      <div className="border-t border-ink-300/70 bg-surface-raised px-3.5 py-2.5">
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 flex-col leading-tight">
          <span
            className="truncate font-display text-[14px] text-ink-900"
            style={{ fontVariationSettings: '"opsz" 36' }}
          >
            {art.name}
          </span>
          <span
            className="tabular text-[10px] uppercase text-ink-500"
            style={{ letterSpacing: "0.14em" }}
          >
            {subline}
          </span>
          </div>
          <ValidationPill state={validation} />
        </div>
        <div className="mt-2.5 flex flex-wrap items-center justify-end gap-1.5">
          <ArtifactDownloadMenu
            artifact={art}
            compact
            pptxExportDisabled={paperBundlePptxExportDisabled}
          />
          {showResearch && resultHref && research?.status !== "running" && research?.status !== "error" ? (
            <a
              href={resultHref}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 rounded-sm border border-ink-300 bg-paper px-2.5 py-1.5 text-[10px] font-medium uppercase text-ink-700 transition hover:border-ink-500 hover:text-ink-900"
              style={{ letterSpacing: "0.14em" }}
              title={t("Open OpenResearch project")}
            >
              <I.Report width={11} height={11} />
              {reportLabel}
            </a>
          ) : showResearch ? (
            <button
              onClick={() => void submitOpenResearchProject(artifact_id)}
              disabled={research?.status === "running"}
              className="inline-flex items-center gap-1.5 rounded-sm border border-ink-300 bg-paper px-2.5 py-1.5 text-[10px] font-medium uppercase text-ink-700 transition hover:border-ink-500 hover:text-ink-900 disabled:cursor-wait disabled:opacity-60"
              style={{ letterSpacing: "0.14em" }}
              title={research?.error || research?.gui_submitter_error || t("Submit to OpenResearch")}
            >
              <I.Report width={11} height={11} />
              {reportLabel}
            </button>
          ) : null}
          <button
            onClick={onOpen}
            className="inline-flex items-center gap-1.5 rounded-sm bg-ink-900 px-3 py-1.5 text-[10px] font-medium uppercase text-ink-50 transition hover:bg-ink-700"
            style={{ letterSpacing: "0.16em" }}
          >
            <I.Edit width={11} height={11} />
            {t("Open canvas")}
          </button>
        </div>
        {showResearch && research?.status === "error" && (
          <div className="mt-2.5 rounded-sm border border-amber-700/25 bg-amber-50 px-3 py-2 text-[11px] leading-snug text-amber-900">
            <div className="flex items-start gap-2">
              <I.Alert width={13} height={13} className="mt-0.5 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="font-medium">{t("OpenResearch failed")}</div>
                {researchMessage && (
                  <div className="mt-0.5 break-words text-amber-900/85">{researchMessage}</div>
                )}
                {needsPaperId && (
                  <div className="mt-2 flex flex-wrap items-center gap-1.5">
                    <input
                      value={paperIdDraft}
                      onChange={(e) => setPaperIdDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          submitPaperIdRetry();
                        }
                      }}
                      placeholder={t("arXiv ID or URL")}
                      className="h-7 min-w-[180px] flex-1 rounded-sm border border-amber-700/30 bg-paper px-2 text-[11px] text-ink-900 outline-none focus:border-amber-700"
                    />
                    <button
                      onClick={submitPaperIdRetry}
                      disabled={!paperIdValue}
                      className="inline-flex h-7 items-center gap-1.5 rounded-sm border border-amber-700/35 bg-amber-100 px-2 text-[10px] font-medium uppercase text-amber-900 transition hover:bg-amber-200 disabled:cursor-not-allowed disabled:opacity-50"
                      style={{ letterSpacing: "0.14em" }}
                    >
                      <I.Refresh width={11} height={11} />
                      {t("Retry")}
                    </button>
                  </div>
                )}
                {research.result_url && (
                  <a
                    href={research.result_url}
                    target="_blank"
                    rel="noreferrer"
                    className="mt-1.5 inline-flex text-[10.5px] font-medium uppercase text-amber-900 underline underline-offset-2"
                    style={{ letterSpacing: "0.12em" }}
                  >
                    {t("Details")}
                  </a>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function ValidationPill({ state }: { state: ReturnType<typeof artifactValidationState> }) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const cls =
    state.tone === "ok"
      ? "border-accent/30 bg-accent-soft text-accent-deep"
      : state.tone === "warning"
        ? "border-amber-700/30 bg-amber-50 text-amber-900"
        : state.tone === "running"
          ? "border-accent/30 bg-paper text-accent-deep"
          : "border-ink-300 bg-paper text-ink-600";
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-sm border px-2 py-1 text-[10px] font-medium uppercase ${cls}`}
      style={{ letterSpacing: "0.12em" }}
      title={t(state.detail)}
    >
      {state.tone === "warning" ? <I.Alert width={10} height={10} /> : <I.Check width={10} height={10} />}
      {t(state.label)}
    </span>
  );
}

function ArtifactThumbnail({ artifact_id }: { artifact_id: string }) {
  const art = useArtifactById(artifact_id);
  if (!art) return null;

  // For mp4 artifacts we want the player's "click to play" affordance,
  // not a static preview. Show the video element directly with a
  // poster so the chat card looks like a thumbnail until clicked.
  if (art.native_format === "mp4" && art.native_file_url) {
    return (
      <video
        src={art.native_file_url}
        poster={art.preview_url}
        muted
        playsInline
        preload="metadata"
        controls
        className="absolute inset-0 h-full w-full bg-black object-contain"
      >
        {art.downloads?.vtt && (
          <track kind="subtitles" srcLang="en" label="English" src={art.downloads.vtt} />
        )}
      </video>
    );
  }

  // Prefer the PNG render (vision-critic preview) when the agent emitted
  // one — much cheaper than an iframe and looks identical. We use
  // <ResilientImg /> so a 404 (e.g. user wiped out/) shows a clear
  // "file no longer available" stub instead of a broken-image icon.
  if (art.preview_url) {
    return (
      <ResilientImg
        src={art.preview_url}
        alt={art.name}
        className="absolute inset-0 h-full w-full object-contain"
      />
    );
  }

  // Path 1 — native preview (SVG inline, HTML iframe, PPTX placeholder)
  if (art.native_file_url && art.native_format) {
    if (art.native_format === "svg") {
      return (
        <ResilientImg
          src={art.native_file_url}
          alt={art.name}
          className="absolute inset-0 h-full w-full object-contain"
        />
      );
    }
    if (art.native_format === "html") {
      return <HtmlArtifactThumbnail art={art} />;
    }
    if (art.native_format === "pptx") {
      return (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-ink-500">
          <I.Deck width={36} height={36} />
          <span className="text-[10px] font-medium uppercase tracking-wider">
            PPTX preview not available
          </span>
        </div>
      );
    }
  }

  // Path 2 — layer-based render (used by sample/mock data and future
  // SVG-to-Layer-parsed artifacts).
  return <LayerArtifactThumbnail art={art} />;
}

function LayerArtifactThumbnail({ art }: { art: Artifact }) {
  const boxRef = useRef<HTMLDivElement | null>(null);
  const [box, setBox] = useState({ w: 0, h: 0 });
  const layers = Array.isArray(art.layers) ? art.layers : [];
  const sourceW = Math.max(1, art.canvas.w);
  const sourceH = Math.max(1, art.canvas.h);
  const scale = box.w > 0 && box.h > 0
    ? Math.min(box.w / sourceW, box.h / sourceH)
    : 1;
  const left = box.w > 0 ? Math.max(0, (box.w - sourceW * scale) / 2) : 0;
  const top = box.h > 0 ? Math.max(0, (box.h - sourceH * scale) / 2) : 0;

  useLayoutEffect(() => {
    if (!boxRef.current) return;
    const el = boxRef.current;
    const read = () => setBox({ w: el.clientWidth, h: el.clientHeight });
    read();
    const obs = new ResizeObserver(read);
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  return (
    <div
      ref={boxRef}
      className="absolute inset-0 overflow-hidden"
      style={{ background: art.canvas.background ?? "#fff" }}
    >
      <div
        className="absolute origin-top-left"
        style={{
          left,
          top,
          width: sourceW,
          height: sourceH,
          transform: `scale(${scale})`,
          background: art.canvas.background,
        }}
      >
        {[...layers]
          .sort((a, b) => a.z_index - b.z_index)
          .map((l) =>
            l.visible === false ? null : (
              <div
                key={l.layer_id}
                style={{
                  position: "absolute",
                  left: l.bbox?.x ?? 0,
                  top: l.bbox?.y ?? 0,
                  width: l.bbox?.w ?? 0,
                  height: l.bbox?.h ?? 0,
                  background:
                    l.kind === "shape" || l.kind === "background"
                      ? l.fill_color
                      : undefined,
                  fontFamily: l.font_family,
                  fontSize: l.font_size_px,
                  fontWeight: l.font_weight,
                  lineHeight: l.line_height,
                  letterSpacing: l.letter_spacing
                    ? `${l.letter_spacing}px`
                    : undefined,
                  textAlign: l.align,
                  color: l.effects?.fill,
                  whiteSpace: "pre-wrap",
                }}
              >
                {l.kind === "text" ? l.text : null}
              </div>
            )
          )}
      </div>
    </div>
  );
}

function HtmlArtifactThumbnail({ art }: { art: Artifact }) {
  const boxRef = useRef<HTMLDivElement | null>(null);
  const [box, setBox] = useState({ w: 0, h: 0 });
  const sourceW = Math.max(1, art.canvas.w);
  const sourceH = Math.max(1, art.canvas.h);
  const scale = box.w > 0 && box.h > 0
    ? Math.min(box.w / sourceW, box.h / sourceH)
    : 1;
  const left = box.w > 0 ? Math.max(0, (box.w - sourceW * scale) / 2) : 0;
  const top = box.h > 0 ? Math.max(0, (box.h - sourceH * scale) / 2) : 0;

  useLayoutEffect(() => {
    if (!boxRef.current) return;
    const el = boxRef.current;
    const read = () => setBox({ w: el.clientWidth, h: el.clientHeight });
    read();
    const obs = new ResizeObserver(read);
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  return (
    <div
      ref={boxRef}
      className="absolute inset-0 overflow-hidden"
      style={{ background: art.canvas.background ?? "#fff" }}
    >
      <iframe
        src={art.native_file_url}
        title={art.name}
        sandbox=""
        className="pointer-events-none absolute border-0 bg-white"
        style={{
          width: sourceW,
          height: sourceH,
          left,
          top,
          transform: `scale(${scale})`,
          transformOrigin: "top left",
        }}
      />
    </div>
  );
}

/** <img> wrapper that swaps to a "file unavailable" stub on load
 *  failure. Persisted artifacts can outlive their on-disk run dirs
 *  (user wiped out/), so a hard 404 must not turn a chat thumbnail
 *  into a broken-image icon. */
function ResilientImg(props: {
  src: string;
  alt: string;
  className?: string;
}) {
  const [errored, setErrored] = useState(false);
  if (errored) {
    return (
      <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-ink-100 text-ink-500">
        <I.File width={28} height={28} />
        <span className="text-[10px] uppercase tracking-wider">
          file no longer available
        </span>
      </div>
    );
  }
  return (
    <img
      src={props.src}
      alt={props.alt}
      className={props.className}
      onError={() => setErrored(true)}
    />
  );
}

function AttachmentChip({ a }: { a: Attachment }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-sm border border-ink-300/70 bg-vellum px-2 py-1 text-[11px] text-ink-700">
      <I.File width={12} height={12} />
      {a.role === "style_reference" && (
        <span className="font-medium text-accent-deep">Style</span>
      )}
      <span className="max-w-[160px] truncate">{a.name}</span>
      <span className="tabular text-ink-500">{Math.ceil(a.size / 1024)} KB</span>
    </span>
  );
}

interface ComposerProps {
  input: string;
  setInput: (s: string) => void;
  attachments: Attachment[];
  removeAttachment: (id: string) => void;
  placeholder: string;
  onSend: () => void;
  onAttachClick: () => void;
  onAttachStyleReference: () => void;
  reference?: Attachment;
  referencePreview?: ReferencePosterPreview | null;
  referenceError?: string | null;
  hasPaperPdf: boolean;
  onRemoveReference: () => void;
  onAttachPaperPdf: () => void;
  disabled: boolean;
  locked: boolean;
  compact?: boolean;
  intent: ArtifactType | null;
  onSetIntent: (t: ArtifactType | null) => void;
  posterContext: boolean;
  palettes: PosterPalette[];
  paletteStatus: "idle" | "loading" | "ready" | "error";
  paletteError: string | null;
  selectedPaletteId: string | null;
  paletteOpenRequest: number;
  paletteInvalid: boolean;
  onSelectPalette: (paletteId: string) => void;
  onRetryPalettes: () => void;
  canvasPresets: PosterCanvasPreset[];
  canvasPresetStatus: "idle" | "loading" | "ready" | "error";
  canvasPresetError: string | null;
  selectedCanvasPresetId: string;
  canvasOpenRequest: number;
  canvasInvalid: boolean;
  canvasValidationError: string | null;
  onSelectCanvasPreset: (presetId: string) => void;
  onRetryCanvasPresets: () => void;
  onClearCanvasValidationError: () => void;
  authoringBudgets: AuthoringBudgets;
  onAuthoringBudgetsChange: (budgets: AuthoringBudgets) => void;
}

function Composer({
  input,
  setInput,
  attachments,
  removeAttachment,
  placeholder,
  onSend,
  onAttachClick,
  onAttachStyleReference,
  reference,
  referencePreview,
  referenceError,
  hasPaperPdf,
  onRemoveReference,
  onAttachPaperPdf,
  disabled,
  locked,
  compact,
  intent,
  onSetIntent,
  posterContext,
  palettes,
  paletteStatus,
  paletteError,
  selectedPaletteId,
  paletteOpenRequest,
  paletteInvalid,
  onSelectPalette,
  onRetryPalettes,
  canvasPresets,
  canvasPresetStatus,
  canvasPresetError,
  selectedCanvasPresetId,
  canvasOpenRequest,
  canvasInvalid,
  canvasValidationError,
  onSelectCanvasPreset,
  onRetryCanvasPresets,
  onClearCanvasValidationError,
  authoringBudgets,
  onAuthoringBudgetsChange,
}: ComposerProps) {
  const areaItems = useApp((s) => s.area_revision_items);
  const demoMode = useApp((s) => s.backend_info?.demo_mode);
  const removeAreaRevisionItem = useApp((s) => s.removeAreaRevisionItem);
  const clearAreaRevisionItems = useApp((s) => s.clearAreaRevisionItems);
  const focusAreaRevisionItem = useApp((s) => s.focusAreaRevisionItem);
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const effectivePlaceholder = areaItems.length
    ? t("Describe what to change in the selected areas...")
    : placeholder;
  const contentAttachments = partitionReferenceAttachments(attachments).content;

  return (
    <fieldset
      disabled={locked}
      aria-busy={locked}
      className={`group/composer relative overflow-visible rounded-md border border-ink-300/70 bg-surface-raised/92 shadow-raised transition focus-within:border-accent/70 ${
        compact ? "rounded-md" : ""
      }`}
    >
      <MemoryHint compact={compact} />
      {canvasValidationError && (
        <div
          role="alert"
          className="border-b border-red-200 bg-red-50 px-3 py-2 text-[11.5px] leading-4 text-red-800"
        >
          {canvasValidationError}
        </div>
      )}
      {areaItems.length > 0 && (
        <AreaSelectionTray
          items={areaItems}
          compact={compact}
          onFocus={focusAreaRevisionItem}
          onRemove={removeAreaRevisionItem}
          onClear={clearAreaRevisionItems}
        />
      )}
      {contentAttachments.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 border-b border-ink-300/50 bg-vellum/35 px-3 py-2">
          {contentAttachments.map((a) => (
            <span
              key={a.id}
              className="inline-flex items-center gap-1.5 rounded-sm border border-ink-300/70 bg-surface-raised px-2 py-1 text-[11px] text-ink-700"
            >
              <I.File width={12} height={12} />
              <span className="max-w-[140px] truncate">{a.name}</span>
              <button
                onClick={() => removeAttachment(a.id)}
                className="text-ink-500 hover:text-ink-900"
              >
                <I.X width={12} height={12} />
              </button>
            </span>
          ))}
        </div>
      )}
      <textarea
        rows={compact ? 2 : 3}
        value={input}
        onChange={(e) => {
          setInput(e.target.value);
          onClearCanvasValidationError();
        }}
        onKeyDown={(e) => {
          // Enter sends; Shift+Enter inserts newline.
          // Skip while an IME composition (CJK input) is active —
          // Enter then confirms the candidate, not "send".
          if (
            e.key === "Enter" &&
            !e.shiftKey &&
            !e.nativeEvent.isComposing &&
            e.keyCode !== 229
          ) {
            e.preventDefault();
            onSend();
          }
        }}
        placeholder={effectivePlaceholder}
        className={`block w-full resize-none bg-transparent text-ink-900 outline-none placeholder:text-ink-500 ${
          compact ? "px-4 py-3 text-[14px] leading-[1.55]" : "px-5 py-4 text-[15px] leading-[1.55]"
        }`}
      />
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-ink-300/50 bg-vellum/30 px-2.5 py-1.5 sm:flex-nowrap">
        <div className="flex min-w-0 flex-wrap items-center gap-1.5 sm:flex-nowrap">
          <button
            onClick={onAttachClick}
            className="icon-btn h-7 w-7"
            title={t("Attach a file (PDF / image / docx / pptx / md)")}
          >
            <I.Paperclip width={14} height={14} />
          </button>
          <span className="mx-0.5 h-3.5 w-px bg-ink-300/75" />
          <TemplateStatusPill
            intent={intent}
            onClear={() => onSetIntent(null)}
          />
          <AuthoringBudgetControl
            budgets={authoringBudgets}
            intent={intent}
            compact={compact}
            disabled={disabled || locked}
            demoMode={demoMode}
            onChange={onAuthoringBudgetsChange}
          />
          {posterContext && (
            <PalettePicker
              palettes={palettes}
              status={paletteStatus}
              error={paletteError}
              selectedId={selectedPaletteId}
              compact={compact}
              openRequest={paletteOpenRequest}
              invalid={paletteInvalid}
              onSelect={onSelectPalette}
              onRetry={onRetryPalettes}
            />
          )}
          {posterContext && (
            <CanvasPicker
              presets={canvasPresets}
              status={canvasPresetStatus}
              error={canvasPresetError}
              selectedId={selectedCanvasPresetId}
              compact={compact}
              openRequest={canvasOpenRequest}
              invalid={canvasInvalid}
              onSelect={onSelectCanvasPreset}
              onRetry={onRetryCanvasPresets}
            />
          )}
          {isReferenceStyleControlEligible(posterContext, demoMode) && (
            <ReferenceStyleControl
              compact={compact}
              reference={reference}
              preview={referencePreview}
              error={referenceError}
              hasPaperPdf={hasPaperPdf}
              onChoose={onAttachStyleReference}
              onRemove={onRemoveReference}
              onAttachPaperPdf={onAttachPaperPdf}
            />
          )}
        </div>
        <div className="flex items-center gap-3">
          {!compact && (
            <span className="hidden eyebrow sm:inline">{t("Enter to send")}</span>
          )}
          <button
            onClick={onSend}
            disabled={disabled}
            className="inline-flex h-8 w-8 items-center justify-center rounded-md bg-accent text-white shadow-soft transition hover:bg-accent-deep disabled:cursor-not-allowed disabled:opacity-40"
            title={t("Send")}
          >
            <span className="sr-only">{t("Send")}</span>
            <I.ArrowRight width={13} height={13} />
          </button>
        </div>
      </div>
    </fieldset>
  );
}

function AreaSelectionTray({
  items,
  compact,
  onFocus,
  onRemove,
  onClear,
}: {
  items: PosterAreaSelectionItem[];
  compact?: boolean;
  onFocus: (selection_id: string) => void;
  onRemove: (selection_id: string) => void;
  onClear: () => void;
}) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  return (
    <div className="border-b border-ink-300/50 bg-vellum/35 px-3 py-2">
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <span className="eyebrow">{t("Selected areas")}</span>
        <button
          type="button"
          className="text-[10px] uppercase text-ink-500 transition hover:text-ink-900"
          style={{ letterSpacing: "0.12em" }}
          onClick={onClear}
        >
          {t("Clear")}
        </button>
      </div>
      <div className={`flex gap-1.5 overflow-x-auto ${compact ? "pb-0.5" : "flex-wrap"}`}>
        {items.map((item, idx) => (
          <div
            key={item.selection_id}
            className={`${compact ? "w-[250px]" : "w-[280px]"} shrink-0 overflow-hidden rounded-sm border border-ink-300/75 bg-surface-raised text-left text-[11px] text-ink-700 shadow-soft transition hover:border-accent/60 hover:text-ink-900`}
          >
            <div className="flex items-center border-b border-ink-300/45">
              <button
                type="button"
                className="inline-flex min-w-0 flex-1 items-center text-left"
                onClick={() => onFocus(item.selection_id)}
                title={item.label}
              >
                <span className={`flex h-7 min-w-7 items-center justify-center border-r border-ink-300/55 font-semibold ${
                  item.kind === "drawing" ? "bg-red-50 text-red-700" : "bg-sky-50 text-sky-700"
                }`}>
                  {idx + 1}
                </span>
                <span className="min-w-0 px-2 py-1">
                  <span className="block truncate font-medium">{areaChipLabel(item)}</span>
                  <span className="block text-[9.5px] uppercase text-ink-500" style={{ letterSpacing: "0.08em" }}>
                    {t(areaKindLabel(item.kind))}
                  </span>
                </span>
              </button>
              <button
                type="button"
                className="flex h-7 w-7 shrink-0 items-center justify-center text-ink-400 transition hover:text-ink-900"
                onClick={(event) => {
                  event.stopPropagation();
                  onRemove(item.selection_id);
                }}
                aria-label={translate(language, "Remove {label}", { label: item.label })}
              >
                <I.X width={11} height={11} />
              </button>
            </div>
            {item.instruction?.trim() && (
              <div className="line-clamp-2 bg-white/70 px-2.5 py-2 text-[11.5px] leading-4 text-ink-700">
                {item.instruction.trim()}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function SelectionSummaryInline({
  summary,
  compact,
}: {
  summary: PosterSelectionSummary;
  compact?: boolean;
}) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const labels = summary.labels.slice(0, 4);
  const remainder = Math.max(0, summary.count - labels.length);
  return (
    <div className={`mt-2 rounded-sm border border-accent/25 bg-vellum/45 px-2 py-1.5 text-left ${
      compact ? "text-[11px]" : "text-[11.5px]"
    }`}>
      <div className="mb-1 text-[9.5px] uppercase text-ink-500" style={{ letterSpacing: "0.12em" }}>
        {t("Edited selected areas")}
      </div>
      <div className="flex flex-wrap gap-1">
        {labels.map((label, idx) => (
          <span
            key={`${label}-${idx}`}
            className="inline-flex max-w-[180px] items-center gap-1 rounded-sm border border-ink-300/60 bg-surface-raised px-1.5 py-0.5 text-ink-700"
          >
            <span className="font-semibold">{idx + 1}</span>
            <span className="truncate">{label}</span>
          </span>
        ))}
        {remainder > 0 && (
          <span className="inline-flex items-center rounded-sm border border-ink-300/60 bg-surface-raised px-1.5 py-0.5 text-ink-500">
            +{remainder}
          </span>
        )}
      </div>
      {summary.area_instructions && summary.area_instructions.length > 0 && (
        <div className="mt-1.5 space-y-1">
          {summary.area_instructions.slice(0, 4).map((item, idx) => (
            <div
              key={`${item.label}-${idx}-note`}
              className="rounded-sm bg-white/55 px-1.5 py-1 text-ink-700"
            >
              <span className="font-semibold">{item.index ?? idx + 1}. {item.label}: </span>
              <span>{item.instruction}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function areaChipLabel(item: PosterAreaSelectionItem): string {
  const label = item.label?.trim();
  if (label) return label;
  if (item.kind === "drawing") return "Drawn markup";
  if (item.kind === "region") return "Selected region";
  return "Selected section";
}

function areaKindLabel(kind: PosterAreaSelectionItem["kind"]): string {
  if (kind === "drawing") return "Draw";
  if (kind === "region") return "Region";
  return "Section";
}

function selectedAreaInstructionBrief(items: PosterAreaSelectionItem[]): string {
  const notes = items
    .slice(0, 6)
    .map((item, idx) => ({ item, idx }))
    .filter(({ item }) => item.instruction?.trim())
    .map(({ item, idx }) => `${idx + 1}. ${areaChipLabel(item)}: ${item.instruction!.trim()}`);
  return notes.length
    ? `Apply these selected-area edits:\n${notes.join("\n")}`
    : "";
}

function TemplateStatusPill({
  intent,
  onClear,
}: {
  intent: ArtifactType | null;
  onClear: () => void;
}) {
  const language = useApp((s) => s.ui_language);
  const t = (text: string) => translate(language, text);
  const card = intent ? QUICK_ACTIONS.find((item) => item.type === intent) : null;
  const Icon = card?.icon ?? I.Layout;
  const value = intent ? TEMPLATE_LABEL[intent] : "None";
  return (
    <button
      type="button"
      onClick={intent ? onClear : undefined}
      title={intent ? t("Clear selected template") : t("Select a template below")}
      className={`inline-flex h-8 items-center overflow-hidden rounded-md border text-left transition ${
        intent
          ? "border-accent/70 bg-surface-raised text-ink-900 ring-2 ring-accent/65 ring-offset-2 ring-offset-vellum"
          : "border-ink-300/70 bg-paper/65 text-ink-500"
      }`}
    >
      <span className="flex h-full w-10 items-center justify-center border-r border-ink-300/55 bg-vellum/80">
        <Icon width={14} height={14} />
      </span>
      <span className="flex min-w-[112px] flex-col justify-center px-2.5 leading-none">
        <span className="text-[9.5px] text-ink-500">{t("Template")}</span>
        <span className="mt-0.5 truncate text-[12px] font-semibold text-ink-900">
          {t(value)}
        </span>
      </span>
      {intent && (
        <span className="pr-2 text-ink-400">
          <I.X width={11} height={11} />
        </span>
      )}
    </button>
  );
}
