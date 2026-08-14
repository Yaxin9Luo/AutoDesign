const BACK_KEYS = new Set(["ArrowLeft", "ArrowUp", "PageUp"]);
const FORWARD_KEYS = new Set(["ArrowRight", "ArrowDown", "PageDown"]);

export type DeckPresentationMode = "stacked" | "player";

export interface DeckSlideIdentitySource {
  dataFrameId?: string | null;
  id?: string | null;
  dataSlideIndex?: string | null;
}

export interface DeckSlidePaintState {
  display: string;
  visibility: string;
  opacity: number;
  width: number;
  height: number;
  position?: string;
  offsetTop?: number;
  offsetLeft?: number;
  offsetParent?: object | null;
  containingBlockWidth?: number;
  containingBlockHeight?: number;
  transform?: string;
  transformOrigin?: string;
  pointerEvents?: string;
  intersectsViewport?: boolean;
}

export interface DeckDomScan {
  slides: HTMLElement[];
  frameIds: string[];
  mode: DeckPresentationMode;
  playerDisplay: string;
  playerWidth: number;
  playerHeight: number;
  playerTransform: string;
  playerTransformOrigin: string;
  playerPointerEvents: string;
  playerOriginNeedsReset: boolean;
  authoredDataActive: boolean;
}

export const DECK_SLIDE_SELECTORS = [
  "[data-slide]",
  "section.slide",
  ".deck-slide",
  ".slide",
  "body > section",
  "body > article",
  "main > section",
] as const;

const NAV_MODE_ATTR = "data-autodesign-nav-mode";
const NAV_SLIDE_ATTR = "data-autodesign-nav-slide";
const NAV_ACTIVE_ATTR = "data-autodesign-nav-active";
const NAV_FRAME_ATTR = "data-autodesign-nav-frame-id";
const NAV_STYLE_ID = "autodesign-trusted-deck-navigation";
const ORIGINAL_ATTRIBUTES = {
  "aria-current": "data-autodesign-nav-original-aria-current",
  "aria-hidden": "data-autodesign-nav-original-aria-hidden",
  "data-active": "data-autodesign-nav-original-data-active",
  "data-current-slide": "data-autodesign-nav-original-current-slide",
} as const;
const CANVAS_NORMALIZATION_STYLE_IDS = [
  "autodesign-web-poster-editor-frame",
  "designanything-web-poster-editor-frame",
];
export const DECK_RUNTIME_ROOT_ATTR = "data-autodesign-editor-deck-root";
export const DECK_RUNTIME_SLIDE_ATTR = "data-autodesign-editor-deck-slide";
const DECK_ARTIFACT_ROOT_SELECTORS = [
  "[data-autodesign-artifact-root]",
  "main#deck",
  "main[data-slide-count]",
] as const;

export function findDeckArtifactRoot(doc: Document): HTMLElement | null {
  for (const selector of DECK_ARTIFACT_ROOT_SELECTORS) {
    const root = doc.querySelector<HTMLElement>(selector);
    if (root) return root;
  }
  const slides = findDeckSlides(doc);
  if (!slides.length) return null;
  let candidate = slides[0].parentElement;
  while (candidate && candidate !== doc.body && candidate !== doc.documentElement) {
    if (slides.every((slide) => candidate?.contains(slide))) return candidate;
    candidate = candidate.parentElement;
  }
  return null;
}

export function markDeckArtifactRoot(doc: Document): HTMLElement | null {
  doc.querySelectorAll<HTMLElement>(`[${DECK_RUNTIME_ROOT_ATTR}]`).forEach(
    (element) => element.removeAttribute(DECK_RUNTIME_ROOT_ATTR),
  );
  const root = findDeckArtifactRoot(doc);
  root?.setAttribute(DECK_RUNTIME_ROOT_ATTR, "true");
  doc.querySelectorAll<HTMLElement>(`[${DECK_RUNTIME_SLIDE_ATTR}]`).forEach(
    (element) => element.removeAttribute(DECK_RUNTIME_SLIDE_ATTR),
  );
  findDeckSlides(doc).forEach(
    (slide) => slide.setAttribute(DECK_RUNTIME_SLIDE_ATTR, "true"),
  );
  return root;
}

export function canonicalDeckFrameIds(
  slides: DeckSlideIdentitySource[],
): string[] {
  const used = new Set<string>();
  return slides.map((slide, index) => {
    const base = (
      slide.dataFrameId?.trim()
      || slide.id?.trim()
      || slide.dataSlideIndex?.trim()
      || `slide_${index + 1}`
    );
    let candidate = base;
    let suffix = 2;
    while (used.has(candidate)) candidate = `${base}__${suffix++}`;
    used.add(candidate);
    return candidate;
  });
}

export function deckPresentationMode(
  slides: DeckSlidePaintState[],
): DeckPresentationMode {
  if (slides.length < 2) return "stacked";
  const painted = slides.filter(isDeckSlidePainted).length;
  if (painted <= 1) return "player";

  const positioned = slides.every((slide) => (
    slide.position === "absolute" || slide.position === "fixed"
  ));
  if (!positioned) return "stacked";
  const offsetParent = slides[0].offsetParent;
  const fixedToViewport = slides.every((slide) => slide.position === "fixed");
  if (
    !fixedToViewport
    && (!offsetParent || slides.some((slide) => slide.offsetParent !== offsetParent))
  ) {
    return "stacked";
  }
  // Script-driven decks often layer every slide at one authored origin and
  // move inactive roots offscreen with transforms.
  const firstTop = slides[0].offsetTop ?? 0;
  const firstLeft = slides[0].offsetLeft ?? 0;
  const sharesLayoutOrigin = slides.every((slide) => (
    Math.abs((slide.offsetTop ?? 0) - firstTop) <= 2
    && Math.abs((slide.offsetLeft ?? 0) - firstLeft) <= 2
  ));
  return sharesLayoutOrigin ? "player" : "stacked";
}

function isDeckSlidePainted(slide: DeckSlidePaintState): boolean {
  return (
    slide.display !== "none"
    && slide.visibility !== "hidden"
    && slide.visibility !== "collapse"
    && slide.opacity > 0.01
    && slide.width > 1
    && slide.height > 1
  );
}

export function deckAccessibilityState(
  mode: DeckPresentationMode,
  activeIndex: number,
  slideCount: number,
) {
  return Array.from({ length: Math.max(0, slideCount) }, (_, index) => ({
    ariaCurrent: index === activeIndex ? "page" : null,
    ariaHidden: mode === "player" ? (index === activeIndex ? "false" : "true") : null,
  }));
}

export function findDeckSlides(doc: Document): HTMLElement[] {
  for (const selector of DECK_SLIDE_SELECTORS) {
    const slides = Array.from(doc.querySelectorAll<HTMLElement>(selector));
    if (slides.length >= 2) return slides;
  }
  return [];
}

function capturePaintState(slide: HTMLElement): DeckSlidePaintState {
  const view = slide.ownerDocument.defaultView;
  const style = view?.getComputedStyle(slide);
  const rect = slide.getBoundingClientRect();
  const offsetParent = slide.offsetParent as HTMLElement | null;
  return {
    display: style?.display ?? "block",
    visibility: style?.visibility ?? "visible",
    opacity: Number(style?.opacity ?? "1"),
    width: slide.offsetWidth || Number.parseFloat(style?.width ?? "0") || rect.width || 0,
    height: slide.offsetHeight || Number.parseFloat(style?.height ?? "0") || rect.height || 0,
    position: style?.position ?? "static",
    offsetTop: slide.offsetTop,
    offsetLeft: slide.offsetLeft,
    offsetParent,
    containingBlockWidth: offsetParent?.clientWidth || view?.innerWidth || 0,
    containingBlockHeight: offsetParent?.clientHeight || view?.innerHeight || 0,
    transform: style?.transform ?? "none",
    transformOrigin: style?.transformOrigin ?? "50% 50%",
    pointerEvents: style?.pointerEvents ?? "auto",
    intersectsViewport: Boolean(
      view
      && rect.right > 0
      && rect.bottom > 0
      && rect.left < view.innerWidth
      && rect.top < view.innerHeight
    ),
  };
}

function hasAuthoredActiveMarker(slide: HTMLElement): boolean {
  const dataActive = slide.getAttribute("data-active")?.trim().toLowerCase();
  return (
    slide.getAttribute("aria-current") === "page"
    || (dataActive !== undefined && !["", "false", "0", "off"].includes(dataActive))
    || slide.classList.contains("active")
    || slide.classList.contains("is-active")
    || slide.classList.contains("current")
  );
}

export function scanDeckDocument(doc: Document): DeckDomScan {
  const normalizationStyles = CANVAS_NORMALIZATION_STYLE_IDS
    .map((id) => doc.getElementById(id))
    .filter((node): node is HTMLStyleElement => node?.tagName === "STYLE");
  const priorDisabled = normalizationStyles.map((style) => style.disabled);
  let slides: HTMLElement[] = [];
  let paintStates: DeckSlidePaintState[] = [];
  try {
    normalizationStyles.forEach((style) => { style.disabled = true; });
    slides = findDeckSlides(doc);
    paintStates = slides.map(capturePaintState);
  } finally {
    normalizationStyles.forEach((style, index) => {
      style.disabled = priorDisabled[index];
    });
  }

  const frameIds = canonicalDeckFrameIds(slides.map((slide) => ({
    dataFrameId: slide.dataset.frameId,
    id: slide.id,
    dataSlideIndex: slide.dataset.slideIndex,
  })));
  const mode = deckPresentationMode(paintStates);
  const authoredActiveIndex = slides.findIndex(hasAuthoredActiveMarker);
  const onScreenPaintedIndex = paintStates.findIndex(
    (state) => isDeckSlidePainted(state) && state.intersectsViewport,
  );
  const paintedIndex = paintStates.findIndex(isDeckSlidePainted);
  const baselineIndex = authoredActiveIndex >= 0
    ? authoredActiveIndex
    : onScreenPaintedIndex >= 0
      ? onScreenPaintedIndex
      : paintedIndex;
  const baseline = paintStates[Math.max(0, baselineIndex)] ?? {
    display: "block",
    visibility: "visible",
    opacity: 1,
    width: 0,
    height: 0,
  };
  const root = doc.querySelector<HTMLElement>(
    ".od-deck, .deck, [data-autodesign-artifact-root]",
  );
  const playerWidth = baseline.width > 1 ? baseline.width : root?.clientWidth || 1280;
  const playerHeight = baseline.height > 1 ? baseline.height : 720;
  const playerOriginNeedsReset = mode === "player"
    && baseline.transform === "none"
    && (baseline.position === "absolute" || baseline.position === "fixed")
    && (baseline.containingBlockWidth ?? 0) > 1
    && (baseline.containingBlockHeight ?? 0) > 1
    && Math.abs(
      (baseline.offsetLeft ?? 0) - (baseline.containingBlockWidth ?? 0) / 2,
    ) <= 2
    && Math.abs(
      (baseline.offsetTop ?? 0) - (baseline.containingBlockHeight ?? 0) / 2,
    ) <= 2;
  return {
    slides,
    frameIds,
    mode,
    playerDisplay: baseline.display === "none" ? "block" : baseline.display,
    playerWidth,
    playerHeight,
    playerTransform: baseline.transform ?? "none",
    playerTransformOrigin: baseline.transformOrigin ?? "50% 50%",
    playerPointerEvents: baseline.pointerEvents ?? "auto",
    playerOriginNeedsReset,
    authoredDataActive: slides.some((slide) => slide.hasAttribute("data-active")),
  };
}

function rememberAttribute(element: HTMLElement, name: keyof typeof ORIGINAL_ATTRIBUTES): void {
  const originalName = ORIGINAL_ATTRIBUTES[name];
  if (element.hasAttribute(originalName)) return;
  element.setAttribute(
    originalName,
    JSON.stringify({
      present: element.hasAttribute(name),
      value: element.getAttribute(name),
    }),
  );
}

function restoreAttribute(element: HTMLElement, name: keyof typeof ORIGINAL_ATTRIBUTES): void {
  const originalName = ORIGINAL_ATTRIBUTES[name];
  if (!element.hasAttribute(originalName)) return;
  const serialized = element.getAttribute(originalName);
  element.removeAttribute(originalName);
  try {
    const original = JSON.parse(serialized ?? "") as {
      present?: boolean;
      value?: string | null;
    };
    if (original.present) element.setAttribute(name, original.value ?? "");
    else element.removeAttribute(name);
  } catch {
    // A malformed parent marker is not safe to interpret as authored state.
  }
}

function safeDisplay(value: string): string {
  return /^(block|flex|grid|inline-block|inline-flex|inline-grid)$/.test(value)
    ? value
    : "block";
}

function safeTransform(value: string): string {
  return /^(?:none|matrix\([^;{}]+\)|matrix3d\([^;{}]+\))$/.test(value)
    ? value
    : "none";
}

function safeTransformOrigin(value: string): string {
  const tokens = value.trim().split(/\s+/);
  const validToken = (token: string) => (
    /^(?:left|center|right|top|bottom)$/.test(token)
    || /^-?(?:\d+(?:\.\d+)?|\.\d+)(?:px|%)$/.test(token)
  );
  return tokens.length >= 1 && tokens.length <= 3 && tokens.every(validToken)
    ? tokens.join(" ")
    : "50% 50%";
}

function safePointerEvents(value: string): string {
  return /^(?:auto|none|all|visible|visiblePainted|visibleFill|visibleStroke|painted|fill|stroke|bounding-box)$/.test(value)
    ? value
    : "auto";
}

function ensureNavigationStyle(doc: Document, scan: DeckDomScan): void {
  if (!doc.head) return;
  let style = doc.getElementById(NAV_STYLE_ID) as HTMLStyleElement | null;
  if (!style) {
    style = doc.createElement("style");
    style.id = NAV_STYLE_ID;
    doc.head.appendChild(style);
  }
  const width = Math.max(2, Math.round(scan.playerWidth));
  const height = Math.max(2, Math.round(scan.playerHeight));
  const transform = safeTransform(scan.playerTransform);
  const resetScriptPosition = transform === "none" && scan.playerOriginNeedsReset
    ? "top: 0 !important; left: 0 !important;"
    : "";
  style.textContent = `
    body[${NAV_MODE_ATTR}="player"] [${NAV_SLIDE_ATTR}="true"] {
      display: none !important;
      visibility: hidden !important;
      opacity: 0 !important;
      width: 0 !important;
      height: 0 !important;
      pointer-events: none !important;
    }
    body[${NAV_MODE_ATTR}="player"] [${NAV_SLIDE_ATTR}="true"][${NAV_ACTIVE_ATTR}="true"] {
      display: ${safeDisplay(scan.playerDisplay)} !important;
      visibility: visible !important;
      opacity: 1 !important;
      width: ${width}px !important;
      height: ${height}px !important;
      ${resetScriptPosition}
      transform: ${transform} !important;
      transform-origin: ${safeTransformOrigin(scan.playerTransformOrigin)} !important;
      pointer-events: ${safePointerEvents(scan.playerPointerEvents)} !important;
    }
  `;
}

export function applyDeckNavigationState(
  doc: Document,
  scan: DeckDomScan,
  activeIndex: number,
): number {
  if (scan.slides.length === 0) return 0;
  const safeIndex = Math.max(0, Math.min(scan.slides.length - 1, activeIndex));
  const accessibility = deckAccessibilityState(
    scan.mode,
    safeIndex,
    scan.slides.length,
  );
  ensureNavigationStyle(doc, scan);
  doc.body.setAttribute(NAV_MODE_ATTR, scan.mode);
  rememberAttribute(doc.body, "data-current-slide");
  doc.body.setAttribute("data-current-slide", String(safeIndex + 1));
  const deck = doc.querySelector<HTMLElement>(
    ".od-deck, .deck, [data-autodesign-artifact-root]",
  );
  if (deck) {
    rememberAttribute(deck, "data-current-slide");
    deck.setAttribute("data-current-slide", String(safeIndex + 1));
  }
  scan.slides.forEach((slide, index) => {
    rememberAttribute(slide, "aria-current");
    rememberAttribute(slide, "aria-hidden");
    if (scan.authoredDataActive) rememberAttribute(slide, "data-active");
    slide.setAttribute(NAV_SLIDE_ATTR, "true");
    slide.setAttribute(NAV_ACTIVE_ATTR, String(index === safeIndex));
    slide.setAttribute(NAV_FRAME_ATTR, scan.frameIds[index]);
    const state = accessibility[index];
    if (state.ariaCurrent === null) slide.removeAttribute("aria-current");
    else slide.setAttribute("aria-current", state.ariaCurrent);
    if (scan.mode === "player") {
      slide.setAttribute("aria-hidden", state.ariaHidden ?? "true");
      if (scan.authoredDataActive) {
        slide.setAttribute("data-active", String(index === safeIndex));
      }
    } else {
      restoreAttribute(slide, "aria-hidden");
      restoreAttribute(slide, "data-active");
    }
  });
  return safeIndex;
}

export function stripDeckNavigationState(root: HTMLElement): void {
  const doc = root.ownerDocument;
  const ownsDocumentState = root === doc.documentElement || root === doc.body;
  const elements = [root, ...Array.from(root.querySelectorAll<HTMLElement>("*"))];
  for (const element of elements) {
    restoreAttribute(element, "aria-current");
    restoreAttribute(element, "aria-hidden");
    restoreAttribute(element, "data-active");
    restoreAttribute(element, "data-current-slide");
    element.removeAttribute(NAV_MODE_ATTR);
    element.removeAttribute(NAV_SLIDE_ATTR);
    element.removeAttribute(NAV_ACTIVE_ATTR);
    element.removeAttribute(NAV_FRAME_ATTR);
    element.removeAttribute(DECK_RUNTIME_ROOT_ATTR);
    element.removeAttribute(DECK_RUNTIME_SLIDE_ATTR);
  }
  if (ownsDocumentState) {
    doc.getElementById(NAV_STYLE_ID)?.remove();
    restoreAttribute(doc.body, "data-current-slide");
    doc.body.removeAttribute(NAV_MODE_ATTR);
  }
}

export function deckIndexFromHash(hash: string, frameIds: string[]): number {
  if (!hash) return -1;
  try {
    return frameIds.indexOf(decodeURIComponent(hash.replace(/^#/, "")));
  } catch {
    return -1;
  }
}

export function deckIndexForKey(
  key: string,
  currentIndex: number,
  slideCount: number,
): number | null {
  if (slideCount <= 0) return null;
  if (key === "Home") return 0;
  if (key === "End") return slideCount - 1;
  if (BACK_KEYS.has(key)) return Math.max(0, currentIndex - 1);
  if (FORWARD_KEYS.has(key)) return Math.min(slideCount - 1, currentIndex + 1);
  return null;
}

export function deckProgress(activeIndex: number, slideCount: number) {
  const total = Math.max(0, slideCount);
  const current = total === 0 ? 0 : Math.min(total, Math.max(1, activeIndex + 1));
  return {
    current,
    total,
    percent: total === 0 ? 0 : (current / total) * 100,
    label: `${current} / ${total}`,
  };
}
