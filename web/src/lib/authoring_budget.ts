import type { ArtifactType } from "./types";

export type AuthoringBudgets = Record<ArtifactType, number>;
export type AuthoringBudgetStorage = Pick<Storage, "getItem" | "setItem">;

export const AUTHORING_BUDGET_STORAGE_KEY = "autodesign.authoring-budgets.v1";
export const AUTHORING_BUDGET_MIN = 1;
export const AUTHORING_BUDGET_MAX = 12;
export const DEFAULT_AUTHORING_BUDGETS: AuthoringBudgets = {
  poster: 12,
  deck: 12,
  landing: 4,
  video: 4,
};

const artifactTypes: ArtifactType[] = ["poster", "deck", "landing", "video"];

function availableStorage(): AuthoringBudgetStorage | undefined {
  if (typeof window === "undefined") return undefined;
  try {
    return window.localStorage;
  } catch {
    return undefined;
  }
}

function validBudget(value: unknown): value is number {
  return Number.isInteger(value)
    && Number(value) >= AUTHORING_BUDGET_MIN
    && Number(value) <= AUTHORING_BUDGET_MAX;
}

export function readAuthoringBudgets(
  storage: AuthoringBudgetStorage | undefined = availableStorage(),
): AuthoringBudgets {
  const next = { ...DEFAULT_AUTHORING_BUDGETS };
  if (!storage) return next;
  try {
    const raw = storage.getItem(AUTHORING_BUDGET_STORAGE_KEY);
    if (!raw) return next;
    const parsed = JSON.parse(raw) as { v?: unknown; budgets?: Record<string, unknown> };
    if (parsed.v !== 1 || !parsed.budgets) return next;
    for (const artifactType of artifactTypes) {
      const value = parsed.budgets[artifactType];
      if (validBudget(value)) next[artifactType] = value;
    }
  } catch {
    return next;
  }
  return next;
}

export function saveAuthoringBudgets(
  budgets: AuthoringBudgets,
  storage: AuthoringBudgetStorage | undefined = availableStorage(),
): void {
  if (!storage) return;
  const sanitized = artifactTypes.reduce<AuthoringBudgets>(
    (result, artifactType) => ({
      ...result,
      [artifactType]: validBudget(budgets[artifactType])
        ? budgets[artifactType]
        : DEFAULT_AUTHORING_BUDGETS[artifactType],
    }),
    { ...DEFAULT_AUTHORING_BUDGETS },
  );
  try {
    storage.setItem(
      AUTHORING_BUDGET_STORAGE_KEY,
      JSON.stringify({ v: 1, budgets: sanitized }),
    );
  } catch {
    // Storage can be unavailable in privacy-restricted browser contexts.
  }
}

export function setAuthoringBudget(
  budgets: AuthoringBudgets,
  artifactType: ArtifactType,
  value: number,
): AuthoringBudgets {
  return {
    ...budgets,
    [artifactType]: validBudget(value)
      ? value
      : DEFAULT_AUTHORING_BUDGETS[artifactType],
  };
}

export function authoringBudgetFor(
  budgets: AuthoringBudgets,
  artifactType: ArtifactType,
): number {
  return validBudget(budgets[artifactType])
    ? budgets[artifactType]
    : DEFAULT_AUTHORING_BUDGETS[artifactType];
}
