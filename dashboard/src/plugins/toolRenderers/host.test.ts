import { forwardRef, memo } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { request } from "../../api/request";
import {
  createPluginUiHost,
  createPluginUiRegistrationHost,
  createPluginUiSurfaceHost,
  createPluginUiToolHost,
  setPluginUiToolContext,
} from "./host";
import {
  clearToolRenderers,
  getPluginSurfaceVersion,
  listPluginSurfaces,
  registerPluginSurface,
  subscribePluginSurfaces,
  unregisterPluginUi,
} from "./registry";
import { parseOctopToolOutput } from "./parseToolOutput";
import { registerToolRenderer, resolveToolRenderer } from "./registry";
import type { PluginSurfaceProps, ToolRenderProps } from "./types";

vi.mock("../../api/request", () => ({
  request: vi.fn(async (path: string) => ({ path })),
}));
vi.mock("../../pages/Chat/hooks/chatStore", () => ({
  patchToolResultData: vi.fn(() => true),
}));
import * as chatStore from "../../pages/Chat/hooks/chatStore";

function DummySurface() {
  return null;
}

function DummyRenderer(_props: ToolRenderProps) {
  return null;
}

describe("plugin UI host and surface registry", () => {
  afterEach(() => {
    clearToolRenderers();
    unregisterPluginUi("one");
    unregisterPluginUi("two");
    setPluginUiToolContext({ agentId: null, threadId: null, locale: "en" });
  });

  it("binds tool UI effects to its owner and call and clones finite JSON state", () => {
    const source = { selected: ["one"] };
    const openSidePanel = vi.fn();
    const closeSidePanel = vi.fn();
    const context = {
      agentId: "agent-current",
      threadId: "thread-current",
      locale: "en",
      theme: "light" as const,
    };
    const host = createPluginUiToolHost("owner", context, {
      callId: "call-current",
      isActive: () => true,
      openSidePanel,
      closeSidePanel,
    });

    host.openSidePanel({ callId: "call-current", state: source });
    source.selected.push("mutated");
    expect(openSidePanel).toHaveBeenCalledWith({
      callId: "call-current",
      pluginId: "owner",
      state: { selected: ["one"] },
      context,
    });
    expect(() => host.openSidePanel({ callId: "other" })).toThrow(/inactive/i);
    expect(() => host.closeSidePanel("other")).toThrow(/inactive/i);
    expect(openSidePanel).toHaveBeenCalledTimes(1);
    expect(closeSidePanel).not.toHaveBeenCalled();
    host.patchResult("call-current", { patched: true });
    expect(chatStore.patchToolResultData).toHaveBeenCalledWith(
      "call-current",
      { patched: true },
      "thread-current",
    );
    expect(() => host.patchResult("other", {})).toThrow(/inactive/i);
    expect(() =>
      host.openSidePanel({
        callId: "call-current",
        state: { value: undefined } as never,
      }),
    ).toThrow(/finite JSON/i);
  });

  it("rejects functions, undefined, non-finite numbers and cycles as plugin state", () => {
    const host = createPluginUiToolHost(
      "owner",
      { agentId: "agent", threadId: "thread", locale: "en", theme: "light" },
      {
        callId: "call",
        isActive: () => true,
        openSidePanel: vi.fn(),
        closeSidePanel: vi.fn(),
      },
    );
    const cycle: Record<string, unknown> = {};
    cycle.self = cycle;
    for (const state of [
      undefined,
      () => null,
      Number.NaN,
      Number.POSITIVE_INFINITY,
      cycle,
    ]) {
      expect(() =>
        host.openSidePanel({ callId: "call", state: state as never }),
      ).toThrow(/finite JSON/i);
    }
  });

  it("invalidates a request when its tool activation ends before rejection", async () => {
    let rejectRequest!: (reason: unknown) => void;
    vi.mocked(request).mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => (rejectRequest = reject)) as never,
    );
    let active = true;
    const host = createPluginUiToolHost(
      "owner",
      { agentId: "agent", threadId: "thread", locale: "en", theme: "light" },
      {
        callId: "call",
        isActive: () => active,
        openSidePanel: vi.fn(),
        closeSidePanel: vi.fn(),
      },
    );
    const pending = host.request("/api/example");
    active = false;
    rejectRequest({ secret: "upstream" });
    await expect(pending).rejects.toThrow("Plugin UI host is inactive");
  });

  it("does not let an unbound setup host control a scoped tool tab", () => {
    const host = createPluginUiHost("setup-owner");
    expect(() => host.openSidePanel({ callId: "forged" })).toThrow(/inactive/i);
    expect(() => host.closeSidePanel("forged")).toThrow(/inactive/i);
  });

  it("refuses dock effects from registration and surface hosts without tool bindings", () => {
    const registrationHost = createPluginUiRegistrationHost(
      "registration-owner",
      1,
      () => true,
      {},
    );
    expect(() => registrationHost.openSidePanel({ callId: "forged" })).toThrow(
      "Plugin UI host is inactive",
    );
    expect(() => registrationHost.closeSidePanel("forged")).toThrow(
      "Plugin UI host is inactive",
    );

    const dispose = registerPluginSurface(
      "surface-owner",
      "settings",
      DummySurface,
    );
    const registration = listPluginSurfaces("settings").find(
      (item) => item.pluginId === "surface-owner",
    )!;
    const surfaceHost = createPluginUiSurfaceHost(
      registration,
      () => ({
        agentId: null,
        threadId: null,
        locale: "en",
        theme: "light",
        generation: "generation",
        timeZone: "UTC",
        readiness: {
          status: "ready",
          requiredPlugins: {},
          unavailablePlugins: [],
        },
      }),
      () => true,
    );
    expect(() => surfaceHost.openSidePanel({ callId: "forged" })).toThrow(
      "Plugin UI host is inactive",
    );
    expect(() => surfaceHost.closeSidePanel("forged")).toThrow(
      "Plugin UI host is inactive",
    );
    dispose();
  });

  it("keeps legacy setup hosts unscoped while preserving renderer lookup", () => {
    setPluginUiToolContext({
      agentId: "chat-agent",
      threadId: "chat-thread",
      locale: "zh",
      theme: "dark",
    });
    const host = createPluginUiHost("legacy-plugin");

    expect(host.getContext()).toBeNull();
    expect(host.getToolContext()).toEqual({
      agentId: "chat-agent",
      threadId: "chat-thread",
      locale: "zh",
      theme: "dark",
    });

    host.registerRenderer({
      id: "legacy",
      tools: ["legacy_tool"],
      component: DummyRenderer,
    });
    host.registerRenderer({
      id: "override",
      pluginId: "another-plugin",
      component: DummyRenderer,
    });
    const resolved = resolveToolRenderer({
      toolName: "legacy_tool",
      parsed: parseOctopToolOutput("plain text"),
    });
    expect(resolved?.pluginId).toBe("legacy-plugin");
    expect(resolved?.component).toBe(DummyRenderer);
    expect(
      resolveToolRenderer({
        pluginId: "another-plugin",
        parsed: { isJson: true, raw: {}, octopUi: { renderer: "override" } },
      })?.pluginId,
    ).toBe("another-plugin");
  });

  it("validates exact surfaces and React component shapes at runtime", () => {
    const register = registerPluginSurface as (
      pluginId: string,
      surface: string,
      component: unknown,
    ) => () => void;

    expect(() => register("one", "unknown", DummySurface)).toThrow(/surface/i);
    expect(() => register("one", "settings", { arbitrary: true })).toThrow(
      /component/i,
    );
    expect(() =>
      register("one", "settings", {
        $$typeof: Symbol.for("react.memo"),
        unrelated: true,
      }),
    ).toThrow(/component/i);

    const MemoSurface = memo(DummySurface);
    const RefSurface = forwardRef<HTMLDivElement, PluginSurfaceProps>(
      (_props, _ref) => null,
    );
    expect(() => register("one", "settings", MemoSurface)).not.toThrow();
    expect(() => register("one", "composer", RefSurface)).not.toThrow();
  });

  it("replaces only its owner's slot and an old disposer cannot remove the replacement", () => {
    const first = registerPluginSurface("one", "settings", DummySurface);
    const replacement = () => null;
    const disposeReplacement = registerPluginSurface(
      "one",
      "settings",
      replacement,
    );
    registerPluginSurface("two", "settings", DummySurface);

    first();
    expect(
      listPluginSurfaces("settings").map((entry) => [
        entry.pluginId,
        entry.component,
      ]),
    ).toEqual([
      ["one", replacement],
      ["two", DummySurface],
    ]);

    disposeReplacement();
    expect(
      listPluginSurfaces("settings").map((entry) => entry.pluginId),
    ).toEqual(["two"]);
  });

  it("notifies subscribers and removes renderers and surfaces together by owner", () => {
    const listener = vi.fn();
    const unsubscribe = subscribePluginSurfaces(listener);
    const before = getPluginSurfaceVersion();
    registerPluginSurface("one", "composer", DummySurface);
    registerToolRenderer({
      id: "renderer",
      pluginId: "one",
      component: DummyRenderer,
    });
    registerPluginSurface("two", "composer", DummySurface);

    expect(getPluginSurfaceVersion()).toBeGreaterThan(before);
    expect(listener).toHaveBeenCalled();
    expect(
      listPluginSurfaces("composer").map((entry) => entry.pluginId),
    ).toEqual(["one", "two"]);

    unregisterPluginUi("one");
    expect(
      listPluginSurfaces("composer").map((entry) => entry.pluginId),
    ).toEqual(["two"]);
    expect(
      resolveToolRenderer({
        toolName: "unused",
        pluginId: "one",
        parsed: { isJson: true, raw: {}, octopUi: { renderer: "renderer" } },
      }),
    ).toBeNull();
    unsubscribe();
  });
});
