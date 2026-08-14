import assert from "node:assert/strict";
import test from "node:test";
import type { Artifact } from "./types.ts";

const memoryStorage = (() => {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
    clear: () => { values.clear(); },
    key: (index: number) => [...values.keys()][index] ?? null,
    get length() { return values.size; },
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

const api = await import("./api.ts");
const { applyEvent, initialProgress } = await import("./progress.ts");
const { useApp } = await import("./store.ts");

const jsonResponse = (body: unknown, status = 200): Response => new Response(
  JSON.stringify(body),
  { status, headers: { "Content-Type": "application/json" } },
);

async function waitFor(predicate: () => boolean, message: string) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  }
  assert.fail(message);
}

class ReconnectingEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static instances: ReconnectingEventSource[] = [];

  readonly url: string;
  readyState = ReconnectingEventSource.OPEN;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    ReconnectingEventSource.instances.push(this);
  }

  close() { this.readyState = ReconnectingEventSource.CLOSED; }

  disconnect() {
    this.readyState = ReconnectingEventSource.CLOSED;
    this.onerror?.();
  }

  emit(payload: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
}

const runStatusResponse = (
  runId: string,
  runState: string,
  terminalEvent: string | null = null,
) => ({
  run_id: runId,
  run_state: runState,
  revision: 1,
  publishable: runState === "completed",
  cancellation_pending: runState === "cancelling" ? "worker_exit_pending" : null,
  worker_pid: null,
  terminal_event: terminalEvent,
});

const successfulRunResponse = (runId: string) => ({
  message: {
    id: `msg_${runId}`,
    role: "assistant" as const,
    text: "Done",
    ts: 2,
    run_id: runId,
    artifact_id: `art_${runId}`,
    status: "done" as const,
  },
  artifact: {
    artifact_id: `art_${runId}`,
    name: "Landing result",
    artifact_type: "landing" as const,
    canvas: { w: 1280, h: 720 },
    layers: [],
  },
});

async function startReconnectingOrdinaryRun(
  fetcher: typeof fetch,
) {
  const originalFetch = globalThis.fetch;
  const originalEventSource = globalThis.EventSource;
  ReconnectingEventSource.instances = [];
  globalThis.fetch = fetcher;
  Object.assign(globalThis, {
    EventSource: ReconnectingEventSource as unknown as typeof EventSource,
  });
  useApp.getState().newConversation();
  useApp.getState().setIntent("landing");
  const conversationId = useApp.getState().current_conversation_id;
  const sending = useApp.getState().sendMessage("Build a landing page", []);
  await waitFor(
    () => ReconnectingEventSource.instances.length >= 1,
    "ordinary run did not open its initial event stream",
  );
  return {
    conversationId,
    sending,
    restore: () => {
      globalThis.fetch = originalFetch;
      Object.assign(globalThis, { EventSource: originalEventSource });
    },
  };
}

test("ordinary file generation reserves before upload and exposes the run first", async () => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; init: RequestInit | undefined }> = [];
  const observed: string[] = [];
  const file = new File([new TextEncoder().encode("paper")], "../../Paper FINAL.PDF", {
    type: "application/pdf",
  });
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    if (url === "/api/runs/reserve") {
      return jsonResponse({
        run_id: "run_reserved",
        upload_token: "upload-token",
        input_slots: [{
          name: "attachment-0.pdf",
          role: "attachment",
          sha256: "382635c9325bf3273d195ff1b8a44e5b11afd7d97addeb8863ea35feb98c1a07",
          size: 5,
        }],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url.includes("/inputs/")) {
      observed.push("upload");
      return jsonResponse({
        run_id: "run_reserved",
        slot: "attachment-0.pdf",
        sha256: "382635c9325bf3273d195ff1b8a44e5b11afd7d97addeb8863ea35feb98c1a07",
        size: 5,
        run_state: "reserved",
        idempotent: false,
      });
    }
    if (url === "/api/runs/run_reserved/start") {
      return jsonResponse({
        run_id: "run_reserved",
        placeholder_message: {
          id: "msg_run_reserved",
          role: "assistant",
          text: "",
          ts: 1,
          status: "streaming",
        },
      });
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch;

  try {
    const ack = await api.startGenerate(
      {
        brief: "Make a poster",
        artifact_type: "poster",
        palette_id: "plum_sage",
        attachments: [{
          id: "attachment",
          name: "../../Paper FINAL.PDF",
          size: file.size,
          kind: "pdf",
          file,
        }],
      },
      undefined,
      {
        reserveUploads: true,
        onReserved: (runId: string) => observed.push(`reserved:${runId}`),
      },
    );

    assert.equal(ack.run_id, "run_reserved");
    assert.deepEqual(observed, ["reserved:run_reserved", "upload"]);
    assert.deepEqual(calls.map((call) => call.url), [
      "/api/runs/reserve",
      "/api/runs/run_reserved/inputs/attachment-0.pdf",
      "/api/runs/run_reserved/start",
    ]);
    const reserve = JSON.parse(String(calls[0].init?.body)) as {
      input_slots: Array<{ name: string; sha256: string; size: number }>;
    };
    assert.deepEqual(reserve.input_slots, [{
      name: "attachment-0.pdf",
      role: "attachment",
      sha256: "382635c9325bf3273d195ff1b8a44e5b11afd7d97addeb8863ea35feb98c1a07",
      size: 5,
    }]);
    assert.ok(new Headers(calls[0].init?.headers).get("Idempotency-Key"));
    assert.equal(
      new Headers(calls[1].init?.headers).get("X-Autodesign-Upload-Token"),
      "upload-token",
    );
    assert.equal(calls[1].init?.body, file);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("ordinary text generation reserves a zero-input run before start", async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  const observed: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    calls.push(url);
    if (url === "/api/runs/reserve") {
      return jsonResponse({
        run_id: "run_text_reserved",
        upload_token: "upload-token",
        input_slots: [],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === "/api/runs/run_text_reserved/start") {
      observed.push("start");
      return jsonResponse({
        run_id: "run_text_reserved",
        placeholder_message: {
          id: "msg_run_text_reserved",
          role: "assistant",
          text: "",
          ts: 1,
          status: "streaming",
        },
      });
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch;

  try {
    const ack = await api.startGenerate(
      {
        brief: "Make a landing page",
        artifact_type: "landing",
        attachments: [],
      },
      undefined,
      {
        reserveUploads: true,
        onReserved: (runId: string) => observed.push(`reserved:${runId}`),
      },
    );

    assert.equal(ack.run_id, "run_text_reserved");
    assert.deepEqual(calls, [
      "/api/runs/reserve",
      "/api/runs/run_text_reserved/start",
    ]);
    assert.deepEqual(observed, ["reserved:run_text_reserved", "start"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Workbench exposes a text-only reservation instead of using legacy generate", async () => {
  const originalFetch = globalThis.fetch;
  const originalEventSource = globalThis.EventSource;
  const calls: string[] = [];

  class FakeEventSource {
    static readonly CLOSED = 2;
    static readonly instances: FakeEventSource[] = [];
    readyState = 1;
    onmessage: ((event: MessageEvent<string>) => void) | null = null;
    onerror: (() => void) | null = null;

    constructor(readonly url: string) {
      FakeEventSource.instances.push(this);
    }

    close() { this.readyState = FakeEventSource.CLOSED; }
    emit(payload: Record<string, unknown>) {
      this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
    }
  }

  Object.assign(globalThis, {
    EventSource: FakeEventSource as unknown as typeof EventSource,
  });
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    calls.push(url);
    if (url === "/api/runs/reserve") {
      return jsonResponse({
        run_id: "run_text_workbench",
        upload_token: "upload-token",
        input_slots: [],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === "/api/runs/run_text_workbench/start") {
      return jsonResponse({
        run_id: "run_text_workbench",
        placeholder_message: {
          id: "msg_run_text_workbench",
          role: "assistant",
          text: "",
          ts: 1,
          status: "streaming",
        },
      });
    }
    if (url === "/api/generate") {
      return jsonResponse({
        run_id: "run_legacy_text",
        placeholder_message: {
          id: "msg_run_legacy_text",
          role: "assistant",
          text: "",
          ts: 1,
          status: "streaming",
        },
      });
    }
    if (url.endsWith("/artifact")) {
      const runId = url.includes("run_text_workbench")
        ? "run_text_workbench"
        : "run_legacy_text";
      return jsonResponse({
        message: {
          id: `msg_${runId}`,
          role: "assistant",
          text: "Run cancelled.",
          ts: 2,
          run_id: runId,
          status: "error",
          failure: { status: "cancelled", produced_files: [] },
        },
        artifact: null,
      });
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch;

  useApp.getState().newConversation();
  useApp.getState().setIntent("landing");
  const conversationId = useApp.getState().current_conversation_id;
  const sending = useApp.getState().sendMessage("Build a landing page", []);

  try {
    await waitFor(
      () => FakeEventSource.instances.length === 1,
      "text-only run did not expose an event stream",
    );
    assert.equal(useApp.getState().conversations[conversationId].run_id, "run_text_workbench");
    assert.equal(calls.filter((url) => url === "/api/runs/reserve").length, 1);
    assert.equal(calls.filter((url) => url === "/api/generate").length, 0);
  } finally {
    FakeEventSource.instances[0]?.emit({ event: "run.cancelled" });
    await sending;
    globalThis.fetch = originalFetch;
    Object.assign(globalThis, { EventSource: originalEventSource });
  }
});

test("closed ordinary SSE reconciles every durable nonterminal state without losing Canvas ownership", async () => {
  const runId = "run_durable_nonterminal";
  const originalWindowSetTimeout = window.setTimeout;
  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => (
    originalWindowSetTimeout(handler, timeout && timeout <= 5_000 ? 0 : timeout, ...args)
  )) as typeof window.setTimeout;
  const nonterminalStates = [
    "reserved",
    "uploading",
    "queued",
    "running",
    "completing",
    "cancelling",
  ] as const;
  let statusReads = 0;
  let artifactReads = 0;
  const context = await startReconnectingOrdinaryRun((async (input) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      return jsonResponse({
        run_id: runId,
        upload_token: "upload-token",
        input_slots: [],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      return jsonResponse({
        run_id: runId,
        placeholder_message: {
          id: `msg_${runId}`,
          role: "assistant",
          text: "",
          ts: 1,
          status: "streaming",
        },
      });
    }
    if (url === `/api/runs/${runId}/status`) {
      const state = nonterminalStates[statusReads];
      statusReads += 1;
      return jsonResponse(runStatusResponse(
        runId,
        state,
        state === "running" ? "run.done" : null,
      ));
    }
    if (url === `/api/runs/${runId}/artifact`) {
      artifactReads += 1;
      return jsonResponse(successfulRunResponse(runId));
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch);

  const draft: Artifact = {
    ...successfulRunResponse("draft").artifact,
    artifact_id: "art_durable_draft",
    candidate_draft: true,
    attempt_lineage: {
      source_run_id: runId,
      source_attempt: 1,
      source_candidate_id: "landing-attempt-01",
      source_candidate_sha256: "a".repeat(64),
    },
  };
  useApp.setState((state) => ({
    conversations: {
      ...state.conversations,
      [context.conversationId]: {
        ...state.conversations[context.conversationId],
        artifacts: { [draft.artifact_id]: draft },
        active_artifact_id: draft.artifact_id,
      },
    },
  }));

  try {
    for (const [index, durableState] of nonterminalStates.entries()) {
      ReconnectingEventSource.instances[index].disconnect();
      await waitFor(
        () => statusReads === index + 1
          && ReconnectingEventSource.instances.length === index + 2,
        `ordinary run did not reconnect from ${durableState}`,
      );
      const state = useApp.getState();
      assert.equal(state.conversations[context.conversationId].pending, true);
      assert.equal(state.conversations[context.conversationId].run_id, runId);
      assert.equal(
        state.conversations[context.conversationId].active_artifact_id,
        draft.artifact_id,
      );
      const progress = state.runs_progress[context.conversationId];
      if (durableState === "cancelling") assert.equal(progress.phase, "cancelling");
      else assert.notEqual(progress.phase, "error");
      assert.match(progress.label, durableState === "cancelling" ? /stopping/i : /reconnect/i);
    }

    ReconnectingEventSource.instances.at(-1)?.emit({
      event: "run.done",
      event_id: "terminal-durable",
    });
    await context.sending;
    assert.equal(artifactReads, 1);
    assert.equal(
      useApp.getState().conversations[context.conversationId].active_artifact_id,
      draft.artifact_id,
    );
  } finally {
    window.setTimeout = originalWindowSetTimeout;
    context.restore();
  }
});

test("ordinary transport timeout reconciles durable state instead of failing the run", async () => {
  const runId = "run_transport_timeout";
  const originalWindowSetTimeout = window.setTimeout;
  const originalWindowClearTimeout = window.clearTimeout;
  let transportDeadline: (() => void) | undefined;
  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    if (timeout === 60 * 60 * 1000 && typeof handler === "function") {
      transportDeadline = () => handler(...args);
      return 123_456;
    }
    return originalWindowSetTimeout(
      handler,
      timeout && timeout <= 5_000 ? 0 : timeout,
      ...args,
    );
  }) as typeof window.setTimeout;
  window.clearTimeout = ((timer?: number) => {
    if (timer !== 123_456) originalWindowClearTimeout(timer);
  }) as typeof window.clearTimeout;
  let statusReads = 0;
  const context = await startReconnectingOrdinaryRun((async (input) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      return jsonResponse({
        run_id: runId,
        upload_token: "upload-token",
        input_slots: [],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      return jsonResponse({
        run_id: runId,
        placeholder_message: { id: "placeholder", role: "assistant", text: "", ts: 1 },
      });
    }
    if (url === `/api/runs/${runId}/status`) {
      statusReads += 1;
      return jsonResponse(runStatusResponse(runId, "running"));
    }
    if (url === `/api/runs/${runId}/artifact`) {
      return jsonResponse(successfulRunResponse(runId));
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch);

  try {
    assert.ok(transportDeadline, "one-hour transport watchdog was not installed");
    transportDeadline();
    await waitFor(
      () => statusReads === 1 && ReconnectingEventSource.instances.length === 2,
      "transport deadline did not reconcile and reopen SSE",
    );
    assert.equal(useApp.getState().conversations[context.conversationId].pending, true);
    assert.match(useApp.getState().runs_progress[context.conversationId].label, /reconnect/i);
    ReconnectingEventSource.instances.at(-1)?.emit({
      event: "run.done",
      event_id: "terminal-timeout",
    });
    await context.sending;
  } finally {
    window.setTimeout = originalWindowSetTimeout;
    window.clearTimeout = originalWindowClearTimeout;
    context.restore();
  }
});

test("ordinary replay deduplicates progress and concurrent terminal signals settle once", async () => {
  const runId = "run_replay";
  const originalWindowSetTimeout = window.setTimeout;
  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => (
    originalWindowSetTimeout(handler, timeout && timeout <= 5_000 ? 0 : timeout, ...args)
  )) as typeof window.setTimeout;
  let statusReads = 0;
  let releaseStatus!: (response: Response) => void;
  let artifactReads = 0;
  const context = await startReconnectingOrdinaryRun((async (input) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      return jsonResponse({
        run_id: runId,
        upload_token: "upload-token",
        input_slots: [],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      return jsonResponse({
        run_id: runId,
        placeholder_message: { id: "placeholder", role: "assistant", text: "", ts: 1 },
      });
    }
    if (url === `/api/runs/${runId}/status`) {
      statusReads += 1;
      if (statusReads === 1) {
        return jsonResponse(runStatusResponse(runId, "running"));
      }
      return new Promise<Response>((resolve) => { releaseStatus = resolve; });
    }
    if (url === `/api/runs/${runId}/artifact`) {
      artifactReads += 1;
      return artifactReads === 1
        ? jsonResponse({ detail: "artifact commit pending" }, 504)
        : jsonResponse(successfulRunResponse(runId));
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch);

  try {
    const source = ReconnectingEventSource.instances[0];
    source.emit({ event: "prompt.enhance.error", event_id: "warning-1" });
    source.emit({
      event: "landing_author.attempt_start",
      event_id: "attempt-1",
      attempt: 1,
      max_attempts: 4,
    });
    source.disconnect();
    await waitFor(
      () => statusReads === 1 && ReconnectingEventSource.instances.length === 2,
      "closed stream did not reopen for replay",
    );
    const replay = ReconnectingEventSource.instances[1];
    replay.emit({ event: "prompt.enhance.error", event_id: "warning-1" });
    replay.emit({
      event: "landing_author.attempt_start",
      event_id: "attempt-1",
      attempt: 1,
      max_attempts: 4,
    });
    assert.equal(useApp.getState().runs_progress[context.conversationId].counts.warnings, 1);
    assert.equal(useApp.getState().runs_progress[context.conversationId].counts.attempts, 1);

    replay.disconnect();
    await waitFor(() => statusReads === 2, "durable status reconciliation did not start");
    replay.emit({ event: "run.done", event_id: "terminal-race" });
    replay.emit({ event: "run.done", event_id: "terminal-race" });
    releaseStatus(jsonResponse(runStatusResponse(runId, "completed", "run.done")));
    await context.sending;

    assert.equal(artifactReads, 2);
    assert.equal(useApp.getState().conversations[context.conversationId].pending, false);
    assert.equal(
      useApp.getState().conversations[context.conversationId].active_artifact_id,
      `art_${runId}`,
    );
  } finally {
    window.setTimeout = originalWindowSetTimeout;
    context.restore();
  }
});

test("confirmed cancellation aborts an in-flight durable status reconciliation", async () => {
  const runId = "run_cancel_during_status";
  let statusSignal: AbortSignal | undefined;
  let statusStarted = false;
  let artifactReads = 0;
  const context = await startReconnectingOrdinaryRun((async (input, init) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      return jsonResponse({
        run_id: runId,
        upload_token: "upload-token",
        input_slots: [],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      return jsonResponse({
        run_id: runId,
        placeholder_message: { id: "placeholder", role: "assistant", text: "", ts: 1 },
      });
    }
    if (url === `/api/runs/${runId}/status`) {
      statusStarted = true;
      statusSignal = init?.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        statusSignal?.addEventListener(
          "abort",
          () => reject(statusSignal?.reason ?? new Error("aborted")),
          { once: true },
        );
      });
    }
    if (url === `/api/runs/${runId}/cancel`) {
      return jsonResponse({
        run_id: runId,
        status: "cancelled",
        run_state: "cancelled",
        confirmed: true,
        terminated_pids: [],
        surviving_pids: [],
      });
    }
    if (url === `/api/runs/${runId}/artifact`) {
      artifactReads += 1;
      return jsonResponse(successfulRunResponse(runId));
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch);

  try {
    ReconnectingEventSource.instances[0].disconnect();
    await waitFor(() => statusStarted, "status reconciliation did not start");
    await useApp.getState().cancelRun(context.conversationId);
    await context.sending;

    assert.equal(statusSignal?.aborted, true);
    assert.equal(artifactReads, 0);
    const conversation = useApp.getState().conversations[context.conversationId];
    assert.equal(conversation.pending, false);
    assert.equal(conversation.messages.at(-1)?.failure?.status, "cancelled");
  } finally {
    context.restore();
  }
});

test("temporary status failure retains ownership until durable completion", async () => {
  const runId = "run_status_retry";
  const originalWindowSetTimeout = window.setTimeout;
  const originalWindowClearTimeout = window.clearTimeout;
  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    if (timeout === 250 && typeof handler === "function") {
      queueMicrotask(() => handler(...args));
      return 654_321;
    }
    return originalWindowSetTimeout(handler, timeout, ...args);
  }) as typeof window.setTimeout;
  window.clearTimeout = ((timer?: number) => {
    if (timer !== 654_321) originalWindowClearTimeout(timer);
  }) as typeof window.clearTimeout;
  let statusReads = 0;
  let artifactReads = 0;
  const context = await startReconnectingOrdinaryRun((async (input) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      return jsonResponse({
        run_id: runId,
        upload_token: "upload-token",
        input_slots: [],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      return jsonResponse({
        run_id: runId,
        placeholder_message: { id: "placeholder", role: "assistant", text: "", ts: 1 },
      });
    }
    if (url === `/api/runs/${runId}/status`) {
      statusReads += 1;
      return statusReads === 1
        ? jsonResponse({ detail: "status temporarily unavailable" }, 503)
        : jsonResponse(runStatusResponse(runId, "completed", "run.done"));
    }
    if (url === `/api/runs/${runId}/artifact`) {
      artifactReads += 1;
      return jsonResponse(successfulRunResponse(runId));
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch);

  try {
    ReconnectingEventSource.instances[0].disconnect();
    await waitFor(() => statusReads === 2, "status reconciliation did not retry");
    await context.sending;
    assert.equal(artifactReads, 1);
    assert.equal(ReconnectingEventSource.instances.length, 1);
    assert.equal(useApp.getState().conversations[context.conversationId].pending, false);
  } finally {
    window.setTimeout = originalWindowSetTimeout;
    window.clearTimeout = originalWindowClearTimeout;
    context.restore();
  }
});

test("run status errors classify permanent HTTP and invalid schema responses", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = (async () => jsonResponse({ detail: "missing run" }, 404)) as typeof fetch;
    await assert.rejects(
      api.fetchRunStatus("run_missing"),
      (error: unknown) => (
        typeof api.RunStatusError === "function"
        && error instanceof api.RunStatusError
        && error.kind === "not_found"
        && error.retryable === false
      ),
    );

    globalThis.fetch = (async () => jsonResponse({ run_id: "run_invalid" })) as typeof fetch;
    await assert.rejects(
      api.fetchRunStatus("run_invalid"),
      (error: unknown) => (
        typeof api.RunStatusError === "function"
        && error instanceof api.RunStatusError
        && error.kind === "invalid_response"
        && error.retryable === false
      ),
    );

    globalThis.fetch = (async () => new Response("{", {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })) as typeof fetch;
    await assert.rejects(
      api.fetchRunStatus("run_malformed"),
      (error: unknown) => (
        typeof api.RunStatusError === "function"
        && error instanceof api.RunStatusError
        && error.kind === "invalid_response"
        && error.retryable === false
      ),
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("ordinary lost start response reconciles the durable run instead of failing it", async () => {
  const runId = "run_lost_start_response";
  let statusReads = 0;
  let artifactReads = 0;
  const originalWindowSetTimeout = window.setTimeout;
  const originalWindowClearTimeout = window.clearTimeout;
  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    if (timeout === 250 && typeof handler === "function") {
      queueMicrotask(() => handler(...args));
      return 777_001;
    }
    return originalWindowSetTimeout(handler, timeout, ...args);
  }) as typeof window.setTimeout;
  window.clearTimeout = ((timer?: number) => {
    if (timer !== 777_001) originalWindowClearTimeout(timer);
  }) as typeof window.clearTimeout;
  const context = await startReconnectingOrdinaryRun((async (input) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      return jsonResponse({
        run_id: runId,
        upload_token: "upload-token",
        input_slots: [],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      throw new TypeError("start response was lost");
    }
    if (url === `/api/runs/${runId}/status`) {
      statusReads += 1;
      return jsonResponse(runStatusResponse(runId, "running"));
    }
    if (url === `/api/runs/${runId}/artifact`) {
      artifactReads += 1;
      return jsonResponse(successfulRunResponse(runId));
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch);

  try {
    await waitFor(
      () => statusReads === 1 && ReconnectingEventSource.instances.length === 2,
      "lost /start response did not reconcile durable status",
    );
    const running = useApp.getState().conversations[context.conversationId];
    assert.equal(running.pending, true);
    assert.equal(running.run_id, runId);
    assert.equal(running.messages.at(-1)?.status, "streaming");

    ReconnectingEventSource.instances[1].emit({
      event: "run.done",
      event_id: "lost-start-terminal",
    });
    await context.sending;
    assert.equal(artifactReads, 1);
    assert.equal(
      useApp.getState().conversations[context.conversationId].active_artifact_id,
      `art_${runId}`,
    );
  } finally {
    window.setTimeout = originalWindowSetTimeout;
    window.clearTimeout = originalWindowClearTimeout;
    context.restore();
  }
});

test("ordinary half-open start acknowledgement times out into durable reconciliation", async () => {
  const runId = "run_half_open_start_ack";
  let reservationRequests = 0;
  let startRequests = 0;
  let statusReads = 0;
  let artifactReads = 0;
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const originalWindowSetTimeout = window.setTimeout;
  const originalWindowClearTimeout = window.clearTimeout;
  const syntheticTimers = new Set<number>();
  let nextTimer = 779_000;
  globalThis.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    if (timeout === 60_000 && typeof handler === "function") {
      const timer = nextTimer;
      nextTimer += 1;
      syntheticTimers.add(timer);
      queueMicrotask(() => {
        if (syntheticTimers.delete(timer)) handler(...args);
      });
      return timer;
    }
    return originalSetTimeout(handler, timeout, ...args);
  }) as typeof setTimeout;
  globalThis.clearTimeout = ((timer?: number) => {
    if (timer !== undefined && syntheticTimers.delete(timer)) return;
    originalClearTimeout(timer);
  }) as typeof clearTimeout;
  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    if (timeout === 250 && typeof handler === "function") {
      const timer = nextTimer;
      nextTimer += 1;
      syntheticTimers.add(timer);
      queueMicrotask(() => {
        if (syntheticTimers.delete(timer)) handler(...args);
      });
      return timer;
    }
    return originalWindowSetTimeout(handler, timeout, ...args);
  }) as typeof window.setTimeout;
  window.clearTimeout = ((timer?: number) => {
    if (timer !== undefined && syntheticTimers.delete(timer)) return;
    originalWindowClearTimeout(timer);
  }) as typeof window.clearTimeout;
  const context = await startReconnectingOrdinaryRun((async (input, init) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      reservationRequests += 1;
      return jsonResponse({
        run_id: runId,
        upload_token: "upload-token",
        input_slots: [],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      startRequests += 1;
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          reject(init.signal?.reason ?? new DOMException("Aborted", "AbortError"));
        }, { once: true });
      });
    }
    if (url === `/api/runs/${runId}/status`) {
      statusReads += 1;
      return jsonResponse(runStatusResponse(runId, "running"));
    }
    if (url === `/api/runs/${runId}/artifact`) {
      artifactReads += 1;
      return jsonResponse(successfulRunResponse(runId));
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch);

  try {
    await waitFor(
      () => statusReads === 1 && ReconnectingEventSource.instances.length >= 2,
      "half-open /start did not reconcile the durable run",
    );
    assert.equal(reservationRequests, 1);
    assert.equal(startRequests, 1);
    ReconnectingEventSource.instances.at(-1)?.emit({
      event: "run.done",
      event_id: "half-open-start-terminal",
    });
    await context.sending;
    assert.equal(artifactReads, 1);
    assert.equal(
      useApp.getState().conversations[context.conversationId].active_artifact_id,
      `art_${runId}`,
    );
  } finally {
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
    window.setTimeout = originalWindowSetTimeout;
    window.clearTimeout = originalWindowClearTimeout;
    context.restore();
  }
});

test("user cancellation aborts a half-open start without replaying it", async () => {
  const runId = "run_half_open_start_cancelled";
  let reservationRequests = 0;
  let startRequests = 0;
  let statusReads = 0;
  let startSignal: AbortSignal | undefined;
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const liveTimers = new Set<number>();
  let nextTimer = 779_100;
  globalThis.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    if (timeout === 60_000 && typeof handler === "function") {
      const timer = nextTimer;
      nextTimer += 1;
      liveTimers.add(timer);
      return timer;
    }
    return originalSetTimeout(handler, timeout, ...args);
  }) as typeof setTimeout;
  globalThis.clearTimeout = ((timer?: number) => {
    if (timer !== undefined && liveTimers.delete(timer)) return;
    originalClearTimeout(timer);
  }) as typeof clearTimeout;
  const context = await startReconnectingOrdinaryRun((async (input, init) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      reservationRequests += 1;
      return jsonResponse({
        run_id: runId,
        upload_token: "upload-token",
        input_slots: [],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      startRequests += 1;
      startSignal = init?.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        startSignal?.addEventListener("abort", () => {
          reject(startSignal?.reason ?? new DOMException("Aborted", "AbortError"));
        }, { once: true });
      });
    }
    if (url === `/api/runs/${runId}/cancel`) {
      return jsonResponse({
        run_id: runId,
        status: "cancelled",
        run_state: "cancelled",
        confirmed: true,
        terminated_pids: [17],
        surviving_pids: [],
      });
    }
    if (url === `/api/runs/${runId}/status`) {
      statusReads += 1;
      return jsonResponse(runStatusResponse(runId, "cancelled", "run.cancelled"));
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch);

  try {
    await waitFor(() => Boolean(startSignal), "half-open /start did not begin");
    await useApp.getState().cancelRun(context.conversationId);
    await context.sending;
    assert.equal(startSignal?.aborted, true);
    assert.equal(reservationRequests, 1);
    assert.equal(startRequests, 1);
    assert.equal(statusReads, 0);
    assert.equal(liveTimers.size, 0);
    const conversation = useApp.getState().conversations[context.conversationId];
    assert.equal(conversation.pending, false);
    assert.equal(conversation.messages.at(-1)?.failure?.status, "cancelled");
  } finally {
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
    context.restore();
  }
});

test("ordinary queued run replays the same start after the first request is not delivered", async () => {
  const runId = "run_queued_start_replay";
  let reservationRequests = 0;
  let startRequests = 0;
  let statusReads = 0;
  let settled = false;
  const originalWindowSetTimeout = window.setTimeout;
  const originalWindowClearTimeout = window.clearTimeout;
  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    if (timeout === 250 && typeof handler === "function") {
      queueMicrotask(() => handler(...args));
      return 777_002;
    }
    return originalWindowSetTimeout(handler, timeout, ...args);
  }) as typeof window.setTimeout;
  window.clearTimeout = ((timer?: number) => {
    if (timer !== 777_002) originalWindowClearTimeout(timer);
  }) as typeof window.clearTimeout;
  const context = await startReconnectingOrdinaryRun((async (input, init) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      reservationRequests += 1;
      return jsonResponse({
        run_id: runId,
        upload_token: "same-upload-token",
        input_slots: [],
        request_digest: "digest",
        run_state: "queued",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      startRequests += 1;
      assert.equal(init?.headers && new Headers(init.headers).get("X-Autodesign-Upload-Token"), "same-upload-token");
      if (startRequests === 1) throw new TypeError("start request was not delivered");
      return jsonResponse({
        run_id: runId,
        progress_mode: "generate",
        placeholder_message: {
          id: `msg_${runId}`,
          role: "assistant",
          text: "",
          ts: 1,
          status: "streaming",
        },
      });
    }
    if (url === `/api/runs/${runId}/status`) {
      statusReads += 1;
      return jsonResponse(runStatusResponse(runId, "queued"));
    }
    if (url === `/api/runs/${runId}/artifact`) {
      return jsonResponse(successfulRunResponse(runId));
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch);
  const sending = context.sending.finally(() => { settled = true; });

  try {
    await waitFor(
      () => startRequests === 2 && ReconnectingEventSource.instances.length >= 2,
      "queued durable status did not replay the same reserved start",
    );
    assert.equal(reservationRequests, 1);
    assert.equal(statusReads, 1);
    assert.equal(useApp.getState().conversations[context.conversationId].run_id, runId);

    ReconnectingEventSource.instances.at(-1)?.emit({
      event: "run.done",
      event_id: "queued-start-terminal",
    });
    await sending;
    assert.equal(
      useApp.getState().conversations[context.conversationId].active_artifact_id,
      `art_${runId}`,
    );
  } finally {
    if (!settled) {
      ReconnectingEventSource.instances.at(-1)?.emit({
        event: "run.done",
        event_id: "queued-start-cleanup",
      });
      await sending;
    }
    window.setTimeout = originalWindowSetTimeout;
    window.clearTimeout = originalWindowClearTimeout;
    context.restore();
  }
});

test("queued start replay is not aborted by the completed status request timeout", async () => {
  const runId = "run_queued_start_replay_timeout_isolation";
  let startRequests = 0;
  let statusTimeout: (() => void) | undefined;
  let replaySignal: AbortSignal | undefined;
  let resolveReplay!: (response: Response) => void;
  let settled = false;
  const originalWindowSetTimeout = window.setTimeout;
  const originalWindowClearTimeout = window.clearTimeout;
  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    if (timeout === 250 && typeof handler === "function") {
      queueMicrotask(() => handler(...args));
      return 777_102;
    }
    if (timeout === 10_000 && typeof handler === "function") {
      statusTimeout = () => handler(...args);
      return 777_103;
    }
    return originalWindowSetTimeout(handler, timeout, ...args);
  }) as typeof window.setTimeout;
  window.clearTimeout = ((timer?: number) => {
    if (timer !== 777_102 && timer !== 777_103) {
      originalWindowClearTimeout(timer);
    }
  }) as typeof window.clearTimeout;
  const context = await startReconnectingOrdinaryRun((async (input, init) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      return jsonResponse({
        run_id: runId,
        upload_token: "same-upload-token",
        input_slots: [],
        request_digest: "digest",
        run_state: "queued",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      startRequests += 1;
      if (startRequests === 1) throw new TypeError("start request was not delivered");
      replaySignal = init?.signal ?? undefined;
      return new Promise<Response>((resolve) => { resolveReplay = resolve; });
    }
    if (url === `/api/runs/${runId}/status`) {
      return jsonResponse(runStatusResponse(runId, "queued"));
    }
    if (url === `/api/runs/${runId}/artifact`) {
      return jsonResponse(successfulRunResponse(runId));
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch);
  const sending = context.sending.finally(() => { settled = true; });

  try {
    await waitFor(
      () => startRequests === 2 && Boolean(statusTimeout) && Boolean(replaySignal),
      "queued start replay did not begin",
    );
    statusTimeout?.();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(replaySignal?.aborted, false);

    resolveReplay(jsonResponse({
      run_id: runId,
      progress_mode: "generate",
      placeholder_message: {
        id: `msg_${runId}`,
        role: "assistant",
        text: "",
        ts: 1,
        status: "streaming",
      },
    }));
    await waitFor(
      () => ReconnectingEventSource.instances.length >= 2,
      "queued start replay did not reconnect the stream",
    );
    ReconnectingEventSource.instances.at(-1)?.emit({
      event: "run.done",
      event_id: "queued-start-timeout-isolation-terminal",
    });
    await sending;
  } finally {
    if (!settled) {
      resolveReplay?.(jsonResponse({
        run_id: runId,
        placeholder_message: {
          id: `msg_${runId}`,
          role: "assistant",
          text: "",
          ts: 1,
          status: "streaming",
        },
      }));
      ReconnectingEventSource.instances.at(-1)?.emit({
        event: "run.done",
        event_id: "queued-start-timeout-isolation-cleanup",
      });
      await sending;
    }
    window.setTimeout = originalWindowSetTimeout;
    window.clearTimeout = originalWindowClearTimeout;
    context.restore();
  }
});

test("permanent run status errors terminate after a bounded confirmation budget", async () => {
  const runId = "run_permanent_status_error";
  const originalWindowSetTimeout = window.setTimeout;
  const originalWindowClearTimeout = window.clearTimeout;
  const acceleratedTimers = new Set<number>();
  let nextTimer = 778_000;
  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    if ((timeout === 250 || timeout === 500) && typeof handler === "function") {
      const timer = nextTimer;
      nextTimer += 1;
      acceleratedTimers.add(timer);
      queueMicrotask(() => {
        if (acceleratedTimers.delete(timer)) handler(...args);
      });
      return timer;
    }
    return originalWindowSetTimeout(handler, timeout, ...args);
  }) as typeof window.setTimeout;
  window.clearTimeout = ((timer?: number) => {
    if (timer !== undefined && acceleratedTimers.delete(timer)) return;
    originalWindowClearTimeout(timer);
  }) as typeof window.clearTimeout;
  let statusReads = 0;
  const context = await startReconnectingOrdinaryRun((async (input) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      return jsonResponse({
        run_id: runId,
        upload_token: "upload-token",
        input_slots: [],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      return jsonResponse({
        run_id: runId,
        placeholder_message: { id: "placeholder", role: "assistant", text: "", ts: 1 },
      });
    }
    if (url === `/api/runs/${runId}/status`) {
      statusReads += 1;
      return jsonResponse({ detail: "run not found" }, 404);
    }
    if (url === `/api/runs/${runId}/cancel`) {
      return jsonResponse({
        run_id: runId,
        status: "cancelled",
        run_state: "cancelled",
        confirmed: true,
        terminated_pids: [],
        surviving_pids: [],
      });
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch);
  let settled = false;
  void context.sending.finally(() => { settled = true; });

  try {
    ReconnectingEventSource.instances[0].disconnect();
    await waitFor(() => statusReads === 3, "permanent status was not confirmed three times");
    await waitFor(() => settled, "permanent status error left the run pending forever");

    const conversation = useApp.getState().conversations[context.conversationId];
    assert.equal(statusReads, 3);
    assert.equal(conversation.pending, false);
    assert.equal(conversation.run_id, undefined);
    assert.equal(conversation.messages.at(-1)?.failure?.status, "run_status_unavailable");
  } finally {
    if (!settled) {
      await useApp.getState().cancelRun(context.conversationId);
      await context.sending;
    }
    window.setTimeout = originalWindowSetTimeout;
    window.clearTimeout = originalWindowClearTimeout;
    context.restore();
  }
});

test("persisted ordinary recovery surfaces its confirmed permanent status error", async () => {
  const runId = "run_missing_persisted";
  const originalFetch = globalThis.fetch;
  const originalEventSource = globalThis.EventSource;
  const originalWindowSetTimeout = window.setTimeout;
  const originalWindowClearTimeout = window.clearTimeout;
  ReconnectingEventSource.instances = [];
  Object.assign(globalThis, {
    EventSource: ReconnectingEventSource as unknown as typeof EventSource,
  });
  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => (
    originalWindowSetTimeout(handler, timeout && timeout <= 5_000 ? 0 : timeout, ...args)
  )) as typeof window.setTimeout;
  window.clearTimeout = originalWindowClearTimeout;

  useApp.getState().newConversation();
  const conversationId = useApp.getState().current_conversation_id;
  useApp.setState((state) => ({
    conversations: {
      ...state.conversations,
      [conversationId]: {
        ...state.conversations[conversationId],
        pending: true,
        run_id: runId,
        messages: [{
          id: "persisted-placeholder",
          role: "assistant",
          text: "",
          ts: 1,
          status: "streaming",
          run_id: runId,
          task_type: "generate",
          task_payload: { artifact_type: "landing" },
        }],
      },
    },
    runs_progress: {
      ...state.runs_progress,
      [conversationId]: initialProgress(runId),
    },
  }));
  let statusReads = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${runId}/artifact`) {
      return jsonResponse({ detail: "artifact missing" }, 404);
    }
    if (url === `/api/runs/${runId}/status`) {
      statusReads += 1;
      return jsonResponse({ detail: "run missing" }, 404);
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch;

  try {
    useApp.getState().recoverActiveRuns();
    await waitFor(() => ReconnectingEventSource.instances.length === 1, "recovery SSE did not start");
    ReconnectingEventSource.instances[0].disconnect();
    await waitFor(() => statusReads === 3, "permanent status was not confirmed");
    await waitFor(
      () => useApp.getState().conversations[conversationId].pending === false,
      "persisted recovery did not settle",
    );
    const failure = useApp.getState().conversations[conversationId].messages.at(-1)?.failure;
    assert.equal(failure?.status, "run_status_unavailable");
    assert.equal(failure?.run_id, runId);
  } finally {
    globalThis.fetch = originalFetch;
    Object.assign(globalThis, { EventSource: originalEventSource });
    window.setTimeout = originalWindowSetTimeout;
    window.clearTimeout = originalWindowClearTimeout;
  }
});

test("repeated closed SSE uses increasing reconnect delays while status stays running", async () => {
  const runId = "run_sse_backoff";
  const originalWindowSetTimeout = window.setTimeout;
  const originalWindowClearTimeout = window.clearTimeout;
  const reconnectTimers: Array<{ delay: number; run: () => void; id: number }> = [];
  let nextTimer = 779_000;
  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    if ((timeout === 250 || timeout === 500) && typeof handler === "function") {
      const id = nextTimer;
      nextTimer += 1;
      reconnectTimers.push({ delay: timeout, run: () => handler(...args), id });
      return id;
    }
    return originalWindowSetTimeout(handler, timeout, ...args);
  }) as typeof window.setTimeout;
  window.clearTimeout = ((timer?: number) => {
    const index = reconnectTimers.findIndex((entry) => entry.id === timer);
    if (index >= 0) {
      reconnectTimers.splice(index, 1);
      return;
    }
    originalWindowClearTimeout(timer);
  }) as typeof window.clearTimeout;
  let statusReads = 0;
  const context = await startReconnectingOrdinaryRun((async (input) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      return jsonResponse({
        run_id: runId,
        upload_token: "upload-token",
        input_slots: [],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      return jsonResponse({
        run_id: runId,
        placeholder_message: { id: "placeholder", role: "assistant", text: "", ts: 1 },
      });
    }
    if (url === `/api/runs/${runId}/status`) {
      statusReads += 1;
      return jsonResponse(runStatusResponse(runId, "running"));
    }
    if (url === `/api/runs/${runId}/artifact`) {
      return jsonResponse(successfulRunResponse(runId));
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch);

  try {
    ReconnectingEventSource.instances[0].disconnect();
    await waitFor(
      () => statusReads === 1 && reconnectTimers.length === 1,
      "first durable status reconciliation did not schedule reconnect",
    );
    assert.equal(ReconnectingEventSource.instances.length, 1);
    assert.deepEqual(reconnectTimers.map((timer) => timer.delay), [250]);
    reconnectTimers.shift()?.run();
    await waitFor(() => ReconnectingEventSource.instances.length === 2, "first delayed reconnect did not run");

    ReconnectingEventSource.instances[1].disconnect();
    await waitFor(
      () => statusReads === 2 && reconnectTimers.length === 1,
      "second durable status reconciliation did not schedule reconnect",
    );
    assert.equal(ReconnectingEventSource.instances.length, 2);
    assert.deepEqual(reconnectTimers.map((timer) => timer.delay), [500]);
    reconnectTimers.shift()?.run();
    await waitFor(() => ReconnectingEventSource.instances.length === 3, "second delayed reconnect did not run");

    ReconnectingEventSource.instances[2].emit({
      event: "run.done",
      event_id: "backoff-terminal",
    });
    await context.sending;
  } finally {
    window.setTimeout = originalWindowSetTimeout;
    window.clearTimeout = originalWindowClearTimeout;
    context.restore();
  }
});

test("terminal artifact exhaustion is resumable without starting a replacement run", async () => {
  const runId = "run_artifact_delivery";
  const originalWindowSetTimeout = window.setTimeout;
  const originalWindowClearTimeout = window.clearTimeout;
  let artifactAvailable = false;
  let artifactReads = 0;
  let newRunRequests = 0;
  let statusReads = 0;
  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => (
    originalWindowSetTimeout(handler, timeout === 60_000 ? 50 : timeout && timeout <= 5_000 ? 0 : timeout, ...args)
  )) as typeof window.setTimeout;
  window.clearTimeout = originalWindowClearTimeout;
  const context = await startReconnectingOrdinaryRun((async (input) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      newRunRequests += 1;
      if (newRunRequests > 1) throw new Error("resume must not reserve a replacement run");
      return jsonResponse({
        run_id: runId,
        upload_token: "upload-token",
        input_slots: [],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      return jsonResponse({
        run_id: runId,
        placeholder_message: { id: "placeholder", role: "assistant", text: "", ts: 1 },
      });
    }
    if (url === `/api/runs/${runId}/artifact`) {
      artifactReads += 1;
      return artifactAvailable
        ? jsonResponse(successfulRunResponse(runId))
        : jsonResponse({ detail: "artifact commit unavailable" }, 504);
    }
    if (url === `/api/runs/${runId}/status`) {
      statusReads += 1;
      return jsonResponse({ detail: "terminal run was archived" }, 404);
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch);

  try {
    ReconnectingEventSource.instances[0].emit({
      event: "run.done",
      event_id: "artifact-delivery-terminal",
    });
    await context.sending;

    const failed = useApp.getState().conversations[context.conversationId].messages.at(-1);
    assert.equal(failed?.failure?.status, "artifact_delivery_failed");
    assert.equal(failed?.run_id, runId);
    assert.equal(failed?.failure?.run_id, runId);
    assert.equal(newRunRequests, 1);

    await useApp.getState().resumeRun(failed?.id ?? "");
    const retryFailure = useApp.getState().conversations[context.conversationId].messages.at(-1);
    assert.equal(statusReads, 0);
    assert.equal(ReconnectingEventSource.instances.length, 1);
    assert.equal(retryFailure?.failure?.status, "artifact_delivery_failed");
    assert.equal(retryFailure?.failure?.run_id, runId);
    assert.equal(newRunRequests, 1);

    artifactAvailable = true;
    await useApp.getState().resumeRun(retryFailure?.id ?? "");
    const recovered = useApp.getState().conversations[context.conversationId];
    assert.equal(newRunRequests, 1);
    assert.equal(recovered.active_artifact_id, `art_${runId}`);
    assert.equal(recovered.messages.at(-1)?.status, "done");
    assert.ok(artifactReads > 1);
  } finally {
    window.setTimeout = originalWindowSetTimeout;
    window.clearTimeout = originalWindowClearTimeout;
    context.restore();
  }
});

function seedRun(runId: string) {
  const current = useApp.getState();
  const conversationId = current.current_conversation_id;
  const conversation = current.conversations[conversationId];
  useApp.setState({
    conversations: {
      ...current.conversations,
      [conversationId]: {
        ...conversation,
        pending: true,
        run_id: runId,
        messages: [{
          id: "placeholder",
          role: "assistant",
          text: "",
          ts: 1,
          status: "streaming",
          run_id: runId,
        }],
      },
    },
    runs_progress: {
      ...current.runs_progress,
      [conversationId]: initialProgress(runId),
    },
  });
  return conversationId;
}

function deferredResponse() {
  let resolve!: (response: Response) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<Response>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

test("cancel keeps the run live until a confirmed cancelled response", async () => {
  const originalFetch = globalThis.fetch;
  const pending = deferredResponse();
  globalThis.fetch = (() => pending.promise) as typeof fetch;
  const conversationId = seedRun("run_confirmed");

  try {
    const cancelling = useApp.getState().cancelRun(conversationId);
    await Promise.resolve();

    assert.equal(useApp.getState().conversations[conversationId].run_id, "run_confirmed");
    assert.equal(useApp.getState().conversations[conversationId].pending, true);
    assert.equal(useApp.getState().runs_progress[conversationId].phase, "cancelling");
    assert.equal(
      useApp.getState().runs_progress[conversationId].cancel_request_in_flight,
      true,
    );

    pending.resolve(jsonResponse({
      run_id: "run_confirmed",
      status: "cancelled",
      run_state: "cancelled",
      confirmed: true,
      terminated_pids: [],
      surviving_pids: [],
    }));
    await cancelling;

    const conversation = useApp.getState().conversations[conversationId];
    assert.equal(conversation.run_id, undefined);
    assert.equal(conversation.pending, false);
    assert.equal(conversation.messages[0].failure?.status, "cancelled");
    assert.equal(useApp.getState().runs_progress[conversationId], undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("ordinary cancel ignores a same-run response after owner rotation", async () => {
  const originalFetch = globalThis.fetch;
  const pending = deferredResponse();
  globalThis.fetch = (() => pending.promise) as typeof fetch;
  memoryStorage.setItem("autodesign.demo_user.v1", "old-owner");
  const conversationId = seedRun("run_owner_rotation");

  try {
    const cancelling = useApp.getState().cancelRun(conversationId);
    await Promise.resolve();
    memoryStorage.setItem("autodesign.demo_user.v1", "new-owner");
    pending.resolve(jsonResponse({
      run_id: "run_owner_rotation",
      status: "cancelled",
      run_state: "cancelled",
      confirmed: true,
      terminated_pids: [],
      surviving_pids: [],
    }));
    await cancelling;

    const state = useApp.getState();
    assert.equal(state.conversations[conversationId].run_id, "run_owner_rotation");
    assert.equal(state.conversations[conversationId].pending, true);
    assert.equal(state.runs_progress[conversationId].phase, "cancelling");
  } finally {
    memoryStorage.setItem("autodesign.demo_user.v1", "test-user");
    globalThis.fetch = originalFetch;
  }
});

test("cancel keeps state and shows an unconfirmed warning for 202", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => jsonResponse({
    run_id: "run_pending",
    status: "cancellation_pending",
    run_state: "cancelling",
    confirmed: false,
    terminated_pids: [],
    surviving_pids: [99],
  }, 202)) as typeof fetch;
  const conversationId = seedRun("run_pending");

  try {
    await useApp.getState().cancelRun(conversationId);

    const state = useApp.getState();
    assert.equal(state.conversations[conversationId].run_id, "run_pending");
    assert.equal(state.conversations[conversationId].pending, true);
    assert.equal(state.conversations[conversationId].messages[0].status, "streaming");
    assert.equal(state.runs_progress[conversationId].phase, "cancelling");
    assert.equal(state.runs_progress[conversationId].cancel_request_in_flight, false);
    assert.equal(
      state.runs_progress[conversationId].label,
      "Cancellation not confirmed; backend may still be stopping",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("already-terminal cancellation response does not relabel the run cancelled", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => jsonResponse({
    run_id: "run_completed",
    status: "already_terminal",
    run_state: "completed",
    confirmed: true,
    terminated_pids: [],
    surviving_pids: [],
  })) as typeof fetch;
  const conversationId = seedRun("run_completed");

  try {
    await useApp.getState().cancelRun(conversationId);

    const state = useApp.getState();
    assert.equal(state.conversations[conversationId].run_id, "run_completed");
    assert.equal(state.conversations[conversationId].messages[0].status, "streaming");
    assert.equal(state.conversations[conversationId].messages[0].failure, undefined);
    assert.equal(state.runs_progress[conversationId].phase, "done");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("network failure retains the run and reports cancellation as unconfirmed", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => { throw new Error("offline"); }) as typeof fetch;
  const conversationId = seedRun("run_offline");

  try {
    await useApp.getState().cancelRun(conversationId);

    const state = useApp.getState();
    assert.equal(state.conversations[conversationId].run_id, "run_offline");
    assert.equal(state.conversations[conversationId].pending, true);
    assert.equal(state.runs_progress[conversationId].phase, "cancelling");
    assert.equal(
      state.runs_progress[conversationId].label,
      "Cancellation not confirmed; backend may still be stopping",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("a stalled cancellation request times out without hiding the live run", async () => {
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = window.setTimeout;
  const originalClearTimeout = window.clearTimeout;
  let observedSignal: AbortSignal | undefined;
  window.setTimeout = ((handler: TimerHandler) => {
    queueMicrotask(() => {
      if (typeof handler === "function") handler();
    });
    return 1;
  }) as typeof window.setTimeout;
  window.clearTimeout = (() => undefined) as typeof window.clearTimeout;
  globalThis.fetch = ((_input: string | URL | Request, init?: RequestInit) => {
    observedSignal = init?.signal ?? undefined;
    return new Promise<Response>((_resolve, reject) => {
      observedSignal?.addEventListener("abort", () => reject(observedSignal?.reason));
    });
  }) as typeof fetch;
  const conversationId = seedRun("run_stalled_cancel");

  try {
    await useApp.getState().cancelRun(conversationId);

    const state = useApp.getState();
    assert.equal(observedSignal?.aborted, true);
    assert.equal(state.conversations[conversationId].run_id, "run_stalled_cancel");
    assert.equal(state.conversations[conversationId].pending, true);
    assert.equal(state.runs_progress[conversationId].phase, "cancelling");
    assert.equal(
      state.runs_progress[conversationId].label,
      "Cancellation not confirmed; backend may still be stopping",
    );
  } finally {
    globalThis.fetch = originalFetch;
    window.setTimeout = originalSetTimeout;
    window.clearTimeout = originalClearTimeout;
  }
});

test("non-terminal SSE activity cannot overwrite cancellation-pending state", () => {
  const cancelling = {
    ...initialProgress("run_pending_event"),
    phase: "cancelling" as const,
    label: "Cancellation not confirmed; backend may still be stopping",
  };

  const next = applyEvent(cancelling, { event: "run.start" });

  assert.equal(next.phase, "cancelling");
  assert.equal(next.label, "Cancellation not confirmed; backend may still be stopping");
});

test("concurrent cancel clicks share one backend request", async () => {
  const originalFetch = globalThis.fetch;
  const requests: ReturnType<typeof deferredResponse>[] = [];
  globalThis.fetch = (() => {
    const request = deferredResponse();
    requests.push(request);
    return request.promise;
  }) as typeof fetch;
  const conversationId = seedRun("run_concurrent_cancel");

  try {
    const first = useApp.getState().cancelRun(conversationId);
    const second = useApp.getState().cancelRun(conversationId);
    await Promise.resolve();

    for (const request of requests) {
      request.resolve(jsonResponse({
        run_id: "run_concurrent_cancel",
        status: "already_terminal",
        run_state: "completed",
        confirmed: true,
        terminated_pids: [],
        surviving_pids: [],
      }));
    }
    await Promise.all([first, second]);

    const state = useApp.getState();
    assert.equal(requests.length, 1);
    assert.equal(state.runs_progress[conversationId].phase, "done");
  } finally {
    for (const request of requests) {
      request.resolve(jsonResponse({
        run_id: "run_concurrent_cancel",
        status: "cancellation_pending",
        run_state: "cancelling",
        confirmed: false,
        terminated_pids: [],
        surviving_pids: [],
      }, 202));
    }
    globalThis.fetch = originalFetch;
  }
});

test("old-owner cancellation cleanup cannot delete a new-owner in-flight request", async () => {
  const originalFetch = globalThis.fetch;
  const requests: ReturnType<typeof deferredResponse>[] = [];
  globalThis.fetch = (() => {
    const request = deferredResponse();
    requests.push(request);
    return request.promise;
  }) as typeof fetch;
  memoryStorage.setItem("autodesign.demo_user.v1", "old-owner");
  const conversationId = seedRun("run_owner_request_map");

  try {
    const oldCancel = useApp.getState().cancelRun(conversationId);
    await Promise.resolve();
    memoryStorage.setItem("autodesign.demo_user.v1", "new-owner");
    const newCancel = useApp.getState().cancelRun(conversationId);
    await Promise.resolve();
    assert.equal(requests.length, 2);

    requests[0].resolve(jsonResponse({
      run_id: "run_owner_request_map",
      status: "cancelled",
      run_state: "cancelled",
      confirmed: true,
      terminated_pids: [],
      surviving_pids: [],
    }));
    await oldCancel;
    const sharedNewCancel = useApp.getState().cancelRun(conversationId);
    await Promise.resolve();
    assert.equal(requests.length, 2);

    requests[1].resolve(jsonResponse({
      run_id: "run_owner_request_map",
      status: "cancelled",
      run_state: "cancelled",
      confirmed: true,
      terminated_pids: [],
      surviving_pids: [],
    }));
    await Promise.all([newCancel, sharedNewCancel]);
    assert.equal(useApp.getState().conversations[conversationId].run_id, undefined);
  } finally {
    memoryStorage.setItem("autodesign.demo_user.v1", "test-user");
    for (const request of requests) {
      request.resolve(jsonResponse({
        run_id: "run_owner_request_map",
        status: "cancellation_pending",
        run_state: "cancelling",
        confirmed: false,
        terminated_pids: [],
        surviving_pids: [],
      }, 202));
    }
    globalThis.fetch = originalFetch;
  }
});

test("HTTP 202 is unconfirmed even when its body claims cancellation", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => jsonResponse({
    run_id: "run_false_confirmation",
    status: "cancelled",
    run_state: "cancelled",
    confirmed: true,
    terminated_pids: [41],
    surviving_pids: [],
  }, 202)) as typeof fetch;
  const conversationId = seedRun("run_false_confirmation");

  try {
    await useApp.getState().cancelRun(conversationId);

    const state = useApp.getState();
    assert.equal(state.conversations[conversationId].run_id, "run_false_confirmation");
    assert.equal(state.conversations[conversationId].pending, true);
    assert.equal(state.conversations[conversationId].messages[0].status, "streaming");
    assert.equal(state.runs_progress[conversationId].phase, "cancelling");
    assert.equal(
      state.runs_progress[conversationId].label,
      "Cancellation not confirmed; backend may still be stopping",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("late unconfirmed responses cannot overwrite terminal SSE progress", async () => {
  const originalFetch = globalThis.fetch;

  try {
    for (const outcome of ["pending", "network"] as const) {
      const request = deferredResponse();
      globalThis.fetch = (() => request.promise) as typeof fetch;
      const runId = `run_terminal_${outcome}`;
      const conversationId = seedRun(runId);
      const cancelling = useApp.getState().cancelRun(conversationId);
      await Promise.resolve();
      useApp.setState((state) => ({
        runs_progress: {
          ...state.runs_progress,
          [conversationId]: {
            ...state.runs_progress[conversationId],
            phase: "done",
            label: "Done.",
          },
        },
      }));

      if (outcome === "pending") {
        request.resolve(jsonResponse({
          run_id: runId,
          status: "cancellation_pending",
          run_state: "cancelling",
          confirmed: false,
          terminated_pids: [],
          surviving_pids: [52],
        }, 202));
      } else {
        request.reject(new Error("offline"));
      }
      await cancelling;

      const progress = useApp.getState().runs_progress[conversationId];
      assert.equal(progress.phase, "done", outcome);
      assert.equal(progress.label, "Done.", outcome);
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("HTTP 200 only confirms cancellation for an explicit cancelled status", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => jsonResponse({
    run_id: "run_invalid_confirmation",
    status: "stopped",
    run_state: "cancelled",
    confirmed: true,
    terminated_pids: [61],
    surviving_pids: [],
  })) as typeof fetch;
  const conversationId = seedRun("run_invalid_confirmation");

  try {
    await useApp.getState().cancelRun(conversationId);

    const state = useApp.getState();
    assert.equal(state.conversations[conversationId].run_id, "run_invalid_confirmation");
    assert.equal(state.conversations[conversationId].messages[0].status, "streaming");
    assert.equal(state.runs_progress[conversationId].phase, "cancelling");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

async function assertMalformedCancellationRetained(
  runId: string,
  responseBody: Record<string, unknown>,
) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => jsonResponse(responseBody)) as typeof fetch;
  const conversationId = seedRun(runId);

  try {
    await useApp.getState().cancelRun(conversationId);

    const state = useApp.getState();
    assert.equal(state.conversations[conversationId].run_id, runId);
    assert.equal(state.conversations[conversationId].pending, true);
    assert.equal(state.conversations[conversationId].messages[0].status, "streaming");
    assert.equal(state.conversations[conversationId].messages[0].failure, undefined);
    assert.equal(state.runs_progress[conversationId].phase, "cancelling");
    assert.equal(
      state.runs_progress[conversationId].label,
      "Cancellation not confirmed; backend may still be stopping",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test("rejects a cancellation response for a different run", async () => {
  await assertMalformedCancellationRetained("run_expected", {
    run_id: "run_other",
    status: "cancelled",
    run_state: "cancelled",
    confirmed: true,
    terminated_pids: [71],
    surviving_pids: [],
  });
});

test("rejects truthy non-boolean cancellation confirmation", async () => {
  await assertMalformedCancellationRetained("run_truthy", {
    run_id: "run_truthy",
    status: "cancelled",
    run_state: "cancelled",
    confirmed: "true",
    terminated_pids: [72],
    surviving_pids: [],
  });
});

test("rejects cancelled statuses without a cancelled run state", async () => {
  for (const status of ["cancelled", "already_cancelled"] as const) {
    await assertMalformedCancellationRetained(`run_bad_state_${status}`, {
      run_id: `run_bad_state_${status}`,
      status,
      run_state: "completed",
      confirmed: true,
      terminated_pids: [73],
      surviving_pids: [],
    });
  }
});

test("rejects invalid already-terminal state without promoting an error", async () => {
  await assertMalformedCancellationRetained("run_bad_terminal", {
    run_id: "run_bad_terminal",
    status: "already_terminal",
    run_state: "cancelling",
    confirmed: true,
    terminated_pids: [],
    surviving_pids: [74],
  });
});

async function assertCancelResponseRejected(
  runId: string,
  responseBody: Record<string, unknown>,
  status = 200,
) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => jsonResponse(responseBody, status)) as typeof fetch;
  try {
    await assert.rejects(() => api.cancelRunRequest(runId));
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test("rejects confirmed cancellation while any process survives", async () => {
  await assertCancelResponseRejected("run_survivor", {
    run_id: "run_survivor",
    status: "cancelled",
    run_state: "cancelled",
    confirmed: true,
    terminated_pids: [81],
    surviving_pids: [82],
  });
});

test("rejects cancellation-pending confirmation and terminal-state contradictions", async () => {
  await assertCancelResponseRejected("run_pending_confirmed", {
    run_id: "run_pending_confirmed",
    status: "cancellation_pending",
    run_state: "cancelling",
    confirmed: true,
    terminated_pids: [],
    surviving_pids: [],
  }, 202);
  await assertCancelResponseRejected("run_pending_terminal", {
    run_id: "run_pending_terminal",
    status: "cancellation_pending",
    run_state: "cancelled",
    confirmed: false,
    terminated_pids: [],
    surviving_pids: [],
  }, 202);
});

test("reserved upload cancellation stays cancelled when upload later returns 409", async () => {
  const originalFetch = globalThis.fetch;
  const originalEventSource = globalThis.EventSource;
  const cancelResponse = deferredResponse();
  let resolveUpload!: (response: Response) => void;
  const uploadResponse = new Promise<Response>((resolve) => { resolveUpload = resolve; });
  let uploadStarted!: () => void;
  const uploadStartedPromise = new Promise<void>((resolve) => { uploadStarted = resolve; });
  let uploadSignal: AbortSignal | null = null;
  let startCalls = 0;

  class FakeEventSource {
    static readonly CLOSED = 2;
    static readonly instances: FakeEventSource[] = [];
    readonly url: string;
    readyState = 1;
    onmessage: ((event: MessageEvent<string>) => void) | null = null;
    onerror: (() => void) | null = null;

    constructor(url: string) {
      this.url = url;
      FakeEventSource.instances.push(this);
    }

    close() {
      this.readyState = FakeEventSource.CLOSED;
    }

    emit(payload: Record<string, unknown>) {
      this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
    }
  }

  Object.assign(globalThis, {
    EventSource: FakeEventSource as unknown as typeof EventSource,
  });
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      return jsonResponse({
        run_id: "run_upload_cancel",
        upload_token: "upload-token",
        input_slots: [{
          name: "attachment-0.pdf",
          role: "attachment",
          sha256: "382635c9325bf3273d195ff1b8a44e5b11afd7d97addeb8863ea35feb98c1a07",
          size: 5,
        }],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === "/api/runs/run_upload_cancel/inputs/attachment-0.pdf") {
      uploadSignal = init?.signal ?? null;
      uploadStarted();
      return uploadResponse;
    }
    if (url === "/api/runs/run_upload_cancel/cancel") {
      return cancelResponse.promise;
    }
    if (url === "/api/runs/run_upload_cancel/start") {
      startCalls += 1;
      throw new Error("cancelled upload must not start");
    }
    if (url === "/api/runs/run_upload_cancel/artifact") {
      return jsonResponse({
        message: {
          id: "msg_run_upload_cancel",
          role: "assistant",
          text: "Run cancelled.",
          ts: 2,
          run_id: "run_upload_cancel",
          status: "error",
          failure: { status: "cancelled", produced_files: [] },
        },
        artifact: null,
      });
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch;

  try {
    useApp.getState().newConversation();
    useApp.getState().setIntent("landing");
    const conversationId = useApp.getState().current_conversation_id;
    const file = new File([new TextEncoder().encode("paper")], "draft.pdf", {
      type: "application/pdf",
    });
    const sending = useApp.getState().sendMessage("Build a landing page", [{
      id: "upload",
      name: "draft.pdf",
      size: file.size,
      kind: "pdf",
      file,
    }]);
    await uploadStartedPromise;

    const cancelling = useApp.getState().cancelRun(conversationId);
    await Promise.resolve();
    assert.equal(FakeEventSource.instances.length, 1);
    FakeEventSource.instances[0].emit({
      run_id: "run_upload_cancel",
      event: "run.cancelled",
    });
    await Promise.resolve();
    cancelResponse.resolve(jsonResponse({
      run_id: "run_upload_cancel",
      status: "cancelled",
      run_state: "cancelled",
      confirmed: true,
      terminated_pids: [83],
      surviving_pids: [],
    }));
    await cancelling;
    resolveUpload(jsonResponse({ detail: "run is cancelled" }, 409));
    await sending;

    const state = useApp.getState();
    const conversation = state.conversations[conversationId];
    assert.equal(conversation.pending, false);
    assert.equal(conversation.run_id, undefined);
    assert.equal(conversation.messages.at(-1)?.failure?.status, "cancelled");
    assert.equal((uploadSignal as AbortSignal | null)?.aborted, true);
    assert.equal(startCalls, 0);
  } finally {
    globalThis.fetch = originalFetch;
    Object.assign(globalThis, { EventSource: originalEventSource });
  }
});
