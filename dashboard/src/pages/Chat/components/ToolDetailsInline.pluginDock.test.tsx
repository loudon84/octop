import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { memo, useEffect, useMemo, useRef, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { invalidateAuthSession } from "../../../api/authSession";
import { request } from "../../../api/request";
import type { ChatMessage } from "../hooks/useChat";
import { useChatDockPanel } from "../hooks/useChatDockPanel";
import ChatDockToolUiContent from "./ChatDockToolUiContent";
import ChatDockPanel from "./ChatDockPanel";
import { ChatToolDockProvider } from "../ChatToolDockContext";
import { ToolDetailsInline } from "./MessageBubble";
import {
  clearToolRenderers,
  registerToolRenderer,
  unregisterPluginRenderers,
} from "../../../plugins/toolRenderers/registry";
import type { ToolRenderProps } from "../../../plugins/toolRenderers/types";
import {
  clearPendingThread,
  isPendingThread,
  markPendingThread,
} from "../hooks/useSessions";
import {
  getSnapshot,
  removeSession,
  setFocusedChatSession,
  setMessages,
} from "../hooks/chatStore";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "en" } }),
}));
vi.mock("../hooks/useToolMessageByCallId", () => ({
  useToolMessageByCallId: (_threadId: string | null, callId: string) =>
    callId === toolData.callId
      ? toolMessage
      : callId === secondToolData.callId
      ? secondToolMessage
      : null,
}));
vi.mock("../../../api/request", () => ({
  request: vi.fn(() => Promise.resolve({ ok: true })),
}));
vi.mock("../../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => ({ id: 1 }),
}));
vi.mock("../../../utils/browserProfile", () => ({
  resolveBrowserProfile: () => "profile-1",
}));
vi.mock("../../../components/BrowserWorkspace", () => ({
  default: () => <div data-testid="browser-workspace" />,
}));
vi.mock("../../../components/BrowserWorkspace/ChatDockPanelShell", () => ({
  default: ({ children }: { children: ReactNode }) => (
    <div data-testid="production-dock-panel">{children}</div>
  ),
}));
vi.mock("../../Agent/Workspace/components/WorkspaceDrawer", () => ({
  default: () => <div data-testid="workspace-drawer" />,
}));
vi.mock("./ChatDockFileList", () => ({
  default: () => <div data-testid="file-list" />,
}));
vi.mock("./FilePanelContent", () => ({
  default: () => <div data-testid="file-panel" />,
}));
vi.mock("./KnowledgeCitationPanelContent", () => ({
  default: () => <div data-testid="knowledge-panel" />,
}));
vi.mock("../../Control/Terminal", () => ({
  default: () => <div data-testid="terminal-page" />,
}));

const toolData = {
  callId: "call-one",
  name: "test_tool",
  pluginId: "dock-test",
  output: JSON.stringify({
    octop_ui: { renderer: "dock-renderer", version: 1 },
    data: { ready: true },
  }),
};
const ORIGINAL_OUTPUT = toolData.output;
const secondToolData = { ...toolData, callId: "call-two" };
const toolMessage = {
  id: "tool-message",
  role: "assistant",
  content: "",
  status: "done",
  timestamp: 1,
  toolData,
} as ChatMessage;
const secondToolMessage = {
  ...toolMessage,
  id: "tool-message-two",
  toolData: secondToolData,
} as ChatMessage;

let latestHost: ToolRenderProps["host"] | null = null;
let latestPresentation: ToolRenderProps["presentation"];
const hostsByPresentation = new Map<string, ToolRenderProps["host"]>();
const mountReads: string[] = [];
const rendererMounts: string[] = [];
const rendererInstances = new Map<string, number>();
const rendererPresentations: Array<{
  pluginId: string;
  location: string | undefined;
  state: unknown;
}> = [];
let readOnMount = false;
let latestDock: ReturnType<typeof useChatDockPanel> | null = null;
let nextRendererInstanceId = 0;

function DockRenderer(props: ToolRenderProps) {
  const instanceIdRef = useRef<number | null>(null);
  if (instanceIdRef.current === null) {
    instanceIdRef.current = ++nextRendererInstanceId;
  }
  const instanceId = instanceIdRef.current;
  latestHost = props.host;
  latestPresentation = props.presentation;
  rendererPresentations.push({
    pluginId: props.pluginId,
    location: props.presentation?.location,
    state: props.presentation?.state ?? null,
  });
  hostsByPresentation.set(
    `${props.callId}:${props.presentation?.location}`,
    props.host,
  );
  rendererInstances.set(
    `${props.callId}:${props.presentation?.location}`,
    instanceId,
  );
  useEffect(() => {
    const mountedInstanceId = instanceIdRef.current!;
    rendererMounts.push(`mount:${mountedInstanceId}`);
    return () => rendererMounts.push(`unmount:${mountedInstanceId}`);
  }, []);
  useEffect(() => {
    if (!readOnMount) return;
    try {
      props.host.getToolContext();
      mountReads.push(`${props.presentation?.location}:allowed`);
    } catch {
      mountReads.push(`${props.presentation?.location}:inactive`);
    }
  }, [props.host, props.presentation?.location]);
  return (
    <div data-testid={`renderer-${props.presentation?.location}`}>
      <output>{JSON.stringify(props.presentation?.state ?? null)}</output>
      <button
        type="button"
        onClick={() =>
          props.host.openSidePanel({
            callId: props.callId!,
            state: {
              selected:
                props.presentation?.location === "inline" ? "first" : "second",
            },
          })
        }
      >
        select-{props.callId}-
        {props.presentation?.location === "inline" ? "first" : "second"}
      </button>
      <button
        type="button"
        onClick={() => props.host.closeSidePanel(props.callId!)}
      >
        close-reader
      </button>
      <button
        type="button"
        onClick={() => props.host.openSidePanel({ callId: props.callId! })}
      >
        generic-open
      </button>
    </div>
  );
}

const MemoToolDetailsInline = memo(ToolDetailsInline);

function Harness({
  agentId = "chat-agent",
  threadId = "thread-one",
  multiple = false,
  stableChild = false,
  productionPanel = false,
  sourceVisible = true,
}: {
  agentId?: string | null;
  threadId?: string | null;
  multiple?: boolean;
  stableChild?: boolean;
  productionPanel?: boolean;
  sourceVisible?: boolean;
}) {
  const toolTargetThreadId =
    threadId && !isPendingThread(threadId) ? threadId : null;
  const dock = useChatDockPanel(false, agentId, toolTargetThreadId);
  latestDock = dock;
  const stableTool = useMemo(
    () => (
      <MemoToolDetailsInline
        toolData={toolData}
        isStreaming={false}
        agentId="team-speaker"
      />
    ),
    [],
  );
  return (
    <ChatToolDockProvider
      dockOpen={dock.dockOpen}
      openTabs={dock.openTabs}
      activeTabId={dock.activeTabId}
      resolvedAgentId={agentId ?? null}
      activeThreadId={toolTargetThreadId}
      scopeIdentity={dock.toolScopeIdentity}
      openToolUiPanel={dock.openToolUiTab}
      closeToolUiPanel={(callId) => dock.closeTab(`toolUi:${callId}`)}
      focusToolUiPanel={dock.focusToolUiTab}
      registerToolUiBinding={dock.registerToolUiBinding}
    >
      {sourceVisible && stableChild ? (
        stableTool
      ) : sourceVisible ? (
        <ToolDetailsInline
          toolData={toolData}
          isStreaming={false}
          agentId="team-speaker"
        />
      ) : null}
      {multiple ? (
        <ToolDetailsInline
          toolData={secondToolData}
          isStreaming={false}
          agentId="another-speaker"
        />
      ) : null}
      {!productionPanel &&
      dock.dockOpen &&
      dock.activeTab?.kind === "toolUi" ? (
        <ChatDockToolUiContent
          threadId={threadId ?? null}
          callId={dock.activeTab.callId}
          agentId={agentId ?? ""}
          isStreamingTurn={false}
        />
      ) : null}
      {productionPanel ? (
        <ChatDockPanel
          mode={dock.dockMode}
          onModeChange={dock.handleModeChange}
          onClose={dock.handleClose}
          agentId={agentId ?? ""}
          filePaths={[]}
          openTabs={dock.openTabs}
          activeTabId={dock.activeTabId}
          onSelectTab={dock.setActiveTab}
          onCloseTab={dock.closeTab}
          onOpenFile={dock.openFileAt}
          threadId={threadId ?? null}
          isStreamingTurn={false}
          surfaceVisible={dock.dockOpen}
        />
      ) : null}
      <button type="button" onClick={dock.handleClose}>
        close-dock
      </button>
      <button type="button" onClick={() => dock.closeTab(dock.activeTab!.id)}>
        close-active-tab
      </button>
      <button type="button" onClick={() => dock.openBrowserTab()}>
        open-browser
      </button>
      <button
        type="button"
        onClick={() => dock.openToolUiTab({ callId: toolData.callId })}
      >
        open-generic
      </button>
    </ChatToolDockProvider>
  );
}

describe("ToolDetailsInline scoped dock bridge", () => {
  afterEach(() => {
    cleanup();
    clearToolRenderers();
    clearPendingThread("thread-being-created");
    clearPendingThread("thread-pending-ready");
    latestHost = null;
    latestPresentation = undefined;
    hostsByPresentation.clear();
    mountReads.length = 0;
    readOnMount = false;
    latestDock = null;
    rendererMounts.length = 0;
    rendererInstances.clear();
    rendererPresentations.length = 0;
    toolData.output = ORIGINAL_OUTPUT;
    setFocusedChatSession(null);
    removeSession("thread-one");
    removeSession("thread-foreign");
  });

  function mount(
    props: {
      agentId?: string | null;
      threadId?: string | null;
      stableChild?: boolean;
      multiple?: boolean;
      productionPanel?: boolean;
      sourceVisible?: boolean;
    } = {},
  ) {
    registerToolRenderer({
      id: "dock-renderer",
      pluginId: "dock-test",
      tools: ["test_tool"],
      component: DockRenderer,
    });
    return render(<Harness {...props} />);
  }

  it("hands cloned state to the actual dock reader and replaces the same call activation", async () => {
    mount();
    expect(latestHost!.getToolContext()).toMatchObject({
      agentId: "chat-agent",
      threadId: "thread-one",
    });
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    expect(screen.getByTestId("renderer-side-panel")).toBeInTheDocument();
    expect(screen.getByText('{"selected":"first"}')).toBeInTheDocument();
    expect(latestPresentation).toMatchObject({
      location: "side-panel",
      active: true,
    });
    const oldHost = latestHost!;
    let resolveRequest!: (value: unknown) => void;
    vi.mocked(request).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveRequest = resolve;
      }) as never,
    );
    const pending = oldHost.request("/api/example");

    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-second" }),
    );
    expect(screen.getByText('{"selected":"second"}')).toBeInTheDocument();
    expect(latestHost).not.toBe(oldHost);
    expect(() => oldHost.openSidePanel({ callId: toolData.callId })).toThrow(
      /inactive/i,
    );
    resolveRequest({ stale: true });
    await expect(pending).rejects.toThrow("Plugin UI host is inactive");
  });

  it("uses the production ChatDockPanel activation key to remount the reader with new state", async () => {
    mount({ productionPanel: true });
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    expect(screen.getByTestId("renderer-side-panel")).toBeInTheDocument();
    const firstInstance = rendererInstances.get("call-one:side-panel")!;
    const oldHost = hostsByPresentation.get("call-one:side-panel")!;
    expect(rendererMounts).toContain(`mount:${firstInstance}`);
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-second" }),
    );
    const secondInstance = rendererInstances.get("call-one:side-panel")!;
    const newHost = hostsByPresentation.get("call-one:side-panel")!;
    expect(secondInstance).not.toBe(firstInstance);
    expect(rendererMounts).toContain(`unmount:${firstInstance}`);
    expect(rendererMounts).toContain(`mount:${secondInstance}`);
    expect(() => oldHost.getToolContext()).toThrow(/inactive/i);
    await expect(newHost.request("/api/second-reader")).resolves.toEqual({
      ok: true,
    });
  });

  it("does not revive hide/reopen requests and proves the new production reader works", async () => {
    mount({ productionPanel: true });
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    const oldHost = hostsByPresentation.get("call-one:side-panel")!;
    let resolveSuccess!: (value: unknown) => void;
    let rejectFailure!: (reason: Error) => void;
    const success = new Promise((resolve) => {
      resolveSuccess = resolve;
    });
    const failure = new Promise((_resolve, reject) => {
      rejectFailure = reject;
    });
    vi.mocked(request)
      .mockImplementationOnce(() => success as never)
      .mockImplementationOnce(() => failure as never);
    const staleSuccess = oldHost.request("/api/late-success");
    const staleFailure = oldHost.request("/api/late-failure");

    act(() => latestDock!.openBrowserTab());
    act(() => latestDock!.setActiveTab("toolUi:call-one"));
    const newHost = hostsByPresentation.get("call-one:side-panel")!;
    expect(newHost).not.toBe(oldHost);
    expect(newHost.getToolContext()).toMatchObject({ threadId: "thread-one" });
    await expect(newHost.request("/api/new-reader")).resolves.toEqual({
      ok: true,
    });
    resolveSuccess({ stale: true });
    rejectFailure(new Error("upstream failure"));
    await expect(staleSuccess).rejects.toThrow("Plugin UI host is inactive");
    await expect(staleFailure).rejects.toThrow("Plugin UI host is inactive");
  });

  it("invalidates dock requests on auth change and permits a fresh same-call Host", async () => {
    mount({ productionPanel: true });
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    const oldHost = hostsByPresentation.get("call-one:side-panel")!;
    let resolveRequest!: (value: unknown) => void;
    let rejectRequest!: (reason: Error) => void;
    vi.mocked(request)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveRequest = resolve;
          }) as never,
      )
      .mockImplementationOnce(
        () =>
          new Promise((_resolve, reject) => {
            rejectRequest = reject;
          }) as never,
      );
    const pendingSuccess = oldHost.request("/api/auth-success");
    const pendingFailure = oldHost.request("/api/auth-failure");
    act(() => invalidateAuthSession());
    expect(() => oldHost.getToolContext()).toThrow(/inactive/i);
    const sourceHost = hostsByPresentation.get("call-one:inline")!;
    expect(sourceHost.getToolContext()).toMatchObject({
      threadId: "thread-one",
    });
    act(() => sourceHost.openSidePanel({ callId: "call-one" }));
    const freshHost = hostsByPresentation.get("call-one:side-panel")!;
    expect(freshHost).not.toBe(oldHost);
    expect(freshHost.getToolContext()).toMatchObject({
      threadId: "thread-one",
    });
    await expect(freshHost.request("/api/fresh-auth")).resolves.toEqual({
      ok: true,
    });
    resolveRequest({ stale: true });
    rejectRequest(new Error("upstream failure"));
    await expect(pendingSuccess).rejects.toThrow("Plugin UI host is inactive");
    await expect(pendingFailure).rejects.toThrow("Plugin UI host is inactive");
  });

  it("notifies memoized stable source children when the tool presentation changes", () => {
    mount({ stableChild: true });
    expect(screen.getByTestId("renderer-inline")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    expect(screen.queryByTestId("renderer-inline")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /test_tool chat\.toolUiDockedHint/ }),
    ).toBeInTheDocument();
  });

  it("allows a newly mounted inline renderer effect to read after dock close", () => {
    readOnMount = true;
    mount();
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "close-dock" }));
    expect(mountReads).toContain("inline:allowed");
    expect(mountReads).not.toContain("inline:inactive");
  });

  it("invalidates a dock host synchronously on close and returns to the inline renderer", () => {
    const view = mount();
    const sourceRegion = screen.getAllByRole("group", {
      name: "test_tool",
    })[0]!;
    const focus = vi.spyOn(sourceRegion, "focus");
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    const staleHost = latestHost!;
    fireEvent.click(screen.getByRole("button", { name: "close-dock" }));
    expect(() => staleHost.openSidePanel({ callId: toolData.callId })).toThrow(
      /inactive/i,
    );
    expect(screen.getByTestId("renderer-inline")).toBeInTheDocument();
    expect(screen.getAllByRole("group", { name: "test_tool" })[0]).toBe(
      sourceRegion,
    );
    expect(focus).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(sourceRegion);
    view.unmount();
  });

  it("returns focus when the owned plugin Host closes its active tab", () => {
    mount();
    const sourceRegion = screen.getAllByRole("group", {
      name: "test_tool",
    })[0]!;
    const focus = vi.spyOn(sourceRegion, "focus");
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "close-reader" }));
    expect(screen.getByTestId("renderer-inline")).toBeInTheDocument();
    expect(focus).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(sourceRegion);
  });

  it("returns focus when the active tool tab is closed", () => {
    mount();
    const sourceRegion = screen.getAllByRole("group", {
      name: "test_tool",
    })[0]!;
    const focus = vi.spyOn(sourceRegion, "focus");
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "close-active-tab" }));
    expect(screen.getByTestId("renderer-inline")).toBeInTheDocument();
    expect(focus).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(sourceRegion);
  });

  it("does not focus the source on background switch or after source detaches", () => {
    const view = mount();
    const sourceRegion = screen.getAllByRole("group", {
      name: "test_tool",
    })[0]!;
    const focus = vi.spyOn(sourceRegion, "focus");
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "open-browser" }));
    expect(focus).not.toHaveBeenCalled();
    act(() => latestDock!.setActiveTab("toolUi:call-one"));
    view.rerender(<Harness sourceVisible={false} />);
    fireEvent.click(screen.getByRole("button", { name: "close-dock" }));
    expect(sourceRegion.isConnected).toBe(false);
    expect(focus).not.toHaveBeenCalled();
  });

  it("does not focus the source when closing its background tool tab", () => {
    mount();
    const sourceRegion = screen.getAllByRole("group", {
      name: "test_tool",
    })[0]!;
    const focus = vi.spyOn(sourceRegion, "focus");
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    act(() => latestDock!.openBrowserTab());
    act(() => latestDock!.closeTab("toolUi:call-one"));
    expect(sourceRegion.isConnected).toBe(true);
    expect(focus).not.toHaveBeenCalled();
  });

  it("invalidates a retained host immediately when auth changes", () => {
    mount();
    const sourceRegion = screen.getAllByRole("group", {
      name: "test_tool",
    })[0]!;
    const focus = vi.spyOn(sourceRegion, "focus");
    const host = latestHost!;
    act(() => {
      invalidateAuthSession();
      expect(() => host.getToolContext()).toThrow(/inactive/i);
      expect(() => host.request("/api/example")).toThrow(/inactive/i);
    });
    expect(focus).not.toHaveBeenCalled();
  });

  it("blocks tool actions when the real chat target is pending", () => {
    mount({ threadId: null });
    expect(() =>
      latestHost!.openSidePanel({ callId: toolData.callId }),
    ).toThrow(/inactive/i);
  });

  it("blocks a nonempty production pending thread target", () => {
    mount({ threadId: "__pending__" });
    expect(() => latestHost!.getToolContext()).toThrow(/inactive/i);
  });

  it("blocks a thread registered in the real pending-thread set", () => {
    markPendingThread("thread-being-created");
    mount({ threadId: "thread-being-created" });
    expect(isPendingThread("thread-being-created")).toBe(true);
    expect(() => latestHost!.getToolContext()).toThrow(/inactive/i);
    clearPendingThread("thread-being-created");
  });

  it("rebinds the same raw thread target when pending creation becomes ready", async () => {
    const threadId = "thread-pending-ready";
    markPendingThread(threadId);
    const view = mount({ threadId });
    const pendingHost = latestHost!;
    expect(() => pendingHost.getToolContext()).toThrow(/inactive/i);

    clearPendingThread(threadId);
    view.rerender(<Harness threadId={threadId} />);
    const readyHost = latestHost!;
    expect(readyHost).not.toBe(pendingHost);
    expect(readyHost.getToolContext()).toMatchObject({
      agentId: "chat-agent",
      threadId,
    });
    await expect(readyHost.request("/api/ready-target")).resolves.toEqual({
      ok: true,
    });
    expect(() => pendingHost.getToolContext()).toThrow(/inactive/i);
    clearPendingThread(threadId);
  });

  it("unmounts the tool reader and invalidates its Host in the same tab-switch call", () => {
    mount();
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    const staleHost = latestHost!;
    fireEvent.click(screen.getByRole("button", { name: "open-browser" }));
    expect(() => staleHost.openSidePanel({ callId: toolData.callId })).toThrow(
      /inactive/i,
    );
    expect(screen.queryByTestId("renderer-side-panel")).not.toBeInTheDocument();
    expect(screen.getByTestId("renderer-inline")).toBeInTheDocument();
    expect(document.activeElement).not.toHaveAttribute(
      "aria-label",
      "test_tool",
    );
  });

  it("revokes every inline Host operation synchronously when re-entering its tool tab", () => {
    mount();
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "open-browser" }));
    const inlineHost = hostsByPresentation.get("call-one:inline")!;
    const outcomes: string[] = [];
    act(() => {
      latestDock!.setActiveTab("toolUi:call-one");
      for (const operation of [
        () => inlineHost.getToolContext(),
        () => inlineHost.request("/api/inline"),
        () => inlineHost.patchResult("call-one", { stale: true }),
        () => inlineHost.openSidePanel({ callId: "call-one" }),
        () => inlineHost.closeSidePanel("call-one"),
      ]) {
        try {
          const result = operation();
          if (result instanceof Promise) void result.catch(() => undefined);
          outcomes.push("allowed");
        } catch {
          outcomes.push("inactive");
        }
      }
    });
    expect(outcomes).toEqual([
      "inactive",
      "inactive",
      "inactive",
      "inactive",
      "inactive",
    ]);
  });

  it("rejects the old Host before commit after open, replace, close and tab switch", () => {
    mount({ productionPanel: true });
    const capture = (host: ToolRenderProps["host"]) => {
      const results: string[] = [];
      for (const operation of [
        () => host.getToolContext(),
        () => host.request("/api/precommit"),
        () => host.patchResult("call-one", { stale: true }),
        () => host.openSidePanel({ callId: "call-one" }),
        () => host.closeSidePanel("call-one"),
      ]) {
        try {
          const result = operation();
          if (result instanceof Promise) void result.catch(() => undefined);
          results.push("allowed");
        } catch {
          results.push("inactive");
        }
      }
      return results;
    };
    const everyOperationInactive = [
      "inactive",
      "inactive",
      "inactive",
      "inactive",
      "inactive",
    ];

    const inlineHost = latestHost!;
    let afterOpen: string[] = [];
    act(() => {
      inlineHost.openSidePanel({
        callId: "call-one",
        state: { selected: "first" },
      });
      afterOpen = capture(inlineHost);
    });
    expect(afterOpen).toEqual(everyOperationInactive);

    const firstReader = hostsByPresentation.get("call-one:side-panel")!;
    let afterReplace: string[] = [];
    act(() => {
      firstReader.openSidePanel({
        callId: "call-one",
        state: { selected: "second" },
      });
      afterReplace = capture(firstReader);
    });
    expect(afterReplace).toEqual(everyOperationInactive);

    const secondReader = hostsByPresentation.get("call-one:side-panel")!;
    let afterClose: string[] = [];
    act(() => {
      secondReader.closeSidePanel("call-one");
      afterClose = capture(secondReader);
    });
    expect(afterClose).toEqual(everyOperationInactive);

    act(() => latestDock!.openToolUiTab({ callId: "call-one" }));
    const thirdReader = hostsByPresentation.get("call-one:side-panel")!;
    let afterSwitch: string[] = [];
    act(() => {
      latestDock!.openBrowserTab();
      afterSwitch = capture(thirdReader);
    });
    expect(afterSwitch).toEqual(everyOperationInactive);
  });

  it("creates an operable new activation when closing a tab falls back to a tool reader", () => {
    mount();
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    const originalHost = hostsByPresentation.get("call-one:side-panel")!;
    fireEvent.click(screen.getByRole("button", { name: "open-browser" }));
    act(() => latestDock!.closeTab("browser"));
    const fallback = latestDock!.activeTab;
    expect(fallback?.kind).toBe("toolUi");
    expect(fallback?.kind === "toolUi" && fallback.activation.active).toBe(
      true,
    );
    const newHost = hostsByPresentation.get("call-one:side-panel")!;
    expect(newHost).not.toBe(originalHost);
    expect(newHost.getToolContext()).toMatchObject({ threadId: "thread-one" });
    expect(() => originalHost.getToolContext()).toThrow(/inactive/i);
  });

  it("creates a fresh reader activation when closing tool B falls back to tool A", () => {
    mount({ multiple: true });
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    const originalA = latestDock!.activeTab;
    act(() =>
      latestDock!.openToolUiTab({
        callId: "call-two",
        pluginId: "dock-test",
        state: { selected: "second-call" },
      }),
    );
    const originalBHost = hostsByPresentation.get("call-two:side-panel")!;
    act(() => latestDock!.closeTab("toolUi:call-two"));
    expect(latestDock!.activeTab?.kind).toBe("toolUi");
    expect(
      latestDock!.activeTab?.kind === "toolUi" && latestDock!.activeTab.callId,
    ).toBe("call-one");
    expect(
      latestDock!.activeTab?.kind === "toolUi" &&
        latestDock!.activeTab.activation.active,
    ).toBe(true);
    const fallbackHost = hostsByPresentation.get("call-one:side-panel")!;
    expect(fallbackHost.getToolContext()).toMatchObject({
      threadId: "thread-one",
    });
    expect(() => originalBHost.getToolContext()).toThrow(/inactive/i);
    expect(originalA?.kind === "toolUi" && originalA.activation.active).toBe(
      false,
    );
  });

  it("keeps two tool calls isolated and leaves a generic open without plugin state", () => {
    mount({ multiple: true });
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-two-first" }),
    );
    expect(screen.getByTestId("renderer-side-panel")).toBeInTheDocument();
    expect(screen.getByText('{"selected":"first"}')).toBeInTheDocument();
    expect(() =>
      hostsByPresentation
        .get("call-two:side-panel")!
        .openSidePanel({ callId: "call-one" }),
    ).toThrow(/inactive/i);

    fireEvent.click(screen.getByRole("button", { name: "open-generic" }));
    const genericReader = screen.getByTestId("renderer-side-panel");
    expect(genericReader).toBeInTheDocument();
    expect(genericReader.querySelector("output")).toHaveTextContent("null");
  });

  it("keeps plugin renderer props.host generic open without state", () => {
    mount();
    fireEvent.click(screen.getByRole("button", { name: "generic-open" }));
    expect(screen.getByTestId("renderer-side-panel")).toBeInTheDocument();
    expect(
      screen.getByTestId("renderer-side-panel").querySelector("output"),
    ).toHaveTextContent("null");
    expect(latestDock!.activeTab).toMatchObject({
      kind: "toolUi",
      pluginId: "dock-test",
    });
  });

  it("opens from the production dock button with trusted target and returns focus to its source", () => {
    mount({ productionPanel: true });
    const sourceRegion = screen.getAllByRole("group", {
      name: "test_tool",
    })[0]!;
    const focus = vi.spyOn(sourceRegion, "focus");
    fireEvent.click(
      screen.getByRole("button", { name: "chat.openToolUiInDock" }),
    );

    const opened = latestDock!.openTabs.find(
      (tab) => tab.kind === "toolUi" && tab.callId === "call-one",
    );
    expect(opened).toMatchObject({
      kind: "toolUi",
      callId: "call-one",
      pluginId: "dock-test",
    });
    expect(opened && "state" in opened).toBe(false);
    const readerHost = hostsByPresentation.get("call-one:side-panel")!;
    expect(readerHost.getToolContext()).toMatchObject({
      agentId: "chat-agent",
      threadId: "thread-one",
    });
    expect(screen.getAllByRole("group", { name: "test_tool" })[0]).toBe(
      sourceRegion,
    );
    expect(screen.getByTestId("renderer-side-panel")).toBeInTheDocument();

    act(() => latestDock!.handleClose());
    expect(focus).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(sourceRegion);
  });

  it("patches the real target store row through the mounted Host and rejects a missing target", () => {
    const storeTool = {
      ...toolMessage,
      toolData: {
        ...toolData,
        output: JSON.stringify({
          octop_ui: { renderer: "dock-renderer", version: 1 },
          data: { value: "before" },
        }),
      },
    };
    setMessages("thread-one", [storeTool]);
    setMessages("thread-foreign", [
      {
        ...storeTool,
        id: "foreign-tool",
        toolData: {
          ...storeTool.toolData,
          output: JSON.stringify({ data: { value: "foreign" } }),
        },
      },
    ]);
    setFocusedChatSession("thread-foreign");
    mount();
    const host = latestHost!;
    host.patchResult("call-one", { value: "target" });
    expect(
      JSON.parse(getSnapshot("thread-one").messages[0].toolData!.output!).data,
    ).toEqual({ value: "target" });
    expect(
      JSON.parse(getSnapshot("thread-foreign").messages[0].toolData!.output!)
        .data,
    ).toEqual({ value: "foreign" });

    setMessages("thread-one", []);
    expect(() => host.patchResult("call-one", { value: "wrong" })).toThrow(
      "Plugin UI host is inactive",
    );
    expect(
      JSON.parse(getSnapshot("thread-foreign").messages[0].toolData!.output!)
        .data,
    ).toEqual({ value: "foreign" });
  });

  it("drops prior owner's state and focus when registry resolution changes owner", async () => {
    toolData.output = "plain";
    mount();
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    const sourceRegion = screen.getAllByRole("group", {
      name: "test_tool",
    })[0]!;
    const focus = vi.spyOn(sourceRegion, "focus");
    const oldHost = hostsByPresentation.get("call-one:side-panel")!;
    let resolveSuccess!: (value: unknown) => void;
    let rejectFailure!: (reason: Error) => void;
    vi.mocked(request)
      .mockImplementationOnce(
        () => new Promise((resolve) => (resolveSuccess = resolve)) as never,
      )
      .mockImplementationOnce(
        () =>
          new Promise((_resolve, reject) => (rejectFailure = reject)) as never,
      );
    const staleSuccess = oldHost.request("/api/owner-success");
    const staleFailure = oldHost.request("/api/owner-failure");

    act(() => {
      registerToolRenderer({
        id: "other-owner-renderer",
        pluginId: "other-owner",
        tools: ["test_tool"],
        component: DockRenderer,
      });
    });

    expect(() => oldHost.getToolContext()).toThrow(/inactive/i);
    expect(rendererPresentations).toContainEqual({
      pluginId: "other-owner",
      location: "side-panel",
      state: null,
    });
    const replacementHost = hostsByPresentation.get("call-one:side-panel")!;
    expect(() => replacementHost.getToolContext()).toThrow(/inactive/i);
    expect(latestDock!.openTabs.some((tab) => tab.kind === "toolUi")).toBe(
      false,
    );
    expect(focus).not.toHaveBeenCalled();
    resolveSuccess({ stale: true });
    rejectFailure(new Error("upstream failure"));
    await expect(staleSuccess).rejects.toThrow("Plugin UI host is inactive");
    await expect(staleFailure).rejects.toThrow("Plugin UI host is inactive");
  });

  it("discards a tool selection and focus when its renderer is unregistered", async () => {
    toolData.output = "plain";
    mount();
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    const sourceRegion = screen.getAllByRole("group", {
      name: "test_tool",
    })[0]!;
    const focus = vi.spyOn(sourceRegion, "focus");
    const oldHost = hostsByPresentation.get("call-one:side-panel")!;
    let resolveSuccess!: (value: unknown) => void;
    let rejectFailure!: (reason: Error) => void;
    vi.mocked(request)
      .mockImplementationOnce(
        () => new Promise((resolve) => (resolveSuccess = resolve)) as never,
      )
      .mockImplementationOnce(
        () =>
          new Promise((_resolve, reject) => (rejectFailure = reject)) as never,
      );
    const staleSuccess = oldHost.request("/api/unregister-success");
    const staleFailure = oldHost.request("/api/unregister-failure");
    act(() => unregisterPluginRenderers("dock-test"));
    expect(() => oldHost.getToolContext()).toThrow(/inactive/i);
    expect(screen.queryByTestId("renderer-side-panel")).not.toBeInTheDocument();
    expect(focus).not.toHaveBeenCalled();
    resolveSuccess({ stale: true });
    rejectFailure(new Error("upstream failure"));
    await expect(staleSuccess).rejects.toThrow("Plugin UI host is inactive");
    await expect(staleFailure).rejects.toThrow("Plugin UI host is inactive");
  });

  it("closes every background tool selection when the owner is unregistered in one batch", () => {
    toolData.output = "plain";
    mount({ multiple: true });
    const sourceRegions = screen.getAllByRole("group", { name: "test_tool" });
    const focus = sourceRegions.map((region) => vi.spyOn(region, "focus"));
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-two-first" }),
    );
    act(() => latestDock!.openBrowserTab());

    act(() => unregisterPluginRenderers("dock-test"));

    expect(
      latestDock!.openTabs.filter((tab) => tab.kind === "toolUi"),
    ).toHaveLength(0);
    expect(latestDock!.openTabs).toContainEqual(
      expect.objectContaining({ id: "browser", kind: "browser" }),
    );
    expect(focus.every((spy) => spy.mock.calls.length === 0)).toBe(true);
  });

  it("drops the prior selection when the same call is reused in a new thread", () => {
    const view = mount();
    const sourceRegion = screen.getAllByRole("group", {
      name: "test_tool",
    })[0]!;
    const focus = vi.spyOn(sourceRegion, "focus");
    fireEvent.click(
      screen.getByRole("button", { name: "select-call-one-first" }),
    );
    const staleHost = latestHost!;
    view.rerender(<Harness threadId="thread-two" />);
    expect(() => staleHost.openSidePanel({ callId: toolData.callId })).toThrow(
      /inactive/i,
    );
    expect(screen.getByTestId("renderer-inline")).toBeInTheDocument();
    expect(screen.queryByText('{"selected":"first"}')).not.toBeInTheDocument();
    expect(document.activeElement).not.toHaveAttribute(
      "aria-label",
      "test_tool",
    );
    expect(focus).not.toHaveBeenCalled();
  });

  it("invalidates pending renderer Host requests when its actual caller unmounts", async () => {
    const view = mount();
    const host = latestHost!;
    let resolveSuccess!: (value: unknown) => void;
    let rejectFailure!: (reason: Error) => void;
    vi.mocked(request)
      .mockImplementationOnce(
        () => new Promise((resolve) => (resolveSuccess = resolve)) as never,
      )
      .mockImplementationOnce(
        () =>
          new Promise((_resolve, reject) => (rejectFailure = reject)) as never,
      );
    const staleSuccess = host.request("/api/unmount-success");
    const staleFailure = host.request("/api/unmount-failure");
    view.unmount();
    expect(() => host.getToolContext()).toThrow(/inactive/i);
    resolveSuccess({ stale: true });
    rejectFailure(new Error("upstream failure"));
    await expect(staleSuccess).rejects.toThrow("Plugin UI host is inactive");
    await expect(staleFailure).rejects.toThrow("Plugin UI host is inactive");
  });
});
