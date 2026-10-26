import { useCallback, useEffect, useLayoutEffect, useRef } from "react";
import type { TFunction } from "i18next";
import { useNavigate } from "react-router-dom";
import type { ChatAttachment, UserComposerContext } from "./useChat";
import {
  isPendingThread,
  syncSessionHitlPolicy,
  type Session,
} from "./useSessions";
import type { HitlSessionPolicy } from "../utils/hitlSessionPolicy";
import * as chatStore from "./chatStore";
import { EMPTY_CHAT_SESSION_KEY, PENDING_THREAD_ID } from "../constants";
import { clipThreadTitle } from "../utils/threadTitle";
import { message } from "@/utils/antdMessage";

import {
  buildComposerContext,
  buildUserMessage,
  resolveTurnModelRef,
} from "../utils/chatMessages";
import { parseSkillSlugsInText } from "../utils/skillSlash";
import type {
  EnqueueChatItemInput,
  NativeExecutionSnapshot,
  NativeTarget,
  SubmitResult,
} from "./useChatMessageQueue";
import type { TurnExecutionOptions } from "./chatStore";
import {
  captureComposerPreparations,
  prepareComposerContext,
} from "../../../plugins/toolRenderers/composerPreparation";
import type {
  PluginReadinessTarget,
  PluginToolUiReadiness,
} from "../../../plugins/toolRenderers/usePluginToolUis";
import {
  getAuthSessionEpoch,
  isAuthSessionCurrent,
  subscribeAuthSession,
} from "../../../api/authSession";
import { listPluginSurfaces } from "../../../plugins/toolRenderers/registry";

interface UseChatSendParams {
  resolvedAgentId: string | null | undefined;
  activeThreadId: string | null;
  sessions: Session[];
  messagesLength: number;
  selectedModel: string | null;
  selectedConnectors: string[];
  selectedKnowledgeBaseIds: string[];
  selectedTargetAgents?: string[];
  reasoningMode: "auto" | "enabled" | "disabled";
  reasoningEffort: string | null;
  conversationMode?: "ask" | "plan" | "craft";
  hitlPolicy?: HitlSessionPolicy;
  defaultModel?: string | null;
  sendMessage: (
    text: string,
    sessionKey: string,
    agentId: string,
    attachments?: ChatAttachment[],
    storeKey?: string,
    modelRef?: string | null,
    mcpServers?: string[] | null,
    knowledgeBaseIds?: string[] | null,
    targetAgentIds?: string[] | null,
    composerContext?: UserComposerContext,
    reasoningMode?: "auto" | "enabled" | "disabled",
    reasoningEffort?: string | null,
    conversationMode?: "ask" | "plan" | "craft" | null,
    hitlPolicy?: HitlSessionPolicy | null,
    executionOptions?: TurnExecutionOptions,
  ) => void;
  createSession: (options?: { isCurrent?: () => boolean }) => {
    session: Session;
    resolvedId: Promise<string>;
  };
  refreshPluginReadiness?: (
    target: PluginReadinessTarget,
  ) => Promise<PluginToolUiReadiness>;
  enqueuePrepared?: (
    target: NativeTarget,
    input: EnqueueChatItemInput,
  ) => SubmitResult;
  requestPreparedFlush?: (target: NativeTarget) => void;
  skipComposerPreparation?: boolean;
  markInitialNavDone?: (agentId: string) => void;
  renameSession: (id: string, name: string) => void;
  /** Called when auto-recording starts successfully from a pending message. */
  onAutoRecordingStarted?: (recordingId: string) => void;
  t: TFunction;
}

function deriveThreadTitle(msg: string): string {
  return clipThreadTitle(msg);
}

function freezeSnapshot<T>(value: T): T {
  if (value === null || typeof value !== "object") return value;
  const copy = Array.isArray(value)
    ? value.map((item) => freezeSnapshot(item))
    : Object.fromEntries(
        Object.entries(value as Record<string, unknown>).map(([key, item]) => [
          key,
          freezeSnapshot(item),
        ]),
      );
  return Object.freeze(copy) as T;
}

/** Optional composer snapshot used when flushing a queued message. */
export type ChatSendOverrides = {
  selectedModel?: string | null;
  selectedConnectors?: string[];
  selectedKnowledgeBaseIds?: string[];
  selectedTargetAgents?: string[];
  composerContext?: UserComposerContext;
  modelRef?: string | null;
  /** Send to this thread instead of the active one (queued flush). */
  threadId?: string | null;
  /** Send as this agent instead of the active one (queued flush). */
  agentId?: string | null;
  conversationMode?: "ask" | "plan" | "craft";
  hitlPolicy?: HitlSessionPolicy;
  executionOptions?: TurnExecutionOptions;
  skipComposerPreparation?: boolean;
  refreshPluginReadiness?: (
    target: PluginReadinessTarget,
  ) => Promise<PluginToolUiReadiness>;
  nativeOptions?: NativeExecutionSnapshot;
  intent?: "send" | "edit";
  queueIntent?: boolean;
  editMessageId?: string;
  submissionLease?: ChatSubmissionLease;
};

export type ChatSubmissionOrigin = {
  agentId: string;
  threadId: string | null;
};

export interface ChatSubmissionLease {
  readonly origin: ChatSubmissionOrigin;
  readonly authEpoch: number;
  readonly signal: AbortSignal;
  isCurrent(): boolean;
  abort(): void;
  release(): void;
}

export function useChatSend({
  resolvedAgentId,
  activeThreadId,
  sessions,
  messagesLength,
  selectedModel,
  selectedConnectors,
  selectedKnowledgeBaseIds,
  selectedTargetAgents = [],
  reasoningMode,
  reasoningEffort,
  conversationMode = "craft",
  hitlPolicy,
  defaultModel,
  sendMessage,
  createSession,
  refreshPluginReadiness,
  enqueuePrepared,
  requestPreparedFlush,
  markInitialNavDone,
  renameSession,
  onAutoRecordingStarted,
  t,
}: UseChatSendParams) {
  const navigate = useNavigate();
  const createdPluginThreadByOrigin = useRef(new Map<string, string>());
  const submissionRef = useRef<ChatSubmissionLease | null>(null);
  const currentOriginRef = useRef<ChatSubmissionOrigin>({
    agentId: resolvedAgentId ?? "",
    threadId: activeThreadId,
  });
  const mountedRef = useRef(false);

  useLayoutEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      const lease = submissionRef.current;
      lease?.abort();
      lease?.release();
    };
  }, []);

  useLayoutEffect(() => {
    const nextOrigin = {
      agentId: resolvedAgentId ?? "",
      threadId: activeThreadId,
    };
    currentOriginRef.current = nextOrigin;
    const lease = submissionRef.current;
    if (
      lease &&
      (lease.origin.agentId !== nextOrigin.agentId ||
        lease.origin.threadId !== nextOrigin.threadId)
    ) {
      lease.abort();
      lease.release();
    }
  }, [resolvedAgentId, activeThreadId]);

  const beginSubmission = useCallback(
    (origin = currentOriginRef.current): ChatSubmissionLease | null => {
      if (!mountedRef.current || submissionRef.current) return null;
      const actualOrigin = { ...origin };
      const authEpoch = getAuthSessionEpoch();
      const controller = new AbortController();
      let unsubscribeAuth = () => {};
      const lease: ChatSubmissionLease = {
        origin: actualOrigin,
        authEpoch,
        signal: controller.signal,
        isCurrent: () =>
          mountedRef.current &&
          isAuthSessionCurrent(authEpoch) &&
          !controller.signal.aborted &&
          currentOriginRef.current.agentId === actualOrigin.agentId &&
          currentOriginRef.current.threadId === actualOrigin.threadId,
        abort: () => controller.abort(),
        release: () => {
          unsubscribeAuth();
          if (submissionRef.current === lease) submissionRef.current = null;
        },
      };
      if (
        actualOrigin.agentId !== currentOriginRef.current.agentId ||
        actualOrigin.threadId !== currentOriginRef.current.threadId
      )
        return null;
      submissionRef.current = lease;
      unsubscribeAuth = subscribeAuthSession(() => {
        controller.abort();
        lease.release();
      });
      return lease;
    },
    [],
  );

  /** @returns false when the send was rejected before starting a turn. */
  const handleSend = useCallback(
    async (
      text: string,
      attachments?: ChatAttachment[],
      overrides?: ChatSendOverrides,
    ): Promise<SubmitResult> => {
      const submissionLease = overrides?.submissionLease ?? beginSubmission();
      if (!submissionLease) return { status: "rejected", reason: "pending" };
      const ownsLease = overrides?.submissionLease == null;
      if (!submissionLease.isCurrent()) {
        if (ownsLease) submissionLease.release();
        return { status: "rejected", reason: "cancelled" };
      }
      try {
        const agent = overrides?.agentId ?? resolvedAgentId;
        if (!agent) {
          message.warning(t("chat.pickAgent", "请先选择一个 Agent"));
          return { status: "rejected", reason: "blocked" };
        }

        const trimmed = text.trim();
        if (!trimmed && !(attachments && attachments.length > 0)) {
          return { status: "rejected", reason: "empty" };
        }
        const attachmentSnapshot = attachments?.length
          ? freezeSnapshot(attachments)
          : undefined;

        const maybeRenameNewThread = (tid: string, hadMessages: boolean) => {
          const current = sessions.find((s) => s.id === tid);
          if (current?.name === "New Chat" && !hadMessages) {
            renameSession(tid, deriveThreadTitle(trimmed));
          }
        };

        const mode = overrides?.conversationMode ?? conversationMode;
        const policy = overrides?.hitlPolicy ?? hitlPolicy;
        const restricted = mode === "ask" || mode === "plan";
        const connectors = restricted
          ? []
          : overrides?.selectedConnectors ?? selectedConnectors;
        const knowledgeBaseIds =
          overrides?.selectedKnowledgeBaseIds ?? selectedKnowledgeBaseIds;
        const targetAgents = restricted
          ? []
          : overrides?.selectedTargetAgents ?? selectedTargetAgents;
        const modelSelection =
          overrides?.selectedModel !== undefined
            ? overrides.selectedModel
            : selectedModel;

        const composerContext =
          overrides?.composerContext ??
          buildComposerContext({
            skills: restricted ? [] : parseSkillSlugsInText(trimmed),
            connectors,
            knowledgeBaseIds,
            targetAgents,
            selectedModel: modelSelection,
            reasoningMode:
              overrides?.composerContext?.reasoningMode ?? reasoningMode,
            reasoningEffort:
              overrides?.composerContext?.reasoningEffort ?? reasoningEffort,
          });

        const modelOverride =
          overrides?.modelRef !== undefined
            ? overrides.modelRef
            : resolveTurnModelRef(modelSelection, defaultModel);

        const runSend = (
          tid: string,
          hadMessages: boolean,
          executionOptions = overrides?.executionOptions,
        ) => {
          maybeRenameNewThread(tid, hadMessages);
          sendMessage(
            trimmed,
            "",
            agent,
            attachmentSnapshot,
            tid,
            modelOverride,
            connectors,
            knowledgeBaseIds,
            targetAgents,
            composerContext,
            composerContext?.reasoningMode ?? reasoningMode,
            composerContext?.reasoningEffort ?? reasoningEffort,
            mode,
            policy,
            executionOptions,
          );
        };

        const targetThreadId =
          overrides?.threadId !== undefined
            ? overrides.threadId
            : activeThreadId;

        if (targetThreadId && isPendingThread(targetThreadId)) {
          message.info(t("chat.creatingSession", "正在创建会话，请稍候"));
          return { status: "rejected", reason: "pending" };
        }

        let destinationThreadId = targetThreadId;
        let executionOptions = overrides?.executionOptions;
        const captured =
          overrides?.skipComposerPreparation ||
          overrides?.executionOptions?.pluginContext
            ? []
            : captureComposerPreparations();
        const authEpoch = submissionLease.authEpoch;
        const originKey = `${agent}:${
          targetThreadId ?? ""
        }:${authEpoch}:${captured.map((item) => item.generation).join(",")}`;
        const isCurrent = () =>
          submissionLease.isCurrent() &&
          captured.every((item) => item.isCurrent());
        let readiness: PluginToolUiReadiness | undefined;
        if (!overrides?.skipComposerPreparation) {
          try {
            const refreshReadiness =
              overrides?.refreshPluginReadiness ?? refreshPluginReadiness;
            if (!refreshReadiness) {
              return { status: "rejected", reason: "blocked" };
            }
            readiness = await refreshReadiness({
              agentId: agent,
              threadId: targetThreadId ?? null,
            });
            if (
              readiness.status !== "ready" ||
              readiness.unavailablePlugins.length > 0 ||
              !isCurrent()
            ) {
              return { status: "rejected", reason: "blocked" };
            }
            const requiredIds = Object.keys(readiness.requiredPlugins);
            const registeredComposerIds = new Set(
              listPluginSurfaces("composer").map(
                (registration) => registration.pluginId,
              ),
            );
            const capturedIds = new Set(
              captured.map((entry) => entry.pluginId),
            );
            if (
              requiredIds.some(
                (id) => registeredComposerIds.has(id) && !capturedIds.has(id),
              )
            ) {
              message.warning(
                t("chat.pluginNotReady", "必需的插件输入尚未就绪"),
              );
              return { status: "rejected", reason: "blocked" };
            }
            const bound = captured.length > 0 || requiredIds.length > 0;
            if (bound && !destinationThreadId) {
              destinationThreadId =
                createdPluginThreadByOrigin.current.get(originKey) ?? null;
              if (!destinationThreadId) {
                markInitialNavDone?.(agent);
                const created = createSession({ isCurrent });
                destinationThreadId = await created.resolvedId;
                if (!destinationThreadId || !isCurrent()) {
                  return { status: "rejected", reason: "cancelled" };
                }
                createdPluginThreadByOrigin.current.set(
                  originKey,
                  destinationThreadId,
                );
              }
              readiness = await refreshReadiness({
                agentId: agent,
                threadId: destinationThreadId,
              });
              if (
                readiness.status !== "ready" ||
                readiness.unavailablePlugins.length > 0 ||
                !isCurrent()
              ) {
                return { status: "rejected", reason: "blocked" };
              }
            }
            if (captured.length > 0 && !destinationThreadId) {
              return { status: "rejected", reason: "cancelled" };
            }
            if (!isCurrent()) {
              return { status: "rejected", reason: "cancelled" };
            }
            if (captured.length > 0) {
              const pluginContext = await prepareComposerContext(captured, {
                target: { agentId: agent, threadId: destinationThreadId! },
                text: trimmed,
                attachments: attachmentSnapshot ?? Object.freeze([]),
                signal: submissionLease.signal,
              });
              if (!isCurrent())
                return { status: "rejected", reason: "cancelled" };
              executionOptions = { ...executionOptions, pluginContext };
            }
            if (
              bound ||
              overrides?.queueIntent ||
              overrides?.intent === "edit"
            ) {
              if (!destinationThreadId || !enqueuePrepared) {
                return { status: "rejected", reason: "blocked" };
              }
              const target = { agentId: agent, threadId: destinationThreadId };
              const nativeOptions: NativeExecutionSnapshot = freezeSnapshot({
                modelRef: modelOverride,
                connectors,
                knowledgeBaseIds,
                targetAgentIds: targetAgents,
                reasoningMode: composerContext?.reasoningMode ?? reasoningMode,
                reasoningEffort:
                  composerContext?.reasoningEffort ?? reasoningEffort,
                conversationMode: mode,
                hitlPolicy: policy,
              });
              const accepted = enqueuePrepared(target, {
                text: trimmed,
                attachments: attachmentSnapshot,
                composerContext,
                modelRef: modelOverride,
                nativeOptions,
                authEpoch,
                pluginContext:
                  executionOptions?.pluginContext as EnqueueChatItemInput["pluginContext"],
                pluginSelection: captured.map(
                  ({ pluginId, generation, selection }) => ({
                    pluginId,
                    generation,
                    selection,
                  }),
                ),
                displayOrigin: targetThreadId
                  ? undefined
                  : { agentId: agent, threadId: null },
                editMessageId: overrides?.editMessageId,
                bound: bound || overrides?.intent === "edit",
              });
              if (accepted.status !== "accepted") return accepted;
              requestPreparedFlush?.(target);
              if (!targetThreadId)
                createdPluginThreadByOrigin.current.delete(originKey);
              return {
                ...accepted,
                acceptedTarget: targetThreadId ? undefined : target,
              };
            }
          } catch {
            return { status: "rejected", reason: "blocked" };
          }
        }

        if (targetThreadId) {
          const hadMessages =
            targetThreadId === activeThreadId
              ? messagesLength > 0
              : chatStore.getSnapshot(targetThreadId).messages.length > 0;
          const destination = destinationThreadId ?? targetThreadId;
          if (captured.length > 0 && !isCurrent()) {
            return { status: "rejected", reason: "cancelled" };
          }
          const destinationHasMessages =
            destination === activeThreadId
              ? messagesLength > 0
              : chatStore.getSnapshot(destination).messages.length > 0;
          runSend(
            destination,
            destinationHasMessages || hadMessages,
            executionOptions,
          );
          return { status: "accepted" };
        }

        if (captured.length > 0) {
          if (!destinationThreadId || !isCurrent()) {
            return { status: "rejected", reason: "cancelled" };
          }
          runSend(destinationThreadId, false, executionOptions);
          navigate(`/chat/${agent}/${destinationThreadId}`, { replace: true });
          createdPluginThreadByOrigin.current.delete(originKey);
          return { status: "accepted" };
        }

        const userMsg = buildUserMessage(
          trimmed,
          attachmentSnapshot,
          composerContext,
        );
        chatStore.appendUserMessage(EMPTY_CHAT_SESSION_KEY, userMsg);

        const { resolvedId } = createSession();
        const snap = chatStore.getSnapshot(EMPTY_CHAT_SESSION_KEY);
        // Break any leftover ``__pending__`` → previous thr_* alias so this new
        // chat cannot mutate / stream into an older thread's bucket.
        chatStore.detachSessionKey(PENDING_THREAD_ID);
        chatStore.setMessages(PENDING_THREAD_ID, snap.messages);
        chatStore.clearMessages(EMPTY_CHAT_SESSION_KEY);
        navigate(`/chat/${agent}/${PENDING_THREAD_ID}`);

        void resolvedId.then((tid) => {
          if (!tid) {
            chatStore.clearMessages(PENDING_THREAD_ID);
            navigate(`/chat/${agent}`, { replace: true });
            message.error(
              t("chat.createSessionFailed", "创建会话失败，请重试"),
            );
            return;
          }
          const currentSnap = chatStore.getSnapshot(PENDING_THREAD_ID);
          const hadMessages = currentSnap.messages.length > 1;
          chatStore.renameSessionKey(PENDING_THREAD_ID, tid);
          // ``sessions`` in this closure predates the thread just created, so the
          // name lookup in maybeRenameNewThread always misses here. A freshly
          // created thread has no title yet — rename it straight away.
          if (!hadMessages) renameSession(tid, deriveThreadTitle(trimmed));
          if (policy) syncSessionHitlPolicy(tid, policy);
          chatStore.sendTurn(
            tid,
            trimmed,
            agent,
            "",
            attachmentSnapshot,
            undefined,
            modelOverride,
            tid,
            connectors,
            knowledgeBaseIds,
            targetAgents,
            composerContext?.reasoningMode ?? reasoningMode,
            composerContext?.reasoningEffort ?? reasoningEffort,
            mode,
            policy,
            overrides?.executionOptions,
          );
          navigate(`/chat/${agent}/${tid}`, { replace: true });
        });
        return { status: "accepted" };
      } finally {
        if (ownsLease) submissionLease.release();
      }
    },
    [
      activeThreadId,
      sessions,
      messagesLength,
      sendMessage,
      createSession,
      renameSession,
      refreshPluginReadiness,
      enqueuePrepared,
      requestPreparedFlush,
      markInitialNavDone,
      navigate,
      resolvedAgentId,
      selectedModel,
      selectedConnectors,
      selectedKnowledgeBaseIds,
      selectedTargetAgents,
      reasoningMode,
      reasoningEffort,
      conversationMode,
      hitlPolicy,
      defaultModel,
      t,
      beginSubmission,
    ],
  );

  useEffect(() => {
    const consumePending = () => {
      const raw = localStorage.getItem("octop.pendingChatMessage");
      if (!raw) return;
      localStorage.removeItem("octop.pendingChatMessage");

      let textToSend = raw;
      let autoRecord = false;

      // Support JSON format: { text, agentId, autoRecord }
      try {
        const parsed = JSON.parse(raw);
        if (typeof parsed === "object" && parsed.text) {
          textToSend = parsed.text;
          autoRecord = Boolean(parsed.autoRecord);
        }
      } catch {
        // Not JSON — treat as plain text (legacy format)
      }

      setTimeout(() => handleSend(textToSend), 500);

      // If autoRecord flag is set, start browser recording after sending the message
      if (autoRecord) {
        setTimeout(async () => {
          try {
            const { browserApi } = await import("../../../api/modules/browser");
            const data = await browserApi.startRecording({
              name: activeThreadId
                ? `chat-${activeThreadId}`
                : "skill-recording",
            });
            if (data.ok) {
              // Notify the chat page that recording has started (update UI state)
              if (data.recordingId && onAutoRecordingStarted) {
                onAutoRecordingStarted(data.recordingId);
              }
              // Push a system message into the chat about recording started
              const { generateId } = await import(
                "../../../utils/messageParser"
              );
              const pushMsg = {
                id: generateId(),
                role: "assistant" as const,
                content: t("skillRecord.recordingStartedMessage"),
                timestamp: Date.now(),
                status: "done" as const,
              };
              // Append directly to chat store
              const { appendPushMessage } = await import("./chatStore");
              appendPushMessage(pushMsg.content);
            }
          } catch {
            // Silently ignore recording start failure; the user can manually start
          }
        }, 1500);
      }
    };

    consumePending();

    const handler = () => consumePending();
    window.addEventListener("octop:pending-chat-message", handler);
    return () =>
      window.removeEventListener("octop:pending-chat-message", handler);
  }, [handleSend, activeThreadId, onAutoRecordingStarted, t]);

  return { handleSend, beginSubmission };
}
