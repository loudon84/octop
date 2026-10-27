import * as React from "react";
import { jsx, jsxs } from "react/jsx-runtime";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPluginUiRegistrationHost } from "../../plugins/toolRenderers/host";
import { PluginSurface } from "../../plugins/toolRenderers/PluginSurface";
import type { PluginSurfaceContext } from "../../plugins/toolRenderers/types";
import { setup as setupEnterpriseKnowledge } from "../../../../plugins/enterprise-knowledge/ui/src/index";
import ChatInput from "./components/ChatInput";
import { dispatchPreparedQueueItem, routeGuardedChatSend } from "./index";
import { useChat } from "./hooks/useChat";
import { useChatMessageQueue } from "./hooks/useChatMessageQueue";
import { useChatSend } from "./hooks/useChatSend";
import * as chatStore from "./hooks/chatStore";
import { findPendingAsk, hasPendingHitl } from "./utils/pendingHitl";
import type { PluginReadinessTarget } from "../../plugins/toolRenderers/usePluginToolUis";
import type { QueuedChatItem } from "./hooks/useChatMessageQueue";

vi.mock("../../components/DocumentPreviewCore", () => ({
  default: () => null,
}));
vi.mock("../../components/PdfDocumentPreview", () => ({
  default: () => null,
  PdfViewerSkeleton: () => null,
}));
vi.mock("./components/ChatInputActionsRow", () => ({
  default: ({
    onSubmit,
    canSend,
  }: {
    onSubmit: () => void;
    canSend: boolean;
  }) => (
    <button type="button" disabled={!canSend} onClick={onSubmit}>
      Send message
    </button>
  ),
}));
vi.mock("./components/ChatInputPreviewBar", () => ({ default: () => null }));
vi.mock("../hooks/useIsMobile", () => ({ useIsMobile: () => false }));
vi.mock("../../hooks/useVoiceInput", () => ({
  useVoiceInput: () => ({
    recording: false,
    transcribing: false,
    realtime: false,
    start: vi.fn(),
    stop: vi.fn(),
    toggle: vi.fn(),
  }),
}));
vi.mock("../../hooks/useKeepInVisualViewport", () => ({
  useKeepInVisualViewport: () => undefined,
}));
vi.mock("../../hooks/viewport", () => ({
  isPwaDisplay: () => false,
  needsComposerVisualViewportFix: () => false,
}));
vi.mock("./hooks/useChatAttachments", async () => {
  const ReactModule = await import("react");
  return {
    useChatAttachments: () => {
      const [attachments, setAttachments] = ReactModule.useState<never[]>([]);
      return {
        attachments,
        uploading: false,
        dragOver: false,
        fileInputRef: { current: null },
        folderInputRef: { current: null },
        handleFileSelect: vi.fn(),
        handleFolderSelect: vi.fn(),
        handleFileChange: vi.fn(),
        removeAttachment: vi.fn(),
        clearAttachments: () => setAttachments([]),
        restoreAttachments: setAttachments,
        handlePaste: vi.fn(),
        handleDragEnter: vi.fn(),
        handleDragLeave: vi.fn(),
        handleDragOver: vi.fn(),
        handleDrop: vi.fn(),
      };
    },
  };
});
vi.mock("../../hooks/useSlashCommands", () => ({
  useSlashCommands: () => ({
    commands: [],
    labelFor: (command: string) => command,
  }),
}));
vi.mock("./hooks/useWorkspaceFileMention", () => ({
  useWorkspaceFileMention: () => ({ files: [], loading: false, error: null }),
}));

const PLUGIN_ID = "enterprise-knowledge";
const AGENT_ID = "agent-b03f";
const THREAD_ID = "thread-b03f";
const GENERATION = "generation-b03f";
const CONTEXT: PluginSurfaceContext = {
  agentId: AGENT_ID,
  threadId: THREAD_ID,
  locale: "en",
  theme: "light",
  generation: GENERATION,
  timeZone: "UTC",
  readiness: {
    status: "ready",
    requiredPlugins: { [PLUGIN_ID]: "1" },
    unavailablePlugins: [],
  },
};

type KnowledgeMode = "KB_ONLY" | "WEB_ONLY" | "HYBRID";
type SessionSnapshot = {
  schema_id: "octop.enterprise.knowledge.v1";
  version: 1;
  agent_id: string;
  thread_id: string;
  revision: number;
  selection_present: boolean;
  selected_kb_ids: string[];
  mode: KnowledgeMode;
  connection_id: string;
  config_revision: number;
  binding_revision: number;
  grant_revision: number;
  search_revision: number;
  invalid: false;
  invalid_reason: null;
  capabilities: {
    can_save_selection: true;
    can_select_knowledge: true;
    knowledge_ready: true;
    web_ready: true;
  };
  search_ready: true;
};

const backend = vi.hoisted(() => ({
  session: null as SessionSnapshot | null,
  operations: [] as {
    operation_id: string;
    revision: number;
    selected_kb_ids: string[];
    mode: KnowledgeMode;
  }[],
  actionCalls: [] as { name: string; body: Record<string, unknown> }[],
  resumeBodies: [] as Record<string, unknown>[],
  nextOperation: 1,
  requiredPlugins: {} as Record<string, string>,
  sessionCreateCalls: 0,
}));

class FakeWebSocket {
  static readonly OPEN = 1;
  static readonly instances: FakeWebSocket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  readonly sent: string[] = [];

  constructor(_url: string) {
    FakeWebSocket.instances.push(this);
    queueMicrotask(() => {
      if (this.readyState !== 3) {
        this.readyState = FakeWebSocket.OPEN;
        this.onopen?.();
      }
    });
  }

  send(frame: string) {
    this.sent.push(frame);
  }

  close() {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.onclose?.();
  }

  receive(frame: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
}

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function conflictResponse(): Response {
  return new Response(
    JSON.stringify({
      error: { code: "CONFIG_CONFLICT", message: "Session revision changed." },
    }),
    {
      status: 409,
      headers: { "Content-Type": "application/json" },
    },
  );
}

function currentSession(): SessionSnapshot {
  if (!backend.session) throw new Error("Test session is not initialized");
  return structuredClone(backend.session);
}

function responseForAction(
  name: string,
  body: Record<string, unknown>,
): Response | Record<string, unknown> {
  backend.actionCalls.push({ name, body });
  const payload = (body.payload ?? {}) as Record<string, unknown>;
  if (name === "session.get") {
    return {
      api_version: 1,
      result: { api_version: 1, version: 1, session: currentSession() },
    };
  }
  if (name === "session.save") {
    const previous = currentSession();
    const expectedRevision = payload.expected_revision;
    if (
      typeof expectedRevision !== "number" ||
      !Number.isInteger(expectedRevision) ||
      expectedRevision !== previous.revision
    ) {
      return conflictResponse();
    }
    backend.session = {
      ...previous,
      revision: previous.revision + 1,
      selection_present: true,
      selected_kb_ids: [...(payload.selected_kb_ids as string[])],
      mode: payload.mode as KnowledgeMode,
    };
    return {
      api_version: 1,
      result: { api_version: 1, version: 1, session: currentSession() },
    };
  }
  if (name === "connection.catalog") {
    return {
      api_version: 1,
      result: {
        connection_id: currentSession().connection_id,
        observed_revision: currentSession().config_revision,
        knowledge_bases: [
          { kb_id: "kb-a", name: "Library A" },
          { kb_id: "kb-b", name: "Library B" },
        ],
      },
    };
  }
  if (name === "operation.prepare") {
    const snapshot = currentSession();
    const expectedRevision = payload.expected_session_revision;
    if (
      typeof expectedRevision !== "number" ||
      !Number.isInteger(expectedRevision) ||
      expectedRevision !== snapshot.revision
    )
      return conflictResponse();
    const operation_id = `operation-${backend.nextOperation++}`;
    backend.operations.push({
      operation_id,
      revision: expectedRevision,
      selected_kb_ids: [...snapshot.selected_kb_ids],
      mode: snapshot.mode,
    });
    return {
      api_version: 1,
      result: {
        schema_id: "octop.enterprise.knowledge.v1",
        version: 1,
        operation: {
          operation_id,
          session_revision: expectedRevision,
          status: "VALIDATING",
        },
      },
    };
  }
  throw new Error(`Unexpected enterprise action: ${name}`);
}

function jsonApiResponse(value: unknown): Response {
  return jsonResponse(value);
}

function installHttpBoundary() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      if (
        url.pathname === `/api/plugins/${PLUGIN_ID}/actions/session.get` ||
        url.pathname === `/api/plugins/${PLUGIN_ID}/actions/session.save` ||
        url.pathname ===
          `/api/plugins/${PLUGIN_ID}/actions/connection.catalog` ||
        url.pathname === `/api/plugins/${PLUGIN_ID}/actions/operation.prepare`
      ) {
        const name = url.pathname.split("/").at(-1)!;
        const body = JSON.parse(String(init?.body ?? "{}")) as Record<
          string,
          unknown
        >;
        const actionResponse = responseForAction(name, body);
        return actionResponse instanceof Response
          ? actionResponse
          : jsonApiResponse(actionResponse);
      }
      if (url.pathname.endsWith(`/threads/${THREAD_ID}/history`)) {
        return jsonApiResponse({
          thread_id: THREAD_ID,
          messages: [],
          has_more: false,
          artifacts: [],
          turn_active: false,
        });
      }
      if (url.pathname === `/api/agents/${AGENT_ID}/chat/hitl/resume`) {
        const body = JSON.parse(String(init?.body ?? "{}")) as Record<
          string,
          unknown
        >;
        backend.resumeBodies.push(body);
        return new Response(
          'data: {"type":"done","thread_id":"thread-b03f"}\n\n',
          {
            status: 200,
            headers: { "Content-Type": "text/event-stream" },
          },
        );
      }
      return jsonApiResponse({});
    }),
  );
}

function pluginReady(_target: PluginReadinessTarget) {
  return Promise.resolve({
    status: "ready" as const,
    requiredPlugins: backend.requiredPlugins,
    unavailablePlugins: [],
  });
}

type HarnessProbe = {
  queueItems: readonly QueuedChatItem[];
  sentTurnCount: number;
};

function Harness({
  probe,
  newChat = false,
  readinessStatus = "ready",
}: {
  probe: { current: HarnessProbe | null };
  newChat?: boolean;
  readinessStatus?: "ready" | "loading" | "error";
}) {
  const chat = useChat(newChat ? null : THREAD_ID, AGENT_ID);
  const queue = useChatMessageQueue({
    agentId: AGENT_ID,
    threadId: THREAD_ID,
    isStreaming: chat.isStreaming,
    shouldDeferFlush: (threadId) =>
      chatStore.isSessionBusyForTurnDispatch(threadId),
    onFlush: (item, _target) =>
      dispatchPreparedQueueItem(
        item,
        item.target,
        pluginReady,
        chat.sendMessage,
        chat.editAndResend,
      ),
  });
  const send = useChatSend({
    resolvedAgentId: AGENT_ID,
    activeThreadId: newChat ? null : THREAD_ID,
    sessions: [],
    messagesLength: chat.messages.length,
    selectedModel: null,
    selectedConnectors: [],
    selectedKnowledgeBaseIds: [],
    reasoningMode: "auto",
    reasoningEffort: null,
    sendMessage: chat.sendMessage,
    createSession: () => {
      backend.sessionCreateCalls += 1;
      return {
        session: {} as never,
        resolvedId: Promise.resolve(THREAD_ID),
      };
    },
    refreshPluginReadiness: pluginReady,
    enqueuePrepared: queue.enqueueForTarget,
    requestPreparedFlush: queue.requestFlush,
    renameSession: () => {},
    t: ((key: string) => key) as never,
  });
  probe.current = {
    queueItems: queue.items,
    sentTurnCount: FakeWebSocket.instances
      .flatMap((socket) => socket.sent)
      .filter((frame) => JSON.parse(frame).type === "user_turn").length,
  };
  const pendingAsk = findPendingAsk(chat.messages);
  const onSend = (
    text: string,
    attachments?: never,
    lease?: Parameters<typeof send.handleSend>[2] extends {
      submissionLease?: infer L;
    }
      ? L
      : never,
  ) =>
    routeGuardedChatSend({
      text,
      attachments,
      activeThreadId: newChat ? null : THREAD_ID,
      pendingAsk,
      hasPendingHitlPause: hasPendingHitl(chat.messages),
      findPendingAskForThread: () => null,
      hasPendingHitlForThread: () => false,
      interceptUserMessage: () => false,
      resumeHitl: (actions, threadId, pendingId) =>
        chat.resumeHitl(actions, threadId, undefined, undefined, pendingId),
      handleSend: (value, files, overrides) =>
        send.handleSend(value, files, { ...overrides, submissionLease: lease }),
      warn: () => {},
    });
  return (
    <>
      <PluginSurface
        surface="composer"
        location="chat"
        context={{
          ...CONTEXT,
          threadId: newChat ? null : THREAD_ID,
          readiness:
            readinessStatus === "ready"
              ? {
                  status: "ready",
                  requiredPlugins: backend.requiredPlugins,
                  unavailablePlugins: [],
                }
              : readinessStatus === "loading"
              ? { status: "loading" }
              : { status: "error", reason: "unavailable" },
        }}
      />
      <ChatInput
        onSend={onSend}
        onQueue={(item, lease) =>
          send.handleSend(item.text, item.attachments, {
            queueIntent: true,
            submissionLease: lease,
          })
        }
        queuedItems={queue.items as QueuedChatItem[]}
        onRemoveQueued={queue.remove}
        onCancel={() => chat.cancelStream()}
        onNewChat={() => {}}
        isStreaming={chat.isStreaming}
        agentId={AGENT_ID}
        threadId={newChat ? null : THREAD_ID}
      />
    </>
  );
}

function turns(): Record<string, unknown>[] {
  return FakeWebSocket.instances.flatMap((socket) =>
    socket.sent
      .map((frame) => JSON.parse(frame) as Record<string, unknown>)
      .filter((frame) => frame.type === "user_turn"),
  );
}

async function submit(text: string) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value: text } });
  const button = screen.getByRole("button", { name: /send|queue/i });
  fireEvent.click(button);
  await act(async () => {});
}

function setReactBridge() {
  Object.assign(window, {
    __OCTOP_REACT__: React,
    __OCTOP_JSX__: { jsx, jsxs },
  });
}

describe("enterprise Composer Host integration", () => {
  let disposePlugin: (() => void) | undefined;

  beforeEach(() => {
    vi.clearAllMocks();
    FakeWebSocket.instances.length = 0;
    backend.session = {
      schema_id: "octop.enterprise.knowledge.v1",
      version: 1,
      agent_id: AGENT_ID,
      thread_id: THREAD_ID,
      revision: 1,
      selection_present: true,
      selected_kb_ids: ["kb-a"],
      mode: "KB_ONLY",
      connection_id: "connection-b03f",
      config_revision: 5,
      binding_revision: 2,
      grant_revision: 3,
      search_revision: 4,
      invalid: false,
      invalid_reason: null,
      capabilities: {
        can_save_selection: true,
        can_select_knowledge: true,
        knowledge_ready: true,
        web_ready: true,
      },
      search_ready: true,
    };
    backend.operations = [];
    backend.actionCalls = [];
    backend.resumeBodies = [];
    backend.nextOperation = 1;
    backend.requiredPlugins = { [PLUGIN_ID]: "1" };
    backend.sessionCreateCalls = 0;
    setReactBridge();
    vi.stubGlobal("BASE_URL", "");
    vi.stubGlobal("WebSocket", FakeWebSocket);
    installHttpBoundary();
    const registrationToken = {};
    const registrationHost = createPluginUiRegistrationHost(
      PLUGIN_ID,
      1,
      () => true,
      registrationToken,
    );
    disposePlugin = setupEnterpriseKnowledge(registrationHost);
  });

  afterEach(async () => {
    await act(async () => {
      disposePlugin?.();
      disposePlugin = undefined;
      for (const socket of FakeWebSocket.instances) socket.close();
      chatStore.cancelStream(THREAD_ID);
      chatStore.removeSession(THREAD_ID);
    });
    Reflect.deleteProperty(window, "__OCTOP_REACT__");
    Reflect.deleteProperty(window, "__OCTOP_JSX__");
    vi.unstubAllGlobals();
  });

  it("sends a first ordinary message once when the global plugin is unbound", async () => {
    backend.requiredPlugins = {};
    const probe = { current: null as HarnessProbe | null };
    render(
      <MemoryRouter>
        <Harness probe={probe} newChat />
      </MemoryRouter>,
    );

    await submit("ordinary first message");
    await waitFor(() => expect(turns()).toHaveLength(1));

    expect(backend.sessionCreateCalls).toBe(1);
    expect(turns()[0]).toMatchObject({ text: "ordinary first message" });
    expect(turns()[0]).not.toHaveProperty("plugin_context");
    expect(backend.actionCalls).toEqual([]);
    expect(backend.operations).toEqual([]);
  });

  it("preserves the knowledge draft during same-scope readiness polling", async () => {
    const probe = { current: null as HarnessProbe | null };
    const rendered = render(
      <MemoryRouter>
        <Harness probe={probe} />
      </MemoryRouter>,
    );
    await screen.findByText("Library A");
    const libraryB = screen.getByLabelText("Library B") as HTMLInputElement;
    fireEvent.click(libraryB);
    expect(libraryB.checked).toBe(true);

    rendered.rerender(
      <MemoryRouter>
        <Harness probe={probe} readinessStatus="loading" />
      </MemoryRouter>,
    );
    expect(
      (screen.getByLabelText("Library B") as HTMLInputElement).checked,
    ).toBe(true);

    rendered.rerender(
      <MemoryRouter>
        <Harness probe={probe} readinessStatus="error" />
      </MemoryRouter>,
    );
    expect(
      (screen.getByLabelText("Library B") as HTMLInputElement).checked,
    ).toBe(true);
    expect(
      screen.getByRole("button", { name: /save selection/i }),
    ).toBeTruthy();
  });

  it("prepares distinct busy queue items from their captured library snapshots and flushes FIFO without cancelling the active turn", async () => {
    const probe = { current: null as HarnessProbe | null };
    render(
      <MemoryRouter>
        <Harness probe={probe} />
      </MemoryRouter>,
    );
    await screen.findByText("Library A");
    await screen.findByText("Library A");
    await screen.findByText("Library B");

    await submit("first turn");
    await waitFor(() => expect(turns()).toHaveLength(1));
    const firstSocket = FakeWebSocket.instances.find((socket) =>
      socket.sent.some((frame) => JSON.parse(frame).type === "user_turn"),
    )!;
    expect(turns()[0]).toMatchObject({
      text: "first turn",
      plugin_context: { [PLUGIN_ID]: { operation_id: "operation-1" } },
    });
    expect(backend.operations).toHaveLength(1);

    await submit("follow up B");
    await waitFor(() => expect(probe.current?.queueItems).toHaveLength(2));
    fireEvent.click(screen.getByLabelText("Library B"));
    fireEvent.click(screen.getByRole("button", { name: /save selection/i }));
    await waitFor(() =>
      expect(currentSession().selected_kb_ids).toEqual(["kb-a", "kb-b"]),
    );
    await submit("follow up A");
    await waitFor(() => expect(probe.current?.queueItems).toHaveLength(3));
    expect(backend.operations).toHaveLength(3);
    expect(backend.operations.map((item) => item.operation_id)).toEqual([
      "operation-1",
      "operation-2",
      "operation-3",
    ]);
    expect(
      backend.operations.map(({ revision, mode }) => ({ revision, mode })),
    ).toEqual([
      { revision: 1, mode: "KB_ONLY" },
      { revision: 1, mode: "KB_ONLY" },
      { revision: 2, mode: "KB_ONLY" },
    ]);
    expect(
      probe.current?.queueItems
        .slice(1)
        .map((item) => item.pluginSelection?.[0]?.selection),
    ).toMatchObject([
      {
        state: "selected",
        value: {
          selected_kb_ids: ["kb-a"],
          session_revision: 1,
          mode: "KB_ONLY",
        },
      },
      {
        state: "selected",
        value: {
          selected_kb_ids: ["kb-a", "kb-b"],
          session_revision: 2,
          mode: "KB_ONLY",
        },
      },
    ]);
    expect(
      probe.current?.queueItems
        .slice(1)
        .map((item) => item.pluginContext?.[PLUGIN_ID]),
    ).toEqual([
      { operation_id: "operation-2" },
      { operation_id: "operation-3" },
    ]);
    expect(firstSocket.sent.map((frame) => JSON.parse(frame).type)).toEqual([
      "user_turn",
    ]);

    fireEvent.click(screen.getByLabelText("Library A"));
    fireEvent.click(screen.getByRole("button", { name: /save selection/i }));
    await waitFor(() =>
      expect(currentSession().selected_kb_ids).toEqual(["kb-b"]),
    );
    expect(currentSession()).toMatchObject({
      revision: 3,
      selected_kb_ids: ["kb-b"],
      mode: "KB_ONLY",
    });
    expect(
      probe.current?.queueItems
        .slice(1)
        .map((item) => item.pluginSelection?.[0]?.selection),
    ).toMatchObject([
      {
        state: "selected",
        value: {
          selected_kb_ids: ["kb-a"],
          session_revision: 1,
          mode: "KB_ONLY",
        },
      },
      {
        state: "selected",
        value: {
          selected_kb_ids: ["kb-a", "kb-b"],
          session_revision: 2,
          mode: "KB_ONLY",
        },
      },
    ]);

    await act(async () =>
      firstSocket.receive({ type: "done", thread_id: THREAD_ID }),
    );
    await waitFor(() => expect(turns()).toHaveLength(2));
    await act(async () => {
      const secondSocket = FakeWebSocket.instances.find((socket) =>
        socket.sent.some(
          (frame) =>
            JSON.parse(frame).type === "user_turn" &&
            JSON.parse(frame).text === "follow up B",
        ),
      )!;
      secondSocket.receive({ type: "done", thread_id: THREAD_ID });
    });
    await waitFor(() => expect(turns()).toHaveLength(3));
    const sent = turns();
    expect(sent.map((frame) => frame.text)).toEqual([
      "first turn",
      "follow up B",
      "follow up A",
    ]);
    expect(
      sent.map(
        (frame) =>
          (frame.plugin_context as Record<string, { operation_id: string }>)[
            PLUGIN_ID
          ]?.operation_id,
      ),
    ).toEqual(["operation-1", "operation-2", "operation-3"]);
    expect(sent.slice(1).map((frame) => frame.delivery)).toEqual([
      "follow_up",
      "follow_up",
    ]);
    expect(backend.operations.map((item) => item.selected_kb_ids)).toEqual([
      ["kb-a"],
      ["kb-a"],
      ["kb-a", "kb-b"],
    ]);
    expect(
      backend.actionCalls.filter((call) => call.name === "operation.prepare"),
    ).toHaveLength(3);
    expect(
      firstSocket.sent.some((frame) => /"cancel"|"steer"/.test(frame)),
    ).toBe(false);
  });

  it("resumes a real pending ask without preparing or sending another enterprise turn", async () => {
    render(
      <MemoryRouter>
        <Harness probe={{ current: null }} />
      </MemoryRouter>,
    );
    await screen.findByText("Library A");
    await submit("ask the agent");
    await waitFor(() => expect(turns()).toHaveLength(1));
    const initialSocket = FakeWebSocket.instances.find((socket) =>
      socket.sent.some((frame) => JSON.parse(frame).type === "user_turn"),
    )!;
    expect(turns()[0]?.plugin_context).toEqual({
      [PLUGIN_ID]: { operation_id: "operation-1" },
    });

    await act(async () => {
      initialSocket.receive({
        type: "tool_call_chunk",
        thread_id: THREAD_ID,
        name: "ask_user_question",
        id: "ask-call-b03f",
        args: JSON.stringify({ questions: [{ question: "Which option?" }] }),
      });
      initialSocket.receive({
        type: "hitl_required",
        thread_id: THREAD_ID,
        request: {
          pending_id: "pending-b03f",
          action_requests: [
            {
              name: "ask_user_question",
              args: { questions: [{ question: "Which option?" }] },
            },
          ],
        },
      });
      initialSocket.receive({
        type: "done",
        thread_id: THREAD_ID,
        hitl_paused: true,
      });
    });
    await waitFor(() =>
      expect(
        findPendingAsk(chatStore.getSnapshot(THREAD_ID).messages),
      ).not.toBeNull(),
    );
    await submit("choose option A");
    await waitFor(() => expect(backend.resumeBodies).toHaveLength(1));
    expect(backend.resumeBodies[0]).toMatchObject({
      thread_id: THREAD_ID,
      pending_id: "pending-b03f",
      decisions: [{ type: "respond", message: "choose option A" }],
    });
    expect(backend.resumeBodies[0]).not.toHaveProperty("plugin_context");
    expect(
      backend.actionCalls.filter((call) => call.name === "operation.prepare"),
    ).toHaveLength(1);
    expect(turns()).toHaveLength(1);
    expect(turns()[0]?.plugin_context).toEqual({
      [PLUGIN_ID]: { operation_id: "operation-1" },
    });
  });
});
