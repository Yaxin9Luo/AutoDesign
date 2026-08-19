import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { Window } from "happy-dom";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { createServer } from "vite";

const window = new Window({ url: "http://localhost/" });
Object.assign(globalThis, {
  window,
  document: window.document,
  Node: window.Node,
  HTMLElement: window.HTMLElement,
  Event: window.Event,
  MouseEvent: window.MouseEvent,
  KeyboardEvent: window.KeyboardEvent,
  requestAnimationFrame: (callback: FrameRequestCallback) => window.setTimeout(callback, 0),
  cancelAnimationFrame: (handle: number) => window.clearTimeout(handle),
  IS_REACT_ACT_ENVIRONMENT: true,
});

const vite = await createServer({
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true },
  appType: "custom",
});
const { CanvasPicker } = await vite.ssrLoadModule("/src/components/CanvasPicker.tsx") as
  typeof import("../src/components/CanvasPicker.tsx");
const { useApp } = await vite.ssrLoadModule("/src/lib/store.ts") as
  typeof import("../src/lib/store.ts");
await vite.close();

const presets = [
  { id: "auto", label: "Auto · Prompt first", template: null, canvas: null },
  {
    id: "cvpr-landscape",
    label: "2:1 Wide",
    template: "cvpr-landscape",
    canvas: { w_px: 3072, h_px: 1536, dpi: 150, aspect_ratio: "2:1", color_mode: "RGB" },
  },
  {
    id: "academic-landscape-5x3",
    label: "5:3 Landscape",
    template: "academic-landscape-5x3",
    canvas: { w_px: 2560, h_px: 1536, dpi: 150, aspect_ratio: "5:3", color_mode: "RGB" },
  },
  {
    id: "academic-landscape-1.4",
    label: "1.4:1 Landscape",
    template: "academic-landscape-1.4",
    canvas: { w_px: 2150, h_px: 1536, dpi: 150, aspect_ratio: "1.4:1", color_mode: "RGB" },
  },
  {
    id: "poster-classic-4x3",
    label: "4:3 Landscape",
    template: "poster-classic-4x3",
    canvas: { w_px: 2048, h_px: 1536, dpi: 300, aspect_ratio: "4:3", color_mode: "RGB" },
  },
  {
    id: "neurips-portrait",
    label: "3:4 Portrait",
    template: "neurips-portrait",
    canvas: { w_px: 1536, h_px: 2048, dpi: 300, aspect_ratio: "3:4", color_mode: "RGB" },
  },
];

type PickerProps = React.ComponentProps<typeof CanvasPicker>;

async function settle() {
  await act(async () => {
    await new Promise((resolve) => window.setTimeout(resolve, 0));
  });
}

async function mountPicker(overrides: Partial<PickerProps> = {}) {
  useApp.setState({ ui_language: "en" });
  const panel = document.createElement("div");
  panel.className = "app-panel";
  const container = document.createElement("div");
  panel.appendChild(container);
  document.body.appendChild(panel);
  const selections: string[] = [];
  let retries = 0;
  let props: PickerProps = {
    presets,
    status: "ready",
    error: null,
    selectedId: "auto",
    openRequest: 0,
    invalid: false,
    onSelect: (id) => { selections.push(id); },
    onRetry: () => { retries += 1; },
    ...overrides,
  };
  const root: Root = createRoot(container);
  const render = async (next: Partial<PickerProps> = {}) => {
    props = { ...props, ...next };
    await act(async () => { root.render(React.createElement(CanvasPicker, props)); });
    await settle();
  };
  await render();
  return {
    panel,
    container,
    selections,
    retries: () => retries,
    render,
    unmount: async () => {
      await act(async () => { root.unmount(); });
      panel.remove();
    },
  };
}

function press(target: Element, key: string) {
  target.dispatchEvent(new window.KeyboardEvent("keydown", { key, bubbles: true }));
}

test("mounted CanvasPicker exposes listbox semantics and complete keyboard focus behavior", async () => {
  const mounted = await mountPicker();
  try {
    const trigger = mounted.container.querySelector<HTMLButtonElement>("button[aria-haspopup='listbox']")!;
    assert.equal(trigger.getAttribute("aria-expanded"), "false");
    await act(async () => { trigger.click(); });
    await settle();

    const listbox = mounted.container.querySelector<HTMLElement>("[role='listbox']")!;
    const options = [...listbox.querySelectorAll<HTMLButtonElement>("[role='option']")];
    assert.equal(trigger.getAttribute("aria-expanded"), "true");
    assert.equal(trigger.getAttribute("aria-controls"), listbox.id);
    assert.equal(listbox.getAttribute("aria-label"), "Canvas");
    assert.equal(options.length, 6);
    assert.equal(options[0]?.getAttribute("aria-selected"), "true");
    assert.equal(document.activeElement, options[0]);

    await act(async () => { press(options[0]!, "ArrowUp"); });
    assert.equal(document.activeElement, options[5]);
    await act(async () => { press(options[5]!, "ArrowDown"); });
    assert.equal(document.activeElement, options[0]);
    await act(async () => { press(options[0]!, "ArrowUp"); });
    assert.equal(document.activeElement, options[5]);
    await act(async () => { press(options[5]!, "Home"); });
    assert.equal(document.activeElement, options[0]);
    await act(async () => { press(options[0]!, "End"); });
    assert.equal(document.activeElement, options[5]);
    await act(async () => { press(options[5]!, "Enter"); });
    await settle();
    assert.deepEqual(mounted.selections, ["neurips-portrait"]);
    assert.equal(mounted.container.querySelector("[role='listbox']"), null);
    assert.equal(document.activeElement, trigger);

    await act(async () => { trigger.click(); });
    await settle();
    const reopened = mounted.container.querySelector<HTMLButtonElement>("[role='option']")!;
    await act(async () => { press(reopened, "Escape"); });
    await settle();
    assert.equal(mounted.container.querySelector("[role='listbox']"), null);
    assert.equal(document.activeElement, trigger);
  } finally {
    await mounted.unmount();
  }
});

test("mounted compact CanvasPicker portals within the viewport and keeps listbox semantics", async () => {
  const mounted = await mountPicker({ compact: true });
  try {
    Object.defineProperties(window, {
      innerWidth: { configurable: true, value: 1024 },
      innerHeight: { configurable: true, value: 768 },
    });
    Object.defineProperty(mounted.panel, "getBoundingClientRect", {
      configurable: true,
      value: () => ({ left: 100, right: 500, top: 0, bottom: 768, width: 400, height: 768 }),
    });
    const trigger = mounted.container.querySelector<HTMLButtonElement>("button")!;
    Object.defineProperty(trigger, "getBoundingClientRect", {
      configurable: true,
      value: () => ({ left: 468, right: 500, top: 700, bottom: 732, width: 32, height: 32 }),
    });
    await act(async () => { trigger.click(); });
    await settle();

    const listbox = document.body.querySelector<HTMLElement>("[role='listbox']")!;
    const popover = listbox.parentElement!;
    assert.equal(mounted.container.contains(popover), false);
    assert.match(popover.className, /fixed/);
    assert.equal(popover.style.left, "172px");
    assert.equal(popover.style.bottom, "76px");
    assert.equal(popover.style.width, "320px");
    assert.equal(listbox.querySelectorAll("[role='option']").length, 6);
  } finally {
    await mounted.unmount();
  }
});

test("mounted CanvasPicker exposes focusable loading and retryable error states", async () => {
  const mounted = await mountPicker({ presets: [], status: "loading" });
  try {
    const trigger = mounted.container.querySelector<HTMLButtonElement>("button")!;
    await act(async () => { trigger.click(); });
    await settle();
    const loading = mounted.container.querySelector<HTMLElement>("[role='status']")!;
    assert.match(loading.textContent ?? "", /Loading canvas presets/);
    assert.equal(document.activeElement, loading);

    await mounted.render({ status: "error", error: "offline" });
    const alert = mounted.container.querySelector<HTMLElement>("[role='alert']")!;
    const retry = alert.querySelector<HTMLButtonElement>("button")!;
    assert.match(alert.textContent ?? "", /Canvas preset catalog unavailable/);
    assert.equal(document.activeElement, retry);
    await act(async () => { retry.click(); });
    assert.equal(mounted.retries(), 1);
  } finally {
    await mounted.unmount();
  }
});
