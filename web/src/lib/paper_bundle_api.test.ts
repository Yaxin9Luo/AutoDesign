import assert from "node:assert/strict";
import test from "node:test";

import type { PaperBundleCreateRequest } from "./api.ts";

const storage = new Map<string, string>();
Object.assign(globalThis, {
  document: { cookie: "" },
  window: {
    localStorage: {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => { storage.set(key, value); },
      removeItem: (key: string) => { storage.delete(key); },
    },
  },
});

const {
  cancelPaperBundleRequest,
  createPaperBundle,
  getPaperBundle,
  listPaperBundles,
  preparePaperBundleInput,
  startReservedRun,
  uploadReservedRunInput,
} = await import("./api.ts");

const DIGEST = "a".repeat(64);
const ARTIFACT_TYPES = ["poster", "deck", "landing", "video"] as const;

const jsonResponse = (body: unknown, status = 200) => new Response(
  JSON.stringify(body),
  { status, headers: { "Content-Type": "application/json" } },
);

function childDescriptor(artifactType: typeof ARTIFACT_TYPES[number], withToken = true) {
  return {
    run_id: `run_${artifactType}`,
    artifact_type: artifactType,
    conversation_id: `conv:${artifactType}`,
    input_slots: [{
      name: "attachment-0.pdf",
      expected_sha256: DIGEST,
      expected_size: 5,
    }],
    ...(withToken ? { upload_token: `token_${artifactType}` } : {}),
    request_digest: DIGEST,
    expires_at: 10,
    state: "reserved",
    terminal: false,
    process_free: true,
  };
}

function bundleRecord(withTokens = true) {
  return {
    schema_version: 1,
    job_id: "job_bundle",
    owner_id: "local",
    conversation_id: "conv_parent",
    source_name: "paper.pdf",
    prompt_version: "1",
    state: "reserved",
    children: Object.fromEntries(
      ARTIFACT_TYPES.map((artifactType) => [
        artifactType,
        childDescriptor(artifactType, withTokens),
      ]),
    ),
    request_digest: DIGEST,
    revision: 1,
    created_at: 1,
    updated_at: 1,
    terminal: false,
    terminal_at: null,
    cancel_requested: false,
    cancel_requested_at: null,
    completed_children: [],
  };
}

function createRequest(): PaperBundleCreateRequest {
  return {
    job_id: "job_bundle",
    conversation_id: "conv_parent",
    source_name: "paper.pdf",
    prompt_version: "1",
    children: Object.fromEntries(ARTIFACT_TYPES.map((artifactType) => [
      artifactType,
      {
        brief: `Create ${artifactType}`,
        artifact_type: artifactType,
        conversation_id: `conv:${artifactType}`,
        input_slots: [{
          name: "attachment-0.pdf",
          role: "attachment",
          sha256: DIGEST,
          size: 5,
        }],
      },
    ])) as PaperBundleCreateRequest["children"],
  };
}

test("paper bundle API hashes one safe PDF slot and performs create before upload/start", async () => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const file = new File([new TextEncoder().encode("paper")], "../../Paper FINAL.PDF", {
    type: "application/pdf",
  });
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    calls.push({ url, init });
    if (url === "/api/paper-bundles") {
      return jsonResponse({ ...bundleRecord(), reused: false });
    }
    if (url === "/api/runs/run_poster/inputs/attachment-0.pdf") {
      return jsonResponse({
        run_id: "run_poster",
        slot: "attachment-0.pdf",
        sha256: DIGEST,
        size: 5,
        run_state: "reserved",
        idempotent: false,
      });
    }
    if (url === "/api/runs/run_poster/start") {
      return jsonResponse({
        run_id: "run_poster",
        placeholder_message: {
          id: "msg_run_poster",
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
    const prepared = await preparePaperBundleInput(file);
    assert.equal(prepared.slot.name, "attachment-0.pdf");
    assert.equal(prepared.slot.role, "attachment");
    assert.equal(prepared.slot.size, 5);
    assert.match(prepared.slot.sha256, /^[0-9a-f]{64}$/);

    const created = await createPaperBundle(createRequest(), "idempotency-job-bundle");
    assert.equal(created.job_id, "job_bundle");
    assert.equal(created.children.poster.upload_token, "token_poster");

    await uploadReservedRunInput(
      created.children.poster,
      prepared.slot.name,
      file,
    );
    const ack = await startReservedRun(created.children.poster);
    assert.equal(ack.run_id, "run_poster");

    assert.deepEqual(calls.map(({ url }) => url), [
      "/api/paper-bundles",
      "/api/runs/run_poster/inputs/attachment-0.pdf",
      "/api/runs/run_poster/start",
    ]);
    assert.equal(
      new Headers(calls[0].init?.headers).get("Idempotency-Key"),
      "idempotency-job-bundle",
    );
    assert.equal(
      new Headers(calls[1].init?.headers).get("X-Autodesign-Upload-Token"),
      "token_poster",
    );
    assert.equal(calls[1].init?.body, file);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("create rejects descriptor booleans encoded as strings", async () => {
  const originalFetch = globalThis.fetch;
  const malformed = bundleRecord();
  malformed.children.poster.terminal = "false" as unknown as boolean;
  globalThis.fetch = (async () => jsonResponse({ ...malformed, reused: false })) as typeof fetch;

  try {
    await assert.rejects(
      () => createPaperBundle(createRequest(), "idempotency-job-bundle"),
      /Invalid paper bundle response/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("create rejects a reservation that does not echo the requested input contract", async () => {
  const originalFetch = globalThis.fetch;
  const mismatched = bundleRecord();
  mismatched.children.poster.input_slots[0].expected_size = 6;
  globalThis.fetch = (async () => jsonResponse({ ...mismatched, reused: false })) as typeof fetch;

  try {
    await assert.rejects(
      () => createPaperBundle(createRequest(), "idempotency-job-bundle"),
      /Invalid paper bundle response/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("list requires redacted, internally consistent parent records", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => jsonResponse([bundleRecord(false)])) as typeof fetch;

  try {
    const jobs = await listPaperBundles();
    assert.equal(jobs.length, 1);
    assert.equal(jobs[0].children.deck.upload_token, undefined);
    assert.equal(jobs[0].terminal, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("create/list/get accept a completed child while its process is still exiting", async (t) => {
  for (const surface of ["create", "list", "get"] as const) {
    await t.test(surface, async () => {
      const originalFetch = globalThis.fetch;
      const record = bundleRecord(surface === "create");
      record.state = "running";
      record.children.poster.state = "completed";
      record.children.poster.terminal = true;
      record.children.poster.process_free = false;
      globalThis.fetch = (async () => jsonResponse(
        surface === "create"
          ? { ...record, reused: false }
          : surface === "list" ? [record] : record,
      )) as typeof fetch;

      try {
        const result = surface === "create"
          ? await createPaperBundle(createRequest(), "idempotency-job-bundle")
          : surface === "list" ? (await listPaperBundles())[0] : await getPaperBundle("job_bundle");
        assert.equal(result.children.poster.state, "completed");
        assert.equal(result.children.poster.process_free, false);
        assert.deepEqual(result.completed_children, []);
      } finally {
        globalThis.fetch = originalFetch;
      }
    });
  }
});

test("create/list/get reject duplicate child run ids", async (t) => {
  for (const surface of ["create", "list", "get"] as const) {
    await t.test(surface, async () => {
      const originalFetch = globalThis.fetch;
      const record = bundleRecord(surface === "create");
      record.children.deck.run_id = record.children.poster.run_id;
      globalThis.fetch = (async () => jsonResponse(
        surface === "create"
          ? { ...record, reused: false }
          : surface === "list" ? [record] : record,
      )) as typeof fetch;

      try {
        await assert.rejects(
          () => surface === "create"
            ? createPaperBundle(createRequest(), "idempotency-job-bundle")
            : surface === "list" ? listPaperBundles() : getPaperBundle("job_bundle"),
          /Invalid paper bundle response/,
        );
      } finally {
        globalThis.fetch = originalFetch;
      }
    });
  }
});

test("list rejects contradictory parent terminal timestamps", async () => {
  const originalFetch = globalThis.fetch;
  const malformed = bundleRecord(false);
  (malformed as { terminal_at: number | null }).terminal_at = 2;
  globalThis.fetch = (async () => jsonResponse([malformed])) as typeof fetch;

  try {
    await assert.rejects(() => listPaperBundles(), /Invalid paper bundle response/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("create/list/get mirror the backend parent-state semantic invariants", async (t) => {
  const setTerminal = (
    record: ReturnType<typeof bundleRecord>,
    state: string,
    childStates: string[],
    cancelRequested = false,
  ) => {
    record.state = state;
    record.terminal = true;
    (record as { terminal_at: number | null }).terminal_at = 2;
    record.cancel_requested = cancelRequested;
    (record as { cancel_requested_at: number | null }).cancel_requested_at = cancelRequested
      ? 2
      : null;
    ARTIFACT_TYPES.forEach((artifactType, index) => {
      record.children[artifactType].state = childStates[index];
      record.children[artifactType].terminal = true;
      record.children[artifactType].process_free = true;
    });
    (record as { completed_children: string[] }).completed_children = ARTIFACT_TYPES.filter(
      (artifactType) => record.children[artifactType].state === "completed",
    );
  };
  const cases: Array<{
    name: string;
    mutate: (record: ReturnType<typeof bundleRecord>) => void;
  }> = [
    {
      name: "terminal parent with a non-quiescent child",
      mutate: (record) => {
        setTerminal(record, "cancelled", ["cancelled", "cancelled", "cancelled", "cancelled"], true);
        record.children.poster.terminal = false;
        record.children.poster.process_free = false;
      },
    },
    {
      name: "completed parent with fewer than four completed children",
      mutate: (record) => setTerminal(
        record,
        "completed",
        ["completed", "completed", "completed", "failed"],
      ),
    },
    {
      name: "partial parent with no completed child",
      mutate: (record) => setTerminal(
        record,
        "partial",
        ["failed", "failed", "cancelled", "cancelled"],
      ),
    },
    {
      name: "partial parent with all children completed",
      mutate: (record) => setTerminal(
        record,
        "partial",
        ["completed", "completed", "completed", "completed"],
      ),
    },
    {
      name: "failed parent containing a completed child",
      mutate: (record) => setTerminal(
        record,
        "failed",
        ["completed", "failed", "failed", "failed"],
      ),
    },
    {
      name: "failed parent whose children are all cancelled",
      mutate: (record) => setTerminal(
        record,
        "failed",
        ["cancelled", "cancelled", "cancelled", "cancelled"],
      ),
    },
    {
      name: "cancelling parent without a cancellation request",
      mutate: (record) => { record.state = "cancelling"; },
    },
    {
      name: "naturally cancelled parent with mixed child outcomes",
      mutate: (record) => setTerminal(
        record,
        "cancelled",
        ["cancelled", "failed", "cancelled", "cancelled"],
      ),
    },
  ];
  const surfaces = ["create", "list", "get"] as const;

  for (const semanticCase of cases) {
    for (const surface of surfaces) {
      await t.test(`${surface}: ${semanticCase.name}`, async () => {
        const originalFetch = globalThis.fetch;
        const record = bundleRecord(surface === "create");
        semanticCase.mutate(record);
        globalThis.fetch = (async () => jsonResponse(
          surface === "create"
            ? { ...record, reused: false }
            : surface === "list" ? [record] : record,
        )) as typeof fetch;
        try {
          await assert.rejects(
            () => surface === "create"
              ? createPaperBundle(createRequest(), "idempotency-job-bundle")
              : surface === "list" ? listPaperBundles() : getPaperBundle("job_bundle"),
            /Invalid paper bundle response/,
          );
        } finally {
          globalThis.fetch = originalFetch;
        }
      });
    }
  }
});

test("cancel-requested terminal cancellation may preserve mixed quiescent child outcomes", async () => {
  const record = bundleRecord(false);
  record.state = "cancelled";
  record.terminal = true;
  (record as { terminal_at: number | null }).terminal_at = 2;
  record.cancel_requested = true;
  (record as { cancel_requested_at: number | null }).cancel_requested_at = 2;
  record.children.poster.state = "completed";
  record.children.poster.terminal = true;
  record.children.poster.process_free = true;
  for (const artifactType of ["deck", "landing", "video"] as const) {
    record.children[artifactType].state = "cancelled";
    record.children[artifactType].terminal = true;
    record.children[artifactType].process_free = true;
  }
  (record as { completed_children: string[] }).completed_children = ["poster"];
  globalThis.fetch = (async () => jsonResponse([record])) as typeof fetch;

  const jobs = await listPaperBundles();
  assert.equal(jobs[0].state, "cancelled");
  assert.deepEqual(jobs[0].completed_children, ["poster"]);
});

test("confirmed parent cancellation preserves the validated response owner", async () => {
  const originalFetch = globalThis.fetch;
  const record = bundleRecord(false);
  record.state = "cancelled";
  record.terminal = true;
  (record as { terminal_at: number | null }).terminal_at = 2;
  record.cancel_requested = true;
  (record as { cancel_requested_at: number | null }).cancel_requested_at = 2;
  for (const artifactType of ARTIFACT_TYPES) {
    record.children[artifactType].state = "cancelled";
    record.children[artifactType].terminal = true;
    record.children[artifactType].process_free = true;
  }
  globalThis.fetch = (async () => jsonResponse({
    ...record,
    status: "cancelled",
    confirmed: true,
  })) as typeof fetch;

  try {
    const cancelled = await cancelPaperBundleRequest("job_bundle");
    assert.equal(cancelled.owner_id, "local");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("parent cancellation keeps 202 pending and rejects false confirmation", async () => {
  const originalFetch = globalThis.fetch;
  const responses = [
    jsonResponse({
      job_id: "job_bundle",
      state: "cancelling",
      confirmed: false,
      status: "cancellation_pending",
      children: {},
    }, 202),
    jsonResponse({
      ...bundleRecord(false),
      state: "cancelled",
      terminal: true,
      confirmed: true,
      status: "cancelled",
    }),
  ];
  globalThis.fetch = (async () => responses.shift()!) as typeof fetch;

  try {
    const pending = await cancelPaperBundleRequest("job_bundle");
    assert.equal(pending.http_status, 202);
    assert.equal(pending.confirmed, false);
    await assert.rejects(
      () => cancelPaperBundleRequest("job_bundle"),
      /Invalid paper bundle cancellation response/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("confirmed parent cancellation rejects an empty child set", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => jsonResponse({
    job_id: "job_bundle",
    state: "cancelled",
    confirmed: true,
    status: "cancelled",
    children: {},
  })) as typeof fetch;

  try {
    await assert.rejects(
      () => cancelPaperBundleRequest("job_bundle"),
      /Invalid paper bundle cancellation response/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("confirmed pre-creation cancellation accepts a quiesced factory tombstone", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => jsonResponse({
    job_id: "job_bundle",
    owner_id: "local",
    state: "cancelled",
    confirmed: true,
    status: "cancelled",
    pending_creation: true,
    factory_quiesced: true,
    children: {},
  })) as typeof fetch;

  try {
    const cancelled = await cancelPaperBundleRequest("job_bundle");
    assert.equal(cancelled.confirmed, true);
    assert.equal(cancelled.pending_creation, true);
    assert.equal(cancelled.factory_quiesced, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("parent cancellation rejects a response for a different job", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => jsonResponse({
    job_id: "job_other",
    state: "cancelled",
    confirmed: true,
    status: "cancelled",
    children: {},
  })) as typeof fetch;

  try {
    await assert.rejects(
      () => cancelPaperBundleRequest("job_bundle"),
      /Invalid paper bundle cancellation response/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
