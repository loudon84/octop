import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CHAT_QUEUE_MAX_ITEMS,
  useChatMessageQueue,
} from "./useChatMessageQueue";
import { invalidateAuthSession } from "../../../api/authSession";

describe("useChatMessageQueue", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  const idleStreaming = () => false;

  it("enqueues FIFO and enforces the max size", () => {
    const onFlush = vi.fn(async () => ({ status: "accepted" as const }));
    const { result } = renderHook(() =>
      useChatMessageQueue({
        agentId: "agent-1",
        threadId: "thread-1",
        isStreaming: true,
        onFlush,
        subscribeStreamEnd: () => () => undefined,
        isThreadStreaming: idleStreaming,
      }),
    );

    act(() => {
      for (let i = 0; i < CHAT_QUEUE_MAX_ITEMS; i++) {
        expect(result.current.enqueue({ text: `msg-${i}` })).toMatchObject({
          status: "accepted",
        });
      }
    });
    expect(result.current.items).toHaveLength(CHAT_QUEUE_MAX_ITEMS);
    let overflow = { status: "accepted" as const };
    act(() => {
      overflow = result.current.enqueue({ text: "overflow" });
    });
    expect(overflow).toEqual({ status: "rejected", reason: "full" });
    expect(result.current.items[0]?.text).toBe("msg-0");
    expect(result.current.items.at(-1)?.text).toBe(
      `msg-${CHAT_QUEUE_MAX_ITEMS - 1}`,
    );
    expect(onFlush).not.toHaveBeenCalled();
  });

  it("isolates queues per thread", () => {
    const onFlush = vi.fn(async () => ({ status: "accepted" as const }));
    const { result, rerender } = renderHook(
      ({ threadId }) =>
        useChatMessageQueue({
          agentId: "agent-1",
          threadId,
          isStreaming: true,
          onFlush,
          subscribeStreamEnd: () => () => undefined,
          isThreadStreaming: idleStreaming,
        }),
      { initialProps: { threadId: "t1" } },
    );

    act(() => {
      expect(result.current.enqueue({ text: "one" })).toMatchObject({
        status: "accepted",
      });
    });
    expect(result.current.items).toHaveLength(1);

    rerender({ threadId: "t2" });
    expect(result.current.items).toHaveLength(0);
    act(() => {
      expect(result.current.enqueue({ text: "two" })).toMatchObject({
        status: "accepted",
      });
    });
    expect(result.current.items[0]?.text).toBe("two");

    rerender({ threadId: "t1" });
    expect(result.current.items[0]?.text).toBe("one");
  });

  it("flushes the head item when streaming ends", async () => {
    const onFlush = vi.fn(async () => ({ status: "accepted" as const }));
    const { result, rerender } = renderHook(
      ({ isStreaming }) =>
        useChatMessageQueue({
          agentId: "agent-1",
          threadId: "thread-1",
          isStreaming,
          onFlush,
          subscribeStreamEnd: () => () => undefined,
          isThreadStreaming: idleStreaming,
        }),
      { initialProps: { isStreaming: true } },
    );

    act(() => {
      result.current.enqueue({ text: "first" });
      result.current.enqueue({ text: "second" });
    });
    expect(result.current.items).toHaveLength(2);

    rerender({ isStreaming: false });
    await act(async () => {
      await vi.runAllTimersAsync();
    });

    expect(onFlush).toHaveBeenCalledTimes(2);
    expect(onFlush.mock.calls[0]?.[0]?.text).toBe("first");
    expect(onFlush.mock.calls[0]?.[1]).toMatchObject({
      agentId: "agent-1",
      threadId: "thread-1",
    });
    expect(result.current.items).toHaveLength(0);
  });

  it("does not flush another thread when switching away mid-stream", async () => {
    const onFlush = vi.fn(async () => ({ status: "accepted" as const }));
    const { result, rerender } = renderHook(
      ({ threadId, isStreaming }) =>
        useChatMessageQueue({
          agentId: "agent-1",
          threadId,
          isStreaming,
          onFlush,
          subscribeStreamEnd: () => () => undefined,
          isThreadStreaming: idleStreaming,
        }),
      { initialProps: { threadId: "t1", isStreaming: true } },
    );

    act(() => {
      result.current.enqueue({ text: "queued-on-t1" });
    });

    // Switch to idle t2 — must not flush t1's queue as a side effect.
    rerender({ threadId: "t2", isStreaming: false });
    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(onFlush).not.toHaveBeenCalled();

    // Returning to idle t1 resumes the pending queue (stream already ended).
    rerender({ threadId: "t1", isStreaming: false });
    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(onFlush).toHaveBeenCalledTimes(1);
    expect(onFlush.mock.calls[0]?.[0]?.text).toBe("queued-on-t1");
    expect(result.current.items).toHaveLength(0);
  });

  it("flushes a background thread on streamEnd while viewing another", async () => {
    const onFlush = vi.fn(async () => ({ status: "accepted" as const }));
    let emitEnd: ((sessionId: string) => void) | null = null;

    const { result, rerender } = renderHook(
      ({ threadId, isStreaming }) =>
        useChatMessageQueue({
          agentId: "agent-1",
          threadId,
          isStreaming,
          onFlush,
          subscribeStreamEnd: (listener) => {
            emitEnd = listener;
            return () => {
              emitEnd = null;
            };
          },
          isThreadStreaming: idleStreaming,
        }),
      { initialProps: { threadId: "t1", isStreaming: true } },
    );

    act(() => {
      result.current.enqueue({ text: "bg-msg" });
    });

    // Switch away; t1 still has a queue, active view is idle t2.
    rerender({ threadId: "t2", isStreaming: false });
    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(onFlush).not.toHaveBeenCalled();

    await act(async () => {
      emitEnd?.("t1");
      await vi.runAllTimersAsync();
    });

    expect(onFlush).toHaveBeenCalledTimes(1);
    expect(onFlush.mock.calls[0]?.[0]?.text).toBe("bg-msg");
    expect(onFlush.mock.calls[0]?.[1]?.threadId).toBe("t1");
  });

  it("continues the background FIFO after each terminal outcome releases its head", async () => {
    const onFlush = vi.fn(async () => ({ status: "accepted" as const }));
    let emitEnd: ((sessionId: string) => void) | null = null;
    const { result } = renderHook(() =>
      useChatMessageQueue({
        agentId: "agent-1",
        threadId: "thread-1",
        isStreaming: false,
        onFlush,
        subscribeStreamEnd: (listener) => {
          emitEnd = listener;
          return () => undefined;
        },
        isThreadStreaming: idleStreaming,
      }),
    );
    act(() => {
      result.current.enqueue({ text: "A" });
      result.current.enqueue({ text: "B" });
      emitEnd?.("thread-1");
    });
    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(onFlush.mock.calls.map(([item]) => item.text)).toEqual(["A", "B"]);
    expect(result.current.items).toHaveLength(0);
  });

  it("holds an explicit bound target flush while busy and dispatches its snapshot once idle", async () => {
    let streaming = true;
    let emitEnd: ((sessionId: string) => void) | null = null;
    const onFlush = vi.fn(async () => ({ status: "accepted" as const }));
    const nativeOptions = {
      modelRef: "model-target",
      connectors: ["connector-target"],
      knowledgeBaseIds: ["kb-target"],
      targetAgentIds: ["expert-target"],
      reasoningMode: "enabled" as const,
      reasoningEffort: "high",
      conversationMode: "plan" as const,
      hitlPolicy: { mode: "allow_tools" as const, tools: ["tool-target"] },
    };
    const target = { agentId: "agent-target", threadId: "thread-target" };
    const { result } = renderHook(() =>
      useChatMessageQueue({
        ...target,
        isStreaming: true,
        onFlush,
        subscribeStreamEnd: (listener) => {
          emitEnd = listener;
          return () => undefined;
        },
        isThreadStreaming: () => streaming,
      }),
    );

    act(() => {
      result.current.enqueue({ text: "prepared", bound: true, nativeOptions });
      result.current.requestFlush(target);
    });
    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(onFlush).not.toHaveBeenCalled();
    expect(result.current.items[0]).toMatchObject({
      status: "queued",
      bound: true,
      target,
      nativeOptions,
    });

    streaming = false;
    await act(async () => {
      emitEnd?.(target.threadId);
      await vi.runAllTimersAsync();
    });
    expect(onFlush).toHaveBeenCalledOnce();
    expect(onFlush.mock.calls[0]?.[0]).toMatchObject({
      text: "prepared",
      bound: true,
      target,
      nativeOptions,
    });
    expect(result.current.items).toHaveLength(0);
  });

  it("preserves an item enqueued after a background flush timer is scheduled", async () => {
    let emitEnd: ((sessionId: string) => void) | null = null;
    const onFlush = vi.fn(async () => ({ status: "accepted" as const }));
    const { result } = renderHook(() =>
      useChatMessageQueue({
        agentId: "agent-1",
        threadId: "thread-1",
        isStreaming: false,
        onFlush,
        subscribeStreamEnd: (listener) => {
          emitEnd = listener;
          return () => undefined;
        },
        isThreadStreaming: idleStreaming,
      }),
    );
    act(() => {
      result.current.enqueue({ text: "A" });
      emitEnd?.("thread-1");
      result.current.enqueue({ text: "B" });
    });
    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(onFlush.mock.calls.map(([item]) => item.text)).toEqual(["A", "B"]);
    expect(result.current.items).toHaveLength(0);
  });

  it("does not revive a removed head when a scheduled flush starts the next item", async () => {
    let emitEnd: ((sessionId: string) => void) | null = null;
    const onFlush = vi.fn(async () => ({ status: "accepted" as const }));
    const { result } = renderHook(() =>
      useChatMessageQueue({
        agentId: "agent-1",
        threadId: "thread-1",
        isStreaming: false,
        onFlush,
        subscribeStreamEnd: (listener) => {
          emitEnd = listener;
          return () => undefined;
        },
        isThreadStreaming: idleStreaming,
      }),
    );
    let headId = "";
    act(() => result.current.enqueue({ text: "A" }));
    act(() => result.current.enqueue({ text: "B" }));
    headId = result.current.items[0]!.id;
    act(() => {
      emitEnd?.("thread-1");
      result.current.remove(headId);
    });
    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(onFlush.mock.calls.map(([item]) => item.text)).toEqual(["B"]);
    expect(result.current.items).toHaveLength(0);
  });

  it("keeps the head blocked when asynchronous flush rejects", async () => {
    const onFlush = vi.fn(async () => ({
      status: "rejected" as const,
      reason: "blocked" as const,
    }));
    const { result, rerender } = renderHook(
      ({ isStreaming }) =>
        useChatMessageQueue({
          agentId: "agent-1",
          threadId: "thread-1",
          isStreaming,
          onFlush,
          subscribeStreamEnd: () => () => undefined,
          isThreadStreaming: idleStreaming,
        }),
      { initialProps: { isStreaming: true } },
    );

    act(() => {
      result.current.enqueue({ text: "retry-me" });
      result.current.enqueue({ text: "second" });
    });

    rerender({ isStreaming: false });
    await act(async () => {
      await vi.runAllTimersAsync();
    });

    expect(onFlush).toHaveBeenCalledTimes(1);
    expect(result.current.items).toHaveLength(2);
    expect(result.current.items[0]).toMatchObject({
      text: "retry-me",
      status: "blocked",
    });
    expect(result.current.items[1]?.text).toBe("second");
  });

  it("keeps uncertain execution visible until an explicit recovery action", async () => {
    const onFlush = vi.fn(async () => ({ status: "uncertain" as const }));
    const { result, rerender } = renderHook(
      ({ isStreaming }) =>
        useChatMessageQueue({
          agentId: "agent-1",
          threadId: "thread-1",
          isStreaming,
          onFlush,
          subscribeStreamEnd: () => () => undefined,
          isThreadStreaming: idleStreaming,
        }),
      { initialProps: { isStreaming: true } },
    );

    act(() => result.current.enqueue({ text: "check history first" }));
    rerender({ isStreaming: false });
    await act(async () => {
      await vi.runAllTimersAsync();
    });

    expect(onFlush).toHaveBeenCalledTimes(1);
    expect(result.current.items[0]).toMatchObject({
      text: "check history first",
      status: "uncertain",
    });
    rerender({ isStreaming: true });
    rerender({ isStreaming: false });
    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(onFlush).toHaveBeenCalledTimes(1);
  });

  it("skips dequeue while the target thread is still streaming", async () => {
    const onFlush = vi.fn(async () => ({ status: "accepted" as const }));
    const streamingById = new Map<string, boolean>([["thread-1", true]]);
    const { result, rerender } = renderHook(
      ({ isStreaming }) =>
        useChatMessageQueue({
          agentId: "agent-1",
          threadId: "thread-1",
          isStreaming,
          onFlush,
          subscribeStreamEnd: () => () => undefined,
          isThreadStreaming: (id) => streamingById.get(id) ?? false,
        }),
      { initialProps: { isStreaming: true } },
    );

    act(() => {
      result.current.enqueue({ text: "wait" });
    });

    rerender({ isStreaming: false });
    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(onFlush).not.toHaveBeenCalled();
    expect(result.current.items).toHaveLength(1);

    streamingById.set("thread-1", false);
    rerender({ isStreaming: true });
    rerender({ isStreaming: false });
    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(onFlush).toHaveBeenCalledTimes(1);
    expect(onFlush.mock.calls[0]?.[0]?.text).toBe("wait");
  });

  it("allows a later flush after a cancelled schedule", async () => {
    const onFlush = vi.fn(async () => ({ status: "accepted" as const }));
    const { result, rerender } = renderHook(
      ({ isStreaming }) =>
        useChatMessageQueue({
          agentId: "agent-1",
          threadId: "thread-1",
          isStreaming,
          onFlush,
          subscribeStreamEnd: () => () => undefined,
          isThreadStreaming: idleStreaming,
        }),
      { initialProps: { isStreaming: true } },
    );

    act(() => {
      result.current.enqueue({ text: "keep" });
    });

    rerender({ isStreaming: false });
    rerender({ isStreaming: true });
    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(onFlush).not.toHaveBeenCalled();

    rerender({ isStreaming: false });
    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(onFlush).toHaveBeenCalledTimes(1);
    expect(onFlush.mock.calls[0]?.[0]?.text).toBe("keep");
  });

  it("reclaims by id without dropping the head", () => {
    const onFlush = vi.fn(async () => ({ status: "accepted" as const }));
    const { result } = renderHook(() =>
      useChatMessageQueue({
        agentId: "agent-1",
        threadId: "thread-1",
        isStreaming: true,
        onFlush,
        subscribeStreamEnd: () => () => undefined,
        isThreadStreaming: idleStreaming,
      }),
    );

    act(() => {
      result.current.enqueue({ text: "keep" });
      result.current.enqueue({ text: "edit-me" });
    });
    const id = result.current.items[1]?.id;
    expect(id).toBeTruthy();

    let reclaimed = null as ReturnType<typeof result.current.reclaim>;
    act(() => {
      reclaimed = result.current.reclaim(id!);
    });
    expect(reclaimed?.text).toBe("edit-me");
    expect(result.current.items).toHaveLength(1);
    expect(result.current.items[0]?.text).toBe("keep");
  });

  it("defers flush while shouldDeferFlush is true, then flushes later", async () => {
    const onFlush = vi.fn(async () => ({ status: "accepted" as const }));
    let defer = true;
    const { result, rerender } = renderHook(
      ({ isStreaming }: { isStreaming: boolean }) =>
        useChatMessageQueue({
          agentId: "agent-1",
          threadId: "thread-1",
          isStreaming,
          onFlush,
          shouldDeferFlush: () => defer,
          subscribeStreamEnd: () => () => undefined,
          isThreadStreaming: idleStreaming,
        }),
      { initialProps: { isStreaming: true } },
    );

    act(() => {
      result.current.enqueue({ text: "after-ask" });
    });
    rerender({ isStreaming: false });
    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(onFlush).not.toHaveBeenCalled();
    expect(result.current.items).toHaveLength(1);

    defer = false;
    rerender({ isStreaming: true });
    rerender({ isStreaming: false });
    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(onFlush).toHaveBeenCalledTimes(1);
    expect(onFlush.mock.calls[0]?.[0]?.text).toBe("after-ask");
  });

  it("keeps an async in-flight head counted and retains blocked work without auto-retry", async () => {
    let resolveFlush!: (result: {
      status: "rejected";
      reason: "blocked";
    }) => void;
    const onFlush = vi.fn(
      () =>
        new Promise<{ status: "rejected"; reason: "blocked" }>((resolve) => {
          resolveFlush = resolve;
        }),
    );
    const { result, rerender } = renderHook(
      ({ isStreaming }) =>
        useChatMessageQueue({
          agentId: "agent-1",
          threadId: "thread-1",
          isStreaming,
          onFlush,
          subscribeStreamEnd: () => () => undefined,
          isThreadStreaming: idleStreaming,
        }),
      { initialProps: { isStreaming: true } },
    );
    act(() => {
      for (let i = 0; i < CHAT_QUEUE_MAX_ITEMS; i++) {
        result.current.enqueue({ text: `msg-${i}` });
      }
    });
    rerender({ isStreaming: false });
    act(() => {
      vi.runOnlyPendingTimers();
    });

    expect(onFlush).toHaveBeenCalledOnce();
    expect(result.current.items).toHaveLength(CHAT_QUEUE_MAX_ITEMS);
    expect(result.current.enqueue({ text: "overflow" })).toEqual({
      status: "rejected",
      reason: "full",
    });

    await act(async () => {
      resolveFlush({ status: "rejected", reason: "blocked" });
    });
    expect(result.current.items[0]).toMatchObject({
      text: "msg-0",
      status: "blocked",
    });
    expect(result.current.items.at(-1)?.text).toBe(
      `msg-${CHAT_QUEUE_MAX_ITEMS - 1}`,
    );

    act(() => {
      vi.runOnlyPendingTimers();
      rerender({ isStreaming: true });
      rerender({ isStreaming: false });
      vi.runOnlyPendingTimers();
    });
    expect(onFlush).toHaveBeenCalledOnce();
  });

  it("deep snapshots plugin selections, prepared context, and native options", () => {
    const { result } = renderHook(() =>
      useChatMessageQueue({
        agentId: "agent-1",
        threadId: "thread-1",
        isStreaming: true,
        onFlush: vi.fn(async () => ({ status: "accepted" as const })),
        subscribeStreamEnd: () => () => undefined,
        isThreadStreaming: idleStreaming,
      }),
    );
    const ids = ["kb-1"];
    const pluginSelection = [
      {
        pluginId: "plugin-a",
        generation: "g1",
        selection: { state: "selected" as const, value: { ids } },
      },
    ];
    const pluginContext = {
      "plugin-a": { operation_id: "op-1", metadata: { ids } },
    };
    const nativeOptions = { connectors: ["conn-1"] };
    act(() => {
      result.current.enqueue({
        text: "prepared",
        pluginSelection,
        pluginContext,
        nativeOptions,
      });
    });
    ids.push("kb-2");
    const item = result.current.items[0]!;
    expect(item.pluginSelection?.[0]?.selection).toEqual({
      state: "selected",
      value: { ids: ["kb-1"] },
    });
    expect(item.pluginContext?.["plugin-a"]?.metadata).toEqual({
      ids: ["kb-1"],
    });
    expect(item.nativeOptions).toEqual({ connectors: ["conn-1"] });
    expect(Object.isFrozen(item.pluginSelection?.[0]?.selection)).toBe(true);
    expect(Object.isFrozen(item.pluginContext?.["plugin-a"]?.metadata)).toBe(
      true,
    );
  });

  it("synchronously clears transient queued work when auth is invalidated", () => {
    const { result } = renderHook(() =>
      useChatMessageQueue({
        agentId: "agent-1",
        threadId: "thread-1",
        isStreaming: true,
        onFlush: vi.fn(async () => ({ status: "accepted" as const })),
        subscribeStreamEnd: () => () => undefined,
        isThreadStreaming: idleStreaming,
      }),
    );
    act(() => {
      result.current.enqueue({ text: "sensitive transient" });
    });
    expect(result.current.items).toHaveLength(1);
    act(() => {
      invalidateAuthSession();
    });
    expect(result.current.items).toHaveLength(0);
  });
});
