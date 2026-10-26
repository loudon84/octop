import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { pluginsApi } from "../../api/modules/plugins";
import { usePluginToolUis } from "./usePluginToolUis";

vi.mock("../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => ({ id: 7, username: "tester" }),
}));
vi.mock("../../api/modules/plugins", () => ({
  pluginsApi: {
    list: vi.fn(),
    requirements: vi.fn(),
  },
}));
vi.mock("./loader", () => ({
  ensureBuiltinToolRenderers: vi.fn(),
  loadInstalledPluginUis: vi.fn(),
  isPluginUiLoaded: vi.fn((pluginId: string) => pluginId === "ui-ready"),
}));
vi.mock("../toolRenderers", () => ({
  ensureBuiltinToolRenderers: vi.fn(),
  loadInstalledPluginUis: vi.fn(),
  isPluginUiLoaded: vi.fn((pluginId: string) => pluginId === "ui-ready"),
  setPluginUiToolContext: vi.fn(),
}));
vi.mock("./toolPluginIndex", () => ({ updateToolPluginIndex: vi.fn() }));

const list = vi.mocked(pluginsApi.list);
const requirements = vi.mocked(pluginsApi.requirements);

function inventory() {
  return [
    { id: "headless", enabled: true, loaded: true },
    {
      id: "ui-ready",
      enabled: true,
      loaded: true,
      ui: { entry: "ui/index.js", manifest: "ui/manifest.json" },
    },
    {
      id: "ui-missing",
      enabled: true,
      loaded: true,
      ui: { entry: "ui/index.js", manifest: "ui/manifest.json" },
    },
  ];
}

function readyRequirements(required: Record<string, string>) {
  return {
    api_version: 1 as const,
    required_plugins: required,
    unavailable_plugins: [],
  };
}

describe("usePluginToolUis readiness", () => {
  beforeEach(() => {
    list.mockResolvedValue(inventory());
    requirements.mockResolvedValue(readyRequirements({}));
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    vi.restoreAllMocks();
  });

  it("keeps headless requirements ready and marks a required UI module unavailable until loaded", async () => {
    requirements.mockResolvedValue(
      readyRequirements({ headless: "1", "ui-ready": "1", "ui-missing": "2" }),
    );
    const { result } = renderHook(() =>
      usePluginToolUis({ agentId: "agent-a", threadId: "thread-a" }),
    );
    await waitFor(() => expect(result.current.readiness.status).toBe("ready"));
    expect(result.current.readiness).toMatchObject({
      status: "ready",
      unavailablePlugins: ["ui-missing"],
    });
    expect(requirements).toHaveBeenCalledWith("agent-a", "thread-a");
  });

  it("resolves explicit targets without publishing a late old-target response", async () => {
    let finishAgentA!: (value: ReturnType<typeof readyRequirements>) => void;
    requirements.mockImplementation((agentId) => {
      if (agentId === "agent-a") {
        return new Promise((resolve) => {
          finishAgentA = resolve;
        });
      }
      return Promise.resolve(readyRequirements({ current: "2" }));
    });
    const { result, rerender } = renderHook(
      ({ agentId }: { agentId: string }) =>
        usePluginToolUis({ agentId, threadId: null }),
      { initialProps: { agentId: "agent-a" } },
    );
    await waitFor(() => expect(finishAgentA).toBeTypeOf("function"));
    rerender({ agentId: "agent-b" });
    await waitFor(() =>
      expect(result.current.readiness).toMatchObject({
        status: "ready",
        requiredPlugins: { current: "2" },
      }),
    );
    await act(async () => {
      finishAgentA(readyRequirements({ stale: "1" }));
    });
    expect(result.current.readiness).toMatchObject({
      status: "ready",
      requiredPlugins: { current: "2" },
    });

    const explicit = await result.current.refresh({
      agentId: "agent-c",
      threadId: "thread-c",
    });
    expect(requirements).toHaveBeenLastCalledWith("agent-c", "thread-c");
    expect(explicit).toMatchObject({ status: "ready" });
    expect(result.current.readiness).toMatchObject({
      status: "ready",
      requiredPlugins: { current: "2" },
    });
  });

  it("refreshes on lifecycle and focus, pauses the 30 second timer while hidden, and retries on return", async () => {
    const nativeSetInterval = globalThis.setInterval.bind(globalThis);
    const intervalCallbacks: (() => void)[] = [];
    vi.spyOn(globalThis, "setInterval").mockImplementation(
      (handler, delay, ...args) => {
        if (delay === 30_000 && typeof handler === "function") {
          intervalCallbacks.push(handler as () => void);
        }
        return nativeSetInterval(handler, delay, ...args);
      },
    );
    const clearIntervalSpy = vi.spyOn(globalThis, "clearInterval");
    const { unmount } = renderHook(() =>
      usePluginToolUis({ agentId: "agent-a" }),
    );
    await waitFor(() => expect(requirements).toHaveBeenCalledTimes(1));
    act(() => {
      window.dispatchEvent(new Event("octop-plugins-changed"));
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(requirements).toHaveBeenCalledTimes(2));
    expect(intervalCallbacks).toHaveLength(1);
    act(() => intervalCallbacks[0]?.());
    await waitFor(() => expect(requirements).toHaveBeenCalledTimes(3));

    Object.defineProperty(document, "visibilityState", { value: "hidden" });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    const callsWhenHidden = requirements.mock.calls.length;
    expect(requirements).toHaveBeenCalledTimes(callsWhenHidden);
    expect(clearIntervalSpy).toHaveBeenCalled();
    Object.defineProperty(document, "visibilityState", { value: "visible" });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(() =>
      expect(requirements.mock.calls.length).toBeGreaterThan(callsWhenHidden),
    );
    expect(intervalCallbacks).toHaveLength(2);
    unmount();
    const callsAfterUnmount = requirements.mock.calls.length;
    window.dispatchEvent(new Event("focus"));
    expect(requirements).toHaveBeenCalledTimes(callsAfterUnmount);
  });

  it("reports inventory or requirement failures as non-ready", async () => {
    list.mockRejectedValueOnce(new Error("inventory failed"));
    const { result } = renderHook(() =>
      usePluginToolUis({ agentId: "agent-a" }),
    );
    await waitFor(() =>
      expect(result.current.readiness).toMatchObject({
        status: "error",
        reason: "unavailable",
      }),
    );
    expect(requirements).not.toHaveBeenCalled();
  });
});
