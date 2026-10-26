import { describe, expect, it, vi, beforeEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  act,
  waitFor,
} from "@testing-library/react";
import { createRef } from "react";
import { MemoryRouter } from "react-router-dom";
import { useChatSend } from "../hooks/useChatSend";
import { reclaimQueueItemIfCurrent, routeGuardedChatSend } from "../index";
import {
  getAuthSessionEpoch,
  invalidateAuthSession,
} from "../../../api/authSession";
import {
  captureComposerPreparations,
  prepareComposerContext,
} from "../../../plugins/toolRenderers/composerPreparation";

const modalControl = vi.hoisted(() => ({ confirm: vi.fn() }));

vi.mock("antd", async (importOriginal) => {
  const actual = await importOriginal<typeof import("antd")>();
  const ui = {
    modal: { confirm: modalControl.confirm },
    message: {
      warning: vi.fn(),
      info: vi.fn(),
      success: vi.fn(),
      error: vi.fn(),
    },
  };
  return { ...actual, App: { ...actual.App, useApp: () => ui } };
});
vi.mock("../../../components/DocumentPreviewCore", () => ({
  default: () => null,
}));
vi.mock("../../../components/PdfDocumentPreview", () => ({
  default: () => null,
  PdfViewerSkeleton: () => null,
}));

const attachmentControl = vi.hoisted(() => ({
  set: null as
    | ((value: { url: string; filename: string; kind: "file" }[]) => void)
    | null,
}));
const slashCommandControl = vi.hoisted(() => ({
  commands: [] as {
    name: string;
    command: string;
    aliases: string[];
    label_en: string;
    label_zh: string;
    description_en: string;
    description_zh: string;
    usage: string;
    icon: string;
    tone: string;
    category: string;
    origins: string[];
    client_action: "none" | "new_chat";
  }[],
}));

vi.mock("../../../hooks/useIsMobile", () => ({
  useIsMobile: () => false,
}));

vi.mock("../../../hooks/useSlashCommands", () => ({
  useSlashCommands: () => ({
    commands: slashCommandControl.commands,
    labelFor: (cmd: string) => cmd,
  }),
}));

vi.mock("../../../hooks/useVoiceInput", () => ({
  useVoiceInput: () => ({
    recording: false,
    transcribing: false,
    realtime: false,
    start: vi.fn(),
    stop: vi.fn(),
    toggle: vi.fn(),
  }),
}));

vi.mock("../../../hooks/useKeepInVisualViewport", () => ({
  useKeepInVisualViewport: () => undefined,
}));

vi.mock("../../../hooks/viewport", () => ({
  isPwaDisplay: () => false,
  needsComposerVisualViewportFix: () => false,
}));

vi.mock("../hooks/useChatAttachments", async () => {
  const React = await import("react");
  return {
    useChatAttachments: () => {
      const [attachments, setAttachments] = React.useState<
        { url: string; filename: string; kind: "file" }[]
      >([]);
      attachmentControl.set = setAttachments;
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

vi.mock("../hooks/chatStore", () => ({
  consumePendingPrefillAttachments: () => [],
  onSteerRestore: () => () => {},
  readInputDraft: (agentId: string) =>
    agentId === "agent-2" ? "target expert draft" : "",
  writeInputDraft: vi.fn(),
}));

vi.mock("../../../plugins/toolRenderers/composerPreparation", () => ({
  captureComposerPreparations: vi.fn(),
  prepareComposerContext: vi.fn(),
}));
vi.mock("../../../plugins/toolRenderers/registry", () => ({
  listPluginSurfaces: () => [{ pluginId: "plugin-a" }],
}));

vi.mock("./ChatInputPreviewBar", () => ({
  default: ({ attachments }: { attachments: { filename?: string }[] }) => (
    <div>{attachments.map((attachment) => attachment.filename)}</div>
  ),
}));

vi.mock("./ChatInputActionsRow", () => ({
  default: ({
    onSubmit,
    canSend,
  }: {
    onSubmit: () => void;
    canSend: boolean;
  }) => (
    <button type="button" disabled={!canSend} onClick={onSubmit}>
      send
    </button>
  ),
}));

vi.mock("./ChatQueuedMessages", () => ({
  default: ({
    items,
    onReclaim,
  }: {
    items: { id: string }[];
    onReclaim: (id: string) => void;
  }) => (
    <div>
      {items.map((item) => (
        <button key={item.id} type="button" onClick={() => onReclaim(item.id)}>
          reclaim {item.id}
        </button>
      ))}
    </div>
  ),
}));

vi.mock("./SlashCommandMenu", () => ({
  default: ({
    flatItems,
    onSelect,
  }: {
    flatItems: { command: string }[];
    onSelect: (command: string) => void;
  }) => (
    <div>
      {flatItems.map((item) => (
        <button
          key={item.command}
          type="button"
          onClick={() => onSelect(item.command)}
        >
          {item.command}
        </button>
      ))}
    </div>
  ),
}));

vi.mock("./MentionPickerMenu", () => ({
  default: () => null,
  buildMentionItems: () => [],
}));

vi.mock("../hooks/useWorkspaceFileMention", () => ({
  useWorkspaceFileMention: () => ({ files: [], loading: false, error: null }),
}));

import ChatInput, { type ChatInputHandle } from "./ChatInput";

beforeEach(() => {
  vi.clearAllMocks();
  attachmentControl.set = null;
  slashCommandControl.commands = [];
});

describe("ChatInput prefill clear-on-send", () => {
  it("resumes a pending ask without preparing or enqueuing a new turn", async () => {
    vi.mocked(captureComposerPreparations).mockReturnValue([]);
    const targetThread = "thread-ask";
    const resumeHitl = vi.fn();
    const enqueuePrepared = vi.fn(() => ({
      status: "accepted" as const,
      acceptedId: "queue-1",
    }));
    const sendMessage = vi.fn();
    const ask = {
      messageId: "message-ask",
      actions: [{ name: "ask_user_question", args: { questions: [] } }],
      questions: [],
    };

    function PendingAskInput() {
      const send = useChatSend({
        resolvedAgentId: "agent-1",
        activeThreadId: targetThread,
        sessions: [],
        messagesLength: 0,
        selectedModel: null,
        selectedConnectors: [],
        selectedKnowledgeBaseIds: [],
        reasoningMode: "auto",
        reasoningEffort: null,
        sendMessage,
        createSession: vi.fn(),
        renameSession: vi.fn(),
        refreshPluginReadiness: vi.fn(),
        enqueuePrepared,
        requestPreparedFlush: vi.fn(),
        t: ((key: string) => key) as never,
      });
      return (
        <ChatInput
          onSend={(text, attachments) =>
            routeGuardedChatSend({
              text,
              attachments,
              activeThreadId: targetThread,
              pendingAsk: ask,
              hasPendingHitlPause: false,
              findPendingAskForThread: () => null,
              hasPendingHitlForThread: () => false,
              interceptUserMessage: () => false,
              resumeHitl,
              handleSend: send.handleSend,
              warn: vi.fn(),
            })
          }
          onCancel={vi.fn()}
          onNewChat={vi.fn()}
          isStreaming={false}
          initialText="answer the question"
        />
      );
    }

    render(
      <MemoryRouter>
        <PendingAskInput />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "send" }));
    await act(async () => {});

    expect(resumeHitl).toHaveBeenCalledExactlyOnceWith(
      [{ type: "respond", message: "answer the question" }],
      targetThread,
    );
    expect(captureComposerPreparations).not.toHaveBeenCalled();
    expect(prepareComposerContext).not.toHaveBeenCalled();
    expect(enqueuePrepared).not.toHaveBeenCalled();
    expect(sendMessage).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox")).toHaveValue("");
  });

  it("retains the draft and does not start a turn when another HITL pause blocks send", async () => {
    vi.mocked(captureComposerPreparations).mockReturnValue([]);
    const enqueuePrepared = vi.fn();
    const sendMessage = vi.fn();

    function BlockedInput() {
      const send = useChatSend({
        resolvedAgentId: "agent-1",
        activeThreadId: "thread-blocked",
        sessions: [],
        messagesLength: 0,
        selectedModel: null,
        selectedConnectors: [],
        selectedKnowledgeBaseIds: [],
        reasoningMode: "auto",
        reasoningEffort: null,
        sendMessage,
        createSession: vi.fn(),
        renameSession: vi.fn(),
        refreshPluginReadiness: vi.fn(),
        enqueuePrepared,
        requestPreparedFlush: vi.fn(),
        t: ((key: string) => key) as never,
      });
      return (
        <ChatInput
          onSend={(text, attachments) =>
            routeGuardedChatSend({
              text,
              attachments,
              activeThreadId: "thread-blocked",
              pendingAsk: null,
              hasPendingHitlPause: true,
              findPendingAskForThread: () => null,
              hasPendingHitlForThread: () => false,
              interceptUserMessage: () => false,
              resumeHitl: vi.fn(),
              handleSend: send.handleSend,
              warn: vi.fn(),
            })
          }
          onCancel={vi.fn()}
          onNewChat={vi.fn()}
          isStreaming={false}
          initialText="keep this blocked draft"
        />
      );
    }

    render(
      <MemoryRouter>
        <BlockedInput />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "send" }));
    await act(async () => {});

    expect(screen.getByRole("textbox")).toHaveValue("keep this blocked draft");
    expect(captureComposerPreparations).not.toHaveBeenCalled();
    expect(prepareComposerContext).not.toHaveBeenCalled();
    expect(enqueuePrepared).not.toHaveBeenCalled();
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("routes a selected server slash command through one pending prepared-send lease", async () => {
    slashCommandControl.commands = [
      {
        name: "server-status",
        command: "/server-status",
        aliases: [],
        label_en: "Server status",
        label_zh: "服务状态",
        description_en: "",
        description_zh: "",
        usage: "/server-status",
        icon: "Square",
        tone: "blue",
        category: "core",
        origins: ["ui"],
        client_action: "none",
      },
    ];
    const captured = {
      pluginId: "plugin-a",
      generation: "gen-a",
      selection: { state: "selected", value: { ids: ["kb-1"] } },
      isCurrent: () => true,
      prepare: vi.fn(),
      restoreSelection: vi.fn(),
    };
    vi.mocked(captureComposerPreparations).mockReturnValue([captured] as never);
    vi.mocked(prepareComposerContext).mockResolvedValue({
      "plugin-a": { operation_id: "op-1" },
    } as never);
    let releaseReadiness!: (value: {
      status: "ready";
      requiredPlugins: Record<string, string>;
      unavailablePlugins: never[];
    }) => void;
    const target = { agentId: "agent-real", threadId: "thread-real" };
    const enqueuePrepared = vi.fn(() => ({
      status: "accepted" as const,
      acceptedId: "queue-1",
      acceptedTarget: target,
    }));
    const refreshPluginReadiness = vi.fn(
      ({ threadId }: { threadId: string | null }) =>
        threadId
          ? Promise.resolve({
              status: "ready" as const,
              requiredPlugins: { "plugin-a": "1" },
              unavailablePlugins: [],
            })
          : new Promise<{
              status: "ready";
              requiredPlugins: Record<string, string>;
              unavailablePlugins: never[];
            }>((resolve) => {
              releaseReadiness = resolve;
            }),
    );
    const createSession = vi.fn(() => ({
      session: { id: "__pending__" } as never,
      resolvedId: Promise.resolve(target.threadId),
    }));
    const sendMessage = vi.fn();
    const onAcceptedTarget = vi.fn();
    const onNewChat = vi.fn();

    function PreparedSendInput() {
      const { handleSend } = useChatSend({
        resolvedAgentId: target.agentId,
        activeThreadId: null,
        sessions: [],
        messagesLength: 0,
        selectedModel: "model-a",
        selectedConnectors: ["connector-a"],
        selectedKnowledgeBaseIds: ["kb-a"],
        selectedTargetAgents: ["expert-a"],
        reasoningMode: "enabled",
        reasoningEffort: "high",
        conversationMode: "plan",
        hitlPolicy: { mode: "allow_tools", tools: ["tool-a"] },
        sendMessage,
        createSession,
        renameSession: vi.fn(),
        refreshPluginReadiness,
        enqueuePrepared,
        requestPreparedFlush: vi.fn(),
        t: ((key: string) => key) as never,
      });
      return (
        <ChatInput
          onSend={handleSend}
          onAcceptedTarget={onAcceptedTarget}
          onCancel={vi.fn()}
          onNewChat={onNewChat}
          isStreaming={false}
          initialText=""
        />
      );
    }

    render(
      <MemoryRouter>
        <PreparedSendInput />
      </MemoryRouter>,
    );
    const textbox = screen.getByRole("textbox");
    fireEvent.change(textbox, { target: { value: "/" } });
    fireEvent.click(screen.getByRole("button", { name: "/server-status" }));
    fireEvent.keyDown(textbox, { key: "Enter" });

    expect(refreshPluginReadiness).toHaveBeenCalledOnce();
    expect(refreshPluginReadiness.mock.calls[0]?.[0].threadId).toBeNull();
    expect(prepareComposerContext).not.toHaveBeenCalled();
    expect(enqueuePrepared).not.toHaveBeenCalled();
    expect(sendMessage).not.toHaveBeenCalled();

    await act(async () => {
      releaseReadiness({
        status: "ready",
        requiredPlugins: { "plugin-a": "1" },
        unavailablePlugins: [],
      });
    });

    expect(refreshPluginReadiness).toHaveBeenCalledTimes(2);
    expect(
      refreshPluginReadiness.mock.calls.map(([arg]) => arg.threadId),
    ).toEqual([null, target.threadId]);
    expect(prepareComposerContext).toHaveBeenCalledOnce();
    expect(enqueuePrepared).toHaveBeenCalledOnce();
    expect(enqueuePrepared.mock.calls[0]?.[1]).toMatchObject({
      text: "/server-status",
      bound: true,
    });
    expect(onAcceptedTarget).toHaveBeenCalledExactlyOnceWith(target);
    expect(sendMessage).not.toHaveBeenCalled();
    expect(onNewChat).not.toHaveBeenCalled();
  });

  it("keeps client slash actions local instead of starting a new send turn", () => {
    slashCommandControl.commands = [
      {
        name: "new-chat",
        command: "/new-chat",
        aliases: [],
        label_en: "New chat",
        label_zh: "新聊天",
        description_en: "",
        description_zh: "",
        usage: "/new-chat",
        icon: "Square",
        tone: "blue",
        category: "core",
        origins: ["ui"],
        client_action: "new_chat",
      },
    ];
    const onSend = vi.fn();
    const onNewChat = vi.fn();
    render(
      <ChatInput
        onSend={onSend}
        onCancel={vi.fn()}
        onNewChat={onNewChat}
        isStreaming={false}
        initialText=""
      />,
    );
    const textbox = screen.getByRole("textbox");
    fireEvent.change(textbox, { target: { value: "/" } });
    fireEvent.click(screen.getByRole("button", { name: "/new-chat" }));

    expect(onNewChat).toHaveBeenCalledOnce();
    expect(onSend).not.toHaveBeenCalled();
  });

  it("does not restore stale initialText after send (skill-card prefill path)", async () => {
    // Mirrors welcome skill/quick-card flow:
    // 1) setPrefillText fills the composer imperatively while parent initialText
    //    is still ""
    // 2) parent keeps the prefill in a ref and re-passes it as initialText on
    //    the next render (e.g. after send updates chat state)
    // 3) composer must stay empty after send — not re-filled from that prop
    const onSend = vi.fn(async () => ({ status: "accepted" as const }));
    const onCancel = vi.fn();
    const onNewChat = vi.fn();
    const inputRef = createRef<ChatInputHandle>();

    const { rerender } = render(
      <ChatInput
        ref={inputRef}
        onSend={onSend}
        onCancel={onCancel}
        onNewChat={onNewChat}
        isStreaming={false}
        initialText=""
        agentId="agent-1"
        threadId="thread-1"
      />,
    );

    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    const prefill = "请填写收件人：";

    act(() => {
      inputRef.current?.setPrefillText(prefill);
    });
    expect(textarea.value).toBe(prefill);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "send" }));
    });
    expect(onSend).toHaveBeenCalledWith(prefill, undefined, undefined);
    expect(textarea.value).toBe("");

    // Parent re-render still holding the prefill string in initialText
    rerender(
      <ChatInput
        ref={inputRef}
        onSend={onSend}
        onCancel={onCancel}
        onNewChat={onNewChat}
        isStreaming={true}
        initialText={prefill}
        agentId="agent-1"
        threadId="thread-1"
      />,
    );

    expect(textarea.value).toBe("");
  });

  it("clears parent prefill via onComposerCleared so edited text cannot be overwritten", async () => {
    const onSend = vi.fn(async () => ({ status: "accepted" as const }));
    const onCancel = vi.fn();
    const onNewChat = vi.fn();
    const inputRef = createRef<ChatInputHandle>();
    let parentPrefill = "";

    const { rerender } = render(
      <ChatInput
        ref={inputRef}
        onSend={onSend}
        onCancel={onCancel}
        onNewChat={onNewChat}
        isStreaming={false}
        initialText=""
        onComposerCleared={() => {
          parentPrefill = "";
        }}
        agentId="agent-1"
        threadId="thread-1"
      />,
    );

    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    const prefill = "请填写收件人：";
    parentPrefill = prefill;

    act(() => {
      inputRef.current?.setPrefillText(prefill);
    });
    fireEvent.change(textarea, {
      target: { value: `${prefill}\n张三` },
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "send" }));
    });
    expect(onSend).toHaveBeenCalledWith(
      `${prefill}\n张三`,
      undefined,
      undefined,
    );
    expect(textarea.value).toBe("");
    expect(parentPrefill).toBe("");

    rerender(
      <ChatInput
        ref={inputRef}
        onSend={onSend}
        onCancel={onCancel}
        onNewChat={onNewChat}
        isStreaming={true}
        initialText={parentPrefill}
        onComposerCleared={() => {
          parentPrefill = "";
        }}
        agentId="agent-1"
        threadId="thread-1"
      />,
    );

    expect(textarea.value).toBe("");
  });

  it("restores the target agent draft instead of carrying stale prefill", () => {
    const props = {
      onSend: vi.fn(),
      onCancel: vi.fn(),
      onNewChat: vi.fn(),
      isStreaming: false,
      initialText: "previous expert prompt",
      threadId: null,
    };
    const { rerender } = render(<ChatInput {...props} agentId="agent-1" />);

    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(textarea.value).toBe("previous expert prompt");

    rerender(<ChatInput {...props} agentId="agent-2" />);

    expect(textarea.value).toBe("target expert draft");
  });

  it("sends immediately in a team room while a turn is still streaming", async () => {
    const onSend = vi.fn(async () => ({ status: "accepted" as const }));
    const onQueue = vi.fn();
    render(
      <ChatInput
        onSend={onSend}
        onQueue={onQueue}
        onCancel={vi.fn()}
        onNewChat={vi.fn()}
        isStreaming
        isTeam
        agentId="team-1"
        threadId="thread-1"
      />,
    );
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "second question" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "send" }));
    });
    expect(onSend).toHaveBeenCalledWith(
      "second question",
      undefined,
      undefined,
    );
    expect(onQueue).not.toHaveBeenCalled();
  });

  it("keeps a newer draft when async submission rejects or accepts an older revision", async () => {
    let resolveSend!: (
      value:
        | {
            status: "accepted";
            acceptedTarget?: { agentId: string; threadId: string };
          }
        | { status: "rejected"; reason: "blocked" },
    ) => void;
    const onSend = vi.fn(
      () =>
        new Promise<
          | {
              status: "accepted";
              acceptedTarget?: { agentId: string; threadId: string };
            }
          | { status: "rejected"; reason: "blocked" }
        >((resolve) => {
          resolveSend = resolve;
        }),
    );
    const onAcceptedTarget = vi.fn();
    render(
      <ChatInput
        onSend={onSend}
        onAcceptedTarget={onAcceptedTarget}
        onCancel={vi.fn()}
        onNewChat={vi.fn()}
        isStreaming={false}
        initialText="first draft"
      />,
    );
    const textbox = screen.getByRole("textbox") as HTMLTextAreaElement;
    fireEvent.click(screen.getByRole("button", { name: "send" }));
    expect(screen.getByRole("button", { name: "send" })).toBeDisabled();
    fireEvent.change(textbox, { target: { value: "newer draft" } });
    resolveSend({ status: "rejected", reason: "blocked" });
    await act(async () => {});
    expect(textbox.value).toBe("newer draft");
    expect(onAcceptedTarget).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "send" }));
    resolveSend({
      status: "accepted",
      acceptedTarget: { agentId: "agent-created", threadId: "thread-created" },
    });
    await act(async () => {});
    expect(textbox.value).toBe("");
    expect(onAcceptedTarget).toHaveBeenCalledExactlyOnceWith({
      agentId: "agent-created",
      threadId: "thread-created",
    });
  });

  it("passes the real prepared-send receipt through ChatInput before clearing and opening its target", async () => {
    const captured = {
      pluginId: "plugin-a",
      generation: "gen-a",
      selection: { state: "selected", value: { ids: ["kb-1"] } },
      isCurrent: () => true,
      prepare: vi.fn(),
      restoreSelection: vi.fn(),
    };
    vi.mocked(captureComposerPreparations).mockReturnValue([captured] as never);
    vi.mocked(prepareComposerContext).mockResolvedValue({
      "plugin-a": { operation_id: "op-1" },
    } as never);
    const target = { agentId: "agent-real", threadId: "thread-real" };
    const onAcceptedTarget = vi.fn();
    const enqueuePrepared = vi.fn(() => ({
      status: "accepted" as const,
      acceptedId: "queue-1",
      acceptedTarget: target,
    }));
    const refreshPluginReadiness = vi.fn(async () => ({
      status: "ready" as const,
      requiredPlugins: { "plugin-a": "1" },
      unavailablePlugins: [],
    }));
    const createSession = vi.fn(() => ({
      session: { id: "__pending__" } as never,
      resolvedId: Promise.resolve(target.threadId),
    }));
    const sendMessage = vi.fn();

    function PreparedSendInput() {
      const { handleSend } = useChatSend({
        resolvedAgentId: target.agentId,
        activeThreadId: null,
        sessions: [],
        messagesLength: 0,
        selectedModel: "model-a",
        selectedConnectors: ["connector-a"],
        selectedKnowledgeBaseIds: ["kb-a"],
        selectedTargetAgents: ["expert-a"],
        reasoningMode: "enabled",
        reasoningEffort: "high",
        conversationMode: "plan",
        hitlPolicy: { mode: "allow_tools", tools: ["tool-a"] },
        sendMessage,
        createSession,
        renameSession: vi.fn(),
        refreshPluginReadiness,
        enqueuePrepared,
        requestPreparedFlush: vi.fn(),
        t: ((key: string) => key) as never,
      });
      return (
        <ChatInput
          onSend={handleSend}
          onAcceptedTarget={onAcceptedTarget}
          onCancel={vi.fn()}
          onNewChat={vi.fn()}
          isStreaming={false}
          initialText="prepared first send"
        />
      );
    }

    render(
      <MemoryRouter>
        <PreparedSendInput />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "send" }));
    await act(async () => {});

    expect(prepareComposerContext).toHaveBeenCalledWith(
      [captured],
      expect.objectContaining({ target, text: "prepared first send" }),
    );
    expect(enqueuePrepared).toHaveBeenCalledWith(
      target,
      expect.objectContaining({ bound: true, text: "prepared first send" }),
    );
    expect(onAcceptedTarget).toHaveBeenCalledExactlyOnceWith(target);
    expect(screen.getByRole("textbox")).toHaveValue("");
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("keeps a newer attachment when real preparation accepts the older snapshot", async () => {
    const captured = {
      pluginId: "plugin-a",
      generation: "gen-a",
      selection: { state: "selected", value: { ids: ["kb-1"] } },
      isCurrent: () => true,
      prepare: vi.fn(),
      restoreSelection: vi.fn(),
    };
    vi.mocked(captureComposerPreparations).mockReturnValue([captured] as never);
    vi.mocked(prepareComposerContext).mockResolvedValue({
      "plugin-a": { operation_id: "op-1" },
    } as never);
    let releaseReadiness!: (value: {
      status: "ready";
      requiredPlugins: Record<string, string>;
      unavailablePlugins: never[];
    }) => void;
    const target = { agentId: "agent-real", threadId: "thread-real" };
    const enqueuePrepared = vi.fn((_target, _item) => ({
      status: "accepted" as const,
      acceptedId: "queue-1",
      acceptedTarget: target,
    }));
    const refreshPluginReadiness = vi.fn(
      ({ threadId }: { threadId: string | null }) =>
        threadId
          ? Promise.resolve({
              status: "ready" as const,
              requiredPlugins: { "plugin-a": "1" },
              unavailablePlugins: [],
            })
          : new Promise<{
              status: "ready";
              requiredPlugins: Record<string, string>;
              unavailablePlugins: never[];
            }>((resolve) => {
              releaseReadiness = resolve;
            }),
    );
    const createSession = vi.fn(() => ({
      session: { id: "__pending__" } as never,
      resolvedId: Promise.resolve(target.threadId),
    }));
    const sendMessage = vi.fn();
    const onAcceptedTarget = vi.fn();

    function PreparedSendInput() {
      const { handleSend } = useChatSend({
        resolvedAgentId: target.agentId,
        activeThreadId: null,
        sessions: [],
        messagesLength: 0,
        selectedModel: "model-a",
        selectedConnectors: ["connector-a"],
        selectedKnowledgeBaseIds: ["kb-a"],
        selectedTargetAgents: ["expert-a"],
        reasoningMode: "enabled",
        reasoningEffort: "high",
        conversationMode: "plan",
        hitlPolicy: { mode: "allow_tools", tools: ["tool-a"] },
        sendMessage,
        createSession,
        renameSession: vi.fn(),
        refreshPluginReadiness,
        enqueuePrepared,
        requestPreparedFlush: vi.fn(),
        t: ((key: string) => key) as never,
      });
      return (
        <ChatInput
          onSend={handleSend}
          onAcceptedTarget={onAcceptedTarget}
          onCancel={vi.fn()}
          onNewChat={vi.fn()}
          isStreaming={false}
          initialText="older prompt"
        />
      );
    }

    render(
      <MemoryRouter>
        <PreparedSendInput />
      </MemoryRouter>,
    );
    act(() => {
      attachmentControl.set?.([
        { url: "/uploads/old.txt", filename: "old.txt", kind: "file" },
      ]);
    });
    fireEvent.click(screen.getByRole("button", { name: "send" }));
    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      attachmentControl.set?.([
        { url: "/uploads/new.txt", filename: "new.txt", kind: "file" },
      ]);
      releaseReadiness({
        status: "ready",
        requiredPlugins: { "plugin-a": "1" },
        unavailablePlugins: [],
      });
    });
    await waitFor(() => expect(enqueuePrepared).toHaveBeenCalledOnce());

    expect(prepareComposerContext).toHaveBeenCalledWith(
      [captured],
      expect.objectContaining({
        attachments: [
          { url: "/uploads/old.txt", filename: "old.txt", kind: "file" },
        ],
      }),
    );
    expect(enqueuePrepared.mock.calls[0]?.[1]).toMatchObject({
      attachments: [
        { url: "/uploads/old.txt", filename: "old.txt", kind: "file" },
      ],
    });
    expect(screen.getByRole("textbox")).toHaveValue("older prompt");
    expect(screen.getByText("new.txt")).toBeInTheDocument();
    expect(onAcceptedTarget).not.toHaveBeenCalled();
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("rejects a headless prepared send when its mounted origin changes during readiness", async () => {
    vi.mocked(captureComposerPreparations).mockReturnValue([]);
    let releaseReadiness!: (value: {
      status: "ready";
      requiredPlugins: Record<string, string>;
      unavailablePlugins: never[];
    }) => void;
    const refreshPluginReadiness = vi.fn(
      () =>
        new Promise<{
          status: "ready";
          requiredPlugins: Record<string, string>;
          unavailablePlugins: never[];
        }>((resolve) => {
          releaseReadiness = resolve;
        }),
    );
    const createSession = vi.fn(() => ({
      session: { id: "__pending__" } as never,
      resolvedId: Promise.resolve("thread-created"),
    }));
    const enqueuePrepared = vi.fn(() => ({
      status: "accepted" as const,
      acceptedId: "queue-1",
    }));
    const sendMessage = vi.fn();
    const onAcceptedTarget = vi.fn();
    const requestPreparedFlush = vi.fn();

    function PreparedSendInput({ agentId }: { agentId: string }) {
      const send = useChatSend({
        resolvedAgentId: agentId,
        activeThreadId: null,
        sessions: [],
        messagesLength: 0,
        selectedModel: null,
        selectedConnectors: [],
        selectedKnowledgeBaseIds: [],
        reasoningMode: "auto",
        reasoningEffort: null,
        sendMessage,
        createSession,
        renameSession: vi.fn(),
        refreshPluginReadiness,
        enqueuePrepared,
        requestPreparedFlush,
        t: ((key: string) => key) as never,
      });
      return (
        <ChatInput
          agentId={agentId}
          beginSubmission={send.beginSubmission}
          onSend={(text, attachments, lease) =>
            send.handleSend(text, attachments, { submissionLease: lease })
          }
          onAcceptedTarget={onAcceptedTarget}
          onCancel={vi.fn()}
          onNewChat={vi.fn()}
          isStreaming={false}
          initialText="headless request"
        />
      );
    }

    const view = render(
      <MemoryRouter>
        <PreparedSendInput agentId="agent-old" />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "send" }));
    await waitFor(() => expect(refreshPluginReadiness).toHaveBeenCalledOnce());
    view.rerender(
      <MemoryRouter>
        <PreparedSendInput agentId="agent-new" />
      </MemoryRouter>,
    );
    await act(async () => {
      releaseReadiness({
        status: "ready",
        requiredPlugins: { "plugin-headless": "1" },
        unavailablePlugins: [],
      });
    });

    expect(createSession).not.toHaveBeenCalled();
    expect(enqueuePrepared).not.toHaveBeenCalled();
    expect(requestPreparedFlush).not.toHaveBeenCalled();
    expect(sendMessage).not.toHaveBeenCalled();
    expect(onAcceptedTarget).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox")).toHaveValue("");
  });

  it("rejects a headless prepared send resolved after input unmount without retaining its placeholder", async () => {
    vi.mocked(captureComposerPreparations).mockReturnValue([]);
    let releaseReadiness!: (value: {
      status: "ready";
      requiredPlugins: Record<string, string>;
      unavailablePlugins: never[];
    }) => void;
    let resolveThread!: (id: string) => void;
    const sessions = new Set<string>();
    const refreshPluginReadiness = vi.fn(
      () =>
        new Promise<{
          status: "ready";
          requiredPlugins: Record<string, string>;
          unavailablePlugins: never[];
        }>((resolve) => {
          releaseReadiness = resolve;
        }),
    );
    const createSession = vi.fn((options?: { isCurrent?: () => boolean }) => {
      sessions.add("__pending__");
      return {
        session: { id: "__pending__" } as never,
        resolvedId: new Promise<string>((resolve) => {
          resolveThread = (id) => {
            if (options?.isCurrent?.()) sessions.add(id);
            sessions.delete("__pending__");
            resolve(id);
          };
        }),
      };
    });
    const enqueuePrepared = vi.fn(() => ({
      status: "accepted" as const,
      acceptedId: "queue-1",
    }));
    const sendMessage = vi.fn();
    const onAcceptedTarget = vi.fn();
    const requestPreparedFlush = vi.fn();

    function PreparedSendInput() {
      const send = useChatSend({
        resolvedAgentId: "agent-old",
        activeThreadId: null,
        sessions: [],
        messagesLength: 0,
        selectedModel: null,
        selectedConnectors: [],
        selectedKnowledgeBaseIds: [],
        reasoningMode: "auto",
        reasoningEffort: null,
        sendMessage,
        createSession,
        renameSession: vi.fn(),
        refreshPluginReadiness,
        enqueuePrepared,
        requestPreparedFlush,
        t: ((key: string) => key) as never,
      });
      return (
        <ChatInput
          agentId="agent-old"
          beginSubmission={send.beginSubmission}
          onSend={(text, attachments, lease) =>
            send.handleSend(text, attachments, { submissionLease: lease })
          }
          onAcceptedTarget={onAcceptedTarget}
          onCancel={vi.fn()}
          onNewChat={vi.fn()}
          isStreaming={false}
          initialText="headless request"
        />
      );
    }

    const view = render(
      <MemoryRouter>
        <PreparedSendInput />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "send" }));
    await waitFor(() => expect(refreshPluginReadiness).toHaveBeenCalledOnce());
    await act(async () => {
      releaseReadiness({
        status: "ready",
        requiredPlugins: { "plugin-headless": "1" },
        unavailablePlugins: [],
      });
    });
    await waitFor(() => expect(createSession).toHaveBeenCalledOnce());
    expect(sessions).toContain("__pending__");
    view.unmount();
    await act(async () => {
      resolveThread("thread-created");
    });

    expect(sessions).toEqual(new Set());
    expect(enqueuePrepared).not.toHaveBeenCalled();
    expect(requestPreparedFlush).not.toHaveBeenCalled();
    expect(sendMessage).not.toHaveBeenCalled();
    expect(onAcceptedTarget).not.toHaveBeenCalled();
  });

  it("accepts the submitted snapshot but preserves a newer same-origin draft", async () => {
    vi.mocked(captureComposerPreparations).mockReturnValue([]);
    let releaseReadiness!: (value: {
      status: "ready";
      requiredPlugins: Record<string, string>;
      unavailablePlugins: never[];
    }) => void;
    const target = { agentId: "agent-real", threadId: "thread-created" };
    const refreshPluginReadiness = vi.fn(
      ({ threadId }: { threadId: string | null }) =>
        threadId
          ? Promise.resolve({
              status: "ready" as const,
              requiredPlugins: { "plugin-headless": "1" },
              unavailablePlugins: [],
            })
          : new Promise<{
              status: "ready";
              requiredPlugins: Record<string, string>;
              unavailablePlugins: never[];
            }>((resolve) => {
              releaseReadiness = resolve;
            }),
    );
    const createSession = vi.fn(() => ({
      session: { id: "__pending__" } as never,
      resolvedId: Promise.resolve(target.threadId),
    }));
    const enqueuePrepared = vi.fn(() => ({
      status: "accepted" as const,
      acceptedId: "queue-1",
      acceptedTarget: target,
    }));
    const sendMessage = vi.fn();
    const onAcceptedTarget = vi.fn();

    function PreparedSendInput() {
      const send = useChatSend({
        resolvedAgentId: target.agentId,
        activeThreadId: null,
        sessions: [],
        messagesLength: 0,
        selectedModel: null,
        selectedConnectors: [],
        selectedKnowledgeBaseIds: [],
        reasoningMode: "auto",
        reasoningEffort: null,
        sendMessage,
        createSession,
        renameSession: vi.fn(),
        refreshPluginReadiness,
        enqueuePrepared,
        requestPreparedFlush: vi.fn(),
        t: ((key: string) => key) as never,
      });
      return (
        <ChatInput
          agentId={target.agentId}
          beginSubmission={send.beginSubmission}
          onSend={(text, attachments, lease) =>
            send.handleSend(text, attachments, { submissionLease: lease })
          }
          onAcceptedTarget={onAcceptedTarget}
          onCancel={vi.fn()}
          onNewChat={vi.fn()}
          isStreaming={false}
          initialText="older request"
        />
      );
    }

    render(
      <MemoryRouter>
        <PreparedSendInput />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "send" }));
    await waitFor(() => expect(refreshPluginReadiness).toHaveBeenCalledOnce());
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "newer request" },
    });
    await act(async () => {
      releaseReadiness({
        status: "ready",
        requiredPlugins: { "plugin-headless": "1" },
        unavailablePlugins: [],
      });
    });
    await waitFor(() => expect(enqueuePrepared).toHaveBeenCalledOnce());

    expect(enqueuePrepared.mock.calls[0]?.[1]).toMatchObject({
      text: "older request",
      bound: true,
    });
    expect(screen.getByRole("textbox")).toHaveValue("newer request");
    expect(onAcceptedTarget).not.toHaveBeenCalled();
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("releases the shared lease on auth change while old readiness is pending", async () => {
    vi.mocked(captureComposerPreparations).mockReturnValue([]);
    const ready = {
      status: "ready" as const,
      requiredPlugins: {},
      unavailablePlugins: [],
    };
    const readinessResolvers: ((value: typeof ready) => void)[] = [];
    const refreshPluginReadiness = vi.fn(
      () =>
        new Promise<typeof ready>((resolve) => {
          readinessResolvers.push(resolve);
        }),
    );
    const createSession = vi.fn();
    const enqueuePrepared = vi.fn(() => ({
      status: "accepted" as const,
      acceptedId: "queue-1",
    }));
    const sendMessage = vi.fn();
    const onAcceptedTarget = vi.fn();
    const inFlight: Promise<{ status: "accepted" | "rejected" }>[] = [];

    function PreparedSendInput() {
      const send = useChatSend({
        resolvedAgentId: "agent-1",
        activeThreadId: "thread-1",
        sessions: [],
        messagesLength: 1,
        selectedModel: null,
        selectedConnectors: [],
        selectedKnowledgeBaseIds: [],
        reasoningMode: "auto",
        reasoningEffort: null,
        sendMessage,
        createSession,
        renameSession: vi.fn(),
        refreshPluginReadiness,
        enqueuePrepared,
        requestPreparedFlush: vi.fn(),
        t: ((key: string) => key) as never,
      });
      return (
        <ChatInput
          agentId="agent-1"
          threadId="thread-1"
          beginSubmission={send.beginSubmission}
          onSend={(text, attachments, lease) => {
            const pending = send.handleSend(text, attachments, {
              submissionLease: lease,
            });
            inFlight.push(pending);
            return pending;
          }}
          onAcceptedTarget={onAcceptedTarget}
          onCancel={vi.fn()}
          onNewChat={vi.fn()}
          isStreaming={false}
          initialText="old request"
        />
      );
    }

    render(
      <MemoryRouter>
        <PreparedSendInput />
      </MemoryRouter>,
    );
    const textbox = screen.getByRole("textbox");
    const sendButton = screen.getByRole("button", { name: "send" });
    fireEvent.click(sendButton);
    await waitFor(() => expect(refreshPluginReadiness).toHaveBeenCalledOnce());
    expect(inFlight).toHaveLength(1);

    act(() => invalidateAuthSession());
    await waitFor(() => expect(sendButton).not.toBeDisabled());
    fireEvent.change(textbox, { target: { value: "new request" } });
    fireEvent.click(sendButton);
    await waitFor(() =>
      expect(refreshPluginReadiness).toHaveBeenCalledTimes(2),
    );
    expect(inFlight).toHaveLength(2);

    await act(async () => {
      readinessResolvers[0]!(ready);
      await inFlight[0];
    });
    expect(enqueuePrepared).not.toHaveBeenCalled();
    expect(sendMessage).not.toHaveBeenCalled();
    expect(onAcceptedTarget).not.toHaveBeenCalled();

    fireEvent.keyDown(textbox, { key: "Enter" });
    await act(async () => {});
    expect(refreshPluginReadiness).toHaveBeenCalledTimes(2);
    expect(inFlight).toHaveLength(2);

    await act(async () => {
      readinessResolvers[1]!(ready);
      await inFlight[1];
    });
    expect(sendMessage).toHaveBeenCalledOnce();
    expect(enqueuePrepared).not.toHaveBeenCalled();
    expect(onAcceptedTarget).not.toHaveBeenCalled();
  });

  it("uses one submission lease for history edit and queue reclaim in both directions", async () => {
    vi.mocked(captureComposerPreparations).mockReturnValue([]);
    let releaseReadiness!: (value: {
      status: "ready";
      requiredPlugins: Record<string, string>;
      unavailablePlugins: never[];
    }) => void;
    let finishRestore!: () => void;
    const refreshPluginReadiness = vi.fn(
      () =>
        new Promise<{
          status: "ready";
          requiredPlugins: Record<string, string>;
          unavailablePlugins: never[];
        }>((resolve) => {
          releaseReadiness = resolve;
        }),
    );
    const enqueuePrepared = vi.fn(() => ({
      status: "accepted" as const,
      acceptedId: "queue-edit",
    }));
    const sendMessage = vi.fn();
    const restore = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finishRestore = resolve;
        }),
    );
    const remove = vi.fn();
    const item = {
      id: "queue-1",
      text: "saved queue text",
      target: { agentId: "agent-1", threadId: "thread-1" },
      attachments: [],
      status: "blocked",
      authEpoch: getAuthSessionEpoch(),
      pluginSelection: [
        {
          pluginId: "plugin-a",
          generation: "gen-a",
          selection: { state: "selected", value: { ids: ["one"] } },
        },
      ],
    } as never;
    let itemRef: typeof item | null = item;
    const onReclaimQueued = vi.fn(
      (
        id: string,
        commit: (queued: typeof item) => boolean,
        lease?: { isCurrent: () => boolean },
      ) =>
        reclaimQueueItemIfCurrent(id, {
          getById: () => itemRef,
          getOrigin: () => ({ agentId: "agent-1", threadId: "thread-1" }),
          restore,
          commit: (queued) => (!lease || lease.isCurrent()) && commit(queued),
          remove: (queuedId) => {
            itemRef = null;
            remove(queuedId);
          },
        }),
    );

    function PreparedSendInput() {
      const send = useChatSend({
        resolvedAgentId: "agent-1",
        activeThreadId: "thread-1",
        sessions: [],
        messagesLength: 1,
        selectedModel: null,
        selectedConnectors: [],
        selectedKnowledgeBaseIds: [],
        reasoningMode: "auto",
        reasoningEffort: null,
        sendMessage,
        createSession: vi.fn(),
        renameSession: vi.fn(),
        refreshPluginReadiness,
        enqueuePrepared,
        requestPreparedFlush: vi.fn(),
        t: ((key: string) => key) as never,
      });
      return (
        <>
          <button
            type="button"
            onClick={() =>
              void send.handleSend("history edit", undefined, {
                intent: "edit",
                threadId: "thread-1",
              })
            }
          >
            history edit
          </button>
          <ChatInput
            agentId="agent-1"
            threadId="thread-1"
            beginSubmission={send.beginSubmission}
            onSend={(text, attachments, lease) =>
              send.handleSend(text, attachments, { submissionLease: lease })
            }
            onReclaimQueued={onReclaimQueued}
            onRemoveQueued={vi.fn()}
            queuedItems={[item]}
            onCancel={vi.fn()}
            onNewChat={vi.fn()}
            isStreaming={false}
          />
        </>
      );
    }

    render(
      <MemoryRouter>
        <PreparedSendInput />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "history edit" }));
    await waitFor(() => expect(refreshPluginReadiness).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: "reclaim queue-1" }));
    await act(async () => {});
    expect(onReclaimQueued).not.toHaveBeenCalled();
    expect(restore).not.toHaveBeenCalled();
    await act(async () => {
      releaseReadiness({
        status: "ready",
        requiredPlugins: {},
        unavailablePlugins: [],
      });
    });
    await waitFor(() => expect(enqueuePrepared).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByRole("button", { name: "reclaim queue-1" }));
    await waitFor(() => expect(restore).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: "history edit" }));
    await act(async () => {});
    expect(refreshPluginReadiness).toHaveBeenCalledOnce();
    expect(enqueuePrepared).toHaveBeenCalledOnce();
    expect(itemRef).toBe(item);
    await act(async () => finishRestore());
    await waitFor(() =>
      expect(remove).toHaveBeenCalledExactlyOnceWith("queue-1"),
    );
    expect(screen.getByRole("textbox")).toHaveValue("saved queue text");
  });

  it("reclaims successfully when the draft changes before confirmation", async () => {
    let finishRestore!: () => void;
    const item = {
      id: "queue-1",
      text: "saved queue text",
      target: { agentId: "agent-1", threadId: "thread-1" },
      attachments: [],
      status: "blocked",
      authEpoch: getAuthSessionEpoch(),
      pluginSelection: [
        {
          pluginId: "plugin-a",
          generation: "gen-a",
          selection: { state: "selected", value: { ids: ["one"] } },
        },
      ],
    } as never;
    let itemRef: typeof item | null = item;
    const remove = vi.fn();
    const onReclaimQueued = vi.fn(
      (id: string, commit: (queued: typeof item) => boolean) =>
        reclaimQueueItemIfCurrent(id, {
          getById: () => itemRef,
          getOrigin: () => ({ agentId: "agent-1", threadId: "thread-1" }),
          restore: () =>
            new Promise<void>((resolve) => {
              finishRestore = resolve;
            }),
          commit,
          remove: (queuedId) => {
            itemRef = null;
            remove(queuedId);
          },
        }),
    );
    render(
      <ChatInput
        onSend={vi.fn(async () => ({ status: "accepted" as const }))}
        onReclaimQueued={onReclaimQueued}
        onRemoveQueued={vi.fn()}
        queuedItems={[item]}
        onCancel={vi.fn()}
        onNewChat={vi.fn()}
        isStreaming={false}
        agentId="agent-1"
        threadId="thread-1"
        initialText="existing draft"
      />,
    );
    const textbox = screen.getByRole("textbox");
    fireEvent.click(screen.getByRole("button", { name: "reclaim queue-1" }));
    const confirm = modalControl.confirm.mock.calls.at(-1)?.[0] as {
      onOk: () => Promise<void>;
    };
    fireEvent.change(textbox, { target: { value: "edited before confirm" } });
    act(() => {
      void confirm.onOk();
    });
    await waitFor(() => expect(onReclaimQueued).toHaveBeenCalledOnce());
    await act(async () => finishRestore());

    expect(textbox).toHaveValue("saved queue text");
    expect(remove).toHaveBeenCalledExactlyOnceWith("queue-1");
    expect(itemRef).toBeNull();
  });

  it("takes reclaim revision after confirmation and keeps edits made after confirmation", async () => {
    let finishRestore!: () => void;
    const item = {
      id: "queue-1",
      text: "saved queue text",
      target: { agentId: "agent-1", threadId: "thread-1" },
      attachments: [],
      status: "blocked",
      authEpoch: getAuthSessionEpoch(),
      pluginSelection: [
        {
          pluginId: "plugin-a",
          generation: "gen-a",
          selection: { state: "selected", value: { ids: ["one"] } },
        },
      ],
    } as never;
    let itemRef: typeof item | null = item;
    const remove = vi.fn();
    const onReclaimQueued = vi.fn(
      (id: string, commit: (queued: typeof item) => boolean) =>
        reclaimQueueItemIfCurrent(id, {
          getById: () => itemRef,
          getOrigin: () => ({ agentId: "agent-1", threadId: "thread-1" }),
          restore: () =>
            new Promise<void>((resolve) => {
              finishRestore = resolve;
            }),
          commit,
          remove: (queuedId) => {
            itemRef = null;
            remove(queuedId);
          },
        }),
    );
    render(
      <ChatInput
        onSend={vi.fn(async () => ({ status: "accepted" as const }))}
        onReclaimQueued={onReclaimQueued}
        onRemoveQueued={vi.fn()}
        queuedItems={[item]}
        onCancel={vi.fn()}
        onNewChat={vi.fn()}
        isStreaming={false}
        agentId="agent-1"
        threadId="thread-1"
        initialText="existing draft"
      />,
    );
    const textbox = screen.getByRole("textbox");
    fireEvent.click(screen.getByRole("button", { name: "reclaim queue-1" }));
    expect(onReclaimQueued).not.toHaveBeenCalled();
    const confirm = modalControl.confirm.mock.calls.at(-1)?.[0] as {
      onOk: () => Promise<void>;
    };
    fireEvent.change(textbox, { target: { value: "edited before confirm" } });
    act(() => {
      void confirm.onOk();
    });
    await waitFor(() => expect(onReclaimQueued).toHaveBeenCalledOnce());
    fireEvent.change(textbox, { target: { value: "edited after confirm" } });
    await act(async () => finishRestore());

    expect(textbox).toHaveValue("edited after confirm");
    expect(remove).not.toHaveBeenCalled();
    expect(itemRef).toBe(item);
  });

  it("does not commit an async queue reclaim over a newer draft", async () => {
    let finishRestore!: (applied: boolean) => void;
    const item = {
      id: "queue-1",
      text: "saved queued text",
      target: { agentId: "agent-1", threadId: "thread-1" },
      attachments: [],
      status: "blocked",
      composerContext: undefined,
      modelRef: null,
    } as never;
    const onReclaimQueued = vi.fn(
      (_id: string, commit: (queued: never) => boolean) =>
        new Promise<boolean>((resolve) => {
          finishRestore = () => resolve(commit(item));
        }),
    );
    render(
      <ChatInput
        onSend={vi.fn(async () => ({ status: "accepted" as const }))}
        onReclaimQueued={onReclaimQueued}
        onRemoveQueued={vi.fn()}
        queuedItems={[item]}
        onCancel={vi.fn()}
        onNewChat={vi.fn()}
        isStreaming={false}
        agentId="agent-1"
        threadId="thread-1"
      />,
    );
    const textbox = screen.getByRole("textbox") as HTMLTextAreaElement;
    fireEvent.click(screen.getByRole("button", { name: "reclaim queue-1" }));
    await act(async () => {});
    expect(onReclaimQueued).toHaveBeenCalledOnce();
    fireEvent.change(textbox, { target: { value: "new user draft" } });
    await act(async () => {
      finishRestore(false);
    });
    expect(textbox.value).toBe("new user draft");
  });

  it("does not infer acceptance from a void callback", async () => {
    const onSend = vi.fn();
    render(
      <ChatInput
        onSend={onSend}
        onCancel={vi.fn()}
        onNewChat={vi.fn()}
        isStreaming={false}
        initialText="unsent"
      />,
    );
    const textbox = screen.getByRole("textbox") as HTMLTextAreaElement;
    fireEvent.click(screen.getByRole("button", { name: "send" }));
    await act(async () => {});
    expect(onSend).toHaveBeenCalledOnce();
    expect(textbox.value).toBe("unsent");
  });

  it("preserves a newer attachment added while an older draft is being submitted", async () => {
    let resolveSend!: (value: { status: "accepted" }) => void;
    const onSend = vi.fn(
      () =>
        new Promise<{ status: "accepted" }>((resolve) => {
          resolveSend = resolve;
        }),
    );
    render(
      <ChatInput
        onSend={onSend}
        onCancel={vi.fn()}
        onNewChat={vi.fn()}
        isStreaming={false}
        initialText="older text"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "send" }));
    act(() => {
      attachmentControl.set?.([
        { url: "/uploads/newer.txt", filename: "newer.txt", kind: "file" },
      ]);
    });
    resolveSend({ status: "accepted" });
    await act(async () => {});

    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(
      "older text",
    );
    expect(screen.getByText("newer.txt")).toBeInTheDocument();
  });
});
