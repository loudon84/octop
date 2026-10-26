import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { useChatSend } from "./useChatSend";
import {
  captureComposerPreparations,
  prepareComposerContext,
} from "../../../plugins/toolRenderers/composerPreparation";
import { invalidateAuthSession } from "../../../api/authSession";

const { navigate } = vi.hoisted(() => ({ navigate: vi.fn() }));
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => navigate };
});
vi.mock("../../../plugins/toolRenderers/composerPreparation", () => ({
  captureComposerPreparations: vi.fn(),
  prepareComposerContext: vi.fn(),
}));
vi.mock("./chatStore", () => ({
  getSnapshot: () => ({ messages: [] }),
  appendUserMessage: vi.fn(),
  detachSessionKey: vi.fn(),
  setMessages: vi.fn(),
  clearMessages: vi.fn(),
  renameSessionKey: vi.fn(),
  sendTurn: vi.fn(async () => undefined),
}));

const captured = {
  pluginId: "plugin-a",
  generation: "gen-a",
  selection: { state: "selected", value: { ids: ["kb-1"] } },
  isCurrent: () => true,
  prepare: vi.fn(),
  restoreSelection: vi.fn(),
};

function setupHook(options?: {
  createThreadId?: string;
  activeThreadId?: string;
  createThreadPromise?: Promise<string>;
}) {
  const order: string[] = [];
  const sendMessage = vi.fn(() => {
    order.push("send");
  });
  const createSession = vi.fn(() => {
    order.push("create");
    return {
      session: { id: "__pending__" },
      resolvedId:
        options?.createThreadPromise ??
        Promise.resolve(
          options?.createThreadId !== undefined
            ? options.createThreadId
            : "thread-real",
        ),
    };
  });
  const refreshPluginReadiness = vi.fn(
    async ({ threadId }: { threadId: string | null }) => {
      order.push(`refresh:${threadId ?? "none"}`);
      return {
        status: "ready",
        requiredPlugins: {},
        unavailablePlugins: [],
      } as const;
    },
  );
  const enqueuePrepared = vi.fn((_target, _input) => ({
    status: "accepted" as const,
    acceptedId: "queued-native-1",
    acceptedTarget: _target,
  }));
  const requestPreparedFlush = vi.fn();
  const markInitialNavDone = vi.fn(() => {
    order.push("mark-nav");
  });
  const { result } = renderHook(
    () =>
      useChatSend({
        resolvedAgentId: "agent-real",
        activeThreadId: options?.activeThreadId ?? null,
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
        markInitialNavDone,
        t: ((key: string) => key) as never,
      }),
    { wrapper: MemoryRouter },
  );
  return {
    result,
    order,
    sendMessage,
    createSession,
    refreshPluginReadiness,
    enqueuePrepared,
    requestPreparedFlush,
    markInitialNavDone,
  };
}

describe("native plugin send preparation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("creates a real thread, prepares, and transfers bound ownership to the queue", async () => {
    vi.mocked(captureComposerPreparations).mockReturnValue([captured] as never);
    vi.mocked(prepareComposerContext).mockImplementation(
      async (_capture, input) => {
        expect(input.target).toEqual({
          agentId: "agent-real",
          threadId: "thread-real",
        });
        return { "plugin-a": { operation_id: "op-1" } } as never;
      },
    );
    const hook = setupHook();
    const result = await act(async () =>
      hook.result.current.handleSend(" native "),
    );
    expect(result).toEqual({
      status: "accepted",
      acceptedId: "queued-native-1",
      acceptedTarget: { agentId: "agent-real", threadId: "thread-real" },
    });
    expect(hook.order.indexOf("mark-nav")).toBeLessThan(
      hook.order.indexOf("create"),
    );
    expect(
      hook.refreshPluginReadiness.mock.calls.map(([target]) => target.threadId),
    ).toEqual([null, "thread-real"]);
    expect(prepareComposerContext).toHaveBeenCalledOnce();
    expect(hook.enqueuePrepared).toHaveBeenCalledWith(
      { agentId: "agent-real", threadId: "thread-real" },
      expect.objectContaining({
        text: "native",
        pluginContext: { "plugin-a": { operation_id: "op-1" } },
        bound: true,
        nativeOptions: expect.objectContaining({ conversationMode: "craft" }),
      }),
    );
    expect(hook.requestPreparedFlush).toHaveBeenCalledWith({
      agentId: "agent-real",
      threadId: "thread-real",
    });
    expect(hook.sendMessage).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
    expect(hook.order).toEqual([
      "refresh:none",
      "mark-nav",
      "create",
      "refresh:thread-real",
    ]);
  });

  it("does not send when native thread creation does not resolve", async () => {
    vi.mocked(captureComposerPreparations).mockReturnValue([captured] as never);
    vi.mocked(prepareComposerContext).mockResolvedValue({
      "plugin-a": { operation_id: "unused" },
    } as never);
    const hook = setupHook({ createThreadId: "" });
    const result = await act(async () =>
      hook.result.current.handleSend("native"),
    );
    expect(result).toEqual({ status: "rejected", reason: "cancelled" });
    expect(prepareComposerContext).not.toHaveBeenCalled();
    expect(hook.sendMessage).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("rejects a pending placeholder before readiness or plugin preparation", async () => {
    vi.mocked(captureComposerPreparations).mockReturnValue([captured] as never);
    const hook = setupHook({ activeThreadId: "__pending__" });
    const result = await act(async () =>
      hook.result.current.handleSend("native"),
    );
    expect(result).toEqual({ status: "rejected", reason: "pending" });
    expect(hook.refreshPluginReadiness).not.toHaveBeenCalled();
    expect(prepareComposerContext).not.toHaveBeenCalled();
    expect(hook.enqueuePrepared).not.toHaveBeenCalled();
  });

  it("rejects a real-thread resolution made stale while session creation is pending", async () => {
    vi.mocked(captureComposerPreparations).mockReturnValue([captured] as never);
    let resolveThread!: (threadId: string) => void;
    const createThreadPromise = new Promise<string>((resolve) => {
      resolveThread = resolve;
    });
    const hook = setupHook({ createThreadPromise });
    let pending!: Promise<unknown>;
    act(() => {
      pending = hook.result.current.handleSend("native");
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(hook.createSession).toHaveBeenCalledOnce();
    act(() => invalidateAuthSession());
    await act(async () => {
      resolveThread("thread-real");
      expect(await pending).toEqual({
        status: "rejected",
        reason: "cancelled",
      });
    });
    expect(prepareComposerContext).not.toHaveBeenCalled();
    expect(hook.enqueuePrepared).not.toHaveBeenCalled();
    expect(hook.sendMessage).not.toHaveBeenCalled();
  });

  it("reuses the same-origin real thread after preparation fails", async () => {
    vi.mocked(captureComposerPreparations).mockReturnValue([captured] as never);
    vi.mocked(prepareComposerContext)
      .mockRejectedValueOnce(new Error("prepare failed"))
      .mockResolvedValueOnce({
        "plugin-a": { operation_id: "op-retry" },
      } as never);
    const hook = setupHook();
    const first = await act(async () =>
      hook.result.current.handleSend("same origin"),
    );
    expect(first).toEqual({ status: "rejected", reason: "blocked" });
    const second = await act(async () =>
      hook.result.current.handleSend("same origin"),
    );
    expect(second.status).toBe("accepted");
    expect(hook.createSession).toHaveBeenCalledOnce();
    expect(hook.enqueuePrepared).toHaveBeenCalledWith(
      { agentId: "agent-real", threadId: "thread-real" },
      expect.any(Object),
    );
  });

  it("captures and freezes attachments before readiness can yield", async () => {
    vi.mocked(captureComposerPreparations).mockReturnValue([captured] as never);
    let releaseReadiness!: (value: {
      status: "ready";
      requiredPlugins: Record<string, never>;
      unavailablePlugins: never[];
    }) => void;
    const hook = setupHook();
    hook.refreshPluginReadiness.mockReturnValue(
      new Promise((resolve) => {
        releaseReadiness = resolve;
      }) as never,
    );
    const attachments = [{ name: "before.txt", url: "/before" }];
    let pending!: Promise<unknown>;
    act(() => {
      pending = hook.result.current.handleSend("native", attachments as never);
    });
    attachments[0].url = "/after";
    attachments.push({ name: "late.txt", url: "/late" });
    let duplicate!: unknown;
    await act(async () => {
      duplicate = await hook.result.current.handleSend("duplicate");
    });
    expect(duplicate).toEqual({ status: "rejected", reason: "pending" });
    await act(async () => {
      releaseReadiness({
        status: "ready",
        requiredPlugins: {},
        unavailablePlugins: [],
      });
      await pending;
    });
    expect(prepareComposerContext).toHaveBeenCalledWith(
      [captured],
      expect.objectContaining({
        attachments: [{ name: "before.txt", url: "/before" }],
      }),
    );
    const prepareInput = vi.mocked(prepareComposerContext).mock.calls[0][1];
    expect(Object.isFrozen(prepareInput.attachments)).toBe(true);
    expect(Object.isFrozen(prepareInput.attachments[0])).toBe(true);
  });

  it("holds history edits in the target queue before any truncate dispatch", async () => {
    vi.mocked(captureComposerPreparations).mockReturnValue([captured] as never);
    vi.mocked(prepareComposerContext).mockResolvedValue({
      "plugin-a": { operation_id: "edit-op" },
    } as never);
    const hook = setupHook();
    const result = await act(async () =>
      hook.result.current.handleSend("rewrite", undefined, {
        intent: "edit",
        editMessageId: "message-1",
        threadId: "thread-existing",
      }),
    );
    expect(result.status).toBe("accepted");
    expect(hook.enqueuePrepared).toHaveBeenCalledWith(
      { agentId: "agent-real", threadId: "thread-existing" },
      expect.objectContaining({
        editMessageId: "message-1",
        text: "rewrite",
        bound: true,
      }),
    );
    expect(hook.sendMessage).not.toHaveBeenCalled();
  });

  it("aborts an outstanding preparation synchronously when auth changes", async () => {
    vi.mocked(captureComposerPreparations).mockReturnValue([captured] as never);
    let resolvePreparation!: (value: Record<string, unknown>) => void;
    let preparationSignal!: AbortSignal;
    vi.mocked(prepareComposerContext).mockImplementation((_captures, input) => {
      preparationSignal = input.signal;
      return new Promise((resolve) => {
        resolvePreparation = resolve;
      }) as never;
    });
    const hook = setupHook();
    let pending!: Promise<unknown>;
    act(() => {
      pending = hook.result.current.handleSend("auth-bound");
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(prepareComposerContext).toHaveBeenCalledOnce();
    act(() => invalidateAuthSession());
    expect(preparationSignal.aborted).toBe(true);
    await act(async () => {
      resolvePreparation({ "plugin-a": { operation_id: "late" } });
      expect(await pending).toEqual({
        status: "rejected",
        reason: "cancelled",
      });
    });
    expect(hook.enqueuePrepared).not.toHaveBeenCalled();
    expect(hook.sendMessage).not.toHaveBeenCalled();
  });

  it("releases a canceled auth owner's slot without letting its finally clear the next lease", async () => {
    vi.mocked(captureComposerPreparations).mockReturnValue([]);
    const readiness = {
      status: "ready" as const,
      requiredPlugins: {},
      unavailablePlugins: [],
    };
    const resolveReadiness: ((value: typeof readiness) => void)[] = [];
    const hook = setupHook({ activeThreadId: "thread-current" });
    hook.refreshPluginReadiness.mockImplementation(
      () =>
        new Promise<typeof readiness>((resolve) => {
          resolveReadiness.push(resolve);
        }),
    );
    let oldSend!: ReturnType<typeof hook.result.current.handleSend>;
    act(() => {
      oldSend = hook.result.current.handleSend("old request");
    });
    await waitFor(() =>
      expect(hook.refreshPluginReadiness).toHaveBeenCalledOnce(),
    );

    act(() => invalidateAuthSession());
    let newSend!: ReturnType<typeof hook.result.current.handleSend>;
    act(() => {
      newSend = hook.result.current.handleSend("new request");
    });
    await waitFor(() =>
      expect(hook.refreshPluginReadiness).toHaveBeenCalledTimes(2),
    );

    await act(async () => {
      resolveReadiness[0]!(readiness);
      await oldSend;
    });
    expect(hook.enqueuePrepared).not.toHaveBeenCalled();
    expect(hook.sendMessage).not.toHaveBeenCalled();
    let thirdResult!: Awaited<
      ReturnType<typeof hook.result.current.handleSend>
    >;
    await act(async () => {
      thirdResult = await hook.result.current.handleSend("must not reenter");
    });
    expect(thirdResult).toEqual({ status: "rejected", reason: "pending" });
    expect(hook.refreshPluginReadiness).toHaveBeenCalledTimes(2);

    await act(async () => {
      resolveReadiness[1]!(readiness);
      await newSend;
    });
    expect(hook.sendMessage).toHaveBeenCalledOnce();
    expect(hook.sendMessage).toHaveBeenCalledWith(
      "new request",
      "",
      "agent-real",
      undefined,
      "thread-current",
      null,
      [],
      [],
      [],
      expect.any(Object),
      "auto",
      null,
      "craft",
      undefined,
      undefined,
    );
  });
});
