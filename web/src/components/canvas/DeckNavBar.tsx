/**
 * Top thumbnail strip for multi-section HTML artifacts (deck / paper2deck).
 *
 * Self-hiding: scans the iframe's body for slide-like elements via a list
 * of selectors (data-slide, section.slide, body > section, …) — if fewer
 * than 2 are found, renders nothing. So this component is safe to drop
 * onto any HtmlArtifactView regardless of artifact_type.
 *
 * Each chip is a tiny iframe loading the same artifact URL, scrolled to
 * its own slide and CSS-scaled to thumbnail size. The trusted parent owns
 * keyboard navigation, hash restoration, current-slide state, and progress;
 * generated deck HTML remains script-free inside its sandbox.
 *
 * Bar height is drag-resizable on the bottom edge; thumbnail dimensions
 * derive from height so the slides visually grow / shrink together.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "@/lib/store";
import {
  applyDeckNavigationState,
  DECK_RUNTIME_ROOT_ATTR,
  DECK_RUNTIME_SLIDE_ATTR,
  markDeckArtifactRoot,
  type DeckDomScan,
  deckIndexForKey,
  deckIndexFromHash,
  deckProgress,
  scanDeckDocument,
  stripDeckNavigationState,
} from "@/lib/deck_navigation";
import { CANVAS_LAYER_ORDER } from "@/lib/canvas_zoom";
import { ResizeHandle } from "../ResizeHandle";

interface Props {
  iframe: HTMLIFrameElement | null;
}

interface SlideRef {
  el: HTMLElement;
  idx: number;
}

const VERTICAL_PADDING = 24; // py-3 (12 top + 12 bottom)
// scanDeckDocument disables this established style ID while classifying authored layouts.
const THUMBNAIL_NORMALIZATION_STYLE_ID = "autodesign-web-poster-editor-frame";

function normalizeThumbnailDocument(
  doc: Document,
  width: number,
  height: number,
): void {
  markDeckArtifactRoot(doc);
  let style = doc.getElementById(THUMBNAIL_NORMALIZATION_STYLE_ID) as HTMLStyleElement | null;
  if (!style) {
    style = doc.createElement("style");
    style.id = THUMBNAIL_NORMALIZATION_STYLE_ID;
    (doc.head ?? doc.documentElement).appendChild(style);
  }
  style.textContent = `
    html, body {
      width: ${width}px !important;
      min-width: ${width}px !important;
      min-height: ${height}px !important;
      margin: 0 !important;
      padding: 0 !important;
      display: block !important;
      background: transparent !important;
      box-sizing: border-box !important;
    }
    [${DECK_RUNTIME_ROOT_ATTR}] {
      display: block !important;
      width: ${width}px !important;
      margin: 0 !important;
      padding: 0 !important;
      gap: 0 !important;
      transform: none !important;
    }
    [${DECK_RUNTIME_SLIDE_ATTR}] {
      width: ${width}px !important;
      height: ${height}px !important;
      margin: 0 !important;
      box-shadow: none !important;
      scroll-margin-top: 0 !important;
    }
    .kbd-help { display: none !important; }
  `;
}

export function DeckNavBar({ iframe }: Props) {
  const [slides, setSlides] = useState<SlideRef[]>([]);
  const [deckScan, setDeckScan] = useState<DeckDomScan | null>(null);
  const [activeIdx, setActiveIdx] = useState(0);
  const [srcUrl, setSrcUrl] = useState<string | null>(null);
  const [documentGeneration, setDocumentGeneration] = useState(0);
  const activeFrameIdRef = useRef<string | null>(null);
  const scannedDocumentRef = useRef<Document | null>(null);
  const pendingStackedRestoreRef = useRef<number | null>(null);

  const barHeight = useApp((s) => s.deck_navbar_height);
  const setDeckNavBarHeight = useApp((s) => s.setDeckNavBarHeight);

  // Thumbnail geometry follows the authored deck viewport. Fixed 16:9 sizing
  // crops decks whose source canvas is wider or taller than that ratio.
  const thumbH = Math.max(40, barHeight - VERTICAL_PADDING);
  const innerWidth = Math.max(2, Math.round(deckScan?.playerWidth ?? 1280));
  const innerHeight = Math.max(2, Math.round(deckScan?.playerHeight ?? 720));
  const thumbW = Math.max(40, Math.round(thumbH * (innerWidth / innerHeight)));
  const scale = thumbW / innerWidth;
  const progress = deckProgress(activeIdx, slides.length);
  const frameIds = deckScan?.frameIds ?? [];

  const setCurrentSlide = useCallback((
    index: number,
    options: { scroll?: boolean; updateHash?: boolean } = {},
  ) => {
    if (!iframe || !deckScan || slides.length === 0) return;
    const safeIndex = Math.max(0, Math.min(slides.length - 1, index));
    const slide = slides[safeIndex];
    const doc = iframe.contentDocument;
    const win = iframe.contentWindow;

    setActiveIdx(safeIndex);
    activeFrameIdRef.current = frameIds[safeIndex];
    if (doc) applyDeckNavigationState(doc, deckScan, safeIndex);

    if (options.updateHash && win) {
      const frameId = frameIds[safeIndex];
      try {
        win.history.replaceState(null, "", `#${encodeURIComponent(frameId)}`);
      } catch {
        // Same-origin access is expected; a transient reload can still detach the frame.
      }
    }
    if (options.scroll) {
      if (deckScan.mode === "stacked") {
        const reducedMotion = win?.matchMedia("(prefers-reduced-motion: reduce)").matches;
        const top = (win?.scrollY ?? 0) + slide.el.getBoundingClientRect().top;
        win?.scrollTo({
          top,
          left: 0,
          behavior: reducedMotion ? "auto" : "smooth",
        });
      } else {
        win?.scrollTo({ top: 0, left: 0 });
      }
    }
  }, [deckScan, frameIds, iframe, slides]);

  // Scan iframe for slide elements after every load.
  useEffect(() => {
    if (!iframe) return;
    let detachDocumentLifecycle: (() => void) | null = null;
    const clearScannedDocument = () => {
      detachDocumentLifecycle?.();
      detachDocumentLifecycle = null;
      scannedDocumentRef.current = null;
      setSlides([]);
      setDeckScan(null);
      setSrcUrl(null);
      pendingStackedRestoreRef.current = null;
    };
    const scan = () => {
      const doc = iframe.contentDocument;
      if (!doc?.body) {
        clearScannedDocument();
        return;
      }
      if (scannedDocumentRef.current === doc) return;
      detachDocumentLifecycle?.();
      detachDocumentLifecycle = null;
      const nextScan = scanDeckDocument(doc);
      if (nextScan.slides.length < 2) {
        clearScannedDocument();
        return;
      }
      scannedDocumentRef.current = doc;
      const docWindow = doc.defaultView;
      const onDocumentLeaving = () => {
        if (scannedDocumentRef.current === doc) clearScannedDocument();
      };
      docWindow?.addEventListener("pagehide", onDocumentLeaving, { once: true });
      docWindow?.addEventListener("beforeunload", onDocumentLeaving, { once: true });
      detachDocumentLifecycle = () => {
        docWindow?.removeEventListener("pagehide", onDocumentLeaving);
        docWindow?.removeEventListener("beforeunload", onDocumentLeaving);
      };
      const nextSlides = nextScan.slides.map((el, i) => ({ el, idx: i }));
      const ids = nextScan.frameIds;
      const hash = iframe.contentWindow?.location.hash ?? "";
      const restoredIndex = deckIndexFromHash(hash, ids);
      const preservedIndex = activeFrameIdRef.current
        ? ids.indexOf(activeFrameIdRef.current)
        : -1;
      const initialIndex = restoredIndex >= 0
        ? restoredIndex
        : preservedIndex >= 0
          ? preservedIndex
          : 0;
      setSlides(nextSlides);
      setDeckScan(nextScan);
      setDocumentGeneration((generation) => generation + 1);
      setActiveIdx(initialIndex);
      activeFrameIdRef.current = ids[initialIndex] ?? null;
      applyDeckNavigationState(doc, nextScan, initialIndex);
      if (nextScan.mode === "stacked" && restoredIndex >= 0) {
        pendingStackedRestoreRef.current = restoredIndex;
        const win = iframe.contentWindow;
        const top = (win?.scrollY ?? 0) + nextSlides[restoredIndex].el.getBoundingClientRect().top;
        if (doc.documentElement) doc.documentElement.scrollTop = top;
        if (doc.body) doc.body.scrollTop = top;
      } else if (nextScan.mode === "player") {
        iframe.contentWindow?.scrollTo({ top: 0, left: 0 });
      }
      try {
        const href = iframe.contentWindow?.location.href ?? iframe.src;
        const url = new URL(href);
        url.hash = "";
        setSrcUrl(url.toString());
      } catch {
        setSrcUrl(iframe.src.split("#", 1)[0]);
      }
    };

    const sourceObserver = new MutationObserver(() => clearScannedDocument());
    sourceObserver.observe(iframe, { attributes: true, attributeFilter: ["src"] });
    iframe.addEventListener("load", scan);
    if (iframe.contentDocument?.readyState === "complete") scan();
    return () => {
      iframe.removeEventListener("load", scan);
      sourceObserver.disconnect();
      detachDocumentLifecycle?.();
      const scannedDocument = scannedDocumentRef.current;
      if (scannedDocument?.documentElement) {
        stripDeckNavigationState(scannedDocument.documentElement);
      }
      if (scannedDocumentRef.current === scannedDocument) {
        scannedDocumentRef.current = null;
      }
    };
  }, [iframe]);

  // Track active slide based on main iframe scroll.
  useEffect(() => {
    if (!iframe || deckScan?.mode !== "stacked" || slides.length === 0) return;
    const win = iframe.contentWindow;
    if (!win) return;

    const onScroll = () => {
      const pendingRestore = pendingStackedRestoreRef.current;
      if (pendingRestore !== null && slides[pendingRestore]) {
        const top = win.scrollY + slides[pendingRestore].el.getBoundingClientRect().top;
        if (win.document.documentElement) win.document.documentElement.scrollTop = top;
        if (win.document.body) win.document.body.scrollTop = top;
        pendingStackedRestoreRef.current = null;
        setCurrentSlide(pendingRestore, { updateHash: true });
        return;
      }
      let best = 0;
      let bestDist = Infinity;
      for (const s of slides) {
        const top = s.el.getBoundingClientRect().top;
        const dist = top < 0 ? -top * 0.5 : top;
        if (dist < bestDist) {
          bestDist = dist;
          best = s.idx;
        }
      }
      setCurrentSlide(best, { updateHash: true });
    };

    win.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => win.removeEventListener("scroll", onScroll);
  }, [deckScan?.mode, iframe, setCurrentSlide, slides]);

  useEffect(() => {
    if (!iframe || slides.length === 0) return;
    const iframeWindow = iframe.contentWindow;
    if (!iframeWindow) return;

    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as { closest?: (selector: string) => Element | null } | null;
      if (target?.closest?.("input, textarea, select, [contenteditable='true']")) return;
      const nextIndex = deckIndexForKey(event.key, activeIdx, slides.length);
      if (nextIndex === null) return;
      event.preventDefault();
      setCurrentSlide(nextIndex, {
        scroll: deckScan?.mode === "stacked",
        updateHash: true,
      });
    };
    const onHashChange = () => {
      const index = deckIndexFromHash(iframeWindow.location.hash, frameIds);
      if (index >= 0) {
        setCurrentSlide(index, { scroll: deckScan?.mode === "stacked" });
      }
    };

    window.addEventListener("keydown", onKeyDown);
    iframeWindow.addEventListener("keydown", onKeyDown);
    iframeWindow.addEventListener("hashchange", onHashChange);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      iframeWindow.removeEventListener("keydown", onKeyDown);
      iframeWindow.removeEventListener("hashchange", onHashChange);
    };
  }, [activeIdx, deckScan?.mode, frameIds, iframe, setCurrentSlide, slides.length]);

  if (slides.length < 2 || !srcUrl) return null;

  const onClick = (s: SlideRef) => {
    setCurrentSlide(s.idx, {
      scroll: deckScan?.mode === "stacked",
      updateHash: true,
    });
  };

  return (
    <div
      className="sticky top-0 flex shrink-0 items-center gap-2 overflow-x-auto border-b border-ink-300/60 bg-surface-raised px-4"
      style={{
        height: barHeight,
        paddingTop: VERTICAL_PADDING / 2,
        paddingBottom: VERTICAL_PADDING / 2,
        zIndex: CANVAS_LAYER_ORDER.deckNavigation,
      }}
    >
      <span
        className="shrink-0 self-center pr-2 text-[10px] font-medium uppercase text-ink-500"
        style={{ letterSpacing: "0.16em" }}
      >
        {slides.length} slides
      </span>
      <span
        className="h-1 w-20 shrink-0 overflow-hidden bg-ink-300/70"
        aria-hidden="true"
      >
        <span
          className="block h-full bg-accent transition-[width] motion-reduce:transition-none"
          style={{ width: `${progress.percent}%` }}
        />
      </span>
      <output
        className="w-10 shrink-0 text-right font-mono text-[10px] tabular-nums text-ink-600"
        aria-live="polite"
      >
        {progress.label}
      </output>
      <span
        className="mr-1 w-px shrink-0 self-center bg-ink-300"
        style={{ height: thumbH }}
      />
      {slides.map((s) => (
        <SlideThumbChip
          key={`${srcUrl}::${documentGeneration}::${s.idx}`}
          srcUrl={srcUrl}
          idx={s.idx}
          expectedFrameIds={frameIds}
          active={activeIdx === s.idx}
          width={thumbW}
          height={thumbH}
          scale={scale}
          innerWidth={innerWidth}
          innerHeight={innerHeight}
          onClick={() => onClick(s)}
        />
      ))}

      <ResizeHandle
        side="bottom"
        getCurrentSize={() => useApp.getState().deck_navbar_height}
        setSize={setDeckNavBarHeight}
      />
    </div>
  );
}

// ---------- Thumbnail chip ----------

function SlideThumbChip({
  srcUrl,
  idx,
  expectedFrameIds,
  active,
  width,
  height,
  scale,
  innerWidth,
  innerHeight,
  onClick,
}: {
  srcUrl: string;
  idx: number;
  expectedFrameIds: string[];
  active: boolean;
  width: number;
  height: number;
  scale: number;
  innerWidth: number;
  innerHeight: number;
  onClick: () => void;
}) {
  const innerRef = useRef<HTMLIFrameElement | null>(null);
  const [trustedReady, setTrustedReady] = useState(false);

  // Scroll the thumbnail iframe so the matching slide sits at top.
  useEffect(() => {
    const f = innerRef.current;
    if (!f) return;
    let disposed = false;
    let appliedDocument: Document | null = null;
    const apply = () => {
      try {
        const doc = f.contentDocument;
        const href = f.contentWindow?.location.href ?? "about:blank";
        if (!doc?.body || doc.readyState === "loading" || href === "about:blank") return;
        const scan = scanDeckDocument(doc);
        if (
          scan.slides.length !== expectedFrameIds.length
          || scan.frameIds.some((frameId, index) => frameId !== expectedFrameIds[index])
        ) return;
        normalizeThumbnailDocument(doc, innerWidth, innerHeight);
        const target = scan.slides[idx];
        if (target) {
          const appliedIndex = applyDeckNavigationState(doc, scan, idx);
          appliedDocument = doc;
          if (scan.mode === "stacked") {
            const win = f.contentWindow;
            const top = (win?.scrollY ?? 0) + target.getBoundingClientRect().top;
            if (doc.documentElement) doc.documentElement.scrollTop = top;
            if (doc.body) doc.body.scrollTop = top;
          } else {
            f.contentWindow?.scrollTo({ top: 0, left: 0 });
          }
          if (
            !disposed
            && appliedIndex === idx
            && target.isConnected
            && target.ownerDocument === doc
            && target.dataset.autodesignNavActive === "true"
          ) setTrustedReady(true);
        }
      } catch {
        /* sandbox cross-origin would throw; we use allow-same-origin */
      }
    };
    f.addEventListener("load", apply);
    if (f.contentDocument?.readyState === "complete") apply();
    return () => {
      disposed = true;
      f.removeEventListener("load", apply);
      if (appliedDocument?.documentElement) {
        stripDeckNavigationState(appliedDocument.documentElement);
      }
    };
  }, [expectedFrameIds, idx, innerHeight, innerWidth, srcUrl]);

  return (
    <button
      onClick={onClick}
      className={`group relative shrink-0 overflow-hidden rounded-md border transition ${
        active
          ? "border-accent shadow-sm ring-1 ring-accent/50"
          : "border-ink-300/70 hover:-translate-y-px hover:border-ink-700 hover:shadow-soft"
      }`}
      style={{ width, height }}
      title={`Slide ${idx + 1}`}
      aria-pressed={active}
    >
      <div
        data-autodesign-thumbnail-ready={trustedReady}
        // Inner viewport: render the iframe at the authored deck size and scale to fit
        // chip. overflow:hidden clips to the chip box; pointer-events:none
        // on the iframe so the chip <button> handles clicks.
        style={{
          width: innerWidth,
          height: innerHeight,
          transform: `scale(${scale})`,
          transformOrigin: "top left",
          visibility: trustedReady ? "visible" : "hidden",
        }}
      >
        <iframe
          ref={innerRef}
          src={srcUrl}
          tabIndex={-1}
          aria-hidden
          sandbox="allow-same-origin"
          loading="lazy"
          style={{
            width: innerWidth,
            height: "100%",
            border: 0,
            background: "white",
            pointerEvents: "none",
          }}
        />
      </div>
      {/* Number badge */}
      <span
        className={`absolute left-1.5 top-1.5 rounded-sm px-1.5 py-0.5 font-mono text-[10px] tabular-nums ${
          active
            ? "bg-accent text-ink-50"
            : "bg-white/80 text-ink-700 backdrop-blur-sm group-hover:bg-white"
        }`}
      >
        {String(idx + 1).padStart(2, "0")}
      </span>
    </button>
  );
}
