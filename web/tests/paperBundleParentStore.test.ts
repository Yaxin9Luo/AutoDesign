import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();
  get length() { return this.values.size; }
  clear() { this.values.clear(); }
  getItem(key: string) { return this.values.get(key) ?? null; }
  key(index: number) { return [...this.values.keys()][index] ?? null; }
  removeItem(key: string) { this.values.delete(key); }
  setItem(key: string, value: string) { this.values.set(key, value); }
}

class MockEventSource {
  static readonly CLOSED = 2;
  static instances: MockEventSource[] = [];
  readonly url: string;
  readyState = 1;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }
  close() { this.readyState = MockEventSource.CLOSED; }
  emit(event: string, payload: Record<string, unknown> = {}) {
    this.onmessage?.({ data: JSON.stringify({ event, ...payload }) } as MessageEvent<string>);
  }
}

const localStorage = new MemoryStorage();
Object.assign(globalThis, {
  window: globalThis,
  localStorage,
  EventSource: MockEventSource,
  document: { cookie: "" },
});

const vite = await createServer({
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true },
  appType: "custom",
});
const {
  PAPER_BUNDLE_ARTIFACT_ORDER,
  createPaperBundleChildState,
  createPaperBundleParentState,
} = await vite.ssrLoadModule("/src/lib/paper_bundle.ts") as
  typeof import("../src/lib/paper_bundle.ts");
const { useApp } = await vite.ssrLoadModule("/src/lib/store.ts") as
  typeof import("../src/lib/store.ts");
await vite.close();

import type {
  ArtifactType,
  Conversation,
  PaperBundleParentState,
} from "../src/lib/types.ts";

const DIGEST = "382635c9325bf3273d195ff1b8a44e5b11afd7d97addeb8863ea35feb98c1a07";

const jsonResponse = (body: unknown, status = 200) => new Response(
  JSON.stringify(body),
  { status, headers: { "Content-Type": "application/json" } },
);

const conversation = (id: string, overrides: Partial<Conversation> = {}): Conversation => ({
  id,
  title: id,
  created_at: 1,
  updated_at: 1,
  messages: [],
  artifacts: {},
  active_artifact_id: null,
  ...overrides,
});

const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

function deferredResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((done) => { resolve = done; });
  return { promise, resolve };
}

async function waitFor(predicate: () => boolean, message: string) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await tick();
  }
  assert.fail(message);
}

function resetStore(conversations: Record<string, Conversation>, currentId: string) {
  MockEventSource.instances = [];
  localStorage.clear();
  localStorage.setItem("autodesign.demo_user.v1", "test-user");
  useApp.setState({
    conversations,
    current_conversation_id: currentId,
    history_user_scope: "test-user",
    runs_progress: {},
    backend_info: {
      designer_model: "test",
      image_model: "test",
      models: { designer: "test", image: "test" },
      demo_mode: false,
      user_isolation: true,
    },
    backend_needs_setup: false,
    intent_type: null,
    poster_palettes: [{
      id: "academic_blue",
      name: "Academic blue",
      roles: {
        background: "#fff",
        text: "#111",
        primary: "#123",
        secondary: "#456",
        accent: "#789",
        header_text: "#fff",
        bar: "#123",
      },
    }],
    poster_palettes_status: "ready",
    poster_palettes_error: null,
  });
}

function descriptor(
  artifactType: ArtifactType,
  state = "reserved",
  terminal = false,
  processFree = true,
  withToken = true,
) {
  return {
    run_id: `run_${artifactType}`,
    artifact_type: artifactType,
    conversation_id: `bundle:paper-bundle:${artifactType}`,
    input_slots: [{
      name: "attachment-0.pdf",
      expected_sha256: DIGEST,
      expected_size: 5,
    }],
    ...(withToken ? { upload_token: `token_${artifactType}` } : {}),
    request_digest: "a".repeat(64),
    expires_at: 10,
    state,
    terminal,
    process_free: processFree,
  };
}

function bundleResponse(
  childStates: Partial<Record<ArtifactType, [string, boolean, boolean]>> = {},
  withTokens = true,
) {
  const children = Object.fromEntries(PAPER_BUNDLE_ARTIFACT_ORDER.map((artifactType) => {
    const [state, terminal, processFree] = childStates[artifactType]
      ?? ["reserved", false, true];
    return [artifactType, descriptor(artifactType, state, terminal, processFree, withTokens)];
  }));
  const allTerminal = Object.values(children).every((child) => child.terminal);
  const state = allTerminal ? "cancelled" : "reserved";
  return {
    schema_version: 1,
    job_id: "job_bundle",
    owner_id: "test-user",
    conversation_id: "bundle",
    source_name: "paper.pdf",
    prompt_version: "1",
    state,
    children,
    request_digest: "b".repeat(64),
    revision: 1,
    created_at: 1,
    updated_at: 1,
    terminal: allTerminal,
    terminal_at: allTerminal ? 2 : null,
    cancel_requested: allTerminal,
    cancel_requested_at: allTerminal ? 2 : null,
    completed_children: Object.entries(children)
      .filter(([, child]) => child.state === "completed")
      .map(([artifactType]) => artifactType),
  };
}

function bundleResponseFor(
  jobId: string,
  parentConversationId: string,
  ownerId: string,
  childStates: Partial<Record<ArtifactType, [string, boolean, boolean]>> = {},
  withTokens = true,
) {
  const record = bundleResponse(childStates, withTokens);
  record.job_id = jobId;
  record.owner_id = ownerId;
  record.conversation_id = parentConversationId;
  for (const artifactType of PAPER_BUNDLE_ARTIFACT_ORDER) {
    record.children[artifactType].conversation_id =
      `${parentConversationId}:paper-bundle:${artifactType}`;
  }
  return record;
}

function seedBackendBundle() {
  const bundle = createPaperBundleParentState("bundle", "paper.pdf");
  bundle.job_id = "job_bundle";
  bundle.revision = 1;
  bundle.backend_state = "running";
  const conversations: Record<string, Conversation> = {
    bundle: conversation("bundle", { paper_bundle: bundle, pending: true }),
  };
  for (const artifactType of PAPER_BUNDLE_ARTIFACT_ORDER) {
    const task = bundle.tasks[artifactType];
    task.status = artifactType === "poster" ? "complete" : "running";
    task.run_id = `run_${artifactType}`;
    conversations[task.child_conversation_id] = conversation(task.child_conversation_id, {
      paper_bundle: createPaperBundleChildState("bundle", artifactType),
      pending: artifactType !== "poster",
      run_id: artifactType === "poster" ? undefined : task.run_id,
    });
  }
  resetStore(conversations, "bundle");
}

test("bundle create exposes all run ids and opens four streams before the first upload", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  const calls: string[] = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    calls.push(url);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        user_isolation: false,
        needs_setup: false,
      });
    }
    if (url === "/api/paper-bundles") {
      const body = JSON.parse(String(init?.body));
      assert.equal(body.job_id, useApp.getState().conversations.bundle.paper_bundle?.kind === "parent"
        ? useApp.getState().conversations.bundle.paper_bundle.job_id
        : undefined);
      assert.deepEqual(Object.keys(body.children).sort(), [...PAPER_BUNDLE_ARTIFACT_ORDER].sort());
      return jsonResponse({
        ...bundleResponse(),
        job_id: body.job_id,
        owner_id: "local",
        reused: false,
      });
    }
    const upload = url.match(/^\/api\/runs\/(run_(poster|deck|landing|video))\/inputs\/attachment-0\.pdf$/);
    if (upload) {
      assert.equal(MockEventSource.instances.length, 4);
      const parent = useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState;
      assert.ok(PAPER_BUNDLE_ARTIFACT_ORDER.every((type) => parent.tasks[type].run_id));
      return jsonResponse({
        run_id: upload[1],
        slot: "attachment-0.pdf",
        sha256: DIGEST,
        size: 5,
        run_state: "reserved",
        idempotent: false,
      });
    }
    const start = url.match(/^\/api\/runs\/(run_(poster|deck|landing|video))\/start$/);
    if (start) {
      return jsonResponse({
        run_id: start[1],
        placeholder_message: {
          id: `msg_${start[1]}`,
          role: "assistant",
          text: "",
          ts: 1,
          status: "streaming",
        },
      });
    }
    const artifact = url.match(/^\/api\/runs\/run_(poster|deck|landing|video)\/artifact$/);
    if (artifact) {
      const artifactType = artifact[1] as ArtifactType;
      return jsonResponse({
        message: {
          id: `msg_run_${artifactType}`,
          role: "assistant",
          text: "Done",
          ts: 2,
          run_id: `run_${artifactType}`,
          artifact_id: `art_${artifactType}`,
          status: "done",
        },
        artifact: {
          artifact_id: `art_${artifactType}`,
          name: artifactType,
          artifact_type: artifactType,
          canvas: { w: 1280, h: 720 },
          layers: [],
        },
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "../../Paper FINAL.PDF", { type: "application/pdf" }),
  );
  await waitFor(
    () => calls.filter((url) => /\/start$/.test(url)).length === 4,
    "four child starts did not run",
  );
  for (const source of MockEventSource.instances) source.emit("run.done");
  await start;

  assert.equal(calls[1], "/api/paper-bundles");
  assert.ok(calls.findIndex((url) => url.includes("/inputs/")) > 1);
  const parent = useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState;
  assert.equal(parent.job_id?.length ? true : false, true);
  assert.ok(Object.values(parent.tasks).every((task) => task.status === "complete"));
});

test("concurrent Cancel All calls share one parent request and keep unconfirmed work live", async () => {
  seedBackendBundle();
  let release!: (response: Response) => void;
  let cancelPosts = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    assert.equal(url, "/api/paper-bundles/job_bundle/cancel");
    cancelPosts += 1;
    return new Promise<Response>((resolve) => { release = resolve; });
  }) as typeof fetch;

  const first = useApp.getState().cancelPaperBundle("bundle");
  const second = useApp.getState().cancelPaperBundle("bundle");
  await waitFor(() => cancelPosts === 1, "parent cancellation did not start");

  const cancelling = useApp.getState().conversations.bundle;
  const cancellingBundle = cancelling.paper_bundle as PaperBundleParentState;
  assert.equal(cancellingBundle.backend_state, "cancelling");
  assert.equal(cancellingBundle.tasks.deck.status, "cancelling");
  assert.equal(cancellingBundle.tasks.deck.run_id, "run_deck");
  assert.equal(cancelling.pending, true);

  release(jsonResponse({
    job_id: "job_bundle",
    state: "cancelling",
    confirmed: false,
    status: "cancellation_pending",
    children: {},
  }, 202));
  await Promise.all([first, second]);

  const pending = useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState;
  assert.equal(cancelPosts, 1);
  assert.equal(pending.backend_state, "cancelling");
  assert.equal(pending.tasks.deck.status, "cancelling");
  assert.match(pending.cancel_error ?? "", /not confirmed/i);
});

test("Cancel All keeps retry disabled while backend confirmation polling is active", async () => {
  seedBackendBundle();
  useApp.setState((state) => ({
    backend_info: state.backend_info
      ? { ...state.backend_info, user_isolation: false }
      : state.backend_info,
  }));
  let cancelPosts = 0;
  let releasePoll!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/paper-bundles/job_bundle/cancel") {
      cancelPosts += 1;
      return jsonResponse({
        job_id: "job_bundle",
        state: "cancelling",
        confirmed: false,
        status: "cancellation_pending",
        children: {},
      }, 202);
    }
    if (url === "/api/paper-bundles/job_bundle") {
      return new Promise<Response>((resolve) => { releasePoll = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const first = useApp.getState().cancelPaperBundle("bundle");
  await waitFor(() => typeof releasePoll === "function", "confirmation poll did not start");
  const pending = useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState;
  assert.equal(pending.cancel_request_in_flight, true);
  assert.equal(pending.cancel_error, undefined);
  const second = useApp.getState().cancelPaperBundle("bundle");
  assert.equal(cancelPosts, 1);

  releasePoll(jsonResponse({
    ...bundleResponse({
      poster: ["completed", true, true],
      deck: ["cancelled", true, true],
      landing: ["cancelled", true, true],
      video: ["cancelled", true, true],
    }, false),
    owner_id: "local",
    state: "cancelled",
    terminal: true,
  }));
  await Promise.all([first, second]);
  const terminal = useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState;
  assert.equal(terminal.cancel_request_in_flight, false);
  assert.equal(terminal.backend_state, "cancelled");
});

test("Cancel All polls a pending parent until the backend confirms a quiescent terminal tree", async () => {
  seedBackendBundle();
  useApp.setState((state) => ({
    backend_info: state.backend_info
      ? { ...state.backend_info, user_isolation: false }
      : state.backend_info,
  }));
  let polls = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/paper-bundles/job_bundle/cancel") {
      return jsonResponse({
        job_id: "job_bundle",
        state: "cancelling",
        confirmed: false,
        status: "cancellation_pending",
        children: {},
      }, 202);
    }
    if (url === "/api/paper-bundles/job_bundle") {
      polls += 1;
      if (polls === 1) {
        return jsonResponse({
          ...bundleResponse({}, false),
          owner_id: "local",
          state: "cancelling",
          terminal: false,
          terminal_at: null,
          cancel_requested: true,
          cancel_requested_at: 2,
        });
      }
      return jsonResponse({
        ...bundleResponse({
          poster: ["completed", true, true],
          deck: ["cancelled", true, true],
          landing: ["cancelled", true, true],
          video: ["cancelled", true, true],
        }, false),
        owner_id: "local",
        state: "cancelled",
        terminal: true,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().cancelPaperBundle("bundle");

  const parent = useApp.getState().conversations.bundle;
  const bundle = parent.paper_bundle as PaperBundleParentState;
  assert.equal(polls, 2);
  assert.equal(bundle.backend_state, "cancelled");
  assert.equal(bundle.cancel_error, undefined);
  assert.equal(bundle.tasks.poster.status, "complete");
  assert.ok([bundle.tasks.deck, bundle.tasks.landing, bundle.tasks.video]
    .every((task) => task.status === "cancelled"));
  assert.equal(parent.pending, false);
});

test("a failed cancellation poll keeps the parent live with every run id", async () => {
  seedBackendBundle();
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/paper-bundles/job_bundle/cancel") {
      return jsonResponse({
        job_id: "job_bundle",
        state: "cancelling",
        confirmed: false,
        status: "cancellation_pending",
        children: {},
      }, 202);
    }
    if (url === "/api/paper-bundles/job_bundle") {
      throw new Error("poll network unavailable");
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().cancelPaperBundle("bundle");

  const parent = useApp.getState().conversations.bundle;
  const bundle = parent.paper_bundle as PaperBundleParentState;
  assert.equal(bundle.backend_state, "cancelling");
  assert.match(bundle.cancel_error ?? "", /not confirmed/i);
  assert.ok(Object.values(bundle.tasks).every((task) => Boolean(task.run_id)));
  assert.equal(parent.pending, true);
});

test("a malformed cancellation poll cannot turn a live child tree terminal", async () => {
  seedBackendBundle();
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/paper-bundles/job_bundle/cancel") {
      return jsonResponse({
        job_id: "job_bundle",
        state: "cancelling",
        confirmed: false,
        status: "cancellation_pending",
        children: {},
      }, 202);
    }
    if (url === "/api/paper-bundles/job_bundle") {
      return jsonResponse({
        ...bundleResponse({}, false),
        state: "cancelled",
        terminal: true,
        terminal_at: 2,
        cancel_requested: true,
        cancel_requested_at: 2,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().cancelPaperBundle("bundle");

  const parent = useApp.getState().conversations.bundle;
  const bundle = parent.paper_bundle as PaperBundleParentState;
  assert.equal(bundle.backend_state, "cancelling");
  assert.match(bundle.cancel_error ?? "", /not confirmed/i);
  assert.ok(Object.values(bundle.tasks).every((task) => Boolean(task.run_id)));
  assert.equal(parent.pending, true);
});

test("a delayed older poll snapshot cannot overwrite a newer local terminal revision", async () => {
  seedBackendBundle();
  let releasePoll!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/paper-bundles/job_bundle/cancel") {
      return jsonResponse({
        job_id: "job_bundle",
        state: "cancelling",
        confirmed: false,
        status: "cancellation_pending",
        children: {},
      }, 202);
    }
    if (url === "/api/paper-bundles/job_bundle") {
      return new Promise<Response>((resolve) => { releasePoll = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const cancel = useApp.getState().cancelPaperBundle("bundle");
  await waitFor(() => !!releasePoll, "parent cancellation poll did not start");
  useApp.setState((state) => {
    const parent = state.conversations.bundle;
    const bundle = parent.paper_bundle as PaperBundleParentState;
    const tasks = Object.fromEntries(PAPER_BUNDLE_ARTIFACT_ORDER.map((artifactType) => [
      artifactType,
      {
        ...bundle.tasks[artifactType],
        status: "complete" as const,
        terminal: true,
        process_free: true,
        error: undefined,
      },
    ])) as PaperBundleParentState["tasks"];
    return {
      conversations: {
        ...state.conversations,
        bundle: {
          ...parent,
          pending: false,
          paper_bundle: {
            ...bundle,
            revision: 3,
            backend_state: "completed" as const,
            cancel_error: undefined,
            tasks,
          },
        },
      },
    };
  });
  releasePoll(jsonResponse({
    ...bundleResponse({
      poster: ["cancelled", true, true],
      deck: ["cancelled", true, true],
      landing: ["cancelled", true, true],
      video: ["cancelled", true, true],
    }, false),
    revision: 2,
    state: "cancelled",
    terminal: true,
  }));
  await cancel;

  const parent = useApp.getState().conversations.bundle;
  const bundle = parent.paper_bundle as PaperBundleParentState;
  assert.equal(bundle.revision, 3);
  assert.equal(bundle.backend_state, "completed");
  assert.ok(Object.values(bundle.tasks).every((task) => task.status === "complete"));
  assert.equal(parent.pending, false);
});

test("a hanging cancellation poll is timed out without hiding the parent", async () => {
  seedBackendBundle();
  const originalSetTimeout = window.setTimeout;
  const originalClearTimeout = window.clearTimeout;
  let pollSignal: AbortSignal | undefined;
  window.setTimeout = ((handler: TimerHandler) => {
    queueMicrotask(() => {
      if (typeof handler === "function") handler();
    });
    return 1;
  }) as typeof window.setTimeout;
  window.clearTimeout = (() => undefined) as typeof window.clearTimeout;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/paper-bundles/job_bundle/cancel") {
      return jsonResponse({
        job_id: "job_bundle",
        state: "cancelling",
        confirmed: false,
        status: "cancellation_pending",
        children: {},
      }, 202);
    }
    if (url === "/api/paper-bundles/job_bundle") {
      pollSignal = init?.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        pollSignal?.addEventListener("abort", () => reject(pollSignal?.reason), { once: true });
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  try {
    await useApp.getState().cancelPaperBundle("bundle");
  } finally {
    window.setTimeout = originalSetTimeout;
    window.clearTimeout = originalClearTimeout;
  }

  const parent = useApp.getState().conversations.bundle;
  const bundle = parent.paper_bundle as PaperBundleParentState;
  assert.equal(pollSignal?.aborted, true);
  assert.equal(bundle.backend_state, "cancelling");
  assert.match(bundle.cancel_error ?? "", /not confirmed/i);
  assert.ok(Object.values(bundle.tasks).every((task) => Boolean(task.run_id)));
  assert.equal(parent.pending, true);
});

test("wall-clock rollback cannot extend the bounded cancellation poll", async () => {
  seedBackendBundle();
  const originalDateNow = Date.now;
  const originalPerformance = globalThis.performance;
  const originalSetTimeout = window.setTimeout;
  const originalClearTimeout = window.clearTimeout;
  let wallClock = 10_000;
  let monotonicClock = -5_000;
  let polls = 0;
  Date.now = () => {
    wallClock -= 1_000;
    return wallClock;
  };
  Object.defineProperty(globalThis, "performance", {
    configurable: true,
    value: { now: () => { monotonicClock += 5_000; return monotonicClock; } },
  });
  window.setTimeout = ((handler: TimerHandler) => {
    queueMicrotask(() => {
      if (typeof handler === "function") handler();
    });
    return 1;
  }) as typeof window.setTimeout;
  window.clearTimeout = (() => undefined) as typeof window.clearTimeout;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/paper-bundles/job_bundle/cancel") {
      return jsonResponse({
        job_id: "job_bundle",
        state: "cancelling",
        confirmed: false,
        status: "cancellation_pending",
        children: {},
      }, 202);
    }
    if (url === "/api/paper-bundles/job_bundle") {
      polls += 1;
      return jsonResponse({
        ...bundleResponse({}, false),
        state: "cancelling",
        terminal: false,
        terminal_at: null,
        cancel_requested: true,
        cancel_requested_at: 2,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  try {
    await useApp.getState().cancelPaperBundle("bundle");
  } finally {
    Date.now = originalDateNow;
    Object.defineProperty(globalThis, "performance", {
      configurable: true,
      value: originalPerformance,
    });
    window.setTimeout = originalSetTimeout;
    window.clearTimeout = originalClearTimeout;
  }

  const parent = useApp.getState().conversations.bundle;
  const bundle = parent.paper_bundle as PaperBundleParentState;
  assert.ok(polls < 10, `expected monotonic deadline, observed ${polls} polls`);
  assert.equal(bundle.backend_state, "cancelling");
  assert.match(bundle.cancel_error ?? "", /not confirmed/i);
  assert.equal(parent.pending, true);
});

test("a hard cancellation deadline survives missing performance and hung polls", async () => {
  seedBackendBundle();
  const originalDateNow = Date.now;
  const originalPerformance = Object.getOwnPropertyDescriptor(globalThis, "performance");
  const originalSetTimeout = window.setTimeout;
  const originalClearTimeout = window.clearTimeout;
  let wallClock = 10_000;
  let polls = 0;
  let releasePoll!: (response: Response) => void;
  let nextTimerId = 0;
  let hardDeadlineTimerId: number | undefined;
  const clearedTimers = new Set<number>();
  Date.now = () => {
    wallClock -= 1_000;
    return wallClock;
  };
  Object.defineProperty(globalThis, "performance", {
    configurable: true,
    value: undefined,
  });
  window.setTimeout = ((handler: TimerHandler, timeout?: number) => {
    const timerId = ++nextTimerId;
    if (timeout === 15_000) hardDeadlineTimerId = timerId;
    queueMicrotask(() => {
      if (!clearedTimers.has(timerId) && typeof handler === "function") handler();
    });
    return timerId;
  }) as typeof window.setTimeout;
  window.clearTimeout = ((timerId?: number) => {
    if (typeof timerId === "number") clearedTimers.add(timerId);
  }) as typeof window.clearTimeout;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/paper-bundles/job_bundle/cancel") {
      return jsonResponse({
        job_id: "job_bundle",
        state: "cancelling",
        confirmed: false,
        status: "cancellation_pending",
        children: {},
      }, 202);
    }
    if (url === "/api/paper-bundles/job_bundle") {
      polls += 1;
      return new Promise<Response>((resolve) => { releasePoll = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  try {
    const outcome = await Promise.race([
      useApp.getState().cancelPaperBundle("bundle").then(() => "settled"),
      new Promise<"hung">((resolve) => originalSetTimeout(() => resolve("hung"), 50)),
    ]);
    assert.equal(outcome, "settled");
    releasePoll(jsonResponse({
      ...bundleResponse({}, false),
      state: "cancelling",
      terminal: false,
      terminal_at: null,
      cancel_requested: true,
      cancel_requested_at: 2,
    }));
    await new Promise<void>((resolve) => originalSetTimeout(resolve, 0));
    assert.equal(polls, 1, "a late GET response restarted polling past the hard deadline");
    assert.ok(hardDeadlineTimerId, "the poll did not install a hard deadline timer");
    assert.ok(
      clearedTimers.has(hardDeadlineTimerId),
      "the hard deadline timer was not cleared after cancellation polling settled",
    );
  } finally {
    Date.now = originalDateNow;
    if (originalPerformance) {
      Object.defineProperty(globalThis, "performance", originalPerformance);
    } else {
      Reflect.deleteProperty(globalThis, "performance");
    }
    window.setTimeout = originalSetTimeout;
    window.clearTimeout = originalClearTimeout;
  }

  const parent = useApp.getState().conversations.bundle;
  const bundle = parent.paper_bundle as PaperBundleParentState;
  assert.ok(polls < 10, `expected one hard deadline, observed ${polls} hung polls`);
  assert.equal(bundle.backend_state, "cancelling");
  assert.match(bundle.cancel_error ?? "", /not confirmed/i);
  assert.equal(parent.pending, true);
});

test("Cancel All during parent create re-drives an unconfirmed request and never starts children", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  let jobId = "";
  let releaseCreate!: (response: Response) => void;
  let parentCancels = 0;
  let childWork = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        user_isolation: true,
        needs_setup: false,
      });
    }
    if (url === "/api/paper-bundles") {
      jobId = JSON.parse(String(init?.body)).job_id;
      return new Promise<Response>((resolve) => { releaseCreate = resolve; });
    }
    if (url === `/api/paper-bundles/${jobId}/cancel`) {
      parentCancels += 1;
      return jsonResponse({
        job_id: jobId,
        state: "cancelling",
        confirmed: false,
        status: "cancellation_pending",
        children: {},
      }, 202);
    }
    if (url.includes("/inputs/") || url.endsWith("/start")) {
      childWork += 1;
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await waitFor(() => !!releaseCreate, "parent create did not enter flight");
  await useApp.getState().cancelPaperBundle("bundle");
  releaseCreate(jsonResponse({ ...bundleResponse(), job_id: jobId, reused: false }));
  await start;

  assert.equal(parentCancels, 2);
  assert.equal(childWork, 0);
  const bundle = useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState;
  assert.equal(bundle.backend_state, "cancelling");
  assert.ok(Object.values(bundle.tasks).every((task) => task.status === "cancelling"));
  assert.ok(Object.values(bundle.tasks).every((task) => Boolean(task.run_id)));
});

test("a pending creation tombstone is confirmed by repeating POST cancel instead of GET", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  let jobId = "";
  let releaseCreate!: (response: Response) => void;
  let cancelPosts = 0;
  let bundleGets = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        user_isolation: true,
        needs_setup: false,
      });
    }
    if (url === "/api/paper-bundles" && init?.method === "POST") {
      jobId = JSON.parse(String(init.body)).job_id;
      return new Promise<Response>((resolve) => { releaseCreate = resolve; });
    }
    if (url === `/api/paper-bundles/${jobId}/cancel`) {
      cancelPosts += 1;
      return jsonResponse({
        job_id: jobId,
        owner_id: "test-user",
        state: cancelPosts === 1 ? "cancelling" : "cancelled",
        confirmed: cancelPosts > 1,
        status: cancelPosts === 1 ? "cancellation_pending" : "cancelled",
        pending_creation: true,
        factory_quiesced: cancelPosts > 1,
        children: {},
      }, cancelPosts === 1 ? 202 : 200);
    }
    if (url === `/api/paper-bundles/${jobId}`) {
      bundleGets += 1;
      return jsonResponse({ detail: "paper bundle not found" }, 404);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await waitFor(() => typeof releaseCreate === "function", "parent create did not enter flight");
  await useApp.getState().cancelPaperBundle("bundle");
  releaseCreate(jsonResponse({ detail: "paper bundle creation was cancelled" }, 409));
  await start;

  const parent = useApp.getState().conversations.bundle;
  const bundle = parent.paper_bundle as PaperBundleParentState;
  assert.equal(cancelPosts, 2);
  assert.equal(bundleGets, 0);
  assert.equal(bundle.backend_state, "cancelled");
  assert.equal(parent.pending, false);
  assert.ok(Object.values(bundle.tasks).every((task) => task.status === "cancelled"));
});

test("a cancel lost before create acknowledgement is re-driven after the parent exists", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  let jobId = "";
  let releaseCreate!: (response: Response) => void;
  let parentCancels = 0;
  let childWork = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        user_isolation: true,
        needs_setup: false,
      });
    }
    if (url === "/api/paper-bundles") {
      jobId = JSON.parse(String(init?.body)).job_id;
      return new Promise<Response>((resolve) => { releaseCreate = resolve; });
    }
    if (url === `/api/paper-bundles/${jobId}/cancel`) {
      parentCancels += 1;
      if (parentCancels === 1) {
        return jsonResponse({ detail: "paper bundle not found" }, 404);
      }
      return jsonResponse({
        ...bundleResponse({
          poster: ["cancelled", true, true],
          deck: ["cancelled", true, true],
          landing: ["cancelled", true, true],
          video: ["cancelled", true, true],
        }, false),
        job_id: jobId,
        status: "cancelled",
        confirmed: true,
        state: "cancelled",
        terminal: true,
      });
    }
    if (url.includes("/inputs/") || url.endsWith("/start")) {
      childWork += 1;
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await waitFor(() => !!releaseCreate, "parent create did not enter flight");
  await useApp.getState().cancelPaperBundle("bundle");
  releaseCreate(jsonResponse({ ...bundleResponse(), job_id: jobId, reused: false }));
  await start;

  const parent = useApp.getState().conversations.bundle;
  const bundle = parent.paper_bundle as PaperBundleParentState;
  assert.equal(parentCancels, 2);
  assert.equal(childWork, 0);
  assert.equal(bundle.backend_state, "cancelled");
  assert.ok(Object.values(bundle.tasks).every((task) => task.status === "cancelled"));
  assert.equal(parent.pending, false);
});

test("a stale create acknowledgement cannot downgrade a newer cancelled revision", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  let jobId = "";
  let releaseCreate!: (response: Response) => void;
  let parentCancels = 0;
  let childWork = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        user_isolation: true,
        needs_setup: false,
      });
    }
    if (url === "/api/paper-bundles") {
      jobId = JSON.parse(String(init?.body)).job_id;
      return new Promise<Response>((resolve) => { releaseCreate = resolve; });
    }
    if (url === `/api/paper-bundles/${jobId}/cancel`) {
      parentCancels += 1;
      return jsonResponse({
        ...bundleResponse({
          poster: ["cancelled", true, true],
          deck: ["cancelled", true, true],
          landing: ["cancelled", true, true],
          video: ["cancelled", true, true],
        }, false),
        job_id: jobId,
        revision: 5,
        status: "cancelled",
        confirmed: true,
        state: "cancelled",
        terminal: true,
      });
    }
    if (url.includes("/inputs/") || url.endsWith("/start")) childWork += 1;
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await waitFor(() => !!releaseCreate, "parent create did not enter flight");
  await useApp.getState().cancelPaperBundle("bundle");
  releaseCreate(jsonResponse({ ...bundleResponse(), job_id: jobId, revision: 1, reused: false }));
  await start;

  const parent = useApp.getState().conversations.bundle;
  const bundle = parent.paper_bundle as PaperBundleParentState;
  assert.equal(parentCancels, 1);
  assert.equal(childWork, 0);
  assert.equal(bundle.revision, 5);
  assert.equal(bundle.backend_state, "cancelled");
  assert.ok(Object.values(bundle.tasks).every((task) => task.status === "cancelled"));
  assert.equal(parent.pending, false);
});

test("parent cancellation network failure retains ids, streams, and cancelling state", async () => {
  seedBackendBundle();
  globalThis.fetch = (async (input) => {
    assert.equal(String(input), "/api/paper-bundles/job_bundle/cancel");
    throw new Error("network unavailable");
  }) as typeof fetch;

  await useApp.getState().cancelPaperBundle("bundle");

  const parent = useApp.getState().conversations.bundle;
  const bundle = parent.paper_bundle as PaperBundleParentState;
  assert.equal(bundle.backend_state, "cancelling");
  assert.equal(bundle.tasks.deck.status, "cancelling");
  assert.equal(bundle.tasks.deck.run_id, "run_deck");
  assert.equal(parent.pending, true);
  assert.match(bundle.cancel_error ?? "", /not confirmed/i);
});

test("a late unconfirmed parent response cannot overwrite a terminal bundle", async () => {
  seedBackendBundle();
  let release!: (response: Response) => void;
  globalThis.fetch = (async () => new Promise<Response>((resolve) => { release = resolve; })) as typeof fetch;

  const cancel = useApp.getState().cancelPaperBundle("bundle");
  await tick();
  useApp.setState((state) => {
    const parent = state.conversations.bundle;
    const bundle = parent.paper_bundle as PaperBundleParentState;
    const tasks = Object.fromEntries(PAPER_BUNDLE_ARTIFACT_ORDER.map((artifactType) => [
      artifactType,
      {
        ...bundle.tasks[artifactType],
        status: "cancelled" as const,
        terminal: true,
        process_free: true,
      },
    ])) as PaperBundleParentState["tasks"];
    return {
      conversations: {
        ...state.conversations,
        bundle: {
          ...parent,
          pending: false,
          paper_bundle: { ...bundle, backend_state: "cancelled", tasks },
        },
      },
    };
  });
  release(jsonResponse({
    job_id: "job_bundle",
    state: "cancelling",
    confirmed: false,
    status: "cancellation_pending",
    children: {},
  }, 202));
  await cancel;

  const parent = useApp.getState().conversations.bundle;
  const bundle = parent.paper_bundle as PaperBundleParentState;
  assert.equal(bundle.backend_state, "cancelled");
  assert.equal(parent.pending, false);
  assert.ok(Object.values(bundle.tasks).every((task) => task.status === "cancelled"));
});

test("Cancel All aborts in-flight uploads after the parent request and prevents every start", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  let jobId = "";
  let uploads = 0;
  let starts = 0;
  let parentCancels = 0;
  const uploadSignals: AbortSignal[] = [];
  const cancelResponse = deferredResponse();
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        user_isolation: true,
        needs_setup: false,
      });
    }
    if (url === "/api/paper-bundles") {
      jobId = JSON.parse(String(init?.body)).job_id;
      return jsonResponse({ ...bundleResponse(), job_id: jobId, reused: false });
    }
    if (/\/inputs\/attachment-0\.pdf$/.test(url)) {
      uploads += 1;
      const signal = init?.signal;
      assert.ok(signal);
      uploadSignals.push(signal);
      return new Promise<Response>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      });
    }
    if (/\/start$/.test(url)) {
      starts += 1;
      throw new Error("start must not run after the cancellation barrier");
    }
    if (url === `/api/paper-bundles/${jobId}/cancel`) {
      parentCancels += 1;
      return cancelResponse.promise;
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await waitFor(() => uploads === 4, "all uploads did not enter flight");
  const cancelling = useApp.getState().cancelPaperBundle("bundle");
  await waitFor(() => parentCancels === 1, "parent cancellation did not start");
  assert.ok(uploadSignals.every((signal) => !signal.aborted));
  cancelResponse.resolve(jsonResponse({
    ...bundleResponse({
      poster: ["cancelled", true, true],
      deck: ["cancelled", true, true],
      landing: ["cancelled", true, true],
      video: ["cancelled", true, true],
    }, false),
    job_id: jobId,
    status: "cancelled",
    confirmed: true,
    state: "cancelled",
    terminal: true,
  }));
  await cancelling;
  await start;

  assert.equal(parentCancels, 1);
  assert.equal(starts, 0);
  assert.ok(uploadSignals.every((signal) => signal.aborted));
  const bundle = useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState;
  assert.ok(Object.values(bundle.tasks).every((task) => task.status === "cancelled"));
});

test("confirmed parent cancellation keeps completed artifacts and cancels only active children", async () => {
  seedBackendBundle();
  globalThis.fetch = (async (input) => {
    assert.equal(String(input), "/api/paper-bundles/job_bundle/cancel");
    return jsonResponse({
      ...bundleResponse({
        poster: ["completed", true, true],
        deck: ["cancelled", true, true],
        landing: ["cancelled", true, true],
        video: ["cancelled", true, true],
      }, false),
      status: "cancelled",
      confirmed: true,
      state: "cancelled",
      terminal: true,
    });
  }) as typeof fetch;

  await useApp.getState().cancelPaperBundle("bundle");

  const parent = useApp.getState().conversations.bundle;
  const bundle = parent.paper_bundle as PaperBundleParentState;
  assert.equal(bundle.backend_state, "cancelled");
  assert.equal(bundle.tasks.poster.status, "complete");
  assert.ok([bundle.tasks.deck, bundle.tasks.landing, bundle.tasks.video]
    .every((task) => task.status === "cancelled"));
  assert.equal(parent.pending, false);
});

test("Cancel All follows a nonterminal backend even when every local task looks failed", async () => {
  seedBackendBundle();
  useApp.setState((state) => {
    const parent = state.conversations.bundle;
    const bundle = parent.paper_bundle as PaperBundleParentState;
    const tasks = Object.fromEntries(PAPER_BUNDLE_ARTIFACT_ORDER.map((artifactType) => [
      artifactType,
      {
        ...bundle.tasks[artifactType],
        status: "failed" as const,
        terminal: true,
        process_free: true,
        error: "stale local failure",
      },
    ])) as PaperBundleParentState["tasks"];
    return {
      conversations: {
        ...state.conversations,
        bundle: { ...parent, pending: false, paper_bundle: { ...bundle, tasks } },
      },
    };
  });
  let cancelPosts = 0;
  globalThis.fetch = (async (input) => {
    assert.equal(String(input), "/api/paper-bundles/job_bundle/cancel");
    cancelPosts += 1;
    return jsonResponse({
      ...bundleResponse({
        poster: ["cancelled", true, true],
        deck: ["cancelled", true, true],
        landing: ["cancelled", true, true],
        video: ["cancelled", true, true],
      }, false),
      status: "cancelled",
      confirmed: true,
      state: "cancelled",
      terminal: true,
    });
  }) as typeof fetch;

  await useApp.getState().cancelPaperBundle("bundle");

  const bundle = useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState;
  assert.equal(cancelPosts, 1);
  assert.equal(bundle.backend_state, "cancelled");
});

test("single child cancellation is nonoptimistic and leaves siblings running", async () => {
  seedBackendBundle();
  let release!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    assert.equal(String(input), "/api/runs/run_deck/cancel");
    return new Promise<Response>((resolve) => { release = resolve; });
  }) as typeof fetch;

  const cancel = useApp.getState().cancelPaperBundleTask("bundle", "deck");
  await tick();
  let bundle = useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState;
  assert.equal(bundle.tasks.deck.status, "cancelling");
  assert.equal(bundle.tasks.poster.status, "complete");
  assert.equal(bundle.tasks.landing.status, "running");
  assert.equal(bundle.tasks.deck.run_id, "run_deck");

  release(jsonResponse({
    run_id: "run_deck",
    status: "cancelled",
    run_state: "cancelled",
    confirmed: true,
    terminated_pids: [],
    surviving_pids: [],
  }));
  await cancel;

  bundle = useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState;
  assert.equal(bundle.tasks.deck.status, "cancelled");
  assert.equal(bundle.tasks.landing.status, "running");
});

test("single child cancellation before create acknowledgement skips only that child", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  let jobId = "";
  let releaseCreate!: (response: Response) => void;
  const uploads: string[] = [];
  const starts: string[] = [];
  let childCancels = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        user_isolation: true,
        needs_setup: false,
      });
    }
    if (url === "/api/paper-bundles") {
      jobId = JSON.parse(String(init?.body)).job_id;
      return new Promise<Response>((resolve) => { releaseCreate = resolve; });
    }
    if (url === "/api/runs/run_poster/cancel") {
      childCancels += 1;
      return jsonResponse({
        run_id: "run_poster",
        status: "cancelled",
        run_state: "cancelled",
        confirmed: true,
        terminated_pids: [],
        surviving_pids: [],
      });
    }
    const upload = url.match(/^\/api\/runs\/(run_(poster|deck|landing|video))\/inputs\/attachment-0\.pdf$/);
    if (upload) {
      uploads.push(upload[2]);
      return jsonResponse({
        run_id: upload[1],
        slot: "attachment-0.pdf",
        sha256: DIGEST,
        size: 5,
        run_state: "reserved",
        idempotent: false,
      });
    }
    const runStart = url.match(/^\/api\/runs\/(run_(poster|deck|landing|video))\/start$/);
    if (runStart) {
      starts.push(runStart[2]);
      return jsonResponse({
        run_id: runStart[1],
        placeholder_message: {
          id: `msg_${runStart[1]}`,
          role: "assistant",
          text: "",
          ts: 1,
          status: "streaming",
        },
      });
    }
    const artifact = url.match(/^\/api\/runs\/run_(poster|deck|landing|video)\/artifact$/);
    if (artifact) {
      const artifactType = artifact[1] as ArtifactType;
      return jsonResponse({
        message: {
          id: `msg_run_${artifactType}`,
          role: "assistant",
          text: "Done",
          ts: 2,
          run_id: `run_${artifactType}`,
          artifact_id: `art_${artifactType}`,
          status: "done",
        },
        artifact: {
          artifact_id: `art_${artifactType}`,
          name: artifactType,
          artifact_type: artifactType,
          canvas: { w: 1280, h: 720 },
          layers: [],
        },
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await waitFor(() => !!releaseCreate, "parent create did not enter flight");
  await useApp.getState().cancelPaperBundleTask("bundle", "poster");
  releaseCreate(jsonResponse({ ...bundleResponse(), job_id: jobId, reused: false }));
  await waitFor(() => starts.length >= 3, "the uncancelled children did not start");
  for (const source of MockEventSource.instances) source.emit("run.done");
  await start;

  const bundle = useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState;
  assert.equal(childCancels, 1);
  assert.deepEqual(uploads.sort(), ["deck", "landing", "video"]);
  assert.deepEqual(starts.sort(), ["deck", "landing", "video"]);
  assert.equal(bundle.tasks.poster.run_id, "run_poster");
  assert.equal(bundle.tasks.poster.status, "cancelled");
  assert.ok([bundle.tasks.deck, bundle.tasks.landing, bundle.tasks.video]
    .every((task) => task.status === "complete"));
});

test("recovery rebuilds a lost backend bundle and streams every active child", async () => {
  resetStore({ blank: conversation("blank") }, "blank");
  useApp.setState((state) => ({
    backend_info: state.backend_info
      ? { ...state.backend_info, user_isolation: false }
      : state.backend_info,
  }));
  const record = {
    ...bundleResponse({
      poster: ["running", false, false],
      deck: ["reserved", false, true],
      landing: ["cancelling", false, false],
      video: ["completed", true, true],
    }, false),
    state: "cancelling",
    terminal: false,
    terminal_at: null,
    cancel_requested: true,
    cancel_requested_at: 2,
    owner_id: "local",
  };
  globalThis.fetch = (async (input) => {
    assert.equal(String(input), "/api/paper-bundles");
    return jsonResponse([record]);
  }) as typeof fetch;

  await useApp.getState().recoverPaperBundles();

  const parent = useApp.getState().conversations.bundle;
  assert.equal(parent.paper_bundle?.kind, "parent");
  const bundle = parent.paper_bundle as PaperBundleParentState;
  assert.equal(bundle.job_id, "job_bundle");
  assert.equal(bundle.tasks.poster.status, "running");
  assert.equal(bundle.tasks.deck.status, "uploading");
  assert.match(bundle.tasks.deck.error ?? "", /interrupted/i);
  assert.equal(bundle.tasks.landing.status, "cancelling");
  assert.equal(bundle.tasks.video.status, "complete");
  assert.deepEqual(
    MockEventSource.instances.map((source) => source.url),
    [
      "/api/runs/run_poster/events",
      "/api/runs/run_deck/events",
      "/api/runs/run_landing/events",
    ],
  );
});

test("late recovery cannot downgrade a newer same-job terminal revision", async () => {
  seedBackendBundle();
  let releaseList!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    assert.equal(String(input), "/api/paper-bundles");
    return new Promise<Response>((resolve) => { releaseList = resolve; });
  }) as typeof fetch;

  const recovery = useApp.getState().recoverPaperBundles();
  await waitFor(() => !!releaseList, "paper bundle list did not enter flight");
  useApp.setState((state) => {
    const parent = state.conversations.bundle;
    const bundle = parent.paper_bundle as PaperBundleParentState;
    const tasks = Object.fromEntries(PAPER_BUNDLE_ARTIFACT_ORDER.map((artifactType) => [
      artifactType,
      {
        ...bundle.tasks[artifactType],
        status: "cancelled" as const,
        terminal: true,
        process_free: true,
        error: "Run cancelled.",
      },
    ])) as PaperBundleParentState["tasks"];
    return {
      conversations: {
        ...state.conversations,
        bundle: {
          ...parent,
          pending: false,
          paper_bundle: { ...bundle, revision: 5, backend_state: "cancelled" as const, tasks },
        },
      },
    };
  });
  releaseList(jsonResponse([{
    ...bundleResponse({}, false),
    revision: 2,
    state: "running",
  }]));
  await recovery;

  const bundle = useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState;
  assert.equal(bundle.revision, 5);
  assert.equal(bundle.backend_state, "cancelled");
  assert.ok(Object.values(bundle.tasks).every((task) => task.status === "cancelled"));
});

test("late recovery cannot replace a newer parent job identity", async () => {
  seedBackendBundle();
  let releaseList!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    assert.equal(String(input), "/api/paper-bundles");
    return new Promise<Response>((resolve) => { releaseList = resolve; });
  }) as typeof fetch;

  const recovery = useApp.getState().recoverPaperBundles();
  await waitFor(() => !!releaseList, "paper bundle list did not enter flight");
  useApp.setState((state) => {
    const parent = state.conversations.bundle;
    const bundle = parent.paper_bundle as PaperBundleParentState;
    const tasks = Object.fromEntries(PAPER_BUNDLE_ARTIFACT_ORDER.map((artifactType) => [
      artifactType,
      {
        ...bundle.tasks[artifactType],
        status: "cancelled" as const,
        terminal: true,
        process_free: true,
        error: "Run cancelled.",
      },
    ])) as PaperBundleParentState["tasks"];
    return {
      conversations: {
        ...state.conversations,
        bundle: {
          ...parent,
          pending: false,
          paper_bundle: {
            ...bundle,
            job_id: "job_new",
            revision: 5,
            backend_state: "cancelled" as const,
            tasks,
          },
        },
      },
    };
  });
  releaseList(jsonResponse([bundleResponse({}, false)]));
  await recovery;

  const bundle = useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState;
  assert.equal(bundle.job_id, "job_new");
  assert.equal(bundle.revision, 5);
  assert.equal(bundle.backend_state, "cancelled");
});

test("late recovery from an old owner cannot hydrate the new owner scope", async () => {
  resetStore({ blank: conversation("blank") }, "blank");
  let releaseList!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    assert.equal(String(input), "/api/paper-bundles");
    return new Promise<Response>((resolve) => { releaseList = resolve; });
  }) as typeof fetch;

  const recovery = useApp.getState().recoverPaperBundles();
  await waitFor(() => !!releaseList, "paper bundle list did not enter flight");
  try {
    localStorage.setItem("autodesign.demo_user.v1", "new-user");
    releaseList(jsonResponse([bundleResponse({}, false)]));
    await recovery;

    assert.equal(useApp.getState().conversations.bundle, undefined);
  } finally {
    localStorage.setItem("autodesign.demo_user.v1", "test-user");
  }
});

test("late create acknowledgement from the old owner cannot start child work", async () => {
  const parentId = "owner-create-parent";
  resetStore({ [parentId]: conversation(parentId) }, parentId);
  let jobId = "";
  let releaseCreate!: (response: Response) => void;
  let childWork = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        user_isolation: true,
        needs_setup: false,
      });
    }
    if (url === "/api/paper-bundles") {
      jobId = JSON.parse(String(init?.body)).job_id;
      return new Promise<Response>((resolve) => { releaseCreate = resolve; });
    }
    if (url.includes("/inputs/") || url.endsWith("/start")) {
      childWork += 1;
      throw new Error("old-owner child work reached transport");
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  try {
    const start = useApp.getState().startPaperBundle(
      new File(["paper"], "paper.pdf", { type: "application/pdf" }),
      parentId,
    );
    await waitFor(() => !!releaseCreate, "parent create did not enter flight");
    localStorage.setItem("autodesign.demo_user.v1", "new-owner");
    releaseCreate(jsonResponse({
      ...bundleResponseFor(jobId, parentId, "test-user"),
      reused: false,
    }));
    await start;

    const bundle = useApp.getState().conversations[parentId].paper_bundle as PaperBundleParentState;
    assert.equal(childWork, 0);
    assert.equal(bundle.revision, undefined);
    assert.ok(Object.values(bundle.tasks).every((task) => task.run_id === undefined));
  } finally {
    localStorage.setItem("autodesign.demo_user.v1", "test-user");
  }
});

test("late create failure cannot overwrite a replacement job for the same owner", async () => {
  const parentId = "replacement-create-parent";
  resetStore({ [parentId]: conversation(parentId) }, parentId);
  let releaseCreate!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/paper-bundles") {
      return new Promise<Response>((resolve) => { releaseCreate = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
    parentId,
  );
  await waitFor(() => !!releaseCreate, "parent create did not enter flight");

  const replacement = createPaperBundleParentState(parentId, "replacement.pdf");
  replacement.job_id = "replacement-job";
  replacement.backend_state = "running";
  for (const task of Object.values(replacement.tasks)) {
    task.status = "running";
  }
  useApp.setState((state) => ({
    conversations: {
      ...state.conversations,
      [parentId]: conversation(parentId, {
        paper_bundle: replacement,
        pending: true,
      }),
    },
  }));

  releaseCreate(jsonResponse({ invalid: true }));
  await start;

  const bundle = useApp.getState().conversations[parentId].paper_bundle as PaperBundleParentState;
  assert.equal(bundle.job_id, "replacement-job");
  assert.equal(bundle.backend_state, "running");
  assert.ok(Object.values(bundle.tasks).every((task) => task.status === "running"));
});

test("late setup failure from an old same-run job cannot abort or rewrite the replacement job", async () => {
  const parentId = "late-child-failure-parent";
  resetStore({ [parentId]: conversation(parentId) }, parentId);
  let createCalls = 0;
  let releaseOldPosterUpload!: (response: Response) => void;
  let oldPosterUploadRequested = false;
  let posterUploadCalls = 0;
  const jobIds: string[] = [];
  const startedRuns: string[] = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/paper-bundles") {
      createCalls += 1;
      const jobId = JSON.parse(String(init?.body)).job_id as string;
      jobIds.push(jobId);
      return jsonResponse({
        ...bundleResponseFor(jobId, parentId, "test-user"),
        reused: false,
      });
    }
    const upload = url.match(/^\/api\/runs\/(run_(poster|deck|landing|video))\/inputs\/attachment-0\.pdf$/);
    if (upload) {
      if (upload[2] === "poster") {
        posterUploadCalls += 1;
        if (posterUploadCalls === 1) {
          oldPosterUploadRequested = true;
          return new Promise<Response>((resolve) => { releaseOldPosterUpload = resolve; });
        }
      }
      return jsonResponse({
        run_id: upload[1],
        slot: "attachment-0.pdf",
        sha256: DIGEST,
        size: 5,
        run_state: "reserved",
        idempotent: false,
      });
    }
    const start = url.match(/^\/api\/runs\/(run_(poster|deck|landing|video))\/start$/);
    if (start) {
      startedRuns.push(start[1]);
      return jsonResponse({
        run_id: start[1],
        placeholder_message: {
          id: `server_${start[1]}`,
          role: "assistant",
          text: "",
          ts: 1,
          status: "streaming",
        },
      });
    }
    const artifact = url.match(/^\/api\/runs\/(run_(poster|deck|landing|video))\/artifact$/);
    if (artifact) {
      return jsonResponse({
        message: {
          id: `msg_${artifact[1]}`,
          role: "assistant",
          text: "Run cancelled.",
          ts: 2,
          run_id: artifact[1],
          status: "error",
          failure: { status: "cancelled", produced_files: [], artifact_type: artifact[2] },
        },
        artifact: null,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const oldStart = useApp.getState().startPaperBundle(
    new File(["paper"], "old.pdf", { type: "application/pdf" }),
    parentId,
  );
  await waitFor(
    () => oldPosterUploadRequested && startedRuns.length === 3,
    "old poster upload did not remain in flight",
  );

  useApp.setState({
    conversations: { [parentId]: conversation(parentId) },
    current_conversation_id: parentId,
    history_user_scope: "test-user",
    runs_progress: {},
    settings_open: false,
  });
  const replacementStart = useApp.getState().startPaperBundle(
    new File(["paper"], "replacement.pdf", { type: "application/pdf" }),
    parentId,
  );
  await waitFor(
    () => startedRuns.length === 7 && MockEventSource.instances.length === 8,
    "replacement child runs did not start",
  );

  const posterChildId = `${parentId}:paper-bundle:poster`;
  const replacementParentBefore = useApp.getState().conversations[parentId];
  const replacementBundleBefore = replacementParentBefore.paper_bundle as PaperBundleParentState;
  const replacementBefore = useApp.getState().conversations[posterChildId];
  const replacementProgressBefore = useApp.getState().runs_progress[posterChildId];
  const replacementPlaceholderBefore = replacementBefore.messages.at(-1);
  const replacementPosterStream = MockEventSource.instances[4];
  assert.equal(createCalls, 2);
  assert.notEqual(jobIds[0], jobIds[1]);
  assert.equal(replacementBundleBefore.job_id, jobIds[1]);
  assert.equal(replacementBundleBefore.tasks.poster.run_id, "run_poster");
  assert.equal(replacementProgressBefore?.run_id, "run_poster");

  releaseOldPosterUpload(jsonResponse({
    detail: { code: "no_api_key", message: "old job needs setup" },
  }, 412));
  await oldStart;

  const state = useApp.getState();
  const parent = state.conversations[parentId];
  const bundle = parent.paper_bundle as PaperBundleParentState;
  const task = bundle.tasks.poster;
  const replacement = state.conversations[posterChildId];
  assert.equal(bundle.job_id, jobIds[1]);
  assert.equal(bundle.backend_state, "running");
  assert.equal(parent.pending, true);
  assert.equal(task.run_id, "run_poster");
  assert.equal(task.status, "running");
  assert.equal(task.terminal, false);
  assert.equal(replacement.pending, true);
  assert.equal(replacement.run_id, "run_poster");
  assert.strictEqual(replacement.messages.at(-1), replacementPlaceholderBefore);
  assert.equal(replacement.messages.at(-1)?.status, "streaming");
  assert.strictEqual(state.runs_progress[posterChildId], replacementProgressBefore);
  assert.equal(state.runs_progress[posterChildId]?.run_id, "run_poster");
  assert.equal(state.settings_open, false);
  assert.notEqual(replacementPosterStream.readyState, MockEventSource.CLOSED);

  for (const source of MockEventSource.instances.slice(4)) source.emit("run.cancelled");
  await replacementStart;
});

test("late child success from an old job cannot finalize the replacement job", async () => {
  const parentId = "late-child-success-parent";
  resetStore({ [parentId]: conversation(parentId) }, parentId);
  let createCalls = 0;
  let releaseOldPosterArtifact!: (response: Response) => void;
  let oldPosterArtifactRequested = false;
  let replacementPosterArtifactRequested = false;
  let replacementPosterArtifactSignal: AbortSignal | null = null;
  let posterArtifactCalls = 0;
  const jobIds: string[] = [];
  const startedRuns: string[] = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/paper-bundles") {
      createCalls += 1;
      const jobId = JSON.parse(String(init?.body)).job_id as string;
      jobIds.push(jobId);
      return jsonResponse({ ...bundleResponseFor(jobId, parentId, "test-user"), reused: false });
    }
    const upload = url.match(/^\/api\/runs\/(run_(poster|deck|landing|video))\/inputs\/attachment-0\.pdf$/);
    if (upload) {
      return jsonResponse({
        run_id: upload[1],
        slot: "attachment-0.pdf",
        sha256: DIGEST,
        size: 5,
        run_state: "reserved",
        idempotent: false,
      });
    }
    const start = url.match(/^\/api\/runs\/(run_(poster|deck|landing|video))\/start$/);
    if (start) {
      startedRuns.push(start[1]);
      return jsonResponse({
        run_id: start[1],
        placeholder_message: {
          id: `server_${start[1]}`,
          role: "assistant",
          text: "",
          ts: 1,
          status: "streaming",
        },
      });
    }
    if (url === "/api/runs/run_poster/artifact") {
      posterArtifactCalls += 1;
      if (posterArtifactCalls === 1) {
        oldPosterArtifactRequested = true;
        return new Promise<Response>((resolve) => { releaseOldPosterArtifact = resolve; });
      }
      if (posterArtifactCalls === 2) {
        replacementPosterArtifactRequested = true;
        replacementPosterArtifactSignal = init?.signal ?? null;
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener(
            "abort",
            () => reject(init.signal?.reason ?? new Error("replacement artifact fetch aborted")),
            { once: true },
          );
        });
      }
    }
    if (url === "/api/runs/run_poster/cancel") {
      return jsonResponse({
        run_id: "run_poster",
        status: "cancelled",
        run_state: "cancelled",
        confirmed: true,
        terminated_pids: [],
        surviving_pids: [],
      });
    }
    const artifact = url.match(/^\/api\/runs\/run_(poster|deck|landing|video)\/artifact$/);
    if (artifact) {
      return jsonResponse({
        message: {
          id: `msg_cleanup_${artifact[1]}`,
          role: "assistant",
          text: "Run cancelled.",
          ts: 3,
          run_id: `run_${artifact[1]}`,
          status: "error",
          failure: { status: "cancelled", produced_files: [], artifact_type: artifact[1] },
        },
        artifact: null,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const oldStart = useApp.getState().startPaperBundle(
    new File(["paper"], "old.pdf", { type: "application/pdf" }),
    parentId,
  );
  await waitFor(() => startedRuns.length === 4, "old child runs did not start");
  MockEventSource.instances[0].emit("run.done");
  await waitFor(() => oldPosterArtifactRequested, "old poster artifact read did not start");

  useApp.setState({
    conversations: { [parentId]: conversation(parentId) },
    current_conversation_id: parentId,
    history_user_scope: "test-user",
    runs_progress: {},
    settings_open: false,
  });
  const replacementStart = useApp.getState().startPaperBundle(
    new File(["paper"], "replacement.pdf", { type: "application/pdf" }),
    parentId,
  );
  await waitFor(
    () => startedRuns.length === 8 && MockEventSource.instances.length === 8,
    "replacement child runs did not start",
  );
  const posterChildId = `${parentId}:paper-bundle:poster`;
  const replacementBefore = useApp.getState().conversations[posterChildId];
  const replacementPlaceholderBefore = replacementBefore.messages.at(-1);
  MockEventSource.instances[4].emit("run.done");
  await waitFor(
    () => replacementPosterArtifactRequested,
    "replacement poster artifact read did not start",
  );
  const replacementProgressBefore = useApp.getState().runs_progress[posterChildId];

  releaseOldPosterArtifact(jsonResponse({
    message: {
      id: "msg_old_poster_success",
      role: "assistant",
      text: "Old poster done",
      ts: 2,
      run_id: "run_poster",
      artifact_id: "art_old_poster",
      status: "done",
    },
    artifact: {
      artifact_id: "art_old_poster",
      name: "Old poster",
      artifact_type: "poster",
      canvas: { w: 1280, h: 720 },
      layers: [],
    },
  }));
  await oldStart;

  const parent = useApp.getState().conversations[parentId];
  const replacement = useApp.getState().conversations[`${parentId}:paper-bundle:poster`];
  const bundle = parent.paper_bundle as PaperBundleParentState;
  const task = bundle.tasks.poster;
  assert.equal(createCalls, 2);
  assert.notEqual(jobIds[0], jobIds[1]);
  assert.equal(bundle.job_id, jobIds[1]);
  assert.equal((parent.paper_bundle as PaperBundleParentState).backend_state, "running");
  assert.equal(parent.pending, true);
  assert.equal(task.status, "running");
  assert.equal(task.terminal, false);
  assert.equal(task.run_id, "run_poster");
  assert.equal(replacement.run_id, "run_poster");
  assert.equal(replacement.pending, true);
  assert.strictEqual(replacement.messages.at(-1), replacementPlaceholderBefore);
  assert.equal(replacement.messages.at(-1)?.status, "streaming");
  assert.strictEqual(useApp.getState().runs_progress[posterChildId], replacementProgressBefore);
  assert.equal(useApp.getState().runs_progress[posterChildId]?.run_id, "run_poster");
  assert.equal(replacementPosterArtifactSignal?.aborted, false);
  assert.equal(useApp.getState().settings_open, false);
  assert.equal(parent.artifacts.art_old_poster, undefined);
  assert.notEqual(MockEventSource.instances[5].readyState, MockEventSource.CLOSED);

  await useApp.getState().cancelPaperBundleTask(parentId, "poster");
  assert.equal(replacementPosterArtifactSignal?.aborted, true);
  for (const source of MockEventSource.instances.slice(5)) source.emit("run.cancelled");
  await replacementStart;
});

test("late confirmed parent cancellation from the old owner is ignored", async () => {
  seedBackendBundle();
  let releaseCancel!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    assert.equal(String(input), "/api/paper-bundles/job_bundle/cancel");
    return new Promise<Response>((resolve) => { releaseCancel = resolve; });
  }) as typeof fetch;

  try {
    const cancel = useApp.getState().cancelPaperBundle("bundle");
    await waitFor(() => !!releaseCancel, "parent cancellation did not enter flight");
    localStorage.setItem("autodesign.demo_user.v1", "new-owner");
    releaseCancel(jsonResponse({
      ...bundleResponseFor("job_bundle", "bundle", "test-user", {
        poster: ["cancelled", true, true],
        deck: ["cancelled", true, true],
        landing: ["cancelled", true, true],
        video: ["cancelled", true, true],
      }, false),
      status: "cancelled",
      confirmed: true,
    }));
    await cancel;

    const parent = useApp.getState().conversations.bundle;
    const bundle = parent.paper_bundle as PaperBundleParentState;
    assert.equal(bundle.backend_state, "cancelling");
    assert.equal(bundle.revision, 1);
    assert.equal(parent.pending, true);
  } finally {
    localStorage.setItem("autodesign.demo_user.v1", "test-user");
  }
});

test("wrong-owner parent cancellation stays live and exposes retry", async () => {
  seedBackendBundle();
  globalThis.fetch = (async (input) => {
    assert.equal(String(input), "/api/paper-bundles/job_bundle/cancel");
    return jsonResponse({
      ...bundleResponseFor("job_bundle", "bundle", "different-owner", {
        poster: ["cancelled", true, true],
        deck: ["cancelled", true, true],
        landing: ["cancelled", true, true],
        video: ["cancelled", true, true],
      }, false),
      status: "cancelled",
      confirmed: true,
    });
  }) as typeof fetch;

  await useApp.getState().cancelPaperBundle("bundle");

  const parent = useApp.getState().conversations.bundle;
  const bundle = parent.paper_bundle as PaperBundleParentState;
  assert.equal(bundle.backend_state, "cancelling");
  assert.equal(bundle.revision, 1);
  assert.equal(parent.pending, true);
  assert.match(bundle.cancel_error ?? "", /owner/i);
});

test("late single-child cancellation from the old owner is ignored", async () => {
  seedBackendBundle();
  let releaseCancel!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    assert.equal(String(input), "/api/runs/run_deck/cancel");
    return new Promise<Response>((resolve) => { releaseCancel = resolve; });
  }) as typeof fetch;

  try {
    const cancel = useApp.getState().cancelPaperBundleTask("bundle", "deck");
    await waitFor(() => !!releaseCancel, "child cancellation did not enter flight");
    localStorage.setItem("autodesign.demo_user.v1", "new-owner");
    releaseCancel(jsonResponse({
      run_id: "run_deck",
      status: "cancelled",
      run_state: "cancelled",
      confirmed: true,
      terminated_pids: [],
      surviving_pids: [],
    }));
    await cancel;

    const parent = useApp.getState().conversations.bundle;
    const bundle = parent.paper_bundle as PaperBundleParentState;
    assert.equal(bundle.tasks.deck.status, "cancelling");
    assert.equal(bundle.tasks.deck.run_id, "run_deck");
    assert.equal(parent.pending, true);
  } finally {
    localStorage.setItem("autodesign.demo_user.v1", "test-user");
  }
});

test("late cancellation poll from the old owner is ignored", async () => {
  seedBackendBundle();
  let releasePoll!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/paper-bundles/job_bundle/cancel") {
      return jsonResponse({
        job_id: "job_bundle",
        state: "cancelling",
        status: "cancellation_pending",
        confirmed: false,
        children: {},
      }, 202);
    }
    if (url === "/api/paper-bundles/job_bundle") {
      return new Promise<Response>((resolve) => { releasePoll = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  try {
    const cancel = useApp.getState().cancelPaperBundle("bundle");
    await waitFor(() => !!releasePoll, "parent cancellation poll did not enter flight");
    localStorage.setItem("autodesign.demo_user.v1", "new-owner");
    releasePoll(jsonResponse(bundleResponseFor("job_bundle", "bundle", "test-user", {
      poster: ["cancelled", true, true],
      deck: ["cancelled", true, true],
      landing: ["cancelled", true, true],
      video: ["cancelled", true, true],
    }, false)));
    await cancel;

    const parent = useApp.getState().conversations.bundle;
    const bundle = parent.paper_bundle as PaperBundleParentState;
    assert.equal(bundle.backend_state, "cancelling");
    assert.equal(bundle.revision, 1);
    assert.equal(parent.pending, true);
  } finally {
    localStorage.setItem("autodesign.demo_user.v1", "test-user");
  }
});

test("pre-ID child cancellation intent cannot cross job generation for the same owner", async () => {
  const parentId = "owner-intent-parent";
  resetStore({ [parentId]: conversation(parentId) }, parentId);
  let oldJobId = "";
  let newJobId = "";
  let createCalls = 0;
  let releaseOldCreate!: (response: Response) => void;
  let posterCancels = 0;
  const uploads: string[] = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        user_isolation: true,
        needs_setup: false,
      });
    }
    if (url === "/api/paper-bundles") {
      createCalls += 1;
      const body = JSON.parse(String(init?.body));
      if (createCalls === 1) {
        oldJobId = body.job_id;
        return new Promise<Response>((resolve) => { releaseOldCreate = resolve; });
      }
      newJobId = body.job_id;
      return jsonResponse({
        ...bundleResponseFor(newJobId, parentId, "test-user"),
        reused: false,
      });
    }
    if (url === "/api/runs/run_poster/cancel") {
      posterCancels += 1;
      return jsonResponse({
        run_id: "run_poster",
        status: "cancelled",
        run_state: "cancelled",
        confirmed: true,
        terminated_pids: [],
        surviving_pids: [],
      });
    }
    const upload = url.match(/^\/api\/runs\/run_(poster|deck|landing|video)\/inputs\/attachment-0\.pdf$/);
    if (upload) {
      uploads.push(upload[1]);
      throw new Error("stop after observing upload eligibility");
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  try {
    const oldStart = useApp.getState().startPaperBundle(
      new File(["paper"], "old.pdf", { type: "application/pdf" }),
      parentId,
    );
    await waitFor(() => !!releaseOldCreate, "old-owner create did not enter flight");
    await useApp.getState().cancelPaperBundleTask(parentId, "poster");

    useApp.setState({
      conversations: { [parentId]: conversation(parentId) },
      current_conversation_id: parentId,
      history_user_scope: "test-user",
      runs_progress: {},
    });
    const newStart = useApp.getState().startPaperBundle(
      new File(["paper"], "new.pdf", { type: "application/pdf" }),
      parentId,
    );
    await newStart;
    releaseOldCreate(jsonResponse({
      ...bundleResponseFor(oldJobId, parentId, "test-user"),
      reused: false,
    }));
    await oldStart;

    const bundle = useApp.getState().conversations[parentId].paper_bundle as PaperBundleParentState;
    assert.notEqual(newJobId, oldJobId);
    assert.equal(posterCancels, 0);
    assert.deepEqual(uploads.sort(), ["deck", "landing", "poster", "video"]);
    assert.notEqual(bundle.tasks.poster.status, "cancelled");
  } finally {
    localStorage.setItem("autodesign.demo_user.v1", "test-user");
  }
});
