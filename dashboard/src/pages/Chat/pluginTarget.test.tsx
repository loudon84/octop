import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { pluginsApi } from "../../api/modules/plugins";
import { getAuthSessionEpoch } from "../../api/authSession";
import { registerPluginSurface } from "../../plugins/toolRenderers/registry";
import * as chatStore from "./hooks/chatStore";
import { useChat } from "./hooks/useChat";
import { useChatMessageQueue } from "./hooks/useChatMessageQueue";
import ChatQueuedMessages from "./components/ChatQueuedMessages";
import {
  ChatComposerTools,
  dispatchPreparedQueueItem,
  reclaimQueueItemIfCurrent,
  requestBoundQueueItemFlush,
} from "./index";

vi.mock("../../api/modules/plugins", () => ({
  pluginsApi: {
    list: vi.fn(),
    requirements: vi.fn(),
  },
}));
vi.mock("../../plugins/toolRenderers/loader", () => ({
  ensureBuiltinToolRenderers: vi.fn(),
  loadInstalledPluginUis: vi.fn(),
  isPluginUiLoaded: vi.fn(() => false),
}));
vi.mock("../../plugins/toolRenderers/toolPluginIndex", () => ({
  updateToolPluginIndex: vi.fn(),
}));
vi.mock("../../plugins/toolRenderers/PluginSurface", () => ({
  PluginSurface: (props: {
    context: { agentId: string | null; threadId: string | null };
  }) => (
    <div
      data-testid="composer-context"
      data-agent={props.context.agentId ?? ""}
      data-thread={props.context.threadId ?? ""}
    />
  ),
}));
vi.mock("../../components/DocumentPreviewCore", () => ({
  default: () => null,
}));
vi.mock("../../components/PdfDocumentPreview", () => ({
  default: () => null,
  PdfViewerSkeleton: () => null,
}));

const list = vi.mocked(pluginsApi.list);
const requirements = vi.mocked(pluginsApi.requirements);

describe("Chat composer plugin target", () => {
  afterEach(() => vi.clearAllMocks());

  it("uses the active Agent for readiness, plugin context and the native input when the route has no Agent", async () => {
    list.mockResolvedValue([]);
    requirements.mockResolvedValue({
      api_version: 1,
      required_plugins: { policy: "1" },
      unavailable_plugins: ["policy"],
    });
    const renderNative = ({ agentId }: { agentId: string | null }) => (
      <input
        aria-label="native chat input"
        data-agent={agentId ?? ""}
        defaultValue="prefilled prompt"
      />
    );
    const view = render(
      <ChatComposerTools
        routeAgentId={null}
        activeAgentId={null}
        threadId="__pending__"
        locale="en"
        theme="light"
        timeZone="Asia/Shanghai"
      >
        {renderNative}
      </ChatComposerTools>,
    );
    expect(screen.getByRole("status")).toBeInTheDocument();

    view.rerender(
      <ChatComposerTools
        routeAgentId={null}
        activeAgentId="agent-active"
        threadId="__pending__"
        locale="en"
        theme="light"
        timeZone="Asia/Shanghai"
      >
        {renderNative}
      </ChatComposerTools>,
    );
    await waitFor(() =>
      expect(requirements).toHaveBeenCalledWith("agent-active", null),
    );
    expect(screen.getByTestId("composer-context")).toHaveAttribute(
      "data-thread",
      "",
    );
    expect(screen.getByLabelText("native chat input")).toHaveAttribute(
      "data-agent",
      "agent-active",
    );
    expect(screen.getByTestId("composer-context")).toHaveAttribute(
      "data-agent",
      "agent-active",
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByLabelText("native chat input")).toHaveValue(
      "prefilled prompt",
    );
  });
});

it("keeps a history edit queued when its target becomes busy during readiness", async () => {
  const threadId = "p08c-r2-edit-streaming";
  const agentId = "agent-r2";
  const target = { agentId, threadId };
  class DeferredSocket {
    static instances: DeferredSocket[] = [];
    readyState = 0;
    onopen: (() => void) | null = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    onerror: (() => void) | null = null;
    onclose: (() => void) | null = null;
    sent: string[] = [];
    constructor(_url: string) {
      DeferredSocket.instances.push(this);
    }
    send(value: string) {
      this.sent.push(value);
    }
    close() {
      this.readyState = 3;
      this.onclose?.();
    }
  }
  vi.stubGlobal("WebSocket", DeferredSocket);
  chatStore.setMessages(threadId, [
    {
      id: "edit-me",
      role: "user",
      content: "original history",
      status: "done",
      timestamp: 1,
    },
  ]);
  const chat = renderHook(() => useChat(threadId, agentId));
  let releaseReadiness!: (value: {
    status: "ready";
    requiredPlugins: Record<string, string>;
    unavailablePlugins: string[];
  }) => void;
  const readiness = new Promise<{
    status: "ready";
    requiredPlugins: Record<string, string>;
    unavailablePlugins: string[];
  }>((resolve) => {
    releaseReadiness = resolve;
  });
  const refresh = vi.fn(() => readiness);
  const queue = renderHook(() =>
    useChatMessageQueue({
      agentId,
      threadId,
      isStreaming: false,
      onFlush: (item) =>
        dispatchPreparedQueueItem(
          item,
          target,
          refresh,
          chat.result.current.sendMessage,
          chat.result.current.editAndResend,
        ),
      subscribeStreamEnd: () => () => undefined,
      isThreadStreaming: (id) => chatStore.getSnapshot(id).isStreaming,
    }),
  );
  let acceptedId = "";
  act(() => {
    const accepted = queue.result.current.enqueueForTarget(target, {
      text: "edited text",
      editMessageId: "edit-me",
      bound: true,
    });
    if (accepted.status !== "accepted")
      throw new Error("queue rejected fixture");
    acceptedId = accepted.acceptedId;
  });
  act(() => queue.result.current.requestFlush(target));
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());

  let activeTurn!: Promise<void>;
  act(() => {
    activeTurn = chatStore.sendTurn(
      threadId,
      "older active turn",
      agentId,
      "",
      undefined,
      undefined,
      undefined,
      threadId,
    );
  });
  const originalSocket = DeferredSocket.instances[0]!;
  act(() => originalSocket.onopen?.());
  await waitFor(() =>
    expect(chatStore.getSnapshot(threadId).isStreaming).toBe(true),
  );

  act(() =>
    releaseReadiness({
      status: "ready",
      requiredPlugins: {},
      unavailablePlugins: [],
    }),
  );
  await act(async () => new Promise((resolve) => setTimeout(resolve, 25)));

  expect(chatStore.getSnapshot(threadId).messages[0]?.content).toBe(
    "original history",
  );
  expect(chatStore.getSnapshot(threadId).isStreaming).toBe(true);
  expect(DeferredSocket.instances).toHaveLength(1);
  expect(originalSocket.sent.some((frame) => frame.includes('"cancel"'))).toBe(
    false,
  );
  expect(queue.result.current.allItems).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        id: acceptedId,
        status: "blocked",
        editMessageId: "edit-me",
      }),
    ]),
  );

  await act(async () => {
    chatStore.cancelStream(threadId);
    await activeTurn;
  });
  queue.unmount();
  chat.unmount();
  chatStore.removeSession(threadId);
  vi.unstubAllGlobals();
});

it("keeps a history edit queued when its target enters HITL during readiness", async () => {
  const threadId = "p08c-r2-edit-hitl";
  const agentId = "agent-r2";
  const target = { agentId, threadId };
  class DeferredSocket {
    static instances: DeferredSocket[] = [];
    readyState = 0;
    onopen: (() => void) | null = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    onerror: (() => void) | null = null;
    onclose: (() => void) | null = null;
    sent: string[] = [];
    constructor(_url: string) {
      DeferredSocket.instances.push(this);
    }
    send(value: string) {
      this.sent.push(value);
    }
    close() {
      this.readyState = 3;
      this.onclose?.();
    }
  }
  vi.stubGlobal("WebSocket", DeferredSocket);
  chatStore.setMessages(threadId, [
    {
      id: "edit-me-hitl",
      role: "user",
      content: "original history",
      status: "done",
      timestamp: 1,
    },
  ]);
  const chat = renderHook(() => useChat(threadId, agentId));
  let releaseReadiness!: (value: {
    status: "ready";
    requiredPlugins: Record<string, string>;
    unavailablePlugins: string[];
  }) => void;
  const readiness = new Promise<{
    status: "ready";
    requiredPlugins: Record<string, string>;
    unavailablePlugins: string[];
  }>((resolve) => {
    releaseReadiness = resolve;
  });
  const refresh = vi.fn(() => readiness);
  const queue = renderHook(() =>
    useChatMessageQueue({
      agentId,
      threadId,
      isStreaming: false,
      onFlush: (item) =>
        dispatchPreparedQueueItem(
          item,
          target,
          refresh,
          chat.result.current.sendMessage,
          chat.result.current.editAndResend,
        ),
      subscribeStreamEnd: () => () => undefined,
      isThreadStreaming: (id) => chatStore.getSnapshot(id).isStreaming,
    }),
  );
  let acceptedId = "";
  act(() => {
    const accepted = queue.result.current.enqueueForTarget(target, {
      text: "edited text",
      editMessageId: "edit-me-hitl",
      bound: true,
    });
    if (accepted.status !== "accepted")
      throw new Error("queue rejected fixture");
    acceptedId = accepted.acceptedId;
  });
  act(() => queue.result.current.requestFlush(target));
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());

  act(() =>
    chatStore.setMessages(threadId, [
      ...chatStore.getSnapshot(threadId).messages,
      {
        id: "pending-hitl",
        role: "assistant",
        content: "approval required",
        status: "done",
        timestamp: 2,
        hitlData: {
          status: "pending",
          pending_id: "pending-1",
          action_requests: [{ name: "tool", args: {} }],
        },
      },
    ]),
  );
  act(() =>
    releaseReadiness({
      status: "ready",
      requiredPlugins: {},
      unavailablePlugins: [],
    }),
  );
  await act(async () => new Promise((resolve) => setTimeout(resolve, 25)));

  expect(chatStore.getSnapshot(threadId).messages).toMatchObject([
    { id: "edit-me-hitl", content: "original history" },
    { id: "pending-hitl", hitlData: { pending_id: "pending-1" } },
  ]);
  expect(DeferredSocket.instances).toHaveLength(0);
  expect(queue.result.current.allItems).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ id: acceptedId, status: "blocked" }),
    ]),
  );

  queue.unmount();
  chat.unmount();
  chatStore.cancelStream(threadId);
  chatStore.removeSession(threadId);
  vi.unstubAllGlobals();
});

it("defers a background team target with live members while another chat is visible", async () => {
  const teamThread = "p08c-r2-background-team";
  const hostAgent = "team-host";
  class TeamSocket {
    static instance: TeamSocket | null = null;
    readyState = 0;
    onopen: (() => void) | null = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    onerror: (() => void) | null = null;
    onclose: (() => void) | null = null;
    sent: string[] = [];
    constructor(_url: string) {
      TeamSocket.instance = this;
    }
    send(value: string) {
      this.sent.push(value);
    }
    close() {
      this.readyState = 3;
      this.onclose?.();
    }
  }
  vi.stubGlobal("WebSocket", TeamSocket);
  const team = renderHook(() => useChat(teamThread, hostAgent, true));
  const visibleChat = renderHook(() =>
    useChat("p08c-r2-visible-chat", "agent-other"),
  );
  let activeTurn!: Promise<void>;
  act(() => {
    activeTurn = chatStore.sendTurn(
      teamThread,
      "team host turn",
      hostAgent,
      "",
      undefined,
      undefined,
      undefined,
      teamThread,
    );
  });
  const socket = TeamSocket.instance!;
  act(() => {
    socket.readyState = 1;
    socket.onopen?.();
    socket.onmessage?.({
      data: JSON.stringify({
        type: "token",
        node: "agent",
        thread_id: teamThread,
        agent_id: "team-member",
        content: "member still working",
      }),
    });
    socket.onmessage?.({
      data: JSON.stringify({ type: "turn.ended", thread_id: teamThread }),
    });
  });
  await act(async () => activeTurn);
  expect(chatStore.getSnapshot(teamThread).isStreaming).toBe(false);
  expect(chatStore.getSnapshot(teamThread).liveSpeakers).toContain(
    "team-member",
  );

  const target = { agentId: hostAgent, threadId: teamThread };
  const onFlush = vi.fn(async () => ({ status: "accepted" as const }));
  const queue = renderHook(() =>
    useChatMessageQueue({
      agentId: hostAgent,
      threadId: teamThread,
      isStreaming: false,
      onFlush,
    }),
  );
  let acceptedId = "";
  act(() => {
    const accepted = queue.result.current.enqueueForTarget(target, {
      text: "follow-up while member is live",
      bound: true,
    });
    if (accepted.status !== "accepted")
      throw new Error("queue rejected fixture");
    acceptedId = accepted.acceptedId;
  });
  await act(async () => {
    queue.result.current.requestFlush(target);
    await new Promise((resolve) => setTimeout(resolve, 25));
  });

  expect(onFlush).not.toHaveBeenCalled();
  expect(queue.result.current.items).toEqual([
    expect.objectContaining({
      id: acceptedId,
      status: "queued",
    }),
  ]);

  queue.unmount();
  visibleChat.unmount();
  team.unmount();
  chatStore.cancelStream(teamThread);
  chatStore.removeSession(teamThread);
  chatStore.removeSession("p08c-r2-visible-chat");
  vi.unstubAllGlobals();
});

it("routes bound chip actions to the target queue instead of direct steering", () => {
  const requestFlush = vi.fn();
  const item = {
    bound: true,
    target: { agentId: "agent-queued", threadId: "thread-queued" },
  };

  expect(requestBoundQueueItemFlush(true, item.target, requestFlush)).toBe(
    true,
  );
  expect(requestFlush).toHaveBeenCalledExactlyOnceWith(item.target);
  expect(requestBoundQueueItemFlush(false, item.target, requestFlush)).toBe(
    false,
  );
  expect(requestFlush).toHaveBeenCalledOnce();
});

it("opens a first-thread alias and enables recovery only at its real target", () => {
  const onOpenTarget = vi.fn();
  const onReclaim = vi.fn();
  const onRemove = vi.fn();
  const onSteer = vi.fn();
  const item = {
    id: "queued-first",
    text: "prepared first message",
    target: { agentId: "agent-real", threadId: "thread-real" },
    displayOrigin: { agentId: "agent-real", threadId: null },
    status: "uncertain" as const,
    bound: true,
    authEpoch: 1,
    createdAt: 1,
  };
  const view = render(
    <ChatQueuedMessages
      items={[item]}
      displayTarget={{ agentId: "agent-real", threadId: null }}
      onRemove={onRemove}
      onReclaim={onReclaim}
      onSteer={onSteer}
      onOpenTarget={onOpenTarget}
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: "Open thread-real" }));
  expect(onOpenTarget).toHaveBeenCalledExactlyOnceWith({
    agentId: "agent-real",
    threadId: "thread-real",
  });
  expect(
    screen.getByRole("button", { name: "chat.queue.reclaim" }),
  ).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "chat.queue.remove" }),
  ).toBeDisabled();
  expect(onReclaim).not.toHaveBeenCalled();
  expect(onRemove).not.toHaveBeenCalled();
  expect(onSteer).not.toHaveBeenCalled();

  view.rerender(
    <ChatQueuedMessages
      items={[item]}
      displayTarget={{ agentId: "agent-real", threadId: "thread-real" }}
      onRemove={onRemove}
      onReclaim={onReclaim}
      onSteer={onSteer}
      onOpenTarget={onOpenTarget}
    />,
  );

  expect(
    screen.getByRole("button", { name: "chat.queue.reclaim" }),
  ).toBeEnabled();
  expect(
    screen.getByRole("button", { name: "chat.queue.remove" }),
  ).toBeEnabled();
  expect(screen.queryByRole("button", { name: "Open thread-real" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "chat.queue.reclaim" }));
  fireEvent.click(screen.getByRole("button", { name: "chat.queue.remove" }));
  expect(onReclaim).toHaveBeenCalledExactlyOnceWith(item.id);
  expect(onRemove).toHaveBeenCalledExactlyOnceWith(item.id);
  expect(onOpenTarget).toHaveBeenCalledOnce();
});

it("refreshes the queued target and dispatches its complete native snapshot", async () => {
  const unregister = registerPluginSurface("plugin-a", "composer", () => null);
  const item = {
    id: "queue-native",
    text: "prepared message",
    target: { agentId: "agent-queued", threadId: "thread-queued" },
    authEpoch: getAuthSessionEpoch(),
    bound: true,
    createdAt: 1,
    status: "sending" as const,
    pluginContext: { "plugin-a": { operation_id: "op-1" } },
    nativeOptions: {
      modelRef: "model-queued",
      connectors: ["connector-queued"],
      knowledgeBaseIds: ["kb-queued"],
      targetAgentIds: ["expert-queued"],
      reasoningMode: "enabled" as const,
      reasoningEffort: "high",
      conversationMode: "plan" as const,
      hitlPolicy: { mode: "allow_tools" as const, tools: ["tool-queued"] },
    },
  } as never;
  const refresh = vi.fn(async () => ({
    status: "ready" as const,
    requiredPlugins: { "plugin-a": "1" },
    unavailablePlugins: [],
  }));
  const send = vi.fn((...args: unknown[]) => {
    (args[14] as { onOutcome: (value: "complete") => void }).onOutcome(
      "complete",
    );
  });

  try {
    await expect(
      dispatchPreparedQueueItem(
        item,
        item.target,
        refresh,
        send as never,
        vi.fn() as never,
      ),
    ).resolves.toEqual({ status: "accepted" });
  } finally {
    unregister();
  }

  expect(refresh).toHaveBeenCalledExactlyOnceWith(item.target);
  expect(send.mock.calls[0]).toEqual([
    "prepared message",
    "",
    "agent-queued",
    undefined,
    "thread-queued",
    "model-queued",
    ["connector-queued"],
    ["kb-queued"],
    ["expert-queued"],
    undefined,
    "enabled",
    "high",
    "plan",
    { mode: "allow_tools", tools: ["tool-queued"] },
    expect.objectContaining({ delivery: "follow_up" }),
  ]);
});

it("blocks a registered required composer without its prepared context", async () => {
  const unregister = registerPluginSurface("plugin-a", "composer", () => null);
  const item = {
    id: "queue-missing-context",
    text: "not ready",
    target: { agentId: "agent-queued", threadId: "thread-queued" },
    authEpoch: getAuthSessionEpoch(),
    status: "sending",
    bound: true,
  } as never;
  const send = vi.fn();
  try {
    await expect(
      dispatchPreparedQueueItem(
        item,
        item.target,
        vi.fn(async () => ({
          status: "ready" as const,
          requiredPlugins: { "plugin-a": "1" },
          unavailablePlugins: [],
        })),
        send as never,
        vi.fn() as never,
      ),
    ).resolves.toEqual({ status: "rejected", reason: "blocked" });
  } finally {
    unregister();
  }
  expect(send).not.toHaveBeenCalled();
});

it("allows a required headless plugin without a mounted composer surface", async () => {
  const item = {
    id: "queue-headless",
    text: "headless context",
    target: { agentId: "agent-queued", threadId: "thread-queued" },
    authEpoch: getAuthSessionEpoch(),
    status: "sending",
    bound: true,
  } as never;
  const send = vi.fn((...args: unknown[]) => {
    (args[14] as { onOutcome: (value: "complete") => void }).onOutcome(
      "complete",
    );
  });
  await expect(
    dispatchPreparedQueueItem(
      item,
      item.target,
      vi.fn(async () => ({
        status: "ready" as const,
        requiredPlugins: { "plugin-headless": "1" },
        unavailablePlugins: [],
      })),
      send as never,
      vi.fn() as never,
    ),
  ).resolves.toEqual({ status: "accepted" });
  expect(send).toHaveBeenCalledOnce();
});

it("retains the queued owner when selection restore or the current commit fails", async () => {
  const item = {
    id: "queue-reclaim",
    text: "recover me",
    target: { agentId: "agent-1", threadId: "thread-1" },
    authEpoch: getAuthSessionEpoch(),
    status: "blocked" as const,
    pluginSelection: [
      { pluginId: "plugin-a", generation: "old", selection: {} },
    ],
  } as never;
  const getById = vi.fn(() => item);
  const remove = vi.fn();
  const restore = vi
    .fn()
    .mockRejectedValueOnce(new Error("restore failed"))
    .mockResolvedValue(undefined);
  const deps = {
    getById,
    getOrigin: () => ({ agentId: "agent-1", threadId: "thread-1" }),
    restore,
    commit: vi.fn(() => false),
    remove,
  };

  await expect(reclaimQueueItemIfCurrent("queue-reclaim", deps)).resolves.toBe(
    false,
  );
  await expect(reclaimQueueItemIfCurrent("queue-reclaim", deps)).resolves.toBe(
    false,
  );
  expect(restore).toHaveBeenCalledTimes(2);
  expect(remove).not.toHaveBeenCalled();
});

it("retains the item if its target changes while async selection restore is pending", async () => {
  const item = {
    id: "queue-reclaim-race",
    text: "recover me",
    target: { agentId: "agent-1", threadId: "thread-1" },
    authEpoch: getAuthSessionEpoch(),
    status: "blocked" as const,
    pluginSelection: [
      { pluginId: "plugin-a", generation: "old", selection: {} },
    ],
  } as never;
  let origin = { agentId: "agent-1", threadId: "thread-1" };
  let finishRestore!: () => void;
  const remove = vi.fn();
  const pending = reclaimQueueItemIfCurrent("queue-reclaim-race", {
    getById: () => item,
    getOrigin: () => origin,
    restore: () =>
      new Promise<void>((resolve) => {
        finishRestore = resolve;
      }),
    commit: vi.fn(() => true),
    remove,
  });
  origin = { agentId: "agent-2", threadId: "thread-2" };
  finishRestore();

  await expect(pending).resolves.toBe(false);
  expect(remove).not.toHaveBeenCalled();
});
