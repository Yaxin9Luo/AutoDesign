import { createRoot } from "react-dom/client";

import "@/index.css";
import { Canvas } from "@/components/Canvas";
import { stripDeckNavigationState } from "@/lib/deck_navigation";
import { useApp } from "@/lib/store";
import type { Artifact, Conversation, Layer } from "@/lib/types";

type DeckVariant =
  | "stacked"
  | "active"
  | "is-active"
  | "current"
  | "aria-hidden"
  | "data-active"
  | "display"
  | "visibility"
  | "opacity"
  | "zero-size"
  | "transform-player"
  | "styled-display-player"
  | "centered-script-player"
  | "mismatched-centered-script-player"
  | "css-centered-player"
  | "active-stacked"
  | "absolute-stacked"
  | "nested-absolute-stacked"
  | "legacy-class-root"
  | "legacy-slide-class-root"
  | "real-main-18"
  | "generated-12";

const conversationId = "canvas-deck-navigation";
const artifactId = "canvas-deck-navigation-artifact";
const blobUrls: string[] = [];
let currentVariant: DeckVariant = "stacked";
let currentHtml = "";
const documentIds = new WeakMap<Document, number>();
let nextDocumentId = 1;

const markerFor = (variant: DeckVariant, index: number): string => {
  const active = index === 0;
  if (variant === "active") return active ? " active" : "";
  if (variant === "is-active" || ["visibility", "opacity", "zero-size"].includes(variant)) {
    return active ? " is-active" : "";
  }
  if (variant === "current") return active ? " current" : "";
  return "";
};

const attributesFor = (variant: DeckVariant, index: number): string => {
  const active = index === 0;
  if (variant === "active" && active) {
    return ' data-frame-id="frame-a" aria-current="__autodesign_missing__"';
  }
  if (variant === "aria-hidden") return ` aria-hidden="${active ? "false" : "true"}"`;
  if (variant === "data-active") return ` data-active="${active ? "true" : "false"}"`;
  if (index === 0) return ' data-frame-id="frame-a"';
  if (index === 2) return ' data-slide-index="3"';
  return "";
};

const playerCss = (variant: DeckVariant): string => {
  if (["active", "current", "aria-hidden", "data-active", "is-active", "generated-12", "real-main-18"].includes(variant)) {
    const activeSelector = variant === "active"
      ? ".deck-slide.active"
      : variant === "current"
        ? ".deck-slide.current"
        : variant === "aria-hidden"
          ? '.deck-slide[aria-hidden="false"]'
          : variant === "data-active"
            ? '.deck-slide[data-active="true"]'
            : ".deck-slide.is-active";
    return `.deck-slide{display:none}${activeSelector}{display:block}`;
  }
  if (variant === "display") {
    return ".deck-slide{display:none}.deck-slide:first-child{display:block}";
  }
  if (variant === "visibility") {
    return ".deck-slide{visibility:hidden}.deck-slide.is-active{visibility:visible}";
  }
  if (variant === "opacity") {
    return ".deck-slide{opacity:0}.deck-slide.is-active{opacity:1}";
  }
  if (variant === "zero-size") {
    return ".deck-slide{width:0;height:0;padding:0;border:0;overflow:hidden}.deck-slide.is-active{width:1280px;height:720px;padding:48px;border:4px solid rgb(20,90,160)}";
  }
  if (variant === "transform-player") {
    return ".od-deck{position:relative;height:720px}.deck-slide{position:absolute;inset:0;transform:translateX(-300vw);pointer-events:none}.deck-slide.is-active{transform:translateX(0);pointer-events:auto}";
  }
  if (variant === "styled-display-player") {
    return ".deck-slide{display:none}.deck-slide.is-active{display:block;transform:scale(.8) rotate(1deg);transform-origin:top left;pointer-events:none}";
  }
  if (["centered-script-player", "mismatched-centered-script-player"].includes(variant)) {
    return ".deck-slide{position:absolute;top:50%;left:50%;display:none}.deck-slide.is-active{display:block}";
  }
  if (variant === "css-centered-player") {
    return ".deck-slide{position:absolute;top:50%;left:50%;display:none;transform:translate(-50%,-50%)}.deck-slide.is-active{display:block}";
  }
  if (variant === "absolute-stacked") {
    return ".od-deck{position:relative;height:2160px}.deck-slide{position:absolute;left:0}.deck-slide:nth-child(1){top:0}.deck-slide:nth-child(2){top:720px}.deck-slide:nth-child(3){top:1440px}";
  }
  if (variant === "nested-absolute-stacked") {
    return ".stack-wrapper{position:relative;height:720px}.deck-slide{position:absolute;inset:0}";
  }
  return "";
};

function deckHtml(variant: DeckVariant): string {
  const count = variant === "real-main-18"
    ? 18
    : variant === "generated-12" || variant === "transform-player"
      ? 12
      : 3;
  const largeDeck = ["generated-12", "real-main-18", "legacy-class-root", "legacy-slide-class-root", "centered-script-player", "mismatched-centered-script-player", "css-centered-player"].includes(variant);
  const width = largeDeck ? 1920 : 1280;
  const height = largeDeck ? 1080 : 720;
  const slideMarkup = Array.from({ length: count }, (_, index) => {
    const id = index === 1 ? "authored-b" : index === 2 ? "" : `slide-${index + 1}`;
    const authoredActiveIndex = variant === "transform-player" ? 1 : 0;
    const marker = variant === "generated-12"
      ? (index === 0 ? " is-active" : "")
      : [
        "transform-player",
        "styled-display-player",
        "centered-script-player",
        "mismatched-centered-script-player",
        "css-centered-player",
        "active-stacked",
      ].includes(variant)
        ? (index === authoredActiveIndex ? " is-active" : "")
        : markerFor(variant, index);
    const content = variant === "real-main-18"
      ? `<h1 data-autodesign-editable="true" data-layer-id="title-${index + 1}" data-kind="text">SLIDE ${index + 1}</h1><p data-autodesign-editable="true" data-layer-id="body-${index + 1}" data-kind="text">Authored deck content ${index + 1}</p><img data-autodesign-editable="true" data-layer-id="image-${index + 1}" data-kind="image" alt="Figure ${index + 1}" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='320' height='180'%3E%3Crect width='320' height='180' fill='%230ea5e9'/%3E%3C/svg%3E">`
      : `<h1>SLIDE ${index + 1}</h1><p>Authored deck content ${index + 1}</p>`;
    const slideClass = variant === "legacy-slide-class-root" ? "slide" : "deck-slide";
    const slide = `<section id="${id}" class="${slideClass}${marker}"${attributesFor(variant, index)} style="--author-token:slide-${index + 1}">${content}</section>`;
    return variant === "nested-absolute-stacked"
      ? `<div class="stack-wrapper">${slide}</div>`
      : slide;
  }).join("");
  const slides = variant === "generated-12"
    ? `<div id="deck-shell">${slideMarkup}</div>`
    : slideMarkup;
  const root = variant === "real-main-18"
    ? `<main id="deck" data-slide-count="18" data-autodesign-artifact-root="deck">${slides}</main>`
    : variant === "legacy-class-root" || variant === "legacy-slide-class-root"
      ? `<main id="${variant === "legacy-slide-class-root" ? "stage" : ""}" class="od-deck">${slides}${variant === "legacy-slide-class-root" ? '<section class="speaker-notes">Speaker notes</section>' : ""}</main>`
      : `<main class="od-deck" data-autodesign-artifact-root>${slides}</main>`;
  return `<!doctype html><html><head><style>
    html,body{margin:0;padding:0;width:100%;height:100%;background:white}
    .od-deck{width:${width}px${variant === "legacy-class-root" || variant === "legacy-slide-class-root" ? ";margin:96px;transform:translate(240px,120px)" : ""}}
    #deck{width:${width}px;margin:96px;transform:translateX(240px)}
    #deck-shell{position:fixed;left:50%;top:50%;width:1920px;height:1080px;transform:translate(-50%,-50%)}
    .deck-slide,.slide{box-sizing:border-box;width:${width}px;height:${height}px;padding:48px;background:white;border:4px solid rgb(20,90,160)}
    .deck-slide img,.slide img{display:block;width:320px;height:180px}
    ${variant === "legacy-slide-class-root" ? ".slide{margin:72px;box-shadow:32px 24px 18px rgba(0,0,0,.3)}.speaker-notes{width:320px;height:80px}" : ""}
    ${playerCss(variant)}
  </style></head><body>${root}
  <script>
    window.__authorScriptRan = true;
    const slides = [...document.querySelectorAll('.deck-slide,.slide')];
    const activate = (index) => slides.forEach((slide, i) => {
      slide.classList.toggle('is-active', i === index);
      slide.setAttribute('aria-hidden', String(i !== index));
    });
    activate(0);
    ${["centered-script-player", "mismatched-centered-script-player"].includes(variant) ? `
    const fitActive = () => {
      const active = slides.find((slide) => slide.classList.contains('is-active'));
      if (!active) return;
      const scale = Math.min(innerWidth / 1920, innerHeight / 1080);
      active.style.transform = 'translate(-50%, -50%) scale(' + scale + ')';
    };
    fitActive();
    addEventListener('resize', fitActive);
    ` : ""}
  </script></body></html>`;
}

function realDeckLayers(): Layer[] {
  return Array.from({ length: 18 }, (_, index) => {
    const number = index + 1;
    return [
      {
        layer_id: `title-${number}`,
        name: `Slide ${number} title`,
        kind: "text" as const,
        z_index: number * 3,
        bbox: { x: 48, y: 48, w: 800, h: 80 },
        text: `SLIDE ${number}`,
      },
      {
        layer_id: `body-${number}`,
        name: `Slide ${number} body`,
        kind: "text" as const,
        z_index: number * 3 + 1,
        bbox: { x: 48, y: 160, w: 800, h: 80 },
        text: `Authored deck content ${number}`,
      },
      {
        layer_id: `image-${number}`,
        name: `Figure ${number}`,
        kind: "image" as const,
        z_index: number * 3 + 2,
        bbox: { x: 48, y: 280, w: 320, h: 180 },
        src: "figure.svg",
      },
    ];
  }).flat();
}

function artifactWithUrl(url: string): Artifact {
  const largeDeck = [
    "generated-12",
    "real-main-18",
    "centered-script-player",
    "css-centered-player",
  ].includes(currentVariant);
  return {
    artifact_id: artifactId,
    name: "Deck navigation regression",
    artifact_type: "deck",
    canvas: largeDeck ? { w: 1920, h: 1080 } : { w: 1280, h: 720 },
    layers: currentVariant === "real-main-18" ? realDeckLayers() : [],
    native_file_url: url,
    native_format: "html",
    view_format: "html",
  };
}

const initialArtifact = artifactWithUrl("about:blank");
const conversation: Conversation = {
  id: conversationId,
  title: "Canvas deck navigation regression",
  created_at: 1,
  updated_at: 1,
  messages: [],
  artifacts: { [artifactId]: initialArtifact },
  active_artifact_id: artifactId,
  pending: false,
};

useApp.setState({
  mode: "canvas",
  ui_language: "en",
  current_conversation_id: conversationId,
  conversations: { [conversationId]: conversation },
  grid_visible: false,
  rulers_visible: false,
  safe_margins_visible: false,
  properties_sidebar_open: false,
});

const rootElement = document.getElementById("root");
if (!rootElement) throw new Error("Canvas deck navigation fixture root is missing.");
const thumbnailInsertionLog: Array<{ ready: string | null; visibility: string }> = [];
const thumbnailObserver = new MutationObserver((records) => {
  for (const record of records) {
    for (const node of record.addedNodes) {
      if (!(node instanceof Element)) continue;
      const iframes = node.matches("iframe")
        ? [node as HTMLIFrameElement]
        : Array.from(node.querySelectorAll<HTMLIFrameElement>("iframe"));
      for (const iframe of iframes) {
        const wrapper = iframe.closest<HTMLElement>("[data-autodesign-thumbnail-ready]");
        const button = iframe.closest<HTMLButtonElement>('button[title^="Slide "]');
        if (!wrapper || !button) continue;
        thumbnailInsertionLog.push({
          ready: wrapper.dataset.autodesignThumbnailReady ?? null,
          visibility: getComputedStyle(wrapper).visibility,
        });
      }
    }
  }
});
thumbnailObserver.observe(rootElement, { childList: true, subtree: true });
const root = createRoot(rootElement);
root.render(<Canvas />);

function installDeck(variant: DeckVariant, hash = ""): string {
  currentVariant = variant;
  currentHtml = deckHtml(variant);
  const url = URL.createObjectURL(new Blob([currentHtml], { type: "text/html" }));
  blobUrls.push(url);
  const artifact = artifactWithUrl(`${url}${hash ? `#${encodeURIComponent(hash)}` : ""}`);
  const prior = useApp.getState().conversations[conversationId];
  useApp.setState({
    mode: "canvas",
    current_conversation_id: conversationId,
    conversations: {
      ...useApp.getState().conversations,
      [conversationId]: {
        ...conversation,
        ...prior,
        updated_at: Date.now(),
        artifacts: { [artifactId]: artifact },
        active_artifact_id: artifactId,
      },
    },
  });
  return artifact.native_file_url ?? url;
}

function mainIframe(): HTMLIFrameElement | null {
  return document.querySelector<HTMLIFrameElement>(
    'iframe[title="Deck navigation regression"]',
  );
}

function slideElements(doc: Document | null | undefined): HTMLElement[] {
  return Array.from(doc?.querySelectorAll<HTMLElement>(".deck-slide,.slide") ?? []);
}

function isPainted(slide: HTMLElement): boolean {
  const style = getComputedStyle(slide);
  const rect = slide.getBoundingClientRect();
  return style.display !== "none"
    && style.visibility !== "hidden"
    && Number(style.opacity || "1") > 0.01
    && rect.width > 1
    && rect.height > 1;
}

function documentId(doc: Document | null | undefined): number | null {
  if (!doc) return null;
  const existing = documentIds.get(doc);
  if (existing) return existing;
  const assigned = nextDocumentId++;
  documentIds.set(doc, assigned);
  return assigned;
}

function snapshot() {
  const iframe = mainIframe();
  const doc = iframe?.contentDocument;
  const slides = slideElements(doc);
  const state = useApp.getState();
  const currentConversation = state.conversations[state.current_conversation_id];
  const activeArtifact = currentConversation?.active_artifact_id
    ? currentConversation.artifacts[currentConversation.active_artifact_id]
    : undefined;
  const thumbIframes = Array.from(
    document.querySelectorAll<HTMLIFrameElement>('button[title^="Slide "] iframe'),
  );
  const activeMainSlide = slides.find(isPainted);
  const mainRect = activeMainSlide?.getBoundingClientRect();
  const helperRect = doc?.querySelector<HTMLElement>(".speaker-notes")?.getBoundingClientRect();
  return {
    variant: currentVariant,
    storeMode: state.mode,
    storeConversationId: state.current_conversation_id,
    storeArtifactUrl: activeArtifact?.native_file_url ?? null,
    selectedLayerIds: [...state.selected_layer_ids],
    pendingLayerEdits: activeArtifact
      ? currentConversation?.pending_edits?.[activeArtifact.artifact_id]?.layers ?? {}
      : {},
    iframeHref: iframe?.contentWindow?.location.href ?? null,
    documentId: documentId(doc),
    slideCount: slides.length,
    mode: doc?.body?.dataset.autodesignNavMode ?? null,
    hash: iframe?.contentWindow?.location.hash ?? "",
    scrollY: iframe?.contentWindow?.scrollY ?? -1,
    authorScriptRan: Boolean(
      (iframe?.contentWindow as (Window & { __authorScriptRan?: boolean }) | null)?.__authorScriptRan,
    ),
    slides: slides.map((slide) => ({
      id: slide.id,
      text: slide.querySelector("h1")?.textContent ?? "",
      painted: isPainted(slide),
      className: slide.className,
      style: slide.getAttribute("style"),
      ariaCurrent: slide.getAttribute("aria-current"),
      ariaHidden: slide.getAttribute("aria-hidden"),
      transform: getComputedStyle(slide).transform,
      transformOrigin: getComputedStyle(slide).transformOrigin,
      pointerEvents: getComputedStyle(slide).pointerEvents,
    })),
    thumbnailPaintedIndexes: thumbIframes.map((thumb) => (
      slideElements(thumb.contentDocument).findIndex(isPainted)
    )),
    thumbnailReady: thumbIframes.map((thumb) => (
      thumb.closest<HTMLElement>("[data-autodesign-thumbnail-ready]")
        ?.dataset.autodesignThumbnailReady ?? null
    )),
    thumbnailSandboxes: thumbIframes.map((thumb) => thumb.getAttribute("sandbox")),
    thumbnailDocumentIds: thumbIframes.map((thumb) => documentId(thumb.contentDocument)),
    thumbnailScrollY: thumbIframes.map((thumb) => thumb.contentWindow?.scrollY ?? -1),
    mainGeometry: {
      viewportWidth: doc?.documentElement?.clientWidth ?? 0,
      viewportHeight: doc?.documentElement?.clientHeight ?? 0,
      slideLeft: mainRect?.left ?? null,
      slideTop: mainRect?.top ?? null,
      slideWidth: mainRect?.width ?? null,
      slideHeight: mainRect?.height ?? null,
    },
    mainHelperGeometry: helperRect ? {
      width: helperRect.width,
      height: helperRect.height,
    } : null,
    thumbnailGeometry: thumbIframes.map((thumb) => {
      const doc = thumb.contentDocument;
      const thumbSlides = slideElements(doc);
      const activeSlide = thumbSlides.find(
        (slide) => slide.dataset.autodesignNavActive === "true",
      ) ?? thumbSlides.find(isPainted);
      const rect = activeSlide?.getBoundingClientRect();
      return {
        viewportWidth: doc?.documentElement?.clientWidth ?? 0,
        viewportHeight: doc?.documentElement?.clientHeight ?? 0,
        slideLeft: rect?.left ?? null,
        slideTop: rect?.top ?? null,
        slideWidth: rect?.width ?? null,
        slideHeight: rect?.height ?? null,
      };
    }),
    thumbnailHelperGeometry: thumbIframes.map((thumb) => {
      const rect = thumb.contentDocument
        ?.querySelector<HTMLElement>(".speaker-notes")
        ?.getBoundingClientRect();
      return rect ? { width: rect.width, height: rect.height } : null;
    }),
    thumbnailPressed: Array.from(
      document.querySelectorAll<HTMLButtonElement>('button[title^="Slide "]'),
    ).map((button) => button.getAttribute("aria-pressed")),
    progress: document.querySelector("output")?.textContent?.trim() ?? null,
  };
}

function selectEditable(slideIndex: number, kind: "text" | "image"): string {
  const doc = mainIframe()?.contentDocument;
  const slide = slideElements(doc)[slideIndex];
  const target = slide?.querySelector<HTMLElement>(`[data-kind="${kind}"]`);
  if (!target) throw new Error(`${kind} layer on slide ${slideIndex + 1} is missing.`);
  target.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, button: 0 }));
  return target.dataset.layerId ?? "";
}

function editSlideTitle(slideIndex: number, text: string): string {
  const doc = mainIframe()?.contentDocument;
  const slide = slideElements(doc)[slideIndex];
  const target = slide?.querySelector<HTMLElement>('[data-kind="text"]');
  if (!target) throw new Error(`Text layer on slide ${slideIndex + 1} is missing.`);
  target.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, button: 0 }));
  target.textContent = text;
  target.dispatchEvent(new InputEvent("input", { bubbles: true, data: text }));
  return target.dataset.layerId ?? "";
}

function clickThumbnail(index: number): void {
  const button = document.querySelector<HTMLButtonElement>(
    `button[title="Slide ${index + 1}"]`,
  );
  if (!button) throw new Error(`Slide ${index + 1} thumbnail is missing.`);
  button.click();
}

function pressKey(key: string): void {
  window.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
}

function remountCurrentSource(): string {
  const url = URL.createObjectURL(new Blob([currentHtml], { type: "text/html" }));
  blobUrls.push(url);
  const prior = useApp.getState().conversations[conversationId];
  useApp.setState({
    mode: "canvas",
    current_conversation_id: conversationId,
    conversations: {
      ...useApp.getState().conversations,
      [conversationId]: {
        ...conversation,
        ...prior,
        updated_at: Date.now(),
        artifacts: { [artifactId]: artifactWithUrl(url) },
        active_artifact_id: artifactId,
      },
    },
  });
  return url;
}

function reloadMain(): void {
  mainIframe()?.contentWindow?.location.reload();
}

function scrollMain(top: number): void {
  const win = mainIframe()?.contentWindow;
  win?.scrollTo({ top, left: 0 });
  win?.dispatchEvent(new Event("scroll"));
}

function cleanedCloneSnapshot() {
  const rootNode = mainIframe()?.contentDocument?.querySelector<HTMLElement>(
    "[data-autodesign-artifact-root]",
  );
  if (!rootNode) throw new Error("Deck root is missing.");
  const clone = rootNode.cloneNode(true) as HTMLElement;
  stripDeckNavigationState(clone);
  const slides = Array.from(clone.querySelectorAll<HTMLElement>(".deck-slide"));
  return {
    hasNavigationMarker: clone.outerHTML.includes("data-autodesign-nav-"),
    classes: slides.map((slide) => slide.className),
    styles: slides.map((slide) => slide.getAttribute("style")),
    ariaHidden: slides.map((slide) => slide.getAttribute("aria-hidden")),
    ariaCurrent: slides.map((slide) => slide.getAttribute("aria-current")),
    dataActive: slides.map((slide) => slide.getAttribute("data-active")),
  };
}

function cleanedDocumentSnapshot() {
  const source = mainIframe()?.contentDocument;
  if (!source) throw new Error("Deck document is missing.");
  const copy = new DOMParser().parseFromString(
    source.documentElement.outerHTML,
    "text/html",
  );
  stripDeckNavigationState(copy.documentElement);
  return {
    hasNavigationMarker: copy.documentElement.outerHTML.includes("data-autodesign-nav-"),
    hasNavigationStyle: Boolean(
      copy.getElementById("autodesign-trusted-deck-navigation"),
    ),
    bodyCurrentSlide: copy.body.getAttribute("data-current-slide"),
  };
}

installDeck("stacked", "authored-b");

window.__canvasDeckNavigationHarness = {
  load: installDeck,
  snapshot,
  clickThumbnail,
  selectEditable,
  editSlideTitle,
  pressKey,
  remountCurrentSource,
  reloadMain,
  scrollMain,
  cleanedCloneSnapshot,
  cleanedDocumentSnapshot,
  resetThumbnailInsertionLog: () => { thumbnailInsertionLog.length = 0; },
  thumbnailInsertionLog: () => [...thumbnailInsertionLog],
  unmount: () => {
    thumbnailObserver.disconnect();
    root.unmount();
    blobUrls.forEach((url) => URL.revokeObjectURL(url));
  },
};

declare global {
  interface Window {
    __canvasDeckNavigationHarness?: {
      load: (variant: DeckVariant, hash?: string) => string;
      snapshot: () => ReturnType<typeof snapshot>;
      clickThumbnail: (index: number) => void;
      selectEditable: (slideIndex: number, kind: "text" | "image") => string;
      editSlideTitle: (slideIndex: number, text: string) => string;
      pressKey: (key: string) => void;
      remountCurrentSource: () => string;
      reloadMain: () => void;
      scrollMain: (top: number) => void;
      cleanedCloneSnapshot: () => ReturnType<typeof cleanedCloneSnapshot>;
      cleanedDocumentSnapshot: () => ReturnType<typeof cleanedDocumentSnapshot>;
      resetThumbnailInsertionLog: () => void;
      thumbnailInsertionLog: () => Array<{ ready: string | null; visibility: string }>;
      unmount: () => void;
    };
  }
}
