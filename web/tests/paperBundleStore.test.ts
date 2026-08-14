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
const triggeredDownloads: Array<{ url: string; filename: string }> = [];
const documentStub = {
  cookie: "",
  body: { appendChild: () => undefined },
  createElement: (tagName: string) => {
    assert.equal(tagName, "a");
    const anchor = {
      href: "",
      download: "",
      rel: "",
      click: () => {
        triggeredDownloads.push({
          url: anchor.href,
          filename: anchor.download,
        });
      },
      remove: () => undefined,
    };
    return anchor;
  },
};
Object.assign(globalThis, {
  window: globalThis,
  localStorage,
  EventSource: MockEventSource,
  document: documentStub,
});

const vite = await createServer({
  root: fileURLToPath(new URL("..", import.meta.url)),
  server: { middlewareMode: true },
  appType: "custom",
});
const { listPaperBundles, publishCandidateDraft, startGenerate } = await vite.ssrLoadModule("/src/lib/api.ts") as
  typeof import("../src/lib/api.ts");
const {
  PAPER_BUNDLE_ARTIFACT_ORDER,
  createPaperBundleChildState,
  createPaperBundleParentState,
} =
  await vite.ssrLoadModule("/src/lib/paper_bundle.ts") as
    typeof import("../src/lib/paper_bundle.ts");
const {
  candidatePublicationIsActive,
  installTokenizedPublicationOwner,
  useApp,
} = await vite.ssrLoadModule("/src/lib/store.ts") as
  typeof import("../src/lib/store.ts");
const { initialProgress } = await vite.ssrLoadModule("/src/lib/progress.ts") as
  typeof import("../src/lib/progress.ts");
await vite.close();
import type {
  Artifact,
  ArtifactType,
  Conversation,
  MessageFailure,
  PaperBundleParentState,
} from "../src/lib/types.ts";
import type { AttemptCandidateSummary } from "../src/lib/attempt_candidates.ts";

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

type MockFetch = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;

let paperBundleListOverride: unknown[] | null = null;
let paperBundleListResponseOverride: (() => Promise<Response>) | null = null;
let paperBundleCancelOverride: { status: number; body: unknown } | null = null;
let paperBundleCancelRequests: string[] = [];
let paperBundleStartResponseOverride: ((runId: string) => Promise<Response> | null) | null = null;

function adaptLegacyBundleMock(delegate: MockFetch): MockFetch {
  const jobs = new Map<string, {
    request: Record<string, unknown>;
    children: Record<string, Record<string, unknown>>;
    starts: Map<string, Record<string, unknown>>;
  }>();
  return async (input, init) => {
    const url = String(input);
    if (url === "/api/paper-bundles" && init?.method === "POST") {
      const request = JSON.parse(String(init.body)) as {
        job_id: string;
        conversation_id: string;
        source_name: string;
        prompt_version: string;
        children: Record<string, {
          brief: string;
          artifact_type: string;
          conversation_id: string;
          palette_id?: string;
          template?: string;
          authoring_max_attempts?: number;
          input_slots: Array<{ name: string; sha256: string; size: number }>;
        }>;
      };
      const children: Record<string, Record<string, unknown>> = {};
      const starts = new Map<string, Record<string, unknown>>();
      const reservations = await Promise.all(PAPER_BUNDLE_ARTIFACT_ORDER.map(async (artifactType) => {
        const child = request.children[artifactType];
        const body = new FormData();
        body.append("brief", child.brief);
        body.append("artifact_type", child.artifact_type);
        body.append("conversation_id", child.conversation_id);
        if (child.palette_id) body.append("palette_id", child.palette_id);
        if (child.template) body.append("template", child.template);
        if (child.authoring_max_attempts !== undefined) {
          body.append("authoring_max_attempts", String(child.authoring_max_attempts));
        }
        const response = await delegate("/api/generate", { method: "POST", body });
        if (!response.ok) throw response;
        const ack = await response.json() as Record<string, unknown>;
        const runId = String(ack.run_id);
        starts.set(runId, ack);
        return [artifactType, {
          run_id: runId,
          artifact_type: artifactType,
          conversation_id: child.conversation_id,
          input_slots: child.input_slots.map((slot) => ({
            name: slot.name,
            expected_sha256: slot.sha256,
            expected_size: slot.size,
          })),
          upload_token: `token_${runId}`,
          request_digest: "a".repeat(64),
          expires_at: 10,
          state: "reserved",
          terminal: false,
          process_free: true,
        }] as const;
      }));
      for (const [artifactType, child] of reservations) {
        children[artifactType] = child;
      }
      jobs.set(request.job_id, {
        request: request as unknown as Record<string, unknown>,
        children,
        starts,
      });
      return jsonResponse({
        schema_version: 1,
        job_id: request.job_id,
        owner_id: "local",
        conversation_id: request.conversation_id,
        source_name: request.source_name,
        prompt_version: request.prompt_version,
        state: "reserved",
        children,
        request_digest: "b".repeat(64),
        revision: 1,
        created_at: 1,
        updated_at: 1,
        terminal: false,
        terminal_at: null,
        cancel_requested: false,
        cancel_requested_at: null,
        completed_children: [],
        reused: false,
      });
    }
    if (url === "/api/paper-bundles" && (!init?.method || init.method === "GET")) {
      if (paperBundleListResponseOverride) return paperBundleListResponseOverride();
      return jsonResponse(paperBundleListOverride ?? []);
    }
    const upload = url.match(/^\/api\/runs\/([^/]+)\/inputs\/([^/]+)$/);
    if (upload && init?.method === "PUT") {
      const child = [...jobs.values()]
        .flatMap((job) => Object.values(job.children))
        .find((candidate) => candidate.run_id === upload[1]);
      const slot = (child?.input_slots as Array<Record<string, unknown>> | undefined)
        ?.find((candidate) => candidate.name === upload[2]);
      if (!child || !slot) return jsonResponse({ detail: "missing reservation" }, 404);
      return jsonResponse({
        run_id: upload[1],
        slot: upload[2],
        sha256: slot.expected_sha256,
        size: slot.expected_size,
        run_state: "reserved",
        idempotent: false,
      });
    }
    const start = url.match(/^\/api\/runs\/([^/]+)\/start$/);
    if (start && init?.method === "POST") {
      const override = paperBundleStartResponseOverride?.(start[1]);
      if (override) return override;
      const ack = [...jobs.values()]
        .map((job) => job.starts.get(start[1]))
        .find(Boolean);
      if (ack) return jsonResponse(ack);
    }
    const cancel = url.match(/^\/api\/paper-bundles\/([^/]+)\/cancel$/);
    if (cancel && init?.method === "POST") {
      paperBundleCancelRequests.push(cancel[1]);
      if (paperBundleCancelOverride) {
        return jsonResponse(
          paperBundleCancelOverride.body,
          paperBundleCancelOverride.status,
        );
      }
      const job = jobs.get(cancel[1]);
      if (!job) {
        return jsonResponse({
          job_id: cancel[1],
          state: "cancelling",
          confirmed: false,
          status: "cancellation_pending",
          children: {},
        }, 202);
      }
      const cancelledChildren = Object.fromEntries(
        Object.entries(job.children).map(([artifactType, child]) => [
          artifactType,
          {
            ...child,
            upload_token: undefined,
            state: "cancelled",
            terminal: true,
            process_free: true,
          },
        ]),
      );
      await Promise.allSettled(Object.values(job.children).map((child) =>
        delegate(`/api/runs/${String(child.run_id)}/cancel`, { method: "POST" })
      ));
      return jsonResponse({
        schema_version: 1,
        job_id: cancel[1],
        owner_id: "local",
        conversation_id: job.request.conversation_id,
        source_name: job.request.source_name,
        prompt_version: job.request.prompt_version,
        state: "cancelled",
        children: cancelledChildren,
        request_digest: "b".repeat(64),
        revision: 2,
        created_at: 1,
        updated_at: 2,
        terminal: true,
        terminal_at: 2,
        cancel_requested: true,
        cancel_requested_at: 2,
        completed_children: [],
        confirmed: true,
        status: "cancelled",
      });
    }
    return delegate(input, init);
  };
}

let assignedFetch = globalThis.fetch as MockFetch;
Object.defineProperty(globalThis, "fetch", {
  configurable: true,
  get: () => assignedFetch,
  set: (value: MockFetch) => { assignedFetch = adaptLegacyBundleMock(value); },
});

const conversation = (
  id: string,
  overrides: Partial<Conversation> = {},
): Conversation => ({
  id,
  title: id,
  created_at: 1,
  updated_at: 1,
  messages: [],
  artifacts: {},
  active_artifact_id: null,
  ...overrides,
});

const artifact = (runId: string, artifactType: ArtifactType): Artifact => ({
  artifact_id: `art_${runId}`,
  name: `${artifactType} result`,
  artifact_type: artifactType,
  canvas: { w: 1280, h: 720 },
  layers: [],
});

const attemptCandidate = (
  runId: string,
  candidateId: string,
  safetyState: AttemptCandidateSummary["safety_state"] = "ready",
): AttemptCandidateSummary => ({
  candidate_id: candidateId,
  run_id: runId,
  artifact_type: "poster",
  attempt: 1,
  max_attempts: 4,
  created_at: "2026-08-05T00:00:00Z",
  source_sha256: "a".repeat(64),
  safety_state: safetyState,
  hard_blockers: safetyState === "blocked"
    ? [{ issue_id: "blocked", message: "Canvas repair required" }]
    : [],
  warnings: [],
  source_url: `/api/files/runs/${runId}/attempt-01/poster.html`,
  preview_urls: [],
});

const backendPaperBundleJob = (
  parentId: string,
  bundle: PaperBundleParentState,
  revision: number,
  backendState: "running" | "cancelling" = "running",
) => ({
  schema_version: 1,
  job_id: bundle.job_id,
  owner_id: "test-user",
  conversation_id: parentId,
  source_name: bundle.source_name,
  prompt_version: String(bundle.prompt_version),
  state: backendState,
  children: Object.fromEntries(PAPER_BUNDLE_ARTIFACT_ORDER.map((artifactType) => {
    const task = bundle.tasks[artifactType];
    const complete = task.status === "complete";
    return [artifactType, {
      run_id: task.run_id ?? `run_${artifactType}`,
      artifact_type: artifactType,
      conversation_id: task.child_conversation_id,
      input_slots: [{
        name: "paper.pdf",
        expected_sha256: "c".repeat(64),
        expected_size: 1,
      }],
      request_digest: "a".repeat(64),
      expires_at: 100,
      state: complete
        ? "completed"
        : task.status === "cancelling" ? "cancelling" : "running",
      terminal: complete,
      process_free: complete,
    }];
  })),
  request_digest: "b".repeat(64),
  revision,
  created_at: 1,
  updated_at: revision,
  terminal: false,
  terminal_at: null,
  cancel_requested: backendState === "cancelling",
  cancel_requested_at: backendState === "cancelling" ? revision : null,
  completed_children: PAPER_BUNDLE_ARTIFACT_ORDER.filter(
    (artifactType) => bundle.tasks[artifactType].status === "complete",
  ),
});

const backendPaperBundlePublicationJob = (
  parentId: string,
  bundle: PaperBundleParentState,
  revision: number,
  overrides: Partial<{
    source_run_id: string;
    publication_run_id: string;
    artifact_id: string;
    source_attempt: number;
    source_candidate_id: string;
    source_candidate_sha256: string;
    generation: number;
    published_at: number;
  }> = {},
) => {
  const sourceRunId = overrides.source_run_id ?? "run_publication_source";
  const publicationRunId = overrides.publication_run_id ?? "run_publication_derived";
  const artifactId = overrides.artifact_id ?? `art_${publicationRunId}`;
  const base = backendPaperBundleJob(parentId, bundle, revision);
  return {
    ...base,
    schema_version: 2,
    state: "partial",
    terminal: true,
    terminal_at: 3,
    updated_at: 3,
    children: Object.fromEntries(Object.entries(base.children).map(
      ([artifactType, descriptor]) => [artifactType, {
        ...descriptor,
        ...(artifactType === "poster" ? { run_id: sourceRunId } : {}),
        state: "failed",
        terminal: true,
        process_free: true,
      }],
    )),
    publications: {
      poster: {
        source_run_id: sourceRunId,
        publication_run_id: publicationRunId,
        artifact_id: artifactId,
        source_attempt: overrides.source_attempt ?? 1,
        source_candidate_id: overrides.source_candidate_id ?? "poster-attempt-01",
        source_candidate_sha256: overrides.source_candidate_sha256 ?? "d".repeat(64),
        generation: overrides.generation ?? 1,
        published_at: overrides.published_at ?? 2,
      },
    },
    completed_children: ["poster"],
  };
};

const completedBackendPaperBundlePublicationJob = (
  parentId: string,
  bundle: PaperBundleParentState,
  revision: number,
  sourceRunId: string,
  publicationRunId: string,
  candidateId: string,
) => {
  const job = backendPaperBundlePublicationJob(parentId, bundle, revision, {
    source_run_id: sourceRunId,
    publication_run_id: publicationRunId,
    artifact_id: `art_${publicationRunId}`,
    source_candidate_id: candidateId,
    source_candidate_sha256: "a".repeat(64),
  });
  return {
    ...job,
    state: "completed",
    children: Object.fromEntries(Object.entries(job.children).map(
      ([artifactType, descriptor]) => [artifactType, artifactType === "poster"
        ? descriptor
        : {
            ...descriptor,
            state: "completed",
            terminal: true,
            process_free: true,
          }],
    )),
    completed_children: [...PAPER_BUNDLE_ARTIFACT_ORDER],
  };
};

const runningBackendPaperBundlePublicationJob = (
  parentId: string,
  bundle: PaperBundleParentState,
  revision: number,
  sourceRunId: string,
  publicationRunId: string,
  deckRunId: string,
) => {
  const job = backendPaperBundlePublicationJob(parentId, bundle, revision, {
    source_run_id: sourceRunId,
    publication_run_id: publicationRunId,
  });
  return {
    ...job,
    state: "running",
    terminal: false,
    terminal_at: null,
    children: {
      ...job.children,
      deck: {
        ...job.children.deck,
        run_id: deckRunId,
        state: "running",
        terminal: false,
        process_free: false,
      },
    },
  };
};

const publishedArtifactResponse = (
  publicationRunId: string,
  sourceRunId: string,
  overrides: Partial<NonNullable<Artifact["attempt_lineage"]>> = {},
) => {
  const published = {
    ...artifact(publicationRunId, "poster"),
    attempt_lineage: {
      materialization_version: 2,
      status: "published" as const,
      source_run_id: sourceRunId,
      source_attempt: 1,
      source_candidate_id: "poster-attempt-01",
      source_candidate_sha256: "d".repeat(64),
      ...overrides,
    },
  };
  return {
    message: {
      id: `msg_${publicationRunId}`,
      role: "assistant" as const,
      text: "Published selected attempt.",
      ts: 3,
      run_id: publicationRunId,
      artifact_id: published.artifact_id,
      status: "done" as const,
      failure: null,
    },
    artifact: published,
  };
};

const responseForRun = (
  runId: string,
  artifactType: ArtifactType,
  failure?: MessageFailure,
) => ({
  message: {
    id: `msg_${runId}`,
    role: "assistant" as const,
    text: failure?.agent_last_note ?? "Done",
    ts: 2,
    run_id: runId,
    artifact_id: `art_${runId}`,
    status: failure ? "error" as const : "done" as const,
    failure,
  },
  artifact: artifact(runId, artifactType),
});

const completedRunStatus = (runId: string) => ({
  run_id: runId,
  run_state: "completed",
  revision: 3,
  publishable: true,
  cancellation_pending: null,
  worker_pid: null,
  terminal_event: "run.done",
});

const pptxResponseForRun = (runId: string, artifactType: ArtifactType = "deck") => ({
  message: {
    id: `msg_${runId}`,
    role: "assistant" as const,
    text: "PowerPoint ready",
    ts: 2,
    run_id: runId,
    artifact_id: `art_${runId}`,
    status: "done" as const,
    download_url: `/api/files/runs/${runId}/final/deck.pptx`,
    download_filename: `${runId}.pptx`,
    download_mime_type: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  },
  artifact: artifact(runId, artifactType),
});

const confirmedCancellation = (runId: string) => jsonResponse({
  run_id: runId,
  status: "cancelled",
  run_state: "cancelled",
  confirmed: true,
  terminated_pids: [],
  surviving_pids: [],
});

const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

async function waitFor(predicate: () => boolean, message: string) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await tick();
  }
  assert.fail(message);
}

function resetStore(conversations: Record<string, Conversation>, currentId: string) {
  MockEventSource.instances = [];
  paperBundleListOverride = null;
  paperBundleListResponseOverride = null;
  paperBundleCancelOverride = null;
  paperBundleCancelRequests = [];
  paperBundleStartResponseOverride = null;
  triggeredDownloads.length = 0;
  localStorage.clear();
  localStorage.setItem("autodesign.demo_user.v1", "test-user");
  useApp.setState({
    conversations,
    current_conversation_id: currentId,
    history_user_scope: "test-user",
    runs_progress: {},
    candidate_publication_owners: {},
    run_attempts: {},
    backend_info: {
      designer_model: "test",
      image_model: "test",
      models: { designer: "test", image: "test" },
      demo_mode: false,
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

function setReadyAttempt(runId: string, candidateId: string) {
  useApp.setState({
    run_attempts: {
      [runId]: {
        run_id: runId,
        candidates: [attemptCandidate(runId, candidateId)],
        selection_phase: "idle",
        loading: false,
      },
    },
  });
}

function activeDraftPublicationTarget(conversationId?: string) {
  const state = useApp.getState();
  const targetConversationId = conversationId ?? state.current_conversation_id;
  const targetConversation = state.conversations[targetConversationId];
  const artifactId = targetConversation?.active_artifact_id;
  const draft = artifactId ? targetConversation.artifacts[artifactId] : undefined;
  assert.ok(draft?.candidate_draft, "expected an active candidate draft");
  assert.ok(draft.attempt_lineage?.source_run_id, "expected candidate source run");
  assert.ok(
    draft.attempt_lineage.source_candidate_id,
    "expected candidate source id",
  );
  return {
    conversationId: targetConversationId,
    artifactId: draft.artifact_id,
    sourceRunId: draft.attempt_lineage.source_run_id,
    sourceCandidateId: draft.attempt_lineage.source_candidate_id,
  };
}

function setupPreAckCandidatePublication(suffix: string) {
  const parentId = `bundle_pre_ack_${suffix}`;
  const sourceRunId = `run_pre_ack_source_${suffix}`;
  const publishRunId = `run_pre_ack_publish_${suffix}`;
  const candidateId = `poster-attempt-${suffix}`;
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = `job_pre_ack_${suffix}`;
  bundle.revision = 1;
  bundle.backend_state = "running";
  bundle.tasks.poster = {
    ...bundle.tasks.poster,
    status: "running",
    run_id: sourceRunId,
    authoring_run_id: sourceRunId,
  };
  for (const artifactType of ["deck", "landing", "video"] as const) {
    bundle.tasks[artifactType] = {
      ...bundle.tasks[artifactType],
      status: "complete",
      run_id: `run_pre_ack_${suffix}_${artifactType}`,
    };
  }
  const childId = bundle.tasks.poster.child_conversation_id;
  const draft: Artifact = {
    ...artifact(`pre_ack_draft_${suffix}`, "poster"),
    candidate_draft: true,
    attempt_lineage: {
      source_run_id: sourceRunId,
      source_attempt: 1,
      source_candidate_id: candidateId,
    },
  };
  resetStore({
    [parentId]: conversation(parentId, { paper_bundle: bundle, pending: true }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState(parentId, "poster"),
      messages: [{
        id: `msg_pre_ack_source_${suffix}`,
        role: "assistant",
        text: "Generating poster.",
        ts: 1,
        run_id: sourceRunId,
        status: "streaming",
      }],
      artifacts: { [draft.artifact_id]: draft },
      active_artifact_id: draft.artifact_id,
      pending: true,
      run_id: sourceRunId,
    }),
  }, childId);
  setReadyAttempt(sourceRunId, candidateId);
  const terminal = backendPaperBundleJob(parentId, bundle, 2);
  paperBundleCancelOverride = {
    status: 200,
    body: {
      ...terminal,
      state: "cancelled",
      terminal: true,
      terminal_at: 2,
      cancel_requested: true,
      cancel_requested_at: 2,
      completed_children: [],
      children: Object.fromEntries(Object.entries(terminal.children).map(
        ([artifactType, descriptor]) => [artifactType, {
          ...descriptor,
          state: "cancelled",
          terminal: true,
          process_free: true,
        }],
      )),
      confirmed: true,
      status: "cancelled",
    },
  };
  return {
    parentId,
    childId,
    sourceRunId,
    publishRunId,
    draft,
  };
}

test("startGenerate forwards its AbortSignal to fetch", async () => {
  const controller = new AbortController();
  let receivedSignal: AbortSignal | null = null;
  globalThis.fetch = (async (_input, init) => {
    receivedSignal = init?.signal ?? null;
    return jsonResponse({
      run_id: "run_signal",
      placeholder_message: { id: "msg", role: "assistant", text: "", ts: 1 },
    });
  }) as typeof fetch;

  await startGenerate({ brief: "test", attachments: [] }, controller.signal);

  assert.equal(receivedSignal, controller.signal);
});

test("candidate publish errors preserve every blocking finding", async () => {
  globalThis.fetch = (async () => jsonResponse({
    detail: {
      code: "candidate_draft_blocked",
      findings: [
        {
          issue_id: "missing_source",
          message: "A required source figure is missing.",
        },
        {
          issue_id: "text_overflow",
          message: "The title is clipped.",
        },
      ],
    },
  }, 422)) as typeof fetch;

  await assert.rejects(
    publishCandidateDraft("art_attempt-draft", "conv-attempt"),
    (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.match(error.message, /required source figure is missing/i);
      assert.match(error.message, /title is clipped/i);
      return true;
    },
  );
});

test("candidate publication fails closed until the exact attempt is loaded", async () => {
  const sourceRunId = "run_fail_closed";
  const candidateId = "poster-attempt-01";
  const ready = attemptCandidate(sourceRunId, candidateId);
  const cases = [
    { name: "missing attempt state", state: undefined },
    {
      name: "loading attempt state",
      state: {
        run_id: sourceRunId,
        candidates: [ready],
        selection_phase: "idle" as const,
        loading: true,
      },
    },
    {
      name: "errored attempt state",
      state: {
        run_id: sourceRunId,
        candidates: [ready],
        selection_phase: "idle" as const,
        loading: false,
        error: "attempt lookup failed",
      },
    },
    {
      name: "missing exact candidate",
      state: {
        run_id: sourceRunId,
        candidates: [attemptCandidate(sourceRunId, "poster-attempt-02")],
        selection_phase: "idle" as const,
        loading: false,
      },
    },
  ];
  const observations: Array<{ name: string; status: string; requests: number }> = [];

  for (const candidateCase of cases) {
    const draft: Artifact = {
      ...artifact(`fail_closed_${candidateCase.name.replaceAll(" ", "_")}`, "poster"),
      candidate_draft: true,
      attempt_lineage: {
        source_run_id: sourceRunId,
        source_attempt: 1,
        source_candidate_id: candidateId,
      },
    };
    resetStore({
      fail_closed: conversation("fail_closed", {
        artifacts: { [draft.artifact_id]: draft },
        active_artifact_id: draft.artifact_id,
      }),
    }, "fail_closed");
    useApp.setState({
      run_attempts: candidateCase.state
        ? { [sourceRunId]: candidateCase.state }
        : {},
    });
    let requests = 0;
    globalThis.fetch = (async () => {
      requests += 1;
      return jsonResponse({ detail: "publication should not reach the network" }, 500);
    }) as typeof fetch;

    const [result] = await Promise.allSettled([
      useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget()),
    ]);
    observations.push({
      name: candidateCase.name,
      status: result.status,
      requests,
    });
  }

  assert.deepEqual(observations, cases.map(({ name }) => ({
    name,
    status: "rejected",
    requests: 0,
  })));
});

test("candidate draft publication keeps the rendered child target when current conversation changes", async () => {
  const parentId = "conv_render_target_parent";
  const sourceRunId = "run_render_target_source";
  const candidateId = "poster-attempt-render-target";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.tasks.poster = {
    ...bundle.tasks.poster,
    status: "complete",
    run_id: sourceRunId,
    authoring_run_id: sourceRunId,
  };
  const childId = bundle.tasks.poster.child_conversation_id;
  const draft: Artifact = {
    ...artifact("render_target_draft", "poster"),
    candidate_draft: true,
    attempt_lineage: {
      status: "draft",
      source_run_id: sourceRunId,
      source_attempt: 1,
      source_candidate_id: candidateId,
    },
  };
  resetStore({
    [parentId]: conversation(parentId, { paper_bundle: bundle }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState(parentId, "poster"),
      artifacts: { [draft.artifact_id]: draft },
      active_artifact_id: draft.artifact_id,
    }),
  }, childId);
  setReadyAttempt(sourceRunId, candidateId);
  useApp.setState({ current_conversation_id: parentId });

  const published: Artifact = {
    ...artifact("render_target_published", "poster"),
    attempt_lineage: {
      status: "published",
      source_run_id: sourceRunId,
      source_attempt: 1,
      source_candidate_id: candidateId,
    },
  };
  const requests: Array<{ url: string; body?: unknown }> = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    requests.push({
      url,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    if (url === `/api/artifacts/${draft.artifact_id}/publish-candidate-draft`) {
      return jsonResponse({
        run_id: "run_render_target_publish",
        start_token: "publish-token",
        progress_mode: "attempt_publish",
      });
    }
    if (url === "/api/runs/run_render_target_publish/start") {
      return jsonResponse({
        run_id: "run_render_target_publish",
        progress_mode: "attempt_publish",
      });
    }
    if (url === "/api/runs/run_render_target_publish/artifact") {
      return jsonResponse({
        message: {
          id: "msg_render_target_publish",
          role: "assistant",
          text: "Published selected attempt.",
          ts: 2,
          run_id: "run_render_target_publish",
          artifact_id: published.artifact_id,
          status: "done",
        },
        artifact: published,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const publishing = useApp.getState().publishActiveCandidateDraft({
    conversationId: childId,
    artifactId: draft.artifact_id,
    sourceRunId,
    sourceCandidateId: candidateId,
  });
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === "/api/runs/run_render_target_publish/events",
    ),
    "candidate publication did not retain the rendered child target",
  );
  MockEventSource.instances.find(
    (source) => source.url === "/api/runs/run_render_target_publish/events",
  )?.emit("run.done");
  await publishing;

  assert.deepEqual(requests[0], {
    url: `/api/artifacts/${draft.artifact_id}/publish-candidate-draft`,
    body: { conversation_id: childId },
  });
  assert.equal(useApp.getState().current_conversation_id, parentId);
  assert.equal(
    useApp.getState().conversations[childId].published_artifact_id,
    published.artifact_id,
  );
});

test("candidate draft publication rejects a stale rendered target instead of resolving silently", async () => {
  const conversationId = "conv_stale_render_target";
  resetStore({
    [conversationId]: conversation(conversationId),
  }, conversationId);
  let requests = 0;
  globalThis.fetch = (async () => {
    requests += 1;
    return jsonResponse({ detail: "stale target must not reach the network" }, 500);
  }) as typeof fetch;

  await assert.rejects(
    useApp.getState().publishActiveCandidateDraft({
      conversationId,
      artifactId: "art_missing_draft",
      sourceRunId: "run_missing_source",
      sourceCandidateId: "poster-attempt-missing",
    }),
    /draft.*no longer available/i,
  );
  assert.equal(requests, 0);
});

test("publishing an attempt fork updates only its Paper All-in-One child", async () => {
  const parentId = "conv_attempt_parent";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.tasks.poster = {
    ...bundle.tasks.poster,
    status: "complete",
    run_id: "run_poster_author",
    artifact_id: "art_run_poster_old",
  };
  bundle.tasks.deck = {
    ...bundle.tasks.deck,
    status: "complete",
    run_id: "run_deck",
    artifact_id: "art_run_deck",
  };
  const childId = bundle.tasks.poster.child_conversation_id;
  const draft: Artifact = {
    ...artifact("draft_attempt", "poster"),
    candidate_draft: true,
    attempt_lineage: {
      source_run_id: "run_poster_author",
      source_attempt: 2,
      source_candidate_id: "poster-attempt-02",
    },
  };
  resetStore({
    [parentId]: conversation(parentId, {
      paper_bundle: bundle,
      artifacts: {
        art_run_poster_old: artifact("run_poster_old", "poster"),
        art_run_deck: artifact("run_deck", "deck"),
      },
    }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState(parentId, "poster"),
      artifacts: { [draft.artifact_id]: draft },
      active_artifact_id: draft.artifact_id,
    }),
  }, childId);
  setReadyAttempt("run_poster_author", "poster-attempt-02");

  const published = artifact("published_attempt", "poster");
  let releaseStart!: (response: Response) => void;
  let artifactReads = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/artifacts/art_draft_attempt/publish-candidate-draft") {
      assert.equal(init?.method, "POST");
      assert.equal(new Headers(init.headers).get("X-Autodesign-Reserve-Only"), "true");
      return jsonResponse({
        run_id: "run_publish_attempt",
        start_token: "start-token",
        progress_mode: "attempt_publish",
      });
    }
    if (url === "/api/runs/run_publish_attempt/start") {
      assert.equal(
        useApp.getState().conversations[childId].run_id,
        "run_publish_attempt",
        "publish onReserved must expose the child run before /start",
      );
      assert.equal(
        useApp.getState().runs_progress[childId]?.run_id,
        "run_publish_attempt",
      );
      return new Promise<Response>((resolve) => { releaseStart = resolve; });
    }
    if (url === "/api/runs/run_publish_attempt/artifact") {
      artifactReads += 1;
      return jsonResponse({
        message: {
          id: "msg_publish_attempt",
          role: "assistant",
          text: "Published selected attempt.",
          ts: 2,
          run_id: "run_publish_attempt",
          artifact_id: published.artifact_id,
          status: "done",
        },
        artifact: published,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const publishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget());
  await waitFor(() => typeof releaseStart === "function", "publish /start did not begin");
  assert.equal(artifactReads, 0);
  assert.equal(
    useApp.getState().conversations[childId].active_artifact_id,
    draft.artifact_id,
  );
  assert.equal(
    (useApp.getState().conversations[parentId].paper_bundle as PaperBundleParentState)
      .tasks.poster.artifact_id,
    "art_run_poster_old",
  );
  releaseStart(jsonResponse({
    run_id: "run_publish_attempt",
    progress_mode: "attempt_publish",
  }));
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === "/api/runs/run_publish_attempt/events",
    ),
    "publish event source was not opened",
  );
  assert.equal(artifactReads, 0);
  MockEventSource.instances.find(
    (source) => source.url === "/api/runs/run_publish_attempt/events",
  )?.emit("run.done");
  await publishing;

  const state = useApp.getState();
  assert.equal(
    state.conversations[childId].active_artifact_id,
    "art_published_attempt",
  );
  const parent = state.conversations[parentId];
  assert.equal(parent.paper_bundle?.kind, "parent");
  if (parent.paper_bundle?.kind !== "parent") return;
  assert.equal(
    parent.paper_bundle.tasks.poster.artifact_id,
    "art_published_attempt",
  );
  assert.equal(parent.paper_bundle.tasks.poster.run_id, "run_poster_author");
  assert.equal(parent.paper_bundle.tasks.deck.artifact_id, "art_run_deck");
  assert.ok(parent.artifacts.art_published_attempt);
});

test("a repaired Canvas draft from a blocked candidate publishes and recovers authoritatively", async () => {
  const fixture = setupTerminalFailedReadyAttempt("canvas_authoritative");
  useApp.setState({
    run_attempts: {
      [fixture.sourceRunId]: {
        run_id: fixture.sourceRunId,
        candidates: [attemptCandidate(
          fixture.sourceRunId,
          fixture.candidateId,
          "blocked",
        )],
        selection_phase: "idle",
        loading: false,
      },
    },
  });
  const draft: Artifact = {
    ...artifact("canvas_authoritative_draft", "poster"),
    candidate_draft: true,
    attempt_lineage: {
      source_run_id: fixture.sourceRunId,
      source_attempt: 1,
      source_candidate_id: fixture.candidateId,
      source_candidate_sha256: "a".repeat(64),
    },
  };
  useApp.setState((state) => ({
    conversations: {
      ...state.conversations,
      [fixture.childId]: {
        ...state.conversations[fixture.childId],
        artifacts: { [draft.artifact_id]: draft },
        active_artifact_id: draft.artifact_id,
      },
    },
  }));
  const authoritativeJob = completedBackendPaperBundlePublicationJob(
    fixture.parentId,
    fixture.bundle,
    8,
    fixture.sourceRunId,
    fixture.publishRunId,
    fixture.candidateId,
  );
  paperBundleListOverride = [authoritativeJob];
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/artifacts/${draft.artifact_id}/publish-candidate-draft`) {
      return jsonResponse({
        run_id: fixture.publishRunId,
        start_token: "canvas-authoritative-token",
        progress_mode: "attempt_publish",
      });
    }
    if (url === `/api/runs/${fixture.publishRunId}/start`) {
      return jsonResponse({
        run_id: fixture.publishRunId,
        progress_mode: "attempt_publish",
      });
    }
    if (url === `/api/runs/${fixture.publishRunId}/artifact`) {
      return jsonResponse(directAttemptPublicationResponse(
        fixture.publishRunId,
        fixture.sourceRunId,
        fixture.candidateId,
      ));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const publishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget());
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === `/api/runs/${fixture.publishRunId}/events`,
    ),
    "Canvas publication listener did not open",
  );
  MockEventSource.instances.find(
    (source) => source.url === `/api/runs/${fixture.publishRunId}/events`,
  )?.emit("run.done");
  await publishing;

  const liveState = useApp.getState();
  const liveParent = liveState.conversations[fixture.parentId];
  const liveBundle = liveParent.paper_bundle as PaperBundleParentState;
  const publishedArtifactId = `art_${fixture.publishRunId}`;
  assert.deepEqual({
    backendState: liveBundle.backend_state,
    revision: liveBundle.revision,
    taskStatus: liveBundle.tasks.poster.status,
    taskRunId: liveBundle.tasks.poster.run_id,
    authoringRunId: liveBundle.tasks.poster.authoring_run_id,
    artifactId: liveBundle.tasks.poster.artifact_id,
    childActive: liveState.conversations[fixture.childId].active_artifact_id,
    parentArtifact: liveParent.artifacts[publishedArtifactId]?.artifact_id,
  }, {
    backendState: "completed",
    revision: 8,
    taskStatus: "complete",
    taskRunId: fixture.publishRunId,
    authoringRunId: fixture.sourceRunId,
    artifactId: publishedArtifactId,
    childActive: publishedArtifactId,
    parentArtifact: publishedArtifactId,
  });

  resetStore({}, fixture.parentId);
  paperBundleListOverride = [authoritativeJob];
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${fixture.publishRunId}/artifact`) {
      return jsonResponse(publishedArtifactResponse(
        fixture.publishRunId,
        fixture.sourceRunId,
        {
          source_candidate_id: fixture.candidateId,
          source_candidate_sha256: "a".repeat(64),
        },
      ));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().recoverPaperBundles();

  const coldState = useApp.getState();
  const coldParent = coldState.conversations[fixture.parentId];
  const coldBundle = coldParent.paper_bundle as PaperBundleParentState;
  assert.deepEqual({
    backendState: coldBundle.backend_state,
    revision: coldBundle.revision,
    taskStatus: coldBundle.tasks.poster.status,
    taskRunId: coldBundle.tasks.poster.run_id,
    authoringRunId: coldBundle.tasks.poster.authoring_run_id,
    artifactId: coldBundle.tasks.poster.artifact_id,
    childActive: coldState.conversations[fixture.childId].active_artifact_id,
    parentActive: coldParent.active_artifact_id,
    childArtifact: coldState.conversations[fixture.childId]
      .artifacts[publishedArtifactId]?.artifact_id,
    parentArtifact: coldParent.artifacts[publishedArtifactId]?.artifact_id,
  }, {
    backendState: "completed",
    revision: 8,
    taskStatus: "complete",
    taskRunId: fixture.publishRunId,
    authoringRunId: fixture.sourceRunId,
    artifactId: publishedArtifactId,
    childActive: publishedArtifactId,
    parentActive: publishedArtifactId,
    childArtifact: publishedArtifactId,
    parentArtifact: publishedArtifactId,
  });
});

test("late Canvas publication cannot install into a replacement bundle job", async () => {
  const fixture = setupTerminalFailedReadyAttempt("canvas_stale_job");
  const draft: Artifact = {
    ...artifact("canvas_stale_job_draft", "poster"),
    candidate_draft: true,
    attempt_lineage: {
      source_run_id: fixture.sourceRunId,
      source_attempt: 1,
      source_candidate_id: fixture.candidateId,
      source_candidate_sha256: "a".repeat(64),
    },
  };
  useApp.setState((state) => ({
    conversations: {
      ...state.conversations,
      [fixture.childId]: {
        ...state.conversations[fixture.childId],
        artifacts: { [draft.artifact_id]: draft },
        active_artifact_id: draft.artifact_id,
      },
    },
  }));
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/artifacts/${draft.artifact_id}/publish-candidate-draft`) {
      return jsonResponse({
        run_id: fixture.publishRunId,
        start_token: "canvas-stale-token",
        progress_mode: "attempt_publish",
      });
    }
    if (url === `/api/runs/${fixture.publishRunId}/start`) {
      return jsonResponse({
        run_id: fixture.publishRunId,
        progress_mode: "attempt_publish",
      });
    }
    if (url === `/api/runs/${fixture.publishRunId}/artifact`) {
      return jsonResponse(directAttemptPublicationResponse(
        fixture.publishRunId,
        fixture.sourceRunId,
        fixture.candidateId,
      ));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const publishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget());
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === `/api/runs/${fixture.publishRunId}/events`,
    ),
    "stale Canvas publication listener did not open",
  );
  useApp.setState((state) => {
    const parent = state.conversations[fixture.parentId];
    if (parent.paper_bundle?.kind !== "parent") return state;
    return {
      conversations: {
        ...state.conversations,
        [fixture.parentId]: {
          ...parent,
          paper_bundle: {
            ...parent.paper_bundle,
            job_id: "job_canvas_replacement",
            revision: 20,
            backend_state: "running",
            tasks: {
              ...parent.paper_bundle.tasks,
              poster: {
                ...parent.paper_bundle.tasks.poster,
                status: "running",
                run_id: "run_canvas_replacement",
                authoring_run_id: "run_canvas_replacement",
                artifact_id: undefined,
                terminal: false,
                process_free: false,
              },
            },
          },
        },
      },
    };
  });
  MockEventSource.instances.find(
    (source) => source.url === `/api/runs/${fixture.publishRunId}/events`,
  )?.emit("run.done");
  await publishing;

  const state = useApp.getState();
  const parentBundle = state.conversations[fixture.parentId]
    .paper_bundle as PaperBundleParentState;
  const child = state.conversations[fixture.childId];
  assert.deepEqual({
    jobId: parentBundle.job_id,
    revision: parentBundle.revision,
    status: parentBundle.tasks.poster.status,
    runId: parentBundle.tasks.poster.run_id,
    artifactId: parentBundle.tasks.poster.artifact_id,
    childActive: child.active_artifact_id,
    staleArtifactInstalled: Boolean(child.artifacts[`art_${fixture.publishRunId}`]),
    childPending: child.pending,
    childRunId: child.run_id,
  }, {
    jobId: "job_canvas_replacement",
    revision: 20,
    status: "running",
    runId: "run_canvas_replacement",
    artifactId: undefined,
    childActive: draft.artifact_id,
    staleArtifactInstalled: false,
    childPending: false,
    childRunId: undefined,
  });
});

test("candidate publish cancellation during /start stays cancelling until confirmed", async () => {
  const draft: Artifact = {
    ...artifact("publish_cancel_draft", "landing"),
    candidate_draft: true,
    attempt_lineage: {
      source_run_id: "run_publish_source",
      source_attempt: 2,
      source_candidate_id: "landing-attempt-02",
    },
  };
  resetStore({
    ordinary: conversation("ordinary", {
      artifacts: { [draft.artifact_id]: draft },
      active_artifact_id: draft.artifact_id,
    }),
  }, "ordinary");
  setReadyAttempt("run_publish_source", "landing-attempt-02");
  let releaseStart!: (response: Response) => void;
  let releaseCancel!: (response: Response) => void;
  let publishRequests = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/artifacts/art_publish_cancel_draft/publish-candidate-draft") {
      publishRequests += 1;
      if (publishRequests > 1) throw new Error("duplicate publish reached network");
      return jsonResponse({
        run_id: "run_publish_start_cancel",
        start_token: "start-token",
        progress_mode: "attempt_publish",
      });
    }
    if (url === "/api/runs/run_publish_start_cancel/start") {
      return new Promise<Response>((resolve) => { releaseStart = resolve; });
    }
    if (url === "/api/runs/run_publish_start_cancel/cancel") {
      return new Promise<Response>((resolve) => { releaseCancel = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const publishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget());
  await waitFor(() => typeof releaseStart === "function", "publish /start did not begin");
  const cancelling = useApp.getState().cancelRun("ordinary");
  await waitFor(() => typeof releaseCancel === "function", "publish cancellation did not begin");
  releaseStart(jsonResponse({ detail: "run was cancelled" }, 409));
  await publishing;

  try {
    const state = useApp.getState();
    const current = state.conversations.ordinary;
    assert.equal(current.active_artifact_id, draft.artifact_id);
    assert.equal(current.published_artifact_id, undefined);
    assert.equal(current.run_id, "run_publish_start_cancel");
    assert.equal(state.runs_progress.ordinary?.phase, "cancelling");
    assert.ok(current.messages.every((message) => !/failed/i.test(message.text)));
    await assert.rejects(
      useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget()),
      /current run.*still active/i,
    );
    assert.equal(publishRequests, 1);
  } finally {
    releaseCancel(confirmedCancellation("run_publish_start_cancel"));
    await cancelling;
  }

  const cancelled = useApp.getState();
  assert.equal(cancelled.conversations.ordinary.pending, false);
  assert.equal(cancelled.conversations.ordinary.run_id, undefined);
  assert.equal(cancelled.conversations.ordinary.active_artifact_id, draft.artifact_id);
  assert.equal(cancelled.runs_progress.ordinary, undefined);
});

test("candidate publish persists its job kind, run id, and source before start", async () => {
  const draft: Artifact = {
    ...artifact("persist_candidate_draft", "landing"),
    candidate_draft: true,
    attempt_lineage: {
      source_run_id: "run_persist_candidate_source",
      source_attempt: 2,
      source_candidate_id: "landing-attempt-02",
    },
  };
  resetStore({
    ordinary: conversation("ordinary", {
      messages: [{
        id: "msg_candidate_source",
        role: "assistant",
        text: "Candidate draft",
        ts: 1,
        artifact_id: draft.artifact_id,
        status: "done",
      }],
      artifacts: { [draft.artifact_id]: draft },
      active_artifact_id: draft.artifact_id,
    }),
  }, "ordinary");
  setReadyAttempt("run_persist_candidate_source", "landing-attempt-02");
  let releaseStart!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/artifacts/art_persist_candidate_draft/publish-candidate-draft") {
      return jsonResponse({
        run_id: "run_persist_candidate_publish",
        start_token: "start-token",
        progress_mode: "attempt_publish",
      });
    }
    if (url === "/api/runs/run_persist_candidate_publish/start") {
      return new Promise<Response>((resolve) => { releaseStart = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const publishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget());
  await waitFor(() => typeof releaseStart === "function", "candidate publish /start did not begin");
  try {
    const raw = localStorage.getItem("autodesign.web.v1");
    assert.ok(raw);
    const persisted = JSON.parse(raw).state.conversations.ordinary as Conversation;
    assert.equal(persisted.pending, true);
    assert.equal(persisted.run_id, "run_persist_candidate_publish");
    const job = persisted.messages.find((message) => (
      message.run_id === "run_persist_candidate_publish"
    ));
    assert.equal(job?.task_type, "candidate_publish");
    assert.equal(job?.task_payload?.source_artifact_id, draft.artifact_id);
    assert.equal(job?.task_payload?.source_run_id, "run_persist_candidate_source");
    assert.equal(job?.task_payload?.source_candidate_id, "landing-attempt-02");
  } finally {
    releaseStart(jsonResponse({ detail: "stop test publish" }, 500));
    await assert.rejects(publishing);
  }
});

test("rehydration finishes candidate publish postprocessing from persisted job metadata", async () => {
  resetStore({ current: conversation("current") }, "current");
  const draft: Artifact = {
    ...artifact("refresh_candidate_draft", "landing"),
    candidate_draft: true,
    attempt_lineage: {
      source_run_id: "run_refresh_candidate_source",
      source_attempt: 2,
      source_candidate_id: "landing-attempt-02",
    },
  };
  const persisted = conversation("persisted", {
    messages: [{
      id: "msg_refresh_candidate",
      role: "assistant",
      text: "Publishing selected attempt.",
      ts: 2,
      run_id: "run_refresh_candidate_publish",
      artifact_id: draft.artifact_id,
      status: "streaming",
      task_type: "candidate_publish",
      task_payload: {
        artifact_type: "landing",
        source_artifact_id: draft.artifact_id,
        source_run_id: "run_refresh_candidate_source",
        source_candidate_id: "landing-attempt-02",
      },
    }],
    artifacts: { [draft.artifact_id]: draft },
    active_artifact_id: draft.artifact_id,
    pending: true,
    run_id: "run_refresh_candidate_publish",
  });
  localStorage.setItem("autodesign.web.v1", JSON.stringify({
    version: 1,
    state: {
      conversations: { persisted },
      current_conversation_id: "persisted",
      history_user_scope: "test-user",
    },
  }));
  const published: Artifact = {
    ...artifact("refresh_candidate_published", "landing"),
    attempt_lineage: {
      source_run_id: "run_refresh_candidate_source",
      source_attempt: 2,
      source_candidate_id: "landing-attempt-02",
      status: "published",
    },
  };
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({ conversations: {}, imported_runs: 0, user_isolated: true });
    }
    if (url === "/api/paper-bundles") return jsonResponse([]);
    if (url === "/api/runs/run_refresh_candidate_publish/artifact") {
      return jsonResponse({
        message: {
          id: "msg_refresh_candidate",
          role: "assistant",
          text: "Published selected attempt.",
          ts: 3,
          run_id: "run_refresh_candidate_publish",
          artifact_id: published.artifact_id,
          status: "done",
        },
        artifact: published,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.persist.rehydrate();
  await useApp.getState().loadServerHistory();
  await waitFor(
    () => useApp.getState().conversations.persisted?.published_artifact_id === published.artifact_id,
    "candidate publish was not recovered and postprocessed",
  );

  const recovered = useApp.getState().conversations.persisted;
  assert.equal(recovered.active_artifact_id, published.artifact_id);
  assert.equal(recovered.pending, false);
  assert.equal(recovered.run_id, undefined);
  assert.ok(recovered.artifacts[published.artifact_id]);
  const saved = JSON.parse(localStorage.getItem("autodesign.web.v1") ?? "{}");
  assert.equal(
    saved.state?.conversations?.persisted?.published_artifact_id,
    published.artifact_id,
  );
});

test("a late source failure cannot replace a published attempt fork", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  const artifactTypes: ArtifactType[] = ["poster", "deck", "landing", "video"];
  let nextRun = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        needs_setup: false,
      });
    }
    if (url === "/api/generate") {
      const artifactType = artifactTypes[nextRun];
      nextRun += 1;
      return jsonResponse({
        run_id: `run_source_${artifactType}`,
        placeholder_message: {
          id: `msg_source_${artifactType}`,
          role: "assistant",
          text: "",
          ts: 1,
          run_id: `run_source_${artifactType}`,
          status: "streaming",
        },
      });
    }
    if (
      url === "/api/artifacts/art_attempt_draft/publish-candidate-draft"
      && init?.method === "POST"
    ) {
      return jsonResponse({
        run_id: "run_publish_attempt",
        start_token: "start-token",
        progress_mode: "attempt_publish",
      });
    }
    if (url === "/api/runs/run_publish_attempt/start") {
      return jsonResponse({
        run_id: "run_publish_attempt",
        progress_mode: "attempt_publish",
      });
    }
    if (url === "/api/runs/run_publish_attempt/artifact") {
      const published = {
        ...artifact("published_attempt", "poster"),
        candidate_draft: false,
        attempt_lineage: {
          status: "published" as const,
          source_run_id: "run_source_poster",
          source_attempt: 2,
          source_candidate_id: "poster-attempt-02",
        },
      };
      return jsonResponse({
        message: {
          id: "msg_publish_attempt",
          role: "assistant",
          text: "Published selected attempt.",
          ts: 2,
          run_id: "run_publish_attempt",
          artifact_id: published.artifact_id,
          status: "done",
        },
        artifact: published,
      });
    }
    const match = url.match(
      /^\/api\/runs\/run_source_(poster|deck|landing|video)\/artifact$/,
    );
    if (match) {
      const artifactType = match[1] as ArtifactType;
      if (artifactType === "poster") {
        return jsonResponse({
          message: {
            id: "msg_source_poster",
            role: "assistant",
            text: "The source author was terminated.",
            ts: 3,
            run_id: "run_source_poster",
            status: "error",
            failure: {
              status: "fail",
              agent_last_note: "The source author was terminated.",
              produced_files: [],
            },
          },
          artifact: null,
        });
      }
      return jsonResponse(
        responseForRun(`run_source_${artifactType}`, artifactType),
      );
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await waitFor(
    () => MockEventSource.instances.length === 4,
    "bundle SSE streams did not start",
  );
  const parent = useApp.getState().conversations.bundle;
  assert.equal(parent.paper_bundle?.kind, "parent");
  if (parent.paper_bundle?.kind !== "parent") return;
  const childId = parent.paper_bundle.tasks.poster.child_conversation_id;
  const draft: Artifact = {
    ...artifact("attempt_draft", "poster"),
    candidate_draft: true,
    attempt_lineage: {
      status: "draft",
      source_run_id: "run_source_poster",
      source_attempt: 2,
      source_candidate_id: "poster-attempt-02",
    },
  };
  useApp.setState((state) => {
    const child = state.conversations[childId];
    return {
      conversations: {
        ...state.conversations,
        [childId]: {
          ...child,
          artifacts: {
            ...child.artifacts,
            [draft.artifact_id]: draft,
          },
          active_artifact_id: draft.artifact_id,
        },
      },
      current_conversation_id: childId,
    };
  });
  setReadyAttempt("run_source_poster", "poster-attempt-02");

  assert.equal(
    useApp.getState().conversations[childId].active_artifact_id,
    "art_attempt_draft",
  );
  assert.equal(useApp.getState().current_conversation_id, childId);
  const publishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget());
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === "/api/runs/run_publish_attempt/events",
    ),
    "publish event source was not opened",
  );
  const sourceAuthor = MockEventSource.instances.find(
    (source) => source.url === "/api/runs/run_source_poster/events",
  );
  assert.equal(sourceAuthor?.readyState, 1, "source author listener must remain open");
  const publishingChild = useApp.getState().conversations[childId];
  assert.equal(publishingChild.run_id, "run_source_poster");
  assert.equal(publishingChild.pending, true);
  assert.equal(useApp.getState().runs_progress[childId]?.run_id, "run_source_poster");
  assert.equal(
    useApp.getState().runs_progress[`${childId}:candidate-publish`]?.run_id,
    "run_publish_attempt",
  );
  MockEventSource.instances.find(
    (source) => source.url === "/api/runs/run_publish_attempt/events",
  )?.emit("run.done");
  await publishing;
  assert.equal(
    useApp.getState().conversations[childId].active_artifact_id,
    "art_published_attempt",
  );
  assert.ok(
    useApp.getState().conversations[childId].messages.some(
      (message) => message.artifact_id === "art_published_attempt",
    ),
    JSON.stringify(useApp.getState().conversations[childId].messages),
  );
  for (const source of MockEventSource.instances) {
    source.emit(source.url.includes("run_source_poster") ? "run.error" : "run.done");
  }
  await start;

  const state = useApp.getState();
  const child = state.conversations[childId];
  assert.equal(child.active_artifact_id, "art_published_attempt");
  assert.equal(
    child.messages.find(
      (message) => message.artifact_id === "art_published_attempt",
    )?.status,
    "done",
  );
  const completedParent = state.conversations.bundle;
  assert.equal(completedParent.paper_bundle?.kind, "parent");
  if (completedParent.paper_bundle?.kind !== "parent") return;
  assert.equal(completedParent.paper_bundle.tasks.poster.status, "complete");
  assert.equal(
    completedParent.paper_bundle.tasks.poster.artifact_id,
    "art_published_attempt",
  );
});

test("backend-managed candidate publication stays isolated from source ownership and competing actions", async () => {
  const parentId = "bundle_backend_publish";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_backend_publish";
  bundle.revision = 4;
  bundle.backend_state = "running";
  bundle.tasks.poster = {
    ...bundle.tasks.poster,
    status: "running",
    run_id: "run_backend_source",
    authoring_run_id: "run_backend_source",
  };
  for (const artifactType of ["deck", "landing", "video"] as const) {
    bundle.tasks[artifactType] = {
      ...bundle.tasks[artifactType],
      status: "complete",
      run_id: `run_backend_${artifactType}`,
      artifact_id: `art_backend_${artifactType}`,
    };
  }
  const childId = bundle.tasks.poster.child_conversation_id;
  const candidateId = "poster-attempt-01";
  const draft: Artifact = {
    ...artifact("backend_draft", "poster"),
    candidate_draft: true,
    attempt_lineage: {
      materialization_version: 2,
      source_run_id: "run_backend_source",
      source_attempt: 1,
      source_candidate_id: candidateId,
      source_candidate_sha256: "a".repeat(64),
    },
  };
  const candidate = attemptCandidate("run_backend_source", candidateId);
  resetStore({
    [parentId]: conversation(parentId, { paper_bundle: bundle, pending: true }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState(parentId, "poster"),
      messages: [{
        id: "msg_backend_source",
        role: "assistant",
        text: "Generating poster",
        ts: 1,
        run_id: "run_backend_source",
        status: "streaming",
      }],
      artifacts: { [draft.artifact_id]: draft },
      active_artifact_id: draft.artifact_id,
      pending: true,
      run_id: "run_backend_source",
    }),
  }, childId);
  useApp.setState({
    run_attempts: {
      run_backend_source: {
        run_id: "run_backend_source",
        candidates: [candidate],
        selection_phase: "idle",
        loading: false,
      },
    },
    runs_progress: {
      [childId]: initialProgress("run_backend_source"),
    },
  });

  let selectionRequests = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/artifacts/art_backend_draft/publish-candidate-draft") {
      return jsonResponse({
        run_id: "run_backend_publish",
        start_token: "start-token",
        progress_mode: "attempt_publish",
      });
    }
    if (url === "/api/runs/run_backend_publish/start") {
      return jsonResponse({
        run_id: "run_backend_publish",
        progress_mode: "attempt_publish",
      });
    }
    if (url === "/api/runs/run_backend_publish/artifact") {
      return jsonResponse({
        message: {
          id: "msg_backend_publish",
          role: "assistant",
          text: "Published selected attempt.",
          ts: 2,
          run_id: "run_backend_publish",
          artifact_id: "art_backend_published",
          status: "done",
        },
        artifact: {
          ...artifact("backend_published", "poster"),
          attempt_lineage: {
            status: "published",
            source_run_id: "run_backend_source",
            source_attempt: 1,
            source_candidate_id: candidateId,
          },
        },
      });
    }
    if (url === "/api/runs/run_backend_source/artifact") {
      return jsonResponse({
        message: {
          id: "msg_backend_source_cancelled",
          role: "assistant",
          text: "Run cancelled.",
          ts: 3,
          run_id: "run_backend_source",
          status: "error",
          failure: {
            status: "cancelled",
            produced_files: [],
            artifact_type: "poster",
          },
        },
        artifact: null,
      });
    }
    if (url === "/api/runs/run_backend_source/attempts/1/select" && init?.method === "POST") {
      selectionRequests += 1;
      return jsonResponse({
        run_id: "run_backend_source",
        candidates: [candidate],
        selection: {
          candidate_id: candidateId,
          source_attempt: 1,
          state: "complete",
          artifact_id: "art_backend_published",
        },
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const publishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget());
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === "/api/runs/run_backend_publish/events",
    ),
    "backend candidate publication did not start",
  );
  const competing = await Promise.allSettled([
    useApp.getState().selectAttempt("run_backend_source", candidate),
    useApp.getState().openAttemptInCanvas("run_backend_source", candidate, childId),
    useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget()),
  ]);
  MockEventSource.instances.find(
    (source) => source.url === "/api/runs/run_backend_publish/events",
  )?.emit("run.done");
  await publishing;

  paperBundleListOverride = [backendPaperBundleJob(parentId, bundle, 5)];
  await useApp.getState().recoverPaperBundles();
  const recovered = useApp.getState();
  const recoveredBundle = recovered.conversations[parentId]
    .paper_bundle as PaperBundleParentState;

  assert.deepEqual({
    competing: competing.map((result) => result.status),
    selectionRequests,
    backendState: recoveredBundle.backend_state,
    revision: recoveredBundle.revision,
    sourceRunId: recovered.conversations[childId].run_id,
    publishedArtifactId: recoveredBundle.tasks.poster.artifact_id,
  }, {
    competing: ["rejected", "rejected", "rejected"],
    selectionRequests: 0,
    backendState: "running",
    revision: 5,
    sourceRunId: "run_backend_source",
    publishedArtifactId: "art_backend_published",
  });
  const sourceRecovery = MockEventSource.instances.find(
    (source) => source.url === "/api/runs/run_backend_source/events",
  );
  sourceRecovery?.emit("run.cancelled");
  await waitFor(
    () => sourceRecovery?.readyState === MockEventSource.CLOSED,
    "backend source recovery did not release its terminal wait",
  );
});

test("task and bundle cancellation settle both source and derived publication owners", async () => {
  const observations: Array<{
    mode: string;
    runCancels: string[];
    bundleCancels: string[];
    sourceClosed: boolean;
    publicationClosed: boolean;
    sourceProgress: boolean;
    publicationProgress: boolean;
    publicationPhase: string | undefined;
    retryableCancellation: boolean;
  }> = [];

  for (const mode of ["task", "bundle", "task-failure"] as const) {
    const parentId = `bundle_dual_cancel_${mode}`;
    const sourceRunId = `run_dual_cancel_source_${mode}`;
    const publishRunId = `run_dual_cancel_publish_${mode}`;
    const bundle = createPaperBundleParentState(parentId, "paper.pdf");
    bundle.job_id = `job_dual_cancel_${mode}`;
    bundle.revision = 1;
    bundle.backend_state = "running";
    bundle.tasks.poster = {
      ...bundle.tasks.poster,
      status: "running",
      run_id: sourceRunId,
      authoring_run_id: sourceRunId,
    };
    for (const artifactType of ["deck", "landing", "video"] as const) {
      bundle.tasks[artifactType] = {
        ...bundle.tasks[artifactType],
        status: "complete",
        run_id: `run_dual_cancel_${mode}_${artifactType}`,
      };
    }
    const childId = bundle.tasks.poster.child_conversation_id;
    const candidateId = "poster-attempt-01";
    const draft: Artifact = {
      ...artifact(`dual_cancel_draft_${mode}`, "poster"),
      candidate_draft: true,
      attempt_lineage: {
        source_run_id: sourceRunId,
        source_attempt: 1,
        source_candidate_id: candidateId,
      },
    };
    resetStore({
      [parentId]: conversation(parentId, { paper_bundle: bundle, pending: true }),
      [childId]: conversation(childId, {
        paper_bundle: createPaperBundleChildState(parentId, "poster"),
        messages: [{
          id: `msg_dual_cancel_source_${mode}`,
          role: "assistant",
          text: "Generating poster.",
          ts: 1,
          run_id: sourceRunId,
          status: "streaming",
        }],
        artifacts: { [draft.artifact_id]: draft },
        active_artifact_id: draft.artifact_id,
        pending: true,
        run_id: sourceRunId,
      }),
    }, childId);
    useApp.setState({
      run_attempts: {
        [sourceRunId]: {
          run_id: sourceRunId,
          candidates: [attemptCandidate(sourceRunId, candidateId)],
          selection_phase: "idle",
          loading: false,
        },
      },
    });
    paperBundleListOverride = [backendPaperBundleJob(parentId, bundle, 1)];
    if (mode === "bundle") {
      const terminal = backendPaperBundleJob(parentId, bundle, 2);
      paperBundleCancelOverride = {
        status: 200,
        body: {
          ...terminal,
          state: "cancelled",
          terminal: true,
          terminal_at: 2,
          cancel_requested: true,
          cancel_requested_at: 2,
          completed_children: [],
          children: Object.fromEntries(Object.entries(terminal.children).map(
            ([artifactType, descriptor]) => [artifactType, {
              ...descriptor,
              state: "cancelled",
              terminal: true,
              process_free: true,
            }],
          )),
          confirmed: true,
          status: "cancelled",
        },
      };
    }
    const runCancels: string[] = [];
    globalThis.fetch = (async (input) => {
      const url = String(input);
      if (url === `/api/artifacts/${draft.artifact_id}/publish-candidate-draft`) {
        return jsonResponse({
          run_id: publishRunId,
          start_token: "start-token",
          progress_mode: "attempt_publish",
        });
      }
      if (url === `/api/runs/${publishRunId}/start`) {
        return jsonResponse({ run_id: publishRunId, progress_mode: "attempt_publish" });
      }
      const cancel = url.match(/^\/api\/runs\/([^/]+)\/cancel$/);
      if (cancel) {
        runCancels.push(cancel[1]);
        if (mode === "task-failure" && cancel[1] === publishRunId) {
          throw new Error("derived cancellation transport failed");
        }
        return confirmedCancellation(cancel[1]);
      }
      if (url === `/api/runs/${publishRunId}/artifact`) {
        return jsonResponse({
          message: {
            id: `msg_dual_cancel_publish_${mode}`,
            role: "assistant",
            text: "Run cancelled.",
            ts: 3,
            run_id: publishRunId,
            status: "error",
            failure: { status: "cancelled", produced_files: [] },
          },
          artifact: null,
        });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }) as typeof fetch;

    await useApp.getState().recoverPaperBundles();
    await waitFor(
      () => MockEventSource.instances.some(
        (source) => source.url === `/api/runs/${sourceRunId}/events`,
      ),
      `${mode} source recovery did not start`,
    );
    const publishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget());
    await waitFor(
      () => MockEventSource.instances.some(
        (source) => source.url === `/api/runs/${publishRunId}/events`,
      ),
      `${mode} candidate publication did not start`,
    );

    if (mode !== "bundle") {
      await useApp.getState().cancelPaperBundleTask(parentId, "poster");
    } else {
      await useApp.getState().cancelPaperBundle(parentId);
    }
    const sourceEvent = MockEventSource.instances.find(
      (source) => source.url === `/api/runs/${sourceRunId}/events`,
    );
    const publicationEvent = MockEventSource.instances.find(
      (source) => source.url === `/api/runs/${publishRunId}/events`,
    );
    const currentBundle = useApp.getState().conversations[parentId]
      .paper_bundle as PaperBundleParentState;
    const publicationProgress = useApp.getState()
      .runs_progress[`${childId}:candidate-publish`];
    observations.push({
      mode,
      runCancels: [...runCancels].sort(),
      bundleCancels: [...paperBundleCancelRequests],
      sourceClosed: sourceEvent?.readyState === MockEventSource.CLOSED,
      publicationClosed: publicationEvent?.readyState === MockEventSource.CLOSED,
      sourceProgress: useApp.getState().runs_progress[childId] !== undefined,
      publicationProgress: publicationProgress !== undefined,
      publicationPhase: publicationProgress?.phase,
      retryableCancellation: currentBundle.tasks.poster.status === "cancelling"
        && /not confirmed/i.test(currentBundle.tasks.poster.error ?? ""),
    });

    if (publicationEvent?.readyState !== MockEventSource.CLOSED) {
      publicationEvent?.emit("run.cancelled");
    }
    await publishing.catch(() => undefined);
  }

  assert.deepEqual(observations, [
    {
      mode: "task",
      runCancels: ["run_dual_cancel_publish_task", "run_dual_cancel_source_task"],
      bundleCancels: [],
      sourceClosed: true,
      publicationClosed: true,
      sourceProgress: false,
      publicationProgress: false,
      publicationPhase: undefined,
      retryableCancellation: false,
    },
    {
      mode: "bundle",
      runCancels: ["run_dual_cancel_publish_bundle"],
      bundleCancels: ["job_dual_cancel_bundle"],
      sourceClosed: true,
      publicationClosed: true,
      sourceProgress: false,
      publicationProgress: false,
      publicationPhase: undefined,
      retryableCancellation: false,
    },
    {
      mode: "task-failure",
      runCancels: [
        "run_dual_cancel_publish_task-failure",
        "run_dual_cancel_source_task-failure",
      ],
      bundleCancels: [],
      sourceClosed: true,
      publicationClosed: false,
      sourceProgress: false,
      publicationProgress: true,
      publicationPhase: "cancelling",
      retryableCancellation: true,
    },
  ]);
});

test("bundle cancellation settles a live derived publication after its source task terminalizes", async () => {
  const parentId = "bundle_terminal_source_cancel";
  const sourceRunId = "run_terminal_source_cancel";
  const publishRunId = "run_terminal_source_publish";
  const candidateId = "poster-attempt-01";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_terminal_source_cancel";
  bundle.revision = 1;
  bundle.backend_state = "running";
  bundle.tasks.poster = {
    ...bundle.tasks.poster,
    status: "running",
    run_id: sourceRunId,
    authoring_run_id: sourceRunId,
  };
  for (const artifactType of ["deck", "landing", "video"] as const) {
    bundle.tasks[artifactType] = {
      ...bundle.tasks[artifactType],
      status: "complete",
      run_id: `run_terminal_source_${artifactType}`,
    };
  }
  const childId = bundle.tasks.poster.child_conversation_id;
  const draft: Artifact = {
    ...artifact("terminal_source_draft", "poster"),
    candidate_draft: true,
    attempt_lineage: {
      source_run_id: sourceRunId,
      source_attempt: 1,
      source_candidate_id: candidateId,
    },
  };
  resetStore({
    [parentId]: conversation(parentId, { paper_bundle: bundle, pending: true }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState(parentId, "poster"),
      messages: [{
        id: "msg_terminal_source",
        role: "assistant",
        text: "Generating poster.",
        ts: 1,
        run_id: sourceRunId,
        status: "streaming",
      }],
      artifacts: { [draft.artifact_id]: draft },
      active_artifact_id: draft.artifact_id,
      pending: true,
      run_id: sourceRunId,
    }),
  }, childId);
  setReadyAttempt(sourceRunId, candidateId);
  paperBundleListOverride = [backendPaperBundleJob(parentId, bundle, 1)];
  const terminal = backendPaperBundleJob(parentId, bundle, 2);
  paperBundleCancelOverride = {
    status: 200,
    body: {
      ...terminal,
      state: "cancelled",
      terminal: true,
      terminal_at: 2,
      cancel_requested: true,
      cancel_requested_at: 2,
      completed_children: [],
      children: Object.fromEntries(Object.entries(terminal.children).map(
        ([artifactType, descriptor]) => [artifactType, {
          ...descriptor,
          state: "cancelled",
          terminal: true,
          process_free: true,
        }],
      )),
      confirmed: true,
      status: "cancelled",
    },
  };
  const runCancels: string[] = [];
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/artifacts/${draft.artifact_id}/publish-candidate-draft`) {
      return jsonResponse({
        run_id: publishRunId,
        start_token: "start-token",
        progress_mode: "attempt_publish",
      });
    }
    if (url === `/api/runs/${publishRunId}/start`) {
      return jsonResponse({ run_id: publishRunId, progress_mode: "attempt_publish" });
    }
    const cancel = url.match(/^\/api\/runs\/([^/]+)\/cancel$/);
    if (cancel) {
      runCancels.push(cancel[1]);
      return confirmedCancellation(cancel[1]);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().recoverPaperBundles();
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === `/api/runs/${sourceRunId}/events`,
    ),
    "terminal-source recovery did not start",
  );
  const publishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget());
  let publicationSettled = false;
  const settledPublication = publishing.then(
    () => { publicationSettled = true; },
    () => { publicationSettled = true; },
  );
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === `/api/runs/${publishRunId}/events`,
    ),
    "terminal-source candidate publication did not start",
  );

  useApp.setState((state) => {
    const currentParent = state.conversations[parentId];
    const currentBundle = currentParent.paper_bundle as PaperBundleParentState;
    return {
      conversations: {
        ...state.conversations,
        [parentId]: {
          ...currentParent,
          paper_bundle: {
            ...currentBundle,
            tasks: {
              ...currentBundle.tasks,
              poster: {
                ...currentBundle.tasks.poster,
                status: "complete",
                terminal: true,
                process_free: true,
              },
            },
          },
        },
      },
    };
  });
  const livePublication = MockEventSource.instances.find(
    (source) => source.url === `/api/runs/${publishRunId}/events`,
  );
  assert.equal(
    useApp.getState().runs_progress[`${childId}:candidate-publish`]?.run_id,
    publishRunId,
  );
  assert.equal(livePublication?.readyState, 1);

  await useApp.getState().cancelPaperBundle(parentId);
  await tick();
  const current = useApp.getState();
  const publicationMessage = current.conversations[childId].messages.find(
    (message) => message.task_type === "candidate_publish" && message.run_id === publishRunId,
  );
  const observation = {
    bundleCancels: [...paperBundleCancelRequests],
    runCancels: [...runCancels],
    publicationClosed: livePublication?.readyState === MockEventSource.CLOSED,
    publicationProgress: current.runs_progress[`${childId}:candidate-publish`] !== undefined,
    publicationStatus: publicationMessage?.status,
    publicationFailure: publicationMessage?.failure?.status,
    publicationSettled,
  };

  if (livePublication?.readyState !== MockEventSource.CLOSED) {
    livePublication?.emit("run.cancelled");
  }
  await settledPublication;

  assert.deepEqual(observation, {
    bundleCancels: ["job_terminal_source_cancel"],
    runCancels: [publishRunId],
    publicationClosed: true,
    publicationProgress: false,
    publicationStatus: "error",
    publicationFailure: "cancelled",
    publicationSettled: true,
  });
});

test("reload preserves pending dual cancellation ownership and rejects a stale publication result", async () => {
  const parentId = "bundle_cancel_reload";
  const sourceRunId = "run_cancel_reload_source";
  const publishRunId = "run_cancel_reload_publish";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_cancel_reload";
  bundle.revision = 1;
  bundle.backend_state = "cancelling";
  bundle.tasks.poster = {
    ...bundle.tasks.poster,
    status: "cancelling",
    run_id: sourceRunId,
    authoring_run_id: sourceRunId,
    error: "Cancellation not confirmed: network unavailable",
  };
  for (const artifactType of ["deck", "landing", "video"] as const) {
    bundle.tasks[artifactType] = {
      ...bundle.tasks[artifactType],
      status: "complete",
      run_id: `run_cancel_reload_${artifactType}`,
    };
  }
  const childId = bundle.tasks.poster.child_conversation_id;
  const draft: Artifact = {
    ...artifact("cancel_reload_draft", "poster"),
    candidate_draft: true,
    attempt_lineage: {
      source_run_id: sourceRunId,
      source_attempt: 1,
      source_candidate_id: "poster-attempt-01",
    },
  };
  const persistedConversations = {
    [parentId]: conversation(parentId, { paper_bundle: bundle, pending: true }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState(parentId, "poster"),
      messages: [
        {
          id: "msg_cancel_reload_source",
          role: "assistant",
          text: "Stopping source.",
          ts: 1,
          run_id: sourceRunId,
          status: "streaming",
        },
        {
          id: "msg_cancel_reload_publish",
          role: "assistant",
          text: "Stopping publication.",
          ts: 2,
          run_id: publishRunId,
          artifact_id: draft.artifact_id,
          status: "streaming",
          task_type: "candidate_publish",
          task_payload: {
            artifact_type: "poster",
            source_artifact_id: draft.artifact_id,
            source_run_id: sourceRunId,
            source_candidate_id: "poster-attempt-01",
          },
        },
      ],
      artifacts: { [draft.artifact_id]: draft },
      active_artifact_id: draft.artifact_id,
      pending: true,
      run_id: sourceRunId,
    }),
  };
  resetStore({ base: conversation("base") }, "base");
  localStorage.setItem("autodesign.web.v1", JSON.stringify({
    version: 1,
    state: {
      conversations: persistedConversations,
      current_conversation_id: childId,
      history_user_scope: "test-user",
    },
  }));
  paperBundleListOverride = [backendPaperBundleJob(parentId, bundle, 2, "cancelling")];
  const runCancels: string[] = [];
  let wrongArtifactReads = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({ conversations: {}, imported_runs: 0, user_isolated: true });
    }
    const cancel = url.match(/^\/api\/runs\/([^/]+)\/cancel$/);
    if (cancel) {
      runCancels.push(cancel[1]);
      return confirmedCancellation(cancel[1]);
    }
    if (url === `/api/runs/${publishRunId}/artifact`) {
      wrongArtifactReads += 1;
      return jsonResponse({
        message: {
          id: "msg_wrong_publication",
          role: "assistant",
          text: "Wrong publication result.",
          ts: 3,
          run_id: "run_wrong_publication",
          artifact_id: "art_wrong_publication",
          status: "done",
        },
        artifact: artifact("wrong_publication", "poster"),
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.persist.rehydrate();
  await useApp.getState().loadServerHistory();
  const sourceUrl = `/api/runs/${sourceRunId}/events`;
  const publishUrl = `/api/runs/${publishRunId}/events`;
  await waitFor(
    () => MockEventSource.instances.filter(
      (source) => source.url === sourceUrl || source.url === publishUrl,
    ).length === 2,
    "pending source and publication cancellation were not both recovered",
  );
  const recovered = useApp.getState();
  const recoveryObservation = {
    taskStatus: (recovered.conversations[parentId].paper_bundle as PaperBundleParentState)
      .tasks.poster.status,
    sourceRunId: recovered.runs_progress[childId]?.run_id,
    sourcePhase: recovered.runs_progress[childId]?.phase,
    publicationRunId: recovered.runs_progress[`${childId}:candidate-publish`]?.run_id,
    publicationPhase: recovered.runs_progress[`${childId}:candidate-publish`]?.phase,
  };

  await useApp.getState().cancelPaperBundleTask(parentId, "poster");
  const sourceEvent = MockEventSource.instances.find((source) => source.url === sourceUrl);
  const publicationEvent = MockEventSource.instances.find((source) => source.url === publishUrl);
  const afterRetry = useApp.getState();
  const settledBeforeStale = {
    runCancels: [...runCancels].sort(),
    sourceClosed: sourceEvent?.readyState === MockEventSource.CLOSED,
    publicationClosed: publicationEvent?.readyState === MockEventSource.CLOSED,
    sourceProgress: afterRetry.runs_progress[childId],
    publicationProgress: afterRetry.runs_progress[`${childId}:candidate-publish`],
  };
  publicationEvent?.emit("run.done");
  await tick();

  const settled = useApp.getState().conversations[childId];
  assert.deepEqual({
    recovery: recoveryObservation,
    ...settledBeforeStale,
    wrongArtifactReads,
    activeArtifactId: settled.active_artifact_id,
    wrongArtifactAccepted: Boolean(settled.artifacts.art_wrong_publication),
  }, {
    recovery: {
      taskStatus: "cancelling",
      sourceRunId,
      sourcePhase: "cancelling",
      publicationRunId: publishRunId,
      publicationPhase: "cancelling",
    },
    runCancels: [publishRunId, sourceRunId].sort(),
    sourceClosed: true,
    publicationClosed: true,
    sourceProgress: undefined,
    publicationProgress: undefined,
    wrongArtifactReads: 0,
    activeArtifactId: draft.artifact_id,
    wrongArtifactAccepted: false,
  });
});

test("loadServerHistory retains one publication across every late source terminal outcome", async () => {
  const observations: Array<Record<string, unknown>> = [];
  const outcomes = [
    { name: "success", event: "run.done", failure: undefined },
    { name: "error", event: "run.error", failure: "fail" },
    { name: "cancellation", event: "run.cancelled", failure: "cancelled" },
  ] as const;

  for (const outcome of outcomes) {
    const parentId = `bundle_candidate_recovery_${outcome.name}`;
    const sourceRunId = `run_source_poster_${outcome.name}`;
    const candidateRunId = `run_candidate_recovery_${outcome.name}`;
    const sourceMessageId = `msg_source_recovery_${outcome.name}`;
    const candidateMessageId = `msg_candidate_recovery_${outcome.name}`;
    const publishedArtifactId = `art_candidate_recovery_published_${outcome.name}`;
    const bundle = createPaperBundleParentState(parentId, "paper.pdf");
    bundle.job_id = `job_candidate_recovery_${outcome.name}`;
    bundle.revision = 2;
    bundle.backend_state = "running";
    bundle.tasks.poster = {
      ...bundle.tasks.poster,
      status: "running",
      run_id: sourceRunId,
      authoring_run_id: sourceRunId,
    };
    for (const artifactType of ["deck", "landing", "video"] as const) {
      bundle.tasks[artifactType] = {
        ...bundle.tasks[artifactType],
        status: "complete",
        run_id: `run_recovered_${outcome.name}_${artifactType}`,
        artifact_id: `art_recovered_${outcome.name}_${artifactType}`,
      };
    }
    const childId = bundle.tasks.poster.child_conversation_id;
    const draft: Artifact = {
      ...artifact(`candidate_recovery_draft_${outcome.name}`, "poster"),
      candidate_draft: true,
      attempt_lineage: {
        source_run_id: sourceRunId,
        source_attempt: 1,
        source_candidate_id: "poster-attempt-01",
      },
    };
    const persistedConversations = {
      [parentId]: conversation(parentId, { paper_bundle: bundle, pending: true }),
      [childId]: conversation(childId, {
        paper_bundle: createPaperBundleChildState(parentId, "poster"),
        messages: [
          {
            id: sourceMessageId,
            role: "assistant",
            text: "Generating poster.",
            ts: 1,
            run_id: sourceRunId,
            status: "streaming",
          },
          {
            id: candidateMessageId,
            role: "assistant",
            text: "Publishing selected attempt.",
            ts: 2,
            run_id: candidateRunId,
            artifact_id: draft.artifact_id,
            status: "streaming",
            task_type: "candidate_publish",
            task_payload: {
              artifact_type: "poster",
              source_artifact_id: draft.artifact_id,
              source_run_id: sourceRunId,
              source_candidate_id: "poster-attempt-01",
            },
          },
        ],
        artifacts: { [draft.artifact_id]: draft },
        active_artifact_id: draft.artifact_id,
        pending: true,
        run_id: sourceRunId,
      }),
    };
    resetStore({ base: conversation("base") }, "base");
    localStorage.setItem("autodesign.web.v1", JSON.stringify({
      version: 1,
      state: {
        conversations: persistedConversations,
        current_conversation_id: childId,
        history_user_scope: "test-user",
      },
    }));
    paperBundleListOverride = [backendPaperBundleJob(parentId, bundle, 3)];
    let releaseCandidateArtifact!: (response: Response) => void;
    let releaseSourceArtifact!: (response: Response) => void;
    globalThis.fetch = (async (input) => {
      const url = String(input);
      if (url.startsWith("/api/history")) {
        return jsonResponse({ conversations: {}, imported_runs: 0, user_isolated: true });
      }
      if (url === `/api/runs/${candidateRunId}/artifact`) {
        return new Promise<Response>((resolve) => { releaseCandidateArtifact = resolve; });
      }
      if (url === `/api/runs/${sourceRunId}/artifact`) {
        return new Promise<Response>((resolve) => { releaseSourceArtifact = resolve; });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }) as typeof fetch;

    await useApp.persist.rehydrate();
    await useApp.getState().loadServerHistory();
    const sourceUrl = `/api/runs/${sourceRunId}/events`;
    const candidateUrl = `/api/runs/${candidateRunId}/events`;
    await waitFor(
      () => MockEventSource.instances.filter(
        (source) => source.url === sourceUrl || source.url === candidateUrl,
      ).length === 2,
      `${outcome.name} source and publication were not both recovered`,
    );
    const recoveringChild = useApp.getState().conversations[childId];
    assert.equal(recoveringChild.run_id, sourceRunId);
    assert.equal(recoveringChild.pending, true);
    assert.equal(useApp.getState().runs_progress[childId]?.run_id, sourceRunId);
    assert.equal(
      useApp.getState().runs_progress[`${childId}:candidate-publish`]?.run_id,
      candidateRunId,
    );

    MockEventSource.instances.find((source) => source.url === candidateUrl)?.emit("run.done");
    await waitFor(
      () => typeof releaseCandidateArtifact === "function",
      `${outcome.name} candidate artifact recovery did not begin`,
    );
    releaseCandidateArtifact(jsonResponse({
      message: {
        id: candidateMessageId,
        role: "assistant",
        text: "Published selected attempt.",
        ts: 3,
        run_id: candidateRunId,
        artifact_id: publishedArtifactId,
        status: "done",
      },
      artifact: {
        ...artifact(`candidate_recovery_published_${outcome.name}`, "poster"),
        attempt_lineage: {
          status: "published",
          source_run_id: sourceRunId,
          source_attempt: 1,
          source_candidate_id: "poster-attempt-01",
        },
      },
    }));
    await waitFor(
      () => useApp.getState().conversations[childId].published_artifact_id
        === publishedArtifactId,
      `${outcome.name} candidate publication was not applied`,
    );

    MockEventSource.instances.find((source) => source.url === sourceUrl)?.emit(outcome.event);
    await waitFor(
      () => typeof releaseSourceArtifact === "function",
      `${outcome.name} source artifact recovery did not begin`,
    );
    const sourceArtifact = outcome.failure
      ? null
      : artifact(`late_source_${outcome.name}`, "poster");
    releaseSourceArtifact(jsonResponse({
      message: {
        id: sourceMessageId,
        role: "assistant",
        text: outcome.failure ? `Source ${outcome.name}.` : "Source completed late.",
        ts: 4,
        run_id: sourceRunId,
        ...(sourceArtifact ? { artifact_id: sourceArtifact.artifact_id } : {}),
        status: outcome.failure ? "error" : "done",
        ...(outcome.failure ? {
          failure: {
            status: outcome.failure,
            agent_last_note: `Source ${outcome.name}.`,
            produced_files: [],
          },
        } : {}),
      },
      artifact: sourceArtifact,
    }));
    await waitFor(
      () => useApp.getState().runs_progress[childId] === undefined,
      `${outcome.name} source recovery did not settle`,
    );

    const settled = useApp.getState();
    const settledChild = settled.conversations[childId];
    const settledBundle = settled.conversations[parentId]
      .paper_bundle as PaperBundleParentState;
    observations.push({
      outcome: outcome.name,
      activeArtifactId: settledChild.active_artifact_id,
      publishedArtifactId: settledBundle.tasks.poster.artifact_id,
      backendState: settledBundle.backend_state,
      revision: settledBundle.revision,
      candidateMessages: settledChild.messages.filter(
        (message) => message.id === candidateMessageId
          && message.artifact_id === publishedArtifactId
          && message.status === "done",
      ).length,
      candidateArtifacts: Object.keys(settledChild.artifacts).filter(
        (artifactId) => artifactId === publishedArtifactId,
      ).length,
      sourceListeners: MockEventSource.instances.filter(
        (source) => source.url === sourceUrl,
      ).length,
      candidateListeners: MockEventSource.instances.filter(
        (source) => source.url === candidateUrl,
      ).length,
    });
    await tick();
  }

  assert.deepEqual(observations, outcomes.map((outcome) => ({
    outcome: outcome.name,
    activeArtifactId: `art_candidate_recovery_published_${outcome.name}`,
    publishedArtifactId: `art_candidate_recovery_published_${outcome.name}`,
    backendState: "running",
    revision: 3,
    candidateMessages: 1,
    candidateArtifacts: 1,
    sourceListeners: 1,
    candidateListeners: 1,
  })));
});

test("opening an attempt fork preserves the current published artifact", async () => {
  const conversationId = "conv_attempt_edit";
  const published = artifact("published_original", "landing");
  resetStore({
    [conversationId]: conversation(conversationId, {
      artifacts: { [published.artifact_id]: published },
      active_artifact_id: published.artifact_id,
    }),
  }, conversationId);

  const draft: Artifact = {
    ...artifact("attempt_draft", "landing"),
    candidate_draft: true,
    attempt_lineage: {
      materialization_version: 2,
      source_run_id: "source_run",
      source_attempt: 2,
      source_candidate_id: "landing-attempt-02",
      source_candidate_sha256: "a".repeat(64),
    },
  };
  let releaseStart!: (response: Response) => void;
  let artifactReads = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/runs/source_run/attempts/2/fork") {
      assert.equal(init?.method, "POST");
      assert.equal(new Headers(init?.headers).get("X-Autodesign-Reserve-Only"), "true");
      return jsonResponse({
        run_id: "run_attempt_fork",
        start_token: "start-token",
        progress_mode: "attempt_fork",
        placeholder_message: {
          id: "msg_attempt_fork",
          role: "assistant",
          text: "",
          ts: 1,
          status: "streaming",
        },
      });
    }
    if (url === "/api/runs/run_attempt_fork/start") {
      assert.equal(
        useApp.getState().conversations[conversationId].run_id,
        "run_attempt_fork",
        "onReserved must expose the run before /start is dispatched",
      );
      assert.equal(
        useApp.getState().runs_progress[conversationId]?.run_id,
        "run_attempt_fork",
      );
      return new Promise<Response>((resolve) => { releaseStart = resolve; });
    }
    if (url === "/api/runs/run_attempt_fork/artifact") {
      artifactReads += 1;
      return jsonResponse({
        message: {
          id: "msg_attempt_fork",
          role: "assistant",
          text: "Done",
          ts: 2,
          run_id: "run_attempt_fork",
          artifact_id: draft.artifact_id,
          status: "done",
        },
        artifact: draft,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;
  useApp.setState({ mode: "chat", properties_sidebar_open: false });

  const opening = useApp.getState().openAttemptInCanvas(
    "source_run",
    {
      candidate_id: "landing-attempt-02",
      run_id: "source_run",
      artifact_type: "landing",
      attempt: 2,
      max_attempts: 4,
      created_at: "2026-07-29T00:00:00Z",
      source_sha256: "a".repeat(64),
      safety_state: "ready",
      hard_blockers: [],
      warnings: [],
      source_url: "/api/files/runs/source_run/index.html",
      preview_urls: [],
    },
  );

  await waitFor(() => typeof releaseStart === "function", "attempt /start did not begin");
  assert.equal(artifactReads, 0);
  assert.equal(useApp.getState().mode, "chat");
  assert.equal(
    useApp.getState().conversations[conversationId].active_artifact_id,
    published.artifact_id,
  );

  releaseStart(jsonResponse({
    run_id: "run_attempt_fork",
    progress_mode: "attempt_fork",
    placeholder_message: {
      id: "msg_attempt_fork",
      role: "assistant",
      text: "",
      ts: 1,
      status: "streaming",
    },
  }));
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === "/api/runs/run_attempt_fork/events",
    ),
    "attempt event source was not opened",
  );
  assert.equal(artifactReads, 0);
  assert.equal(useApp.getState().mode, "chat");
  MockEventSource.instances.find(
    (source) => source.url === "/api/runs/run_attempt_fork/events",
  )?.emit("run.done");
  await opening;

  const updated = useApp.getState().conversations[conversationId];
  assert.equal(artifactReads, 1);
  assert.equal(updated.active_artifact_id, draft.artifact_id);
  assert.equal(updated.published_artifact_id, published.artifact_id);
  assert.ok(updated.artifacts[published.artifact_id]);
  assert.equal(useApp.getState().properties_sidebar_open, true);
});

test("opening a live Paper All-in-One attempt keeps its source stream running", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  const artifactTypes: ArtifactType[] = ["poster", "deck", "landing", "video"];
  let nextRun = 0;
  const draft: Artifact = {
    ...artifact("attempt_fork_draft", "poster"),
    candidate_draft: true,
    attempt_lineage: {
      materialization_version: 2,
      source_run_id: "run_source_poster",
      source_attempt: 1,
      source_candidate_id: "poster-attempt-01",
      source_candidate_sha256: "a".repeat(64),
    },
  };
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        needs_setup: false,
      });
    }
    if (url === "/api/generate") {
      const artifactType = artifactTypes[nextRun];
      nextRun += 1;
      return jsonResponse({
        run_id: `run_source_${artifactType}`,
        placeholder_message: {
          id: `msg_source_${artifactType}`,
          role: "assistant",
          text: "",
          ts: 1,
          run_id: `run_source_${artifactType}`,
          status: "streaming",
        },
      });
    }
    if (url === "/api/runs/run_source_poster/attempts/1/fork") {
      assert.equal(init?.method, "POST");
      return jsonResponse({
        run_id: "run_attempt_fork",
        start_token: "start-token",
        progress_mode: "attempt_fork",
        placeholder_message: {
          id: "msg_attempt_fork",
          role: "assistant",
          text: "",
          ts: 2,
          status: "streaming",
        },
      });
    }
    if (url === "/api/runs/run_attempt_fork/start") {
      return jsonResponse({
        run_id: "run_attempt_fork",
        progress_mode: "attempt_fork",
        placeholder_message: {
          id: "msg_attempt_fork",
          role: "assistant",
          text: "",
          ts: 2,
          status: "streaming",
        },
      });
    }
    if (url === "/api/runs/run_attempt_fork/artifact") {
      return jsonResponse({
        message: {
          id: "msg_attempt_fork",
          role: "assistant",
          text: "Canvas draft ready.",
          ts: 3,
          run_id: "run_attempt_fork",
          artifact_id: draft.artifact_id,
          status: "done",
        },
        artifact: draft,
      });
    }
    const sourceArtifact = url.match(
      /^\/api\/runs\/run_source_(poster|deck|landing|video)\/artifact$/,
    );
    if (sourceArtifact) {
      const artifactType = sourceArtifact[1] as ArtifactType;
      return jsonResponse(responseForRun(`run_source_${artifactType}`, artifactType));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const bundleStart = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await waitFor(
    () => MockEventSource.instances.length === 4,
    "bundle source streams did not start",
  );
  const parent = useApp.getState().conversations.bundle;
  assert.equal(parent.paper_bundle?.kind, "parent");
  if (parent.paper_bundle?.kind !== "parent") return;
  const childId = parent.paper_bundle.tasks.poster.child_conversation_id;
  const sourceStream = MockEventSource.instances.find(
    (source) => source.url === "/api/runs/run_source_poster/events",
  );
  assert.ok(sourceStream);

  const opening = useApp.getState().openAttemptInCanvas(
    "run_source_poster",
    {
      candidate_id: "poster-attempt-01",
      run_id: "run_source_poster",
      artifact_type: "poster",
      attempt: 1,
      max_attempts: 6,
      created_at: "2026-08-04T00:00:00Z",
      source_sha256: "a".repeat(64),
      safety_state: "blocked",
      hard_blockers: [{
        issue_id: "paper_poster_html_designer_flow_canvas_overflow",
        message: "Poster validation finding",
      }],
      warnings: [],
      source_url: "/api/files/runs/run_source_poster/attempt-01/poster.html",
      preview_urls: [],
    },
    childId,
  );
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === "/api/runs/run_attempt_fork/events",
    ),
    "attempt fork event source was not opened",
  );

  const duringFork = useApp.getState();
  assert.equal(sourceStream.readyState, 1, "source SSE must remain open");
  assert.equal(duringFork.conversations[childId].pending, true);
  assert.equal(duringFork.conversations[childId].run_id, "run_source_poster");
  assert.equal(duringFork.runs_progress[childId]?.run_id, "run_source_poster");
  const liveParent = duringFork.conversations.bundle.paper_bundle;
  assert.equal(liveParent?.kind, "parent");
  if (liveParent?.kind !== "parent") return;
  assert.equal(liveParent.tasks.poster.status, "running");
  assert.equal(liveParent.tasks.poster.run_id, "run_source_poster");

  MockEventSource.instances.find(
    (source) => source.url === "/api/runs/run_attempt_fork/events",
  )?.emit("run.done");
  await opening;

  const afterFork = useApp.getState();
  assert.equal(sourceStream.readyState, 1, "source SSE must still be open after Canvas opens");
  assert.equal(afterFork.conversations[childId].pending, true);
  assert.equal(afterFork.conversations[childId].run_id, "run_source_poster");
  assert.equal(afterFork.runs_progress[childId]?.run_id, "run_source_poster");
  assert.equal(afterFork.conversations[childId].active_artifact_id, draft.artifact_id);

  for (const source of MockEventSource.instances) {
    if (source.url.includes("run_source_")) source.emit("run.done");
  }
  await bundleStart;
});

test("attempt actions do not start while their paper bundle is cancelling", async () => {
  const parentId = "bundle_attempt_cancelling";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  const childId = bundle.tasks.poster.child_conversation_id;
  bundle.backend_state = "cancelling";
  bundle.tasks.poster = {
    ...bundle.tasks.poster,
    status: "cancelling",
    run_id: "run_attempts",
    authoring_run_id: "run_attempts",
  };
  resetStore({
    [parentId]: conversation(parentId, { paper_bundle: bundle }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState(parentId, "poster"),
    }),
  }, parentId);
  let requests = 0;
  globalThis.fetch = (async (input) => {
    requests += 1;
    throw new Error(`Unexpected fetch: ${String(input)}`);
  }) as typeof fetch;
  const candidate = {
    candidate_id: "poster-attempt-01",
    run_id: "run_attempts",
    artifact_type: "poster" as const,
    attempt: 1,
    max_attempts: 4,
    created_at: "2026-08-03T00:00:00Z",
    source_sha256: "a".repeat(64),
    safety_state: "ready" as const,
    hard_blockers: [],
    warnings: [],
    source_url: "/candidate.html",
    preview_urls: [],
  };

  await assert.rejects(
    useApp.getState().selectAttempt("run_attempts", candidate),
    /cancellation to finish/i,
  );
  await assert.rejects(
    useApp.getState().openAttemptInCanvas("run_attempts", candidate, childId),
    /cancellation to finish/i,
  );
  assert.equal(requests, 0);

  const unrelated = { ...candidate, run_id: "unrelated_run" };
  globalThis.fetch = (async (input) => {
    requests += 1;
    assert.equal(
      String(input),
      "/api/runs/unrelated_run/attempts/1/select",
    );
    return jsonResponse({ run_id: "unrelated_run", candidates: [] });
  }) as typeof fetch;
  await useApp.getState().selectAttempt("unrelated_run", unrelated);
  assert.equal(requests, 1);
});

test("reopening an existing attempt draft does not fork it again", async () => {
  const conversationId = "conv_attempt_reopen";
  const published = artifact("published_original", "poster");
  const draft: Artifact = {
    ...artifact("attempt_draft", "poster"),
    candidate_draft: true,
    attempt_lineage: {
      materialization_version: 2,
      source_run_id: "source_run",
      source_attempt: 1,
      source_candidate_id: "poster-attempt-01",
      source_candidate_sha256: "a".repeat(64),
    },
  };
  resetStore({
    [conversationId]: conversation(conversationId, {
      artifacts: {
        [published.artifact_id]: published,
        [draft.artifact_id]: draft,
      },
      active_artifact_id: draft.artifact_id,
      published_artifact_id: published.artifact_id,
    }),
  }, conversationId);
  useApp.setState({ mode: "chat", properties_sidebar_open: false });
  let fetches = 0;
  globalThis.fetch = (async () => {
    fetches += 1;
    throw new Error("reopening an existing draft must not fork it again");
  }) as typeof fetch;

  await useApp.getState().openAttemptInCanvas(
    "source_run",
    {
      candidate_id: "poster-attempt-01",
      run_id: "source_run",
      artifact_type: "poster",
      attempt: 1,
      max_attempts: 4,
      created_at: "2026-07-29T00:00:00Z",
      source_sha256: "a".repeat(64),
      safety_state: "blocked",
      hard_blockers: [{
        issue_id: "poster_validation",
        message: "Poster validation finding",
      }],
      warnings: [],
      source_url: "/api/files/runs/source_run/poster.html",
      preview_urls: [],
    },
  );

  const state = useApp.getState();
  assert.equal(fetches, 0);
  assert.equal(state.mode, "canvas");
  assert.equal(
    state.conversations[conversationId].active_artifact_id,
    draft.artifact_id,
  );
  assert.equal(
    Object.keys(state.conversations[conversationId].artifacts).length,
    2,
  );
  assert.equal(state.properties_sidebar_open, true);
});

test("reopening an edited attempt uses its newest saved draft", async () => {
  const conversationId = "conv_attempt_reopen_saved";
  const original: Artifact = {
    ...artifact("attempt_original", "poster"),
    candidate_draft: true,
    attempt_lineage: {
      materialization_version: 2,
      source_run_id: "source_run",
      source_attempt: 1,
      source_candidate_id: "poster-attempt-01",
      source_candidate_sha256: "a".repeat(64),
    },
  };
  const saved: Artifact = {
    ...original,
    artifact_id: "art_attempt_saved",
    attempt_lineage: {
      ...original.attempt_lineage!,
      status: "draft",
      edited_at: "2026-07-30T12:00:00Z",
    },
  };
  resetStore({
    [conversationId]: conversation(conversationId, {
      artifacts: {
        [original.artifact_id]: original,
        [saved.artifact_id]: saved,
      },
      active_artifact_id: saved.artifact_id,
    }),
  }, conversationId);
  useApp.setState({ mode: "chat" });
  let fetches = 0;
  globalThis.fetch = (async () => {
    fetches += 1;
    throw new Error("a saved attempt draft must be reused");
  }) as typeof fetch;

  await useApp.getState().openAttemptInCanvas(
    "source_run",
    {
      candidate_id: "poster-attempt-01",
      run_id: "source_run",
      artifact_type: "poster",
      attempt: 1,
      max_attempts: 4,
      created_at: "2026-07-29T00:00:00Z",
      source_sha256: "a".repeat(64),
      safety_state: "ready",
      hard_blockers: [],
      warnings: [],
      source_url: "/api/files/runs/source_run/poster.html",
      preview_urls: [],
    },
  );

  assert.equal(fetches, 0);
  assert.equal(
    useApp.getState().conversations[conversationId].active_artifact_id,
    saved.artifact_id,
  );
});

test("an attempt draft can autosave while its palette is recovered by the backend", async () => {
  const conversationId = "conv_attempt_save";
  const draft: Artifact = {
    ...artifact("attempt_draft", "poster"),
    native_format: "html",
    native_file_url: "/api/files/runs/attempt_draft/final/poster.html",
    candidate_draft: true,
    attempt_lineage: {
      source_run_id: "source_run",
      source_attempt: 1,
      source_candidate_id: "poster-attempt-01",
      source_candidate_sha256: "a".repeat(64),
    },
  };
  const saved: Artifact = {
    ...draft,
    artifact_id: "art_attempt_saved",
  };
  resetStore({
    [conversationId]: conversation(conversationId, {
      artifacts: { [draft.artifact_id]: draft },
      active_artifact_id: draft.artifact_id,
      poster_palette_id: null,
      pending_edits: {
        [draft.artifact_id]: {
          title: { text: "Edited title" },
        },
      },
    }),
  }, conversationId);

  globalThis.fetch = (async (input, init) => {
    assert.equal(String(input), "/api/edits/apply");
    const body = init?.body;
    assert.ok(body instanceof FormData);
    assert.equal(body.get("palette_id"), null);
    return jsonResponse({
      message: {
        id: "msg_attempt_saved",
        role: "assistant",
        text: "Saved",
        ts: 3,
        run_id: "attempt_saved",
        artifact_id: saved.artifact_id,
        status: "done",
      },
      artifact: saved,
    });
  }) as typeof fetch;

  await useApp.getState().flushAutoSave();

  const updated = useApp.getState().conversations[conversationId];
  assert.equal(updated.active_artifact_id, saved.artifact_id);
  assert.equal(useApp.getState().autosave_error, null);
});

test("ordinary server run history merges into its existing local conversation", async () => {
  const runId = "run_existing_local";
  const conversationId = "conv_existing_local";
  const draft: Artifact = {
    ...artifact("attempt_draft", "poster"),
    candidate_draft: true,
    attempt_lineage: {
      source_run_id: runId,
      source_attempt: 1,
      source_candidate_id: "poster-attempt-01",
      source_candidate_sha256: "b".repeat(64),
    },
  };
  resetStore({
    [conversationId]: conversation(conversationId, {
      updated_at: 10,
      messages: [{
        id: "msg_local_run",
        role: "assistant",
        text: "Generation completed",
        ts: 10,
        run_id: runId,
        status: "done",
      }],
      artifacts: { [draft.artifact_id]: draft },
      active_artifact_id: draft.artifact_id,
    }),
    server_run_attempt_draft: conversation("server_run_attempt_draft", {
      title: "Poster - 20260729",
      updated_at: 9,
      artifacts: { [draft.artifact_id]: draft },
      active_artifact_id: draft.artifact_id,
    }),
  }, conversationId);
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({
        conversations: {
          [`server_run_${runId}`]: conversation(`server_run_${runId}`, {
            title: "Poster - running",
            updated_at: 20,
            messages: [{
              id: `msg_${runId}`,
              role: "assistant",
              text: "Generation completed",
              ts: 20,
              run_id: runId,
              status: "done",
            }],
          }),
        },
        imported_runs: 1,
        user_isolated: false,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().loadServerHistory();

  const state = useApp.getState();
  assert.equal(state.conversations[`server_run_${runId}`], undefined);
  assert.equal(state.conversations.server_run_attempt_draft, undefined);
  assert.equal(
    state.conversations[conversationId].active_artifact_id,
    draft.artifact_id,
  );
});

test("retrying one failed bundle task preserves ready siblings and reconciles the parent", async () => {
  const parentId = "conv_retry_parent";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  for (const artifactType of ["poster", "deck", "landing"] as const) {
    bundle.tasks[artifactType] = {
      ...bundle.tasks[artifactType],
      status: "complete",
      run_id: `run_ready_${artifactType}`,
      artifact_id: `art_run_ready_${artifactType}`,
    };
  }
  bundle.tasks.video = {
    ...bundle.tasks.video,
    status: "failed",
    run_id: "run_failed_video",
    error: "Video runtime was unavailable.",
  };
  const childId = bundle.tasks.video.child_conversation_id;
  const readyArtifacts = Object.fromEntries(
    (["poster", "deck", "landing"] as const).map((artifactType) => [
      `art_run_ready_${artifactType}`,
      artifact(`run_ready_${artifactType}`, artifactType),
    ]),
  );
  resetStore({
    [parentId]: conversation(parentId, {
      paper_bundle: bundle,
      artifacts: readyArtifacts,
    }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState(parentId, "video"),
      messages: [{
        id: "msg_failed_video",
        role: "assistant",
        text: "Failed",
        ts: 2,
        run_id: "run_failed_video",
        status: "error",
        failure: {
          status: "fail",
          artifact_type: "video",
          produced_files: [],
        },
      }],
    }),
  }, parentId);

  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (
      url === "/api/runs/run_failed_video/retry-video-export"
      && init?.method === "POST"
    ) {
      return jsonResponse({
        run_id: "run_retry_video",
        progress_mode: "generate",
        placeholder_message: {
          id: "msg_run_retry_video",
          role: "assistant",
          text: "",
          ts: 3,
          run_id: "run_retry_video",
          status: "streaming",
        },
      });
    }
    if (url === "/api/runs/run_retry_video/artifact") {
      return jsonResponse(responseForRun("run_retry_video", "video"));
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch;

  const retry = useApp.getState().retryPaperBundleTask(parentId, "video");
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === "/api/runs/run_retry_video/events",
    ),
    "retry event source was not opened",
  );
  MockEventSource.instances.find(
    (source) => source.url === "/api/runs/run_retry_video/events",
  )?.emit("run.done", { terminal_status: "pass" });
  await retry;

  const parent = useApp.getState().conversations[parentId];
  assert.equal(parent.paper_bundle?.kind, "parent");
  if (parent.paper_bundle?.kind !== "parent") return;
  assert.equal(parent.paper_bundle.tasks.poster.run_id, "run_ready_poster");
  assert.equal(parent.paper_bundle.tasks.deck.run_id, "run_ready_deck");
  assert.equal(parent.paper_bundle.tasks.landing.run_id, "run_ready_landing");
  assert.equal(parent.paper_bundle.tasks.video.status, "complete");
  assert.equal(parent.paper_bundle.tasks.video.run_id, "run_retry_video");
  assert.equal(parent.paper_bundle.tasks.video.authoring_run_id, "run_failed_video");
  assert.equal(parent.paper_bundle.tasks.video.artifact_id, "art_run_retry_video");
  assert.ok(parent.artifacts.art_run_retry_video);
});

test("async export authoring failure recovers the root and binds each retry run", async () => {
  const parentId = "conv_retry_parent";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.tasks.video = {
    ...bundle.tasks.video,
    status: "failed",
    run_id: "run_failed_export",
    error: "Authored source needs repair.",
  };
  const childId = bundle.tasks.video.child_conversation_id;
  resetStore({
    [parentId]: conversation(parentId, { paper_bundle: bundle }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState(parentId, "video"),
      messages: [{
        id: "msg_failed_export",
        role: "assistant",
        text: "Failed",
        ts: 2,
        run_id: "run_failed_export",
        status: "error",
        failure: {
          status: "error",
          parent_run_id: "run_original_authoring",
          artifact_type: "video",
          produced_files: [],
        },
      }],
    }),
  }, parentId);

  const requests: string[] = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    requests.push(url);
    if (
      url === "/api/runs/run_failed_export/retry-video-export"
      && init?.method === "POST"
    ) {
      return jsonResponse({
        run_id: "run_export_retry",
        progress_mode: "video_export",
        placeholder_message: {
          id: "msg_run_export_retry",
          role: "assistant",
          text: "",
          ts: 3,
          run_id: "run_export_retry",
          status: "streaming",
        },
      });
    }
    if (url === "/api/runs/run_export_retry/artifact") {
      return jsonResponse({
        message: {
          id: "msg_run_export_retry",
          role: "assistant",
          text: "Authored clip nesting is invalid.",
          ts: 4,
          run_id: "run_export_retry",
          status: "error",
          failure: {
            status: "error",
            phase: "authoring_lint",
            retry_route: "full_authoring",
            parent_run_id: "run_failed_export",
            artifact_type: "video",
            produced_files: [],
          },
        },
        artifact: null,
      });
    }
    if (
      url === "/api/runs/run_original_authoring/retry"
      && init?.method === "POST"
    ) {
      return jsonResponse({
        run_id: "run_full_retry",
        progress_mode: "generate",
        placeholder_message: {
          id: "msg_run_full_retry",
          role: "assistant",
          text: "",
          ts: 5,
          run_id: "run_full_retry",
          status: "streaming",
        },
      });
    }
    if (url === "/api/runs/run_full_retry/artifact") {
      return jsonResponse(responseForRun("run_full_retry", "video"));
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch;

  const retry = useApp.getState().retryPaperBundleTask(parentId, "video");
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === "/api/runs/run_export_retry/events",
    ),
    "export retry event source was not opened",
  );
  let parent = useApp.getState().conversations[parentId];
  assert.equal(parent.paper_bundle?.kind, "parent");
  if (parent.paper_bundle?.kind !== "parent") return;
  assert.equal(parent.paper_bundle.tasks.video.run_id, "run_export_retry");
  assert.equal(
    parent.paper_bundle.tasks.video.authoring_run_id,
    "run_original_authoring",
  );
  MockEventSource.instances.find(
    (source) => source.url === "/api/runs/run_export_retry/events",
  )?.emit("run.error");
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === "/api/runs/run_full_retry/events",
    ),
    "full retry event source was not opened",
  );
  parent = useApp.getState().conversations[parentId];
  assert.equal(parent.paper_bundle?.kind, "parent");
  if (parent.paper_bundle?.kind !== "parent") return;
  assert.equal(parent.paper_bundle.tasks.video.run_id, "run_full_retry");
  useApp.setState((state) => ({
    conversations: {
      ...state.conversations,
      [childId]: {
        ...state.conversations[childId],
        messages: [
          ...state.conversations[childId].messages,
          {
            id: "msg_unrelated",
            role: "assistant",
            text: "A newer unrelated message",
            ts: 10,
            run_id: "run_unrelated",
            status: "error",
            failure: {
              status: "error",
              artifact_type: "video",
              produced_files: [],
            },
          },
        ],
      },
    },
  }));
  MockEventSource.instances.find(
    (source) => source.url === "/api/runs/run_full_retry/events",
  )?.emit("run.done");
  await retry;

  assert.deepEqual(requests, [
    "/api/runs/run_failed_export/retry-video-export",
    "/api/runs/run_export_retry/artifact",
    "/api/runs/run_original_authoring/retry",
    "/api/runs/run_full_retry/artifact",
  ]);
  parent = useApp.getState().conversations[parentId];
  assert.equal(parent.paper_bundle?.kind, "parent");
  if (parent.paper_bundle?.kind !== "parent") return;
  assert.equal(parent.paper_bundle.tasks.video.status, "complete");
  assert.equal(parent.paper_bundle.tasks.video.run_id, "run_full_retry");
  assert.equal(
    parent.paper_bundle.tasks.video.authoring_run_id,
    "run_original_authoring",
  );
});

test("async export runtime failure does not fall back to authoring", async () => {
  const parentId = "conv_retry_parent";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.tasks.video = {
    ...bundle.tasks.video,
    status: "failed",
    run_id: "run_failed_video",
    authoring_run_id: "run_original_authoring",
    error: "Video runtime was unavailable.",
  };
  const childId = bundle.tasks.video.child_conversation_id;
  resetStore({
    [parentId]: conversation(parentId, { paper_bundle: bundle }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState(parentId, "video"),
      messages: [{
        id: "msg_failed_video",
        role: "assistant",
        text: "Failed",
        ts: 2,
        run_id: "run_failed_video",
        status: "error",
        failure: {
          status: "error",
          artifact_type: "video",
          produced_files: [],
        },
      }],
    }),
  }, parentId);

  const requests: string[] = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    requests.push(url);
    if (
      url === "/api/runs/run_failed_video/retry-video-export"
      && init?.method === "POST"
    ) {
      return jsonResponse({
        run_id: "run_export_retry",
        progress_mode: "video_export",
        placeholder_message: {
          id: "msg_run_export_retry",
          role: "assistant",
          text: "",
          ts: 3,
          run_id: "run_export_retry",
          status: "streaming",
        },
      });
    }
    if (url === "/api/runs/run_export_retry/artifact") {
      return jsonResponse({
        message: {
          id: "msg_run_export_retry",
          role: "assistant",
          text: "Kokoro narration synthesis failed.",
          ts: 4,
          run_id: "run_export_retry",
          status: "error",
          failure: {
            status: "error",
            phase: "tts",
            retry_route: "export_only",
            parent_run_id: "run_failed_video",
            artifact_type: "video",
            produced_files: [],
          },
        },
        artifact: null,
      });
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch;

  const retry = useApp.getState().retryPaperBundleTask(parentId, "video");
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === "/api/runs/run_export_retry/events",
    ),
    "export retry event source was not opened",
  );
  MockEventSource.instances.find(
    (source) => source.url === "/api/runs/run_export_retry/events",
  )?.emit("run.error");
  await retry;

  assert.deepEqual(requests, [
    "/api/runs/run_failed_video/retry-video-export",
    "/api/runs/run_export_retry/artifact",
  ]);
  const parent = useApp.getState().conversations[parentId];
  assert.equal(parent.paper_bundle?.kind, "parent");
  if (parent.paper_bundle?.kind !== "parent") return;
  assert.equal(parent.paper_bundle.tasks.video.status, "failed");
  assert.equal(parent.paper_bundle.tasks.video.run_id, "run_export_retry");
  assert.equal(
    parent.paper_bundle.tasks.video.authoring_run_id,
    "run_original_authoring",
  );
});

test("unavailable export retry falls back to the stable authoring run", async () => {
  const childId = "conv_retry_video";
  resetStore({
    [childId]: conversation(childId, {
      messages: [{
        id: "msg_failed_export",
        role: "assistant",
        text: "Failed",
        ts: 2,
        run_id: "run_derived_export",
        status: "error",
        failure: {
          status: "error",
          artifact_type: "video",
          produced_files: [],
        },
      }],
    }),
  }, childId);

  const requests: string[] = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    requests.push(url);
    if (
      url === "/api/runs/run_derived_export/retry-video-export"
      && init?.method === "POST"
    ) {
      return jsonResponse({
        detail: {
          code: "video_export_retry_unavailable",
          message: "Retry the full Video task instead.",
        },
      }, 422);
    }
    if (
      url === "/api/runs/run_original_authoring/retry"
      && init?.method === "POST"
    ) {
      return jsonResponse({
        run_id: "run_full_retry",
        progress_mode: "generate",
        placeholder_message: {
          id: "msg_run_full_retry",
          role: "assistant",
          text: "",
          ts: 3,
          run_id: "run_full_retry",
          status: "streaming",
        },
      });
    }
    if (url === "/api/runs/run_full_retry/artifact") {
      return jsonResponse(responseForRun("run_full_retry", "video"));
    }
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch;

  const retry = useApp.getState().retryRun(
    "msg_failed_export",
    undefined,
    true,
    "run_original_authoring",
  );
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === "/api/runs/run_full_retry/events",
    ),
    "full retry event source was not opened",
  );
  MockEventSource.instances.find(
    (source) => source.url === "/api/runs/run_full_retry/events",
  )?.emit("run.done");
  await retry;

  assert.deepEqual(requests, [
    "/api/runs/run_derived_export/retry-video-export",
    "/api/runs/run_original_authoring/retry",
    "/api/runs/run_full_retry/artifact",
  ]);
});

test("saving an edited bundle artifact promotes the new lineage into its parent task", async () => {
  const parentId = "conv_edit_parent";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.tasks.poster = {
    ...bundle.tasks.poster,
    status: "complete",
    run_id: "run_poster_old",
    artifact_id: "art_run_poster_old",
  };
  const childId = bundle.tasks.poster.child_conversation_id;
  const oldArtifact: Artifact = {
    ...artifact("run_poster_old", "poster"),
    native_format: "html",
    native_file_url: "/api/runs/run_poster_old/files/final/poster.html",
    layers: [{
      id: "title",
      type: "text",
      text: "Old title",
      bbox: { x: 0, y: 0, w: 100, h: 20 },
    }],
  };
  resetStore({
    [parentId]: conversation(parentId, {
      paper_bundle: bundle,
      artifacts: { [oldArtifact.artifact_id]: oldArtifact },
    }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState(parentId, "poster"),
      artifacts: { [oldArtifact.artifact_id]: oldArtifact },
      active_artifact_id: oldArtifact.artifact_id,
      poster_palette_id: "academic_blue",
      pending_edits: {
        [oldArtifact.artifact_id]: {
          title: { text: "New title" },
        },
      },
      messages: [{
        id: "msg_old",
        role: "assistant",
        text: "Done",
        ts: 2,
        run_id: "run_poster_old",
        artifact_id: oldArtifact.artifact_id,
        status: "done",
      }],
    }),
  }, childId);

  const newArtifact: Artifact = {
    ...oldArtifact,
    artifact_id: "art_run_poster_edited",
    name: "edited poster",
  };
  globalThis.fetch = (async (input) => {
    assert.equal(String(input), "/api/edits/apply");
    return jsonResponse({
      message: {
        id: "msg_edited",
        role: "assistant",
        text: "Saved",
        ts: 3,
        run_id: "run_poster_edited",
        artifact_id: newArtifact.artifact_id,
        status: "done",
      },
      artifact: newArtifact,
    });
  }) as typeof fetch;

  await useApp.getState().flushAutoSave();

  const parent = useApp.getState().conversations[parentId];
  assert.equal(parent.paper_bundle?.kind, "parent");
  if (parent.paper_bundle?.kind !== "parent") return;
  assert.equal(parent.paper_bundle.tasks.poster.run_id, "run_poster_edited");
  assert.equal(parent.paper_bundle.tasks.poster.artifact_id, newArtifact.artifact_id);
  assert.ok(parent.artifacts[newArtifact.artifact_id]);
});

test("saving an inactive Deck slide adopts the derived draft and reopens it without reforking", async () => {
  const parentId = "conv_deck_edit_parent";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  const sourceRunId = "run_deck_attempt_source";
  const candidateId = "deck-attempt-01";
  bundle.tasks.deck = {
    ...bundle.tasks.deck,
    status: "complete",
    run_id: sourceRunId,
    artifact_id: `art_${sourceRunId}`,
  };
  const childId = bundle.tasks.deck.child_conversation_id;
  const sourceArtifact: Artifact = {
    ...artifact(sourceRunId, "deck"),
    native_format: "html",
    native_file_url: `/api/files/runs/${sourceRunId}/final/deck.html`,
    candidate_draft: true,
    layers: [{
      layer_id: "title-18",
      name: "Slide 18 title",
      kind: "text",
      z_index: 18,
      bbox: { x: 40, y: 40, w: 800, h: 80 },
      text: "Original final slide",
    }],
    attempt_lineage: {
      materialization_version: 2,
      status: "draft",
      source_run_id: sourceRunId,
      source_attempt: 1,
      source_candidate_id: candidateId,
      source_candidate_sha256: "a".repeat(64),
    },
  };
  resetStore({
    [parentId]: conversation(parentId, {
      paper_bundle: bundle,
      artifacts: { [sourceArtifact.artifact_id]: sourceArtifact },
    }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState(parentId, "deck"),
      artifacts: { [sourceArtifact.artifact_id]: sourceArtifact },
      active_artifact_id: sourceArtifact.artifact_id,
      messages: [{
        id: "msg_deck_attempt",
        role: "assistant",
        text: "Attempt ready",
        ts: 2,
        run_id: sourceRunId,
        artifact_id: sourceArtifact.artifact_id,
        status: "done",
      }],
    }),
  }, childId);

  const savedRunId = "run_deck_attempt_saved";
  const savedArtifact: Artifact = {
    ...sourceArtifact,
    artifact_id: `art_${savedRunId}`,
    native_file_url: `/api/files/runs/${savedRunId}/final/deck.html`,
    layers: sourceArtifact.layers.map((layer) => ({
      ...layer,
      text: "Edited final slide",
    })),
  };
  let editRequests = 0;
  globalThis.fetch = (async (input, init) => {
    assert.equal(String(input), "/api/edits/apply");
    editRequests += 1;
    const body = init?.body;
    assert.ok(body instanceof FormData);
    assert.equal(body.get("artifact_type"), "deck");
    assert.deepEqual(JSON.parse(String(body.get("edits_json"))), {
      layers: {
        "title-18": { text: "Edited final slide", effects: {} },
      },
    });
    return jsonResponse({
      message: {
        id: "msg_deck_saved",
        role: "assistant",
        text: "Saved",
        ts: 3,
        run_id: savedRunId,
        artifact_id: savedArtifact.artifact_id,
        status: "done",
      },
      artifact: savedArtifact,
    });
  }) as typeof fetch;

  useApp.getState().updateLayer("title-18", { text: "Edited final slide" });
  const pending = useApp.getState().conversations[childId].pending_edits
    ?.[sourceArtifact.artifact_id];
  assert.deepEqual(pending, {
    layers: {
      "title-18": { text: "Edited final slide", effects: {} },
    },
  });
  await useApp.getState().flushAutoSave();

  const child = useApp.getState().conversations[childId];
  assert.equal(child.active_artifact_id, savedArtifact.artifact_id);
  assert.equal(child.pending_edits?.[sourceArtifact.artifact_id], undefined);
  assert.equal(child.messages[0].artifact_id, savedArtifact.artifact_id);
  const parent = useApp.getState().conversations[parentId];
  assert.equal(parent.paper_bundle?.kind, "parent");
  if (parent.paper_bundle?.kind !== "parent") return;
  assert.equal(parent.paper_bundle.tasks.deck.run_id, savedRunId);
  assert.equal(parent.paper_bundle.tasks.deck.artifact_id, savedArtifact.artifact_id);

  const candidate: AttemptCandidateSummary = {
    candidate_id: candidateId,
    run_id: sourceRunId,
    artifact_type: "deck",
    attempt: 1,
    max_attempts: 4,
    created_at: "2026-08-06T00:00:00Z",
    source_sha256: "a".repeat(64),
    safety_state: "ready",
    hard_blockers: [],
    warnings: [],
    source_url: `/api/files/runs/${sourceRunId}/attempt-01/deck.html`,
    preview_urls: [],
  };
  await useApp.getState().openAttemptInCanvas(sourceRunId, candidate, childId);
  assert.equal(editRequests, 1);
  assert.equal(
    useApp.getState().conversations[childId].active_artifact_id,
    savedArtifact.artifact_id,
  );
});

test("job-backed parent bundle Deck save survives recovery and remains the next Open", async () => {
  const parentId = "conv_parent_bundle_deck_edit";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_parent_bundle_deck_edit";
  bundle.revision = 4;
  bundle.backend_state = "partial";
  const authoringRunId = "run_parent_bundle_deck_authoring";
  const sourceRunId = "run_parent_bundle_deck_source";
  const sourceArtifactId = `art_${sourceRunId}`;
  bundle.tasks.deck = {
    ...bundle.tasks.deck,
    status: "complete",
    run_id: sourceRunId,
    authoring_run_id: authoringRunId,
    artifact_id: sourceArtifactId,
    terminal: true,
    process_free: true,
  };
  const childId = bundle.tasks.deck.child_conversation_id;
  const sourceArtifact: Artifact = {
    ...artifact(sourceRunId, "deck"),
    native_format: "html",
    native_file_url: `/api/files/runs/${sourceRunId}/final/deck.html`,
    layers: [{
      layer_id: "title-18",
      name: "Slide 18 title",
      kind: "text",
      z_index: 18,
      bbox: { x: 40, y: 40, w: 800, h: 80 },
      text: "Original final slide",
    }],
    attempt_lineage: {
      materialization_version: 2,
      status: "published",
      source_run_id: authoringRunId,
      source_attempt: 1,
      source_candidate_id: "deck-attempt-parent-card",
      source_candidate_sha256: "b".repeat(64),
    },
  };
  resetStore({
    [parentId]: conversation(parentId, {
      paper_bundle: bundle,
      artifacts: { [sourceArtifactId]: sourceArtifact },
    }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState(parentId, "deck"),
      artifacts: { [sourceArtifactId]: sourceArtifact },
      active_artifact_id: sourceArtifactId,
    }),
  }, parentId);

  useApp.getState().enterCanvas(sourceArtifactId);
  assert.equal(useApp.getState().current_conversation_id, parentId);
  assert.equal(
    useApp.getState().conversations[parentId].active_artifact_id,
    sourceArtifactId,
  );

  const savedRunId = "run_parent_bundle_deck_saved";
  const savedArtifact: Artifact = {
    ...sourceArtifact,
    artifact_id: `art_${savedRunId}`,
    native_file_url: `/api/files/runs/${savedRunId}/final/deck.html`,
    candidate_draft: true,
    attempt_lineage: {
      ...sourceArtifact.attempt_lineage,
      status: "draft",
      parent_draft_run_id: sourceRunId,
      published_artifact_id_at_fork: sourceArtifactId,
    },
    layers: sourceArtifact.layers.map((layer) => ({
      ...layer,
      text: "Edited final slide",
    })),
  };
  let editRequests = 0;
  globalThis.fetch = (async (input) => {
    assert.equal(String(input), "/api/edits/apply");
    editRequests += 1;
    return jsonResponse({
      message: {
        id: "msg_parent_bundle_deck_saved",
        role: "assistant",
        text: "Saved",
        ts: 3,
        run_id: savedRunId,
        artifact_id: savedArtifact.artifact_id,
        status: "done",
      },
      artifact: savedArtifact,
    });
  }) as typeof fetch;

  useApp.getState().updateLayer("title-18", { text: "Edited final slide" });
  await useApp.getState().flushAutoSave();

  let parent = useApp.getState().conversations[parentId];
  assert.equal(parent.paper_bundle?.kind, "parent");
  if (parent.paper_bundle?.kind !== "parent") return;
  assert.equal(parent.paper_bundle.tasks.deck.run_id, savedRunId);
  assert.equal(parent.paper_bundle.tasks.deck.artifact_id, savedArtifact.artifact_id);
  assert.ok(parent.artifacts[savedArtifact.artifact_id]);

  const recoveredJob = backendPaperBundlePublicationJob(parentId, bundle, 4, {
    source_run_id: authoringRunId,
    publication_run_id: sourceRunId,
    artifact_id: sourceArtifactId,
    source_candidate_id: "deck-attempt-parent-card",
    source_candidate_sha256: "b".repeat(64),
  }) as Record<string, any>;
  const deckPublication = recoveredJob.publications.poster;
  recoveredJob.publications = { deck: deckPublication };
  recoveredJob.children.deck.run_id = authoringRunId;
  recoveredJob.children.poster.run_id = "run_parent_bundle_poster_failed";
  recoveredJob.completed_children = ["deck"];
  paperBundleListOverride = [recoveredJob];
  const [parsedRecoveredJob] = await listPaperBundles();
  assert.equal(parsedRecoveredJob.publications.deck?.artifact_id, sourceArtifactId);

  await useApp.getState().recoverPaperBundles();

  parent = useApp.getState().conversations[parentId];
  assert.equal(parent.paper_bundle?.kind, "parent");
  if (parent.paper_bundle?.kind !== "parent") return;
  assert.equal(parent.paper_bundle.tasks.deck.run_id, savedRunId);
  assert.equal(parent.paper_bundle.tasks.deck.artifact_id, savedArtifact.artifact_id);

  useApp.getState().enterChat();
  parent = useApp.getState().conversations[parentId];
  assert.equal(parent.paper_bundle?.kind, "parent");
  if (parent.paper_bundle?.kind !== "parent") return;
  useApp.getState().enterCanvas(parent.paper_bundle.tasks.deck.artifact_id);
  assert.equal(editRequests, 1);
  assert.equal(
    useApp.getState().conversations[parentId].active_artifact_id,
    savedArtifact.artifact_id,
  );
});

test("isolated server history preserves a late local bundle and hides linked remote children", async () => {
  let releaseHistory!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return new Promise<Response>((resolve) => { releaseHistory = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  resetStore({ base: conversation("base") }, "base");
  const load = useApp.getState().loadServerHistory();
  await tick();

  const parentBundle = createPaperBundleParentState("bundle", "paper.pdf");
  const posterId = parentBundle.tasks.poster.child_conversation_id;
  resetStore({
    bundle: conversation("bundle", { paper_bundle: parentBundle, pending: true }),
  }, "bundle");

  releaseHistory(jsonResponse({
    conversations: {
      [posterId]: conversation(posterId, {
        title: "server poster child",
        updated_at: 10,
      }),
      ordinary: conversation("ordinary", { updated_at: 5 }),
    },
    imported_runs: 2,
    user_isolated: true,
  }));
  await load;

  const state = useApp.getState();
  assert.equal(state.current_conversation_id, "bundle");
  assert.equal(state.conversations.bundle.paper_bundle?.kind, "parent");
  assert.deepEqual(state.conversations[posterId].paper_bundle,
    createPaperBundleChildState("bundle", "poster"));
  assert.equal(state.conversations[posterId].title, "paper.pdf - Poster");
});

test("server history preserves a Paper All-in-One child draft opened in Canvas", async () => {
  const parentId = "bundle_canvas_draft";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  const childId = bundle.tasks.poster.child_conversation_id;
  const draft: Artifact = {
    ...artifact("canvas_history_draft", "poster"),
    candidate_draft: true,
    attempt_lineage: {
      status: "draft",
      source_run_id: "run_canvas_history_source",
      source_attempt: 1,
      source_candidate_id: "poster-attempt-canvas-history",
    },
  };
  resetStore({
    [parentId]: conversation(parentId, { paper_bundle: bundle }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState(parentId, "poster"),
      artifacts: { [draft.artifact_id]: draft },
      active_artifact_id: draft.artifact_id,
    }),
  }, childId);
  useApp.setState({ mode: "canvas" });
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({
        conversations: {},
        imported_runs: 0,
        user_isolated: true,
        request_scope: "test-user",
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().loadServerHistory();

  const state = useApp.getState();
  assert.equal(state.mode, "canvas");
  assert.equal(state.current_conversation_id, childId);
  assert.equal(
    state.conversations[childId].active_artifact_id,
    draft.artifact_id,
  );
});

test("an in-flight history refresh keeps a candidate draft after the user leaves Canvas", async () => {
  const parentId = "bundle_canvas_draft_switch";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  const childId = bundle.tasks.poster.child_conversation_id;
  const draft: Artifact = {
    ...artifact("canvas_history_switch_draft", "poster"),
    candidate_draft: true,
    attempt_lineage: {
      status: "draft",
      source_run_id: "run_canvas_history_switch_source",
      source_attempt: 1,
      source_candidate_id: "poster-attempt-canvas-history-switch",
    },
  };
  resetStore({
    [parentId]: conversation(parentId, { paper_bundle: bundle }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState(parentId, "poster"),
      artifacts: { [draft.artifact_id]: draft },
      active_artifact_id: draft.artifact_id,
    }),
  }, childId);
  useApp.setState({ mode: "canvas" });
  let releaseHistory!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return new Promise<Response>((resolve) => { releaseHistory = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const load = useApp.getState().loadServerHistory();
  await tick();
  useApp.setState({ current_conversation_id: parentId, mode: "chat" });
  releaseHistory(jsonResponse({
    conversations: {},
    imported_runs: 0,
    user_isolated: true,
    request_scope: "test-user",
  }));
  await load;

  const state = useApp.getState();
  assert.equal(state.current_conversation_id, parentId);
  assert.equal(
    state.conversations[childId]?.artifacts[draft.artifact_id]?.candidate_draft,
    true,
  );
});

test("empty isolated history replaces a stale current id with a visible conversation", async () => {
  resetStore({ stale: conversation("stale") }, "missing-current");
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({ conversations: {}, imported_runs: 0, user_isolated: true });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().loadServerHistory();

  const state = useApp.getState();
  assert.ok(state.conversations[state.current_conversation_id]);
  assert.notEqual(state.conversations[state.current_conversation_id].paper_bundle?.kind, "child");
  assert.equal(state.conversations.stale, undefined);
});

test("server history reconnects an ordinary in-progress run after backend restart", async () => {
  let artifactRequests = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({
        conversations: {
          server_run_active: conversation("server_run_active", {
            title: "Deck - running",
            updated_at: 20,
            pending: true,
            run_id: "run_active",
            messages: [{
              id: "msg_run_active",
              role: "assistant",
              text: "",
              ts: 20,
              run_id: "run_active",
              status: "streaming",
              task_type: "generate",
              task_payload: { artifact_type: "deck" },
            }],
          }),
        },
        imported_runs: 0,
        user_isolated: true,
      });
    }
    if (url === "/api/runs/run_active/artifact") {
      artifactRequests += 1;
      return artifactRequests === 1
        ? jsonResponse({ detail: "run still in progress" }, 404)
        : jsonResponse(responseForRun("run_active", "deck"));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;
  resetStore({ stale: conversation("stale") }, "stale");

  await useApp.getState().loadServerHistory();

  await waitFor(
    () => MockEventSource.instances.some((source) => source.url === "/api/runs/run_active/events"),
    "history recovery should reconnect the run SSE stream",
  );
  let restored = useApp.getState().conversations.server_run_active;
  assert.equal(restored.pending, true);
  assert.equal(restored.run_id, "run_active");

  MockEventSource.instances.at(-1)?.emit("run.done");
  await waitFor(
    () => useApp.getState().conversations.server_run_active?.pending === false,
    "recovered run should publish its artifact after the terminal event",
  );

  restored = useApp.getState().conversations.server_run_active;
  assert.equal(restored.run_id, undefined);
  assert.equal(restored.active_artifact_id, "art_run_active");
});

test("compact server history reconnects candidate publish with its original task metadata", async () => {
  const conversationId = "candidate_publish_conversation";
  const runId = "candidate_publish_run";
  const taskPayload = {
    artifact_type: "poster" as const,
    source_artifact_id: "art_candidate_draft",
    source_run_id: "source_author_run",
    source_candidate_id: "attempt-2",
  };
  const fullConversation = conversation(conversationId, {
    title: "Publishing selected attempt",
    updated_at: 20,
    pending: true,
    run_id: runId,
    messages: [{
      id: `msg_${runId}`,
      role: "assistant",
      text: "Publishing selected attempt.",
      ts: 20,
      run_id: runId,
      status: "streaming",
      task_type: "candidate_publish",
      task_payload: taskPayload,
    }],
  });
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/history/conversations/${conversationId}`) {
      return jsonResponse({
        conversation: fullConversation,
        user_isolated: true,
        request_scope: "test-user",
      });
    }
    if (url.startsWith("/api/history")) {
      return jsonResponse({
        conversations: {
          [conversationId]: {
            id: conversationId,
            title: "Publishing selected attempt",
            created_at: 10,
            updated_at: 20,
            message_count: 1,
            artifacts: {},
            active_artifact_id: null,
            pending: true,
            run_id: runId,
            pending_artifact_type: "poster",
            pending_task_type: "candidate_publish",
            pending_task_payload: taskPayload,
          },
        },
        imported_runs: 0,
        user_isolated: true,
      });
    }
    if (url === `/api/runs/${runId}/artifact`) {
      return jsonResponse({
        message: {
          id: `msg_${runId}`,
          role: "assistant",
          text: "Run cancelled.",
          ts: 21,
          run_id: runId,
          status: "error",
          failure: {
            status: "cancelled",
            produced_files: [],
            artifact_type: "poster",
          },
        },
        artifact: null,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;
  resetStore({ stale: conversation("stale") }, "stale");

  await useApp.getState().loadServerHistory();

  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === `/api/runs/${runId}/events`,
    ),
    "candidate publish history should reconnect the original derived run",
  );
  const restored = useApp.getState().conversations[conversationId];
  const message = restored.messages.find((candidate) => candidate.run_id === runId);
  assert.equal(message?.task_type, "candidate_publish");
  assert.deepEqual(message?.task_payload, taskPayload);
  const publicationRecovery = MockEventSource.instances.find(
    (source) => source.url === `/api/runs/${runId}/events`,
  );
  publicationRecovery?.emit("run.cancelled");
  await waitFor(
    () => publicationRecovery?.readyState === MockEventSource.CLOSED,
    "candidate publication recovery did not release its terminal wait",
  );
});

test("isolated history preserves only ordinary conversations created or mutated after request start", async () => {
  let releaseHistory!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return new Promise<Response>((resolve) => { releaseHistory = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;
  resetStore({
    untouched: conversation("untouched", { updated_at: 10 }),
    pending: conversation("pending", { updated_at: 10 }),
  }, "untouched");

  const load = useApp.getState().loadServerHistory();
  await tick();
  useApp.setState((state) => ({
    conversations: {
      ...state.conversations,
      pending: {
        ...state.conversations.pending,
        updated_at: 11,
        pending: true,
        run_id: "run_pending",
      },
      created: conversation("created", {
        updated_at: 12,
        pending: true,
        run_id: "run_created",
      }),
    },
    current_conversation_id: "created",
  }));
  releaseHistory(jsonResponse({
    conversations: { server: conversation("server", { updated_at: 20 }) },
    imported_runs: 1,
    user_isolated: true,
  }));
  await load;

  const state = useApp.getState();
  assert.equal(state.conversations.untouched, undefined);
  assert.equal(state.conversations.pending.run_id, "run_pending");
  assert.equal(state.conversations.created.run_id, "run_created");
  assert.ok(state.conversations.server);
  assert.equal(state.current_conversation_id, "created");
});

test("isolated server history drops stale local paper bundles", async () => {
  const staleBundle = createPaperBundleParentState("stale-bundle", "stale.pdf");
  const staleChildren = Object.fromEntries(
    Object.values(staleBundle.tasks).map((task) => [
      task.child_conversation_id,
      conversation(task.child_conversation_id, {
        paper_bundle: createPaperBundleChildState("stale-bundle", task.artifact_type),
      }),
    ]),
  );
  resetStore({
    "stale-bundle": conversation("stale-bundle", { paper_bundle: staleBundle }),
    ...staleChildren,
  }, "stale-bundle");
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({
        conversations: { server: conversation("server", { updated_at: 20 }) },
        imported_runs: 1,
        user_isolated: true,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().loadServerHistory();

  const state = useApp.getState();
  assert.equal(state.conversations["stale-bundle"], undefined);
  for (const childId of Object.keys(staleChildren)) {
    assert.equal(state.conversations[childId], undefined);
  }
  assert.ok(state.conversations.server);
});

test("server_run history merges into the bundle child matched by message run_id", async () => {
  const runId = "20260721-123456-deadbeef";
  const serverConversationId = `server_run_${runId}`;
  const parentBundle = createPaperBundleParentState("bundle", "paper.pdf");
  const posterId = parentBundle.tasks.poster.child_conversation_id;
  parentBundle.tasks.poster = {
    ...parentBundle.tasks.poster,
    status: "complete",
    run_id: runId,
    artifact_id: `art_${runId}`,
  };
  for (const type of ["deck", "landing", "video"] as const) {
    parentBundle.tasks[type] = { ...parentBundle.tasks[type], status: "failed" };
  }
  resetStore({
    bundle: conversation("bundle", { paper_bundle: parentBundle }),
    [posterId]: conversation(posterId, {
      paper_bundle: createPaperBundleChildState("bundle", "poster"),
    }),
  }, "bundle");
  const serverArtifact = artifact(runId, "poster");
  const hydratedArtifact: Artifact = {
    ...serverArtifact,
    layers: [{ id: "layer_1", type: "text", name: "Editable title" }],
    native_file_url: "/api/runs/recovered/native",
    view_file_url: "/api/runs/recovered/view",
  };
  let detailFetches = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/history/conversations/${serverConversationId}`) {
      detailFetches += 1;
      return jsonResponse({
        conversation: conversation(serverConversationId, {
          title: "Recovered poster run",
          updated_at: 21,
          messages: [{
            id: `msg_${runId}`,
            role: "assistant",
            text: "Done",
            ts: 21,
            run_id: runId,
            artifact_id: hydratedArtifact.artifact_id,
            status: "done",
          }],
          artifacts: { [hydratedArtifact.artifact_id]: hydratedArtifact },
          active_artifact_id: hydratedArtifact.artifact_id,
        }),
        imported_runs: 1,
        user_isolated: true,
      });
    }
    if (url.startsWith("/api/history")) {
      return jsonResponse({
        conversations: {
          [serverConversationId]: {
            id: serverConversationId,
            title: "Recovered poster run",
            created_at: 20,
            updated_at: 20,
            message_count: 1,
            artifacts: {
              [serverArtifact.artifact_id]: {
                artifact_id: serverArtifact.artifact_id,
                name: serverArtifact.name,
                artifact_type: serverArtifact.artifact_type,
                canvas: serverArtifact.canvas,
              },
            },
            active_artifact_id: serverArtifact.artifact_id,
            last_run: {
              run_id: runId,
              status: "done",
              artifact_id: serverArtifact.artifact_id,
            },
          },
        },
        imported_runs: 1,
        user_isolated: true,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().loadServerHistory();
  await waitFor(
    () => useApp.getState().conversations[posterId]?.history_summary !== true,
    "recovered Paper All-in-One child did not hydrate",
  );

  const state = useApp.getState();
  assert.ok(state.conversations.bundle);
  assert.equal(state.conversations[serverConversationId], undefined);
  assert.equal(state.conversations[posterId].id, posterId);
  assert.equal(state.conversations[posterId].title, "Recovered poster run");
  assert.equal(state.conversations[posterId].history_summary, undefined);
  assert.equal(detailFetches, 1);
  assert.equal(state.conversations[posterId].artifacts[hydratedArtifact.artifact_id].native_file_url, hydratedArtifact.native_file_url);
  assert.deepEqual(
    state.conversations[posterId].paper_bundle,
    createPaperBundleChildState("bundle", "poster"),
  );
  assert.equal(state.conversations.bundle.paper_bundle?.kind, "parent");
  assert.equal(
    (state.conversations.bundle.paper_bundle as PaperBundleParentState).tasks.poster.status,
    "complete",
  );
  assert.equal(
    (state.conversations.bundle.paper_bundle as PaperBundleParentState).tasks.poster.artifact_id,
    serverArtifact.artifact_id,
  );
  assert.equal(
    state.conversations.bundle.artifacts[hydratedArtifact.artifact_id].native_file_url,
    hydratedArtifact.native_file_url,
  );
});

test("server_run terminal history promotes a recovered artifact without replacing a valid parent active artifact", async () => {
  const runId = "run_terminal_history";
  const parentBundle = createPaperBundleParentState("bundle", "paper.pdf");
  const posterId = parentBundle.tasks.poster.child_conversation_id;
  parentBundle.tasks.poster = {
    ...parentBundle.tasks.poster,
    status: "running",
    run_id: runId,
    started_at: 10,
    attempts: 2,
    max_attempts: 12,
  };
  for (const type of ["deck", "landing", "video"] as const) {
    parentBundle.tasks[type] = { ...parentBundle.tasks[type], status: "failed" };
  }
  const selectedArtifact = artifact("selected", "landing");
  const recoveredArtifact = artifact(runId, "poster");
  resetStore({
    bundle: conversation("bundle", {
      paper_bundle: parentBundle,
      artifacts: { [selectedArtifact.artifact_id]: selectedArtifact },
      active_artifact_id: selectedArtifact.artifact_id,
    }),
    [posterId]: conversation(posterId, {
      paper_bundle: createPaperBundleChildState("bundle", "poster"),
      pending: true,
      run_id: runId,
    }),
  }, "bundle");
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({
        conversations: {
          [`server_run_${runId}`]: conversation(`server_run_${runId}`, {
            updated_at: 20,
            messages: [{
              id: `msg_${runId}`,
              role: "assistant",
              text: "Done",
              ts: 20,
              run_id: runId,
              artifact_id: recoveredArtifact.artifact_id,
              status: "done",
            }],
            artifacts: { [recoveredArtifact.artifact_id]: recoveredArtifact },
            active_artifact_id: recoveredArtifact.artifact_id,
          }),
        },
        imported_runs: 1,
        user_isolated: true,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().loadServerHistory();

  const parent = useApp.getState().conversations.bundle;
  assert.equal(parent.paper_bundle?.kind, "parent");
  assert.equal((parent.paper_bundle as PaperBundleParentState).tasks.poster.status, "complete");
  assert.equal(
    (parent.paper_bundle as PaperBundleParentState).tasks.poster.artifact_id,
    recoveredArtifact.artifact_id,
  );
  assert.equal(
    parent.artifacts[recoveredArtifact.artifact_id].artifact_id,
    recoveredArtifact.artifact_id,
  );
  assert.equal(
    (parent.paper_bundle as PaperBundleParentState).tasks.poster.finished_at,
    20,
  );
  assert.equal(
    (parent.paper_bundle as PaperBundleParentState).tasks.poster.attempts,
    2,
  );
  assert.equal(
    (parent.paper_bundle as PaperBundleParentState).tasks.poster.max_attempts,
    12,
  );
  assert.equal(parent.active_artifact_id, selectedArtifact.artifact_id);
  assert.equal(parent.pending, false);
});

test("server history uses the newer terminal time when a cancelled task is promoted to complete", async () => {
  const runId = "run_cancelled_then_complete";
  const parentBundle = createPaperBundleParentState("bundle", "paper.pdf");
  const posterId = parentBundle.tasks.poster.child_conversation_id;
  parentBundle.tasks.poster = {
    ...parentBundle.tasks.poster,
    status: "cancelled",
    run_id: runId,
    started_at: 10,
    finished_at: 15,
    error: "Run cancelled.",
  };
  for (const type of ["deck", "landing", "video"] as const) {
    parentBundle.tasks[type] = { ...parentBundle.tasks[type], status: "failed" };
  }
  const recoveredArtifact = artifact(runId, "poster");
  resetStore({
    bundle: conversation("bundle", { paper_bundle: parentBundle }),
    [posterId]: conversation(posterId, {
      paper_bundle: createPaperBundleChildState("bundle", "poster"),
    }),
  }, "bundle");
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({
        conversations: {
          [`server_run_${runId}`]: conversation(`server_run_${runId}`, {
            updated_at: 30,
            messages: [{
              id: `msg_${runId}`,
              role: "assistant",
              text: "Done",
              ts: 30,
              run_id: runId,
              artifact_id: recoveredArtifact.artifact_id,
              status: "done",
            }],
            artifacts: { [recoveredArtifact.artifact_id]: recoveredArtifact },
            active_artifact_id: recoveredArtifact.artifact_id,
          }),
        },
        imported_runs: 1,
        user_isolated: true,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().loadServerHistory();

  const task = (useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState)
    .tasks.poster;
  assert.equal(task.status, "complete");
  assert.equal(task.finished_at, 30);
  assert.equal(task.error, undefined);
});

test("successful server history clears a stale same-run degraded bundle warning", async () => {
  const runId = "run_degraded_history";
  const parentBundle = createPaperBundleParentState("bundle", "paper.pdf");
  const posterId = parentBundle.tasks.poster.child_conversation_id;
  parentBundle.tasks.poster = {
    ...parentBundle.tasks.poster,
    status: "complete",
    run_id: runId,
    artifact_id: `art_${runId}`,
    error: "Critic score remained below the quality gate.",
  };
  for (const type of ["deck", "landing", "video"] as const) {
    parentBundle.tasks[type] = { ...parentBundle.tasks[type], status: "failed" };
  }
  const recoveredArtifact = artifact(runId, "poster");
  resetStore({
    bundle: conversation("bundle", { paper_bundle: parentBundle }),
    [posterId]: conversation(posterId, {
      paper_bundle: createPaperBundleChildState("bundle", "poster"),
    }),
  }, "bundle");
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({
        conversations: {
          [`server_run_${runId}`]: conversation(`server_run_${runId}`, {
            updated_at: 20,
            messages: [{
              id: `msg_${runId}`,
              role: "assistant",
              text: "Done",
              ts: 20,
              run_id: runId,
              artifact_id: recoveredArtifact.artifact_id,
              status: "done",
            }],
            artifacts: { [recoveredArtifact.artifact_id]: recoveredArtifact },
            active_artifact_id: recoveredArtifact.artifact_id,
          }),
        },
        imported_runs: 1,
        user_isolated: true,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().loadServerHistory();

  const bundle = useApp.getState().conversations.bundle.paper_bundle;
  assert.equal(bundle?.kind, "parent");
  assert.equal((bundle as PaperBundleParentState).tasks.poster.error, undefined);
});

test("server_run terminal failure does not promote a stale child artifact", async () => {
  const runId = "run_failed_history";
  const parentBundle = createPaperBundleParentState("bundle", "paper.pdf");
  const posterId = parentBundle.tasks.poster.child_conversation_id;
  parentBundle.tasks.poster = {
    ...parentBundle.tasks.poster,
    status: "running",
    run_id: runId,
  };
  for (const type of ["deck", "landing", "video"] as const) {
    parentBundle.tasks[type] = { ...parentBundle.tasks[type], status: "failed" };
  }
  const staleArtifact = artifact("stale-child", "poster");
  resetStore({
    bundle: conversation("bundle", { paper_bundle: parentBundle }),
    [posterId]: conversation(posterId, {
      paper_bundle: createPaperBundleChildState("bundle", "poster"),
      artifacts: { [staleArtifact.artifact_id]: staleArtifact },
      active_artifact_id: staleArtifact.artifact_id,
      pending: true,
      run_id: runId,
    }),
  }, "bundle");
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({
        conversations: {
          [`server_run_${runId}`]: conversation(`server_run_${runId}`, {
            updated_at: 20,
            messages: [{
              id: `msg_${runId}`,
              role: "assistant",
              text: "Generation failed",
              ts: 20,
              run_id: runId,
              status: "error",
              failure: { status: "error", produced_files: [] },
            }],
          }),
        },
        imported_runs: 1,
        user_isolated: true,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().loadServerHistory();

  const bundle = useApp.getState().conversations.bundle.paper_bundle;
  assert.equal(bundle?.kind, "parent");
  assert.equal((bundle as PaperBundleParentState).tasks.poster.status, "failed");
  assert.equal((bundle as PaperBundleParentState).tasks.poster.artifact_id, undefined);
  assert.equal(useApp.getState().conversations.bundle.artifacts[staleArtifact.artifact_id], undefined);
});

test("late isolated server_run does not promote into a bundle parent changed after request start", async () => {
  const runId = "run_stale_parent";
  const parentBundle = createPaperBundleParentState("bundle", "paper.pdf");
  const posterId = parentBundle.tasks.poster.child_conversation_id;
  parentBundle.tasks.poster = {
    ...parentBundle.tasks.poster,
    status: "running",
    run_id: runId,
  };
  for (const type of ["deck", "landing", "video"] as const) {
    parentBundle.tasks[type] = { ...parentBundle.tasks[type], status: "failed" };
  }
  resetStore({
    bundle: conversation("bundle", { paper_bundle: parentBundle }),
    [posterId]: conversation(posterId, {
      paper_bundle: createPaperBundleChildState("bundle", "poster"),
      pending: true,
      run_id: runId,
    }),
  }, "bundle");
  let releaseHistory!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return new Promise<Response>((resolve) => { releaseHistory = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const load = useApp.getState().loadServerHistory();
  await tick();
  const changedBundle = {
    ...parentBundle,
    tasks: {
      ...parentBundle.tasks,
      poster: { ...parentBundle.tasks.poster, status: "failed" as const, error: "Stopped locally" },
    },
  };
  const changedParent = {
    ...useApp.getState().conversations.bundle,
    updated_at: 2,
    pending: false,
    paper_bundle: changedBundle,
  };
  useApp.setState((state) => ({
    conversations: { ...state.conversations, bundle: changedParent },
  }));
  const staleArtifact = artifact(runId, "poster");
  releaseHistory(jsonResponse({
    conversations: {
      [`server_run_${runId}`]: conversation(`server_run_${runId}`, {
        updated_at: 20,
        messages: [{
          id: `msg_${runId}`,
          role: "assistant",
          text: "Done",
          ts: 20,
          run_id: runId,
          artifact_id: staleArtifact.artifact_id,
          status: "done",
        }],
        artifacts: { [staleArtifact.artifact_id]: staleArtifact },
        active_artifact_id: staleArtifact.artifact_id,
      }),
    },
    imported_runs: 1,
    user_isolated: true,
  }));
  await load;

  assert.deepEqual(useApp.getState().conversations.bundle, changedParent);
  assert.equal(useApp.getState().conversations.bundle.artifacts[staleArtifact.artifact_id], undefined);
});

test("late isolated history does not hydrate or overwrite a conversation changed after request start", async () => {
  const localArtifact = artifact("shared", "poster");
  resetStore({
    changed: conversation("changed", {
      title: "Before request",
      artifacts: { [localArtifact.artifact_id]: localArtifact },
      active_artifact_id: localArtifact.artifact_id,
    }),
  }, "changed");
  let releaseHistory!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return new Promise<Response>((resolve) => { releaseHistory = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const load = useApp.getState().loadServerHistory();
  await tick();
  const changedArtifact = { ...localArtifact, name: "Locally changed artifact" };
  const changedConversation = {
    ...useApp.getState().conversations.changed,
    title: "Locally changed conversation",
    updated_at: 2,
    artifacts: { [changedArtifact.artifact_id]: changedArtifact },
  };
  useApp.setState((state) => ({
    conversations: { ...state.conversations, changed: changedConversation },
  }));
  const staleServerArtifact = {
    ...localArtifact,
    name: "Stale server artifact",
    native_file_url: "/stale/server/poster.html",
  };
  releaseHistory(jsonResponse({
    conversations: {
      changed: conversation("changed", {
        title: "Stale server conversation",
        updated_at: 50,
        artifacts: { [staleServerArtifact.artifact_id]: staleServerArtifact },
        active_artifact_id: staleServerArtifact.artifact_id,
      }),
    },
    imported_runs: 1,
    user_isolated: true,
  }));
  await load;

  assert.equal(useApp.getState().conversations.changed, changedConversation);
  assert.equal(
    useApp.getState().conversations.changed.artifacts[changedArtifact.artifact_id],
    changedArtifact,
  );
});

test("isolated history drops a recovering persisted bundle after identity rotation", async () => {
  const oldBundle = createPaperBundleParentState("old-bundle", "old.pdf");
  const posterId = oldBundle.tasks.poster.child_conversation_id;
  oldBundle.tasks.poster = {
    ...oldBundle.tasks.poster,
    status: "running",
    run_id: "run_old_scope",
  };
  resetStore({
    "old-bundle": conversation("old-bundle", { paper_bundle: oldBundle }),
    [posterId]: conversation(posterId, {
      paper_bundle: createPaperBundleChildState("old-bundle", "poster"),
      pending: true,
      run_id: "run_old_scope",
    }),
  }, "old-bundle");
  useApp.setState({ history_user_scope: "old-user" });
  localStorage.setItem("autodesign.demo_user.v1", "new-user");
  let requestUser = "";
  let releaseHistory!: (response: Response) => void;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      requestUser = new Headers(init?.headers).get("X-Demo-User") ?? "";
      return new Promise<Response>((resolve) => { releaseHistory = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const load = useApp.getState().loadServerHistory();
  await tick();
  useApp.setState((state) => ({
    conversations: {
      ...state.conversations,
      "old-bundle": {
        ...state.conversations["old-bundle"],
        updated_at: 2,
        pending: true,
      },
    },
  }));
  releaseHistory(jsonResponse({
    conversations: { server: conversation("server", { updated_at: 20 }) },
    imported_runs: 1,
    user_isolated: true,
  }));
  await load;

  const state = useApp.getState();
  assert.equal(requestUser, "new-user");
  assert.equal(state.history_user_scope, "new-user");
  assert.equal(state.conversations["old-bundle"], undefined);
  assert.equal(state.conversations[posterId], undefined);
  assert.ok(state.conversations.server);
});

test("isolated history ignores an old-scope response when identity rotates in flight", async () => {
  resetStore({ old: conversation("old") }, "old");
  useApp.setState({ history_user_scope: "old-user" });
  localStorage.setItem("autodesign.demo_user.v1", "old-user");
  let requestUser = "";
  let releaseHistory!: (response: Response) => void;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      requestUser = new Headers(init?.headers).get("X-Demo-User") ?? "";
      return new Promise<Response>((resolve) => { releaseHistory = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const load = useApp.getState().loadServerHistory();
  await tick();
  localStorage.setItem("autodesign.demo_user.v1", "new-user");
  useApp.setState((state) => ({
    conversations: {
      ...state.conversations,
      "new-local": conversation("new-local", { title: "New scope draft", updated_at: 30 }),
      "old-recovery-child": conversation("old-recovery-child", {
        updated_at: 31,
        paper_bundle: createPaperBundleChildState("old", "poster"),
      }),
    },
    current_conversation_id: "new-local",
  }));
  releaseHistory(jsonResponse({
    conversations: { oldServer: conversation("oldServer", { updated_at: 20 }) },
    imported_runs: 1,
    user_isolated: true,
  }));
  await load;

  const state = useApp.getState();
  assert.equal(requestUser, "old-user");
  assert.equal(state.history_user_scope, "new-user");
  assert.equal(state.conversations.old, undefined);
  assert.equal(state.conversations.oldServer, undefined);
  assert.equal(state.conversations["old-recovery-child"], undefined);
  assert.equal(state.conversations["new-local"].title, "New scope draft");
  assert.equal(state.current_conversation_id, "new-local");
});

test("persisted conversations from another user scope are removed during rehydration", async () => {
  resetStore({ current: conversation("current") }, "current");
  localStorage.setItem("autodesign.demo_user.v1", "new-user");
  localStorage.setItem("autodesign.web.v1", JSON.stringify({
    version: 1,
    state: {
      conversations: { old: conversation("old") },
      current_conversation_id: "old",
      history_user_scope: "old-user",
    },
  }));

  const rehydration = useApp.persist.rehydrate();

  assert.equal(useApp.getState().conversations.old, undefined);
  assert.equal(useApp.getState().history_user_scope, "new-user");
  await rehydration;
});

test("PPTX export rejects an active paper bundle before state or network mutation", async () => {
  const bundle = createPaperBundleParentState("bundle", "paper.pdf");
  const source = artifact("source", "poster");
  const parent = conversation("bundle", {
    paper_bundle: bundle,
    artifacts: { [source.artifact_id]: source },
    active_artifact_id: source.artifact_id,
  });
  resetStore({ bundle: parent }, "bundle");
  let fetches = 0;
  globalThis.fetch = (async () => {
    fetches += 1;
    throw new Error("PPTX guard should prevent fetch");
  }) as typeof fetch;

  await assert.rejects(
    useApp.getState().exportArtifactPptx(source.artifact_id),
    /Paper All-in-One.*finish/i,
  );

  assert.equal(fetches, 0);
  assert.equal(useApp.getState().conversations.bundle, parent);
});

test("PPTX export rejects a duplicate conversation request before another mutation", async () => {
  const source = artifact("source", "poster");
  resetStore({
    ordinary: conversation("ordinary", {
      artifacts: { [source.artifact_id]: source },
      active_artifact_id: null,
    }),
  }, "ordinary");
  let releaseExport!: (response: Response) => void;
  let exportRequests = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/artifacts/export/pptx-run") {
      exportRequests += 1;
      if (exportRequests === 1) {
        return new Promise<Response>((resolve) => { releaseExport = resolve; });
      }
      return jsonResponse({ detail: "unguarded duplicate" }, 500);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const first = useApp.getState().exportArtifactPptx(source.artifact_id);
  await waitFor(() => exportRequests === 1, "first PPTX export did not start");
  const messagesAfterFirst = useApp.getState().conversations.ordinary.messages;

  try {
    await assert.rejects(
      useApp.getState().exportArtifactPptx(source.artifact_id),
      /already running/i,
    );
    assert.equal(exportRequests, 1);
    assert.equal(useApp.getState().conversations.ordinary.messages, messagesAfterFirst);
  } finally {
    releaseExport(jsonResponse({ detail: "stop test export" }, 500));
    await first;
  }
});

test("PPTX export retries a transient post-terminal artifact race", async () => {
  const source = artifact("pptx_retry_source", "poster");
  resetStore({
    ordinary: conversation("ordinary", {
      artifacts: { [source.artifact_id]: source },
      active_artifact_id: source.artifact_id,
    }),
  }, "ordinary");
  const runId = "run_pptx_artifact_retry";
  let artifactReads = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/artifacts/export/pptx-run") {
      return jsonResponse({
        run_id: runId,
        placeholder_message: {
          id: `msg_${runId}`,
          role: "assistant",
          text: "",
          ts: 1,
          status: "streaming",
        },
        progress_mode: "artifact_export",
      });
    }
    if (url === `/api/runs/${runId}/artifact`) {
      artifactReads += 1;
      if (artifactReads === 1) {
        return jsonResponse({ detail: "artifact commit pending" }, 504);
      }
      return jsonResponse({
        message: {
          id: `msg_${runId}`,
          role: "assistant",
          text: "Export ready.",
          ts: 2,
          run_id: runId,
          status: "done",
        },
        artifact: null,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const exporting = useApp.getState().exportArtifactPptx(source.artifact_id);
  await waitFor(() => MockEventSource.instances.length === 1, "PPTX event stream did not start");
  MockEventSource.instances[0].emit("run.done", { event_id: "pptx-terminal" });
  await exporting;

  assert.equal(artifactReads, 2);
  const latest = useApp.getState().conversations.ordinary.messages.at(-1);
  assert.equal(latest?.status, "done");
  assert.equal(latest?.failure, undefined);
});

test("Paper All-in-One rejects a conversation with an active PPTX export lock", async () => {
  const source = artifact("source", "poster");
  resetStore({
    ordinary: conversation("ordinary", {
      artifacts: { [source.artifact_id]: source },
      active_artifact_id: null,
    }),
  }, "ordinary");
  let releaseExport!: (response: Response) => void;
  let bundleRequests = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/artifacts/export/pptx-run") {
      return new Promise<Response>((resolve) => { releaseExport = resolve; });
    }
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        needs_setup: false,
      });
    }
    if (url === "/api/generate") bundleRequests += 1;
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const exporting = useApp.getState().exportArtifactPptx(source.artifact_id);
  await waitFor(() => useApp.getState().conversations.ordinary.pending === true, "PPTX export did not start");

  try {
    await assert.rejects(
      useApp.getState().startPaperBundle(
        new File(["paper"], "paper.pdf", { type: "application/pdf" }),
      ),
      /PowerPoint export.*already running/i,
    );
    assert.equal(bundleRequests, 0);
    assert.equal(useApp.getState().conversations.ordinary.paper_bundle, undefined);
  } finally {
    releaseExport(jsonResponse({ detail: "stop test export" }, 500));
    await exporting;
  }
});

test("PPTX resume shares the conversation lock and releases it after failure", async () => {
  const source = {
    ...artifact("source", "poster"),
    native_format: "html",
    native_file_url: "/runs/source/poster.html",
  };
  const exportUser = (id: string) => ({
    id: `user-${id}`,
    role: "user" as const,
    text: `Export this design as an editable PPTX: ${source.name}`,
    ts: 1,
    status: "done" as const,
    task_type: "artifact_export_pptx" as const,
    task_payload: { source_artifact_id: source.artifact_id, export_format: "pptx" as const },
    source_artifact_id: source.artifact_id,
  });
  const failedExport = (id: string) => ({
    id,
    role: "assistant" as const,
    text: "PowerPoint export interrupted.",
    ts: 2,
    status: "error" as const,
    task_type: "artifact_export_pptx" as const,
    task_payload: { source_artifact_id: source.artifact_id, export_format: "pptx" as const },
    source_artifact_id: source.artifact_id,
    failure: { status: "connection_lost", produced_files: [] },
  });
  resetStore({
    ordinary: conversation("ordinary", {
      messages: [exportUser("one"), failedExport("failed-one"), exportUser("two"), failedExport("failed-two")],
      artifacts: { [source.artifact_id]: source },
      active_artifact_id: source.artifact_id,
    }),
  }, "ordinary");
  let releaseFirst!: (response: Response) => void;
  let exportRequests = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/artifacts/export/pptx-run") {
      exportRequests += 1;
      if (exportRequests === 1) {
        return new Promise<Response>((resolve) => { releaseFirst = resolve; });
      }
      return jsonResponse({ detail: "stop resumed export" }, 500);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const first = useApp.getState().resumeRun("failed-one");
  await waitFor(() => exportRequests === 1, "first resumed PPTX export did not start");
  const secondFailure = useApp.getState().conversations.ordinary.messages
    .find((message) => message.id === "failed-two");

  await assert.rejects(
    useApp.getState().resumeRun("failed-two"),
    /already running/i,
  );
  assert.equal(exportRequests, 1);
  assert.equal(
    useApp.getState().conversations.ordinary.messages.find((message) => message.id === "failed-two"),
    secondFailure,
  );

  releaseFirst(jsonResponse({ detail: "stop first resumed export" }, 500));
  await first;
  await useApp.getState().resumeRun("failed-two");
  assert.equal(exportRequests, 2);
});

test("PPTX resume rejects an active paper bundle before state or network mutation", async () => {
  const source = {
    ...artifact("source", "poster"),
    native_format: "html",
    native_file_url: "/runs/source/poster.html",
  };
  const bundle = createPaperBundleParentState("bundle", "paper.pdf");
  const exportUser = {
    id: "export-user",
    role: "user" as const,
    text: `Export this design as an editable PPTX: ${source.name}`,
    ts: 1,
    status: "done" as const,
    task_type: "artifact_export_pptx" as const,
    task_payload: { source_artifact_id: source.artifact_id, export_format: "pptx" as const },
    source_artifact_id: source.artifact_id,
  };
  const failedExport = {
    id: "failed-export",
    role: "assistant" as const,
    text: "PowerPoint export interrupted.",
    ts: 2,
    status: "error" as const,
    task_type: "artifact_export_pptx" as const,
    task_payload: { source_artifact_id: source.artifact_id, export_format: "pptx" as const },
    source_artifact_id: source.artifact_id,
    failure: { status: "connection_lost" as const, produced_files: [] },
  };
  const parent = conversation("bundle", {
    paper_bundle: bundle,
    messages: [exportUser, failedExport],
    artifacts: { [source.artifact_id]: source },
    active_artifact_id: source.artifact_id,
  });
  resetStore({ bundle: parent }, "bundle");
  let fetches = 0;
  globalThis.fetch = (async () => {
    fetches += 1;
    throw new Error("PPTX resume guard should prevent fetch");
  }) as typeof fetch;

  await assert.rejects(
    useApp.getState().resumeRun("failed-export"),
    /Paper All-in-One.*finish/i,
  );

  assert.equal(fetches, 0);
  assert.equal(useApp.getState().conversations.bundle, parent);
});

test("rehydration converts an ownerless streaming PPTX placeholder into a resumable error", async () => {
  resetStore({ current: conversation("current") }, "current");
  const source = {
    ...artifact("source", "poster"),
    native_format: "html",
    native_file_url: "/runs/source/poster.html",
  };
  const persisted = conversation("persisted", {
    messages: [{
      id: "export-user",
      role: "user",
      text: `Export this design as an editable PPTX: ${source.name}`,
      ts: 1,
      status: "done",
      task_type: "artifact_export_pptx",
      source_artifact_id: source.artifact_id,
    }, {
      id: "export-placeholder",
      role: "assistant",
      text: "",
      ts: 2,
      status: "streaming",
      task_type: "artifact_export_pptx",
      task_payload: { source_artifact_id: source.artifact_id, export_format: "pptx" },
      source_artifact_id: source.artifact_id,
    }],
    artifacts: { [source.artifact_id]: source },
    active_artifact_id: source.artifact_id,
    pending: true,
    run_id: "run_orphaned_export",
  });
  localStorage.setItem("autodesign.web.v1", JSON.stringify({
    version: 1,
    state: {
      conversations: { persisted },
      current_conversation_id: "persisted",
      history_user_scope: "test-user",
    },
  }));

  await useApp.persist.rehydrate();

  const placeholder = useApp.getState().conversations.persisted.messages
    .find((message) => message.id === "export-placeholder");
  assert.equal(placeholder?.status, "error");
  assert.equal(placeholder?.failure?.status, "connection_lost");
  assert.equal(placeholder?.task_type, "artifact_export_pptx");
  assert.equal(useApp.getState().conversations.persisted.pending, undefined);
  assert.equal(useApp.getState().conversations.persisted.run_id, undefined);
});

test("immediate isolated history keeps a rehydrated orphaned PPTX failure when the server has no match", async () => {
  resetStore({ current: conversation("current") }, "current");
  const source = {
    ...artifact("source", "poster"),
    native_format: "html",
    native_file_url: "/runs/source/poster.html",
  };
  const persisted = conversation("persisted", {
    messages: [{
      id: "export-user",
      role: "user",
      text: `Export this design as an editable PPTX: ${source.name}`,
      ts: 1,
      status: "done",
      task_type: "artifact_export_pptx",
      source_artifact_id: source.artifact_id,
    }, {
      id: "export-placeholder",
      role: "assistant",
      text: "",
      ts: 2,
      status: "streaming",
      task_type: "artifact_export_pptx",
      task_payload: { source_artifact_id: source.artifact_id, export_format: "pptx" },
      source_artifact_id: source.artifact_id,
    }],
    artifacts: { [source.artifact_id]: source },
    active_artifact_id: source.artifact_id,
    pending: true,
    run_id: "run_orphaned_export",
  });
  localStorage.setItem("autodesign.web.v1", JSON.stringify({
    version: 1,
    state: {
      conversations: { persisted },
      current_conversation_id: "persisted",
      history_user_scope: "test-user",
    },
  }));
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({ conversations: {}, imported_runs: 0, user_isolated: true });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.persist.rehydrate();
  await useApp.getState().loadServerHistory();

  const state = useApp.getState();
  const placeholder = state.conversations.persisted?.messages
    .find((message) => message.id === "export-placeholder");
  assert.equal(placeholder?.status, "error");
  assert.equal(placeholder?.failure?.status, "connection_lost");
  assert.equal(state.current_conversation_id, "persisted");
});

test("isolated history does not keep a rehydrated orphaned PPTX failure across user scopes", async () => {
  resetStore({ current: conversation("current") }, "current");
  const source = {
    ...artifact("source", "poster"),
    native_format: "html",
    native_file_url: "/runs/source/poster.html",
  };
  const persisted = conversation("persisted", {
    messages: [{
      id: "export-user",
      role: "user",
      text: `Export this design as an editable PPTX: ${source.name}`,
      ts: 1,
      status: "done",
      task_type: "artifact_export_pptx",
      source_artifact_id: source.artifact_id,
    }, {
      id: "export-placeholder",
      role: "assistant",
      text: "",
      ts: 2,
      status: "streaming",
      task_type: "artifact_export_pptx",
      task_payload: { source_artifact_id: source.artifact_id, export_format: "pptx" },
      source_artifact_id: source.artifact_id,
    }],
    artifacts: { [source.artifact_id]: source },
    active_artifact_id: source.artifact_id,
    pending: true,
    run_id: "run_orphaned_export",
  });
  localStorage.setItem("autodesign.web.v1", JSON.stringify({
    version: 1,
    state: {
      conversations: { persisted },
      current_conversation_id: "persisted",
      history_user_scope: "old-user",
    },
  }));
  localStorage.setItem("autodesign.demo_user.v1", "new-user");
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({ conversations: {}, imported_runs: 0, user_isolated: true });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.persist.rehydrate();
  await useApp.getState().loadServerHistory();

  const state = useApp.getState();
  assert.equal(state.conversations.persisted, undefined);
  assert.equal(state.history_user_scope, "new-user");
});

test("history merge derives bundle pending and recovers a persisted run idempotently", async () => {
  const parentBundle = createPaperBundleParentState("bundle", "paper.pdf");
  const childId = parentBundle.tasks.poster.child_conversation_id;
  parentBundle.tasks.poster = {
    ...parentBundle.tasks.poster,
    status: "running",
    run_id: "run_recover",
  };
  for (const type of ["deck", "landing", "video"] as const) {
    parentBundle.tasks[type] = { ...parentBundle.tasks[type], status: "failed" };
  }
  resetStore({
    bundle: conversation("bundle", { paper_bundle: parentBundle }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState("bundle", "poster"),
      messages: [{ id: "placeholder", role: "assistant", text: "", ts: 1, status: "streaming" }],
    }),
  }, "bundle");

  let historyFetches = 0;
  let artifactFetches = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      historyFetches += 1;
      return jsonResponse({ conversations: {}, imported_runs: 0, user_isolated: false });
    }
    if (url === "/api/runs/run_recover/artifact") {
      artifactFetches += 1;
      return jsonResponse(responseForRun("run_recover", "poster"));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await Promise.all([
    useApp.getState().loadServerHistory(),
    useApp.getState().loadServerHistory(),
  ]);
  await waitFor(
    () => useApp.getState().conversations.bundle.paper_bundle?.kind === "parent"
      && useApp.getState().conversations.bundle.paper_bundle.tasks.poster.status === "complete",
    "persisted bundle run was not recovered",
  );

  const parent = useApp.getState().conversations.bundle;
  assert.equal(parent.pending, false);
  assert.equal(parent.artifacts.art_run_recover.artifact_type, "poster");
  assert.equal(historyFetches, 1);
  assert.equal(artifactFetches, 1);
  assert.equal(MockEventSource.instances.length, 1);
});

test("SSE disconnect does not start the post-terminal artifact budget", async () => {
  const parentBundle = createPaperBundleParentState("bundle", "paper.pdf");
  const childId = parentBundle.tasks.poster.child_conversation_id;
  parentBundle.tasks.poster = {
    ...parentBundle.tasks.poster,
    status: "running",
    run_id: "run_disconnect_recovery",
  };
  for (const type of ["deck", "landing", "video"] as const) {
    parentBundle.tasks[type] = { ...parentBundle.tasks[type], status: "failed" };
  }
  resetStore({
    bundle: conversation("bundle", { paper_bundle: parentBundle }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState("bundle", "poster"),
      messages: [{ id: "placeholder", role: "assistant", text: "", ts: 1, status: "streaming" }],
    }),
  }, "bundle");

  let artifactFetches = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({ conversations: {}, imported_runs: 0, user_isolated: false });
    }
    if (url === "/api/runs/run_disconnect_recovery/artifact") {
      artifactFetches += 1;
      return artifactFetches <= 8
        ? jsonResponse({ detail: "artifact pending" }, 504)
        : jsonResponse(responseForRun("run_disconnect_recovery", "poster"));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = ((handler: TimerHandler, _timeout?: number, ...args: unknown[]) =>
    realSetTimeout(handler, 0, ...args)
  ) as typeof setTimeout;
  try {
    await useApp.getState().loadServerHistory();
    await waitFor(() => MockEventSource.instances.length === 1, "recovery SSE did not start");
    MockEventSource.instances[0].readyState = MockEventSource.CLOSED;
    MockEventSource.instances[0].onerror?.();
    await waitFor(
      () => useApp.getState().conversations.bundle.paper_bundle?.kind === "parent"
        && useApp.getState().conversations.bundle.paper_bundle.tasks.poster.status === "complete",
      "artifact recovery stopped after the SSE disconnected",
    );
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }

  assert.equal(artifactFetches, 9);
});

test("Paper All-in-One recovery stops after confirmed permanent status failures", async () => {
  const parentBundle = createPaperBundleParentState("bundle", "paper.pdf");
  const childId = parentBundle.tasks.poster.child_conversation_id;
  const runId = "run_missing_recovery";
  parentBundle.tasks.poster = {
    ...parentBundle.tasks.poster,
    status: "running",
    run_id: runId,
  };
  for (const artifactType of ["deck", "landing", "video"] as const) {
    parentBundle.tasks[artifactType] = {
      ...parentBundle.tasks[artifactType],
      status: "failed",
    };
  }
  resetStore({
    bundle: conversation("bundle", { paper_bundle: parentBundle, pending: true }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState("bundle", "poster"),
      messages: [{
        id: "placeholder",
        role: "assistant",
        text: "",
        ts: 1,
        status: "streaming",
        run_id: runId,
        task_type: "generate",
        task_payload: { artifact_type: "poster" },
      }],
      pending: true,
      run_id: runId,
    }),
  }, "bundle");

  let statusReads = 0;
  let artifactReads = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({ conversations: {}, imported_runs: 0, user_isolated: false });
    }
    if (url === `/api/runs/${runId}/status`) {
      statusReads += 1;
      return jsonResponse({ detail: "missing run" }, 404);
    }
    if (url === `/api/runs/${runId}/artifact`) {
      artifactReads += 1;
      return jsonResponse({ detail: "artifact pending" }, 504);
    }
    if (url === `/api/runs/${runId}/cancel`) {
      return confirmedCancellation(runId);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) =>
    realSetTimeout(handler, timeout && timeout <= 5_000 ? 0 : timeout, ...args)
  ) as typeof setTimeout;
  try {
    await useApp.getState().loadServerHistory();
    await waitFor(() => MockEventSource.instances.length === 1, "recovery SSE did not start");
    MockEventSource.instances[0].readyState = MockEventSource.CLOSED;
    MockEventSource.instances[0].onerror?.();
    await waitFor(() => statusReads === 3, "permanent status failures were not confirmed");
    await waitFor(
      () => {
        const bundle = useApp.getState().conversations.bundle.paper_bundle;
        return bundle?.kind === "parent" && bundle.tasks.poster.status === "failed";
      },
      "permanent status failure did not settle the recovered task",
    );
    const failure = useApp.getState().conversations[childId].messages.at(-1)?.failure;
    assert.equal(failure?.status, "run_status_unavailable");
    assert.equal(failure?.run_id, runId);
    assert.ok(artifactReads > 0);
    assert.ok(artifactReads < 100);
  } finally {
    const bundle = useApp.getState().conversations.bundle.paper_bundle;
    if (bundle?.kind === "parent" && bundle.tasks.poster.status === "running") {
      await useApp.getState().cancelPaperBundleTask("bundle", "poster");
    }
    globalThis.setTimeout = realSetTimeout;
  }
});

test("Paper All-in-One reconciles every durable nonterminal state and preserves its Canvas draft", async () => {
  const originalSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => (
    originalSetTimeout(handler, timeout && timeout <= 5_000 ? 0 : timeout, ...args)
  )) as typeof setTimeout;
  const parentBundle = createPaperBundleParentState("bundle", "paper.pdf");
  const childId = parentBundle.tasks.poster.child_conversation_id;
  const runId = "run_bundle_reconcile";
  parentBundle.tasks.poster = {
    ...parentBundle.tasks.poster,
    status: "running",
    run_id: runId,
  };
  for (const artifactType of ["deck", "landing", "video"] as const) {
    parentBundle.tasks[artifactType] = {
      ...parentBundle.tasks[artifactType],
      status: "failed",
    };
  }
  const draft = {
    ...artifact("bundle_reconcile_draft", "poster"),
    candidate_draft: true,
    attempt_lineage: {
      source_run_id: runId,
      source_attempt: 1,
      source_candidate_id: "poster-attempt-01",
    },
  };
  resetStore({
    bundle: conversation("bundle", { paper_bundle: parentBundle, pending: true }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState("bundle", "poster"),
      messages: [{
        id: "placeholder",
        role: "assistant",
        text: "",
        ts: 1,
        status: "streaming",
        run_id: runId,
      }],
      artifacts: { [draft.artifact_id]: draft },
      active_artifact_id: draft.artifact_id,
      pending: true,
      run_id: runId,
    }),
  }, "bundle");

  const durableStates = [
    "reserved",
    "uploading",
    "queued",
    "running",
    "completing",
    "cancelling",
  ] as const;
  let statusReads = 0;
  let artifactAvailable = false;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({ conversations: {}, imported_runs: 0, user_isolated: false });
    }
    if (url === `/api/runs/${runId}/status`) {
      const runState = durableStates[statusReads];
      statusReads += 1;
      return jsonResponse({
        run_id: runId,
        run_state: runState,
        revision: statusReads,
        publishable: false,
        cancellation_pending: runState === "cancelling" ? "worker_exit_pending" : null,
        worker_pid: null,
        terminal_event: null,
      });
    }
    if (url === `/api/runs/${runId}/artifact`) {
      return artifactAvailable
        ? jsonResponse(responseForRun(runId, "poster"))
        : jsonResponse({ detail: "artifact pending" }, 504);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().loadServerHistory();
  await waitFor(() => MockEventSource.instances.length === 1, "bundle recovery SSE did not start");
  try {
    for (const [index, durableState] of durableStates.entries()) {
      const source = MockEventSource.instances[index];
      source.readyState = MockEventSource.CLOSED;
      source.onerror?.();
      await waitFor(
        () => statusReads === index + 1 && MockEventSource.instances.length === index + 2,
        `bundle run did not reconnect from ${durableState}`,
      );
      const state = useApp.getState();
      assert.equal(state.conversations[childId].pending, true);
      assert.equal(state.conversations[childId].run_id, runId);
      assert.equal(state.conversations[childId].active_artifact_id, draft.artifact_id);
      const progress = state.runs_progress[childId];
      if (durableState === "cancelling") assert.equal(progress.phase, "cancelling");
      else assert.notEqual(progress.phase, "error");
    }
    artifactAvailable = true;
    MockEventSource.instances.at(-1)?.emit("run.done", { event_id: "bundle-terminal" });
    await waitFor(
      () => useApp.getState().conversations.bundle.paper_bundle?.kind === "parent"
        && useApp.getState().conversations.bundle.paper_bundle.tasks.poster.status === "complete",
      "bundle completion did not win the cancelling transport race",
    );
    assert.equal(useApp.getState().conversations[childId].active_artifact_id, draft.artifact_id);
  } finally {
    artifactAvailable = true;
    MockEventSource.instances.at(-1)?.emit("run.done", { event_id: "bundle-cleanup" });
    globalThis.setTimeout = originalSetTimeout;
  }
});

test("late bundle completion preserves an existing valid parent active artifact", async () => {
  const parentBundle = createPaperBundleParentState("bundle", "paper.pdf");
  const deckId = parentBundle.tasks.deck.child_conversation_id;
  parentBundle.tasks.poster = {
    ...parentBundle.tasks.poster,
    status: "complete",
    run_id: "run_poster",
    artifact_id: "art_poster",
  };
  parentBundle.tasks.deck = {
    ...parentBundle.tasks.deck,
    status: "running",
    run_id: "run_late_deck",
  };
  for (const type of ["landing", "video"] as const) {
    parentBundle.tasks[type] = { ...parentBundle.tasks[type], status: "failed" };
  }
  const existingArtifact = artifact("manually-selected", "landing");
  const posterArtifact = artifact("poster", "poster");
  resetStore({
    bundle: conversation("bundle", {
      paper_bundle: parentBundle,
      artifacts: {
        [existingArtifact.artifact_id]: existingArtifact,
        [posterArtifact.artifact_id]: posterArtifact,
      },
      active_artifact_id: existingArtifact.artifact_id,
    }),
    [deckId]: conversation(deckId, {
      paper_bundle: createPaperBundleChildState("bundle", "deck"),
      messages: [{ id: "placeholder", role: "assistant", text: "", ts: 1, status: "streaming" }],
    }),
  }, "bundle");
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({ conversations: {}, imported_runs: 0, user_isolated: false });
    }
    if (url === "/api/runs/run_late_deck/artifact") {
      return jsonResponse(responseForRun("run_late_deck", "deck"));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().loadServerHistory();
  await waitFor(
    () => useApp.getState().conversations.bundle.paper_bundle?.kind === "parent"
      && useApp.getState().conversations.bundle.paper_bundle.tasks.deck.status === "complete",
    "late deck run was not recovered",
  );

  assert.equal(
    useApp.getState().conversations.bundle.active_artifact_id,
    existingArtifact.artifact_id,
  );
});

test("rehydration recovers run-backed tasks and interrupts pre-ACK tasks", async () => {
  resetStore({ base: conversation("base") }, "base");
  const parentBundle = createPaperBundleParentState("bundle", "paper.pdf");
  const posterId = parentBundle.tasks.poster.child_conversation_id;
  const deckId = parentBundle.tasks.deck.child_conversation_id;
  parentBundle.tasks.poster = {
    ...parentBundle.tasks.poster,
    status: "running",
    run_id: "run_hydrate",
  };
  for (const type of ["landing", "video"] as const) {
    parentBundle.tasks[type] = { ...parentBundle.tasks[type], status: "failed" };
  }
  const persistedConversations = {
    bundle: conversation("bundle", { paper_bundle: parentBundle }),
    [posterId]: conversation(posterId, {
      paper_bundle: createPaperBundleChildState("bundle", "poster"),
      messages: [{ id: "poster-placeholder", role: "assistant" as const, text: "", ts: 1, status: "streaming" as const }],
    }),
    [deckId]: conversation(deckId, {
      paper_bundle: createPaperBundleChildState("bundle", "deck"),
      messages: [{ id: "deck-placeholder", role: "assistant" as const, text: "", ts: 1, status: "streaming" as const }],
    }),
  };
  localStorage.setItem("autodesign.web.v1", JSON.stringify({
    version: 1,
    state: {
      conversations: persistedConversations,
      current_conversation_id: "bundle",
    },
  }));
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/runs/run_hydrate/artifact") {
      return jsonResponse(responseForRun("run_hydrate", "poster"));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.persist.rehydrate();
  await waitFor(
    () => useApp.getState().conversations.bundle.paper_bundle?.kind === "parent"
      && useApp.getState().conversations.bundle.paper_bundle.tasks.poster.status === "complete",
    "rehydrated run was not recovered",
  );

  const bundle = useApp.getState().conversations.bundle.paper_bundle;
  assert.equal(bundle?.kind, "parent");
  assert.equal((bundle as PaperBundleParentState).tasks.deck.status, "failed");
  assert.match((bundle as PaperBundleParentState).tasks.deck.error ?? "", /Interrupted/);
  assert.equal(useApp.getState().conversations.bundle.pending, false);
});

test("Cancel All during parent reservation re-drives cancellation after acknowledgement", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  const uploadSignals: Array<AbortSignal | null> = [];
  const generateBodies: FormData[] = [];
  const releaseGenerate: Array<(response: Response) => void> = [];
  let cancelPosts = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        needs_setup: false,
      });
    }
    if (url === "/api/generate") {
      generateBodies.push(init?.body as FormData);
      uploadSignals.push(init?.signal ?? null);
      return new Promise<Response>((resolve) => {
        releaseGenerate.push(resolve);
      });
    }
    if (/^\/api\/runs\/run_\d+\/cancel$/.test(url)) {
      cancelPosts += 1;
      return jsonResponse({ cancelled: true });
    }
    if (/^\/api\/runs\/run_\d+\/artifact$/.test(url)) {
      return jsonResponse({
        message: {
          id: `msg_${url}`,
          role: "assistant",
          text: "Run cancelled.",
          ts: 2,
          status: "error",
          failure: { status: "cancelled", produced_files: [] },
        },
        artifact: null,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await waitFor(() => releaseGenerate.length === 4, "bundle uploads did not start");
  await useApp.getState().cancelPaperBundle("bundle");
  releaseGenerate.forEach((resolve, index) => resolve(jsonResponse({
    run_id: `run_${index}`,
    placeholder_message: { id: `server_${index}`, role: "assistant", text: "", ts: 1 },
  })));
  await start;

  assert.ok(uploadSignals.every((signal) => !signal?.aborted));
  assert.equal(cancelPosts, 4);
  for (const body of generateBodies) {
    assert.equal(body.has("conversation_history"), false);
    assert.equal(body.has("prior_artifacts"), false);
  }
  const bundle = useApp.getState().conversations.bundle.paper_bundle;
  assert.equal(bundle?.kind, "parent");
  assert.ok(Object.values((bundle as PaperBundleParentState).tasks)
    .every((task) => task.status === "cancelled"));
  assert.equal((bundle as PaperBundleParentState).backend_state, "cancelled");
  assert.equal(useApp.getState().conversations.bundle.pending, false);
});

test("Paper All-in-One stays attached to its initiating parent across health await", async () => {
  resetStore({
    initiating: conversation("initiating"),
    later: conversation("later"),
  }, "initiating");
  let releaseHealth!: (response: Response) => void;
  let nextRun = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/health") {
      return new Promise<Response>((resolve) => { releaseHealth = resolve; });
    }
    if (url === "/api/generate") {
      assert.equal(init?.signal, undefined);
      const runId = `run_parent_${nextRun}`;
      nextRun += 1;
      return jsonResponse({
        run_id: runId,
        placeholder_message: { id: `msg_${runId}`, role: "assistant", text: "", ts: 1 },
      });
    }
    if (/^\/api\/runs\/run_parent_\d+\/cancel$/.test(url)) {
      return jsonResponse({ cancelled: true });
    }
    if (/^\/api\/runs\/run_parent_\d+\/artifact$/.test(url)) {
      return jsonResponse({
        message: {
          id: `msg_${url}`,
          role: "assistant",
          text: "Run cancelled.",
          ts: 2,
          status: "error",
          failure: { status: "cancelled", produced_files: [] },
        },
        artifact: null,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await tick();
  useApp.setState({ current_conversation_id: "later" });
  releaseHealth(jsonResponse({
    designer_model: "test",
    image_model: "test",
    models: { designer: "test", image: "test" },
    demo_mode: false,
    needs_setup: false,
  }));
  await waitFor(() => MockEventSource.instances.length === 4, "bundle runs did not start");

  const state = useApp.getState();
  const ownerId = state.conversations.initiating.paper_bundle?.kind === "parent"
    ? "initiating"
    : "later";
  try {
    assert.equal(state.conversations.initiating.paper_bundle?.kind, "parent");
    assert.equal(state.conversations.later.paper_bundle, undefined);
  } finally {
    await useApp.getState().cancelPaperBundle(ownerId);
    for (const source of MockEventSource.instances) source.emit("run.cancelled");
    await start;
  }
});

test("artifact plus failure completes a bundle task without a user-facing warning", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  const artifactTypes: ArtifactType[] = ["poster", "deck", "landing", "video"];
  let nextRun = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        needs_setup: false,
      });
    }
    if (url === "/api/generate") {
      const artifactType = artifactTypes[nextRun];
      nextRun += 1;
      return jsonResponse({
        run_id: `run_${artifactType}`,
        placeholder_message: { id: `msg_${artifactType}`, role: "assistant", text: "", ts: 1 },
      });
    }
    const match = url.match(/^\/api\/runs\/run_(poster|deck|landing|video)\/artifact$/);
    if (match) {
      const artifactType = match[1] as ArtifactType;
      const failure = artifactType === "poster"
        ? {
            status: "quality_degraded",
            agent_last_note: "Critic score remained below the quality gate.",
            produced_files: ["final/poster.html"],
          }
        : undefined;
      return jsonResponse(responseForRun(`run_${artifactType}`, artifactType, failure));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await waitFor(() => MockEventSource.instances.length === 4, "bundle SSE streams did not start");
  for (const source of MockEventSource.instances) source.emit("run.done");
  await start;

  const bundle = useApp.getState().conversations.bundle.paper_bundle;
  assert.equal(bundle?.kind, "parent");
  assert.equal((bundle as PaperBundleParentState).tasks.poster.status, "complete");
  assert.equal((bundle as PaperBundleParentState).tasks.poster.error, undefined);
});

test("Paper All-in-One does not turn a one-hour browser deadline into a run failure", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  const originalSetTimeout = window.setTimeout.bind(window);
  const oneHourTimers: number[] = [];
  window.setTimeout = ((
    handler: TimerHandler,
    timeout?: number,
    ...args: unknown[]
  ) => {
    if (timeout === 60 * 60 * 1000) {
      oneHourTimers.push(timeout);
      return 987_654;
    }
    return originalSetTimeout(handler, timeout, ...args);
  }) as typeof window.setTimeout;

  let nextRun = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        needs_setup: false,
      });
    }
    if (url === "/api/generate") {
      const runId = `run_no_deadline_${nextRun}`;
      nextRun += 1;
      return jsonResponse({
        run_id: runId,
        placeholder_message: { id: `msg_${runId}`, role: "assistant", text: "", ts: 1 },
      });
    }
    if (/^\/api\/runs\/run_no_deadline_\d+\/cancel$/.test(url)) {
      return jsonResponse({ cancelled: true });
    }
    if (/^\/api\/runs\/run_no_deadline_\d+\/artifact$/.test(url)) {
      return jsonResponse({
        message: {
          id: `msg_${url}`,
          role: "assistant",
          text: "Run cancelled.",
          ts: 2,
          status: "error",
          failure: { status: "cancelled", produced_files: [] },
        },
        artifact: null,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  try {
    await waitFor(() => MockEventSource.instances.length === 4, "bundle SSE streams did not start");
    assert.equal(oneHourTimers.length, 0);
  } finally {
    window.setTimeout = originalSetTimeout as typeof window.setTimeout;
    await useApp.getState().cancelPaperBundle("bundle");
    for (const source of MockEventSource.instances) source.emit("run.cancelled");
    await start;
  }
});

test("Paper All-in-One lost start response stays attached through durable reconciliation", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  const artifactTypes: ArtifactType[] = ["poster", "deck", "landing", "video"];
  let nextRun = 0;
  let posterStatusReads = 0;
  const originalSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => (
    originalSetTimeout(handler, timeout && timeout <= 5_000 ? 0 : timeout, ...args)
  )) as typeof setTimeout;
  paperBundleStartResponseOverride = (runId) => runId === "run_lost_start_poster"
    ? Promise.reject(new TypeError("start response was lost"))
    : null;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        needs_setup: false,
      });
    }
    if (url === "/api/generate") {
      const artifactType = artifactTypes[nextRun];
      nextRun += 1;
      const runId = artifactType === "poster"
        ? "run_lost_start_poster"
        : `run_lost_start_${artifactType}`;
      return jsonResponse({
        run_id: runId,
        placeholder_message: { id: `msg_${artifactType}`, role: "assistant", text: "", ts: 1 },
      });
    }
    if (url === "/api/runs/run_lost_start_poster/status") {
      posterStatusReads += 1;
      return jsonResponse({
        run_id: "run_lost_start_poster",
        run_state: "running",
        revision: 2,
        publishable: false,
        cancellation_pending: null,
        worker_pid: null,
        terminal_event: null,
      });
    }
    const artifactMatch = url.match(
      /^\/api\/runs\/run_lost_start_(poster|deck|landing|video)\/artifact$/,
    );
    if (artifactMatch) {
      const artifactType = artifactMatch[1] as ArtifactType;
      return jsonResponse(responseForRun(`run_lost_start_${artifactType}`, artifactType));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  try {
    await waitFor(
      () => posterStatusReads === 1 && MockEventSource.instances.length === 5,
      "lost Paper Bundle /start response did not reconcile durable status",
    );
    const inFlight = useApp.getState();
    const bundle = inFlight.conversations.bundle.paper_bundle as PaperBundleParentState;
    const poster = bundle.tasks.poster;
    assert.notEqual(poster.status, "failed");
    assert.equal(poster.run_id, "run_lost_start_poster");
    assert.equal(inFlight.conversations[poster.child_conversation_id].run_id, poster.run_id);

    for (const source of MockEventSource.instances) {
      if (source.readyState !== MockEventSource.CLOSED) source.emit("run.done");
    }
    await start;
    const completed = useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState;
    assert.equal(completed.tasks.poster.status, "complete");
  } finally {
    for (const source of MockEventSource.instances) source.emit("run.done");
    await start;
    globalThis.setTimeout = originalSetTimeout;
  }
});

test("Paper All-in-One retries a transient terminal artifact race", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  const artifactTypes: ArtifactType[] = ["poster", "deck", "landing", "video"];
  const artifactReads = new Map<ArtifactType, number>();
  let nextRun = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        needs_setup: false,
      });
    }
    if (url === "/api/generate") {
      const artifactType = artifactTypes[nextRun];
      nextRun += 1;
      return jsonResponse({
        run_id: `run_retry_${artifactType}`,
        placeholder_message: { id: `msg_${artifactType}`, role: "assistant", text: "", ts: 1 },
      });
    }
    const match = url.match(/^\/api\/runs\/run_retry_(poster|deck|landing|video)\/artifact$/);
    if (match) {
      const artifactType = match[1] as ArtifactType;
      const reads = (artifactReads.get(artifactType) ?? 0) + 1;
      artifactReads.set(artifactType, reads);
      if (artifactType === "poster" && reads === 1) {
        return jsonResponse({ detail: "run still in progress; retry shortly" }, 504);
      }
      return jsonResponse(responseForRun(`run_retry_${artifactType}`, artifactType));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await waitFor(() => MockEventSource.instances.length === 4, "bundle SSE streams did not start");
  for (const source of MockEventSource.instances) source.emit("run.done");
  await start;

  const bundle = useApp.getState().conversations.bundle.paper_bundle;
  assert.equal(bundle?.kind, "parent");
  assert.equal(artifactReads.get("poster"), 2);
  assert.equal((bundle as PaperBundleParentState).tasks.poster.status, "complete");
  assert.ok(Object.values((bundle as PaperBundleParentState).tasks)
    .every((task) => task.status === "complete"));
});

test("Paper All-in-One persists terminal elapsed time and author attempts", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  const artifactTypes: ArtifactType[] = ["poster", "deck", "landing", "video"];
  let nextRun = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        needs_setup: false,
      });
    }
    if (url === "/api/generate") {
      const artifactType = artifactTypes[nextRun];
      nextRun += 1;
      return jsonResponse({
        run_id: `run_stats_${artifactType}`,
        placeholder_message: { id: `msg_${artifactType}`, role: "assistant", text: "", ts: 1 },
      });
    }
    const match = url.match(/^\/api\/runs\/run_stats_(poster|deck|landing|video)\/artifact$/);
    if (match) {
      const artifactType = match[1] as ArtifactType;
      return jsonResponse(responseForRun(`run_stats_${artifactType}`, artifactType));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await waitFor(() => MockEventSource.instances.length === 4, "bundle SSE streams did not start");
  for (const source of MockEventSource.instances) {
    source.emit("slides_author.attempt_start", { attempt: 3, max_attempts: 12 });
    source.emit("run.done");
  }
  await start;

  const bundle = useApp.getState().conversations.bundle.paper_bundle;
  assert.equal(bundle?.kind, "parent");
  for (const task of Object.values((bundle as PaperBundleParentState).tasks)) {
    assert.equal(task.status, "complete");
    assert.equal(task.attempts, 3);
    assert.equal(task.max_attempts, 12);
    assert.equal(typeof task.started_at, "number");
    assert.equal(typeof task.finished_at, "number");
    assert.ok((task.finished_at ?? 0) >= (task.started_at ?? 0));
  }
});

test("Paper All-in-One rehydration preserves terminal timing and attempts", async () => {
  resetStore({ base: conversation("base") }, "base");
  const parentBundle = createPaperBundleParentState("bundle", "paper.pdf");
  for (const [index, artifactType] of PAPER_BUNDLE_ARTIFACT_ORDER.entries()) {
    parentBundle.tasks[artifactType] = {
      ...parentBundle.tasks[artifactType],
      status: "complete",
      started_at: 1_000 + index,
      finished_at: 2_000 + index,
      attempts: index + 1,
      max_attempts: 12,
    };
  }
  localStorage.setItem("autodesign.web.v1", JSON.stringify({
    version: 1,
    state: {
      conversations: {
        bundle: conversation("bundle", {
          paper_bundle: parentBundle,
          pending: false,
        }),
      },
      current_conversation_id: "bundle",
      history_user_scope: "test-user",
    },
  }));

  await useApp.persist.rehydrate();

  const bundle = useApp.getState().conversations.bundle.paper_bundle;
  assert.equal(bundle?.kind, "parent");
  for (const [index, artifactType] of PAPER_BUNDLE_ARTIFACT_ORDER.entries()) {
    const task = (bundle as PaperBundleParentState).tasks[artifactType];
    assert.equal(task.started_at, 1_000 + index);
    assert.equal(task.finished_at, 2_000 + index);
    assert.equal(task.attempts, index + 1);
    assert.equal(task.max_attempts, 12);
  }
});

test("Paper All-in-One bounds unavailable artifact retries after terminal", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  const artifactTypes: ArtifactType[] = ["poster", "deck", "landing", "video"];
  const artifactReads = new Map<ArtifactType, number>();
  let nextRun = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        needs_setup: false,
      });
    }
    if (url === "/api/generate") {
      const artifactType = artifactTypes[nextRun];
      nextRun += 1;
      return jsonResponse({
        run_id: `run_unavailable_${artifactType}`,
        placeholder_message: { id: `msg_${artifactType}`, role: "assistant", text: "", ts: 1 },
      });
    }
    const match = url.match(
      /^\/api\/runs\/run_unavailable_(poster|deck|landing|video)\/artifact$/,
    );
    if (match) {
      const artifactType = match[1] as ArtifactType;
      artifactReads.set(artifactType, (artifactReads.get(artifactType) ?? 0) + 1);
      if (artifactType === "poster") {
        return jsonResponse({ detail: "artifact unavailable" }, 504);
      }
      return jsonResponse(responseForRun(`run_unavailable_${artifactType}`, artifactType));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await waitFor(() => MockEventSource.instances.length === 4, "bundle SSE streams did not start");
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) =>
    realSetTimeout(handler, timeout === 60_000 ? 25 : 1, ...args)
  ) as typeof setTimeout;
  try {
    for (const source of MockEventSource.instances) source.emit("run.done");
    await start;
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }

  const bundle = useApp.getState().conversations.bundle.paper_bundle;
  assert.equal(bundle?.kind, "parent");
  const poster = (bundle as PaperBundleParentState).tasks.poster;
  assert.equal(poster.status, "failed");
  assert.match(poster.error ?? "", /artifact unavailable/i);
  const posterChild = useApp.getState().conversations[poster.child_conversation_id];
  const posterFailure = posterChild.messages.at(-1);
  assert.equal(posterFailure?.failure?.status, "artifact_delivery_failed");
  assert.equal(posterFailure?.failure?.run_id, "run_unavailable_poster");
  assert.equal(posterFailure?.run_id, "run_unavailable_poster");
  assert.ok((artifactReads.get("poster") ?? 0) > 1);
  assert.ok((artifactReads.get("poster") ?? 0) < 100);
  for (const artifactType of ["deck", "landing", "video"] as const) {
    assert.equal((bundle as PaperBundleParentState).tasks[artifactType].status, "complete");
  }
});

test("Paper All-in-One retries artifact delivery on the same terminal run", async () => {
  const parentBundle = createPaperBundleParentState("bundle", "paper.pdf");
  parentBundle.job_id = "job_artifact_delivery";
  parentBundle.backend_state = "partial";
  const posterRunId = "run_bundle_artifact_delivery";
  const posterChildId = parentBundle.tasks.poster.child_conversation_id;
  parentBundle.tasks.poster = {
    ...parentBundle.tasks.poster,
    status: "failed",
    run_id: posterRunId,
    terminal: true,
    process_free: true,
    error: "The artifact could not be delivered.",
  };
  for (const artifactType of ["deck", "landing", "video"] as const) {
    parentBundle.tasks[artifactType] = {
      ...parentBundle.tasks[artifactType],
      status: "failed",
      terminal: true,
      process_free: true,
    };
  }
  resetStore({
    bundle: conversation("bundle", { paper_bundle: parentBundle }),
    [posterChildId]: conversation(posterChildId, {
      paper_bundle: createPaperBundleChildState("bundle", "poster"),
      messages: [{
        id: "bundle-artifact-user",
        role: "user",
        text: "Create the paper poster.",
        ts: 1,
        status: "done",
        task_type: "generate",
        task_payload: { artifact_type: "poster" },
      }, {
        id: "bundle-artifact-failure",
        role: "assistant",
        text: "The artifact could not be delivered.",
        ts: 2,
        run_id: posterRunId,
        status: "error",
        task_type: "generate",
        task_payload: { artifact_type: "poster" },
        failure: {
          status: "artifact_delivery_failed",
          run_id: posterRunId,
          phase: "artifact_delivery",
          produced_files: [],
          artifact_type: "poster",
        },
      }],
    }),
  }, "bundle");
  let artifactReads = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${posterRunId}/artifact`) {
      artifactReads += 1;
      return jsonResponse(responseForRun(posterRunId, "poster"));
    }
    throw new Error(`same-run artifact recovery must not call ${url}`);
  }) as typeof fetch;

  await useApp.getState().retryPaperBundleTask("bundle", "poster");

  const recovered = useApp.getState();
  const bundle = recovered.conversations.bundle.paper_bundle as PaperBundleParentState;
  assert.equal(artifactReads, 1);
  assert.equal(bundle.tasks.poster.run_id, posterRunId);
  assert.equal(bundle.tasks.poster.status, "complete");
  assert.equal(bundle.tasks.poster.artifact_id, `art_${posterRunId}`);
  assert.equal(recovered.conversations[posterChildId].active_artifact_id, `art_${posterRunId}`);
});

test("Paper All-in-One keeps retrying fast failures throughout the terminal grace", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  const artifactTypes: ArtifactType[] = ["poster", "deck", "landing", "video"];
  let posterArtifactReads = 0;
  let nextRun = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        needs_setup: false,
      });
    }
    if (url === "/api/generate") {
      const artifactType = artifactTypes[nextRun];
      nextRun += 1;
      return jsonResponse({
        run_id: `run_late_${artifactType}`,
        placeholder_message: { id: `msg_${artifactType}`, role: "assistant", text: "", ts: 1 },
      });
    }
    const match = url.match(/^\/api\/runs\/run_late_(poster|deck|landing|video)\/artifact$/);
    if (match) {
      const artifactType = match[1] as ArtifactType;
      if (artifactType === "poster") {
        posterArtifactReads += 1;
        if (posterArtifactReads <= 8) {
          return jsonResponse({ detail: "artifact pending" }, 504);
        }
      }
      return jsonResponse(responseForRun(`run_late_${artifactType}`, artifactType));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await waitFor(() => MockEventSource.instances.length === 4, "bundle SSE streams did not start");
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) =>
    realSetTimeout(handler, typeof timeout === "number" && timeout >= 59_000 ? 1000 : 0, ...args)
  ) as typeof setTimeout;
  try {
    for (const source of MockEventSource.instances) source.emit("run.done");
    await start;
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }

  const bundle = useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState;
  assert.equal(bundle.tasks.poster.status, "complete");
  assert.equal(posterArtifactReads, 9);
});

test("Paper All-in-One bounds a hanging artifact read by the post-terminal grace", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  const artifactTypes: ArtifactType[] = ["poster", "deck", "landing", "video"];
  const artifactTimeouts: number[] = [];
  let nextRun = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        needs_setup: false,
      });
    }
    if (url === "/api/generate") {
      const artifactType = artifactTypes[nextRun];
      nextRun += 1;
      return jsonResponse({
        run_id: `run_hanging_${artifactType}`,
        placeholder_message: { id: `msg_${artifactType}`, role: "assistant", text: "", ts: 1 },
      });
    }
    if (url === "/api/runs/run_hanging_poster/artifact") {
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener(
          "abort",
          () => reject(init.signal?.reason ?? new Error("aborted")),
          { once: true },
        );
      });
    }
    const match = url.match(/^\/api\/runs\/run_hanging_(deck|landing|video)\/artifact$/);
    if (match) {
      const artifactType = match[1] as ArtifactType;
      return jsonResponse(responseForRun(`run_hanging_${artifactType}`, artifactType));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await waitFor(() => MockEventSource.instances.length === 4, "bundle SSE streams did not start");
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    if (typeof timeout === "number" && timeout > 0) artifactTimeouts.push(timeout);
    return realSetTimeout(handler, 0, ...args);
  }) as typeof setTimeout;
  try {
    for (const source of MockEventSource.instances) source.emit("run.done");
    await start;
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }

  const bundle = useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState;
  assert.equal(bundle.tasks.poster.status, "failed");
  assert.ok(artifactTimeouts.length > 0);
  assert.ok(Math.max(...artifactTimeouts) <= 60_000);
  for (const artifactType of ["deck", "landing", "video"] as const) {
    assert.equal(bundle.tasks[artifactType].status, "complete");
  }
});

test("cancelling a Paper All-in-One task aborts its active artifact read", async () => {
  resetStore({ bundle: conversation("bundle") }, "bundle");
  const artifactTypes: ArtifactType[] = ["poster", "deck", "landing", "video"];
  let nextRun = 0;
  let posterArtifactSignal: AbortSignal | undefined;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/health") {
      return jsonResponse({
        designer_model: "test",
        image_model: "test",
        models: { designer: "test", image: "test" },
        demo_mode: false,
        needs_setup: false,
      });
    }
    if (url === "/api/generate") {
      const artifactType = artifactTypes[nextRun];
      nextRun += 1;
      return jsonResponse({
        run_id: `run_abort_${artifactType}`,
        placeholder_message: { id: `msg_${artifactType}`, role: "assistant", text: "", ts: 1 },
      });
    }
    if (url === "/api/runs/run_abort_poster/artifact") {
      posterArtifactSignal = init?.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        posterArtifactSignal?.addEventListener(
          "abort",
          () => reject(posterArtifactSignal?.reason ?? new Error("aborted")),
          { once: true },
        );
      });
    }
    const match = url.match(/^\/api\/runs\/run_abort_(deck|landing|video)\/artifact$/);
    if (match) {
      const artifactType = match[1] as ArtifactType;
      return jsonResponse(responseForRun(`run_abort_${artifactType}`, artifactType));
    }
    if (url === "/api/runs/run_abort_poster/cancel") {
      return jsonResponse({
        run_id: "run_abort_poster",
        status: "cancelled",
        run_state: "cancelled",
        confirmed: true,
        terminated_pids: [],
        surviving_pids: [],
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const start = useApp.getState().startPaperBundle(
    new File(["paper"], "paper.pdf", { type: "application/pdf" }),
  );
  await waitFor(() => MockEventSource.instances.length === 4, "bundle SSE streams did not start");
  for (const source of MockEventSource.instances) source.emit("run.done");
  await waitFor(() => !!posterArtifactSignal, "poster artifact read did not start");

  await useApp.getState().cancelPaperBundleTask("bundle", "poster");
  await start;

  assert.equal(posterArtifactSignal?.aborted, true);
  const bundle = useApp.getState().conversations.bundle.paper_bundle;
  assert.equal(bundle?.kind, "parent");
  assert.equal((bundle as PaperBundleParentState).tasks.poster.status, "cancelled");
  for (const artifactType of ["deck", "landing", "video"] as const) {
    assert.equal((bundle as PaperBundleParentState).tasks[artifactType].status, "complete");
  }
});

test("Paper All-in-One cancellation transport failure stays visibly unconfirmed", async () => {
  const parentBundle = createPaperBundleParentState("bundle", "paper.pdf");
  const posterId = parentBundle.tasks.poster.child_conversation_id;
  parentBundle.tasks.poster = {
    ...parentBundle.tasks.poster,
    status: "running",
    run_id: "run_cancel_transport",
  };
  for (const type of ["deck", "landing", "video"] as const) {
    parentBundle.tasks[type] = { ...parentBundle.tasks[type], status: "failed" };
  }
  resetStore({
    bundle: conversation("bundle", { paper_bundle: parentBundle, pending: true }),
    [posterId]: conversation(posterId, {
      paper_bundle: createPaperBundleChildState("bundle", "poster"),
      pending: true,
      run_id: "run_cancel_transport",
    }),
  }, "bundle");

  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/runs/run_cancel_transport/cancel") {
      throw new Error("network unavailable");
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().cancelPaperBundleTask("bundle", "poster");

  const task = (useApp.getState().conversations.bundle.paper_bundle as PaperBundleParentState)
    .tasks.poster;
  assert.equal(task.status, "cancelling");
  assert.equal(task.run_id, "run_cancel_transport");
  assert.match(task.error ?? "", /not confirmed/i);
  assert.equal(useApp.getState().conversations[posterId].pending, true);
});

test("cancelling one Paper All-in-One run leaves its siblings running", async () => {
  const bundle = createPaperBundleParentState("bundle", "paper.pdf");
  bundle.tasks.poster = {
    ...bundle.tasks.poster,
    status: "running",
    run_id: "run_poster",
    started_at: 1_000,
  };
  bundle.tasks.deck = {
    ...bundle.tasks.deck,
    status: "running",
    run_id: "run_deck",
    started_at: 1_000,
  };
  for (const artifactType of ["landing", "video"] as const) {
    bundle.tasks[artifactType] = {
      ...bundle.tasks[artifactType],
      status: "complete",
    };
  }
  const deckId = bundle.tasks.deck.child_conversation_id;
  resetStore({
    bundle: conversation("bundle", { paper_bundle: bundle, pending: true }),
    [deckId]: conversation(deckId, {
      paper_bundle: createPaperBundleChildState("bundle", "deck"),
      pending: true,
      run_id: "run_deck",
    }),
  }, "bundle");
  const requested: string[] = [];
  globalThis.fetch = (async (input) => {
    const url = String(input);
    requested.push(url);
    if (url === "/api/runs/run_deck/cancel") {
      return jsonResponse({
        run_id: "run_deck",
        status: "cancelled",
        run_state: "cancelled",
        confirmed: true,
        terminated_pids: [],
        surviving_pids: [],
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().cancelPaperBundleTask("bundle", "deck");

  const updated = useApp.getState().conversations.bundle.paper_bundle;
  assert.equal(updated?.kind, "parent");
  assert.equal((updated as PaperBundleParentState).tasks.deck.status, "cancelled");
  assert.equal(typeof (updated as PaperBundleParentState).tasks.deck.finished_at, "number");
  assert.equal((updated as PaperBundleParentState).tasks.poster.status, "running");
  assert.deepEqual(requested, ["/api/runs/run_deck/cancel"]);
});

test("ordinary cancel settles its owning send promise", async () => {
  resetStore({ ordinary: conversation("ordinary") }, "ordinary");
  useApp.setState({ intent_type: "landing" });
  let releaseCancel!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      return jsonResponse({
        run_id: "run_cancel",
        upload_token: "upload-token",
        input_slots: [],
        request_digest: "digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === "/api/runs/run_cancel/start") {
      return jsonResponse({
        run_id: "run_cancel",
        placeholder_message: { id: "server-placeholder", role: "assistant", text: "", ts: 1 },
      });
    }
    if (url === "/api/runs/run_cancel/cancel") {
      return new Promise<Response>((resolve) => { releaseCancel = resolve; });
    }
    if (url === "/api/runs/run_cancel/artifact") {
      return jsonResponse({
        message: {
          id: "msg_run_cancel",
          role: "assistant",
          text: "Run cancelled.",
          ts: 2,
          run_id: "run_cancel",
          status: "error",
          failure: { status: "cancelled", produced_files: [] },
        },
        artifact: null,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  let settled = false;
  const send = useApp.getState().sendMessage("make a page", [])
    .finally(() => { settled = true; });
  await waitFor(() => MockEventSource.instances.length === 1, "send SSE did not start");
  assert.equal(useApp.getState().conversations.ordinary.run_id, "run_cancel");
  const cancel = useApp.getState().cancelRun("ordinary");
  await waitFor(() => typeof releaseCancel === "function", "cancel request did not start");

  try {
    assert.equal(settled, false);
  } finally {
    releaseCancel(jsonResponse({
      run_id: "run_cancel",
      status: "cancelled",
      run_state: "cancelled",
      confirmed: true,
      terminated_pids: [],
      surviving_pids: [],
    }));
    await cancel;
    MockEventSource.instances[0].emit("run.cancelled");
    await send;
  }
  assert.equal(useApp.getState().conversations.ordinary.pending, false);
});

test("replacing an SSE wait settles the old owner without clearing the newer run", async () => {
  resetStore({ ordinary: conversation("ordinary") }, "ordinary");
  useApp.setState({ intent_type: "landing" });
  let generation = 0;
  let releaseOldStatus!: (response: Response) => void;
  let oldStatusSignal: AbortSignal | undefined;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      generation += 1;
      return jsonResponse({
        run_id: `run_${generation}`,
        upload_token: `upload-token-${generation}`,
        input_slots: [],
        request_digest: `digest-${generation}`,
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (/\/api\/runs\/run_\d+\/start$/.test(url)) {
      const runId = url.split("/").at(-2) ?? "";
      const runGeneration = Number(runId.split("_").at(-1));
      return jsonResponse({
        run_id: runId,
        placeholder_message: {
          id: `server-${runGeneration}`,
          role: "assistant",
          text: "",
          ts: runGeneration,
        },
      });
    }
    if (url === "/api/runs/run_1/status") {
      oldStatusSignal = init?.signal ?? undefined;
      return new Promise<Response>((resolve) => { releaseOldStatus = resolve; });
    }
    if (url === "/api/runs/run_2/artifact") {
      return jsonResponse(responseForRun("run_2", "landing"));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  let firstSettled = false;
  const first = useApp.getState().sendMessage("first", [])
    .finally(() => { firstSettled = true; });
  await waitFor(() => MockEventSource.instances.length === 1, "first SSE did not start");
  MockEventSource.instances[0].readyState = MockEventSource.CLOSED;
  MockEventSource.instances[0].onerror?.();
  await waitFor(() => typeof releaseOldStatus === "function", "old status request did not start");

  useApp.setState((state) => ({
    conversations: {
      ...state.conversations,
      ordinary: { ...state.conversations.ordinary, pending: false },
    },
    intent_type: "landing",
  }));
  const second = useApp.getState().sendMessage("second", []);
  await waitFor(() => MockEventSource.instances.length === 2, "replacement SSE did not start");
  await waitFor(
    () => firstSettled && useApp.getState().conversations.ordinary.run_id === "run_2",
    "replacement run did not take ownership",
  );
  releaseOldStatus(jsonResponse({
    run_id: "run_1",
    run_state: "running",
    revision: 1,
    publishable: false,
    cancellation_pending: null,
    worker_pid: null,
    terminal_event: null,
  }));
  await tick();

  try {
    assert.equal(firstSettled, true);
    assert.equal(oldStatusSignal?.aborted, true);
    assert.equal(MockEventSource.instances.length, 2);
    assert.equal(useApp.getState().conversations.ordinary.run_id, "run_2");
    assert.equal(useApp.getState().conversations.ordinary.pending, true);
  } finally {
    MockEventSource.instances[1].emit("run.done");
    MockEventSource.instances[0].emit("run.error");
    await Promise.all([first, second]);
  }
});

test("same-run recovery reattachment shares the terminal artifact read", async () => {
  resetStore({ ordinary: conversation("ordinary") }, "ordinary");
  useApp.setState({ intent_type: "landing" });
  const runId = "run_same_recovery";
  let artifactReads = 0;
  globalThis.fetch = (async (input) => {
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
    if (url === `/api/runs/${runId}/artifact`) {
      artifactReads += 1;
      return artifactReads === 1
        ? jsonResponse({ detail: "artifact commit pending" }, 504)
        : jsonResponse(responseForRun(runId, "landing"));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const sending = useApp.getState().sendMessage("make a page", []);
  await waitFor(() => MockEventSource.instances.length === 1, "source SSE did not start");
  MockEventSource.instances[0].emit("run.done", { event_id: "same-run-terminal" });
  await waitFor(() => artifactReads === 1, "first artifact read did not start retrying");

  useApp.getState().recoverActiveRuns();
  await waitFor(
    () => MockEventSource.instances.length === 2,
    "same-run recovery did not install its newer waiter",
  );
  await sending;
  await waitFor(
    () => useApp.getState().conversations.ordinary.pending === false,
    "same-run recovery did not settle",
  );

  const state = useApp.getState();
  assert.equal(artifactReads, 2);
  assert.equal(state.conversations.ordinary.active_artifact_id, `art_${runId}`);
  assert.equal(state.conversations.ordinary.messages.at(-1)?.status, "done");
  assert.equal(state.conversations.ordinary.messages.at(-1)?.failure, undefined);
});

test("lost PPTX /start acknowledgement keeps the reserved run and reconciles its download", async () => {
  const source = artifact("pptx_lost_ack_source", "poster");
  const runId = "run_pptx_lost_ack";
  resetStore({
    ordinary: conversation("ordinary", {
      artifacts: { [source.artifact_id]: source },
      active_artifact_id: source.artifact_id,
    }),
  }, "ordinary");
  let startRequests = 0;
  let statusReads = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/artifacts/export/pptx-run") {
      return jsonResponse({
        run_id: runId,
        start_token: "start-token",
        progress_mode: "artifact_export",
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      startRequests += 1;
      throw new TypeError("start acknowledgement was lost");
    }
    if (url === `/api/runs/${runId}/status`) {
      statusReads += 1;
      return jsonResponse(completedRunStatus(runId));
    }
    if (url === `/api/runs/${runId}/artifact`) {
      return jsonResponse(pptxResponseForRun(runId));
    }
    if (url === "/api/design-events") return jsonResponse({ ok: true });
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().exportArtifactPptx(source.artifact_id);

  const state = useApp.getState();
  assert.equal(startRequests, 1);
  assert.equal(statusReads, 1);
  assert.equal(state.conversations.ordinary.pending, false);
  assert.equal(state.conversations.ordinary.run_id, undefined);
  assert.equal(state.conversations.ordinary.active_artifact_id, source.artifact_id);
  assert.equal(state.conversations.ordinary.messages.at(-1)?.status, "done");
  assert.deepEqual(triggeredDownloads, [{
    url: `/api/files/runs/${runId}/final/deck.pptx`,
    filename: `${runId}.pptx`,
  }]);
});

test("queued PPTX export replays the same start after the first request is not delivered", async () => {
  const source = artifact("pptx_queued_start_source", "poster");
  const runId = "run_pptx_queued_start_replay";
  resetStore({
    ordinary: conversation("ordinary", {
      artifacts: { [source.artifact_id]: source },
      active_artifact_id: source.artifact_id,
    }),
  }, "ordinary");
  let reservationRequests = 0;
  let startRequests = 0;
  let statusReads = 0;
  let settled = false;
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  globalThis.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    if (timeout === 250 && typeof handler === "function") {
      queueMicrotask(() => handler(...args));
      return 777_003;
    }
    return originalSetTimeout(handler, timeout, ...args);
  }) as typeof setTimeout;
  globalThis.clearTimeout = ((timer?: number) => {
    if (timer !== 777_003) originalClearTimeout(timer);
  }) as typeof clearTimeout;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/artifacts/export/pptx-run") {
      reservationRequests += 1;
      return jsonResponse({
        run_id: runId,
        start_token: "same-start-token",
        progress_mode: "artifact_export",
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      startRequests += 1;
      assert.equal(init?.headers && new Headers(init.headers).get("X-Autodesign-Upload-Token"), "same-start-token");
      if (startRequests === 1) throw new TypeError("start request was not delivered");
      return jsonResponse({
        run_id: runId,
        progress_mode: "artifact_export",
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
      return jsonResponse({
        run_id: runId,
        run_state: "queued",
        revision: 1,
        publishable: false,
        cancellation_pending: null,
        worker_pid: null,
        terminal_event: null,
      });
    }
    if (url === `/api/runs/${runId}/artifact`) {
      return jsonResponse(pptxResponseForRun(runId));
    }
    if (url === "/api/design-events") return jsonResponse({ ok: true });
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const exporting = useApp.getState().exportArtifactPptx(source.artifact_id)
    .finally(() => { settled = true; });
  try {
    await waitFor(
      () => startRequests === 2 && MockEventSource.instances.length >= 2,
      "queued PPTX status did not replay the same reserved start",
    );
    assert.equal(reservationRequests, 1);
    assert.equal(statusReads, 1);
    assert.equal(useApp.getState().conversations.ordinary.run_id, runId);

    MockEventSource.instances.at(-1)?.emit("run.done", {
      event_id: "queued-pptx-terminal",
    });
    await exporting;
    assert.equal(useApp.getState().conversations.ordinary.messages.at(-1)?.status, "done");
    assert.deepEqual(triggeredDownloads, [{
      url: `/api/files/runs/${runId}/final/deck.pptx`,
      filename: `${runId}.pptx`,
    }]);
  } finally {
    if (!settled) {
      MockEventSource.instances.at(-1)?.emit("run.done", {
        event_id: "queued-pptx-cleanup",
      });
      await exporting;
    }
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
  }
});

test("lost retry /start acknowledgement reconciles the reserved retry instead of replacing it", async () => {
  const runId = "run_retry_lost_ack";
  resetStore({
    ordinary: conversation("ordinary", {
      messages: [{
        id: "retry-lost-ack-failure",
        role: "assistant",
        text: "Original run failed",
        ts: 1,
        run_id: "run_retry_lost_ack_source",
        status: "error",
        task_type: "generate",
        task_payload: { artifact_type: "landing" },
        failure: { status: "error", artifact_type: "landing", produced_files: [] },
      }],
    }),
  }, "ordinary");
  let retryReservations = 0;
  let startRequests = 0;
  let statusReads = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/runs/run_retry_lost_ack_source/retry") {
      retryReservations += 1;
      return jsonResponse({
        run_id: runId,
        start_token: "start-token",
        progress_mode: "generate",
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      startRequests += 1;
      throw new TypeError("retry start acknowledgement was lost");
    }
    if (url === `/api/runs/${runId}/status`) {
      statusReads += 1;
      return jsonResponse(completedRunStatus(runId));
    }
    if (url === `/api/runs/${runId}/artifact`) {
      return jsonResponse(responseForRun(runId, "landing"));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const resultRunId = await useApp.getState().retryRun("retry-lost-ack-failure");

  const state = useApp.getState();
  assert.equal(resultRunId, runId);
  assert.equal(retryReservations, 1);
  assert.equal(startRequests, 1);
  assert.equal(statusReads, 1);
  assert.equal(state.conversations.ordinary.pending, false);
  assert.equal(state.conversations.ordinary.run_id, undefined);
  assert.equal(state.conversations.ordinary.active_artifact_id, `art_${runId}`);
  assert.equal(state.conversations.ordinary.messages.at(-1)?.status, "done");
});

test("lost resume /start acknowledgement reconciles the reserved resume without a second generation", async () => {
  const runId = "run_resume_lost_ack";
  resetStore({
    ordinary: conversation("ordinary", {
      messages: [{
        id: "resume-lost-ack-user",
        role: "user",
        text: "Create a landing page",
        ts: 1,
        status: "done",
        task_type: "generate",
        task_payload: { artifact_type: "landing" },
      }, {
        id: "resume-lost-ack-failure",
        role: "assistant",
        text: "Connection lost",
        ts: 2,
        run_id: "run_resume_lost_ack_source",
        status: "error",
        task_type: "generate",
        task_payload: { artifact_type: "landing" },
        failure: { status: "connection_lost", artifact_type: "landing", produced_files: [] },
      }],
    }),
  }, "ordinary");
  let generationReservations = 0;
  let startRequests = 0;
  let statusReads = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/generate") {
      generationReservations += 1;
      return jsonResponse({
        run_id: runId,
        start_token: "start-token",
        progress_mode: "generate",
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      startRequests += 1;
      throw new TypeError("resume start acknowledgement was lost");
    }
    if (url === `/api/runs/${runId}/status`) {
      statusReads += 1;
      return jsonResponse(completedRunStatus(runId));
    }
    if (url === `/api/runs/${runId}/artifact`) {
      return jsonResponse(responseForRun(runId, "landing"));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().resumeRun("resume-lost-ack-failure");

  const state = useApp.getState();
  assert.equal(generationReservations, 1);
  assert.equal(startRequests, 1);
  assert.equal(statusReads, 1);
  assert.equal(state.conversations.ordinary.pending, false);
  assert.equal(state.conversations.ordinary.run_id, undefined);
  assert.equal(state.conversations.ordinary.active_artifact_id, `art_${runId}`);
  assert.equal(state.conversations.ordinary.messages.at(-1)?.status, "done");
});

test("lost video render /start acknowledgement reconciles the reserved render without duplication", async () => {
  const runId = "run_video_lost_ack";
  const source: Artifact = {
    ...artifact("video_lost_ack_source", "video"),
    video_project: {
      duration_s: 5,
      fps: 30,
      scenes: [{
        scene_id: "scene-1",
        name: "Scene 1",
        frame_layer_id: "frame-1",
        duration_s: 5,
        transition: "cut",
      }],
    },
  };
  const rendered: Artifact = {
    ...artifact(runId, "video"),
    native_file_url: `/api/files/runs/${runId}/final/video.mp4`,
  };
  resetStore({
    ordinary: conversation("ordinary", {
      artifacts: { [source.artifact_id]: source },
      active_artifact_id: source.artifact_id,
    }),
  }, "ordinary");
  let renderReservations = 0;
  let startRequests = 0;
  let statusReads = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/video/render") {
      renderReservations += 1;
      return jsonResponse({
        run_id: runId,
        start_token: "start-token",
        progress_mode: "video_render",
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      startRequests += 1;
      throw new TypeError("video start acknowledgement was lost");
    }
    if (url === `/api/runs/${runId}/status`) {
      statusReads += 1;
      return jsonResponse(completedRunStatus(runId));
    }
    if (url === `/api/runs/${runId}/artifact`) {
      return jsonResponse({
        message: {
          id: `msg_${runId}`,
          role: "assistant",
          text: "Video ready",
          ts: 2,
          run_id: runId,
          artifact_id: rendered.artifact_id,
          status: "done",
        },
        artifact: rendered,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().renderActiveVideo();

  const state = useApp.getState();
  const latestRender = state.conversations.ordinary.artifacts[source.artifact_id]
    .video_project?.latest_render;
  assert.equal(renderReservations, 1);
  assert.equal(startRequests, 1);
  assert.equal(statusReads, 1);
  assert.equal(latestRender?.run_id, runId);
  assert.equal(latestRender?.mp4_url, rendered.native_file_url);
  assert.equal(latestRender?.error, undefined);
  assert.equal(state.conversations.ordinary.run_id, undefined);
});

test("persisted PPTX export recovery uses task_type without artifact_type and retains download semantics", async () => {
  const runId = "run_pptx_persisted_recovery";
  const source = artifact("pptx_persisted_source", "poster");
  resetStore({
    ordinary: conversation("ordinary", {
      pending: true,
      run_id: runId,
      artifacts: { [source.artifact_id]: source },
      active_artifact_id: source.artifact_id,
      messages: [{
        id: "pptx-persisted-user",
        role: "user",
        text: `Export this design as an editable PPTX: ${source.name}`,
        ts: 1,
        status: "done",
        task_type: "artifact_export_pptx",
        task_payload: {
          source_artifact_id: source.artifact_id,
          export_format: "pptx",
        },
        source_artifact_id: source.artifact_id,
      }, {
        id: "pptx-persisted-placeholder",
        role: "assistant",
        text: "",
        ts: 2,
        run_id: runId,
        status: "streaming",
        task_type: "artifact_export_pptx",
        task_payload: {
          source_artifact_id: source.artifact_id,
          export_format: "pptx",
        },
        source_artifact_id: source.artifact_id,
      }],
    }),
  }, "ordinary");
  let artifactReads = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${runId}/artifact`) {
      artifactReads += 1;
      return jsonResponse(pptxResponseForRun(runId));
    }
    if (url === "/api/design-events") return jsonResponse({ ok: true });
    throw new Error(`Persisted PPTX recovery must not call ${url}`);
  }) as typeof fetch;

  useApp.getState().recoverActiveRuns();
  await waitFor(
    () => useApp.getState().conversations.ordinary.pending === false,
    "persisted PPTX export did not settle",
  );

  const state = useApp.getState();
  assert.equal(artifactReads, 1);
  assert.equal(MockEventSource.instances.length, 1);
  assert.equal(state.conversations.ordinary.run_id, undefined);
  assert.equal(state.conversations.ordinary.active_artifact_id, source.artifact_id);
  assert.equal(state.conversations.ordinary.messages.at(-1)?.status, "done");
  assert.deepEqual(triggeredDownloads, [{
    url: `/api/files/runs/${runId}/final/deck.pptx`,
    filename: `${runId}.pptx`,
  }]);
});

test("recoverActiveRuns does not steal a pre-acknowledgement PPTX export", async () => {
  const runId = "run_pptx_pre_ack_recovery";
  const source = artifact("pptx_pre_ack_source", "poster");
  resetStore({
    ordinary: conversation("ordinary", {
      artifacts: { [source.artifact_id]: source },
      active_artifact_id: source.artifact_id,
    }),
  }, "ordinary");
  let releaseStart!: (response: Response) => void;
  let artifactReads = 0;
  let terminalReady = false;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/artifacts/export/pptx-run") {
      return jsonResponse({
        run_id: runId,
        start_token: "start-token",
        progress_mode: "artifact_export",
      });
    }
    if (url === `/api/runs/${runId}/start`) {
      return new Promise<Response>((resolve) => { releaseStart = resolve; });
    }
    if (url === `/api/runs/${runId}/artifact`) {
      artifactReads += 1;
      return terminalReady
        ? jsonResponse(pptxResponseForRun(runId))
        : jsonResponse({ detail: "artifact is not ready" }, 409);
    }
    if (url === "/api/design-events") return jsonResponse({ ok: true });
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const exporting = useApp.getState().exportArtifactPptx(source.artifact_id);
  await waitFor(() => typeof releaseStart === "function", "PPTX /start did not begin");
  useApp.getState().recoverActiveRuns();
  await tick();
  await tick();

  releaseStart(jsonResponse({
    run_id: runId,
    progress_mode: "artifact_export",
    placeholder_message: {
      id: `msg_${runId}`,
      role: "assistant",
      text: "",
      ts: 1,
      status: "streaming",
    },
  }));
  await waitFor(
    () => MockEventSource.instances.some((eventSource) => (
      eventSource.readyState !== MockEventSource.CLOSED
    )),
    "PPTX event stream did not start",
  );
  await tick();
  terminalReady = true;
  for (const eventSource of MockEventSource.instances) {
    if (eventSource.readyState !== MockEventSource.CLOSED) {
      eventSource.emit("run.done", { event_id: "pre-ack-pptx-terminal" });
    }
  }
  await exporting;
  await tick();

  const state = useApp.getState();
  assert.equal(MockEventSource.instances.length, 1);
  assert.equal(artifactReads, 1);
  assert.equal(state.conversations.ordinary.pending, false);
  assert.equal(state.conversations.ordinary.run_id, undefined);
  assert.equal(state.conversations.ordinary.active_artifact_id, source.artifact_id);
  assert.equal(state.conversations.ordinary.messages.at(-1)?.status, "done");
  assert.equal(state.conversations.ordinary.messages.at(-1)?.failure, undefined);
  assert.deepEqual(triggeredDownloads, [{
    url: `/api/files/runs/${runId}/final/deck.pptx`,
    filename: `${runId}.pptx`,
  }]);
});

test("PPTX cancellation during /start never flashes a failed export", async () => {
  const source = artifact("pptx_cancel_source", "poster");
  resetStore({
    ordinary: conversation("ordinary", {
      artifacts: { [source.artifact_id]: source },
      active_artifact_id: source.artifact_id,
    }),
  }, "ordinary");
  let releaseStart!: (response: Response) => void;
  let releaseCancel!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/artifacts/export/pptx-run") {
      return jsonResponse({
        run_id: "run_pptx_start_cancel",
        start_token: "start-token",
        progress_mode: "artifact_export",
      });
    }
    if (url === "/api/runs/run_pptx_start_cancel/start") {
      return new Promise<Response>((resolve) => { releaseStart = resolve; });
    }
    if (url === "/api/runs/run_pptx_start_cancel/cancel") {
      return new Promise<Response>((resolve) => { releaseCancel = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const exporting = useApp.getState().exportArtifactPptx(source.artifact_id);
  await waitFor(() => typeof releaseStart === "function", "PPTX /start did not begin");
  const cancelling = useApp.getState().cancelRun("ordinary");
  await waitFor(() => typeof releaseCancel === "function", "PPTX cancellation did not begin");
  releaseStart(jsonResponse({ detail: "run was cancelled" }, 409));
  await exporting;

  try {
    const state = useApp.getState();
    const latest = state.conversations.ordinary.messages.at(-1);
    assert.equal(latest?.status, "streaming");
    assert.doesNotMatch(latest?.text ?? "", /failed/i);
    assert.equal(state.conversations.ordinary.run_id, "run_pptx_start_cancel");
    assert.equal(state.runs_progress.ordinary?.phase, "cancelling");
  } finally {
    releaseCancel(confirmedCancellation("run_pptx_start_cancel"));
    await cancelling;
  }

  const cancelled = useApp.getState().conversations.ordinary.messages.at(-1);
  assert.equal(cancelled?.failure?.status, "cancelled");
  assert.doesNotMatch(cancelled?.text ?? "", /failed/i);
});

test("a late successful PPTX /start acknowledgement cannot revive a cancelled run", async () => {
  const source = artifact("pptx_late_start_source", "poster");
  resetStore({
    ordinary: conversation("ordinary", {
      artifacts: { [source.artifact_id]: source },
      active_artifact_id: source.artifact_id,
    }),
  }, "ordinary");
  let releaseStart!: (response: Response) => void;
  let startSignal: AbortSignal | null = null;
  let artifactReads = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/artifacts/export/pptx-run") {
      return jsonResponse({
        run_id: "run_pptx_late_start",
        start_token: "start-token",
        progress_mode: "artifact_export",
      });
    }
    if (url === "/api/runs/run_pptx_late_start/start") {
      startSignal = init?.signal ?? null;
      return new Promise<Response>((resolve) => { releaseStart = resolve; });
    }
    if (url === "/api/runs/run_pptx_late_start/cancel") {
      return confirmedCancellation("run_pptx_late_start");
    }
    if (url === "/api/runs/run_pptx_late_start/artifact") {
      artifactReads += 1;
      return jsonResponse(responseForRun("run_pptx_late_start", "deck"));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  let settled = false;
  const exporting = useApp.getState().exportArtifactPptx(source.artifact_id)
    .finally(() => { settled = true; });
  await waitFor(() => typeof releaseStart === "function", "late PPTX /start did not begin");
  await useApp.getState().cancelRun("ordinary");
  assert.equal(startSignal?.aborted, true);
  releaseStart(jsonResponse({
    run_id: "run_pptx_late_start",
    progress_mode: "artifact_export",
  }));
  await waitFor(
    () => settled || MockEventSource.instances.length > 0,
    "late PPTX acknowledgement did not settle or open an event stream",
  );
  for (const sourceEvent of MockEventSource.instances) sourceEvent.emit("run.done");
  await exporting;

  const state = useApp.getState();
  assert.equal(artifactReads, 0);
  assert.equal(MockEventSource.instances.length, 0);
  assert.equal(state.conversations.ordinary.pending, false);
  assert.equal(state.conversations.ordinary.run_id, undefined);
  assert.equal(state.conversations.ordinary.messages.at(-1)?.failure?.status, "cancelled");
});

test("retry cancellation during /start never replaces cancelling with failed", async () => {
  resetStore({
    ordinary: conversation("ordinary", {
      messages: [{
        id: "retry-failure",
        role: "assistant",
        text: "Original run failed",
        ts: 1,
        run_id: "run_retry_source",
        status: "error",
        task_type: "generate",
        task_payload: { artifact_type: "landing" },
        failure: { status: "error", artifact_type: "landing", produced_files: [] },
      }],
    }),
  }, "ordinary");
  let releaseStart!: (response: Response) => void;
  let releaseCancel!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/runs/run_retry_source/retry") {
      return jsonResponse({
        run_id: "run_retry_start_cancel",
        start_token: "start-token",
        progress_mode: "generate",
      });
    }
    if (url === "/api/runs/run_retry_start_cancel/start") {
      return new Promise<Response>((resolve) => { releaseStart = resolve; });
    }
    if (url === "/api/runs/run_retry_start_cancel/cancel") {
      return new Promise<Response>((resolve) => { releaseCancel = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const retrying = useApp.getState().retryRun("retry-failure");
  await waitFor(() => typeof releaseStart === "function", "retry /start did not begin");
  const cancelling = useApp.getState().cancelRun("ordinary");
  await waitFor(() => typeof releaseCancel === "function", "retry cancellation did not begin");
  releaseStart(jsonResponse({ detail: "run was cancelled" }, 409));
  await retrying;

  try {
    const state = useApp.getState();
    const latest = state.conversations.ordinary.messages.at(-1);
    assert.equal(latest?.status, "streaming");
    assert.doesNotMatch(latest?.text ?? "", /failed/i);
    assert.equal(state.conversations.ordinary.run_id, "run_retry_start_cancel");
    assert.equal(state.runs_progress.ordinary?.phase, "cancelling");
  } finally {
    releaseCancel(confirmedCancellation("run_retry_start_cancel"));
    await cancelling;
  }

  assert.equal(
    useApp.getState().conversations.ordinary.messages.at(-1)?.failure?.status,
    "cancelled",
  );
});

test("resume cancellation during /start stays cancelling until confirmation", async () => {
  resetStore({
    ordinary: conversation("ordinary", {
      messages: [{
        id: "resume-user",
        role: "user",
        text: "Create a landing page",
        ts: 1,
        status: "done",
        task_type: "generate",
        task_payload: { artifact_type: "landing" },
      }, {
        id: "resume-failure",
        role: "assistant",
        text: "Connection lost",
        ts: 2,
        run_id: "run_resume_source",
        status: "error",
        task_type: "generate",
        task_payload: { artifact_type: "landing" },
        failure: { status: "connection_lost", artifact_type: "landing", produced_files: [] },
      }],
    }),
  }, "ordinary");
  let releaseStart!: (response: Response) => void;
  let releaseCancel!: (response: Response) => void;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/generate") {
      return jsonResponse({
        run_id: "run_resume_start_cancel",
        start_token: "start-token",
        progress_mode: "generate",
      });
    }
    if (url === "/api/runs/run_resume_start_cancel/start") {
      return new Promise<Response>((resolve) => { releaseStart = resolve; });
    }
    if (url === "/api/runs/run_resume_start_cancel/cancel") {
      return new Promise<Response>((resolve) => { releaseCancel = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const resuming = useApp.getState().resumeRun("resume-failure");
  await waitFor(() => typeof releaseStart === "function", "resume /start did not begin");
  const cancelling = useApp.getState().cancelRun("ordinary");
  await waitFor(() => typeof releaseCancel === "function", "resume cancellation did not begin");
  releaseStart(jsonResponse({ detail: "run was cancelled" }, 409));
  await resuming;

  try {
    const state = useApp.getState();
    const latest = state.conversations.ordinary.messages.at(-1);
    assert.equal(latest?.status, "streaming");
    assert.doesNotMatch(latest?.text ?? "", /failed/i);
    assert.equal(state.conversations.ordinary.run_id, "run_resume_start_cancel");
    assert.equal(state.runs_progress.ordinary?.phase, "cancelling");
  } finally {
    releaseCancel(confirmedCancellation("run_resume_start_cancel"));
    await cancelling;
  }

  assert.equal(
    useApp.getState().conversations.ordinary.messages.at(-1)?.failure?.status,
    "cancelled",
  );
});

test("editable video cancellation during /start does not create a failed render", async () => {
  const video: Artifact = {
    ...artifact("video_cancel_source", "video"),
    video_project: {
      duration_s: 5,
      fps: 30,
      scenes: [{
        scene_id: "scene-1",
        name: "Scene 1",
        frame_layer_id: "frame-1",
        duration_s: 5,
        transition: "cut",
      }],
    },
  };
  resetStore({
    ordinary: conversation("ordinary", {
      artifacts: { [video.artifact_id]: video },
      active_artifact_id: video.artifact_id,
    }),
  }, "ordinary");
  let releaseStart!: (response: Response) => void;
  let releaseCancel!: (response: Response) => void;
  let renderRequests = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/video/render") {
      renderRequests += 1;
      if (renderRequests > 1) throw new Error("duplicate render reached network");
      return jsonResponse({
        run_id: "run_video_start_cancel",
        start_token: "start-token",
        progress_mode: "video_render",
      });
    }
    if (url === "/api/runs/run_video_start_cancel/start") {
      return new Promise<Response>((resolve) => { releaseStart = resolve; });
    }
    if (url === "/api/runs/run_video_start_cancel/cancel") {
      return new Promise<Response>((resolve) => { releaseCancel = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const rendering = useApp.getState().renderActiveVideo();
  await waitFor(() => typeof releaseStart === "function", "video /start did not begin");
  const cancelling = useApp.getState().cancelRun("ordinary");
  await waitFor(() => typeof releaseCancel === "function", "video cancellation did not begin");
  releaseStart(jsonResponse({ detail: "run was cancelled" }, 409));
  await rendering;

  try {
    const state = useApp.getState();
    assert.equal(
      state.conversations.ordinary.artifacts[video.artifact_id].video_project?.latest_render,
      undefined,
    );
    assert.equal(state.conversations.ordinary.run_id, "run_video_start_cancel");
    assert.equal(state.runs_progress.ordinary?.phase, "cancelling");
    await assert.rejects(
      useApp.getState().renderActiveVideo(),
      /current run.*still active/i,
    );
    assert.equal(renderRequests, 1);
  } finally {
    releaseCancel(confirmedCancellation("run_video_start_cancel"));
    await cancelling;
  }

  assert.equal(
    useApp.getState().conversations.ordinary.artifacts[video.artifact_id]
      .video_project?.latest_render,
    undefined,
  );
});

test("a stale production release closure cannot clear a replacement token owner", () => {
  const conversationId = "tokenized-publication-owner";
  const moduleOwners = new Map<string, { token: symbol; label: string }>();
  let reactiveOwners: Record<string, {
    token: symbol;
    operationConversationId: string;
  }> = {};
  const reactiveOwner = () => reactiveOwners[conversationId];
  const replaceReactiveOwner = (
    next: { token: symbol; operationConversationId: string } | undefined,
  ) => {
    const updated = { ...reactiveOwners };
    if (next) updated[conversationId] = next;
    else delete updated[conversationId];
    reactiveOwners = updated;
  };
  const tokenA = Symbol("owner-a");
  const ownerA = { token: tokenA, label: "A" };
  const releaseA = installTokenizedPublicationOwner(
    moduleOwners,
    conversationId,
    ownerA,
    { token: tokenA, operationConversationId: "operation-a" },
    reactiveOwner,
    replaceReactiveOwner,
  );

  releaseA();
  assert.equal(moduleOwners.has(conversationId), false);
  assert.equal(reactiveOwner(), undefined);

  const tokenB = Symbol("owner-b");
  const ownerB = { token: tokenB, label: "B" };
  const releaseB = installTokenizedPublicationOwner(
    moduleOwners,
    conversationId,
    ownerB,
    { token: tokenB, operationConversationId: "operation-b" },
    reactiveOwner,
    replaceReactiveOwner,
  );
  releaseA();

  assert.equal(moduleOwners.get(conversationId), ownerB);
  assert.equal(reactiveOwner()?.token, tokenB);
  assert.equal(reactiveOwner()?.operationConversationId, "operation-b");
  releaseB();
});

test("bundle cancellation latches a pre-ack candidate publication and cancels a racing reservation", async () => {
  const {
    parentId,
    childId,
    sourceRunId,
    publishRunId,
    draft,
  } = setupPreAckCandidatePublication("confirmed");
  let releaseFirstReserve!: (response: Response) => void;
  let rejectSecondReserve!: (error: Error) => void;
  let releaseExactCancellation!: (response: Response) => void;
  let firstReserveSignal: AbortSignal | undefined;
  let secondReserveSignal: AbortSignal | undefined;
  let cancellationLatched = false;
  let transientPublicationState = false;
  let reserveRequests = 0;
  let startRequests = 0;
  let artifactReads = 0;
  let publicationSettled = false;
  let cancellationSawSettledPublication = false;
  const runCancels: string[] = [];
  const activityTransitions = [
    candidatePublicationIsActive(useApp.getState(), childId),
  ];
  let lastPublicationActivity = activityTransitions[0];
  const unsubscribe = useApp.subscribe((state) => {
    const publicationActive = candidatePublicationIsActive(state, childId);
    if (publicationActive !== lastPublicationActivity) {
      activityTransitions.push(publicationActive);
      lastPublicationActivity = publicationActive;
    }
    if (!cancellationLatched) return;
    const child = state.conversations[childId];
    if (
      child?.messages.some((message) => message.task_type === "candidate_publish")
      || state.runs_progress[`${childId}:candidate-publish`]
      || state.runs_progress[childId]?.mode === "attempt_publish"
    ) {
      transientPublicationState = true;
    }
  });

  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === `/api/artifacts/${draft.artifact_id}/publish-candidate-draft`) {
      reserveRequests += 1;
      if (reserveRequests === 1) {
        firstReserveSignal = init?.signal ?? undefined;
        firstReserveSignal?.addEventListener("abort", () => {
          cancellationLatched = true;
        }, { once: true });
        return new Promise<Response>((resolve) => { releaseFirstReserve = resolve; });
      }
      secondReserveSignal = init?.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        rejectSecondReserve = reject;
      });
    }
    if (url === `/api/runs/${publishRunId}/start`) {
      startRequests += 1;
      return jsonResponse({ detail: "latched publication must not start" }, 500);
    }
    if (url === `/api/runs/${publishRunId}/artifact`) {
      artifactReads += 1;
      return jsonResponse({ detail: "latched publication has no artifact" }, 500);
    }
    const cancel = url.match(/^\/api\/runs\/([^/]+)\/cancel$/);
    if (cancel) {
      runCancels.push(cancel[1]);
      if (cancel[1] === publishRunId) {
        return new Promise<Response>((resolve) => {
          releaseExactCancellation = resolve;
        });
      }
      return confirmedCancellation(cancel[1]);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const publishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget());
  const publicationOutcome = publishing.then(
    () => {
      publicationSettled = true;
      return "fulfilled" as const;
    },
    () => {
      publicationSettled = true;
      return "rejected" as const;
    },
  );
  await waitFor(() => Boolean(firstReserveSignal), "candidate reserve did not begin");
  const preAckState = useApp.getState();
  const preAckOwner = preAckState.candidate_publication_owners[childId];
  const persistedPreAckState = localStorage.getItem("autodesign.web.v1") ?? "";
  const preAckObservation = {
    active: candidatePublicationIsActive(preAckState, childId),
    ownerInstalled: Boolean(preAckOwner?.token),
    operationConversationId: preAckOwner?.operationConversationId,
    sourceRunUnchanged: preAckState.conversations[childId].run_id === sourceRunId,
    candidateMessages: preAckState.conversations[childId].messages.filter(
      (message) => message.task_type === "candidate_publish",
    ).length,
    candidateProgress: Boolean(
      preAckState.runs_progress[`${childId}:candidate-publish`]
      || preAckState.runs_progress[childId]?.mode === "attempt_publish"
    ),
    persistedOwner: persistedPreAckState.includes("candidate_publication_owners"),
  };
  const cancelling = useApp.getState().cancelPaperBundle(parentId).then(() => {
    cancellationSawSettledPublication = publicationSettled;
  });
  await tick();
  const signalWasAbortedBeforeAck = firstReserveSignal?.aborted === true;
  const ownerAfterLatch = useApp.getState().candidate_publication_owners[childId];

  releaseFirstReserve(jsonResponse({
    run_id: publishRunId,
    start_token: "start-token",
    progress_mode: "attempt_publish",
  }));
  await waitFor(
    () => typeof releaseExactCancellation === "function" || startRequests > 0,
    "late acknowledgement neither cancelled nor started",
  );
  assert.equal(publicationSettled, false);
  assert.equal(cancellationSawSettledPublication, false);
  const ownerBeforeConfirmation = useApp.getState().candidate_publication_owners[childId];
  assert.equal(ownerBeforeConfirmation?.token, preAckOwner?.token);
  assert.equal(candidatePublicationIsActive(useApp.getState(), childId), true);
  if (typeof releaseExactCancellation === "function") {
    releaseExactCancellation(confirmedCancellation(publishRunId));
  }
  const [publishStatus] = await Promise.all([publicationOutcome, cancelling]);
  unsubscribe();

  const state = useApp.getState();
  const child = state.conversations[childId];
  const firstObservation = {
    signalWasAbortedBeforeAck,
    publishStatus,
    cancellationSawSettledPublication,
    preAckObservation,
    ownerRetainedAfterLatch: ownerAfterLatch?.token === preAckOwner?.token,
    reactiveOwnerCleared: state.candidate_publication_owners[childId] === undefined,
    activityTransitions,
    bundleCancels: [...paperBundleCancelRequests],
    runCancels: [...runCancels],
    sourceCancels: runCancels.filter((runId) => runId === sourceRunId).length,
    startRequests,
    artifactReads,
    candidateMessages: child.messages.filter(
      (message) => message.task_type === "candidate_publish",
    ).length,
    candidateProgress: Boolean(
      state.runs_progress[`${childId}:candidate-publish`]
      || state.runs_progress[childId]?.mode === "attempt_publish"
    ),
    publicationSources: MockEventSource.instances.filter(
      (source) => source.url === `/api/runs/${publishRunId}/events`,
    ).length,
    transientPublicationState,
    activeArtifactId: child.active_artifact_id,
    publishedArtifactId: child.published_artifact_id,
  };

  const secondPublishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget());
  const secondOutcome = secondPublishing.then(
    () => "fulfilled" as const,
    () => "rejected" as const,
  );
  await waitFor(
    () => reserveRequests === 2,
    "a second publication did not reach reservation after confirmed cancellation",
  );
  const competing = await Promise.allSettled([
    useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget()),
  ]);
  await tick();
  const secondOwnerObservation = {
    reserveRequests,
    secondSignalAborted: secondReserveSignal?.aborted === true,
    competing: competing[0].status,
  };
  rejectSecondReserve(new Error("stop second publication after ownership assertion"));
  assert.equal(await secondOutcome, "rejected");

  assert.deepEqual(firstObservation, {
    signalWasAbortedBeforeAck: true,
    publishStatus: "fulfilled",
    cancellationSawSettledPublication: true,
    preAckObservation: {
      active: true,
      ownerInstalled: true,
      operationConversationId: `${childId}:candidate-publish`,
      sourceRunUnchanged: true,
      candidateMessages: 0,
      candidateProgress: false,
      persistedOwner: false,
    },
    ownerRetainedAfterLatch: true,
    reactiveOwnerCleared: true,
    activityTransitions: [false, true, false],
    bundleCancels: [`job_pre_ack_confirmed`],
    runCancels: [publishRunId],
    sourceCancels: 0,
    startRequests: 0,
    artifactReads: 0,
    candidateMessages: 0,
    candidateProgress: false,
    publicationSources: 0,
    transientPublicationState: false,
    activeArtifactId: draft.artifact_id,
    publishedArtifactId: undefined,
  });
  assert.deepEqual(secondOwnerObservation, {
    reserveRequests: 2,
    secondSignalAborted: false,
    competing: "rejected",
  });
});

test("late pre-ack cancellation failures retain a retryable exact owner", async () => {
  const observations: Array<Record<string, unknown>> = [];

  for (const failureMode of ["unconfirmed", "transport", "timeout"] as const) {
    const {
      parentId,
      childId,
      sourceRunId,
      publishRunId,
      draft,
    } = setupPreAckCandidatePublication(failureMode);
    const nativeSetTimeout = window.setTimeout;
    let cancellationTimers = 0;
    if (failureMode === "timeout") {
      window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
        if (timeout === 10_000) {
          cancellationTimers += 1;
          if (cancellationTimers === 2) {
            queueMicrotask(() => {
              if (typeof handler === "function") handler(...args);
            });
            return 91 as unknown as number;
          }
        }
        return nativeSetTimeout(handler, timeout, ...args);
      }) as typeof window.setTimeout;
    }

    let releaseReserve!: (response: Response) => void;
    let rejectSecondReserve!: (error: Error) => void;
    let reserveSignal: AbortSignal | undefined;
    let secondReserveSignal: AbortSignal | undefined;
    let reserveRequests = 0;
    let startRequests = 0;
    let artifactReads = 0;
    let retrying = false;
    const runCancels: string[] = [];
    const activityTransitions = [
      candidatePublicationIsActive(useApp.getState(), childId),
    ];
    let lastPublicationActivity = activityTransitions[0];
    const unsubscribeActivity = useApp.subscribe((state) => {
      const active = candidatePublicationIsActive(state, childId);
      if (active !== lastPublicationActivity) {
        activityTransitions.push(active);
        lastPublicationActivity = active;
      }
    });
    try {
      globalThis.fetch = (async (input, init) => {
        const url = String(input);
        if (url === `/api/artifacts/${draft.artifact_id}/publish-candidate-draft`) {
          reserveRequests += 1;
          if (reserveRequests === 1) {
            reserveSignal = init?.signal ?? undefined;
            return new Promise<Response>((resolve) => { releaseReserve = resolve; });
          }
          secondReserveSignal = init?.signal ?? undefined;
          return new Promise<Response>((_resolve, reject) => {
            rejectSecondReserve = reject;
          });
        }
        if (url === `/api/runs/${publishRunId}/start`) {
          startRequests += 1;
          return jsonResponse({ detail: "latched publication must not start" }, 500);
        }
        if (url === `/api/runs/${publishRunId}/artifact`) {
          artifactReads += 1;
          return jsonResponse({ detail: "latched publication has no artifact" }, 500);
        }
        const cancel = url.match(/^\/api\/runs\/([^/]+)\/cancel$/);
        if (cancel) {
          runCancels.push(cancel[1]);
          if (cancel[1] !== publishRunId || retrying) {
            return confirmedCancellation(cancel[1]);
          }
          if (failureMode === "transport") {
            throw new Error("late publication cancellation transport failed");
          }
          if (failureMode === "timeout") {
            return new Promise<Response>((_resolve, reject) => {
              const signal = init?.signal;
              const rejectForAbort = () => reject(
                signal?.reason instanceof Error
                  ? signal.reason
                  : new Error("late publication cancellation timed out"),
              );
              if (signal?.aborted) rejectForAbort();
              else signal?.addEventListener("abort", rejectForAbort, { once: true });
            });
          }
          return jsonResponse({
            run_id: publishRunId,
            status: "cancellation_pending",
            run_state: "cancelling",
            confirmed: false,
            terminated_pids: [],
            surviving_pids: [],
          }, 202);
        }
        throw new Error(`Unexpected fetch: ${url}`);
      }) as typeof fetch;

      const publishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget());
      const publicationOutcome = publishing.then(
        () => "fulfilled" as const,
        () => "rejected" as const,
      );
      await waitFor(() => Boolean(reserveSignal), `${failureMode} reserve did not begin`);
      const preAckToken = useApp.getState()
        .candidate_publication_owners[childId]?.token;
      const cancelling = useApp.getState().cancelPaperBundle(parentId);
      await tick();
      const signalWasAbortedBeforeAck = reserveSignal?.aborted === true;
      releaseReserve(jsonResponse({
        run_id: publishRunId,
        start_token: "start-token",
        progress_mode: "attempt_publish",
      }));
      const [publishStatus] = await Promise.all([publicationOutcome, cancelling]);
      const state = useApp.getState();
      const bundle = state.conversations[parentId].paper_bundle as PaperBundleParentState;
      const retainedReactiveOwner = state.candidate_publication_owners[childId];
      const initialObservation = {
        failureMode,
        signalWasAbortedBeforeAck,
        publishStatus,
        bundleCancels: paperBundleCancelRequests.length,
        exactCancels: runCancels.filter((runId) => runId === publishRunId).length,
        sourceCancels: runCancels.filter((runId) => runId === sourceRunId).length,
        startRequests,
        artifactReads,
        publicationSources: MockEventSource.instances.filter(
          (source) => source.url === `/api/runs/${publishRunId}/events`,
        ).length,
        backendState: bundle.backend_state,
        taskStatus: bundle.tasks.poster.status,
        retryable: /not confirmed/i.test(
          `${bundle.cancel_error ?? ""} ${bundle.tasks.poster.error ?? ""}`,
        ),
        reactiveTokenRetained: Boolean(preAckToken)
          && retainedReactiveOwner?.token === preAckToken,
        sharedPredicateActive: candidatePublicationIsActive(state, childId),
      };

      let blockedBeforeRetry: string | undefined;
      let releasedForSecondReservation = false;
      let secondSignalAborted = false;
      let reactiveOwnerCleared = false;
      if (
        signalWasAbortedBeforeAck
        && startRequests === 0
        && initialObservation.exactCancels === 1
        && bundle.backend_state === "cancelling"
      ) {
        const [blocked] = await Promise.allSettled([
          useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget()),
        ]);
        blockedBeforeRetry = blocked.status;
        assert.equal(reserveRequests, 1);
        retrying = true;
        await useApp.getState().cancelPaperBundle(parentId);
        reactiveOwnerCleared = useApp.getState()
          .candidate_publication_owners[childId] === undefined
          && !candidatePublicationIsActive(useApp.getState(), childId);
        unsubscribeActivity();
        const secondPublishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget());
        const secondOutcome = secondPublishing.then(
          () => "fulfilled" as const,
          () => "rejected" as const,
        );
        await waitFor(
          () => reserveRequests === 2,
          `${failureMode} owner was not released after confirmed retry`,
        );
        releasedForSecondReservation = true;
        secondSignalAborted = secondReserveSignal?.aborted === true;
        rejectSecondReserve(new Error("stop retry ownership probe"));
        assert.equal(await secondOutcome, "rejected");
      }

      observations.push({
        ...initialObservation,
        blockedBeforeRetry,
        retryExactCancels: runCancels.filter((runId) => runId === publishRunId).length,
        retryBundleCancels: paperBundleCancelRequests.length,
        releasedForSecondReservation,
        secondSignalAborted,
        reactiveOwnerCleared,
        activityTransitions,
      });
    } finally {
      unsubscribeActivity();
      window.setTimeout = nativeSetTimeout;
    }
  }

  assert.deepEqual(observations, ["unconfirmed", "transport", "timeout"].map(
    (failureMode) => ({
      failureMode,
      signalWasAbortedBeforeAck: true,
      publishStatus: "fulfilled",
      bundleCancels: 1,
      exactCancels: 1,
      sourceCancels: 0,
      startRequests: 0,
      artifactReads: 0,
      publicationSources: 0,
      backendState: "cancelling",
      taskStatus: "cancelling",
      retryable: true,
      reactiveTokenRetained: true,
      sharedPredicateActive: true,
      blockedBeforeRetry: "rejected",
      retryExactCancels: 2,
      retryBundleCancels: 2,
      releasedForSecondReservation: true,
      secondSignalAborted: false,
      reactiveOwnerCleared: true,
      activityTransitions: [false, true, false],
    }),
  ));
});

const step4dSourceMessage = (
  id: string,
  artifactType: ArtifactType,
  runId?: string,
) => ({
  id,
  role: "assistant" as const,
  text: "Generating source.",
  ts: 1,
  ...(runId ? { run_id: runId } : {}),
  status: "streaming" as const,
  task_type: "generate" as const,
  task_payload: { artifact_type: artifactType },
});

const step4dSourceResult = (
  messageId: string,
  runId: string,
  artifactType: ArtifactType,
) => ({
  message: {
    id: messageId,
    role: "assistant" as const,
    text: "Source complete.",
    ts: 4,
    run_id: runId,
    artifact_id: `art_${runId}`,
    status: "done" as const,
  },
  artifact: artifact(runId, artifactType),
});

const step4dDraft = (
  id: string,
  artifactType: ArtifactType,
  sourceRunId: string,
  candidateId: string,
): Artifact => ({
  ...artifact(id, artifactType),
  candidate_draft: true,
  attempt_lineage: {
    materialization_version: 2,
    status: "draft",
    source_run_id: sourceRunId,
    source_attempt: 1,
    source_candidate_id: candidateId,
    source_candidate_sha256: "a".repeat(64),
  },
});

const step4dPublished = (
  id: string,
  artifactType: ArtifactType,
  sourceRunId: string,
  candidateId: string,
): Artifact => ({
  ...artifact(id, artifactType),
  candidate_draft: false,
  attempt_lineage: {
    materialization_version: 2,
    status: "published",
    source_run_id: sourceRunId,
    source_attempt: 1,
    source_candidate_id: candidateId,
    source_candidate_sha256: "a".repeat(64),
  },
});

const step4dEventSource = (runId: string) => MockEventSource.instances.find(
  (source) => source.url === `/api/runs/${runId}/events`,
);

const settleStep4dTicks = async () => {
  await tick();
  await tick();
  await tick();
};

test("Step4d ordinary attempt forks isolate no-progress and terminal-held source owners", async () => {
  const observations: Array<Record<string, unknown>> = [];

  for (const sourcePhase of ["no-progress", "terminal-artifact-held"] as const) {
    const conversationId = `step4d_fork_${sourcePhase}`;
    const sourceRunId = `run_step4d_source_${sourcePhase}`;
    const forkRunId = `run_step4d_fork_${sourcePhase}`;
    const sourceMessageId = `msg_step4d_source_${sourcePhase}`;
    const candidateId = `poster-step4d-${sourcePhase}`;
    const draft = step4dDraft(
      `step4d_fork_draft_${sourcePhase}`,
      "poster",
      sourceRunId,
      candidateId,
    );
    resetStore({
      [conversationId]: conversation(conversationId, {
        pending: true,
        run_id: sourceRunId,
        messages: [step4dSourceMessage(sourceMessageId, "poster")],
      }),
    }, conversationId);

    let sourceReads = 0;
    let releaseSourceArtifact: ((response: Response) => void) | undefined;
    let releaseForkStart: ((response: Response) => void) | undefined;
    globalThis.fetch = (async (input) => {
      const url = String(input);
      if (url === `/api/runs/${sourceRunId}/artifact`) {
        sourceReads += 1;
        if (sourceReads === 1) {
          return jsonResponse({ detail: "source still running" }, 404);
        }
        if (sourcePhase === "terminal-artifact-held") {
          return new Promise<Response>((resolve) => { releaseSourceArtifact = resolve; });
        }
        return jsonResponse(step4dSourceResult(sourceMessageId, sourceRunId, "poster"));
      }
      if (url === `/api/runs/${sourceRunId}/attempts/1/fork`) {
        return jsonResponse({
          run_id: forkRunId,
          start_token: "fork-start-token",
          progress_mode: "attempt_fork",
          placeholder_message: {
            id: `msg_${forkRunId}`,
            role: "assistant",
            text: "",
            ts: 2,
            status: "streaming",
          },
        });
      }
      if (url === `/api/runs/${forkRunId}/start`) {
        const ack = jsonResponse({
          run_id: forkRunId,
          progress_mode: "attempt_fork",
          placeholder_message: {
            id: `msg_${forkRunId}`,
            role: "assistant",
            text: "",
            ts: 2,
            status: "streaming",
          },
        });
        if (sourcePhase === "terminal-artifact-held") {
          return new Promise<Response>((resolve) => { releaseForkStart = resolve; });
        }
        return ack;
      }
      if (url === `/api/runs/${forkRunId}/artifact`) {
        return jsonResponse({
          message: {
            id: `msg_${forkRunId}`,
            role: "assistant",
            text: "Candidate draft ready.",
            ts: 3,
            run_id: forkRunId,
            artifact_id: draft.artifact_id,
            status: "done",
          },
          artifact: draft,
        });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }) as typeof fetch;

    useApp.getState().recoverActiveRuns();
    await waitFor(
      () => Boolean(step4dEventSource(sourceRunId)),
      `${sourcePhase} source listener did not recover`,
    );
    if (sourcePhase === "no-progress") {
      useApp.setState((state) => {
        const progress = { ...state.runs_progress };
        delete progress[conversationId];
        return { runs_progress: progress };
      });
    } else {
      step4dEventSource(sourceRunId)?.emit("run.done");
      await waitFor(
        () => typeof releaseSourceArtifact === "function",
        "source terminal artifact read was not held",
      );
    }

    const opening = useApp.getState().openAttemptInCanvas(
      sourceRunId,
      {
        ...attemptCandidate(sourceRunId, candidateId),
        artifact_type: "poster",
      },
      conversationId,
    );
    if (sourcePhase === "terminal-artifact-held") {
      await waitFor(
        () => typeof releaseForkStart === "function",
        "fork /start acknowledgement was not held",
      );
      releaseForkStart?.(jsonResponse({
        run_id: forkRunId,
        progress_mode: "attempt_fork",
        placeholder_message: {
          id: `msg_${forkRunId}`,
          role: "assistant",
          text: "",
          ts: 2,
          status: "streaming",
        },
      }));
    }
    await waitFor(
      () => Boolean(step4dEventSource(forkRunId)),
      `${sourcePhase} fork listener did not open`,
    );
    const live = useApp.getState();
    const during = {
      sourceListenerOpen: step4dEventSource(sourceRunId)?.readyState === 1,
      pending: live.conversations[conversationId].pending,
      runId: live.conversations[conversationId].run_id,
      sourceProgressRunId: live.runs_progress[conversationId]?.run_id,
      forkProgressRunId:
        live.runs_progress[`${conversationId}:attempt-fork`]?.run_id,
    };

    step4dEventSource(forkRunId)?.emit("run.done");
    await opening.catch(() => undefined);
    if (sourcePhase === "no-progress") {
      step4dEventSource(sourceRunId)?.emit("run.done");
    } else {
      releaseSourceArtifact?.(
        jsonResponse(step4dSourceResult(sourceMessageId, sourceRunId, "poster")),
      );
    }
    await settleStep4dTicks();

    const settled = useApp.getState().conversations[conversationId];
    const sourceMessages = settled.messages.filter(
      (message) => message.id === sourceMessageId,
    );
    const sourceMessage = sourceMessages[0];
    observations.push({
      sourcePhase,
      during,
      activeArtifactId: settled.active_artifact_id,
      sourceArtifactRetained: Boolean(settled.artifacts[`art_${sourceRunId}`]),
      sourceMessageCount: sourceMessages.length,
      streamingSourcePlaceholders: sourceMessages.filter(
        (message) => message.status === "streaming",
      ).length,
      sourceMessageStatus: sourceMessage?.status,
      sourceMessageArtifactId: sourceMessage?.artifact_id,
      pending: settled.pending,
      runId: settled.run_id,
    });
  }

  assert.deepEqual(observations, [
    {
      sourcePhase: "no-progress",
      during: {
        sourceListenerOpen: true,
        pending: true,
        runId: "run_step4d_source_no-progress",
        sourceProgressRunId: undefined,
        forkProgressRunId: "run_step4d_fork_no-progress",
      },
      activeArtifactId: "art_step4d_fork_draft_no-progress",
      sourceArtifactRetained: true,
      sourceMessageCount: 1,
      streamingSourcePlaceholders: 0,
      sourceMessageStatus: "done",
      sourceMessageArtifactId: "art_run_step4d_source_no-progress",
      pending: false,
      runId: undefined,
    },
    {
      sourcePhase: "terminal-artifact-held",
      during: {
        sourceListenerOpen: false,
        pending: true,
        runId: "run_step4d_source_terminal-artifact-held",
        sourceProgressRunId: "run_step4d_source_terminal-artifact-held",
        forkProgressRunId: "run_step4d_fork_terminal-artifact-held",
      },
      activeArtifactId: "art_step4d_fork_draft_terminal-artifact-held",
      sourceArtifactRetained: true,
      sourceMessageCount: 1,
      streamingSourcePlaceholders: 0,
      sourceMessageStatus: "done",
      sourceMessageArtifactId: "art_run_step4d_source_terminal-artifact-held",
      pending: false,
      runId: undefined,
    },
  ]);
});

test("Step4d stale progress alone never grants ordinary source ownership", async () => {
  const observations: Array<Record<string, unknown>> = [];

  for (const ownership of ["not-pending", "mismatched-run"] as const) {
    const conversationId = `step4d_stale_${ownership}`;
    const sourceRunId = `run_step4d_stale_source_${ownership}`;
    const forkRunId = `run_step4d_stale_fork_${ownership}`;
    const candidateId = `poster-step4d-stale-${ownership}`;
    const draft = step4dDraft(
      `step4d_stale_draft_${ownership}`,
      "poster",
      sourceRunId,
      candidateId,
    );
    resetStore({
      [conversationId]: conversation(conversationId, {
        pending: ownership === "mismatched-run",
        run_id: ownership === "mismatched-run" ? "run_new" : undefined,
      }),
    }, conversationId);
    useApp.setState({
      runs_progress: {
        [conversationId]: initialProgress(sourceRunId),
      },
    });
    globalThis.fetch = (async (input) => {
      const url = String(input);
      if (url === `/api/runs/${sourceRunId}/attempts/1/fork`) {
        return jsonResponse({
          run_id: forkRunId,
          start_token: "fork-token",
          progress_mode: "attempt_fork",
        });
      }
      if (url === `/api/runs/${forkRunId}/start`) {
        return jsonResponse({ run_id: forkRunId, progress_mode: "attempt_fork" });
      }
      if (url === `/api/runs/${forkRunId}/artifact`) {
        return jsonResponse({
          message: {
            id: `msg_${forkRunId}`,
            role: "assistant",
            text: "Candidate draft ready.",
            ts: 3,
            run_id: forkRunId,
            artifact_id: draft.artifact_id,
            status: "done",
          },
          artifact: draft,
        });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }) as typeof fetch;

    const opening = useApp.getState().openAttemptInCanvas(
      sourceRunId,
      attemptCandidate(sourceRunId, candidateId),
      conversationId,
    );
    await waitFor(
      () => Boolean(step4dEventSource(forkRunId)),
      `${ownership} fork listener did not open`,
    );
    const live = useApp.getState();
    observations.push({
      ownership,
      ordinaryRunId: live.conversations[conversationId].run_id,
      ordinaryProgressRunId: live.runs_progress[conversationId]?.run_id,
      derivedProgress: live.runs_progress[`${conversationId}:attempt-fork`],
    });
    step4dEventSource(forkRunId)?.emit("run.done");
    await opening;
  }

  assert.deepEqual(observations, [
    {
      ownership: "not-pending",
      ordinaryRunId: "run_step4d_stale_fork_not-pending",
      ordinaryProgressRunId: "run_step4d_stale_fork_not-pending",
      derivedProgress: undefined,
    },
    {
      ownership: "mismatched-run",
      ordinaryRunId: "run_step4d_stale_fork_mismatched-run",
      ordinaryProgressRunId: "run_step4d_stale_fork_mismatched-run",
      derivedProgress: undefined,
    },
  ]);
});

test("Step4d ordinary deck publication keeps its captured derived key and wins a late source artifact", async () => {
  const conversationId = "step4d_live_deck_publish";
  const sourceRunId = "run_step4d_live_deck_source";
  const publishRunId = "run_step4d_live_deck_publish";
  const sourceMessageId = "msg_step4d_live_deck_source";
  const candidateId = "deck-step4d-live-01";
  const draft = step4dDraft(
    "step4d_live_deck_draft",
    "deck",
    sourceRunId,
    candidateId,
  );
  const published = step4dPublished(
    "step4d_live_deck_published",
    "deck",
    sourceRunId,
    candidateId,
  );
  resetStore({
    [conversationId]: conversation(conversationId, {
      pending: true,
      run_id: sourceRunId,
      messages: [step4dSourceMessage(sourceMessageId, "deck")],
      artifacts: { [draft.artifact_id]: draft },
      active_artifact_id: draft.artifact_id,
    }),
  }, conversationId);
  useApp.setState({
    run_attempts: {
      [sourceRunId]: {
        run_id: sourceRunId,
        candidates: [{
          ...attemptCandidate(sourceRunId, candidateId),
          artifact_type: "deck",
        }],
        selection_phase: "idle",
        loading: false,
      },
    },
  });

  let sourceReads = 0;
  let releaseSourceArtifact: ((response: Response) => void) | undefined;
  let releasePublishStart: ((response: Response) => void) | undefined;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${sourceRunId}/artifact`) {
      sourceReads += 1;
      if (sourceReads === 1) {
        return jsonResponse({ detail: "source still running" }, 404);
      }
      return new Promise<Response>((resolve) => { releaseSourceArtifact = resolve; });
    }
    if (url === `/api/artifacts/${draft.artifact_id}/publish-candidate-draft`) {
      return jsonResponse({
        run_id: publishRunId,
        start_token: "publish-token",
        progress_mode: "attempt_publish",
      });
    }
    if (url === `/api/runs/${publishRunId}/start`) {
      return new Promise<Response>((resolve) => { releasePublishStart = resolve; });
    }
    if (url === `/api/runs/${publishRunId}/artifact`) {
      return jsonResponse({
        message: {
          id: "msg_step4d_live_deck_publish_result",
          role: "assistant",
          text: "Published selected attempt.",
          ts: 5,
          run_id: publishRunId,
          artifact_id: published.artifact_id,
          status: "done",
        },
        artifact: published,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  useApp.getState().recoverActiveRuns();
  await waitFor(
    () => Boolean(step4dEventSource(sourceRunId)),
    "deck source listener did not recover",
  );
  const publishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget());
  await waitFor(
    () => typeof releasePublishStart === "function",
    "publication /start acknowledgement was not held",
  );
  const beforeSourceTerminal = useApp.getState();
  const captured = {
    sourceListenerOpen: step4dEventSource(sourceRunId)?.readyState === 1,
    pending: beforeSourceTerminal.conversations[conversationId].pending,
    runId: beforeSourceTerminal.conversations[conversationId].run_id,
    sourceProgressRunId: beforeSourceTerminal.runs_progress[conversationId]?.run_id,
    publicationProgressRunId:
      beforeSourceTerminal.runs_progress[`${conversationId}:candidate-publish`]?.run_id,
  };
  step4dEventSource(sourceRunId)?.emit("run.done");
  await waitFor(
    () => typeof releaseSourceArtifact === "function",
    "source terminal artifact response was not held",
  );
  releasePublishStart?.(jsonResponse({
    run_id: publishRunId,
    progress_mode: "attempt_publish",
  }));
  await waitFor(
    () => Boolean(step4dEventSource(publishRunId)),
    "publication listener did not open after delayed acknowledgement",
  );
  const afterAck = useApp.getState();
  const capturedKeyStayedDerived =
    afterAck.runs_progress[`${conversationId}:candidate-publish`]?.run_id === publishRunId;
  step4dEventSource(publishRunId)?.emit("run.done");
  await publishing.catch(() => undefined);
  releaseSourceArtifact?.(
    jsonResponse(step4dSourceResult(sourceMessageId, sourceRunId, "deck")),
  );
  await settleStep4dTicks();

  const settled = useApp.getState().conversations[conversationId];
  const sourceMessages = settled.messages.filter(
    (message) => message.id === sourceMessageId,
  );
  const sourceMessage = sourceMessages[0];
  const publicationMessage = settled.messages.find((message) => message.run_id === publishRunId);
  assert.deepEqual({
    captured,
    capturedKeyStayedDerived,
    activeArtifactId: settled.active_artifact_id,
    publishedArtifactId: settled.published_artifact_id,
    sourceArtifactRetained: Boolean(settled.artifacts[`art_${sourceRunId}`]),
    sourceMessageCount: sourceMessages.length,
    streamingSourcePlaceholders: sourceMessages.filter(
      (message) => message.status === "streaming",
    ).length,
    sourceMessageStatus: sourceMessage?.status,
    sourceMessageArtifactId: sourceMessage?.artifact_id,
    publicationStatus: publicationMessage?.status,
    publicationArtifactId: publicationMessage?.artifact_id,
    pending: settled.pending,
    runId: settled.run_id,
  }, {
    captured: {
      sourceListenerOpen: true,
      pending: true,
      runId: sourceRunId,
      sourceProgressRunId: sourceRunId,
      publicationProgressRunId: publishRunId,
    },
    capturedKeyStayedDerived: true,
    activeArtifactId: published.artifact_id,
    publishedArtifactId: published.artifact_id,
    sourceArtifactRetained: true,
    sourceMessageCount: 1,
    streamingSourcePlaceholders: 0,
    sourceMessageStatus: "done",
    sourceMessageArtifactId: `art_${sourceRunId}`,
    publicationStatus: "done",
    publicationArtifactId: published.artifact_id,
    pending: false,
    runId: undefined,
  });
});

test("Step4d rehydrate recovers legacy ordinary source and publication owners in both completion orders", async () => {
  const observations: Array<Record<string, unknown>> = [];

  for (const completionOrder of ["publication-first", "source-first"] as const) {
    const conversationId = `step4d_rehydrate_${completionOrder}`;
    const sourceRunId = `run_step4d_rehydrate_source_${completionOrder}`;
    const publishRunId = `run_step4d_rehydrate_publish_${completionOrder}`;
    const sourceMessageId = `msg_step4d_rehydrate_source_${completionOrder}`;
    const publicationMessageId = `msg_step4d_rehydrate_publish_${completionOrder}`;
    const candidateId = `deck-step4d-rehydrate-${completionOrder}`;
    const draft = step4dDraft(
      `step4d_rehydrate_draft_${completionOrder}`,
      "deck",
      sourceRunId,
      candidateId,
    );
    const published = step4dPublished(
      `step4d_rehydrate_published_${completionOrder}`,
      "deck",
      sourceRunId,
      candidateId,
    );
    const persisted = conversation(conversationId, {
      pending: true,
      run_id: sourceRunId,
      messages: [
        step4dSourceMessage(sourceMessageId, "deck"),
        {
          id: publicationMessageId,
          role: "assistant",
          text: "Publishing selected attempt.",
          ts: 2,
          run_id: publishRunId,
          artifact_id: draft.artifact_id,
          status: "streaming",
          task_type: "candidate_publish",
          task_payload: {
            artifact_type: "deck",
            source_artifact_id: draft.artifact_id,
            source_run_id: sourceRunId,
            source_candidate_id: candidateId,
          },
          source_artifact_id: draft.artifact_id,
        },
      ],
      artifacts: { [draft.artifact_id]: draft },
      active_artifact_id: draft.artifact_id,
    });
    resetStore({ base: conversation("base") }, "base");
    localStorage.setItem("autodesign.web.v1", JSON.stringify({
      version: 1,
      state: {
        conversations: { [conversationId]: persisted },
        current_conversation_id: conversationId,
        history_user_scope: "test-user",
      },
    }));

    const artifactReads: Record<string, number> = {
      [sourceRunId]: 0,
      [publishRunId]: 0,
    };
    globalThis.fetch = (async (input) => {
      const url = String(input);
      if (url.startsWith("/api/history")) {
        return jsonResponse({
          conversations: { [conversationId]: persisted },
          imported_runs: 0,
          user_isolated: true,
          request_scope: "test-user",
        });
      }
      if (url === `/api/runs/${sourceRunId}/artifact`) {
        artifactReads[sourceRunId] += 1;
        return artifactReads[sourceRunId] === 1
          ? jsonResponse({ detail: "source still running" }, 404)
          : jsonResponse(step4dSourceResult(sourceMessageId, sourceRunId, "deck"));
      }
      if (url === `/api/runs/${publishRunId}/artifact`) {
        artifactReads[publishRunId] += 1;
        return artifactReads[publishRunId] === 1
          ? jsonResponse({ detail: "publication still running" }, 404)
          : jsonResponse({
              message: {
                id: publicationMessageId,
                role: "assistant",
                text: "Published selected attempt.",
                ts: 5,
                run_id: publishRunId,
                artifact_id: published.artifact_id,
                status: "done",
              },
              artifact: published,
            });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }) as typeof fetch;

    await useApp.persist.rehydrate();
    await useApp.getState().loadServerHistory();
    await waitFor(
      () => Boolean(step4dEventSource(publishRunId)),
      `${completionOrder} publication listener did not recover`,
    );
    await settleStep4dTicks();
    const recovered = useApp.getState();
    const recovery = {
      sourceListeners: MockEventSource.instances.filter(
        (source) => source.url === `/api/runs/${sourceRunId}/events`,
      ).length,
      publicationListeners: MockEventSource.instances.filter(
        (source) => source.url === `/api/runs/${publishRunId}/events`,
      ).length,
      sourceProgressRunId: recovered.runs_progress[conversationId]?.run_id,
      publicationProgressRunId:
        recovered.runs_progress[`${conversationId}:candidate-publish`]?.run_id,
      sourcePlaceholderHasRunId: recovered.conversations[conversationId].messages.find(
        (message) => message.id === sourceMessageId,
      )?.run_id,
    };

    const orderedRuns = completionOrder === "publication-first"
      ? [publishRunId, sourceRunId]
      : [sourceRunId, publishRunId];
    step4dEventSource(orderedRuns[0])?.emit("run.done");
    await settleStep4dTicks();
    const afterFirst = useApp.getState();
    const intermediate = {
      activeArtifactId: afterFirst.conversations[conversationId].active_artifact_id,
      pending: afterFirst.conversations[conversationId].pending,
      runId: afterFirst.conversations[conversationId].run_id,
      sourceProgressRunId: afterFirst.runs_progress[conversationId]?.run_id,
      publicationProgressRunId:
        afterFirst.runs_progress[`${conversationId}:candidate-publish`]?.run_id,
      sourceListenerOpen: step4dEventSource(sourceRunId)?.readyState === 1,
      publicationListenerOpen: step4dEventSource(publishRunId)?.readyState === 1,
    };
    step4dEventSource(orderedRuns[1])?.emit("run.done");
    await settleStep4dTicks();
    const settled = useApp.getState().conversations[conversationId];
    const sourceMessages = settled.messages.filter(
      (message) => message.id === sourceMessageId,
    );
    const publicationMessages = settled.messages.filter(
      (message) => message.id === publicationMessageId,
    );
    observations.push({
      completionOrder,
      recovery,
      intermediate,
      activeArtifactId: settled.active_artifact_id,
      publishedArtifactId: settled.published_artifact_id,
      sourceArtifactRetained: Boolean(settled.artifacts[`art_${sourceRunId}`]),
      sourceMessages: sourceMessages.map((message) => ({
        id: message.id,
        runId: message.run_id,
        status: message.status,
        artifactId: message.artifact_id,
      })),
      streamingSourcePlaceholders: sourceMessages.filter(
        (message) => message.status === "streaming",
      ).length,
      publicationMessages: publicationMessages.map((message) => ({
        runId: message.run_id,
        status: message.status,
        artifactId: message.artifact_id,
      })),
      pending: settled.pending,
      runId: settled.run_id,
      sourceProgress: useApp.getState().runs_progress[conversationId],
      publicationProgress:
        useApp.getState().runs_progress[`${conversationId}:candidate-publish`],
      sourceClosed:
        step4dEventSource(sourceRunId)?.readyState === MockEventSource.CLOSED,
      publicationClosed:
        step4dEventSource(publishRunId)?.readyState === MockEventSource.CLOSED,
    });
  }

  assert.deepEqual(observations, [
    {
      completionOrder: "publication-first",
      recovery: {
        sourceListeners: 1,
        publicationListeners: 1,
        sourceProgressRunId: "run_step4d_rehydrate_source_publication-first",
        publicationProgressRunId: "run_step4d_rehydrate_publish_publication-first",
        sourcePlaceholderHasRunId: undefined,
      },
      intermediate: {
        activeArtifactId: "art_step4d_rehydrate_published_publication-first",
        pending: true,
        runId: "run_step4d_rehydrate_source_publication-first",
        sourceProgressRunId: "run_step4d_rehydrate_source_publication-first",
        publicationProgressRunId: undefined,
        sourceListenerOpen: true,
        publicationListenerOpen: false,
      },
      activeArtifactId: "art_step4d_rehydrate_published_publication-first",
      publishedArtifactId: "art_step4d_rehydrate_published_publication-first",
      sourceArtifactRetained: true,
      sourceMessages: [{
        id: "msg_step4d_rehydrate_source_publication-first",
        runId: "run_step4d_rehydrate_source_publication-first",
        status: "done",
        artifactId: "art_run_step4d_rehydrate_source_publication-first",
      }],
      streamingSourcePlaceholders: 0,
      publicationMessages: [{
        runId: "run_step4d_rehydrate_publish_publication-first",
        status: "done",
        artifactId: "art_step4d_rehydrate_published_publication-first",
      }],
      pending: false,
      runId: undefined,
      sourceProgress: undefined,
      publicationProgress: undefined,
      sourceClosed: true,
      publicationClosed: true,
    },
    {
      completionOrder: "source-first",
      recovery: {
        sourceListeners: 1,
        publicationListeners: 1,
        sourceProgressRunId: "run_step4d_rehydrate_source_source-first",
        publicationProgressRunId: "run_step4d_rehydrate_publish_source-first",
        sourcePlaceholderHasRunId: undefined,
      },
      intermediate: {
        activeArtifactId: "art_step4d_rehydrate_draft_source-first",
        pending: false,
        runId: undefined,
        sourceProgressRunId: undefined,
        publicationProgressRunId: "run_step4d_rehydrate_publish_source-first",
        sourceListenerOpen: false,
        publicationListenerOpen: true,
      },
      activeArtifactId: "art_step4d_rehydrate_published_source-first",
      publishedArtifactId: "art_step4d_rehydrate_published_source-first",
      sourceArtifactRetained: true,
      sourceMessages: [{
        id: "msg_step4d_rehydrate_source_source-first",
        runId: "run_step4d_rehydrate_source_source-first",
        status: "done",
        artifactId: "art_run_step4d_rehydrate_source_source-first",
      }],
      streamingSourcePlaceholders: 0,
      publicationMessages: [{
        runId: "run_step4d_rehydrate_publish_source-first",
        status: "done",
        artifactId: "art_step4d_rehydrate_published_source-first",
      }],
      pending: false,
      runId: undefined,
      sourceProgress: undefined,
      publicationProgress: undefined,
      sourceClosed: true,
      publicationClosed: true,
    },
  ]);
});

test("Step4d middleware persistence stamps and rehydrates isolated ordinary source and publication owners", async () => {
  const conversationId = "step4d_middleware_persist";
  const sourceRunId = "run_step4d_middleware_source";
  const publishRunId = "run_step4d_middleware_publish";
  const candidateId = "deck-step4d-middleware-01";
  const draft = step4dDraft(
    "step4d_middleware_draft",
    "deck",
    sourceRunId,
    candidateId,
  );
  const published = step4dPublished(
    "step4d_middleware_published",
    "deck",
    sourceRunId,
    candidateId,
  );
  resetStore({ [conversationId]: conversation(conversationId) }, conversationId);
  useApp.setState({ intent_type: "deck" });

  let phase: "live" | "recovery" = "live";
  let sourcePlaceholderId = "";
  let persistedConversation: Conversation | undefined;
  const recoveryArtifactReads = new Map<string, number>();
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === "/api/runs/reserve") {
      return jsonResponse({
        run_id: sourceRunId,
        upload_token: "source-upload-token",
        input_slots: [],
        request_digest: "source-request-digest",
        run_state: "reserved",
        expires_at: 123,
        reused: false,
      });
    }
    if (url === `/api/runs/${sourceRunId}/start`) {
      return jsonResponse({
        run_id: sourceRunId,
        progress_mode: "generate",
        placeholder_message: {
          id: "server-step4d-source-placeholder",
          role: "assistant",
          text: "",
          ts: 1,
          run_id: sourceRunId,
          status: "streaming",
        },
      });
    }
    if (url === `/api/artifacts/${draft.artifact_id}/publish-candidate-draft`) {
      return jsonResponse({
        run_id: publishRunId,
        start_token: "publication-start-token",
        progress_mode: "attempt_publish",
      });
    }
    if (url === `/api/runs/${publishRunId}/start`) {
      return jsonResponse({ run_id: publishRunId, progress_mode: "attempt_publish" });
    }
    if (url.startsWith("/api/history")) {
      return jsonResponse({
        conversations: persistedConversation
          ? { [conversationId]: persistedConversation }
          : {},
        imported_runs: 0,
        user_isolated: true,
        request_scope: "test-user",
      });
    }
    if (url === `/api/runs/${sourceRunId}/artifact`) {
      if (phase === "recovery") {
        const reads = (recoveryArtifactReads.get(sourceRunId) ?? 0) + 1;
        recoveryArtifactReads.set(sourceRunId, reads);
        if (reads === 1) return jsonResponse({ detail: "source still running" }, 404);
      }
      return jsonResponse(step4dSourceResult(
        sourcePlaceholderId,
        sourceRunId,
        "deck",
      ));
    }
    if (url === `/api/runs/${publishRunId}/artifact`) {
      if (phase === "recovery") {
        const reads = (recoveryArtifactReads.get(publishRunId) ?? 0) + 1;
        recoveryArtifactReads.set(publishRunId, reads);
        if (reads === 1) {
          return jsonResponse({ detail: "publication still running" }, 404);
        }
      }
      return jsonResponse({
        message: {
          id: "msg_step4d_middleware_publication_result",
          role: "assistant",
          text: "Published selected attempt.",
          ts: 4,
          run_id: publishRunId,
          artifact_id: published.artifact_id,
          status: "done",
        },
        artifact: published,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  let sendingSettled = false;
  const sending = useApp.getState().sendMessage("Create a deck.", [])
    .finally(() => { sendingSettled = true; });
  await waitFor(
    () => Boolean(step4dEventSource(sourceRunId)),
    "newly reserved source listener did not open",
  );
  const liveSourcePlaceholder = useApp.getState().conversations[conversationId]
    .messages.find((message) => (
      message.role === "assistant"
      && message.status === "streaming"
      && message.task_type === "generate"
    ));
  assert.ok(liveSourcePlaceholder);
  sourcePlaceholderId = liveSourcePlaceholder.id;
  useApp.setState((state) => ({
    conversations: {
      ...state.conversations,
      [conversationId]: {
        ...state.conversations[conversationId],
        artifacts: {
          ...state.conversations[conversationId].artifacts,
          [draft.artifact_id]: draft,
        },
        active_artifact_id: draft.artifact_id,
      },
    },
    run_attempts: {
      [sourceRunId]: {
        run_id: sourceRunId,
        candidates: [{
          ...attemptCandidate(sourceRunId, candidateId),
          artifact_type: "deck",
        }],
        selection_phase: "idle",
        loading: false,
      },
    },
  }));
  let publishingSettled = false;
  const publishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget())
    .finally(() => { publishingSettled = true; });
  await waitFor(
    () => Boolean(step4dEventSource(publishRunId)),
    "newer publication listener did not open",
  );
  await settleStep4dTicks();
  const persistedBytes = localStorage.getItem("autodesign.web.v1");
  assert.ok(persistedBytes);
  persistedConversation = JSON.parse(persistedBytes).state
    .conversations[conversationId] as Conversation;
  const serializedSource = persistedConversation.messages.find(
    (message) => message.id === sourcePlaceholderId,
  );
  const serializedPublication = persistedConversation.messages.find(
    (message) => message.run_id === publishRunId,
  );
  const serializedObservation = {
    liveSourcePlaceholderRunId: liveSourcePlaceholder.run_id,
    pending: persistedConversation.pending,
    runId: persistedConversation.run_id,
    sourcePlaceholderRunId: serializedSource?.run_id,
    sourcePlaceholderStatus: serializedSource?.status,
    publicationRunId: serializedPublication?.run_id,
    publicationStatus: serializedPublication?.status,
    publicationIsNewer: persistedConversation.messages.indexOf(serializedPublication!)
      > persistedConversation.messages.indexOf(serializedSource!),
    sourceListenerOpen: step4dEventSource(sourceRunId)?.readyState === 1,
    publicationProgressRunId:
      useApp.getState().runs_progress[`${conversationId}:candidate-publish`]?.run_id,
  };

  step4dEventSource(publishRunId)?.emit("run.done");
  await settleStep4dTicks();
  step4dEventSource(sourceRunId)?.emit("run.done");
  await settleStep4dTicks();
  const liveFlowsSettled = { sendingSettled, publishingSettled };
  void Promise.allSettled([sending, publishing]);

  phase = "recovery";
  resetStore({ base: conversation("base") }, "base");
  localStorage.setItem("autodesign.web.v1", persistedBytes);
  await useApp.persist.rehydrate();
  await useApp.getState().loadServerHistory();
  await waitFor(
    () => Boolean(step4dEventSource(publishRunId)),
    "persisted publication listener did not recover",
  );
  await settleStep4dTicks();
  const recovered = useApp.getState();
  const recoveryObservation = {
    sourceListeners: MockEventSource.instances.filter(
      (source) => source.url === `/api/runs/${sourceRunId}/events`,
    ).length,
    publicationListeners: MockEventSource.instances.filter(
      (source) => source.url === `/api/runs/${publishRunId}/events`,
    ).length,
    pending: recovered.conversations[conversationId].pending,
    runId: recovered.conversations[conversationId].run_id,
    sourceProgressRunId: recovered.runs_progress[conversationId]?.run_id,
    publicationProgressRunId:
      recovered.runs_progress[`${conversationId}:candidate-publish`]?.run_id,
  };
  step4dEventSource(publishRunId)?.emit("run.done");
  await settleStep4dTicks();
  step4dEventSource(sourceRunId)?.emit("run.done");
  await settleStep4dTicks();
  const settled = useApp.getState().conversations[conversationId];

  assert.deepEqual({
    serialized: serializedObservation,
    liveFlowsSettled,
    recovery: recoveryObservation,
    activeArtifactId: settled.active_artifact_id,
    sourceArtifactRetained: Boolean(settled.artifacts[`art_${sourceRunId}`]),
    sourceMessages: settled.messages.filter(
      (message) => message.id === sourcePlaceholderId,
    ).map((message) => ({
      id: message.id,
      runId: message.run_id,
      status: message.status,
      artifactId: message.artifact_id,
    })),
    streamingSourcePlaceholders: settled.messages.filter(
      (message) => message.id === sourcePlaceholderId && message.status === "streaming",
    ).length,
  }, {
    serialized: {
      liveSourcePlaceholderRunId: sourceRunId,
      pending: true,
      runId: sourceRunId,
      sourcePlaceholderRunId: sourceRunId,
      sourcePlaceholderStatus: "streaming",
      publicationRunId: publishRunId,
      publicationStatus: "streaming",
      publicationIsNewer: true,
      sourceListenerOpen: true,
      publicationProgressRunId: publishRunId,
    },
    liveFlowsSettled: {
      sendingSettled: true,
      publishingSettled: true,
    },
    recovery: {
      sourceListeners: 1,
      publicationListeners: 1,
      pending: true,
      runId: sourceRunId,
      sourceProgressRunId: sourceRunId,
      publicationProgressRunId: publishRunId,
    },
    activeArtifactId: published.artifact_id,
    sourceArtifactRetained: true,
    sourceMessages: [{
      id: sourcePlaceholderId,
      runId: sourceRunId,
      status: "done",
      artifactId: `art_${sourceRunId}`,
    }],
    streamingSourcePlaceholders: 0,
  });
});

test("Step4d recovered publication uses the ordinary slot only when it owns pending run_id", async () => {
  const observations: Array<Record<string, unknown>> = [];

  for (const ownership of [
    "source-terminal",
    "publication-owned",
    "replacement-owned",
  ] as const) {
    const conversationId = `step4d_publish_route_${ownership}`;
    const sourceRunId = `run_step4d_publish_route_source_${ownership}`;
    const publishRunId = `run_step4d_publish_route_publish_${ownership}`;
    const candidateId = `deck-step4d-route-${ownership}`;
    const draft = step4dDraft(
      `step4d_publish_route_draft_${ownership}`,
      "deck",
      sourceRunId,
      candidateId,
    );
    const published = step4dPublished(
      `step4d_publish_route_published_${ownership}`,
      "deck",
      sourceRunId,
      candidateId,
    );
    resetStore({
      [conversationId]: conversation(conversationId, {
        pending: ownership !== "source-terminal",
        run_id: ownership === "publication-owned"
          ? publishRunId
          : ownership === "replacement-owned"
            ? "run_new"
            : undefined,
        messages: [
          {
            id: `msg_${sourceRunId}`,
            role: "assistant",
            text: "Source complete.",
            ts: 1,
            run_id: sourceRunId,
            artifact_id: `art_${sourceRunId}`,
            status: "done",
            task_type: "generate",
            task_payload: { artifact_type: "deck" },
          },
          {
            id: `msg_${publishRunId}`,
            role: "assistant",
            text: "Publishing selected attempt.",
            ts: 2,
            run_id: publishRunId,
            artifact_id: draft.artifact_id,
            status: "streaming",
            task_type: "candidate_publish",
            task_payload: {
              artifact_type: "deck",
              source_artifact_id: draft.artifact_id,
              source_run_id: sourceRunId,
              source_candidate_id: candidateId,
            },
            source_artifact_id: draft.artifact_id,
          },
        ],
        artifacts: {
          [`art_${sourceRunId}`]: artifact(sourceRunId, "deck"),
          [draft.artifact_id]: draft,
        },
        active_artifact_id: draft.artifact_id,
      }),
    }, conversationId);
    if (ownership === "replacement-owned") {
      useApp.setState({
        runs_progress: {
          [conversationId]: initialProgress("run_new"),
        },
      });
    }

    let artifactReads = 0;
    globalThis.fetch = (async (input) => {
      const url = String(input);
      if (url === `/api/runs/${publishRunId}/artifact`) {
        artifactReads += 1;
        return artifactReads === 1
          ? jsonResponse({ detail: "publication still running" }, 404)
          : jsonResponse({
              message: {
                id: `msg_${publishRunId}`,
                role: "assistant",
                text: "Published selected attempt.",
                ts: 3,
                run_id: publishRunId,
                artifact_id: published.artifact_id,
                status: "done",
              },
              artifact: published,
            });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }) as typeof fetch;

    useApp.getState().recoverActiveRuns();
    await waitFor(
      () => Boolean(step4dEventSource(publishRunId)),
      `${ownership} publication listener did not recover`,
    );
    const live = useApp.getState();
    const beforeTerminal = {
      ordinaryProgressRunId: live.runs_progress[conversationId]?.run_id,
      derivedProgressRunId:
        live.runs_progress[`${conversationId}:candidate-publish`]?.run_id,
      pending: live.conversations[conversationId].pending,
      runId: live.conversations[conversationId].run_id,
    };
    step4dEventSource(publishRunId)?.emit("run.done");
    await settleStep4dTicks();
    const settled = useApp.getState();
    observations.push({
      ownership,
      beforeTerminal,
      activeArtifactId: settled.conversations[conversationId].active_artifact_id,
      pending: settled.conversations[conversationId].pending,
      runId: settled.conversations[conversationId].run_id,
      ordinaryProgressRunId: settled.runs_progress[conversationId]?.run_id,
      derivedProgress:
        settled.runs_progress[`${conversationId}:candidate-publish`],
      publicationClosed:
        step4dEventSource(publishRunId)?.readyState === MockEventSource.CLOSED,
    });
  }

  const expectedObservations = [
    {
      ownership: "source-terminal",
      beforeTerminal: {
        ordinaryProgressRunId: undefined,
        derivedProgressRunId: "run_step4d_publish_route_publish_source-terminal",
        pending: false,
        runId: undefined,
      },
      activeArtifactId: "art_step4d_publish_route_published_source-terminal",
      pending: false,
      runId: undefined,
      ordinaryProgressRunId: undefined,
      derivedProgress: undefined,
      publicationClosed: true,
    },
    {
      ownership: "publication-owned",
      beforeTerminal: {
        ordinaryProgressRunId: "run_step4d_publish_route_publish_publication-owned",
        derivedProgressRunId: undefined,
        pending: true,
        runId: "run_step4d_publish_route_publish_publication-owned",
      },
      activeArtifactId: "art_step4d_publish_route_published_publication-owned",
      pending: false,
      runId: undefined,
      ordinaryProgressRunId: undefined,
      derivedProgress: undefined,
      publicationClosed: true,
    },
    {
      ownership: "replacement-owned",
      beforeTerminal: {
        ordinaryProgressRunId: "run_new",
        derivedProgressRunId: "run_step4d_publish_route_publish_replacement-owned",
        pending: true,
        runId: "run_new",
      },
      activeArtifactId: "art_step4d_publish_route_published_replacement-owned",
      pending: true,
      runId: "run_new",
      ordinaryProgressRunId: "run_new",
      derivedProgress: undefined,
      publicationClosed: true,
    },
  ];

  const cancelConversationId = "step4d_publish_route_source_terminal_cancel";
  const cancelSourceRunId = "run_step4d_publish_route_source_terminal_cancel";
  const cancelPublishRunId = "run_step4d_publish_route_publish_terminal_cancel";
  const cancelDraft = step4dDraft(
    "step4d_publish_route_draft_terminal_cancel",
    "deck",
    cancelSourceRunId,
    "deck-step4d-route-terminal-cancel",
  );
  resetStore({
    [cancelConversationId]: conversation(cancelConversationId, {
      pending: false,
      messages: [
        {
          id: `msg_${cancelSourceRunId}`,
          role: "assistant",
          text: "Source complete.",
          ts: 1,
          run_id: cancelSourceRunId,
          artifact_id: `art_${cancelSourceRunId}`,
          status: "done",
          task_type: "generate",
          task_payload: { artifact_type: "deck" },
        },
        {
          id: `msg_${cancelPublishRunId}`,
          role: "assistant",
          text: "Publishing selected attempt.",
          ts: 2,
          run_id: cancelPublishRunId,
          artifact_id: cancelDraft.artifact_id,
          status: "streaming",
          task_type: "candidate_publish",
          task_payload: {
            artifact_type: "deck",
            source_artifact_id: cancelDraft.artifact_id,
            source_run_id: cancelSourceRunId,
            source_candidate_id: "deck-step4d-route-terminal-cancel",
          },
          source_artifact_id: cancelDraft.artifact_id,
        },
      ],
      artifacts: { [cancelDraft.artifact_id]: cancelDraft },
      active_artifact_id: cancelDraft.artifact_id,
    }),
  }, cancelConversationId);
  let cancelArtifactReads = 0;
  const terminalPublicationCancels: string[] = [];
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${cancelPublishRunId}/artifact`) {
      cancelArtifactReads += 1;
      return cancelArtifactReads === 1
        ? jsonResponse({ detail: "publication still running" }, 404)
        : jsonResponse({
            message: {
              id: `msg_${cancelPublishRunId}`,
              role: "assistant",
              text: "Run cancelled.",
              ts: 3,
              run_id: cancelPublishRunId,
              status: "error",
              failure: { status: "cancelled", produced_files: [] },
            },
            artifact: null,
          });
    }
    if (url === `/api/runs/${cancelPublishRunId}/cancel`) {
      terminalPublicationCancels.push(cancelPublishRunId);
      return jsonResponse({
        run_id: cancelPublishRunId,
        status: "already_cancelled",
        run_state: "cancelled",
        confirmed: true,
        terminated_pids: [],
        surviving_pids: [],
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;
  useApp.getState().recoverActiveRuns();
  await waitFor(
    () => Boolean(step4dEventSource(cancelPublishRunId)),
    "source-terminal publication listener did not recover",
  );
  await useApp.getState().cancelRun(cancelConversationId);
  await settleStep4dTicks();
  const cancelled = useApp.getState();
  const cancellationObservation = {
    cancels: terminalPublicationCancels,
    messageFailure: cancelled.conversations[cancelConversationId].messages.find(
      (message) => message.id === `msg_${cancelPublishRunId}`,
    )?.failure?.status,
    progress: cancelled.runs_progress[`${cancelConversationId}:candidate-publish`],
    listenerClosed:
      step4dEventSource(cancelPublishRunId)?.readyState === MockEventSource.CLOSED,
    activeArtifactId: cancelled.conversations[cancelConversationId].active_artifact_id,
    artifactReads: cancelArtifactReads,
  };
  if (step4dEventSource(cancelPublishRunId)?.readyState !== MockEventSource.CLOSED) {
    step4dEventSource(cancelPublishRunId)?.emit("run.cancelled");
    await settleStep4dTicks();
  }
  assert.deepEqual({
    routes: observations,
    cancellation: cancellationObservation,
  }, {
    routes: expectedObservations,
    cancellation: {
      cancels: [cancelPublishRunId],
      messageFailure: "cancelled",
      progress: undefined,
      listenerClosed: true,
      activeArtifactId: cancelDraft.artifact_id,
      artifactReads: 1,
    },
  });
});

test("Step4d stale source responses are inert while wrong-lineage artifacts allow source activation", async () => {
  const staleConversationId = "step4d_stale_response";
  const staleSourceRunId = "run_step4d_stale_response_source";
  const staleMessageId = "msg_step4d_stale_response_source";
  resetStore({
    [staleConversationId]: conversation(staleConversationId, {
      pending: true,
      run_id: staleSourceRunId,
      messages: [step4dSourceMessage(staleMessageId, "poster")],
    }),
  }, staleConversationId);
  let staleReads = 0;
  let releaseStaleArtifact: ((response: Response) => void) | undefined;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${staleSourceRunId}/artifact`) {
      staleReads += 1;
      return staleReads === 1
        ? jsonResponse({ detail: "source still running" }, 404)
        : new Promise<Response>((resolve) => { releaseStaleArtifact = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;
  useApp.getState().recoverActiveRuns();
  await waitFor(
    () => Boolean(step4dEventSource(staleSourceRunId)),
    "stale source listener did not recover",
  );
  step4dEventSource(staleSourceRunId)?.emit("run.done");
  await waitFor(
    () => typeof releaseStaleArtifact === "function",
    "stale source artifact response was not in flight",
  );
  useApp.setState((state) => ({
    conversations: {
      ...state.conversations,
      [staleConversationId]: {
        ...state.conversations[staleConversationId],
        pending: true,
        run_id: "run_new",
      },
    },
    runs_progress: {
      ...state.runs_progress,
      [staleConversationId]: initialProgress("run_new"),
      [`${staleConversationId}:candidate-publish`]: initialProgress(
        "run_unrelated_derived",
        "attempt_publish",
      ),
    },
  }));
  const staleConversationBefore = structuredClone(
    useApp.getState().conversations[staleConversationId],
  );
  const staleProgressBefore = structuredClone(useApp.getState().runs_progress);
  releaseStaleArtifact?.(jsonResponse(step4dSourceResult(
    staleMessageId,
    staleSourceRunId,
    "poster",
  )));
  await settleStep4dTicks();
  const staleConversationAfter = useApp.getState().conversations[staleConversationId];
  const staleProgressAfter = useApp.getState().runs_progress;

  const activationObservations: Array<Record<string, unknown>> = [];
  for (const activeKind of ["wrong-draft", "wrong-published"] as const) {
    const conversationId = `step4d_wrong_lineage_${activeKind}`;
    const sourceRunId = `run_step4d_wrong_lineage_source_${activeKind}`;
    const sourceMessageId = `msg_step4d_wrong_lineage_source_${activeKind}`;
    const wrong = activeKind === "wrong-draft"
      ? step4dDraft(
          `step4d_wrong_draft_${activeKind}`,
          "poster",
          "run_unrelated",
          "poster-unrelated",
        )
      : step4dPublished(
          `step4d_wrong_published_${activeKind}`,
          "poster",
          "run_unrelated",
          "poster-unrelated",
        );
    resetStore({
      [conversationId]: conversation(conversationId, {
        pending: true,
        run_id: sourceRunId,
        messages: [step4dSourceMessage(sourceMessageId, "poster")],
        artifacts: { [wrong.artifact_id]: wrong },
        active_artifact_id: wrong.artifact_id,
        published_artifact_id:
          activeKind === "wrong-published" ? wrong.artifact_id : undefined,
      }),
    }, conversationId);
    let sourceReads = 0;
    globalThis.fetch = (async (input) => {
      const url = String(input);
      if (url === `/api/runs/${sourceRunId}/artifact`) {
        sourceReads += 1;
        return sourceReads === 1
          ? jsonResponse({ detail: "source still running" }, 404)
          : jsonResponse(step4dSourceResult(sourceMessageId, sourceRunId, "poster"));
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }) as typeof fetch;
    useApp.getState().recoverActiveRuns();
    await waitFor(
      () => Boolean(step4dEventSource(sourceRunId)),
      `${activeKind} source listener did not recover`,
    );
    step4dEventSource(sourceRunId)?.emit("run.done");
    await settleStep4dTicks();
    const settled = useApp.getState().conversations[conversationId];
    activationObservations.push({
      activeKind,
      activeArtifactId: settled.active_artifact_id,
      wrongArtifactRetained: Boolean(settled.artifacts[wrong.artifact_id]),
      sourceArtifactRetained: Boolean(settled.artifacts[`art_${sourceRunId}`]),
    });
  }

  assert.deepEqual(staleConversationAfter, staleConversationBefore);
  assert.deepEqual(staleProgressAfter, staleProgressBefore);
  assert.deepEqual(activationObservations, [
    {
      activeKind: "wrong-draft",
      activeArtifactId: "art_run_step4d_wrong_lineage_source_wrong-draft",
      wrongArtifactRetained: true,
      sourceArtifactRetained: true,
    },
    {
      activeKind: "wrong-published",
      activeArtifactId: "art_run_step4d_wrong_lineage_source_wrong-published",
      wrongArtifactRetained: true,
      sourceArtifactRetained: true,
    },
  ]);
});

test("Step4d ordinary cancel settles live, recovered, pre-ACK, and retryable publication owners exactly", async () => {
  const liveObservations: Array<Record<string, unknown>> = [];

  for (const cancellationMode of [
    "confirmed",
    "unconfirmed",
    "source-unconfirmed",
  ] as const) {
    const conversationId = `step4d_publish_cancel_${cancellationMode}`;
    const sourceRunId = `run_step4d_publish_cancel_source_${cancellationMode}`;
    const publishRunId = `run_step4d_publish_cancel_publish_${cancellationMode}`;
    const sourceMessageId = `msg_step4d_publish_cancel_source_${cancellationMode}`;
    const candidateId = `deck-step4d-cancel-${cancellationMode}`;
    const draft = step4dDraft(
      `step4d_publish_cancel_draft_${cancellationMode}`,
      "deck",
      sourceRunId,
      candidateId,
    );
    const published = step4dPublished(
      `step4d_publish_cancel_late_${cancellationMode}`,
      "deck",
      sourceRunId,
      candidateId,
    );
    resetStore({
      [conversationId]: conversation(conversationId, {
        pending: true,
        run_id: sourceRunId,
        messages: [step4dSourceMessage(sourceMessageId, "deck", sourceRunId)],
        artifacts: { [draft.artifact_id]: draft },
        active_artifact_id: draft.artifact_id,
      }),
    }, conversationId);
    useApp.setState({
      run_attempts: {
        [sourceRunId]: {
          run_id: sourceRunId,
          candidates: [{
            ...attemptCandidate(sourceRunId, candidateId),
            artifact_type: "deck",
          }],
          selection_phase: "idle",
          loading: false,
        },
      },
    });

    let sourceArtifactReads = 0;
    let publicationArtifactReads = 0;
    let retrying = false;
    const cancelRequests: string[] = [];
    globalThis.fetch = (async (input) => {
      const url = String(input);
      if (url === `/api/runs/${sourceRunId}/artifact`) {
        sourceArtifactReads += 1;
        return jsonResponse({ detail: "source still running" }, 404);
      }
      if (url === `/api/artifacts/${draft.artifact_id}/publish-candidate-draft`) {
        return jsonResponse({
          run_id: publishRunId,
          start_token: "publish-token",
          progress_mode: "attempt_publish",
        });
      }
      if (url === `/api/runs/${publishRunId}/start`) {
        return jsonResponse({ run_id: publishRunId, progress_mode: "attempt_publish" });
      }
      if (url === `/api/runs/${publishRunId}/artifact`) {
        publicationArtifactReads += 1;
        return jsonResponse({
          message: {
            id: `msg_${publishRunId}`,
            role: "assistant",
            text: "Late publication result.",
            ts: 5,
            run_id: publishRunId,
            artifact_id: published.artifact_id,
            status: "done",
          },
          artifact: published,
        });
      }
      const cancel = url.match(/^\/api\/runs\/([^/]+)\/cancel$/);
      if (cancel) {
        cancelRequests.push(cancel[1]);
        if (
          (
            (cancellationMode === "unconfirmed" && cancel[1] === publishRunId)
            || (
              cancellationMode === "source-unconfirmed"
              && cancel[1] === sourceRunId
            )
          )
          && !retrying
        ) {
          return jsonResponse({
            run_id: cancel[1],
            status: "cancellation_pending",
            run_state: "cancelling",
            confirmed: false,
            terminated_pids: [],
            surviving_pids: [],
          }, 202);
        }
        return confirmedCancellation(cancel[1]);
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }) as typeof fetch;

    useApp.getState().recoverActiveRuns();
    await waitFor(
      () => Boolean(step4dEventSource(sourceRunId)),
      `${cancellationMode} source listener did not recover`,
    );
    const publishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget());
    await waitFor(
      () => Boolean(step4dEventSource(publishRunId)),
      `${cancellationMode} publication listener did not open`,
    );
    await useApp.getState().cancelRun(conversationId);
    await settleStep4dTicks();

    const afterCancelState = useApp.getState();
    const afterCancel = afterCancelState.conversations[conversationId];
    const sourceMessage = afterCancel.messages.find(
      (message) => message.id === sourceMessageId,
    );
    const publicationMessage = afterCancel.messages.find(
      (message) => message.run_id === publishRunId,
    );
    const sourceProgress = afterCancelState.runs_progress[conversationId];
    const observation: Record<string, unknown> = {
      cancellationMode,
      cancelRequests: [...cancelRequests].sort(),
      sourceMessage: sourceMessage && {
        id: sourceMessage.id,
        runId: sourceMessage.run_id,
        status: sourceMessage.status,
        failure: sourceMessage.failure?.status,
      },
      publicationMessage: publicationMessage && {
        runId: publicationMessage.run_id,
        status: publicationMessage.status,
        failure: publicationMessage.failure?.status,
      },
      pending: afterCancel.pending,
      runId: afterCancel.run_id,
      sourceProgressRunId: sourceProgress?.run_id,
      sourceProgressPhase: sourceProgress?.phase,
      sourceProgressRetryable: /not confirmed/i.test(sourceProgress?.label ?? ""),
      publicationProgress: afterCancelState
        .runs_progress[`${conversationId}:candidate-publish`]?.phase,
      sourceClosed:
        step4dEventSource(sourceRunId)?.readyState === MockEventSource.CLOSED,
      publicationClosed:
        step4dEventSource(publishRunId)?.readyState === MockEventSource.CLOSED,
      publicationOwnerActive:
        candidatePublicationIsActive(afterCancelState, conversationId),
    };
    liveObservations.push(observation);

    if (cancellationMode !== "source-unconfirmed") {
      step4dEventSource(sourceRunId)?.emit("run.done");
    }
    step4dEventSource(publishRunId)?.emit("run.done");
    await settleStep4dTicks();
    const afterLate = useApp.getState().conversations[conversationId];
    observation.lateResultApplied =
      afterLate.active_artifact_id === published.artifact_id
      || Boolean(afterLate.artifacts[published.artifact_id]);
    if (cancellationMode !== "confirmed") {
      retrying = true;
      await useApp.getState().cancelRun(conversationId);
    }
    await publishing.catch(() => undefined);
    await settleStep4dTicks();
    observation.sourceArtifactReads = sourceArtifactReads;
    observation.publicationArtifactReads = publicationArtifactReads;
    observation.finalCancelRequests = [...cancelRequests].sort();
    observation.publicationOwnerActiveAfterRetry =
      candidatePublicationIsActive(useApp.getState(), conversationId);
    const finalState = useApp.getState();
    observation.finalSourceFailure = finalState.conversations[conversationId]
      .messages.find((message) => message.id === sourceMessageId)?.failure?.status;
    observation.finalSourceProgress = finalState.runs_progress[conversationId];
    observation.sourceClosedAfterRetry =
      step4dEventSource(sourceRunId)?.readyState === MockEventSource.CLOSED;
  }

  const expectedLiveObservations = [
    {
      cancellationMode: "confirmed",
      cancelRequests: [
        "run_step4d_publish_cancel_publish_confirmed",
        "run_step4d_publish_cancel_source_confirmed",
      ].sort(),
      sourceMessage: {
        id: "msg_step4d_publish_cancel_source_confirmed",
        runId: "run_step4d_publish_cancel_source_confirmed",
        status: "error",
        failure: "cancelled",
      },
      publicationMessage: {
        runId: "run_step4d_publish_cancel_publish_confirmed",
        status: "error",
        failure: "cancelled",
      },
      pending: false,
      runId: undefined,
      sourceProgressRunId: undefined,
      sourceProgressPhase: undefined,
      sourceProgressRetryable: false,
      publicationProgress: undefined,
      sourceClosed: true,
      publicationClosed: true,
      publicationOwnerActive: false,
      lateResultApplied: false,
      sourceArtifactReads: 1,
      publicationArtifactReads: 0,
      finalCancelRequests: [
        "run_step4d_publish_cancel_publish_confirmed",
        "run_step4d_publish_cancel_source_confirmed",
      ].sort(),
      publicationOwnerActiveAfterRetry: false,
      finalSourceFailure: "cancelled",
      finalSourceProgress: undefined,
      sourceClosedAfterRetry: true,
    },
    {
      cancellationMode: "unconfirmed",
      cancelRequests: [
        "run_step4d_publish_cancel_publish_unconfirmed",
        "run_step4d_publish_cancel_source_unconfirmed",
      ].sort(),
      sourceMessage: {
        id: "msg_step4d_publish_cancel_source_unconfirmed",
        runId: "run_step4d_publish_cancel_source_unconfirmed",
        status: "error",
        failure: "cancelled",
      },
      publicationMessage: {
        runId: "run_step4d_publish_cancel_publish_unconfirmed",
        status: "streaming",
        failure: undefined,
      },
      pending: false,
      runId: undefined,
      sourceProgressRunId: undefined,
      sourceProgressPhase: undefined,
      sourceProgressRetryable: false,
      publicationProgress: "cancelling",
      sourceClosed: true,
      publicationClosed: false,
      publicationOwnerActive: true,
      lateResultApplied: false,
      sourceArtifactReads: 1,
      publicationArtifactReads: 1,
      finalCancelRequests: [
        "run_step4d_publish_cancel_publish_unconfirmed",
        "run_step4d_publish_cancel_publish_unconfirmed",
        "run_step4d_publish_cancel_source_unconfirmed",
      ].sort(),
      publicationOwnerActiveAfterRetry: false,
      finalSourceFailure: "cancelled",
      finalSourceProgress: undefined,
      sourceClosedAfterRetry: true,
    },
    {
      cancellationMode: "source-unconfirmed",
      cancelRequests: [
        "run_step4d_publish_cancel_publish_source-unconfirmed",
        "run_step4d_publish_cancel_source_source-unconfirmed",
      ].sort(),
      sourceMessage: {
        id: "msg_step4d_publish_cancel_source_source-unconfirmed",
        runId: "run_step4d_publish_cancel_source_source-unconfirmed",
        status: "streaming",
        failure: undefined,
      },
      publicationMessage: {
        runId: "run_step4d_publish_cancel_publish_source-unconfirmed",
        status: "error",
        failure: "cancelled",
      },
      pending: true,
      runId: "run_step4d_publish_cancel_source_source-unconfirmed",
      sourceProgressRunId: "run_step4d_publish_cancel_source_source-unconfirmed",
      sourceProgressPhase: "cancelling",
      sourceProgressRetryable: true,
      publicationProgress: undefined,
      sourceClosed: false,
      publicationClosed: true,
      publicationOwnerActive: false,
      lateResultApplied: false,
      sourceArtifactReads: 1,
      publicationArtifactReads: 0,
      finalCancelRequests: [
        "run_step4d_publish_cancel_publish_source-unconfirmed",
        "run_step4d_publish_cancel_source_source-unconfirmed",
        "run_step4d_publish_cancel_source_source-unconfirmed",
      ].sort(),
      publicationOwnerActiveAfterRetry: false,
      finalSourceFailure: "cancelled",
      finalSourceProgress: undefined,
      sourceClosedAfterRetry: true,
    },
  ];

  const recoveredConversationId = "step4d_publish_cancel_recovered";
  const recoveredSourceRunId = "run_step4d_publish_cancel_recovered_source";
  const recoveredPublishRunId = "run_step4d_publish_cancel_recovered_publish";
  const recoveredSourceMessageId = "msg_step4d_publish_cancel_recovered_source";
  const recoveredPublishMessageId = "msg_step4d_publish_cancel_recovered_publish";
  const recoveredDraft = step4dDraft(
    "step4d_publish_cancel_recovered_draft",
    "deck",
    recoveredSourceRunId,
    "deck-step4d-cancel-recovered",
  );
  const recoveredConversation = conversation(recoveredConversationId, {
    pending: true,
    run_id: recoveredSourceRunId,
    messages: [
      step4dSourceMessage(recoveredSourceMessageId, "deck"),
      {
        id: recoveredPublishMessageId,
        role: "assistant",
        text: "Publishing selected attempt.",
        ts: 2,
        run_id: recoveredPublishRunId,
        artifact_id: recoveredDraft.artifact_id,
        status: "streaming",
        task_type: "candidate_publish",
        task_payload: {
          artifact_type: "deck",
          source_artifact_id: recoveredDraft.artifact_id,
          source_run_id: recoveredSourceRunId,
          source_candidate_id: "deck-step4d-cancel-recovered",
        },
        source_artifact_id: recoveredDraft.artifact_id,
      },
    ],
    artifacts: { [recoveredDraft.artifact_id]: recoveredDraft },
    active_artifact_id: recoveredDraft.artifact_id,
  });
  resetStore({ base: conversation("base") }, "base");
  localStorage.setItem("autodesign.web.v1", JSON.stringify({
    version: 1,
    state: {
      conversations: { [recoveredConversationId]: recoveredConversation },
      current_conversation_id: recoveredConversationId,
      history_user_scope: "test-user",
    },
  }));
  const recoveredCancels: string[] = [];
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({
        conversations: { [recoveredConversationId]: recoveredConversation },
        imported_runs: 0,
        user_isolated: true,
        request_scope: "test-user",
      });
    }
    if (
      url === `/api/runs/${recoveredSourceRunId}/artifact`
      || url === `/api/runs/${recoveredPublishRunId}/artifact`
    ) {
      return jsonResponse({ detail: "run still active" }, 404);
    }
    const cancel = url.match(/^\/api\/runs\/([^/]+)\/cancel$/);
    if (cancel) {
      recoveredCancels.push(cancel[1]);
      return confirmedCancellation(cancel[1]);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;
  await useApp.persist.rehydrate();
  await useApp.getState().loadServerHistory();
  await settleStep4dTicks();
  await useApp.getState().cancelRun(recoveredConversationId);
  await settleStep4dTicks();
  const recoveredSettled = useApp.getState().conversations[recoveredConversationId];
  const recoveredObservation = {
    listeners: [recoveredSourceRunId, recoveredPublishRunId].map(
      (runId) => step4dEventSource(runId)?.readyState,
    ),
    cancels: recoveredCancels.sort(),
    sourceStatus: recoveredSettled.messages.find(
      (message) => message.id === recoveredSourceMessageId,
    )?.failure?.status,
    publicationStatus: recoveredSettled.messages.find(
      (message) => message.id === recoveredPublishMessageId,
    )?.failure?.status,
    sourceProgress: useApp.getState().runs_progress[recoveredConversationId],
    publicationProgress:
      useApp.getState().runs_progress[`${recoveredConversationId}:candidate-publish`],
  };
  const expectedRecoveredObservation = {
    listeners: [MockEventSource.CLOSED, MockEventSource.CLOSED],
    cancels: [recoveredPublishRunId, recoveredSourceRunId].sort(),
    sourceStatus: "cancelled",
    publicationStatus: "cancelled",
    sourceProgress: undefined,
    publicationProgress: undefined,
  };

  const preAckConversationId = "step4d_publish_cancel_pre_ack";
  const preAckSourceRunId = "run_step4d_publish_cancel_pre_ack_source";
  const preAckPublishRunId = "run_step4d_publish_cancel_pre_ack_publish";
  const preAckSourceMessageId = "msg_step4d_publish_cancel_pre_ack_source";
  const preAckCandidateId = "deck-step4d-cancel-pre-ack";
  const preAckDraft = step4dDraft(
    "step4d_publish_cancel_pre_ack_draft",
    "deck",
    preAckSourceRunId,
    preAckCandidateId,
  );
  resetStore({
    [preAckConversationId]: conversation(preAckConversationId, {
      pending: true,
      run_id: preAckSourceRunId,
      messages: [step4dSourceMessage(preAckSourceMessageId, "deck")],
      artifacts: { [preAckDraft.artifact_id]: preAckDraft },
      active_artifact_id: preAckDraft.artifact_id,
    }),
  }, preAckConversationId);
  useApp.setState({
    run_attempts: {
      [preAckSourceRunId]: {
        run_id: preAckSourceRunId,
        candidates: [{
          ...attemptCandidate(preAckSourceRunId, preAckCandidateId),
          artifact_type: "deck",
        }],
        selection_phase: "idle",
        loading: false,
      },
    },
  });
  let releasePublicationReserve: ((response: Response) => void) | undefined;
  let publicationReserveSignal: AbortSignal | undefined;
  let publicationStartRequests = 0;
  const preAckCancels: string[] = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === `/api/runs/${preAckSourceRunId}/artifact`) {
      return jsonResponse({ detail: "source still running" }, 404);
    }
    if (url === `/api/artifacts/${preAckDraft.artifact_id}/publish-candidate-draft`) {
      publicationReserveSignal = init?.signal ?? undefined;
      return new Promise<Response>((resolve) => { releasePublicationReserve = resolve; });
    }
    if (url === `/api/runs/${preAckPublishRunId}/start`) {
      publicationStartRequests += 1;
      return jsonResponse({ detail: "cancelled before start" }, 409);
    }
    const cancel = url.match(/^\/api\/runs\/([^/]+)\/cancel$/);
    if (cancel) {
      preAckCancels.push(cancel[1]);
      return confirmedCancellation(cancel[1]);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;
  useApp.getState().recoverActiveRuns();
  await waitFor(
    () => Boolean(step4dEventSource(preAckSourceRunId)),
    "pre-ACK source listener did not recover",
  );
  const preAckPublishing = useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget());
  await waitFor(
    () => Boolean(publicationReserveSignal),
    "publication reservation did not begin",
  );
  const preAckCancelling = useApp.getState().cancelRun(preAckConversationId);
  await waitFor(
    () => preAckCancels.includes(preAckSourceRunId),
    "source cancellation was not issued before publication acknowledgement",
  );
  const beforePublicationAck = {
    raceKind: "aborted-reservation-defense",
    reserveAborted: publicationReserveSignal?.aborted === true,
    sourceCancels: preAckCancels.filter((runId) => runId === preAckSourceRunId).length,
    publicationCancels:
      preAckCancels.filter((runId) => runId === preAckPublishRunId).length,
  };
  releasePublicationReserve?.(jsonResponse({
    run_id: preAckPublishRunId,
    start_token: "publish-token",
    progress_mode: "attempt_publish",
  }));
  await Promise.allSettled([preAckPublishing, preAckCancelling]);
  await settleStep4dTicks();
  const preAckSettled = useApp.getState().conversations[preAckConversationId];
  const preAckObservation = {
    beforePublicationAck,
    sourceCancels: preAckCancels.filter(
      (runId) => runId === preAckSourceRunId,
    ).length,
    publicationCancels: preAckCancels.filter(
      (runId) => runId === preAckPublishRunId,
    ).length,
    publicationStartRequests,
    sourceFailure: preAckSettled.messages.find(
      (message) => message.id === preAckSourceMessageId,
    )?.failure?.status,
    publicationMessages: preAckSettled.messages.filter(
      (message) => message.run_id === preAckPublishRunId,
    ).length,
    activeArtifactId: preAckSettled.active_artifact_id,
    publicationOwnerActive:
      candidatePublicationIsActive(useApp.getState(), preAckConversationId),
  };
  const expectedPreAckObservation = {
    beforePublicationAck: {
      raceKind: "aborted-reservation-defense",
      reserveAborted: true,
      sourceCancels: 1,
      publicationCancels: 0,
    },
    sourceCancels: 1,
    publicationCancels: 1,
    publicationStartRequests: 0,
    sourceFailure: "cancelled",
    publicationMessages: 0,
    activeArtifactId: preAckDraft.artifact_id,
    publicationOwnerActive: false,
  };
  assert.deepEqual({
    live: liveObservations,
    recovered: recoveredObservation,
    preAck: preAckObservation,
  }, {
    live: expectedLiveObservations,
    recovered: expectedRecoveredObservation,
    preAck: expectedPreAckObservation,
  });
});

test("Step4d ordinary attempt-fork cancel settles acknowledged and pre-ACK descendants and persists only source", async () => {
  const conversationId = "step4d_fork_cancel_acknowledged";
  const sourceRunId = "run_step4d_fork_cancel_source_acknowledged";
  const forkRunId = "run_step4d_fork_cancel_fork_acknowledged";
  const sourceMessageId = "msg_step4d_fork_cancel_source_acknowledged";
  const candidateId = "poster-step4d-fork-cancel-acknowledged";
  const baseArtifact = artifact("step4d_fork_cancel_base", "poster");
  const lateDraft = step4dDraft(
    "step4d_fork_cancel_late_draft",
    "poster",
    sourceRunId,
    candidateId,
  );
  resetStore({
    [conversationId]: conversation(conversationId, {
      pending: true,
      run_id: sourceRunId,
      messages: [step4dSourceMessage(sourceMessageId, "poster")],
      artifacts: { [baseArtifact.artifact_id]: baseArtifact },
      active_artifact_id: baseArtifact.artifact_id,
    }),
  }, conversationId);
  let acknowledgedForkArtifactReads = 0;
  const acknowledgedCancels: string[] = [];
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${sourceRunId}/artifact`) {
      return jsonResponse({ detail: "source still running" }, 404);
    }
    if (url === `/api/runs/${sourceRunId}/attempts/1/fork`) {
      return jsonResponse({
        run_id: forkRunId,
        start_token: "fork-token",
        progress_mode: "attempt_fork",
      });
    }
    if (url === `/api/runs/${forkRunId}/start`) {
      return jsonResponse({ run_id: forkRunId, progress_mode: "attempt_fork" });
    }
    if (url === `/api/runs/${forkRunId}/artifact`) {
      acknowledgedForkArtifactReads += 1;
      return jsonResponse({
        message: {
          id: `msg_${forkRunId}`,
          role: "assistant",
          text: "Late draft.",
          ts: 5,
          run_id: forkRunId,
          artifact_id: lateDraft.artifact_id,
          status: "done",
        },
        artifact: lateDraft,
      });
    }
    const cancel = url.match(/^\/api\/runs\/([^/]+)\/cancel$/);
    if (cancel) {
      acknowledgedCancels.push(cancel[1]);
      return confirmedCancellation(cancel[1]);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  useApp.getState().recoverActiveRuns();
  await waitFor(
    () => Boolean(step4dEventSource(sourceRunId)),
    "acknowledged fork source listener did not recover",
  );
  const opening = useApp.getState().openAttemptInCanvas(
    sourceRunId,
    attemptCandidate(sourceRunId, candidateId),
    conversationId,
  );
  await waitFor(
    () => Boolean(step4dEventSource(forkRunId)),
    "acknowledged fork listener did not open",
  );
  const persistedWhileForking = localStorage.getItem("autodesign.web.v1");
  assert.ok(persistedWhileForking);
  await useApp.getState().cancelRun(conversationId);
  await opening.catch(() => undefined);
  step4dEventSource(sourceRunId)?.emit("run.done");
  step4dEventSource(forkRunId)?.emit("run.done");
  await settleStep4dTicks();
  const acknowledgedState = useApp.getState();
  const acknowledgedConversation = acknowledgedState.conversations[conversationId];
  const acknowledgedObservation = {
    cancels: [...acknowledgedCancels].sort(),
    sourceFailure: acknowledgedConversation.messages.find(
      (message) => message.id === sourceMessageId,
    )?.failure?.status,
    sourceMessageRunId: acknowledgedConversation.messages.find(
      (message) => message.id === sourceMessageId,
    )?.run_id,
    misleadingMaterializationErrors: acknowledgedConversation.messages.filter(
      (message) => message.status === "error" && /materialization/i.test(message.text),
    ).length,
    sourceClosed:
      step4dEventSource(sourceRunId)?.readyState === MockEventSource.CLOSED,
    forkClosed:
      step4dEventSource(forkRunId)?.readyState === MockEventSource.CLOSED,
    sourceProgress: acknowledgedState.runs_progress[conversationId],
    forkProgress: acknowledgedState.runs_progress[`${conversationId}:attempt-fork`],
    activeArtifactId: acknowledgedConversation.active_artifact_id,
    lateDraftApplied: Boolean(acknowledgedConversation.artifacts[lateDraft.artifact_id]),
    forkArtifactReads: acknowledgedForkArtifactReads,
  };

  resetStore({ base: conversation("base") }, "base");
  localStorage.setItem("autodesign.web.v1", persistedWhileForking);
  const persistedConversation = (
    JSON.parse(persistedWhileForking).state.conversations[conversationId]
  ) as Conversation;
  let reloadSourceReads = 0;
  let reloadForkReads = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.startsWith("/api/history")) {
      return jsonResponse({
        conversations: { [conversationId]: persistedConversation },
        imported_runs: 0,
        user_isolated: true,
        request_scope: "test-user",
      });
    }
    if (url === `/api/runs/${sourceRunId}/artifact`) {
      reloadSourceReads += 1;
      return jsonResponse({ detail: "source still running" }, 404);
    }
    if (url === `/api/runs/${forkRunId}/artifact`) {
      reloadForkReads += 1;
      return jsonResponse({ detail: "fork recovery is out of scope" }, 404);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;
  await useApp.persist.rehydrate();
  await useApp.getState().loadServerHistory();
  await settleStep4dTicks();
  const reloadObservation = {
    persistedPending: useApp.getState().conversations[conversationId].pending,
    persistedRunId: useApp.getState().conversations[conversationId].run_id,
    sourceListeners: MockEventSource.instances.filter(
      (source) => source.url === `/api/runs/${sourceRunId}/events`,
    ).length,
    forkListeners: MockEventSource.instances.filter(
      (source) => source.url === `/api/runs/${forkRunId}/events`,
    ).length,
    sourceProgressRunId: useApp.getState().runs_progress[conversationId]?.run_id,
    forkProgress:
      useApp.getState().runs_progress[`${conversationId}:attempt-fork`],
    reloadSourceReads,
    reloadForkReads,
  };
  step4dEventSource(sourceRunId)?.emit("run.error");
  step4dEventSource(forkRunId)?.emit("run.error");
  await settleStep4dTicks();

  const preAckConversationId = "step4d_fork_cancel_pre_ack";
  const preAckSourceRunId = "run_step4d_fork_cancel_source_pre_ack";
  const preAckForkRunId = "run_step4d_fork_cancel_fork_pre_ack";
  const preAckSourceMessageId = "msg_step4d_fork_cancel_source_pre_ack";
  const preAckCandidateId = "poster-step4d-fork-cancel-pre-ack";
  const preAckBase = artifact("step4d_fork_cancel_pre_ack_base", "poster");
  resetStore({
    [preAckConversationId]: conversation(preAckConversationId, {
      pending: true,
      run_id: preAckSourceRunId,
      messages: [step4dSourceMessage(preAckSourceMessageId, "poster")],
      artifacts: { [preAckBase.artifact_id]: preAckBase },
      active_artifact_id: preAckBase.artifact_id,
    }),
  }, preAckConversationId);
  let releaseForkReserve: ((response: Response) => void) | undefined;
  let forkReserveSignal: AbortSignal | undefined;
  let preAckForkStartRequests = 0;
  let preAckForkArtifactReads = 0;
  const preAckCancels: string[] = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === `/api/runs/${preAckSourceRunId}/artifact`) {
      return jsonResponse({ detail: "source still running" }, 404);
    }
    if (url === `/api/runs/${preAckSourceRunId}/attempts/1/fork`) {
      forkReserveSignal = init?.signal ?? undefined;
      return new Promise<Response>((resolve) => { releaseForkReserve = resolve; });
    }
    if (url === `/api/runs/${preAckForkRunId}/start`) {
      preAckForkStartRequests += 1;
      return jsonResponse({ detail: "cancelled before start" }, 409);
    }
    if (url === `/api/runs/${preAckForkRunId}/artifact`) {
      preAckForkArtifactReads += 1;
      return jsonResponse({ detail: "cancelled fork has no artifact" }, 404);
    }
    const cancel = url.match(/^\/api\/runs\/([^/]+)\/cancel$/);
    if (cancel) {
      preAckCancels.push(cancel[1]);
      return confirmedCancellation(cancel[1]);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;
  useApp.getState().recoverActiveRuns();
  await waitFor(
    () => Boolean(step4dEventSource(preAckSourceRunId)),
    "pre-ACK fork source listener did not recover",
  );
  const preAckOpening = useApp.getState().openAttemptInCanvas(
    preAckSourceRunId,
    attemptCandidate(preAckSourceRunId, preAckCandidateId),
    preAckConversationId,
  );
  await waitFor(
    () => Boolean(forkReserveSignal),
    "fork reservation did not begin",
  );
  releaseForkReserve?.(jsonResponse({
    run_id: preAckForkRunId,
    start_token: "fork-token",
    progress_mode: "attempt_fork",
  }));
  const preAckCancelling = useApp.getState().cancelRun(preAckConversationId);
  const beforeForkAck = {
    raceKind: "resolved-before-await-continuation",
    reserveAborted: forkReserveSignal?.aborted === true,
    sourceCancels: preAckCancels.filter((runId) => runId === preAckSourceRunId).length,
    forkCancels: preAckCancels.filter((runId) => runId === preAckForkRunId).length,
  };
  await waitFor(
    () => preAckCancels.includes(preAckSourceRunId),
    "source cancellation was not issued before fork acknowledgement",
  );
  const [openingOutcome] = await Promise.all([
    preAckOpening.then(() => "fulfilled" as const, () => "rejected" as const),
    preAckCancelling,
  ]);
  await settleStep4dTicks();
  const preAckState = useApp.getState();
  const preAckConversation = preAckState.conversations[preAckConversationId];
  const preAckObservation = {
    beforeForkAck,
    openingOutcome,
    sourceCancels: preAckCancels.filter(
      (runId) => runId === preAckSourceRunId,
    ).length,
    forkCancels: preAckCancels.filter(
      (runId) => runId === preAckForkRunId,
    ).length,
    forkStartRequests: preAckForkStartRequests,
    forkArtifactReads: preAckForkArtifactReads,
    sourceFailure: preAckConversation.messages.find(
      (message) => message.id === preAckSourceMessageId,
    )?.failure?.status,
    misleadingMaterializationErrors: preAckConversation.messages.filter(
      (message) => message.status === "error" && /materialization/i.test(message.text),
    ).length,
    activeArtifactId: preAckConversation.active_artifact_id,
    lateDrafts: Object.values(preAckConversation.artifacts).filter(
      (candidate) => candidate.attempt_lineage?.source_run_id === preAckSourceRunId,
    ).length,
    sourceProgress: preAckState.runs_progress[preAckConversationId],
    forkProgress: preAckState.runs_progress[`${preAckConversationId}:attempt-fork`],
  };

  assert.deepEqual({
    acknowledged: acknowledgedObservation,
    reload: reloadObservation,
    preAck: preAckObservation,
  }, {
    acknowledged: {
      cancels: [forkRunId, sourceRunId].sort(),
      sourceFailure: "cancelled",
      sourceMessageRunId: sourceRunId,
      misleadingMaterializationErrors: 0,
      sourceClosed: true,
      forkClosed: true,
      sourceProgress: undefined,
      forkProgress: undefined,
      activeArtifactId: baseArtifact.artifact_id,
      lateDraftApplied: false,
      forkArtifactReads: 0,
    },
    reload: {
      persistedPending: true,
      persistedRunId: sourceRunId,
      sourceListeners: 1,
      forkListeners: 0,
      sourceProgressRunId: sourceRunId,
      forkProgress: undefined,
      reloadSourceReads: 1,
      reloadForkReads: 0,
    },
    preAck: {
      beforeForkAck: {
        raceKind: "resolved-before-await-continuation",
        reserveAborted: true,
        sourceCancels: 1,
        forkCancels: 0,
      },
      openingOutcome: "fulfilled",
      sourceCancels: 1,
      forkCancels: 1,
      forkStartRequests: 0,
      forkArtifactReads: 0,
      sourceFailure: "cancelled",
      misleadingMaterializationErrors: 0,
      activeArtifactId: preAckBase.artifact_id,
      lateDrafts: 0,
      sourceProgress: undefined,
      forkProgress: undefined,
    },
  });
});

test("Step4d native pre-ACK abort rejection settles ordinary source without a derived run", async () => {
  const observations: Array<Record<string, unknown>> = [];

  for (const kind of ["publication", "fork"] as const) {
    const conversationId = `step4d_native_abort_${kind}`;
    const sourceRunId = `run_step4d_native_abort_source_${kind}`;
    const neverReservedRunId = `run_step4d_native_abort_never_reserved_${kind}`;
    const sourceMessageId = `msg_step4d_native_abort_source_${kind}`;
    const candidateId = `${kind}-step4d-native-abort`;
    const artifactType = kind === "publication" ? "deck" : "poster";
    const baseArtifact = kind === "publication"
      ? step4dDraft(
          `step4d_native_abort_draft_${kind}`,
          artifactType,
          sourceRunId,
          candidateId,
        )
      : artifact(`step4d_native_abort_base_${kind}`, artifactType);
    const operationId = `${conversationId}:${
      kind === "publication" ? "candidate-publish" : "attempt-fork"
    }`;
    const reservationUrl = kind === "publication"
      ? `/api/artifacts/${baseArtifact.artifact_id}/publish-candidate-draft`
      : `/api/runs/${sourceRunId}/attempts/1/fork`;

    resetStore({
      [conversationId]: conversation(conversationId, {
        pending: true,
        run_id: sourceRunId,
        messages: [step4dSourceMessage(sourceMessageId, artifactType, sourceRunId)],
        artifacts: { [baseArtifact.artifact_id]: baseArtifact },
        active_artifact_id: baseArtifact.artifact_id,
      }),
    }, conversationId);
    if (kind === "publication") {
      useApp.setState({
        run_attempts: {
          [sourceRunId]: {
            run_id: sourceRunId,
            candidates: [{
              ...attemptCandidate(sourceRunId, candidateId),
              artifact_type: artifactType,
            }],
            selection_phase: "idle",
            loading: false,
          },
        },
      });
    }

    let reservationSignal: AbortSignal | undefined;
    let resolveReservation: ((response: Response) => void) | undefined;
    let rejectReservationForCleanup: ((reason: unknown) => void) | undefined;
    let abortListenerRejections = 0;
    let derivedStartRequests = 0;
    let derivedArtifactReads = 0;
    const cancels: string[] = [];
    globalThis.fetch = (async (input, init) => {
      const url = String(input);
      if (url === `/api/runs/${sourceRunId}/artifact`) {
        return jsonResponse({ detail: "source still running" }, 404);
      }
      if (url === reservationUrl) {
        reservationSignal = init?.signal ?? undefined;
        return new Promise<Response>((resolve, reject) => {
          resolveReservation = resolve;
          rejectReservationForCleanup = reject;
          const rejectFromAbort = () => {
            abortListenerRejections += 1;
            reject(
              reservationSignal?.reason
              ?? new DOMException("This operation was aborted", "AbortError"),
            );
          };
          if (reservationSignal?.aborted) rejectFromAbort();
          else reservationSignal?.addEventListener("abort", rejectFromAbort, { once: true });
        });
      }
      if (/\/api\/runs\/[^/]+\/start$/.test(url)) {
        derivedStartRequests += 1;
        return jsonResponse({ detail: "an unreserved run must not start" }, 409);
      }
      if (/\/api\/runs\/[^/]+\/artifact$/.test(url)) {
        derivedArtifactReads += 1;
        return jsonResponse({ detail: "an unreserved run has no artifact" }, 404);
      }
      const cancel = url.match(/^\/api\/runs\/([^/]+)\/cancel$/);
      if (cancel) {
        cancels.push(cancel[1]);
        return confirmedCancellation(cancel[1]);
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }) as typeof fetch;

    useApp.getState().recoverActiveRuns();
    await waitFor(
      () => Boolean(step4dEventSource(sourceRunId)),
      `${kind} native-abort source listener did not recover`,
    );
    const action = kind === "publication"
      ? useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget())
      : useApp.getState().openAttemptInCanvas(
          sourceRunId,
          attemptCandidate(sourceRunId, candidateId),
          conversationId,
        );
    let actionSettled = false;
    let actionError: string | undefined;
    const actionOutcome = action.then(
      () => "fulfilled" as const,
      (error) => {
        actionError = error instanceof Error ? error.message : String(error);
        return "rejected" as const;
      },
    ).finally(() => { actionSettled = true; });
    await waitFor(
      () => Boolean(reservationSignal),
      `${kind} native-abort reservation did not begin`,
    );
    const cancelling = useApp.getState().cancelRun(conversationId);
    await settleStep4dTicks();
    const cleanupNeeded = !actionSettled;
    if (cleanupNeeded) {
      rejectReservationForCleanup?.(
        new DOMException("Test cleanup after missing reservation abort", "AbortError"),
      );
    }
    const [handledOutcome] = await Promise.all([actionOutcome, cancelling]);
    const fingerprintBeforeLateResolve = JSON.stringify({
      conversation: useApp.getState().conversations[conversationId],
      progress: useApp.getState().runs_progress,
      sources: MockEventSource.instances.map((source) => [source.url, source.readyState]),
    });
    resolveReservation?.(jsonResponse({
      run_id: neverReservedRunId,
      start_token: `${kind}-token`,
      progress_mode: kind === "publication" ? "attempt_publish" : "attempt_fork",
    }));
    await settleStep4dTicks();

    const state = useApp.getState();
    const settled = state.conversations[conversationId];
    const sourceMessage = settled.messages.find((message) => message.id === sourceMessageId);
    observations.push({
      kind,
      reservationAborted: reservationSignal?.aborted === true,
      abortListenerRejections,
      cleanupNeeded,
      actionOutcome: handledOutcome,
      actionError,
      sourceCancels: cancels.filter((runId) => runId === sourceRunId).length,
      derivedCancels: cancels.filter((runId) => runId !== sourceRunId).length,
      sourceMessage: sourceMessage && {
        runId: sourceMessage.run_id,
        status: sourceMessage.status,
        failure: sourceMessage.failure?.status,
      },
      pending: settled.pending,
      runId: settled.run_id,
      sourceClosed: step4dEventSource(sourceRunId)?.readyState === MockEventSource.CLOSED,
      sourceProgress: state.runs_progress[conversationId],
      derivedMessages: settled.messages.filter((message) => message.id !== sourceMessageId).length,
      misleadingActionErrors: settled.messages.filter(
        (message) => message.id !== sourceMessageId && message.status === "error",
      ).length,
      derivedProgress: state.runs_progress[operationId],
      derivedSources: MockEventSource.instances.filter(
        (source) => source.url !== `/api/runs/${sourceRunId}/events`,
      ).length,
      derivedStartRequests,
      derivedArtifactReads,
      derivedArtifacts: Object.keys(settled.artifacts).filter(
        (artifactId) => artifactId !== baseArtifact.artifact_id,
      ).length,
      activeArtifactId: settled.active_artifact_id,
      derivedOwnerActive: candidatePublicationIsActive(state, conversationId),
      reactiveOwnerPresent: Boolean(state.candidate_publication_owners[conversationId]),
      lateNoIdRevived: fingerprintBeforeLateResolve !== JSON.stringify({
        conversation: settled,
        progress: state.runs_progress,
        sources: MockEventSource.instances.map((source) => [source.url, source.readyState]),
      }),
    });
  }

  assert.deepEqual(observations, ["publication", "fork"].map((kind) => ({
    kind,
    reservationAborted: true,
    abortListenerRejections: 1,
    cleanupNeeded: false,
    actionOutcome: "fulfilled",
    actionError: undefined,
    sourceCancels: 1,
    derivedCancels: 0,
    sourceMessage: {
      runId: `run_step4d_native_abort_source_${kind}`,
      status: "error",
      failure: "cancelled",
    },
    pending: false,
    runId: undefined,
    sourceClosed: true,
    sourceProgress: undefined,
    derivedMessages: 0,
    misleadingActionErrors: 0,
    derivedProgress: undefined,
    derivedSources: 0,
    derivedStartRequests: 0,
    derivedArtifactReads: 0,
    derivedArtifacts: 0,
    activeArtifactId: kind === "publication"
      ? "art_step4d_native_abort_draft_publication"
      : "art_step4d_native_abort_base_fork",
    derivedOwnerActive: false,
    reactiveOwnerPresent: false,
    lateNoIdRevived: false,
  })));
});

type Step4fDerivedKind = "publication" | "fork";
type Step4fUnconfirmedOutcome = "http-202" | "transport" | "timeout";

const step4fUnconfirmedCancellation = (runId: string) => jsonResponse({
  run_id: runId,
  status: "cancellation_pending",
  run_state: "cancelling",
  confirmed: false,
  terminated_pids: [],
  surviving_pids: [],
}, 202);

async function observeStep4fStillLiveRetry(
  kind: Step4fDerivedKind,
  firstOutcome: Step4fUnconfirmedOutcome,
) {
  const suffix = `${kind}_${firstOutcome.replace("-", "_")}`;
  const conversationId = `step4f_live_retry_${suffix}`;
  const sourceRunId = `run_step4f_live_retry_source_${suffix}`;
  const derivedRunId = `run_step4f_live_retry_derived_${suffix}`;
  const sourceMessageId = `msg_step4f_live_retry_source_${suffix}`;
  const candidateId = `poster-step4f-live-retry-${suffix}`;
  const competingRunId = `run_step4f_live_retry_competing_${suffix}`;
  const competingCandidate = {
    ...attemptCandidate(competingRunId, `poster-step4f-competing-${suffix}`),
    attempt: 2,
  };
  const initialArtifact = kind === "publication"
    ? step4dDraft(
        `step4f_live_retry_initial_${suffix}`,
        "poster",
        sourceRunId,
        candidateId,
      )
    : artifact(`step4f_live_retry_initial_${suffix}`, "poster");
  const lateArtifact = kind === "publication"
    ? step4dPublished(
        `step4f_live_retry_late_${suffix}`,
        "poster",
        sourceRunId,
        candidateId,
      )
    : step4dDraft(
        `step4f_live_retry_late_${suffix}`,
        "poster",
        sourceRunId,
        candidateId,
      );
  const operationId = `${conversationId}:${
    kind === "publication" ? "candidate-publish" : "attempt-fork"
  }`;

  resetStore({
    [conversationId]: conversation(conversationId, {
      pending: true,
      run_id: sourceRunId,
      messages: [step4dSourceMessage(sourceMessageId, "poster", sourceRunId)],
      artifacts: { [initialArtifact.artifact_id]: initialArtifact },
      active_artifact_id: initialArtifact.artifact_id,
    }),
  }, conversationId);
  setReadyAttempt(sourceRunId, candidateId);
  const originalArtifactFingerprint = JSON.stringify({
    activeArtifactId: initialArtifact.artifact_id,
    artifacts: { [initialArtifact.artifact_id]: initialArtifact },
  });

  const nativeSetTimeout = window.setTimeout;
  let cancellationTimeouts = 0;
  let timeoutCallbacks = 0;
  let timeoutTriggered = false;
  let triggerFirstCancellationTimeout: (() => void) | undefined;
  if (firstOutcome === "timeout") {
    window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
      if (timeout === 10_000) {
        cancellationTimeouts += 1;
        if (cancellationTimeouts === 1) {
          triggerFirstCancellationTimeout = () => {
            if (timeoutTriggered) return;
            timeoutTriggered = true;
            timeoutCallbacks += 1;
            if (typeof handler === "function") handler(...args);
          };
          return 94 as unknown as number;
        }
      }
      return nativeSetTimeout(handler, timeout, ...args);
    }) as typeof window.setTimeout;
  }

  let sourceArtifactReads = 0;
  let derivedArtifactReads = 0;
  let sourceCancelPosts = 0;
  let derivedCancelPosts = 0;
  let competingForkPosts = 0;
  let firstDerivedCancelSignal: AbortSignal | undefined;
  let timeoutAbortRejections = 0;
  let releaseFirstDerivedCancel: ((response: Response) => void) | undefined;
  let rejectFirstDerivedCancel: ((error: Error) => void) | undefined;
  let releaseRetryDerivedCancel: ((response: Response) => void) | undefined;
  let retryResponseReleased = false;
  let originalActionSettled = false;
  let originalActionOutcome: "fulfilled" | "rejected" | undefined;
  let originalAction: Promise<void> | undefined;
  let derivedSource: MockEventSource | undefined;
  let firstWaveSettled = 0;
  let intendedRetrySettled = 0;
  let firstCancels: Promise<void>[] = [];
  let retryCancels: Promise<void>[] = [];
  const competingActions: Promise<void>[] = [];

  try {
    globalThis.fetch = (async (input, init) => {
      const url = String(input);
      if (url === `/api/runs/${sourceRunId}/artifact`) {
        sourceArtifactReads += 1;
        return jsonResponse({ detail: "source still running" }, 404);
      }
      if (
        kind === "publication"
        && url === `/api/artifacts/${initialArtifact.artifact_id}/publish-candidate-draft`
      ) {
        return jsonResponse({
          run_id: derivedRunId,
          start_token: "publication-token",
          progress_mode: "attempt_publish",
        });
      }
      if (
        kind === "fork"
        && url === `/api/runs/${sourceRunId}/attempts/1/fork`
      ) {
        return jsonResponse({
          run_id: derivedRunId,
          start_token: "fork-token",
          progress_mode: "attempt_fork",
        });
      }
      if (url === `/api/runs/${derivedRunId}/start`) {
        return jsonResponse({
          run_id: derivedRunId,
          progress_mode: kind === "publication" ? "attempt_publish" : "attempt_fork",
        });
      }
      if (url === `/api/runs/${derivedRunId}/artifact`) {
        derivedArtifactReads += 1;
        return jsonResponse({
          message: {
            id: `msg_${derivedRunId}`,
            role: "assistant",
            text: "Late derived result.",
            ts: 3,
            run_id: derivedRunId,
            artifact_id: lateArtifact.artifact_id,
            status: "done",
          },
          artifact: lateArtifact,
        });
      }
      if (url === `/api/runs/${competingRunId}/attempts/2/fork`) {
        competingForkPosts += 1;
        return jsonResponse({ detail: "ownership probe reached the backend" }, 409);
      }
      const cancellation = url.match(/^\/api\/runs\/([^/]+)\/cancel$/);
      if (cancellation?.[1] === sourceRunId) {
        sourceCancelPosts += 1;
        return confirmedCancellation(sourceRunId);
      }
      if (cancellation?.[1] === derivedRunId) {
        derivedCancelPosts += 1;
        if (derivedCancelPosts === 1) {
          firstDerivedCancelSignal = init?.signal ?? undefined;
          return new Promise<Response>((resolve, reject) => {
            releaseFirstDerivedCancel = resolve;
            rejectFirstDerivedCancel = reject;
            if (firstOutcome !== "timeout") return;
            const rejectForAbort = () => {
              timeoutAbortRejections += 1;
              reject(
                firstDerivedCancelSignal?.reason instanceof Error
                  ? firstDerivedCancelSignal.reason
                  : new Error("derived cancellation timed out"),
              );
            };
            if (firstDerivedCancelSignal?.aborted) rejectForAbort();
            else firstDerivedCancelSignal?.addEventListener("abort", rejectForAbort, { once: true });
          });
        }
        if (derivedCancelPosts === 2) {
          return new Promise<Response>((resolve) => {
            releaseRetryDerivedCancel = resolve;
          });
        }
        return confirmedCancellation(derivedRunId);
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }) as typeof fetch;

    useApp.getState().recoverActiveRuns();
    await waitFor(
      () => Boolean(step4dEventSource(sourceRunId)),
      `${suffix} source listener did not recover`,
    );
    originalAction = (
      kind === "publication"
        ? useApp.getState().publishActiveCandidateDraft(activeDraftPublicationTarget())
        : useApp.getState().openAttemptInCanvas(
            sourceRunId,
            attemptCandidate(sourceRunId, candidateId),
            conversationId,
          )
    ).then(
      () => { originalActionOutcome = "fulfilled"; },
      () => { originalActionOutcome = "rejected"; },
    ).finally(() => { originalActionSettled = true; });
    await waitFor(
      () => Boolean(step4dEventSource(derivedRunId)),
      `${suffix} derived listener did not open`,
    );
    derivedSource = step4dEventSource(derivedRunId)!;
    const publicationOwnerToken = kind === "publication"
      ? useApp.getState().candidate_publication_owners[conversationId]?.token
      : undefined;

    firstCancels = [
      useApp.getState().cancelRun(conversationId)
        .finally(() => { firstWaveSettled += 1; }),
      useApp.getState().cancelRun(conversationId)
        .finally(() => { firstWaveSettled += 1; }),
    ];
    await waitFor(
      () => derivedCancelPosts === 1 && sourceCancelPosts === 1,
      `${suffix} first cancellations did not coalesce`,
    );
    assert.equal(
      firstWaveSettled,
      0,
      `${suffix}: both first-wave callers must await the held exact response`,
    );
    if (firstOutcome === "http-202") {
      releaseFirstDerivedCancel?.(step4fUnconfirmedCancellation(derivedRunId));
    } else if (firstOutcome === "transport") {
      rejectFirstDerivedCancel?.(new Error("derived cancellation transport failed"));
    } else {
      triggerFirstCancellationTimeout?.();
      await waitFor(
        () => firstDerivedCancelSignal?.aborted === true && timeoutAbortRejections === 1,
        `${suffix} did not exercise the production cancellation timeout`,
      );
    }
    await Promise.all(firstCancels);
    assert.equal(
      firstWaveSettled,
      2,
      `${suffix}: both first-wave callers must settle after the held response`,
    );
    await settleStep4dTicks();

    const afterFirstState = useApp.getState();
    const afterFirstProgress = afterFirstState.runs_progress[operationId];
    const derivedSseOpenAfterFirst = derivedSource.readyState === 1;
    const blockedPostsBefore = competingForkPosts;
    const blockedAction = useApp.getState().openAttemptInCanvas(
      competingRunId,
      competingCandidate,
      conversationId,
    );
    competingActions.push(blockedAction);
    const [blockedProbe] = await Promise.allSettled([blockedAction]);
    const ownerRetainedBeforeRetry = competingForkPosts === blockedPostsBefore
      && blockedProbe.status === "rejected";

    retryCancels = [
      useApp.getState().cancelRun(conversationId)
        .finally(() => { intendedRetrySettled += 1; }),
      useApp.getState().cancelRun(conversationId)
        .finally(() => { intendedRetrySettled += 1; }),
    ];
    await waitFor(
      () => derivedCancelPosts >= 2 || intendedRetrySettled === 2,
      `${suffix} retry callers neither settled nor started a fresh request`,
    );
    if (derivedCancelPosts >= 2 && releaseRetryDerivedCancel) {
      await tick();
      assert.equal(
        intendedRetrySettled,
        0,
        `${suffix}: both retry callers must await the shared fresh response`,
      );
      retryResponseReleased = true;
      releaseRetryDerivedCancel(confirmedCancellation(derivedRunId));
    }
    await Promise.all(retryCancels);
    assert.equal(
      intendedRetrySettled,
      2,
      `${suffix}: both retry callers must settle after the fresh response`,
    );
    await settleStep4dTicks();
    if (derivedSource.readyState === MockEventSource.CLOSED) {
      await originalAction;
      await settleStep4dTicks();
    }

    const afterRetryState = useApp.getState();
    const afterRetryConversation = afterRetryState.conversations[conversationId];
    const artifactFingerprintAfterRetry = JSON.stringify({
      activeArtifactId: afterRetryConversation.active_artifact_id,
      artifacts: afterRetryConversation.artifacts,
    });
    const releasedPostsBefore = competingForkPosts;
    const releasedAction = useApp.getState().openAttemptInCanvas(
      competingRunId,
      competingCandidate,
      conversationId,
    );
    competingActions.push(releasedAction);
    const [releasedProbe] = await Promise.allSettled([releasedAction]);
    const ownerReleasedAfterRetry = competingForkPosts === releasedPostsBefore + 1
      && releasedProbe.status === "rejected";

    const observation = {
      observedDerivedCancelPostsAfterRetry: derivedCancelPosts,
      observedSourceCancelPostsAfterRetry: sourceCancelPosts,
      ownerRetainedBeforeRetry,
      publicationOwnerTokenRetained: kind !== "publication"
        || Boolean(publicationOwnerToken)
          && afterFirstState.candidate_publication_owners[conversationId]?.token
            === publicationOwnerToken,
      afterFirstProgressPhase: afterFirstProgress?.phase,
      afterFirstCancelRequestInFlight: afterFirstProgress?.cancel_request_in_flight,
      derivedSseOpenAfterFirst,
      sourceSseClosedAfterFirst:
        step4dEventSource(sourceRunId)?.readyState === MockEventSource.CLOSED,
      ownerReleasedAfterRetry,
      publicationOwnerClearedAfterRetry: kind !== "publication"
        || !afterRetryState.candidate_publication_owners[conversationId],
      derivedSseClosedAfterRetry: derivedSource.readyState === MockEventSource.CLOSED,
      derivedProgressAfterRetry: afterRetryState.runs_progress[operationId],
      derivedArtifactReadsAfterRetry: derivedArtifactReads,
      originalActionSettledAfterRetry: originalActionSettled,
      originalActionOutcome,
      artifactUnchangedAfterRetry:
        artifactFingerprintAfterRetry === originalArtifactFingerprint,
      timeoutUsedProductionAbort: firstOutcome !== "timeout"
        || (
          cancellationTimeouts >= 1
          && timeoutCallbacks === 1
          && timeoutAbortRejections === 1
          && firstDerivedCancelSignal?.aborted === true
        ),
      nonTimeoutSignalStayedLive: firstOutcome === "timeout"
        || firstDerivedCancelSignal?.aborted === false,
      sourceArtifactReads,
    };

    if (derivedSource.readyState !== MockEventSource.CLOSED) {
      derivedSource.emit("run.done");
      await originalAction;
      await settleStep4dTicks();
      const cleanupCancel = useApp.getState().cancelRun(conversationId);
      await waitFor(
        () => derivedCancelPosts >= 2,
        `${suffix} cleanup retry did not start`,
      );
      if (!retryResponseReleased && releaseRetryDerivedCancel) {
        retryResponseReleased = true;
        releaseRetryDerivedCancel(confirmedCancellation(derivedRunId));
      }
      await cleanupCancel;
      await settleStep4dTicks();
    }

    const beforeLateState = useApp.getState();
    const beforeLateConversation = beforeLateState.conversations[conversationId];
    const beforeLateFingerprint = JSON.stringify({
      activeArtifactId: beforeLateConversation.active_artifact_id,
      artifacts: beforeLateConversation.artifacts,
    });
    const artifactReadsBeforeLateEvent = derivedArtifactReads;
    derivedSource.emit("run.done", { artifact_id: lateArtifact.artifact_id });
    await settleStep4dTicks();
    const afterLateConversation = useApp.getState().conversations[conversationId];
    return {
      ...observation,
      lateEventStartedNoArtifactFetch:
        derivedArtifactReads === artifactReadsBeforeLateEvent,
      lateEventLeftArtifactsByteExact: JSON.stringify({
        activeArtifactId: afterLateConversation.active_artifact_id,
        artifacts: afterLateConversation.artifacts,
      }) === beforeLateFingerprint,
    };
  } finally {
    try {
      if (firstOutcome === "http-202") {
        releaseFirstDerivedCancel?.(step4fUnconfirmedCancellation(derivedRunId));
      } else if (firstOutcome === "transport") {
        rejectFirstDerivedCancel?.(new Error("Step4f test teardown"));
      } else {
        triggerFirstCancellationTimeout?.();
      }
      releaseRetryDerivedCancel?.(confirmedCancellation(derivedRunId));
      await Promise.allSettled(firstCancels);

      const liveDerivedSource = derivedSource ?? step4dEventSource(derivedRunId);
      if (liveDerivedSource?.readyState !== MockEventSource.CLOSED) {
        liveDerivedSource?.emit("run.done");
        await settleStep4dTicks();
      }
      if (originalAction) await Promise.allSettled([originalAction]);

      const teardownCancel = useApp.getState().cancelRun(conversationId);
      await tick();
      releaseRetryDerivedCancel?.(confirmedCancellation(derivedRunId));
      await teardownCancel;
      if (liveDerivedSource?.readyState !== MockEventSource.CLOSED) {
        liveDerivedSource?.emit("run.cancelled");
      }
      await settleStep4dTicks();
      await Promise.allSettled([
        ...retryCancels,
        ...competingActions,
        ...(originalAction ? [originalAction] : []),
      ]);
    } catch {
      derivedSource?.emit("run.error");
      step4dEventSource(sourceRunId)?.emit("run.error");
      await settleStep4dTicks();
      derivedSource?.close();
      step4dEventSource(sourceRunId)?.close();
    } finally {
      window.setTimeout = nativeSetTimeout;
    }
  }
}

for (const kind of ["publication", "fork"] as const) {
  for (const firstOutcome of ["http-202", "transport", "timeout"] as const) {
    test(`Step4f ${kind} retries a still-live ${firstOutcome} cancellation exactly once`, async () => {
      const observation = await observeStep4fStillLiveRetry(kind, firstOutcome);

      assert.equal(
        observation.observedDerivedCancelPostsAfterRetry,
        2,
        `${kind}/${firstOutcome}: concurrent retry callers must create one fresh exact request`,
      );
      assert.equal(observation.observedSourceCancelPostsAfterRetry, 1);
      assert.equal(observation.ownerRetainedBeforeRetry, true);
      assert.equal(observation.publicationOwnerTokenRetained, true);
      assert.equal(observation.afterFirstProgressPhase, "cancelling");
      assert.equal(observation.afterFirstCancelRequestInFlight, false);
      assert.equal(observation.derivedSseOpenAfterFirst, true);
      assert.equal(observation.sourceSseClosedAfterFirst, true);
      assert.equal(observation.ownerReleasedAfterRetry, true);
      assert.equal(observation.publicationOwnerClearedAfterRetry, true);
      assert.equal(observation.derivedSseClosedAfterRetry, true);
      assert.equal(observation.derivedProgressAfterRetry, undefined);
      assert.equal(observation.derivedArtifactReadsAfterRetry, 0);
      assert.equal(observation.originalActionSettledAfterRetry, true);
      assert.equal(observation.originalActionOutcome, "fulfilled");
      assert.equal(observation.artifactUnchangedAfterRetry, true);
      assert.equal(observation.timeoutUsedProductionAbort, true);
      assert.equal(observation.nonTimeoutSignalStayedLive, true);
      assert.equal(observation.sourceArtifactReads, 1);
      assert.equal(observation.lateEventStartedNoArtifactFetch, true);
      assert.equal(observation.lateEventLeftArtifactsByteExact, true);
    });
  }
}

function setupTerminalFailedReadyAttempt(suffix: string, currentId?: string) {
  const parentId = `terminal_publish_parent_${suffix}`;
  const sourceRunId = `run_terminal_publish_source_${suffix}`;
  const publishRunId = `run_terminal_publish_${suffix}`;
  const candidateId = `poster-terminal-${suffix}`;
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = `job_terminal_publish_${suffix}`;
  bundle.revision = 7;
  bundle.backend_state = "failed";
  bundle.tasks.poster = {
    ...bundle.tasks.poster,
    status: "failed",
    run_id: sourceRunId,
    authoring_run_id: sourceRunId,
    error: "The source author stopped after producing Attempt 1.",
    terminal: true,
    process_free: true,
  };
  for (const artifactType of ["deck", "landing", "video"] as const) {
    bundle.tasks[artifactType] = {
      ...bundle.tasks[artifactType],
      status: "complete",
      run_id: `run_terminal_publish_${suffix}_${artifactType}`,
      artifact_id: `art_terminal_publish_${suffix}_${artifactType}`,
      terminal: true,
      process_free: true,
    };
  }
  const childId = bundle.tasks.poster.child_conversation_id;
  const candidate = attemptCandidate(sourceRunId, candidateId);
  const conversations = {
    [parentId]: conversation(parentId, {
      paper_bundle: bundle,
      pending: false,
    }),
    [childId]: conversation(childId, {
      paper_bundle: createPaperBundleChildState(parentId, "poster"),
      pending: false,
      messages: [{
        id: `msg_${sourceRunId}`,
        role: "assistant",
        text: "The source author stopped after producing Attempt 1.",
        ts: 1,
        run_id: sourceRunId,
        status: "error",
        failure: {
          status: "fail",
          produced_files: [],
          artifact_type: "poster",
        },
      }],
    }),
  };
  resetStore(conversations, currentId ?? childId);
  setReadyAttempt(sourceRunId, candidateId);
  return {
    parentId,
    childId,
    sourceRunId,
    publishRunId,
    candidateId,
    candidate,
    bundle,
  };
}

function directAttemptPublicationResponse(
  publishRunId: string,
  sourceRunId: string,
  candidateId: string,
): ReturnType<typeof responseForRun> {
  const response = responseForRun(publishRunId, "poster");
  response.artifact.attempt_lineage = {
    status: "published",
    source_run_id: sourceRunId,
    source_attempt: 1,
    source_candidate_id: candidateId,
    source_candidate_sha256: "a".repeat(64),
  };
  return response;
}

test("terminal failed source publishes a Ready attempt through a derived run into its child and parent", async () => {
  const fixture = setupTerminalFailedReadyAttempt("success");
  let legacySelections = 0;
  let directReservations = 0;
  let directStarts = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === `/api/runs/${fixture.sourceRunId}/attempts/1/select`) {
      legacySelections += 1;
      return jsonResponse({ detail: { code: "run_not_selectable" } }, 409);
    }
    if (url === `/api/runs/${fixture.sourceRunId}/attempts/1/publish`) {
      directReservations += 1;
      assert.equal(init?.method, "POST");
      assert.equal(new Headers(init?.headers).get("X-Autodesign-Reserve-Only"), "true");
      assert.equal(init?.signal instanceof AbortSignal, true);
      const body = JSON.parse(String(init?.body));
      assert.equal(body.conversation_id, fixture.childId);
      assert.equal(body.expected_candidate_sha256, fixture.candidate.source_sha256);
      assert.equal(typeof body.idempotency_key, "string");
      assert.ok(body.idempotency_key.length > 0);
      return jsonResponse({
        run_id: fixture.publishRunId,
        start_token: "terminal-publish-token",
        progress_mode: "attempt_publish",
      });
    }
    if (url === `/api/runs/${fixture.publishRunId}/start`) {
      directStarts += 1;
      return jsonResponse({
        run_id: fixture.publishRunId,
        progress_mode: "attempt_publish",
        placeholder_message: {
          id: `msg_${fixture.publishRunId}`,
          role: "assistant",
          text: "",
          ts: 2,
          run_id: fixture.publishRunId,
          status: "streaming",
        },
      });
    }
    if (url === `/api/runs/${fixture.publishRunId}/artifact`) {
      return jsonResponse(directAttemptPublicationResponse(
        fixture.publishRunId,
        fixture.sourceRunId,
        fixture.candidateId,
      ));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const publishing = useApp.getState().selectAttempt(
    fixture.sourceRunId,
    fixture.candidate,
    fixture.childId,
  );
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === `/api/runs/${fixture.publishRunId}/events`,
    ),
    "derived publication listener did not open",
  );
  const live = useApp.getState();
  assert.equal(candidatePublicationIsActive(live, fixture.childId), true);
  assert.equal(live.conversations[fixture.childId].run_id, fixture.publishRunId);
  assert.equal(
    live.conversations[fixture.childId].messages.at(-1)?.task_payload?.source_artifact_id,
    undefined,
  );
  assert.equal(
    live.conversations[fixture.childId].messages.at(-1)?.task_payload?.source_run_id,
    fixture.sourceRunId,
  );
  assert.equal(
    live.conversations[fixture.childId].messages.at(-1)?.task_payload?.source_candidate_id,
    fixture.candidateId,
  );
  MockEventSource.instances.find(
    (source) => source.url === `/api/runs/${fixture.publishRunId}/events`,
  )?.emit("run.done");
  await publishing;

  const settled = useApp.getState();
  const child = settled.conversations[fixture.childId];
  const parent = settled.conversations[fixture.parentId];
  const task = parent.paper_bundle?.kind === "parent"
    ? parent.paper_bundle.tasks.poster
    : undefined;
  assert.deepEqual({
    legacySelections,
    directReservations,
    directStarts,
    childPending: child.pending,
    childRunId: child.run_id,
    childArtifactId: child.published_artifact_id,
    parentArtifactId: task?.artifact_id,
    parentStatus: task?.status,
    parentError: task?.error,
    terminal: task?.terminal,
    processFree: task?.process_free,
    retainedAuthoringRunId: task?.authoring_run_id,
    parentHasArtifact: Boolean(
      task?.artifact_id && parent.artifacts[task.artifact_id]
    ),
  }, {
    legacySelections: 0,
    directReservations: 1,
    directStarts: 1,
    childPending: false,
    childRunId: undefined,
    childArtifactId: `art_${fixture.publishRunId}`,
    parentArtifactId: `art_${fixture.publishRunId}`,
    parentStatus: "complete",
    parentError: undefined,
    terminal: true,
    processFree: true,
    retainedAuthoringRunId: fixture.sourceRunId,
    parentHasArtifact: true,
  });
});

test("direct publication reconciles its parent to the authoritative completed revision", async () => {
  const fixture = setupTerminalFailedReadyAttempt("authoritative_parent");
  paperBundleListOverride = [completedBackendPaperBundlePublicationJob(
    fixture.parentId,
    fixture.bundle,
    8,
    fixture.sourceRunId,
    fixture.publishRunId,
    fixture.candidateId,
  )];
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${fixture.sourceRunId}/attempts/1/publish`) {
      return jsonResponse({
        run_id: fixture.publishRunId,
        start_token: "authoritative-token",
        progress_mode: "attempt_publish",
      });
    }
    if (url === `/api/runs/${fixture.publishRunId}/start`) {
      return jsonResponse({
        run_id: fixture.publishRunId,
        progress_mode: "attempt_publish",
        placeholder_message: {
          id: `msg_${fixture.publishRunId}`,
          role: "assistant",
          text: "",
          ts: 2,
        },
      });
    }
    if (url === `/api/runs/${fixture.publishRunId}/artifact`) {
      return jsonResponse(directAttemptPublicationResponse(
        fixture.publishRunId,
        fixture.sourceRunId,
        fixture.candidateId,
      ));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const action = useApp.getState().selectAttempt(
    fixture.sourceRunId,
    fixture.candidate,
    fixture.childId,
  );
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === `/api/runs/${fixture.publishRunId}/events`,
    ),
    "authoritative publication listener did not open",
  );
  MockEventSource.instances.find(
    (source) => source.url === `/api/runs/${fixture.publishRunId}/events`,
  )?.emit("run.done");
  await action;

  const parent = useApp.getState().conversations[fixture.parentId];
  const bundle = parent.paper_bundle as PaperBundleParentState;
  assert.deepEqual({
    backendState: bundle.backend_state,
    revision: bundle.revision,
    taskStatus: bundle.tasks.poster.status,
    taskRunId: bundle.tasks.poster.run_id,
    taskAuthoringRunId: bundle.tasks.poster.authoring_run_id,
    artifactId: bundle.tasks.poster.artifact_id,
  }, {
    backendState: "completed",
    revision: 8,
    taskStatus: "complete",
    taskRunId: fixture.publishRunId,
    taskAuthoringRunId: fixture.sourceRunId,
    artifactId: `art_${fixture.publishRunId}`,
  });
});

test("late authoritative publication reconciliation cannot overwrite a replacement job", async () => {
  const fixture = setupTerminalFailedReadyAttempt("stale_authoritative_parent");
  const oldJob = completedBackendPaperBundlePublicationJob(
    fixture.parentId,
    fixture.bundle,
    8,
    fixture.sourceRunId,
    fixture.publishRunId,
    fixture.candidateId,
  );
  let listRequested = false;
  let releaseList!: (response: Response) => void;
  paperBundleListResponseOverride = () => {
    listRequested = true;
    return new Promise<Response>((resolve) => { releaseList = resolve; });
  };
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${fixture.sourceRunId}/attempts/1/publish`) {
      return jsonResponse({
        run_id: fixture.publishRunId,
        start_token: "stale-authoritative-token",
        progress_mode: "attempt_publish",
      });
    }
    if (url === `/api/runs/${fixture.publishRunId}/start`) {
      return jsonResponse({
        run_id: fixture.publishRunId,
        progress_mode: "attempt_publish",
        placeholder_message: {
          id: `msg_${fixture.publishRunId}`,
          role: "assistant",
          text: "",
          ts: 2,
        },
      });
    }
    if (url === `/api/runs/${fixture.publishRunId}/artifact`) {
      return jsonResponse(directAttemptPublicationResponse(
        fixture.publishRunId,
        fixture.sourceRunId,
        fixture.candidateId,
      ));
    }
    if (url === "/api/runs/run_replacement_after_publication/artifact") {
      return jsonResponse({
        message: {
          id: "msg_replacement_after_publication_cancelled",
          role: "assistant",
          text: "Run cancelled.",
          ts: 21,
          run_id: "run_replacement_after_publication",
          status: "error",
          failure: {
            status: "cancelled",
            produced_files: [],
            artifact_type: "poster",
          },
        },
        artifact: null,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const action = useApp.getState().selectAttempt(
    fixture.sourceRunId,
    fixture.candidate,
    fixture.childId,
  );
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === `/api/runs/${fixture.publishRunId}/events`,
    ),
    "stale authoritative publication listener did not open",
  );
  MockEventSource.instances.find(
    (source) => source.url === `/api/runs/${fixture.publishRunId}/events`,
  )?.emit("run.done");
  await waitFor(() => listRequested, "authoritative parent reconciliation did not start");
  useApp.setState((state) => {
    const parent = state.conversations[fixture.parentId];
    if (parent.paper_bundle?.kind !== "parent") return state;
    return {
      conversations: {
        ...state.conversations,
        [fixture.parentId]: {
          ...parent,
          paper_bundle: {
            ...parent.paper_bundle,
            job_id: "job_replacement_after_publication",
            revision: 20,
            backend_state: "running",
            tasks: {
              ...parent.paper_bundle.tasks,
              poster: {
                ...parent.paper_bundle.tasks.poster,
                status: "running",
                run_id: "run_replacement_after_publication",
                authoring_run_id: "run_replacement_after_publication",
                artifact_id: undefined,
                terminal: false,
                process_free: false,
              },
            },
          },
        },
      },
    };
  });
  releaseList(jsonResponse([oldJob]));
  await action;

  const bundle = useApp.getState().conversations[fixture.parentId]
    .paper_bundle as PaperBundleParentState;
  assert.deepEqual({
    jobId: bundle.job_id,
    revision: bundle.revision,
    backendState: bundle.backend_state,
    taskStatus: bundle.tasks.poster.status,
    taskRunId: bundle.tasks.poster.run_id,
    taskArtifactId: bundle.tasks.poster.artifact_id,
  }, {
    jobId: "job_replacement_after_publication",
    revision: 20,
    backendState: "running",
    taskStatus: "running",
    taskRunId: "run_replacement_after_publication",
    taskArtifactId: undefined,
  });
  const replacementRecovery = MockEventSource.instances.find(
    (source) => source.url === "/api/runs/run_replacement_after_publication/events",
  );
  replacementRecovery?.emit("run.cancelled");
  await waitFor(
    () => replacementRecovery?.readyState === MockEventSource.CLOSED,
    "replacement job recovery did not release its terminal wait",
  );
});

test("a terminal direct publication installs its token owner before reserve and cancels a late acknowledgement", async () => {
  const fixture = setupTerminalFailedReadyAttempt("late_ack");
  let releaseReservation!: (response: Response) => void;
  let reservationSignal: AbortSignal | undefined;
  let cancelPosts = 0;
  let startPosts = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === `/api/runs/${fixture.sourceRunId}/attempts/1/publish`) {
      reservationSignal = init?.signal ?? undefined;
      return new Promise<Response>((resolve) => { releaseReservation = resolve; });
    }
    if (url === `/api/runs/${fixture.publishRunId}/start`) {
      startPosts += 1;
      return jsonResponse({ detail: "cancelled publication must not start" }, 500);
    }
    if (url === `/api/runs/${fixture.publishRunId}/cancel`) {
      cancelPosts += 1;
      return confirmedCancellation(fixture.publishRunId);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const publishing = useApp.getState().selectAttempt(
    fixture.sourceRunId,
    fixture.candidate,
    fixture.childId,
  );
  await waitFor(() => Boolean(reservationSignal), "direct reserve did not begin");
  const ownerBeforeAck = useApp.getState().candidate_publication_owners[fixture.childId];
  assert.ok(ownerBeforeAck?.token);
  assert.equal(candidatePublicationIsActive(useApp.getState(), fixture.childId), true);

  const cancelling = useApp.getState().cancelRun(fixture.childId);
  await waitFor(
    () => reservationSignal?.aborted === true,
    "pre-ack cancellation did not abort the direct reservation",
  );
  releaseReservation(jsonResponse({
    run_id: fixture.publishRunId,
    start_token: "late-token",
    progress_mode: "attempt_publish",
  }));
  await Promise.all([publishing, cancelling]);

  const settled = useApp.getState();
  assert.deepEqual({
    cancelPosts,
    startPosts,
    ownerCleared: settled.candidate_publication_owners[fixture.childId] === undefined,
    active: candidatePublicationIsActive(settled, fixture.childId),
    published: settled.conversations[fixture.childId].published_artifact_id,
  }, {
    cancelPosts: 1,
    startPosts: 0,
    ownerCleared: true,
    active: false,
    published: undefined,
  });
});

test("recovery reconnects a persisted direct candidate publication without a source artifact id", async () => {
  const fixture = setupTerminalFailedReadyAttempt("recovery");
  const streamingMessage = {
    id: `msg_${fixture.publishRunId}`,
    role: "assistant" as const,
    text: "Publishing selected attempt.",
    ts: 2,
    run_id: fixture.publishRunId,
    status: "streaming" as const,
    task_type: "candidate_publish" as const,
    task_payload: {
      artifact_type: "poster" as const,
      source_run_id: fixture.sourceRunId,
      source_candidate_id: fixture.candidateId,
    },
  };
  useApp.setState((state) => ({
    conversations: {
      ...state.conversations,
      [fixture.childId]: {
        ...state.conversations[fixture.childId],
        pending: true,
        run_id: fixture.publishRunId,
        messages: [
          ...state.conversations[fixture.childId].messages,
          streamingMessage,
        ],
      },
    },
  }));
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${fixture.publishRunId}/artifact`) {
      return jsonResponse(directAttemptPublicationResponse(
        fixture.publishRunId,
        fixture.sourceRunId,
        fixture.candidateId,
      ));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  useApp.getState().recoverActiveRuns();
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === `/api/runs/${fixture.publishRunId}/events`,
    ),
    "persisted direct publication did not reconnect",
  );
  MockEventSource.instances.find(
    (source) => source.url === `/api/runs/${fixture.publishRunId}/events`,
  )?.emit("run.done");
  await waitFor(
    () => useApp.getState().conversations[fixture.childId]
      .published_artifact_id === `art_${fixture.publishRunId}`,
    "recovered direct publication did not settle",
  );
});

test("run_not_selectable falls back from a live legacy selection to direct publication", async () => {
  const fixture = setupTerminalFailedReadyAttempt("fallback_run_not_selectable");
  useApp.setState((state) => ({
    conversations: {
      ...state.conversations,
      [fixture.childId]: {
        ...state.conversations[fixture.childId],
        pending: true,
        run_id: fixture.sourceRunId,
      },
    },
  }));
  let legacySelections = 0;
  let directReservations = 0;
  let legacyIdempotencyKey: string | undefined;
  let directIdempotencyKey: string | undefined;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === `/api/runs/${fixture.sourceRunId}/attempts/1/select`) {
      legacySelections += 1;
      legacyIdempotencyKey = JSON.parse(String(init?.body)).idempotency_key;
      return jsonResponse({ detail: { code: "run_not_selectable" } }, 409);
    }
    if (url === `/api/runs/${fixture.sourceRunId}/attempts/1/publish`) {
      directReservations += 1;
      directIdempotencyKey = JSON.parse(String(init?.body)).idempotency_key;
      return jsonResponse({
        run_id: fixture.publishRunId,
        start_token: "fallback-token",
        progress_mode: "attempt_publish",
      });
    }
    if (url === `/api/runs/${fixture.publishRunId}/start`) {
      return jsonResponse({
        run_id: fixture.publishRunId,
        progress_mode: "attempt_publish",
        placeholder_message: {
          id: `msg_${fixture.publishRunId}`,
          role: "assistant",
          text: "",
          ts: 2,
        },
      });
    }
    if (url === `/api/runs/${fixture.publishRunId}/artifact`) {
      return jsonResponse(directAttemptPublicationResponse(
        fixture.publishRunId,
        fixture.sourceRunId,
        fixture.candidateId,
      ));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const action = useApp.getState().selectAttempt(
    fixture.sourceRunId,
    fixture.candidate,
    fixture.childId,
  );
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === `/api/runs/${fixture.publishRunId}/events`,
    ),
    "run_not_selectable did not fall back",
  );
  MockEventSource.instances.find(
    (source) => source.url === `/api/runs/${fixture.publishRunId}/events`,
  )?.emit("run.done");
  await action;
  assert.equal(legacySelections, 1);
  assert.equal(directReservations, 1);
  assert.ok(legacyIdempotencyKey);
  assert.equal(directIdempotencyKey, legacyIdempotencyKey);
});

test("candidate_changed never falls back from legacy selection", async () => {
  const fixture = setupTerminalFailedReadyAttempt("fallback_candidate_changed");
  useApp.setState((state) => ({
    conversations: {
      ...state.conversations,
      [fixture.childId]: {
        ...state.conversations[fixture.childId],
        pending: true,
        run_id: fixture.sourceRunId,
      },
    },
  }));
  let directReservations = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${fixture.sourceRunId}/attempts/1/select`) {
      return jsonResponse({ detail: { code: "candidate_changed" } }, 409);
    }
    if (url === `/api/runs/${fixture.sourceRunId}/attempts/1/publish`) {
      directReservations += 1;
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await assert.rejects(
    useApp.getState().selectAttempt(
      fixture.sourceRunId,
      fixture.candidate,
      fixture.childId,
    ),
    (error: unknown) => Boolean(
      error && typeof error === "object" && "code" in error
      && error.code === "candidate_changed"
    ),
  );
  assert.equal(directReservations, 0);
});

test("an active source keeps the legacy select path when selection is accepted", async () => {
  const fixture = setupTerminalFailedReadyAttempt("legacy_control");
  useApp.setState((state) => ({
    conversations: {
      ...state.conversations,
      [fixture.childId]: {
        ...state.conversations[fixture.childId],
        pending: true,
        run_id: fixture.sourceRunId,
      },
    },
  }));
  let legacySelections = 0;
  let directReservations = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${fixture.sourceRunId}/attempts/1/select`) {
      legacySelections += 1;
      return jsonResponse({
        run_id: fixture.sourceRunId,
        candidates: [fixture.candidate],
        selection: {
          candidate_id: fixture.candidateId,
          source_attempt: 1,
          state: "requested",
        },
      });
    }
    if (url === `/api/runs/${fixture.sourceRunId}/attempts/1/publish`) {
      directReservations += 1;
      throw new Error("active source must not start a derived publication");
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().selectAttempt(
    fixture.sourceRunId,
    fixture.candidate,
    fixture.childId,
  );

  assert.equal(legacySelections, 1);
  assert.equal(directReservations, 0);
  assert.equal(
    useApp.getState().run_attempts[fixture.sourceRunId].selection_phase,
    "requested",
  );
});

test("terminal publication defaults to the current Inspector conversation when no target is passed", async () => {
  const fixture = setupTerminalFailedReadyAttempt("current_inspector");
  let requestBody: Record<string, unknown> | undefined;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === `/api/runs/${fixture.sourceRunId}/attempts/1/publish`) {
      requestBody = JSON.parse(String(init?.body));
      return jsonResponse({ detail: { code: "test_stop_after_target_capture" } }, 422);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await assert.rejects(
    useApp.getState().selectAttempt(fixture.sourceRunId, fixture.candidate),
  );

  assert.equal(requestBody?.conversation_id, fixture.childId);
  assert.equal(requestBody?.expected_candidate_sha256, fixture.candidate.source_sha256);
});

test("a direct publication cannot overwrite a newer parent task generation", async () => {
  const fixture = setupTerminalFailedReadyAttempt("newer_job");
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${fixture.sourceRunId}/attempts/1/publish`) {
      return jsonResponse({
        run_id: fixture.publishRunId,
        start_token: "newer-job-token",
        progress_mode: "attempt_publish",
      });
    }
    if (url === `/api/runs/${fixture.publishRunId}/start`) {
      return jsonResponse({
        run_id: fixture.publishRunId,
        progress_mode: "attempt_publish",
        placeholder_message: {
          id: `msg_${fixture.publishRunId}`,
          role: "assistant",
          text: "",
          ts: 2,
        },
      });
    }
    if (url === `/api/runs/${fixture.publishRunId}/artifact`) {
      return jsonResponse(directAttemptPublicationResponse(
        fixture.publishRunId,
        fixture.sourceRunId,
        fixture.candidateId,
      ));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const publishing = useApp.getState().selectAttempt(
    fixture.sourceRunId,
    fixture.candidate,
    fixture.childId,
  );
  await waitFor(
    () => MockEventSource.instances.some(
      (source) => source.url === `/api/runs/${fixture.publishRunId}/events`,
    ),
    "direct publication listener did not open",
  );
  useApp.setState((state) => {
    const parent = state.conversations[fixture.parentId];
    if (parent.paper_bundle?.kind !== "parent") return state;
    return {
      conversations: {
        ...state.conversations,
        [fixture.parentId]: {
          ...parent,
          paper_bundle: {
            ...parent.paper_bundle,
            job_id: "job_replacement",
            revision: 8,
            backend_state: "running",
            tasks: {
              ...parent.paper_bundle.tasks,
              poster: {
                ...parent.paper_bundle.tasks.poster,
                status: "running",
                run_id: "run_replacement",
                authoring_run_id: "run_replacement",
                artifact_id: undefined,
                error: undefined,
                terminal: false,
                process_free: false,
              },
            },
          },
        },
      },
    };
  });
  MockEventSource.instances.find(
    (source) => source.url === `/api/runs/${fixture.publishRunId}/events`,
  )?.emit("run.done");
  await publishing;

  const state = useApp.getState();
  const parent = state.conversations[fixture.parentId];
  const task = parent.paper_bundle?.kind === "parent"
    ? parent.paper_bundle.tasks.poster
    : undefined;
  assert.deepEqual({
    jobId: parent.paper_bundle?.kind === "parent" ? parent.paper_bundle.job_id : undefined,
    runId: task?.run_id,
    status: task?.status,
    artifactId: task?.artifact_id,
    parentReceivedStaleArtifact: Boolean(parent.artifacts[`art_${fixture.publishRunId}`]),
  }, {
    jobId: "job_replacement",
    runId: "run_replacement",
    status: "running",
    artifactId: undefined,
    parentReceivedStaleArtifact: false,
  });
});

test("paper bundle API migrates schema v1 records with an empty publication overlay", async () => {
  const parentId = "bundle_schema_v1";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_schema_v1";
  resetStore({}, parentId);
  globalThis.fetch = (async (input) => {
    throw new Error(`Unexpected fetch: ${String(input)}`);
  }) as typeof fetch;
  paperBundleListOverride = [backendPaperBundleJob(parentId, bundle, 1)];

  const [parsed] = await listPaperBundles();

  assert.equal(parsed.schema_version, 1);
  assert.deepEqual(parsed.publications, {});
});

test("paper bundle API accepts a strict schema v2 publication overlay", async () => {
  const parentId = "bundle_schema_v2";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_schema_v2";
  resetStore({}, parentId);
  globalThis.fetch = (async (input) => {
    throw new Error(`Unexpected fetch: ${String(input)}`);
  }) as typeof fetch;
  paperBundleListOverride = [backendPaperBundlePublicationJob(parentId, bundle, 4)];

  const [parsed] = await listPaperBundles();

  assert.equal(parsed.schema_version, 2);
  assert.deepEqual(parsed.completed_children, ["poster"]);
  assert.equal(
    parsed.publications.poster?.publication_run_id,
    "run_publication_derived",
  );
});

test("paper bundle API recovers a publication from a completed process-free source", async () => {
  const parentId = "bundle_schema_v2_completed_source";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_schema_v2_completed_source";
  resetStore({}, parentId);
  globalThis.fetch = (async (input) => {
    throw new Error(`Unexpected fetch: ${String(input)}`);
  }) as typeof fetch;
  const payload = backendPaperBundlePublicationJob(parentId, bundle, 4);
  payload.children.poster.state = "completed";
  paperBundleListOverride = [payload];

  const [parsed] = await listPaperBundles();

  assert.equal(parsed.children.poster.state, "completed");
  assert.equal(parsed.children.poster.terminal, true);
  assert.equal(parsed.children.poster.process_free, true);
  assert.equal(
    parsed.publications.poster?.publication_run_id,
    "run_publication_derived",
  );
});

test("paper bundle API rejects malformed schema v2 publication overlays", async () => {
  const parentId = "bundle_schema_v2_invalid";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_schema_v2_invalid";
  resetStore({}, parentId);
  globalThis.fetch = (async (input) => {
    throw new Error(`Unexpected fetch: ${String(input)}`);
  }) as typeof fetch;
  const valid = backendPaperBundlePublicationJob(parentId, bundle, 4);
  const keepParentNonterminal = (payload: Record<string, any>) => {
    payload.state = "running";
    payload.terminal = false;
    payload.terminal_at = null;
    for (const artifactType of ["deck", "landing", "video"]) {
      payload.children[artifactType].state = "running";
      payload.children[artifactType].terminal = false;
      payload.children[artifactType].process_free = false;
    }
  };
  const malformed: Array<[string, (payload: Record<string, any>) => void]> = [
    ["missing publications", (payload) => { delete payload.publications; }],
    ["unknown artifact key", (payload) => { payload.publications.audio = payload.publications.poster; }],
    ["source lineage mismatch", (payload) => { payload.publications.poster.source_run_id = "other_source"; }],
    ["source child is running", (payload) => {
      keepParentNonterminal(payload);
      payload.children.poster.state = "running";
      payload.children.poster.terminal = false;
    }],
    ["source child is cancelled", (payload) => { payload.children.poster.state = "cancelled"; }],
    ["source child is non-terminal", (payload) => {
      keepParentNonterminal(payload);
      payload.children.poster.state = "completing";
      payload.children.poster.terminal = false;
    }],
    ["source child is not process-free", (payload) => {
      keepParentNonterminal(payload);
      payload.children.poster.process_free = false;
    }],
    ["unsafe publication run id", (payload) => { payload.publications.poster.publication_run_id = "../escape"; }],
    ["unsafe artifact id", (payload) => { payload.publications.poster.artifact_id = "bad/artifact"; }],
    ["unsafe candidate id", (payload) => { payload.publications.poster.source_candidate_id = "bad/candidate"; }],
    ["non-lowercase sha", (payload) => { payload.publications.poster.source_candidate_sha256 = "D".repeat(64); }],
    ["zero source attempt", (payload) => { payload.publications.poster.source_attempt = 0; }],
    ["boolean generation", (payload) => { payload.publications.poster.generation = true; }],
    ["timestamp before job creation", (payload) => { payload.publications.poster.published_at = 0; }],
    ["unknown publication field", (payload) => { payload.publications.poster.unexpected = "value"; }],
  ];

  for (const [label, mutate] of malformed) {
    const payload = JSON.parse(JSON.stringify(valid)) as Record<string, any>;
    mutate(payload);
    paperBundleListOverride = [payload];
    await assert.rejects(listPaperBundles(), { message: "Invalid paper bundle response." }, label);
  }
});

test("confirmed bundle cancellation preserves a durable direct publication", async () => {
  const parentId = "bundle_publication_cancel";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_publication_cancel";
  bundle.revision = 3;
  bundle.backend_state = "running";
  const sourceRunId = "run_publication_cancel_source";
  const publicationRunId = "run_publication_cancel_derived";
  const artifactId = `art_${publicationRunId}`;
  bundle.tasks.poster = {
    ...bundle.tasks.poster,
    status: "complete",
    run_id: publicationRunId,
    authoring_run_id: sourceRunId,
    artifact_id: artifactId,
    terminal: true,
    process_free: true,
  };
  for (const artifactType of ["deck", "landing", "video"] as const) {
    bundle.tasks[artifactType] = {
      ...bundle.tasks[artifactType],
      status: "running",
      run_id: `run_publication_cancel_${artifactType}`,
      authoring_run_id: `run_publication_cancel_${artifactType}`,
    };
  }
  resetStore({
    [parentId]: conversation(parentId, { paper_bundle: bundle, pending: true }),
  }, parentId);
  globalThis.fetch = (async (input) => {
    throw new Error(`Unexpected fetch: ${String(input)}`);
  }) as typeof fetch;
  const terminal = backendPaperBundlePublicationJob(parentId, bundle, 4, {
    source_run_id: sourceRunId,
    publication_run_id: publicationRunId,
    artifact_id: artifactId,
  });
  terminal.state = "cancelled";
  terminal.cancel_requested = true;
  terminal.cancel_requested_at = terminal.updated_at;
  paperBundleCancelOverride = {
    status: 200,
    body: {
      ...terminal,
      confirmed: true,
      status: "cancelled",
    },
  };

  await useApp.getState().cancelPaperBundle(parentId);

  const recovered = useApp.getState().conversations[parentId]
    .paper_bundle as PaperBundleParentState;
  assert.deepEqual({
    backendState: recovered.backend_state,
    status: recovered.tasks.poster.status,
    runId: recovered.tasks.poster.run_id,
    authoringRunId: recovered.tasks.poster.authoring_run_id,
    artifactId: recovered.tasks.poster.artifact_id,
    error: recovered.tasks.poster.error,
    cancelError: recovered.cancel_error,
  }, {
    backendState: "cancelled",
    status: "complete",
    runId: publicationRunId,
    authoringRunId: sourceRunId,
    artifactId,
    error: undefined,
    cancelError: undefined,
  });
});

test("paper bundle recovery hydrates a durable direct publication without local history", async () => {
  const parentId = "bundle_publication_recovery";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_publication_recovery";
  const sourceRunId = "run_publication_source";
  const publicationRunId = "run_publication_derived";
  const childId = bundle.tasks.poster.child_conversation_id;
  resetStore({}, parentId);
  paperBundleListOverride = [backendPaperBundlePublicationJob(parentId, bundle, 4, {
    source_run_id: sourceRunId,
    publication_run_id: publicationRunId,
  })];
  const artifactRequests: string[] = [];
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${publicationRunId}/artifact`) {
      artifactRequests.push(url);
      return jsonResponse(publishedArtifactResponse(publicationRunId, sourceRunId));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().recoverPaperBundles();

  const state = useApp.getState();
  const parent = state.conversations[parentId];
  const recoveredBundle = parent.paper_bundle as PaperBundleParentState;
  const task = recoveredBundle.tasks.poster;
  const publishedArtifactId = `art_${publicationRunId}`;
  assert.deepEqual({
    status: task.status,
    runId: task.run_id,
    authoringRunId: task.authoring_run_id,
    artifactId: task.artifact_id,
    error: task.error,
    terminal: task.terminal,
    processFree: task.process_free,
    childArtifact: state.conversations[childId].artifacts[publishedArtifactId]?.artifact_id,
    childActive: state.conversations[childId].active_artifact_id,
    parentArtifact: parent.artifacts[publishedArtifactId]?.artifact_id,
    parentActive: parent.active_artifact_id,
    requests: artifactRequests,
  }, {
    status: "complete",
    runId: publicationRunId,
    authoringRunId: sourceRunId,
    artifactId: publishedArtifactId,
    error: undefined,
    terminal: true,
    processFree: true,
    childArtifact: publishedArtifactId,
    childActive: publishedArtifactId,
    parentArtifact: publishedArtifactId,
    parentActive: publishedArtifactId,
    requests: [`/api/runs/${publicationRunId}/artifact`],
  });
});

test("publication hydration does not delay recovery of an active sibling stream", async () => {
  const parentId = "bundle_publication_active_sibling";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_publication_active_sibling";
  const sourceRunId = "run_publication_active_sibling_source";
  const publicationRunId = "run_publication_active_sibling_derived";
  const deckRunId = "run_publication_active_sibling_deck";
  resetStore({}, parentId);
  paperBundleListOverride = [runningBackendPaperBundlePublicationJob(
    parentId,
    bundle,
    4,
    sourceRunId,
    publicationRunId,
    deckRunId,
  )];
  let releasePublication!: (response: Response) => void;
  let publicationFetchStarted = false;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${publicationRunId}/artifact`) {
      publicationFetchStarted = true;
      return new Promise<Response>((resolve) => { releasePublication = resolve; });
    }
    if (url === `/api/runs/${deckRunId}/artifact`) {
      return jsonResponse(responseForRun(deckRunId, "deck"));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const recovery = useApp.getState().recoverPaperBundles();
  await waitFor(() => publicationFetchStarted, "publication hydration did not start");
  for (let turn = 0; turn < 5; turn += 1) await tick();
  const siblingRecoveredBeforePublication = MockEventSource.instances.some(
    (source) => source.url === `/api/runs/${deckRunId}/events`,
  );
  releasePublication(jsonResponse(publishedArtifactResponse(publicationRunId, sourceRunId)));
  await recovery;
  const deckSource = MockEventSource.instances.find(
    (source) => source.url === `/api/runs/${deckRunId}/events`,
  );
  deckSource?.emit("run.done");
  await waitFor(
    () => (useApp.getState().conversations[parentId]
      .paper_bundle as PaperBundleParentState).tasks.deck.status === "complete",
    "active sibling recovery did not settle",
  );

  assert.equal(siblingRecoveredBeforePublication, true);
});

test("publication hydration survives a newer confirmed cancellation revision", async () => {
  const parentId = "bundle_publication_cancel_hydration";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_publication_cancel_hydration";
  const sourceRunId = "run_publication_cancel_hydration_source";
  const publicationRunId = "run_publication_cancel_hydration_derived";
  const deckRunId = "run_publication_cancel_hydration_deck";
  const childId = bundle.tasks.poster.child_conversation_id;
  resetStore({}, parentId);
  paperBundleListOverride = [runningBackendPaperBundlePublicationJob(
    parentId,
    bundle,
    4,
    sourceRunId,
    publicationRunId,
    deckRunId,
  )];
  let releasePublication!: (response: Response) => void;
  let publicationFetchStarted = false;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${publicationRunId}/artifact`) {
      publicationFetchStarted = true;
      return new Promise<Response>((resolve) => { releasePublication = resolve; });
    }
    if (url === `/api/runs/${deckRunId}/artifact`) {
      return jsonResponse({
        message: {
          id: `msg_${deckRunId}`,
          role: "assistant",
          text: "Run cancelled.",
          ts: 3,
          run_id: deckRunId,
          status: "error",
          failure: { status: "cancelled", produced_files: [] },
        },
        artifact: null,
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const recovery = useApp.getState().recoverPaperBundles();
  await waitFor(() => publicationFetchStarted, "publication hydration did not start");
  const terminal = backendPaperBundlePublicationJob(parentId, bundle, 5, {
    source_run_id: sourceRunId,
    publication_run_id: publicationRunId,
  });
  terminal.state = "cancelled";
  terminal.cancel_requested = true;
  terminal.cancel_requested_at = terminal.updated_at;
  paperBundleCancelOverride = {
    status: 200,
    body: {
      ...terminal,
      confirmed: true,
      status: "cancelled",
    },
  };
  await useApp.getState().cancelPaperBundle(parentId);
  MockEventSource.instances.find(
    (source) => source.url === `/api/runs/${deckRunId}/events`,
  )?.emit("run.cancelled");
  releasePublication(jsonResponse(publishedArtifactResponse(publicationRunId, sourceRunId)));
  await recovery;

  const state = useApp.getState();
  const recovered = state.conversations[parentId].paper_bundle as PaperBundleParentState;
  assert.equal(recovered.revision, 5);
  assert.equal(recovered.backend_state, "cancelled");
  assert.equal(recovered.tasks.poster.status, "complete");
  assert.equal(
    state.conversations[childId].active_artifact_id,
    `art_${publicationRunId}`,
  );
});

test("paper bundle publication hydration retries after a transient artifact fetch failure", async () => {
  const parentId = "bundle_publication_retry";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_publication_retry";
  const sourceRunId = "run_publication_retry_source";
  const publicationRunId = "run_publication_retry_derived";
  const childId = bundle.tasks.poster.child_conversation_id;
  resetStore({}, parentId);
  paperBundleListOverride = [backendPaperBundlePublicationJob(parentId, bundle, 4, {
    source_run_id: sourceRunId,
    publication_run_id: publicationRunId,
  })];
  let fetchAttempts = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${publicationRunId}/artifact`) {
      fetchAttempts += 1;
      return fetchAttempts === 1
        ? jsonResponse({ detail: "artifact still syncing" }, 503)
        : jsonResponse(publishedArtifactResponse(publicationRunId, sourceRunId));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().recoverPaperBundles();
  const state = useApp.getState();
  const task = (state.conversations[parentId].paper_bundle as PaperBundleParentState).tasks.poster;
  assert.deepEqual({
    fetchAttempts,
    status: task.status,
    error: task.error,
    artifactId: task.artifact_id,
    hydrated: Boolean(state.conversations[childId].artifacts[task.artifact_id ?? ""]),
  }, {
    fetchAttempts: 2,
    status: "complete",
    error: undefined,
    artifactId: `art_${publicationRunId}`,
    hydrated: true,
  });
});

test("concurrent recovery deduplicates publication artifact hydration", async () => {
  const parentId = "bundle_publication_deduplicated";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_publication_deduplicated";
  const sourceRunId = "run_publication_deduplicated_source";
  const publicationRunId = "run_publication_deduplicated_derived";
  const childId = bundle.tasks.poster.child_conversation_id;
  resetStore({}, parentId);
  paperBundleListOverride = [backendPaperBundlePublicationJob(parentId, bundle, 4, {
    source_run_id: sourceRunId,
    publication_run_id: publicationRunId,
  })];
  let releaseArtifact!: (response: Response) => void;
  let fetchAttempts = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${publicationRunId}/artifact`) {
      fetchAttempts += 1;
      return new Promise<Response>((resolve) => { releaseArtifact = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const first = useApp.getState().recoverPaperBundles();
  await waitFor(() => fetchAttempts === 1, "publication hydration did not start");
  const second = useApp.getState().recoverPaperBundles();
  await tick();
  assert.equal(fetchAttempts, 1);
  releaseArtifact(jsonResponse(publishedArtifactResponse(publicationRunId, sourceRunId)));
  await Promise.all([first, second]);

  assert.equal(fetchAttempts, 1);
  assert.equal(
    useApp.getState().conversations[childId].active_artifact_id,
    `art_${publicationRunId}`,
  );
});

test("permanent publication hydration failure is bounded and stays non-failing", async () => {
  const parentId = "bundle_publication_bounded";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_publication_bounded";
  const sourceRunId = "run_publication_bounded_source";
  const publicationRunId = "run_publication_bounded_derived";
  const childId = bundle.tasks.poster.child_conversation_id;
  resetStore({}, parentId);
  paperBundleListOverride = [backendPaperBundlePublicationJob(parentId, bundle, 4, {
    source_run_id: sourceRunId,
    publication_run_id: publicationRunId,
  })];
  let fetchAttempts = 0;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${publicationRunId}/artifact`) {
      fetchAttempts += 1;
      return jsonResponse({ detail: "artifact remains unavailable" }, 503);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;
  const realSetTimeout = window.setTimeout.bind(window);
  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => (
    realSetTimeout(
      handler,
      typeof timeout === "number" && timeout >= 50_000
        ? 5
        : timeout === 250 ? 10 : timeout,
      ...args,
    )
  )) as typeof window.setTimeout;
  try {
    await useApp.getState().recoverPaperBundles();
  } finally {
    window.setTimeout = realSetTimeout as typeof window.setTimeout;
  }

  const state = useApp.getState();
  const task = (state.conversations[parentId].paper_bundle as PaperBundleParentState).tasks.poster;
  assert.ok(fetchAttempts > 0);
  assert.ok(fetchAttempts < 20);
  assert.equal(task.status, "complete");
  assert.equal(task.error, undefined);
  assert.equal(state.conversations[childId].active_artifact_id, null);
});

test("paper bundle publication hydration fails closed on artifact lineage mismatch", async () => {
  const parentId = "bundle_publication_lineage";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_publication_lineage";
  const sourceRunId = "run_publication_lineage_source";
  const publicationRunId = "run_publication_lineage_derived";
  const childId = bundle.tasks.poster.child_conversation_id;
  resetStore({}, parentId);
  paperBundleListOverride = [backendPaperBundlePublicationJob(parentId, bundle, 4, {
    source_run_id: sourceRunId,
    publication_run_id: publicationRunId,
  })];
  let valid = false;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${publicationRunId}/artifact`) {
      return jsonResponse(publishedArtifactResponse(
        publicationRunId,
        sourceRunId,
        valid ? {} : { source_run_id: "wrong_source" },
      ));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  await useApp.getState().recoverPaperBundles();
  assert.equal(
    Object.keys(useApp.getState().conversations[childId].artifacts).length,
    0,
  );
  assert.equal(
    (useApp.getState().conversations[parentId].paper_bundle as PaperBundleParentState)
      .tasks.poster.status,
    "complete",
  );

  valid = true;
  await useApp.getState().recoverPaperBundles();
  assert.equal(
    useApp.getState().conversations[childId].active_artifact_id,
    `art_${publicationRunId}`,
  );
});

test("late publication hydration cannot overwrite a newer paper bundle revision", async () => {
  const parentId = "bundle_publication_order";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_publication_order";
  const childId = bundle.tasks.poster.child_conversation_id;
  const sourceRunId = "run_publication_order_source";
  const oldRunId = "run_publication_order_old";
  const newRunId = "run_publication_order_new";
  const oldJob = backendPaperBundlePublicationJob(parentId, bundle, 2, {
    source_run_id: sourceRunId,
    publication_run_id: oldRunId,
    generation: 1,
  });
  const newJob = backendPaperBundlePublicationJob(parentId, bundle, 3, {
    source_run_id: sourceRunId,
    publication_run_id: newRunId,
    generation: 2,
    published_at: 2.5,
  });
  resetStore({}, parentId);
  paperBundleListOverride = [oldJob];
  let releaseOld!: (response: Response) => void;
  let oldFetchStarted = false;
  const artifactRequests: string[] = [];
  globalThis.fetch = (async (input) => {
    const url = String(input);
    artifactRequests.push(url);
    if (url === `/api/runs/${oldRunId}/artifact`) {
      oldFetchStarted = true;
      return new Promise<Response>((resolve) => { releaseOld = resolve; });
    }
    if (url === `/api/runs/${newRunId}/artifact`) {
      return jsonResponse(publishedArtifactResponse(newRunId, sourceRunId));
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const oldRecovery = useApp.getState().recoverPaperBundles();
  await waitFor(() => oldFetchStarted, "old publication artifact request did not start");
  paperBundleListOverride = [newJob];
  await useApp.getState().recoverPaperBundles();
  releaseOld(jsonResponse(publishedArtifactResponse(oldRunId, sourceRunId)));
  await oldRecovery;

  paperBundleListOverride = [oldJob];
  await useApp.getState().recoverPaperBundles();
  const state = useApp.getState();
  const task = (state.conversations[parentId].paper_bundle as PaperBundleParentState).tasks.poster;
  assert.deepEqual({
    revision: (state.conversations[parentId].paper_bundle as PaperBundleParentState).revision,
    runId: task.run_id,
    artifactId: task.artifact_id,
    childActive: state.conversations[childId].active_artifact_id,
    parentHasOld: Boolean(state.conversations[parentId].artifacts[`art_${oldRunId}`]),
    parentHasNew: Boolean(state.conversations[parentId].artifacts[`art_${newRunId}`]),
    oldRequests: artifactRequests.filter((url) => url.includes(oldRunId)).length,
  }, {
    revision: 3,
    runId: newRunId,
    artifactId: `art_${newRunId}`,
    childActive: `art_${newRunId}`,
    parentHasOld: false,
    parentHasNew: true,
    oldRequests: 1,
  });
});

test("late publication hydration from an old user scope is ignored", async () => {
  const parentId = "bundle_publication_old_scope";
  const bundle = createPaperBundleParentState(parentId, "paper.pdf");
  bundle.job_id = "job_publication_old_scope";
  const sourceRunId = "run_publication_old_scope_source";
  const publicationRunId = "run_publication_old_scope_derived";
  const childId = bundle.tasks.poster.child_conversation_id;
  resetStore({}, parentId);
  paperBundleListOverride = [backendPaperBundlePublicationJob(parentId, bundle, 4, {
    source_run_id: sourceRunId,
    publication_run_id: publicationRunId,
  })];
  let releaseArtifact!: (response: Response) => void;
  let artifactFetchStarted = false;
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url === `/api/runs/${publicationRunId}/artifact`) {
      artifactFetchStarted = true;
      return new Promise<Response>((resolve) => { releaseArtifact = resolve; });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const recovery = useApp.getState().recoverPaperBundles();
  await waitFor(() => artifactFetchStarted, "publication artifact request did not start");
  localStorage.setItem("autodesign.demo_user.v1", "new-user");
  releaseArtifact(jsonResponse(publishedArtifactResponse(publicationRunId, sourceRunId)));
  await recovery;

  const state = useApp.getState();
  assert.equal(state.conversations[childId].active_artifact_id, null);
  assert.equal(state.conversations[parentId].artifacts[`art_${publicationRunId}`], undefined);
});

test("attempt hydration times out into a retryable terminal state", async () => {
  const runId = "run_attempt_hydration_timeout";
  resetStore({}, "conv_attempt_hydration_timeout");
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = window.setTimeout;
  const originalClearTimeout = window.clearTimeout;
  const timeoutId = 991_001;
  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    if (timeout === 10_000 && typeof handler === "function") {
      queueMicrotask(() => handler(...args));
      return timeoutId;
    }
    return originalSetTimeout(handler, timeout, ...args);
  }) as typeof window.setTimeout;
  window.clearTimeout = ((timer?: number) => {
    if (timer !== timeoutId) originalClearTimeout(timer);
  }) as typeof window.clearTimeout;
  globalThis.fetch = (async (input, init) => {
    assert.equal(String(input), `/api/runs/${runId}/attempts`);
    return new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => {
        reject(init.signal?.reason ?? new DOMException("Aborted", "AbortError"));
      }, { once: true });
    });
  }) as typeof fetch;

  try {
    await useApp.getState().loadRunAttempts(runId);
    const state = useApp.getState().run_attempts[runId];
    assert.equal(state?.loading, false);
    assert.match(state?.error ?? "", /timed out/i);
  } finally {
    globalThis.fetch = originalFetch;
    window.setTimeout = originalSetTimeout;
    window.clearTimeout = originalClearTimeout;
  }
});

function attemptCandidatePayload(runId: string, attempt: number) {
  return {
    candidate_id: `candidate-${attempt}`,
    run_id: runId,
    artifact_type: "deck",
    attempt,
    max_attempts: 4,
    created_at: `2026-08-06T00:00:0${attempt}Z`,
    source_sha256: String(attempt).repeat(64),
    safety_state: "ready",
    hard_blockers: [],
    warnings: [],
    source_url: `/api/files/runs/${runId}/candidate-${attempt}/slides.html`,
    preview_urls: [],
  };
}

test("newer attempt hydration cannot be overwritten by an older response", async () => {
  const runId = "run_attempt_hydration_out_of_order";
  resetStore({}, "conv_attempt_hydration_out_of_order");
  const originalFetch = globalThis.fetch;
  const requests: Array<(response: Response) => void> = [];
  globalThis.fetch = (async (input) => {
    assert.equal(String(input), `/api/runs/${runId}/attempts`);
    return new Promise<Response>((resolve) => requests.push(resolve));
  }) as typeof fetch;

  try {
    const older = useApp.getState().loadRunAttempts(runId);
    const newer = useApp.getState().loadRunAttempts(runId);
    await waitFor(() => requests.length === 2, "attempt hydration requests did not start");
    requests[1](jsonResponse({
      run_id: runId,
      candidates: [attemptCandidatePayload(runId, 2)],
    }));
    await newer;
    assert.deepEqual(
      useApp.getState().run_attempts[runId]?.candidates.map((candidate) => candidate.attempt),
      [2],
    );
    requests[0](jsonResponse({
      run_id: runId,
      candidates: [attemptCandidatePayload(runId, 1)],
    }));
    await older;
    assert.deepEqual(
      useApp.getState().run_attempts[runId]?.candidates.map((candidate) => candidate.attempt),
      [2],
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("a stale attempt hydration timeout cannot overwrite a newer success", async () => {
  const runId = "run_attempt_hydration_stale_timeout";
  resetStore({}, "conv_attempt_hydration_stale_timeout");
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = window.setTimeout;
  const originalClearTimeout = window.clearTimeout;
  const timeoutHandlers = new Map<number, () => void>();
  let nextTimeoutId = 991_100;
  let requestCount = 0;
  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    if (timeout === 10_000 && typeof handler === "function") {
      const timeoutId = nextTimeoutId++;
      timeoutHandlers.set(timeoutId, () => handler(...args));
      return timeoutId;
    }
    return originalSetTimeout(handler, timeout, ...args);
  }) as typeof window.setTimeout;
  window.clearTimeout = ((timer?: number) => {
    if (timer !== undefined && timeoutHandlers.delete(timer)) return;
    originalClearTimeout(timer);
  }) as typeof window.clearTimeout;
  globalThis.fetch = (async (input, init) => {
    assert.equal(String(input), `/api/runs/${runId}/attempts`);
    requestCount += 1;
    if (requestCount === 1) {
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          reject(init.signal?.reason ?? new DOMException("Aborted", "AbortError"));
        }, { once: true });
      });
    }
    return jsonResponse({
      run_id: runId,
      candidates: [attemptCandidatePayload(runId, 2)],
    });
  }) as typeof fetch;

  try {
    const older = useApp.getState().loadRunAttempts(runId);
    await waitFor(() => requestCount === 1, "older attempt hydration did not start");
    const staleTimeout = [...timeoutHandlers.values()][0];
    assert.ok(staleTimeout, "older attempt hydration timeout was not installed");

    await useApp.getState().loadRunAttempts(runId);
    assert.deepEqual(
      useApp.getState().run_attempts[runId]?.candidates.map((candidate) => candidate.attempt),
      [2],
    );

    staleTimeout();
    await older;
    const state = useApp.getState().run_attempts[runId];
    assert.deepEqual(state?.candidates.map((candidate) => candidate.attempt), [2]);
    assert.equal(state?.loading, false);
    assert.equal(state?.error, undefined);
  } finally {
    globalThis.fetch = originalFetch;
    window.setTimeout = originalSetTimeout;
    window.clearTimeout = originalClearTimeout;
  }
});

test("attempt hydration recovers after a timeout without starting a new run", async () => {
  const runId = "run_attempt_hydration_retry";
  resetStore({}, "conv_attempt_hydration_retry");
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = window.setTimeout;
  const originalClearTimeout = window.clearTimeout;
  const timeoutId = 991_002;
  let requestCount = 0;
  let attemptTimeoutCount = 0;
  const requestedUrls: string[] = [];
  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    if (timeout === 10_000 && typeof handler === "function" && attemptTimeoutCount++ === 0) {
      queueMicrotask(() => handler(...args));
      return timeoutId;
    }
    return originalSetTimeout(handler, timeout, ...args);
  }) as typeof window.setTimeout;
  window.clearTimeout = ((timer?: number) => {
    if (timer !== timeoutId) originalClearTimeout(timer);
  }) as typeof window.clearTimeout;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    requestedUrls.push(url);
    assert.equal(url, `/api/runs/${runId}/attempts`);
    requestCount += 1;
    if (requestCount === 1) {
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          reject(init.signal?.reason ?? new DOMException("Aborted", "AbortError"));
        }, { once: true });
      });
    }
    return jsonResponse({
      run_id: runId,
      candidates: [attemptCandidatePayload(runId, 1)],
    });
  }) as typeof fetch;

  try {
    await useApp.getState().loadRunAttempts(runId);
    assert.match(useApp.getState().run_attempts[runId]?.error ?? "", /timed out/i);
    await useApp.getState().loadRunAttempts(runId);
    const state = useApp.getState().run_attempts[runId];
    assert.equal(state?.loading, false);
    assert.equal(state?.error, undefined);
    assert.deepEqual(state?.candidates.map((candidate) => candidate.attempt), [1]);
    assert.deepEqual(requestedUrls.filter((url) => url.includes(runId)), [
      `/api/runs/${runId}/attempts`,
      `/api/runs/${runId}/attempts`,
    ]);
  } finally {
    globalThis.fetch = originalFetch;
    window.setTimeout = originalSetTimeout;
    window.clearTimeout = originalClearTimeout;
  }
});
