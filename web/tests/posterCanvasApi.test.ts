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
  };
})();

Object.assign(globalThis, {
  document: { cookie: "" },
  window: { localStorage: memoryStorage },
});

const vite = await createServer({
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true },
  appType: "custom",
});
const { fetchPosterCanvasPresets, startGenerate } = await vite.ssrLoadModule(
  "/src/lib/api.ts",
) as typeof import("../src/lib/api.ts");
await vite.close();

const jsonResponse = (body: unknown, status = 200): Response => new Response(
  JSON.stringify(body),
  { status, headers: { "Content-Type": "application/json" } },
);

test("fetches the Poster canvas catalog from the backend", async () => {
  const originalFetch = globalThis.fetch;
  let requested = "";
  globalThis.fetch = (async (input: string | URL | Request) => {
    requested = String(input);
    return jsonResponse({
      version: 1,
      kind: "poster_canvas_presets",
      default_preset_id: "cvpr-landscape",
      presets: [],
    });
  }) as typeof fetch;
  try {
    const result = await fetchPosterCanvasPresets();
    assert.equal(requested, "/api/canvas-presets?artifact_type=poster");
    assert.equal(result.kind, "poster_canvas_presets");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("reserved Auto generation preserves prompt bytes and sends no template", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Array<{ url: string; body: unknown }> = [];
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    requests.push({ url, body: init?.body });
    if (url === "/api/runs/reserve") {
      return jsonResponse({
        run_id: "run_auto",
        upload_token: "token",
        input_slots: [],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === "/api/runs/run_auto/start") {
      return jsonResponse({
        run_id: "run_auto",
        placeholder_message: {
          id: "msg_run_auto",
          role: "assistant",
          text: "",
          ts: 1,
          status: "streaming",
        },
      });
    }
    throw new Error(`unexpected fetch ${url}`);
  }) as typeof fetch;

  const brief = "Poster 1.4:1\nKeep   these spaces.";
  try {
    await startGenerate({
      brief,
      attachments: [],
      artifact_type: "poster",
      palette_id: "academic_blue",
      canvas_preset_id: "auto",
    }, undefined, { reserveUploads: true });
    const body = JSON.parse(String(requests[0]?.body));
    assert.equal(body.brief, brief);
    assert.equal(body.canvas_preset_id, "auto");
    assert.equal(body.template, null);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("explicit curated presets send the matching template and selection", async () => {
  const originalFetch = globalThis.fetch;
  const expected = [
    "cvpr-landscape",
    "academic-landscape-5x3",
    "academic-landscape-1.4",
    "poster-classic-4x3",
    "neurips-portrait",
  ];
  const bodies: Array<Record<string, unknown>> = [];
  let sequence = 0;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      const runId = `run_${sequence++}`;
      bodies.push(JSON.parse(String(init?.body)));
      return jsonResponse({
        run_id: runId,
        upload_token: "token",
        input_slots: [],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (/\/api\/runs\/run_\d+\/start/.test(url)) {
      const runId = url.split("/")[3]!;
      return jsonResponse({
        run_id: runId,
        placeholder_message: { id: `msg_${runId}`, role: "assistant", text: "", ts: 1, status: "streaming" },
      });
    }
    throw new Error(`unexpected fetch ${url}`);
  }) as typeof fetch;

  try {
    for (const presetId of expected) {
      await startGenerate({
        brief: "Create a poster",
        attachments: [],
        artifact_type: "poster",
        palette_id: "academic_blue",
        template: presetId,
        canvas_preset_id: presetId,
      }, undefined, { reserveUploads: true });
    }
    assert.deepEqual(
      bodies.map((body) => [body.canvas_preset_id, body.template]),
      expected.map((presetId) => [presetId, presetId]),
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
