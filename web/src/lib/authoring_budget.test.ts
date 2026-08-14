import assert from "node:assert/strict";
import test from "node:test";

import {
  AUTHORING_BUDGET_STORAGE_KEY,
  readAuthoringBudgets,
  saveAuthoringBudgets,
  setAuthoringBudget,
} from "./authoring_budget.ts";

class MemoryStorage implements Pick<Storage, "getItem" | "setItem"> {
  private readonly values = new Map<string, string>();

  constructor(initial: Record<string, string> = {}) {
    for (const [key, value] of Object.entries(initial)) {
      this.values.set(key, value);
    }
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

class DeniedStorage implements Pick<Storage, "getItem" | "setItem"> {
  getItem(): string | null {
    throw new DOMException("denied", "SecurityError");
  }

  setItem(): void {
    throw new DOMException("quota", "QuotaExceededError");
  }
}

test("uses 12/12/4/4 defaults without saved state", () => {
  assert.deepEqual(readAuthoringBudgets(new MemoryStorage()), {
    poster: 12,
    deck: 12,
    landing: 4,
    video: 4,
  });
});

test("repairs invalid fields without discarding valid siblings", () => {
  const storage = new MemoryStorage({
    [AUTHORING_BUDGET_STORAGE_KEY]: JSON.stringify({
      v: 1,
      budgets: {
        poster: 8,
        deck: 0,
        landing: 5,
        video: 99,
      },
    }),
  });

  assert.deepEqual(readAuthoringBudgets(storage), {
    poster: 8,
    deck: 12,
    landing: 5,
    video: 4,
  });
});

test("malformed saved state falls back to all defaults", () => {
  const storage = new MemoryStorage({
    [AUTHORING_BUDGET_STORAGE_KEY]: "{bad json",
  });

  assert.deepEqual(readAuthoringBudgets(storage), {
    poster: 12,
    deck: 12,
    landing: 4,
    video: 4,
  });
});

test("updates and persists one artifact without changing siblings", () => {
  const storage = new MemoryStorage();
  const changed = setAuthoringBudget(
    readAuthoringBudgets(storage),
    "video",
    7,
  );

  saveAuthoringBudgets(changed, storage);

  assert.deepEqual(readAuthoringBudgets(storage), {
    poster: 12,
    deck: 12,
    landing: 4,
    video: 7,
  });
});

test("storage failures fall back without throwing", () => {
  const storage = new DeniedStorage();

  assert.deepEqual(readAuthoringBudgets(storage), {
    poster: 12,
    deck: 12,
    landing: 4,
    video: 4,
  });
  assert.doesNotThrow(() => saveAuthoringBudgets({
    poster: 12,
    deck: 12,
    landing: 4,
    video: 4,
  }, storage));
});
