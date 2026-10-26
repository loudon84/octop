import type {
  CapturedComposerPreparation,
  ComposerPreparationHandler,
  ComposerSelectionCapture,
  PluginJson,
  PluginJsonObject,
} from "./types";

interface ComposerRegistration {
  readonly pluginId: string;
  readonly generation: string;
  readonly owner: object;
  readonly handler: ComposerPreparationHandler;
  readonly isCurrent: () => boolean;
}

const registrations = new Map<string, ComposerRegistration>();

function cloneJson(value: unknown, seen = new Set<object>()): PluginJson {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return value;
  }
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value !== "object" || value === null) {
    throw new TypeError("Composer data must be JSON-safe");
  }
  if (seen.has(value)) throw new TypeError("Composer data must be JSON-safe");
  seen.add(value);
  let copy: PluginJson;
  if (Array.isArray(value)) {
    copy = value.map((item) => cloneJson(item, seen));
  } else {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw new TypeError("Composer data must be JSON-safe");
    }
    const record: Record<string, PluginJson> = {};
    for (const [key, item] of Object.entries(value)) {
      record[key] = cloneJson(item, seen);
    }
    copy = record;
  }
  seen.delete(value);
  return deepFreeze(copy);
}

function deepFreeze<T extends PluginJson>(value: T): T {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const item of Array.isArray(value) ? value : Object.values(value)) {
      deepFreeze(item);
    }
  }
  return value;
}

function cloneSelection(
  selection: ComposerSelectionCapture,
): ComposerSelectionCapture {
  if (selection?.state === "uninitialized") {
    return Object.freeze({ state: "uninitialized" });
  }
  if (
    selection?.state !== "selected" ||
    !selection.value ||
    Array.isArray(selection.value)
  ) {
    throw new TypeError("Composer selection must be a JSON-safe selection");
  }
  const value = cloneJson(selection.value);
  if (!value || Array.isArray(value) || typeof value !== "object") {
    throw new TypeError("Composer selection must be a JSON-safe object");
  }
  return Object.freeze({ state: "selected", value: value as PluginJsonObject });
}

export function registerComposerPreparation(
  pluginId: string,
  owner: object,
  generation: string,
  handler: ComposerPreparationHandler,
  isCurrent: () => boolean,
): () => void {
  const registration: ComposerRegistration = {
    pluginId,
    generation,
    owner,
    handler,
    isCurrent: () =>
      registrations.get(pluginId) === registration && isCurrent(),
  };
  registrations.set(pluginId, registration);

  let disposed = false;
  return () => {
    if (disposed) return;
    disposed = true;
    if (registrations.get(pluginId) === registration) {
      registrations.delete(pluginId);
    }
  };
}

export function captureComposerPreparations(): readonly CapturedComposerPreparation[] {
  const captured: CapturedComposerPreparation[] = [];
  for (const registration of registrations.values()) {
    if (!registration.isCurrent()) continue;
    // Re-capture each item synchronously at the exact submit/enqueue boundary.
    captured.push({
      pluginId: registration.pluginId,
      generation: registration.generation,
      selection: cloneSelection(registration.handler.captureSelection()),
      isCurrent: registration.isCurrent,
      prepare: registration.handler.prepare,
      restoreSelection: registration.handler.restoreSelection,
    });
  }
  return Object.freeze(captured);
}

export async function prepareComposerContext(
  captured: readonly CapturedComposerPreparation[],
  input: {
    readonly target: { readonly agentId: string; readonly threadId: string };
    readonly text: string;
    readonly attachments: readonly unknown[];
    readonly signal: AbortSignal;
  },
): Promise<Readonly<Record<string, PluginJsonObject>>> {
  const pluginContext: Record<string, PluginJsonObject> = {};
  for (const item of captured) {
    if (!item.isCurrent() || input.signal.aborted) {
      throw new Error("Composer preparation is no longer active");
    }
    const result = await item.prepare({
      selection: item.selection,
      target: Object.freeze({ ...input.target }),
      text: input.text,
      attachments: input.attachments,
      generation: item.generation,
      signal: input.signal,
    });
    if (!item.isCurrent() || input.signal.aborted) {
      throw new Error("Composer preparation is no longer active");
    }
    const json = cloneJson(result);
    if (!json || Array.isArray(json) || typeof json !== "object") {
      throw new TypeError("Composer preparation result must be a JSON object");
    }
    pluginContext[item.pluginId] = json as PluginJsonObject;
  }
  return Object.freeze(pluginContext);
}

export async function restoreComposerSelections(
  selections: readonly {
    readonly pluginId: string;
    readonly generation: string;
    readonly selection: ComposerSelectionCapture;
  }[],
): Promise<void> {
  for (const selection of selections) {
    const registration = registrations.get(selection.pluginId);
    if (!registration || !registration.isCurrent()) {
      throw new Error("Composer selection owner is no longer active");
    }
    await registration.handler.restoreSelection(selection.selection);
    if (!registration.isCurrent()) {
      throw new Error("Composer selection owner is no longer active");
    }
  }
}
