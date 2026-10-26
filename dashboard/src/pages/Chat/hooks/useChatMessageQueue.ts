import { useCallback, useEffect, useRef, useState } from "react";
import { generateId } from "../../../utils/messageParser";
import type { ChatAttachment, UserComposerContext } from "./sseHelpers";
import * as chatStore from "./chatStore";
import {
  getAuthSessionEpoch,
  subscribeAuthSession,
} from "../../../api/authSession";
import type {
  ComposerSelectionCapture,
  PluginJsonObject,
} from "../../../plugins/toolRenderers/types";
import type { HitlSessionPolicy } from "../utils/hitlSessionPolicy";

export const CHAT_QUEUE_MAX_ITEMS = 10;

export type SubmitResult =
  | {
      status: "accepted";
      acceptedTarget?: { agentId: string; threadId: string };
      acceptedId?: string;
    }
  | {
      status: "rejected";
      reason: "empty" | "full" | "pending" | "blocked" | "cancelled";
    };

export type QueuedChatItem = {
  id: string;
  text: string;
  attachments?: ChatAttachment[];
  composerContext?: UserComposerContext;
  modelRef?: string | null;
  createdAt: number;
  status: "queued" | "sending" | "blocked" | "uncertain";
  pluginSelection?: readonly {
    pluginId: string;
    generation: string;
    selection: ComposerSelectionCapture;
  }[];
  pluginContext?: Readonly<Record<string, PluginJsonObject>>;
  authEpoch: number;
  target: { agentId: string; threadId: string };
  displayOrigin?: { agentId: string; threadId: string | null };
  editMessageId?: string;
  bound: boolean;
  nativeOptions?: NativeExecutionSnapshot;
};

export type NativeExecutionSnapshot = Readonly<{
  modelRef: string | null;
  connectors: readonly string[];
  knowledgeBaseIds: readonly string[];
  targetAgentIds: readonly string[];
  reasoningMode: "auto" | "enabled" | "disabled";
  reasoningEffort: string | null;
  conversationMode: "ask" | "plan" | "craft";
  hitlPolicy?: HitlSessionPolicy;
}>;

export type EnqueueChatItemInput = {
  text: string;
  attachments?: ChatAttachment[];
  composerContext?: UserComposerContext;
  modelRef?: string | null;
  pluginSelection?: QueuedChatItem["pluginSelection"];
  pluginContext?: QueuedChatItem["pluginContext"];
  authEpoch?: number;
  nativeOptions?: NativeExecutionSnapshot;
  displayOrigin?: { agentId: string; threadId: string | null };
  editMessageId?: string;
  bound?: boolean;
};

export type NativeTarget = Readonly<{ agentId: string; threadId: string }>;

function snapshot<T>(value: T): T {
  if (value === null || typeof value !== "object") return value;
  const copy = Array.isArray(value)
    ? value.map((item) => snapshot(item))
    : Object.fromEntries(
        Object.entries(value as Record<string, unknown>).map(([key, item]) => [
          key,
          snapshot(item),
        ]),
      );
  return Object.freeze(copy) as T;
}

export type ChatQueueFlushContext = {
  agentId: string;
  threadId: string;
  queueKey: string;
};

export type ChatQueueFlushResult = SubmitResult | { status: "uncertain" };

/** Resolve only after the existing stream reaches a terminal outcome. */
export type ChatQueueFlushHandler = (
  item: QueuedChatItem,
  ctx: ChatQueueFlushContext,
) => Promise<ChatQueueFlushResult>;

export function threadQueueKey(
  agentId: string | null | undefined,
  threadId: string | null | undefined,
): string {
  return `${agentId ?? ""}:${threadId ?? ""}`;
}

export function parseThreadQueueKey(key: string): {
  agentId: string;
  threadId: string;
} {
  const idx = key.indexOf(":");
  if (idx < 0) return { agentId: key, threadId: "" };
  return { agentId: key.slice(0, idx), threadId: key.slice(idx + 1) };
}

function queueKeysForThread(
  queues: Record<string, QueuedChatItem[]>,
  threadId: string,
): string[] {
  if (!threadId) return [];
  const suffix = `:${threadId}`;
  return Object.keys(queues).filter(
    (k) => k.endsWith(suffix) && (queues[k]?.length ?? 0) > 0,
  );
}

function withoutQueueKey(
  prev: Record<string, QueuedChatItem[]>,
  key: string,
): Record<string, QueuedChatItem[]> {
  if (!(key in prev)) return prev;
  const { [key]: _, ...rest } = prev;
  return rest;
}

function defaultSubscribeStreamEnd(
  listener: (sessionId: string) => void,
): () => void {
  return chatStore.onStreamEvent((event) => {
    if (event.kind === "streamEnd") listener(event.sessionId);
  });
}

function defaultIsThreadStreaming(threadId: string): boolean {
  if (!threadId) return false;
  return chatStore.isSessionBusyForTurnDispatch(threadId);
}

interface UseChatMessageQueueParams {
  agentId: string | null | undefined;
  threadId: string | null | undefined;
  isStreaming: boolean;
  onFlush: ChatQueueFlushHandler;
  /**
   * When true, keep the queue intact (e.g. open HITL pause). Retries on the
   * next stream-end / idle edge after the blocker clears.
   */
  shouldDeferFlush?: (threadId: string) => boolean;
  /** Test seam — defaults to chatStore streamEnd events. */
  subscribeStreamEnd?: (listener: (sessionId: string) => void) => () => void;
  /** Test seam — defaults to chatStore session snapshot. */
  isThreadStreaming?: (threadId: string) => boolean;
}

/**
 * Per-thread FIFO message queue for typing while a turn is streaming.
 * Flushes when the active thread's `isStreaming` falls false, when a
 * chatStore `streamEnd` fires for a queued thread (incl. background), or
 * when returning to an idle thread that still has items.
 */
export function useChatMessageQueue({
  agentId,
  threadId,
  isStreaming,
  onFlush,
  shouldDeferFlush,
  subscribeStreamEnd = defaultSubscribeStreamEnd,
  isThreadStreaming = defaultIsThreadStreaming,
}: UseChatMessageQueueParams) {
  const [queues, setQueues] = useState<Record<string, QueuedChatItem[]>>({});
  const key = threadQueueKey(agentId, threadId);
  const items = queues[key] ?? [];

  const queuesRef = useRef(queues);
  queuesRef.current = queues;
  const onFlushRef = useRef(onFlush);
  onFlushRef.current = onFlush;
  const shouldDeferFlushRef = useRef(shouldDeferFlush);
  shouldDeferFlushRef.current = shouldDeferFlush;
  const isThreadStreamingRef = useRef(isThreadStreaming);
  isThreadStreamingRef.current = isThreadStreaming;
  const flushTimerRef = useRef(new Map<string, number>());
  /** Streaming edge is tracked per queue key so thread switches never mis-flush. */
  const streamStateRef = useRef<{ key: string; streaming: boolean }>({
    key,
    streaming: isStreaming,
  });

  const writeQueues = useCallback((next: Record<string, QueuedChatItem[]>) => {
    queuesRef.current = next;
    setQueues(next);
  }, []);

  const enqueueForTarget = useCallback(
    (target: NativeTarget, input: EnqueueChatItemInput): SubmitResult => {
      const trimmed = input.text.trim();
      const hasAttachments = Boolean(input.attachments?.length);
      if (!trimmed && !hasAttachments)
        return { status: "rejected", reason: "empty" };

      const targetKey = threadQueueKey(target.agentId, target.threadId);
      const latest = queuesRef.current[targetKey] ?? [];
      if (latest.length >= CHAT_QUEUE_MAX_ITEMS)
        return { status: "rejected", reason: "full" };

      const item: QueuedChatItem = {
        id: generateId(),
        text: trimmed,
        attachments: input.attachments?.length
          ? snapshot(input.attachments)
          : undefined,
        composerContext: input.composerContext
          ? snapshot(input.composerContext)
          : undefined,
        modelRef: input.modelRef ?? null,
        createdAt: Date.now(),
        status: "queued",
        target: { ...target },
        displayOrigin: input.displayOrigin,
        editMessageId: input.editMessageId,
        bound: input.bound ?? false,
        pluginSelection: input.pluginSelection
          ? snapshot(input.pluginSelection)
          : undefined,
        pluginContext: input.pluginContext
          ? snapshot(input.pluginContext)
          : undefined,
        authEpoch: input.authEpoch ?? getAuthSessionEpoch(),
        nativeOptions: input.nativeOptions
          ? snapshot(input.nativeOptions)
          : undefined,
      };
      writeQueues({ ...queuesRef.current, [targetKey]: [...latest, item] });
      return {
        status: "accepted",
        acceptedId: item.id,
        acceptedTarget: target,
      };
    },
    [writeQueues],
  );

  const enqueue = useCallback(
    (input: EnqueueChatItemInput) =>
      enqueueForTarget(
        { agentId: agentId ?? "", threadId: threadId ?? "" },
        input,
      ),
    [agentId, threadId, enqueueForTarget],
  );

  const remove = useCallback(
    (id: string) => {
      const current = queuesRef.current[key] ?? [];
      const next = current.filter((item) => item.id !== id);
      if (next.length === current.length) return;
      if (next.length === 0) {
        writeQueues(withoutQueueKey(queuesRef.current, key));
        return;
      }
      writeQueues({ ...queuesRef.current, [key]: next });
    },
    [key, writeQueues],
  );

  const setStatus = useCallback(
    (id: string, status: QueuedChatItem["status"]) => {
      const current = queuesRef.current[key] ?? [];
      const next = current.map((item) =>
        item.id === id ? { ...item, status } : item,
      );
      if (next.some((item, index) => item !== current[index])) {
        writeQueues({ ...queuesRef.current, [key]: next });
      }
    },
    [key, writeQueues],
  );

  const reclaim = useCallback(
    (id: string): QueuedChatItem | null => {
      const current = queuesRef.current[key] ?? [];
      const found = current.find((item) => item.id === id) ?? null;
      if (!found) return null;
      const next = current.filter((item) => item.id !== id);
      if (next.length === 0) {
        writeQueues(withoutQueueKey(queuesRef.current, key));
      } else {
        writeQueues({ ...queuesRef.current, [key]: next });
      }
      return found;
    },
    [key, writeQueues],
  );

  const clear = useCallback(() => {
    writeQueues(withoutQueueKey(queuesRef.current, key));
  }, [key, writeQueues]);

  const removeById = useCallback(
    (id: string) => {
      for (const [queueKey, current] of Object.entries(queuesRef.current)) {
        if (!current.some((item) => item.id === id)) continue;
        const next = current.filter((item) => item.id !== id);
        writeQueues(
          next.length
            ? { ...queuesRef.current, [queueKey]: next }
            : withoutQueueKey(queuesRef.current, queueKey),
        );
        return;
      }
    },
    [writeQueues],
  );

  const setStatusById = useCallback(
    (id: string, status: QueuedChatItem["status"]) => {
      for (const [queueKey, current] of Object.entries(queuesRef.current)) {
        if (!current.some((item) => item.id === id)) continue;
        writeQueues({
          ...queuesRef.current,
          [queueKey]: current.map((item) =>
            item.id === id ? { ...item, status } : item,
          ),
        });
        return;
      }
    },
    [writeQueues],
  );

  const getById = useCallback((id: string) => {
    for (const current of Object.values(queuesRef.current)) {
      const found = current.find((item) => item.id === id);
      if (found) return found;
    }
    return null;
  }, []);

  const inFlightRef = useRef(new Set<string>());

  const cancelScheduledFlush = useCallback((queueKey?: string) => {
    if (queueKey !== undefined) {
      const timer = flushTimerRef.current.get(queueKey);
      if (timer !== undefined) window.clearTimeout(timer);
      flushTimerRef.current.delete(queueKey);
      return;
    }
    for (const timer of flushTimerRef.current.values()) {
      window.clearTimeout(timer);
    }
    flushTimerRef.current.clear();
  }, []);

  useEffect(
    () =>
      subscribeAuthSession(() => {
        cancelScheduledFlush();
        inFlightRef.current.clear();
        writeQueues({});
      }),
    [cancelScheduledFlush, writeQueues],
  );

  const scheduleFlush = useCallback(
    (queueKey: string) => {
      cancelScheduledFlush(queueKey);
      const queued = queuesRef.current[queueKey] ?? [];
      if (queued.length === 0 || queued[0]?.status !== "queued") return;
      if (inFlightRef.current.has(queueKey)) return;
      const timer = window.setTimeout(() => {
        flushTimerRef.current.delete(queueKey);
        const currentBeforeDispatch = queuesRef.current[queueKey] ?? [];
        const head = currentBeforeDispatch[0];
        if (!head || head.status !== "queued") return;
        const { agentId: flushAgentId, threadId: flushThreadId } =
          parseThreadQueueKey(queueKey);
        // Another turn may have started between schedule and fire.
        if (flushThreadId && isThreadStreamingRef.current(flushThreadId)) {
          return;
        }
        // Open HITL (ask / approval) must not start a new turn — that cancels
        // the interrupt via PatchToolCallsMiddleware.
        if (
          flushThreadId &&
          shouldDeferFlushRef.current?.(flushThreadId) === true
        ) {
          return;
        }
        inFlightRef.current.add(queueKey);
        writeQueues({
          ...queuesRef.current,
          [queueKey]: currentBeforeDispatch.map((item) =>
            item.id === head.id ? { ...item, status: "sending" } : item,
          ),
        });
        let flushResult: Promise<ChatQueueFlushResult>;
        try {
          flushResult = onFlushRef.current(head, {
            agentId: flushAgentId,
            threadId: flushThreadId,
            queueKey,
          });
        } catch {
          flushResult = Promise.reject(new Error("Queue flush failed"));
        }
        void Promise.resolve(flushResult)
          .then((result) => {
            const current = queuesRef.current[queueKey] ?? [];
            if (!current.some((item) => item.id === head.id)) return;
            if (result?.status === "accepted") {
              const next = current.filter((item) => item.id !== head.id);
              writeQueues(
                next.length
                  ? { ...queuesRef.current, [queueKey]: next }
                  : withoutQueueKey(queuesRef.current, queueKey),
              );
            } else {
              const status =
                result?.status === "uncertain" ? "uncertain" : "blocked";
              writeQueues({
                ...queuesRef.current,
                [queueKey]: current.map((item) =>
                  item.id === head.id ? { ...item, status } : item,
                ),
              });
            }
          })
          .catch(() => {
            const current = queuesRef.current[queueKey] ?? [];
            if (current.some((item) => item.id === head.id)) {
              writeQueues({
                ...queuesRef.current,
                [queueKey]: current.map((item) =>
                  item.id === head.id ? { ...item, status: "uncertain" } : item,
                ),
              });
            }
          })
          .finally(() => {
            inFlightRef.current.delete(queueKey);
            if (queuesRef.current[queueKey]?.[0]?.status === "queued") {
              scheduleFlush(queueKey);
            }
          });
      }, 0);
      flushTimerRef.current.set(queueKey, timer);
    },
    [cancelScheduledFlush, writeQueues],
  );

  const requestFlush = useCallback(
    (target: NativeTarget) => {
      scheduleFlush(threadQueueKey(target.agentId, target.threadId));
    },
    [scheduleFlush],
  );

  useEffect(() => {
    const prev = streamStateRef.current;
    streamStateRef.current = { key, streaming: isStreaming };

    if (prev.key !== key) {
      // Returning to an idle thread that still has a queue — resume sending.
      if (!isStreaming) {
        scheduleFlush(key);
      } else {
        cancelScheduledFlush(key);
      }
    } else if (prev.streaming && !isStreaming) {
      // Same thread: only flush on true → false.
      scheduleFlush(key);
    } else if (isStreaming) {
      // Stream resumed before the timer fired — keep the queue intact.
      cancelScheduledFlush(key);
    }

    return () => cancelScheduledFlush(key);
  }, [key, isStreaming, scheduleFlush, cancelScheduledFlush]);

  // Background (and active) threads: flush when chatStore reports streamEnd.
  useEffect(() => {
    return subscribeStreamEnd((sessionId) => {
      for (const queueKey of queueKeysForThread(queuesRef.current, sessionId)) {
        scheduleFlush(queueKey);
      }
    });
  }, [subscribeStreamEnd, scheduleFlush]);

  return {
    items,
    allItems: Object.values(queues).flat(),
    enqueue,
    enqueueForTarget,
    requestFlush,
    remove,
    removeById,
    setStatus,
    setStatusById,
    getById,
    reclaim,
    clear,
  };
}
