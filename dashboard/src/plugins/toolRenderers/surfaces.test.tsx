import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  loadInstalledPluginUis,
  reloadPluginToolUis,
  resetPluginUiLoader,
} from "./loader";
import { PluginSurface } from "./PluginSurface";
import {
  clearToolRenderers,
  registerPluginSurface,
  resolveToolRenderer,
} from "./registry";
import { parseOctopToolOutput } from "./parseToolOutput";
import { ensureBuiltinToolRenderers } from "./ensureBuiltins";
import { invalidateAuthSession } from "../../api/authSession";
import type { PluginSurfaceContext } from "./types";

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));
vi.mock("../../pages/Chat/hooks/chatStore", () => ({
  patchToolResultData: vi.fn(),
}));
vi.mock("../../api/config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
}));
vi.mock("../../api/request", () => ({
  getAuthToken: () => null,
  request: (path: string) => requestMock(path),
}));

const settingsContext: PluginSurfaceContext = {
  agentId: "agent-b",
  threadId: null,
  locale: "en",
  theme: "light",
  generation: "generation-b",
  timeZone: "Asia/Shanghai",
  readiness: {
    status: "ready",
    requiredPlugins: { alpha: "1" },
    unavailablePlugins: [],
  },
};
const composerContext: PluginSurfaceContext = {
  ...settingsContext,
  agentId: "agent-a",
  threadId: "thread-a",
  generation: "generation-a",
};

function moduleSource(marker: string): string {
  return `
    const marker = "${marker}";
    const jsx = window.__OCTOP_JSX__;
    window.__surfaceResults = window.__surfaceResults || [];
    window.__surfaceFailures = window.__surfaceFailures || [];
    window.__surfaceHosts = window.__surfaceHosts || {};
    window.__surfaceContexts = window.__surfaceContexts || {};
    function Surface({ host, context, location }) {
      window.__lastSurfaceHost = host;
      window.__lastSurfaceContext = host.getContext();
      window.__surfaceHosts[location] = host;
      window.__surfaceContexts[location] = host.getContext();
      return jsx.jsx("button", {
        "data-testid": marker + "-" + location,
        onClick: async () => {
          try {
            const result = await host.request("/scope/" + context.agentId);
            window.__surfaceResults.push(result.path);
          } catch (error) {
            window.__surfaceFailures.push(error.message);
          }
        },
        children: context.agentId + ":" + context.threadId + ":" + context.locale + ":" + context.theme + ":" + context.timeZone
      });
    }
    export function setup(host) {
      host.registerSurface("settings", Surface);
      host.registerSurface("composer", Surface);
    }
  `;
}

function installModuleImportStub() {
  const originalCreate = URL.createObjectURL;
  const originalRevoke = URL.revokeObjectURL;
  const revoked: string[] = [];
  let nextUrl = 0;
  class CapturedBlob {
    source: string;

    constructor(parts: BlobPart[]) {
      this.source = parts.map((part) => String(part)).join("");
    }
  }
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(moduleSource("current"), { status: 200 })),
  );
  vi.stubGlobal("Blob", CapturedBlob);
  URL.createObjectURL = ((blob: Blob) => {
    const source = (blob as unknown as CapturedBlob).source;
    return `data:text/javascript;base64,${btoa(source)}#${nextUrl++}`;
  }) as typeof URL.createObjectURL;
  URL.revokeObjectURL = ((url: string) =>
    revoked.push(url)) as typeof URL.revokeObjectURL;
  return () => {
    URL.createObjectURL = originalCreate;
    URL.revokeObjectURL = originalRevoke;
    return revoked;
  };
}

describe("PluginSurface", () => {
  afterEach(() => {
    cleanup();
    resetPluginUiLoader();
    clearToolRenderers();
    requestMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("renders setup components in independent settings and composer scopes", async () => {
    const restoreUrls = installModuleImportStub();
    ensureBuiltinToolRenderers();
    await loadInstalledPluginUis([
      {
        id: "surface-plugin",
        version: "1",
        loaded: true,
        ui: { entry: "ui/index.js", manifest: "ui/manifest.json" },
      },
    ]);

    const { rerender, unmount } = render(
      <>
        <PluginSurface
          surface="settings"
          location="connectors"
          context={settingsContext}
        />
        <PluginSurface
          surface="composer"
          location="chat"
          context={composerContext}
        />
      </>,
    );

    expect(
      await screen.findByText("agent-b:null:en:light:Asia/Shanghai"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("agent-a:thread-a:en:light:Asia/Shanghai"),
    ).toBeInTheDocument();
    const latestSurface = window as typeof window & {
      __surfaceHosts: Record<
        string,
        {
          getContext: () => PluginSurfaceContext | null;
          getToolContext: () => {
            agentId: string | null;
            threadId: string | null;
          };
        }
      >;
      __surfaceContexts: Record<string, PluginSurfaceContext>;
      __lastSurfaceHost: { getContext: () => PluginSurfaceContext | null };
      __lastSurfaceContext: PluginSurfaceContext;
    };
    expect(latestSurface.__surfaceHosts.connectors.getContext()?.agentId).toBe(
      "agent-b",
    );
    expect(latestSurface.__surfaceHosts.connectors.getToolContext()).toEqual({
      agentId: "agent-b",
      threadId: null,
      locale: "en",
      theme: "light",
    });
    expect(latestSurface.__surfaceHosts.chat.getContext()?.agentId).toBe(
      "agent-a",
    );
    expect(latestSurface.__surfaceHosts.chat.getToolContext()).toEqual({
      agentId: "agent-a",
      threadId: "thread-a",
      locale: "en",
      theme: "light",
    });
    expect("setTurnContext" in latestSurface.__surfaceHosts.chat).toBe(false);
    expect("userId" in latestSurface.__surfaceContexts.chat).toBe(false);
    expect(Object.isFrozen(latestSurface.__lastSurfaceContext.readiness)).toBe(
      true,
    );
    expect(
      Object.isFrozen(
        latestSurface.__lastSurfaceContext.readiness.requiredPlugins,
      ),
    ).toBe(true);
    requestMock.mockImplementation(async (path: string) => ({ path }));
    fireEvent.click(screen.getByTestId("current-connectors"));
    fireEvent.click(screen.getByTestId("current-chat"));
    await waitFor(() =>
      expect(requestMock.mock.calls.map(([path]) => path)).toEqual([
        "/scope/agent-b",
        "/scope/agent-a",
      ]),
    );

    const oldContextHost = latestSurface.__surfaceHosts.chat;
    const switchedContext: PluginSurfaceContext = {
      ...composerContext,
      agentId: "agent-c",
      threadId: "thread-c",
      generation: "generation-c",
    };
    rerender(
      <>
        <PluginSurface
          surface="settings"
          location="connectors"
          context={settingsContext}
        />
        <PluginSurface
          surface="composer"
          location="chat"
          context={switchedContext}
        />
      </>,
    );
    expect(
      await screen.findByText("agent-c:thread-c:en:light:Asia/Shanghai"),
    ).toBeInTheDocument();
    await expect(
      (async () => oldContextHost.request("/old-generation"))(),
    ).rejects.toThrow(/inactive/i);
    expect(requestMock).toHaveBeenCalledTimes(2);

    const metadataUpdate: PluginSurfaceContext = {
      ...switchedContext,
      locale: "zh",
      theme: "dark",
      timeZone: "UTC",
    };
    rerender(
      <>
        <PluginSurface
          surface="settings"
          location="connectors"
          context={settingsContext}
        />
        <PluginSurface
          surface="composer"
          location="chat"
          context={metadataUpdate}
        />
      </>,
    );
    expect(
      screen.getByText("agent-c:thread-c:zh:dark:UTC"),
    ).toBeInTheDocument();
    expect(latestSurface.__surfaceHosts.chat.getContext()).toMatchObject({
      agentId: "agent-c",
      threadId: "thread-c",
      locale: "zh",
      theme: "dark",
      timeZone: "UTC",
    });

    let finishRequest!: (value: { path: string }) => void;
    requestMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishRequest = resolve;
        }),
    );
    fireEvent.click(screen.getByTestId("current-chat"));
    await waitFor(() => expect(requestMock).toHaveBeenCalledTimes(3));
    const failures = (window as typeof window & { __surfaceFailures: string[] })
      .__surfaceFailures;

    unmount();
    finishRequest({ path: "/scope/agent-c" });
    await waitFor(() =>
      expect(failures).toContain("Plugin UI host is inactive"),
    );
    const surfaceHost = (
      window as typeof window & {
        __lastSurfaceHost?: { request: (path: string) => Promise<unknown> };
      }
    ).__lastSurfaceHost;
    const requestCount = requestMock.mock.calls.length;
    await expect((async () => surfaceHost?.request("/late"))()).rejects.toThrow(
      /inactive/i,
    );
    expect(requestMock).toHaveBeenCalledTimes(requestCount);
    resetPluginUiLoader();
    expect(restoreUrls().length).toBeGreaterThan(0);
  });

  it("does not render registrations into an invalid surface location", () => {
    const invalidProps = {
      surface: "composer",
      location: "expert",
      context: composerContext,
    } as unknown as Parameters<typeof PluginSurface>[0];
    const { container } = render(<PluginSurface {...invalidProps} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("revokes a mounted Host synchronously when the auth session changes", () => {
    let mountedHost: import("./types").OctopPluginUIHost | undefined;
    const dispose = registerPluginSurface(
      "auth-lease-plugin",
      "composer",
      ({ host }) => {
        mountedHost = host;
        return null;
      },
    );
    const rendered = render(
      <PluginSurface
        surface="composer"
        location="chat"
        context={composerContext}
      />,
    );
    const host = mountedHost!;
    const prepare = vi.fn(async () => ({ ok: true }));
    host.registerComposerPreparation({
      captureSelection: () => ({ state: "selected", value: {} }),
      prepare,
      restoreSelection: async () => undefined,
    });

    act(() => invalidateAuthSession());

    expect(host.getContext()).toBeNull();
    expect(() => host.request("/after-auth-change")).toThrow(/inactive/i);
    expect(() =>
      host.registerComposerPreparation({
        captureSelection: () => ({ state: "selected", value: {} }),
        prepare,
        restoreSelection: async () => undefined,
      }),
    ).toThrow(/inactive/i);
    expect(requestMock).not.toHaveBeenCalled();
    rendered.unmount();
    dispose();
  });

  it("recovers the error boundary when the same owner replaces a crashed surface", async () => {
    const restoreUrls = installModuleImportStub();
    const sources = [
      `
        function CrashedSurface({ host }) {
          window.__crashedSurfaceHost = host;
          throw new Error("first surface crashed");
        }
        export function setup(host) { host.registerSurface("settings", CrashedSurface); }
      `,
      `
        const jsx = window.__OCTOP_JSX__;
        function RecoveredSurface() {
          return jsx.jsx("div", { "data-testid": "recovered-surface", children: "replacement rendered" });
        }
        export function setup(host) { host.registerSurface("settings", RecoveredSurface); }
      `,
    ];
    let sourceIndex = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(sources[sourceIndex++] ?? sources[1], { status: 200 }),
      ),
    );
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const installed = [
      {
        id: "replace-plugin",
        version: "1",
        loaded: true,
        ui: { entry: "ui/index.js", manifest: "ui/manifest.json" },
      },
    ];
    await loadInstalledPluginUis(installed);
    const { container } = render(
      <PluginSurface
        surface="settings"
        location="connectors"
        context={settingsContext}
      />,
    );
    expect(
      container.querySelector('[data-octop-plugin-ui="builtin-fallback"]'),
    ).toBeInTheDocument();

    const previousHost = (
      window as typeof window & {
        __crashedSurfaceHost: { request: (path: string) => Promise<unknown> };
      }
    ).__crashedSurfaceHost;
    await act(async () => reloadPluginToolUis(installed));
    expect(await screen.findByTestId("recovered-surface")).toHaveTextContent(
      "replacement rendered",
    );
    expect(() => previousHost.request("/old-surface")).toThrow(/inactive/i);
    expect(requestMock).not.toHaveBeenCalled();
    resetPluginUiLoader();
    restoreUrls();
  });

  it("cleans renderer registrations created by a mounted loader-owned surface host", async () => {
    const restoreUrls = installModuleImportStub();
    const source = `
      const React = window.__OCTOP_REACT__;
      function Renderer() { return null; }
      function Surface({ host }) {
        React.useEffect(() => {
          window.__mountedRendererHost = host;
          host.registerRenderer({ id: "mounted", tools: ["mounted_tool"], component: Renderer });
        }, [host]);
        return null;
      }
      export function setup(host) { host.registerSurface("settings", Surface); }
    `;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(source, { status: 200 })),
    );
    ensureBuiltinToolRenderers();
    const installed = [
      {
        id: "mounted-plugin",
        version: "1",
        loaded: true,
        ui: { entry: "ui/index.js", manifest: "ui/manifest.json" },
      },
    ];
    await loadInstalledPluginUis(installed);
    render(
      <PluginSurface
        surface="settings"
        location="connectors"
        context={settingsContext}
      />,
    );
    await waitFor(() =>
      expect(
        resolveToolRenderer({
          toolName: "mounted_tool",
          parsed: parseOctopToolOutput("plain text"),
        })?.pluginId,
      ).toBe("mounted-plugin"),
    );

    await act(async () =>
      loadInstalledPluginUis([{ ...installed[0], enabled: false }]),
    );
    expect(
      resolveToolRenderer({
        toolName: "mounted_tool",
        parsed: parseOctopToolOutput("plain text"),
      }),
    ).toBeNull();
    const mountedHost = (
      window as typeof window & {
        __mountedRendererHost: {
          request: (path: string) => Promise<unknown>;
        };
      }
    ).__mountedRendererHost;
    expect(() => mountedHost.request("/mounted-stale")).toThrow(/inactive/i);
    restoreUrls();
  });
});
