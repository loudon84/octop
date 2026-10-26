import { afterEach, describe, expect, it, vi } from "vitest";
import {
  isPluginUiLoaded,
  loadInstalledPluginUis,
  reloadPluginToolUis,
  resetPluginUiLoader,
} from "./loader";
import { createPluginUiHost } from "./host";
import {
  listPluginSurfaces,
  listRegisteredRenderers,
  resolveToolRenderer,
  subscribeToolRenderers,
} from "./registry";
import type { InstalledPlugin } from "../../api/modules/plugins";

vi.mock("../../api/config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
}));
vi.mock("../../api/request", () => ({ getAuthToken: () => "test-token" }));
vi.mock("../../pages/Chat/hooks/chatStore", () => ({
  patchToolResultData: vi.fn(),
}));

function plugin(overrides: Partial<InstalledPlugin> = {}): InstalledPlugin {
  return {
    id: "demo",
    version: "1.0",
    loaded: true,
    enabled: true,
    ui: { entry: "ui/index.js", manifest: "ui/manifest.json" },
    ...overrides,
  };
}

function moduleSource(extra: string, setup = "") {
  return `
    function Renderer() { return null; }
    function Surface() { return null; }
    export function setup(host) {
      host.registerRenderer({ id: "main", component: Renderer });
      host.registerSurface("settings", Surface);
      ${setup}
      ${extra}
    }
  `;
}

function defaultModuleSource() {
  return `
    export default { setup() { globalThis.__defaultSetupCount = (globalThis.__defaultSetupCount || 0) + 1; } };
  `;
}

function mockImportUrls() {
  const originalCreate = URL.createObjectURL;
  const originalRevoke = URL.revokeObjectURL;
  const created: string[] = [];
  const revoked: string[] = [];
  let nextUrl = 0;
  class CapturedBlob {
    source: string;

    constructor(parts: BlobPart[]) {
      this.source = parts.map((part) => String(part)).join("");
    }
  }
  vi.stubGlobal("Blob", CapturedBlob);
  URL.createObjectURL = ((blob: Blob) => {
    const source = (blob as unknown as CapturedBlob).source;
    const url = `data:text/javascript;base64,${btoa(source)}#${nextUrl++}`;
    created.push(url);
    return url;
  }) as typeof URL.createObjectURL;
  URL.revokeObjectURL = ((url: string) =>
    revoked.push(url)) as typeof URL.revokeObjectURL;
  return {
    created,
    revoked,
    restore: () => {
      URL.createObjectURL = originalCreate;
      URL.revokeObjectURL = originalRevoke;
    },
  };
}

describe("plugin UI loader lifecycle", () => {
  afterEach(() => {
    resetPluginUiLoader();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("supports old void/default setup and coalesces successful same-version loads", async () => {
    const urls = mockImportUrls();
    const source = moduleSource(
      "",
      "globalThis.__setupCount = (globalThis.__setupCount || 0) + 1;",
    );
    const fetchMock = vi.fn(
      async (url: RequestInfo | URL) =>
        new Response(
          String(url).includes("default-demo") ? defaultModuleSource() : source,
          { status: 200 },
        ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const current = plugin();
    const defaultSetup = plugin({ id: "default-demo" });

    await Promise.all([
      loadInstalledPluginUis([current, defaultSetup]),
      loadInstalledPluginUis([current, defaultSetup]),
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(isPluginUiLoaded("demo")).toBe(true);
    expect(listPluginSurfaces("settings").map((item) => item.pluginId)).toEqual(
      ["demo"],
    );
    expect(isPluginUiLoaded("default-demo")).toBe(true);

    await loadInstalledPluginUis([current, defaultSetup]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(
      (globalThis as typeof globalThis & { __setupCount: number }).__setupCount,
    ).toBe(1);
    expect(
      (globalThis as typeof globalThis & { __defaultSetupCount: number })
        .__defaultSetupCount,
    ).toBe(1);
    expect(fetchMock.mock.calls[0]?.[1]?.headers).toEqual({
      Authorization: "Bearer test-token",
    });
    await reloadPluginToolUis([current, defaultSetup]);
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(
      (globalThis as typeof globalThis & { __setupCount: number }).__setupCount,
    ).toBe(2);
    resetPluginUiLoader();
    urls.restore();
  });

  it("reconciles disable, removal and fingerprint changes and keeps explicit reload support", async () => {
    const urls = mockImportUrls();
    const fetchMock = vi.fn(
      async () => new Response(moduleSource(""), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await loadInstalledPluginUis([plugin()]);
    await loadInstalledPluginUis([plugin({ enabled: false })]);
    expect(isPluginUiLoaded("demo")).toBe(false);
    expect(listPluginSurfaces("settings")).toHaveLength(0);

    await loadInstalledPluginUis([plugin({ id: "demo2" })]);
    await loadInstalledPluginUis([plugin({ id: "demo2", loaded: false })]);
    expect(isPluginUiLoaded("demo2")).toBe(false);
    await loadInstalledPluginUis([plugin({ id: "demo2", error: "disabled" })]);
    expect(isPluginUiLoaded("demo2")).toBe(false);
    await loadInstalledPluginUis([
      plugin({
        id: "demo2",
        ui: { entry: "ui/next.js", manifest: "ui/manifest.json" },
      }),
    ]);
    await loadInstalledPluginUis([plugin({ id: "demo2", version: "2.0" })]);
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(isPluginUiLoaded("demo2")).toBe(true);
    await loadInstalledPluginUis([]);
    expect(isPluginUiLoaded("demo2")).toBe(false);
    expect(listPluginSurfaces("settings")).toHaveLength(0);
    urls.restore();
  });

  it("revokes capabilities before cleanup, removes partial registrations after failure, and continues cleanup", async () => {
    const urls = mockImportUrls();
    const firstSource = moduleSource(
      "",
      `
      host.registerSurface("composer", Surface);
      globalThis.__capturedHost = host;
      globalThis.__badCleanupCount = 0;
      host.registerRenderer({ id: "override", pluginId: "escaped-owner", component: Renderer });
      return () => {
        globalThis.__badCleanupCount += 1;
        throw new Error("cleanup failed");
      };
    `,
    );
    const secondSource = moduleSource("");
    const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
      const path = String(url);
      return new Response(path.includes("bad") ? firstSource : secondSource, {
        status: 200,
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    await loadInstalledPluginUis([
      plugin({ id: "bad" }),
      plugin({ id: "good" }),
    ]);
    expect(isPluginUiLoaded("bad")).toBe(true);
    expect(isPluginUiLoaded("good")).toBe(true);
    resetPluginUiLoader();
    expect(listPluginSurfaces("settings")).toHaveLength(0);
    expect(listPluginSurfaces("composer")).toHaveLength(0);
    expect(
      listRegisteredRenderers().some(
        (renderer) => renderer.pluginId === "escaped-owner",
      ),
    ).toBe(false);
    expect(urls.revoked).toHaveLength(2);
    expect(
      (globalThis as typeof globalThis & { __badCleanupCount: number })
        .__badCleanupCount,
    ).toBe(1);
    resetPluginUiLoader();
    expect(
      (globalThis as typeof globalThis & { __badCleanupCount: number })
        .__badCleanupCount,
    ).toBe(1);
    const captured = (
      globalThis as typeof globalThis & {
        __capturedHost?: {
          registerSurface: (...args: unknown[]) => unknown;
          request: (path: string) => Promise<unknown>;
        };
      }
    ).__capturedHost;
    expect(() => captured?.registerSurface("settings", () => null)).toThrow(
      /inactive/i,
    );
    await expect((async () => captured?.request("/stale"))()).rejects.toThrow(
      /inactive/i,
    );
    urls.restore();
  });

  it("bulk-removes partial registrations after thrown or asynchronous setup", async () => {
    const urls = mockImportUrls();
    const thrown = moduleSource(
      'host.registerRenderer({ id: "escaped", pluginId: "escaped-owner", component: Renderer }); throw new Error("setup failed");',
    );
    const asynchronous = `
      function Surface() { return null; }
      export async function setup(host) {
        host.registerSurface("settings", Surface);
        await Promise.resolve();
        try {
          host.registerSurface("composer", Surface);
        } catch {
          globalThis.__asyncRegistrationRejected = true;
        }
      }
    `;
    (
      globalThis as typeof globalThis & {
        __asyncRegistrationRejected?: boolean;
      }
    ).__asyncRegistrationRejected = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (url: RequestInfo | URL) =>
          new Response(String(url).includes("async") ? asynchronous : thrown, {
            status: 200,
          }),
      ),
    );
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    await loadInstalledPluginUis([
      plugin({
        id: "throw",
        ui: { entry: "ui/throw.js", manifest: "ui/manifest.json" },
      }),
      plugin({
        id: "async",
        ui: { entry: "ui/async.js", manifest: "ui/manifest.json" },
      }),
    ]);
    expect(isPluginUiLoaded("throw")).toBe(false);
    expect(isPluginUiLoaded("async")).toBe(false);
    expect(listPluginSurfaces("settings")).toHaveLength(0);
    expect(
      resolveToolRenderer({
        pluginId: "throw",
        parsed: { isJson: true, raw: {}, octopUi: { renderer: "main" } },
      }),
    ).toBeNull();
    expect(
      listRegisteredRenderers().some(
        (renderer) => renderer.pluginId === "escaped-owner",
      ),
    ).toBe(false);
    expect(
      (
        globalThis as typeof globalThis & {
          __asyncRegistrationRejected?: boolean;
        }
      ).__asyncRegistrationRejected,
    ).toBe(true);
    expect(urls.revoked).toHaveLength(2);
    urls.restore();
  });

  it("does not setup after a stale fetch resolves and releases no-longer-current module URLs", async () => {
    const urls = mockImportUrls();
    let finishFetch!: (response: Response) => void;
    const fetchMock = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          finishFetch = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const pending = loadInstalledPluginUis([plugin()]);
    await Promise.resolve();
    await loadInstalledPluginUis([plugin({ enabled: false })]);
    finishFetch(
      new Response(moduleSource("globalThis.__staleSetup = true;"), {
        status: 200,
      }),
    );
    await pending;
    expect(
      (globalThis as typeof globalThis & { __staleSetup?: boolean })
        .__staleSetup,
    ).toBeUndefined();
    expect(urls.created).toHaveLength(0);
    expect(isPluginUiLoaded("demo")).toBe(false);
    urls.restore();
  });

  it.each(["fetch", "import"] as const)(
    "does not let a stale %s rejection unregister the fresh generation",
    async (failureStage) => {
      const urls = mockImportUrls();
      let rejectOldFetch!: (error: Error) => void;
      let fetchCount = 0;
      const oldImportSource = `
        await new Promise((resolve, reject) => {
          globalThis.__rejectOldImport = reject;
          globalThis.__finishOldImport = resolve;
        });
        export function setup() { throw new Error("old import unexpectedly resolved"); }
      `;
      vi.stubGlobal(
        "fetch",
        vi.fn(() => {
          fetchCount += 1;
          if (fetchCount === 1) {
            if (failureStage === "fetch") {
              return new Promise<Response>((_resolve, reject) => {
                rejectOldFetch = reject;
              });
            }
            return Promise.resolve(
              new Response(oldImportSource, { status: 200 }),
            );
          }
          return Promise.resolve(
            new Response(moduleSource(""), { status: 200 }),
          );
        }),
      );
      vi.spyOn(console, "warn").mockImplementation(() => undefined);

      const oldLoad = loadInstalledPluginUis([plugin()]);
      if (failureStage === "import") await waitForGlobal("__rejectOldImport");
      resetPluginUiLoader();
      await loadInstalledPluginUis([plugin()]);
      expect(isPluginUiLoaded("demo")).toBe(true);
      expect(listPluginSurfaces("settings")).toHaveLength(1);

      if (failureStage === "fetch") {
        rejectOldFetch(new Error("old fetch rejected"));
      } else {
        const state = globalThis as typeof globalThis & {
          __rejectOldImport: (error: Error) => void;
        };
        state.__rejectOldImport(new Error("old import rejected"));
      }
      await oldLoad;
      expect(isPluginUiLoaded("demo")).toBe(true);
      expect(listPluginSurfaces("settings")).toHaveLength(1);
      resetPluginUiLoader();
      urls.restore();
    },
  );

  it("binds loaded renderer ownership to the real plugin despite owner overrides", async () => {
    const urls = mockImportUrls();
    const source = `
      function Renderer() { return null; }
      export function setup(host) {
        host.registerRenderer({ id: "override", pluginId: "other", component: Renderer });
      }
    `;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(source, { status: 200 })),
    );
    await loadInstalledPluginUis([plugin()]);
    expect(
      listRegisteredRenderers().find((renderer) => renderer.id === "override")
        ?.pluginId,
    ).toBe("demo");
    expect(
      resolveToolRenderer({
        pluginId: "demo",
        parsed: { isJson: true, raw: {}, octopUi: { renderer: "override" } },
      })?.pluginId,
    ).toBe("demo");
    await loadInstalledPluginUis([plugin({ enabled: false })]);
    expect(
      listRegisteredRenderers().some((renderer) => renderer.id === "override"),
    ).toBe(false);
    urls.restore();
  });

  it("cleans replaced renderer aliases without removing an alias taken by another load", async () => {
    const urls = mockImportUrls();
    const replacingSource = `
      function First() { return null; }
      function Second() { return null; }
      export function setup(host) {
        host.registerRenderer({ id: "main", tools: ["old_tool"], component: First });
        host.registerRenderer({ id: "main", tools: ["new_tool"], component: Second });
      }
    `;
    const otherSource = `
      function Other() { return null; }
      export function setup(host) {
        host.registerRenderer({ id: "other", tools: ["new_tool"], component: Other });
      }
    `;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (url: RequestInfo | URL) =>
          new Response(
            String(url).includes("/plugins/other/")
              ? otherSource
              : replacingSource,
            {
              status: 200,
            },
          ),
      ),
    );
    await loadInstalledPluginUis([plugin(), plugin({ id: "other" })]);
    expect(
      resolveToolRenderer({
        toolName: "old_tool",
        parsed: { isJson: false, raw: "plain" },
      })?.id,
    ).toBe("main");
    expect(
      resolveToolRenderer({
        toolName: "new_tool",
        parsed: { isJson: false, raw: "plain" },
      })?.pluginId,
    ).toBe("other");

    await loadInstalledPluginUis([
      plugin({ enabled: false }),
      plugin({ id: "other" }),
    ]);
    expect(
      resolveToolRenderer({
        toolName: "old_tool",
        parsed: { isJson: false, raw: "plain" },
      }),
    ).toBeNull();
    expect(
      resolveToolRenderer({
        toolName: "new_tool",
        parsed: { isJson: false, raw: "plain" },
      })?.pluginId,
    ).toBe("other");
    expect(isPluginUiLoaded("other")).toBe(true);
    resetPluginUiLoader();
    urls.restore();
  });

  it("revokes an invalidated pending import URL before its module gate settles", async () => {
    const urls = mockImportUrls();
    const source = `
      await new Promise(resolve => { globalThis.__finishNeverReadyImport = resolve; });
      export function setup() { globalThis.__neverReadySetup = true; }
    `;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(source, { status: 200 })),
    );
    const pending = loadInstalledPluginUis([plugin()]);
    await waitForGlobal("__finishNeverReadyImport");
    expect(urls.created).toHaveLength(1);
    expect(urls.revoked).toHaveLength(0);

    try {
      resetPluginUiLoader();
      expect(urls.revoked).toEqual(urls.created);
    } finally {
      (
        globalThis as typeof globalThis & {
          __finishNeverReadyImport: () => void;
        }
      ).__finishNeverReadyImport();
      await pending;
    }
    expect(
      (globalThis as typeof globalThis & { __neverReadySetup?: boolean })
        .__neverReadySetup,
    ).toBeUndefined();
    urls.restore();
  });

  it("removes only the stale setup registrations after a synchronous registry reset", async () => {
    const urls = mockImportUrls();
    const source = `
      function Renderer() { return null; }
      export function setup(host) {
        globalThis.__reentrantSetupHost = host;
        host.registerRenderer({ id: "reentrant", tools: ["reentrant_tool"], component: Renderer });
      }
    `;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(source, { status: 200 })),
    );
    let reset = false;
    const unsubscribe = subscribeToolRenderers(() => {
      if (reset) return;
      reset = true;
      resetPluginUiLoader();
    });
    try {
      await loadInstalledPluginUis([plugin()]);
      expect(reset).toBe(true);
      expect(isPluginUiLoaded("demo")).toBe(false);
      expect(
        resolveToolRenderer({
          toolName: "reentrant_tool",
          parsed: { isJson: false, raw: "plain" },
        }),
      ).toBeNull();
      const oldHost = (
        globalThis as typeof globalThis & {
          __reentrantSetupHost: { request: (path: string) => Promise<unknown> };
        }
      ).__reentrantSetupHost;
      expect(() => oldHost.request("/stale-setup")).toThrow(/inactive/i);
    } finally {
      unsubscribe();
      resetPluginUiLoader();
      urls.restore();
    }
  });

  it("revokes a URL when top-level module evaluation resolves after reset", async () => {
    const urls = mockImportUrls();
    const source = `
      await new Promise(resolve => { globalThis.__finishModuleImport = resolve; });
      export function setup() { globalThis.__staleImportedSetup = true; }
    `;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(source, { status: 200 })),
    );
    const pending = loadInstalledPluginUis([plugin()]);
    await waitForGlobal("__finishModuleImport");
    resetPluginUiLoader();
    (
      globalThis as typeof globalThis & { __finishModuleImport: () => void }
    ).__finishModuleImport();
    await pending;
    expect(
      (globalThis as typeof globalThis & { __staleImportedSetup?: boolean })
        .__staleImportedSetup,
    ).toBeUndefined();
    expect(urls.revoked).toHaveLength(1);
    urls.restore();
  });

  it("keeps legacy hosts compatible for direct existing callers", () => {
    const host = createPluginUiHost("direct-plugin");
    expect(host.getContext()).toBeNull();
    expect(typeof host.registerRenderer).toBe("function");
  });
});

async function waitForGlobal(key: string): Promise<void> {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    if (key in globalThis) return;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  throw new Error(`Module did not reach ${key}`);
}
