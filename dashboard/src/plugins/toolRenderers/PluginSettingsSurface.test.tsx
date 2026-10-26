import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { pluginsApi } from "../../api/modules/plugins";
import { PluginSettingsSurface } from "./PluginSettingsSurface";
import { usePluginToolUis } from "./usePluginToolUis";

const { loadUiMock, updateIndexMock, setChatContextMock, authState } =
  vi.hoisted(() => ({
    loadUiMock: vi.fn(),
    updateIndexMock: vi.fn(),
    setChatContextMock: vi.fn(),
    authState: {
      epoch: 1,
      user: { id: 7, username: "tester", role: "user" },
    },
  }));

const contextSpy = vi.fn();
vi.mock("../../api/modules/plugins", () => ({
  pluginsApi: { list: vi.fn(), requirements: vi.fn() },
}));
vi.mock("../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => authState.user,
}));
vi.mock("../../api/authSession", () => ({
  getAuthSessionEpoch: () => authState.epoch,
  subscribeAuthSession: () => () => {},
}));
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "Asia/Shanghai",
}));
vi.mock("../../i18n", () => ({ default: { language: "en" } }));
vi.mock("./loader", () => ({ loadInstalledPluginUis: loadUiMock }));
vi.mock("./toolPluginIndex", () => ({
  updateToolPluginIndex: updateIndexMock,
}));
vi.mock("../toolRenderers", () => ({
  ensureBuiltinToolRenderers: vi.fn(),
  loadInstalledPluginUis: loadUiMock,
  isPluginUiLoaded: vi.fn(() => false),
  setPluginUiToolContext: setChatContextMock,
}));
vi.mock("./PluginSurface", () => ({
  PluginSurface: (props: { location: string; context: unknown }) => {
    contextSpy(props);
    const jsx = (
      window as Window & {
        __OCTOP_JSX__?: { jsx: (type: string, props: unknown) => ReactNode };
      }
    ).__OCTOP_JSX__;
    if (
      jsx &&
      (window as Window & { __OCTOP_PLUGIN_RUNTIME_TEST__?: boolean })
        .__OCTOP_PLUGIN_RUNTIME_TEST__
    ) {
      return jsx.jsx("div", {
        "data-testid": "runtime-plugin-surface",
        children: "plugin runtime ready",
      });
    }
    const generation = (props.context as { generation: string }).generation;
    return (
      <>
        <div data-testid={`surface-${props.location}`} />
        <div key={generation}>
          <input
            data-testid={`surface-draft-${props.location}`}
            defaultValue=""
          />
        </div>
      </>
    );
  },
}));

const list = vi.mocked(pluginsApi.list);

describe("PluginSettingsSurface", () => {
  afterEach(() => {
    vi.clearAllMocks();
    delete (window as Window & { __OCTOP_PLUGIN_RUNTIME_TEST__?: boolean })
      .__OCTOP_PLUGIN_RUNTIME_TEST__;
    delete (window as Window & { __OCTOP_REACT__?: unknown }).__OCTOP_REACT__;
    delete (window as Window & { __OCTOP_JSX__?: unknown }).__OCTOP_JSX__;
  });

  it("initializes the host React runtime before the first settings module setup", async () => {
    const React = await import("react");
    const ReactJSX = await import("react/jsx-runtime");
    list.mockResolvedValueOnce([]);
    loadUiMock.mockImplementationOnce(async () => {
      const hostWindow = window as Window & {
        __OCTOP_REACT__?: unknown;
        __OCTOP_JSX__?: unknown;
        __OCTOP_PLUGIN_RUNTIME_TEST__?: boolean;
      };
      if (hostWindow.__OCTOP_PLUGIN_RUNTIME_TEST__) {
        expect(hostWindow.__OCTOP_REACT__).toBe(React);
        expect(hostWindow.__OCTOP_JSX__).toBe(ReactJSX);
      }
    });
    (
      window as Window & { __OCTOP_PLUGIN_RUNTIME_TEST__?: boolean }
    ).__OCTOP_PLUGIN_RUNTIME_TEST__ = true;
    render(
      <PluginSettingsSurface agentId="agent-settings" location="expert" />,
    );
    expect(
      await screen.findByTestId("runtime-plugin-surface"),
    ).toHaveTextContent("plugin runtime ready");
    expect(pluginsApi.requirements).not.toHaveBeenCalled();
    expect(setChatContextMock).not.toHaveBeenCalled();
  });

  it("preserves a settings draft for the same verified principal and invalidates it with the auth epoch", async () => {
    authState.epoch = 17;
    authState.user = { id: 7, username: "tester", role: "user" };
    list.mockResolvedValue([]);
    const { rerender } = render(
      <PluginSettingsSurface agentId="agent-expert" location="expert" />,
    );
    const draft = await screen.findByTestId("surface-draft-expert");
    const originalGeneration =
      contextSpy.mock.calls.at(-1)?.[0].context.generation;
    (draft as HTMLInputElement).value = "unsaved settings draft";

    authState.user = { id: 7, username: "tester", role: "user" };
    rerender(
      <PluginSettingsSurface agentId="agent-expert" location="expert" />,
    );
    expect(contextSpy.mock.calls.at(-1)?.[0].context.generation).toBe(
      originalGeneration,
    );
    expect(screen.getByTestId("surface-draft-expert")).toHaveProperty(
      "value",
      "unsaved settings draft",
    );

    authState.user = { id: 8, username: "other", role: "user" };
    authState.epoch += 1;
    rerender(
      <PluginSettingsSurface agentId="agent-expert" location="expert" />,
    );
    expect(contextSpy.mock.calls.at(-1)?.[0].context.generation).not.toBe(
      originalGeneration,
    );
    expect(screen.getByTestId("surface-draft-expert")).toHaveProperty(
      "value",
      "",
    );
  });

  it("loads inventory for independent settings targets without querying requirements or changing Chat context", async () => {
    list.mockResolvedValue([]);
    const { rerender } = render(
      <>
        <PluginSettingsSurface agentId={null} location="connectors" />
        <PluginSettingsSurface agentId="agent-expert" location="expert" />
      </>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("surface-connectors")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("surface-expert")).toBeInTheDocument();
    expect(contextSpy.mock.calls.map(([props]) => props)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          location: "connectors",
          context: expect.objectContaining({ agentId: null, threadId: null }),
        }),
        expect.objectContaining({
          location: "expert",
          context: expect.objectContaining({
            agentId: "agent-expert",
            threadId: null,
            timeZone: "Asia/Shanghai",
          }),
        }),
      ]),
    );
    expect(pluginsApi.requirements).not.toHaveBeenCalled();

    rerender(<PluginSettingsSurface agentId={null} location="connectors" />);
    expect(pluginsApi.requirements).not.toHaveBeenCalled();
  });

  it("shows a localized error and retries inventory on the shared lifecycle event", async () => {
    list.mockRejectedValueOnce(new Error("not shown to users"));
    const { container } = render(
      <PluginSettingsSurface agentId={null} location="connectors" />,
    );
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(container).not.toHaveTextContent("not shown to users");
    list.mockResolvedValueOnce([]);
    act(() => window.dispatchEvent(new Event("octop-plugins-changed")));
    await waitFor(() =>
      expect(screen.getByTestId("surface-connectors")).toBeInTheDocument(),
    );
  });

  it("does not let a late Chat inventory override a newer settings inventory", async () => {
    let resolveOldInventory!: (plugins: never[]) => void;
    list
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveOldInventory = resolve as (plugins: never[]) => void;
        }),
      )
      .mockResolvedValueOnce([]);
    const chat = renderHook(() =>
      usePluginToolUis({ agentId: "agent-chat", threadId: null }),
    );
    await waitFor(() => expect(list).toHaveBeenCalledTimes(1));
    render(<PluginSettingsSurface agentId={null} location="connectors" />);
    await waitFor(() =>
      expect(screen.getByTestId("surface-connectors")).toBeInTheDocument(),
    );
    expect(loadUiMock).toHaveBeenCalledTimes(1);
    expect(updateIndexMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveOldInventory([
        {
          id: "stale-enabled",
          enabled: true,
          loaded: true,
        },
      ] as never[]);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(loadUiMock).toHaveBeenCalledTimes(1);
    expect(updateIndexMock).toHaveBeenCalledTimes(1);
    chat.unmount();
  });

  it("shows retryable error when the newer inventory owner is cancelled before its inventory returns", async () => {
    let resolveSettingsInventory!: (plugins: never[]) => void;
    let resolveCancelledInventory!: (plugins: never[]) => void;
    list
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveSettingsInventory = resolve as (plugins: never[]) => void;
        }),
      )
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveCancelledInventory = resolve as (plugins: never[]) => void;
        }),
      );
    render(<PluginSettingsSurface agentId={null} location="connectors" />);
    await waitFor(() => expect(list).toHaveBeenCalledTimes(1));
    const chat = renderHook(() =>
      usePluginToolUis({ agentId: "agent-cancelled", threadId: null }),
    );
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));

    chat.unmount();
    await act(async () => {
      resolveCancelledInventory([]);
      await Promise.resolve();
    });
    await act(async () => {
      resolveSettingsInventory([]);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "common.retry" }),
    ).toBeInTheDocument();
    expect(updateIndexMock).not.toHaveBeenCalled();
    expect(loadUiMock).not.toHaveBeenCalled();
  });

  it("shows retryable error when the newer owner is cancelled during loader reconciliation", async () => {
    let resolveSettingsInventory!: (plugins: never[]) => void;
    let resolveNewerInventory!: (plugins: never[]) => void;
    let finishNewerLoader!: () => void;
    list
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveSettingsInventory = resolve as (plugins: never[]) => void;
        }),
      )
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveNewerInventory = resolve as (plugins: never[]) => void;
        }),
      );
    loadUiMock.mockReturnValueOnce(
      new Promise<void>((resolve) => {
        finishNewerLoader = resolve;
      }),
    );
    render(<PluginSettingsSurface agentId={null} location="connectors" />);
    await waitFor(() => expect(list).toHaveBeenCalledTimes(1));
    const chat = renderHook(() =>
      usePluginToolUis({ agentId: "agent-cancelled", threadId: null }),
    );
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    await act(async () => {
      resolveNewerInventory([]);
      await Promise.resolve();
    });
    await waitFor(() => expect(loadUiMock).toHaveBeenCalledTimes(1));

    chat.unmount();
    await act(async () => {
      resolveSettingsInventory([]);
      await Promise.resolve();
    });
    await act(async () => {
      finishNewerLoader();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "common.retry" }),
    ).toBeInTheDocument();
    expect(updateIndexMock).toHaveBeenCalledTimes(1);
    expect(loadUiMock).toHaveBeenCalledTimes(1);
  });
});
