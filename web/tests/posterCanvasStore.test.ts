import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const memoryStorage = (() => {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
    clear: () => values.clear(),
  };
})();

Object.assign(globalThis, {
  document: { cookie: "" },
  window: {
    localStorage: memoryStorage,
    setTimeout: globalThis.setTimeout.bind(globalThis),
    clearTimeout: globalThis.clearTimeout.bind(globalThis),
  },
});

const vite = await createServer({
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true },
  appType: "custom",
});
const { useApp } = await vite.ssrLoadModule("/src/lib/store.ts") as
  typeof import("../src/lib/store.ts");
const { isCanvasValidationError } = await vite.ssrLoadModule(
  "/src/lib/poster_canvas_state.ts",
) as typeof import("../src/lib/poster_canvas_state.ts");
await vite.close();

const canvasPresets = [
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

function resetStore() {
  memoryStorage.clear();
  useApp.getState().newConversation();
  const conversationId = useApp.getState().current_conversation_id;
  useApp.setState({
    intent_type: "poster",
    poster_palettes: [{
      id: "academic_blue",
      name: "Academic blue",
      roles: {
        background: "#ffffff",
        text: "#111111",
        primary: "#123456",
        secondary: "#456789",
        accent: "#789abc",
        header_text: "#ffffff",
        bar: "#123456",
      },
    }],
    poster_palettes_status: "ready",
    poster_palettes_error: null,
    poster_canvas_presets: canvasPresets,
    poster_canvas_presets_status: "ready",
    poster_canvas_presets_error: null,
    canvas_validation_errors: {},
  });
  useApp.getState().setPosterPalette("academic_blue");
  return conversationId;
}

function installFailedPosterResume(
  conversationId: string,
  taskPayload: { template?: string; canvas_preset_id?: string },
) {
  const conversation = useApp.getState().conversations[conversationId]!;
  useApp.setState({
    poster_canvas_presets: [],
    poster_canvas_presets_status: "idle",
    poster_canvas_presets_error: null,
    conversations: {
      [conversationId]: {
        ...conversation,
        poster_canvas_preset_id: taskPayload.canvas_preset_id,
        messages: [{
          id: "resume-user",
          role: "user",
          text: "Resume this poster",
          ts: 1,
          status: "done",
          task_type: "generate",
          task_payload: { artifact_type: "poster", ...taskPayload },
        }, {
          id: "resume-failure",
          role: "assistant",
          text: "Connection lost",
          ts: 2,
          status: "error",
          task_type: "generate",
          task_payload: { artifact_type: "poster", ...taskPayload },
          failure: {
            status: "connection_lost",
            artifact_type: "poster",
            produced_files: [],
          },
        }],
      },
    },
  });
}

test("stores explicit Auto independently from a legacy absent selection", () => {
  const conversationId = resetStore();
  useApp.getState().setPosterCanvasPreset("poster-classic-4x3");
  assert.equal(
    useApp.getState().conversations[conversationId]?.poster_canvas_preset_id,
    "poster-classic-4x3",
  );
  useApp.getState().setPosterCanvasPreset("auto");
  assert.equal(
    useApp.getState().conversations[conversationId]?.poster_canvas_preset_id,
    "auto",
  );
});

test("does not accept an unverified explicit preset while the catalog is unavailable", () => {
  const conversationId = resetStore();
  useApp.setState({
    poster_canvas_presets: [],
    poster_canvas_presets_status: "idle",
  });
  useApp.getState().setPosterCanvasPreset("invented-preset");
  assert.equal(
    useApp.getState().conversations[conversationId]?.poster_canvas_preset_id,
    "auto",
  );
});

test("Auto remains sendable without inventing a template while the catalog reloads", async () => {
  const conversationId = resetStore();
  useApp.setState({
    poster_canvas_presets: [],
    poster_canvas_presets_status: "idle",
  });
  const originalFetch = globalThis.fetch;
  let reserveCalls = 0;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    assert.equal(String(input), "/api/runs/reserve");
    reserveCalls += 1;
    const body = JSON.parse(String(init?.body));
    assert.equal(body.canvas_preset_id, "auto");
    assert.equal(body.template, null);
    return new Response(JSON.stringify({
      detail: {
        code: "conflicting_canvas_directives",
        message: "Choose one canvas requirement.",
      },
    }), { status: 422, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
  try {
    await useApp.getState().sendMessage("Poster ratio 4:3 and 2:1", []);
    assert.equal(reserveCalls, 1);
    assert.equal(
      useApp.getState().conversations[conversationId]?.poster_canvas_preset_id,
      "auto",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("classifies a structured canvas 422 as input validation", () => {
  const error = Object.assign(new Error("Choose one canvas size."), {
    status: 422,
    code: "conflicting_canvas_directives",
  });
  assert.equal(isCanvasValidationError(error), true);
  assert.equal(
    isCanvasValidationError(Object.assign(new Error("offline"), { status: 0, code: null })),
    false,
  );
});

test("keeps the prompt and picker state after a canvas 422 without a connection-lost message", async () => {
  const conversationId = resetStore();
  useApp.getState().setPosterCanvasPreset("poster-classic-4x3");
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    assert.equal(String(input), "/api/runs/reserve");
    return new Response(JSON.stringify({
      detail: {
        code: "conflicting_canvas_directives",
        message: "The prompt contains incompatible canvas requirements.",
      },
    }), { status: 422, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;

  const brief = "Exact 2400x1350 px, but also make it 4:3.";
  try {
    await useApp.getState().sendMessage(brief, []);
    const state = useApp.getState();
    const conversation = state.conversations[conversationId]!;
    assert.equal(conversation.poster_canvas_preset_id, "poster-classic-4x3");
    assert.equal(conversation.pending, false);
    assert.equal(conversation.messages.length, 0);
    assert.deepEqual(state.canvas_validation_errors[conversationId], {
      brief,
      message: "The prompt contains incompatible canvas requirements.",
    });
    assert.equal(
      conversation.messages.some((message) => message.failure?.status === "connection_lost"),
      false,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("resume validates an explicit canvas preset before replacing a stale legacy template", async () => {
  const conversationId = resetStore();
  installFailedPosterResume(conversationId, {
    canvas_preset_id: "poster-classic-4x3",
    template: "cvpr-landscape",
  });
  const originalFetch = globalThis.fetch;
  const requests: string[] = [];
  let generateBody: FormData | null = null;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    requests.push(url);
    if (url === "/api/canvas-presets?artifact_type=poster") {
      return new Response(JSON.stringify({
        version: 1,
        kind: "poster_canvas_presets",
        default_preset_id: "cvpr-landscape",
        presets: canvasPresets,
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (url === "/api/generate") {
      generateBody = init?.body as FormData;
      return new Response(JSON.stringify({ detail: "stop after request inspection" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }
    throw new Error(`unexpected fetch ${url}`);
  }) as typeof fetch;
  try {
    await useApp.getState().resumeRun("resume-failure");
    assert.equal(requests[0], "/api/canvas-presets?artifact_type=poster");
    assert.equal(generateBody?.get("canvas_preset_id"), "poster-classic-4x3");
    assert.equal(generateBody?.get("template"), "poster-classic-4x3");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("resume stops with visible validation when an explicit canvas catalog cannot load", async () => {
  const conversationId = resetStore();
  installFailedPosterResume(conversationId, {
    canvas_preset_id: "poster-classic-4x3",
    template: "cvpr-landscape",
  });
  const originalFetch = globalThis.fetch;
  let generateCalls = 0;
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url === "/api/canvas-presets?artifact_type=poster") {
      return new Response(JSON.stringify({ detail: "catalog offline" }), {
        status: 503,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url === "/api/generate") generateCalls += 1;
    throw new Error(`unexpected fetch ${url}`);
  }) as typeof fetch;
  try {
    await useApp.getState().resumeRun("resume-failure");
    const state = useApp.getState();
    assert.equal(generateCalls, 0);
    assert.match(state.canvas_validation_errors[conversationId]?.message ?? "", /catalog/i);
    assert.notEqual(state.conversations[conversationId]?.pending, true);
    assert.equal(state.conversations[conversationId]?.messages.at(-1)?.id, "resume-failure");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("resume keeps a true legacy template when no canvas selection snapshot exists", async () => {
  const conversationId = resetStore();
  installFailedPosterResume(conversationId, { template: "cvpr-landscape" });
  const originalFetch = globalThis.fetch;
  let generateBody: FormData | null = null;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    assert.equal(url, "/api/generate");
    generateBody = init?.body as FormData;
    return new Response(JSON.stringify({ detail: "stop after request inspection" }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
  try {
    await useApp.getState().resumeRun("resume-failure");
    assert.equal(generateBody?.get("template"), "cvpr-landscape");
    assert.equal(generateBody?.has("canvas_preset_id"), false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
