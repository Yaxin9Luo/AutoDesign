import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const vite = await createServer({
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true },
  appType: "custom",
});
const bridge = await vite.ssrLoadModule("/src/lib/iframe_bridge.ts");
const { FloatingToolbar } = await vite.ssrLoadModule(
  "/src/components/canvas/FloatingToolbar.tsx",
);
const { isIframeImageElement } = await vite.ssrLoadModule(
  "/src/components/canvas/InPlaceEditor.tsx",
);
const { findDeckArtifactRoot } = await vite.ssrLoadModule(
  "/src/components/Canvas.tsx",
);
const { useApp } = await vite.ssrLoadModule("/src/lib/store.ts");
await vite.close();

test("deck root lookup honors the trusted backend precedence", () => {
  const explicitRoot = { id: "explicit" };
  const mainDeck = { id: "main-deck" };
  const countedMain = { id: "counted-main" };
  const queries: string[] = [];
  const doc = {
    querySelector(selector: string) {
      queries.push(selector);
      if (selector === "[data-autodesign-artifact-root]") return explicitRoot;
      if (selector === "main#deck") return mainDeck;
      if (selector === "main[data-slide-count]") return countedMain;
      return null;
    },
    querySelectorAll() {
      return [];
    },
    body: null,
    documentElement: null,
  };

  assert.equal(findDeckArtifactRoot(doc), explicitRoot);
  assert.deepEqual(queries, ["[data-autodesign-artifact-root]"]);

  const mainDeckQueries: string[] = [];
  const mainDeckDoc = {
    ...doc,
    querySelector(selector: string) {
      mainDeckQueries.push(selector);
      return selector === "main#deck" ? mainDeck : null;
    },
  };
  assert.equal(findDeckArtifactRoot(mainDeckDoc), mainDeck);
  assert.deepEqual(mainDeckQueries, [
    "[data-autodesign-artifact-root]",
    "main#deck",
  ]);

  const countedQueries: string[] = [];
  const countedDoc = {
    ...doc,
    querySelector(selector: string) {
      countedQueries.push(selector);
      return selector === "main[data-slide-count]" ? countedMain : null;
    },
  };
  assert.equal(findDeckArtifactRoot(countedDoc), countedMain);
  assert.deepEqual(countedQueries, [
    "[data-autodesign-artifact-root]",
    "main#deck",
    "main[data-slide-count]",
  ]);
});

test("pointer selection resolves the innermost editable poster layer", () => {
  const child = { id: "heading" };
  const target = {
    closest(selector: string) {
      assert.equal(selector, "[data-layer-id]");
      return child;
    },
  };

  assert.equal(bridge.editableLayerForPointerTarget(target), child);
});

test("poster heading and author block ids remain their editable layer ids", () => {
  const heading = {
    getAttribute(name: string) {
      return name === "data-block-id" ? "heading_01" : null;
    },
  };

  assert.equal(bridge.paperPosterTextLayerId(heading, 1), "heading_01");
});

test("text toolbar exposes a dedicated drag handle", () => {
  const originalWindow = globalThis.window;
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: { innerWidth: 1440 },
  });
  try {
    const markup = renderToStaticMarkup(
      createElement(FloatingToolbar, {
        state: { layer_id: "authors", text: "Authors", kind: "text" },
        rect: { top: 160, left: 240, width: 420, height: 48 },
        onPatch: () => undefined,
        onDismiss: () => undefined,
        onMoveStart: () => undefined,
      }),
    );

    assert.match(markup, /aria-label="Move text box"/);
  } finally {
    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: originalWindow,
    });
  }
});

test("image replacement fit survives the pending-edits round trip", () => {
  const originalWindow = globalThis.window;
  const storage = new Map<string, string>();
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      localStorage: {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => storage.set(key, value),
        removeItem: (key: string) => storage.delete(key),
      },
    },
  });
  const state = useApp.getState();
  try {
    Object.assign(state, {
      current_conversation_id: "canvas-replacement",
      conversations: {
        "canvas-replacement": {
          id: "canvas-replacement",
          title: "Poster",
          created_at: 1,
          updated_at: 1,
          messages: [],
          artifacts: {
            poster: {
              artifact_id: "poster",
              name: "Poster",
              artifact_type: "poster",
              canvas: { w: 1600, h: 900 },
              native_format: "html",
              native_file_url: "/api/files/runs/poster/poster.html",
              layers: [{
                layer_id: "figure",
                kind: "image",
                name: "Figure",
                bbox: { x: 0, y: 0, w: 640, h: 360 },
                src: "old.png",
              }],
            },
          },
          active_artifact_id: "poster",
          pending_edits: {},
          poster_palette_id: null,
          pending: false,
        },
      },
    });

    state.updateLayer("figure", {
      src: "new.png",
      fit: "contain",
      object_position: { x: 0.5, y: 0.5 },
    });

    const edits = useApp.getState().conversations["canvas-replacement"]
      .pending_edits?.poster;
    assert.deepEqual(edits?.layers?.figure, {
      src: "new.png",
      fit: "contain",
      object_position: { x: 0.5, y: 0.5 },
      effects: {},
    });
  } finally {
    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: originalWindow,
    });
  }
});

test("image replacement recognizes image elements from the iframe realm", () => {
  assert.equal(isIframeImageElement({ tagName: "IMG" }), true);
  assert.equal(isIframeImageElement({ tagName: "FIGURE" }), false);
});
