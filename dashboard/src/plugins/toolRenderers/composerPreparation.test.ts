import { describe, expect, it, vi } from "vitest";
import { createPluginUiSurfaceHost } from "./host";
import { listPluginSurfaces, registerPluginSurface } from "./registry";
import {
  captureComposerPreparations,
  prepareComposerContext,
  restoreComposerSelections,
} from "./composerPreparation";

it("exposes composer preparation only on a live composer surface host", () => {
  registerPluginSurface("plugin-a", "composer", () => null);
  const registration = listPluginSurfaces("composer").find(
    (item) => item.pluginId === "plugin-a",
  )!;
  const host = createPluginUiSurfaceHost(
    registration,
    () => ({
      agentId: "agent-a",
      threadId: null,
      locale: "en",
      theme: "light",
      generation: "generation-a",
      timeZone: "UTC",
      readiness: {
        status: "ready",
        requiredPlugins: {},
        unavailablePlugins: [],
      },
    }),
    () => true,
  );

  expect(typeof host.registerComposerPreparation).toBe("function");
});

describe("captured plugin preparation", () => {
  function makeScopedHost(
    pluginId: string,
    surface: "composer" | "settings" = "composer",
    generation = "generation-a",
  ) {
    const dispose = registerPluginSurface(pluginId, surface, () => null);
    const registration = listPluginSurfaces(surface).find(
      (item) => item.pluginId === pluginId,
    )!;
    let mounted = true;
    return {
      host: createPluginUiSurfaceHost(
        registration,
        () => ({
          agentId: "agent-a",
          threadId: null,
          locale: "en",
          theme: "light",
          generation,
          timeZone: "UTC",
          readiness: {
            status: "ready",
            requiredPlugins: {},
            unavailablePlugins: [],
          },
        }),
        () => mounted,
      ),
      dispose,
      unmount: () => {
        mounted = false;
      },
    };
  }

  it("distinguishes selected empty values and deep-freezes a per-send selection", async () => {
    const mutableSelection = {
      state: "selected" as const,
      value: { ids: [] as string[], nested: { enabled: true } },
    };
    const prepare = vi.fn(async ({ selection }: { selection: unknown }) => ({
      operation: { selection },
    }));
    const host = makeScopedHost("capture-plugin").host;
    host.registerComposerPreparation({
      captureSelection: () => mutableSelection,
      prepare,
      restoreSelection: async () => undefined,
    });
    const captured = captureComposerPreparations();
    mutableSelection.value.ids.push("later");
    mutableSelection.value.nested.enabled = false;

    expect(captured[0]?.selection).toEqual({
      state: "selected",
      value: { ids: [], nested: { enabled: true } },
    });
    expect(Object.isFrozen(captured[0]?.selection.value)).toBe(true);

    const context = await prepareComposerContext(captured, {
      target: { agentId: "agent-a", threadId: "thread-real" },
      text: "native text",
      attachments: [],
      signal: new AbortController().signal,
    });
    expect(context["capture-plugin"]).toEqual({
      operation: { selection: captured[0]?.selection },
    });
    expect(prepare).toHaveBeenCalledWith(
      expect.objectContaining({
        target: { agentId: "agent-a", threadId: "thread-real" },
        text: "native text",
        generation: "generation-a",
      }),
    );

    const uninitializedHost = makeScopedHost("uninitialized-plugin").host;
    uninitializedHost.registerComposerPreparation({
      captureSelection: () => ({ state: "uninitialized" }),
      prepare: async () => ({}),
      restoreSelection: async () => undefined,
    });
    expect(
      captureComposerPreparations().find(
        (item) => item.pluginId === "uninitialized-plugin",
      )?.selection,
    ).toEqual({ state: "uninitialized" });
  });

  it("keeps the replacement when an old registration disposer runs", () => {
    const host = makeScopedHost("replacement-plugin").host;
    const disposeFirst = host.registerComposerPreparation({
      captureSelection: () => ({ state: "selected", value: { version: 1 } }),
      prepare: async () => ({ version: 1 }),
      restoreSelection: async () => undefined,
    });
    host.registerComposerPreparation({
      captureSelection: () => ({ state: "selected", value: { version: 2 } }),
      prepare: async () => ({ version: 2 }),
      restoreSelection: async () => undefined,
    });

    disposeFirst();
    expect(
      captureComposerPreparations().find(
        (item) => item.pluginId === "replacement-plugin",
      )?.selection,
    ).toEqual({
      state: "selected",
      value: { version: 2 },
    });
  });

  it("rejects settings, unmounted, stale and non-JSON registrations", async () => {
    const handler = {
      captureSelection: () => ({ state: "uninitialized" as const }),
      prepare: async () => ({}),
      restoreSelection: async () => undefined,
    };
    const settings = makeScopedHost("settings-host", "settings");
    const settingsHost = settings.host;
    expect(() => settingsHost.registerComposerPreparation(handler)).toThrow(
      /composer surface/i,
    );

    const scoped = makeScopedHost("unmounted-host");
    scoped.host.registerComposerPreparation(handler);
    const stale = captureComposerPreparations().filter(
      (item) => item.pluginId === "unmounted-host",
    );
    scoped.unmount();
    expect(() => scoped.host.registerComposerPreparation(handler)).toThrow(
      /inactive/i,
    );
    await expect(
      prepareComposerContext(stale, {
        target: { agentId: "agent-a", threadId: "thread-real" },
        text: "text",
        attachments: [],
        signal: new AbortController().signal,
      }),
    ).rejects.toThrow(/active/i);
    scoped.dispose();
    settings.dispose();

    const bad = makeScopedHost("bad-json-host");
    const badHost = bad.host;
    const disposeBadHandler = badHost.registerComposerPreparation({
      ...handler,
      captureSelection: () => ({
        state: "selected",
        value: { number: Number.NaN },
      }),
    });
    expect(() => captureComposerPreparations()).toThrow(/JSON-safe/i);
    disposeBadHandler();
    bad.dispose();
  });

  it("restores a same-target selection through the currently mounted generation", async () => {
    const original = makeScopedHost("reclaim-generation-a");
    const disposeOriginalHandler = original.host.registerComposerPreparation({
      captureSelection: () => ({
        state: "selected",
        value: { choice: "saved" },
      }),
      prepare: async () => ({}),
      restoreSelection: async () => undefined,
    });
    const queuedSelection = captureComposerPreparations().find(
      (entry) => entry.pluginId === "reclaim-generation-a",
    )!;
    original.unmount();
    const restored = vi.fn(async () => undefined);
    const current = makeScopedHost(
      "reclaim-generation-a",
      "composer",
      "generation-b",
    );
    const disposeCurrentHandler = current.host.registerComposerPreparation({
      captureSelection: () => ({ state: "uninitialized" }),
      prepare: async () => ({}),
      restoreSelection: restored,
    });

    await restoreComposerSelections([
      {
        pluginId: queuedSelection.pluginId,
        generation: queuedSelection.generation,
        selection: queuedSelection.selection,
      },
    ]);
    expect(restored).toHaveBeenCalledExactlyOnceWith(queuedSelection.selection);
    disposeOriginalHandler();
    disposeCurrentHandler();
    original.dispose();
    current.dispose();
  });
});
