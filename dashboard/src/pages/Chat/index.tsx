import {
  useState,
  useCallback,
  useEffect,
  useRef,
  useMemo,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams, useLocation } from "react-router-dom";
import {
  PanelLeftOpen,
  GraduationCap,
  Users,
  Globe,
  FilePen,
  Terminal,
  FolderOpen,
  Activity,
} from "lucide-react";
import { Alert, Button, Tooltip } from "antd";
import { message as antMessage } from "@/utils/antdMessage";
import { showConfirmModal } from "../../utils/confirmModal";
import PlanReadyCard from "./components/PlanReadyCard";
import { useIsMobile } from "../../hooks/useIsMobile";
import { useHitlEnabled } from "../../hooks/useHitlEnabled";
import { useCurrentUser } from "../../hooks/useCurrentUser";
import { navAllowed, userCan } from "../../utils/permissions";
import { useChat } from "./hooks/useChat";
import {
  useSessions,
  fetchAndSyncSessionArtifacts,
  isPendingThread,
} from "./hooks/useSessions";
import * as chatStore from "./hooks/chatStore";
import { formatRunUsage, assistantTurnsFromEnd } from "./utils/chatMessages";
import { useChatSidebarState } from "./hooks/useChatSidebarState";
import { useChatHistoryRail } from "./hooks/useChatHistoryRail";
import { useChatDockPanel } from "./hooks/useChatDockPanel";
import { useChatSend, type ChatSendOverrides } from "./hooks/useChatSend";
import {
  useChatMessageQueue,
  type ChatQueueFlushContext,
  type ChatQueueFlushResult,
  type EnqueueChatItemInput,
  type NativeTarget,
  type QueuedChatItem,
  type SubmitResult,
} from "./hooks/useChatMessageQueue";
import { useChatNavigation } from "./hooks/useChatNavigation";
import { useChatSessionActions } from "./hooks/useChatSessionActions";

import { useChatComposerResources } from "./hooks/useChatComposerResources";
import type { HitlSessionPolicy } from "./utils/hitlSessionPolicy";
import {
  mergeAllowTools,
  parseHitlSessionPolicy,
} from "./utils/hitlSessionPolicy";
import { useChatContextWindow } from "./hooks/useChatContextWindow";
import {
  disableProviderStreamUsage,
  providerIdForTurn,
} from "../../utils/disableProviderStreamUsage";
import { useBrowserToolDetection } from "./hooks/useBrowserToolDetection";
import { useSkillRecordingWorkflow } from "./hooks/useSkillRecordingWorkflow";
import { listDockFilePathsForTree } from "./utils/dockFilePath";
import {
  shouldJumpToChromeInstall,
  WORKBENCH_BROWSER_PATH,
} from "./utils/chromeInstallGate";
import { isFileToolName } from "./constants";
import { browserApi } from "../../api/modules/browser";
import { octopThreadsApi } from "../../api/modules/octopThreads";
import type { TokenUsage } from "../../api/types";
import type { ChatAttachment } from "./hooks/useChat";
import MessageList from "./components/MessageList";
import ChatInput, { type ChatInputHandle } from "./components/ChatInput";
import WelcomeScreen from "./components/WelcomeScreen";
import AgentNotReadyScreen from "./components/AgentNotReadyScreen";
import ModelConfigEmpty from "./components/ModelConfigEmpty";
import AgentProfileDrawer from "../../components/AgentProfileDrawer";
import TrajectoryDrawer from "./components/TrajectoryDrawer";
import { useExpertChatWelcome } from "./hooks/useExpertQuickCards";
import { useSkills } from "../Agent/Skills/useSkills";
import { useChatSubagents } from "./hooks/useChatSubagents";
import {
  useAgent,
  selectEnabledExperts,
  projectChatAgentOption,
} from "../../context/AgentContext";
import { useLayoutMode } from "../../context/LayoutModeContext";
import { useBrowserSessionState } from "../../hooks/useBrowserSessionState";
import { prefetchVoiceConfig } from "../../hooks/useVoiceConfig";
import {
  chatSkillCatalogAgentId,
  isSharedExpertViewer,
} from "../../utils/sharedExpert";
import { isTeamAgent } from "../../utils/teamAgent";
import ChatDockPanels from "./components/ChatDockPanels";
import type { ChatDockAddTabHandlers } from "./components/ChatDockPanel";
import { ChatFilePreviewProvider } from "./ChatFilePreviewContext";
import { ChatAgentProfileProvider } from "./ChatAgentProfileContext";
import {
  ChatToolDockProvider,
  dockTabIdForToolCall,
} from "./ChatToolDockContext";
import ChatSidebarPanel from "./components/ChatSidebarPanel";
import ChatTitleBar from "./components/ChatTitleBar";
import TeamChatBadge from "./components/TeamChatBadge";
import RemoteExpertHint from "./components/RemoteExpertHint";
import ChatComposerChrome from "./components/ChatComposerChrome";
import AskQuestionCard from "./components/AskQuestionCard";
import {
  findAutoResumableApproval,
  findPendingAsk,
  hasPendingHitl,
  type PendingAsk,
} from "./utils/pendingHitl";
import { isAgentChatReady } from "../../utils/agentError";
import { useMemoryMaintenance } from "./hooks/useMemoryMaintenance";
import { restoreComposerSelections } from "../../plugins/toolRenderers/composerPreparation";
import { listPluginSurfaces } from "../../plugins/toolRenderers/registry";
import { isAuthSessionCurrent } from "../../api/authSession";
import MemoryMaintenanceBanner from "./components/MemoryMaintenanceBanner";
import { useHistoryMigration } from "./hooks/useHistoryMigration";
import HistoryMigrationBanner from "./components/HistoryMigrationBanner";
import { apiErrorMessage } from "../../utils/apiError";
import PwaInstallPrompt from "../../components/PwaInstallPrompt";
import { promptNeedsUserInput } from "../../utils/quickInputPrefill";
import { OPEN_NAV_RECORDS_EVENT } from "../../layouts/chatHistoryRail";
import {
  usePluginToolUis,
  setPluginUiDockHandlers,
} from "../../plugins/toolRenderers";
import { PluginSurface } from "../../plugins/toolRenderers/PluginSurface";
import { PluginReadinessNotice } from "../../plugins/toolRenderers/PluginReadinessNotice";
import type {
  PluginReadinessTarget,
  PluginToolUiReadiness,
} from "../../plugins/toolRenderers/usePluginToolUis";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import styles from "./index.module.less";

export default function ChatPage() {
  return <ChatPageInner />;
}

export function requestBoundQueueItemFlush(
  isBound: boolean,
  target: NativeTarget,
  requestFlush: (target: NativeTarget) => void,
): boolean {
  if (!isBound) return false;
  requestFlush(target);
  return true;
}

export async function routeGuardedChatSend(args: {
  text: string;
  attachments?: ChatAttachment[];
  overrides?: ChatSendOverrides;
  activeThreadId: string | null;
  pendingAsk: PendingAsk | null;
  hasPendingHitlPause: boolean;
  findPendingAskForThread: (threadId: string) => PendingAsk | null;
  hasPendingHitlForThread: (threadId: string) => boolean;
  interceptUserMessage: (text: string) => boolean;
  resumeHitl: (
    actions: { type: "respond"; message: string }[],
    threadId?: string,
    pendingId?: string,
  ) => void;
  handleSend: (
    text: string,
    attachments?: ChatAttachment[],
    overrides?: ChatSendOverrides,
  ) => Promise<SubmitResult>;
  warn: (key: string) => void;
}): Promise<SubmitResult> {
  const {
    text,
    attachments,
    overrides,
    activeThreadId,
    pendingAsk,
    hasPendingHitlPause,
    findPendingAskForThread,
    hasPendingHitlForThread,
    interceptUserMessage,
    resumeHitl,
    handleSend,
    warn,
  } = args;
  if (interceptUserMessage(text)) return { status: "accepted" };

  const targetThreadId = overrides?.threadId ?? activeThreadId ?? undefined;
  const ask =
    targetThreadId && targetThreadId === activeThreadId
      ? pendingAsk
      : targetThreadId
      ? findPendingAskForThread(targetThreadId)
      : pendingAsk;
  if (ask) {
    const trimmed = text.trim();
    if (attachments && attachments.length > 0) {
      warn("chat.ask.useCardForAttachments");
      return { status: "rejected", reason: "blocked" };
    }
    if (!trimmed) return { status: "rejected", reason: "empty" };
    resumeHitl(
      ask.actions.map(() => ({ type: "respond", message: trimmed })),
      targetThreadId,
      ask.pendingId,
    );
    return { status: "accepted" };
  }

  const blocked =
    targetThreadId && targetThreadId === activeThreadId
      ? hasPendingHitlPause
      : targetThreadId
      ? hasPendingHitlForThread(targetThreadId)
      : hasPendingHitlPause;
  if (blocked) {
    warn("chat.hitl.finishPendingFirst");
    return { status: "rejected", reason: "blocked" };
  }
  return await handleSend(text, attachments, overrides);
}

export async function dispatchPreparedQueueItem(
  item: QueuedChatItem,
  target: NativeTarget,
  refresh: (target: NativeTarget) => Promise<PluginToolUiReadiness>,
  sendMessage: ReturnType<typeof useChat>["sendMessage"],
  editAndResend: ReturnType<typeof useChat>["editAndResend"],
): Promise<ChatQueueFlushResult> {
  if (!target.threadId) return { status: "rejected", reason: "blocked" };
  if (!isAuthSessionCurrent(item.authEpoch)) {
    return { status: "rejected", reason: "cancelled" };
  }
  const readiness = await refresh(target);
  if (
    readiness.status !== "ready" ||
    readiness.unavailablePlugins.length > 0 ||
    !isAuthSessionCurrent(item.authEpoch)
  )
    return { status: "rejected", reason: "blocked" };
  if (chatStore.isSessionBusyForTurnDispatch(target.threadId, target.agentId)) {
    return { status: "rejected", reason: "blocked" };
  }
  const composerIds = new Set(
    listPluginSurfaces("composer").map((entry) => entry.pluginId),
  );
  if (
    Object.keys(readiness.requiredPlugins).some(
      (pluginId) =>
        composerIds.has(pluginId) && !item.pluginContext?.[pluginId],
    )
  )
    return { status: "rejected", reason: "blocked" };
  const native = item.nativeOptions;
  return await new Promise<ChatQueueFlushResult>((resolve) => {
    let outcomeSettled = false;
    const settleOutcome = (outcome: "complete" | "rejected" | "uncertain") => {
      if (outcomeSettled) return;
      outcomeSettled = true;
      resolve(
        outcome === "complete"
          ? { status: "accepted" }
          : outcome === "rejected"
          ? { status: "rejected", reason: "blocked" }
          : { status: "uncertain" },
      );
    };
    const executionOptions = {
      ...(item.pluginContext ? { pluginContext: item.pluginContext } : {}),
      ...(item.bound || item.editMessageId
        ? { delivery: "follow_up" as const }
        : {}),
      onOutcome: settleOutcome,
    };
    try {
      if (item.editMessageId) {
        const accepted = editAndResend(
          item.editMessageId,
          item.text,
          target.agentId,
          {
            targetThreadId: target.threadId,
            modelRef: native?.modelRef ?? item.modelRef,
            mcpServers: native?.connectors
              ? [...native.connectors]
              : item.composerContext?.connectors,
            knowledgeBaseIds: native?.knowledgeBaseIds
              ? [...native.knowledgeBaseIds]
              : item.composerContext?.knowledgeBaseIds,
            targetAgentIds: native?.targetAgentIds
              ? [...native.targetAgentIds]
              : item.composerContext?.targetAgents,
            composerContext: item.composerContext,
            reasoningMode:
              native?.reasoningMode ?? item.composerContext?.reasoningMode,
            reasoningEffort:
              native?.reasoningEffort ?? item.composerContext?.reasoningEffort,
            conversationMode: native?.conversationMode,
            hitlPolicy: native?.hitlPolicy,
            executionOptions,
          },
        );
        if (!accepted) {
          outcomeSettled = true;
          resolve({ status: "rejected", reason: "blocked" });
        }
      } else {
        sendMessage(
          item.text,
          "",
          target.agentId,
          item.attachments,
          target.threadId,
          native?.modelRef ?? item.modelRef,
          native?.connectors
            ? [...native.connectors]
            : item.composerContext?.connectors,
          native?.knowledgeBaseIds
            ? [...native.knowledgeBaseIds]
            : item.composerContext?.knowledgeBaseIds,
          native?.targetAgentIds
            ? [...native.targetAgentIds]
            : item.composerContext?.targetAgents,
          item.composerContext,
          native?.reasoningMode ?? item.composerContext?.reasoningMode,
          native?.reasoningEffort ?? item.composerContext?.reasoningEffort,
          native?.conversationMode,
          native?.hitlPolicy,
          executionOptions,
        );
      }
    } catch {
      outcomeSettled = true;
      resolve({ status: "uncertain" });
    }
  });
}

export async function reclaimQueueItemIfCurrent(
  id: string,
  deps: {
    getById: (id: string) => QueuedChatItem | null;
    getOrigin: () => { agentId: string; threadId: string };
    restore: (
      selection: NonNullable<QueuedChatItem["pluginSelection"]>,
    ) => Promise<void>;
    commit: (item: QueuedChatItem) => boolean;
    remove: (id: string) => void;
  },
): Promise<boolean> {
  const item = deps.getById(id);
  const origin = deps.getOrigin();
  if (
    !item ||
    !isAuthSessionCurrent(item.authEpoch) ||
    item.status === "sending" ||
    item.target.agentId !== origin.agentId ||
    item.target.threadId !== origin.threadId
  )
    return false;
  try {
    if (item.pluginSelection?.length) await deps.restore(item.pluginSelection);
  } catch {
    return false;
  }
  const latest = deps.getById(id);
  const latestOrigin = deps.getOrigin();
  if (
    !latest ||
    latest.status === "sending" ||
    latest.authEpoch !== item.authEpoch ||
    !isAuthSessionCurrent(item.authEpoch) ||
    latestOrigin.agentId !== origin.agentId ||
    latestOrigin.threadId !== origin.threadId ||
    latest.target.agentId !== latestOrigin.agentId ||
    latest.target.threadId !== latestOrigin.threadId ||
    !deps.commit(latest)
  )
    return false;
  deps.remove(id);
  return true;
}

interface ChatComposerPluginState {
  agentId: string | null;
  readiness: ReturnType<typeof usePluginToolUis>["readiness"];
  refresh: ReturnType<typeof usePluginToolUis>["refresh"];
  generation: string;
}

export function ChatComposerTools(props: {
  routeAgentId: string | null;
  activeAgentId: string | null;
  threadId: string | null;
  locale: string;
  theme: "light" | "dark";
  timeZone: string;
  children: (state: ChatComposerPluginState) => ReactNode;
}) {
  const {
    routeAgentId,
    activeAgentId,
    threadId,
    locale,
    theme,
    timeZone,
    children,
  } = props;
  const agentId = routeAgentId || activeAgentId;
  const pluginThreadId =
    threadId && !isPendingThread(threadId) ? threadId : null;
  const pluginUi = usePluginToolUis({ agentId, threadId: pluginThreadId });
  return (
    <>
      {agentId && (
        <>
          <PluginReadinessNotice
            readiness={pluginUi.readiness}
            onRetry={() => void pluginUi.refresh()}
          />
          <PluginSurface
            surface="composer"
            location="chat"
            context={{
              agentId,
              threadId: pluginThreadId,
              locale,
              theme,
              generation: pluginUi.generation,
              timeZone,
              readiness: pluginUi.readiness,
            }}
          />
        </>
      )}
      {children({
        agentId,
        readiness: pluginUi.readiness,
        refresh: pluginUi.refresh,
        generation: pluginUi.generation,
      })}
    </>
  );
}

function ChatPageInner() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  prefetchVoiceConfig();
  const { agentId: routeAgentId, threadId } = useParams<{
    agentId?: string;
    threadId?: string;
  }>();
  const serverTimeZone = useServerTimezone();
  const isMobile = useIsMobile();
  const user = useCurrentUser();
  const { layoutMode } = useLayoutMode();
  const isMinimalLayout = layoutMode === "minimal";
  const canTerminal = userCan(user, "terminal");
  const canConfigureModels = navAllowed(user, "models");
  const chatHistoryRail = useChatHistoryRail();
  const [browserRecording, setBrowserRecording] = useState(false);
  const [browserRecordingId, setBrowserRecordingId] = useState<string | null>(
    null,
  );
  const [, setBrowserLastRecordingId] = useState<string | null>(null);
  const {
    sidebarOpen,
    setSidebarOpen,
    sidebarWidth,
    isSidebarResizing,
    sidebarElRef,
    handleSidebarResizeStart,
  } = useChatSidebarState(isMobile);

  // Pre-fill text from router state or module-level pending prefill
  // (set by cron-jobs suggestions before navigating here).
  // Stored as a ref (not state) so it never triggers a parent re-render —
  // re-renders would cause ChatInput to receive a new initialText prop and
  // potentially overwrite text the user is already editing.
  const prefillInputRef = useRef(
    chatStore.consumePendingPrefillText() ||
      ((location.state as { prefillInput?: string } | null)?.prefillInput ??
        ""),
  );
  // Imperative handle to push a new prefill into the already-mounted ChatInput.
  const chatInputRef = useRef<ChatInputHandle | null>(null);
  const pluginReadinessRefreshRef = useRef<
    ((target: PluginReadinessTarget) => Promise<PluginToolUiReadiness>) | null
  >(null);
  const preparedQueueRef = useRef<{
    enqueue: (
      target: NativeTarget,
      input: EnqueueChatItemInput,
    ) => SubmitResult;
    flush: (target: NativeTarget) => void;
  } | null>(null);

  // Clear the router state after consuming prefillInput so it doesn't persist
  // on subsequent visits or page refreshes.
  useEffect(() => {
    if (prefillInputRef.current) {
      navigate(location.pathname, { replace: true, state: {} });
    }
    // Only run once on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // When location.state arrives with a new prefillInput (component already mounted,
  // user navigated here again from another page), push it imperatively so we
  // never trigger a parent re-render that could disrupt the user's editing.
  useEffect(() => {
    const pending = chatStore.consumePendingPrefillText();
    const val =
      pending ||
      ((location.state as { prefillInput?: string } | null)?.prefillInput ??
        "");
    if (val && val !== prefillInputRef.current) {
      prefillInputRef.current = val;
      chatInputRef.current?.setPrefillText(val);
      navigate(location.pathname, { replace: true, state: {} });
    }
  }, [location.state, location.pathname, navigate]);

  const activeThreadId = threadId || null;
  const toolUiThreadId =
    activeThreadId && !isPendingThread(activeThreadId) ? activeThreadId : null;

  const {
    activeAgentId,
    agents,
    setActiveAgent,
    refresh: refreshAgents,
    loading: agentsLoading,
  } = useAgent();
  const resolvedAgentId = routeAgentId || activeAgentId;
  const queueOriginRef = useRef({
    agentId: resolvedAgentId ?? "",
    threadId: activeThreadId ?? "",
  });
  queueOriginRef.current = {
    agentId: resolvedAgentId ?? "",
    threadId: activeThreadId ?? "",
  };
  const hitlEnabled = useHitlEnabled(resolvedAgentId);
  const activeAgent = useMemo(
    () => agents.find((a) => a.agent_id === resolvedAgentId) ?? null,
    [agents, resolvedAgentId],
  );
  const agentChatReady = isAgentChatReady(activeAgent?.state, activeAgent);
  const trajectoryEnabled =
    activeAgent !== null && activeAgent.config?.enable_trajectory !== false;
  const sharedExpertViewer = isSharedExpertViewer(activeAgent ?? {});
  const isTeamChat = isTeamAgent(activeAgent);
  const [agentProfileOpen, setAgentProfileOpen] = useState(false);
  const [profileAgentId, setProfileAgentId] = useState<string | null>(null);
  const profileAgent = useMemo(() => {
    if (!profileAgentId || profileAgentId === activeAgent?.agent_id) {
      return activeAgent;
    }
    const found = agents.find((item) => item.agent_id === profileAgentId);
    if (found) return found;
    if (!activeAgent) return null;
    return {
      ...activeAgent,
      agent_id: profileAgentId,
      name: profileAgentId,
      kind: "expert",
      member_ids: undefined,
      description: null,
    };
  }, [profileAgentId, agents, activeAgent]);
  const profileOpenLabel = isTeamChat
    ? t("chat.agentProfile.openTeam")
    : t("chat.agentProfile.open");
  const ProfileIcon = isTeamChat ? Users : GraduationCap;
  const noAgents = !agentsLoading && agents.length === 0;

  // Sidebar only lists "enabled" experts (harness running). Stopped / failed
  // experts are hidden so the nav stays focused on agents that can actually
  // chat right now — including the focused expert the user just stopped. The
  // main panel still renders ``AgentNotReadyScreen`` on the same URL so users
  // see a clear path back to ``/experts`` to re-enable it.
  const sidebarAgents = useMemo(
    () => selectEnabledExperts(agents, resolvedAgentId, { pinActive: false }),
    [agents, resolvedAgentId],
  );

  const {
    status: memoryMaint,
    visible: memoryMaintVisible,
    blocking: memoryMaintBlocking,
    connectionLost: memoryMaintConnectionLost,
  } = useMemoryMaintenance(resolvedAgentId, agentChatReady && !noAgents);
  const historyMigration = useHistoryMigration(
    resolvedAgentId,
    agentChatReady && !noAgents && !sharedExpertViewer,
  );

  useEffect(() => {
    void refreshAgents({ silent: true });
  }, [refreshAgents]);

  const { quickCards: expertQuickCards, welcomeSuffix } =
    useExpertChatWelcome(activeAgent);
  const { skills: chatSkills } = useSkills(
    chatSkillCatalogAgentId(resolvedAgentId, agentChatReady, agentsLoading),
  );
  const chatSubagents = useChatSubagents(
    chatSkillCatalogAgentId(resolvedAgentId, agentChatReady, agentsLoading),
  );
  const [trajectoryDrawerOpen, setTrajectoryDrawerOpen] = useState(false);
  const [turnRailVisible, setTurnRailVisible] = useState(false);
  const {
    sessions,
    loading: sessionsLoading,
    hasMore: sessionsHasMore,
    loadingMore: sessionsLoadingMore,
    createSession,
    deleteSession,
    renameSession,
    pinSession,
    fetchSessions,
    loadMoreSessions,
    fetchAllSessions,
    ensureThreadInList,
  } = useSessions(resolvedAgentId ?? null);

  const handleLoadMoreSessions = useCallback(() => {
    void loadMoreSessions(activeThreadId ?? undefined);
  }, [loadMoreSessions, activeThreadId]);

  const handleFetchAllSessions = useCallback(() => {
    void fetchAllSessions(activeThreadId ?? undefined);
  }, [fetchAllSessions, activeThreadId]);

  useEffect(() => {
    if (routeAgentId && routeAgentId !== activeAgentId) {
      setActiveAgent(routeAgentId);
    }
  }, [routeAgentId, activeAgentId, setActiveAgent]);

  // Agent profile is agent-scoped — close when switching between two agents.
  const prevProfileAgentRef = useRef(resolvedAgentId);
  useEffect(() => {
    const prev = prevProfileAgentRef.current;
    prevProfileAgentRef.current = resolvedAgentId;
    if (prev == null || resolvedAgentId == null || prev === resolvedAgentId) {
      return;
    }
    setAgentProfileOpen(false);
    setProfileAgentId(null);
    setTrajectoryDrawerOpen(false);
  }, [resolvedAgentId]);

  // Weak stream resume may skip intermediate tokens — hint once after rebind.
  useEffect(() => {
    return chatStore.onStreamEvent((event) => {
      if (event.kind === "streamResume") {
        const key = activeThreadId || "__empty__";
        if (event.sessionId !== key) return;
        antMessage.info(t("chat.streamResumed"));
        return;
      }
      if (event.kind !== "streamEnd") return;
      const key = activeThreadId || "__empty__";
      if (event.sessionId !== key || !resolvedAgentId || key === "__empty__") {
        return;
      }
      void fetchAndSyncSessionArtifacts(resolvedAgentId, key);
    });
  }, [activeThreadId, resolvedAgentId, t]);

  // Refresh thread artifacts after file-producing tools finish (mid-turn updates).
  useEffect(() => {
    return chatStore.onToolEvent((event) => {
      if (event.kind !== "toolDone") return;
      const key = activeThreadId || "__empty__";
      if (event.sessionId !== key || !resolvedAgentId || key === "__empty__") {
        return;
      }
      if (!isFileToolName(event.toolName)) return;
      void fetchAndSyncSessionArtifacts(resolvedAgentId, key);
    });
  }, [activeThreadId, resolvedAgentId]);

  const {
    messages,
    isStreaming,
    thinkingStartedAt,
    historyLoading,
    historyError,
    historyHasMore,
    historyLoadingMore,
    historyRefreshing,
    historyHydrated,
    runUsage,
    contextUsage,
    pendingPlanPath,
    liveSpeakers,
    activeTurnId,
    sendMessage,
    editAndResend,
    cancelStream,
    loadHistory,
    loadMoreHistory,
    refreshHistory,
    retryHistory,
    clearMessages,
    resumeHitl,
  } = useChat(activeThreadId, resolvedAgentId, isTeamChat);

  const hasPendingHitlPause = useMemo(
    () => hasPendingHitl(messages),
    [messages],
  );
  const pendingAsk = useMemo(() => findPendingAsk(messages), [messages]);
  const autoResumedApprovalRef = useRef<string | null>(null);

  const refreshBrowserRef = useRef<() => void>(() => {});

  const { hasBrowserTool, setHasBrowserTool } = useBrowserToolDetection(
    activeThreadId,
    messages,
    () => refreshBrowserRef.current(),
  );

  const {
    sessionId: browserSessionId,
    state: browserSessionState,
    controlOwner: browserControlOwner,
    environment: browserEnvironment,
    refresh: refreshBrowserSession,
  } = useBrowserSessionState(
    threadId,
    hasBrowserTool,
    activeAgent?.bridge ? activeAgent.bridge_connection_id : null,
  );

  refreshBrowserRef.current = refreshBrowserSession;

  const {
    dockOpen,
    dockMode,
    openTabs,
    toolScopeIdentity,
    activeTabId,
    panelSizes: dockPanelSizes,
    isResizing: dockIsResizing,
    handleResizeStart: dockHandleResizeStart,
    handleClose: handleDockClose,
    handleModeChange: handleDockModeChange,
    openFileList,
    openFileAt,
    openKnowledgeCitation,
    openWorkspaceTab,
    openBrowserTab,
    openTerminalTab,
    toggleBrowserPanel,
    toggleWorkspacePanel,
    toggleTerminalPanel,
    openToolUiTab,
    registerToolUiBinding,
    focusToolUiTab,
    closeTab: closeDockTab,
    setActiveTab: setDockActiveTab,
  } = useChatDockPanel(isMobile, resolvedAgentId, toolUiThreadId);

  const chromeCheckInFlightRef = useRef(false);
  const bridgeConnectionId =
    activeAgent?.bridge && activeAgent.bridge_connection_id
      ? activeAgent.bridge_connection_id
      : null;
  const ensureChromeThen = useCallback(
    async (then: () => void) => {
      // A live session means Chrome is already running — skip the probe.
      if (!browserSessionId) {
        if (chromeCheckInFlightRef.current) return;
        chromeCheckInFlightRef.current = true;
        try {
          const env = await browserApi.checkEnvStatus(resolvedAgentId);
          if (shouldJumpToChromeInstall(env)) {
            // Peer Chromium install is not tunneled — only nudge local install UX.
            if (bridgeConnectionId) {
              showConfirmModal(
                {
                  title: t("browserWorkspace.chromeMissingTitle"),
                  content: t("chat.remoteExpert.manageToast"),
                  okText: t("common.confirm"),
                  cancelText: t("common.cancel"),
                },
                { isMobile },
              );
              return;
            }
            showConfirmModal(
              {
                title: t("browserWorkspace.chromeMissingTitle"),
                content: t("browserWorkspace.chromeMissingJumpToInstall"),
                okText: t("common.confirm"),
                cancelText: t("common.cancel"),
                onOk: () => {
                  navigate(WORKBENCH_BROWSER_PATH);
                },
              },
              { isMobile },
            );
            return;
          }
        } catch {
          // Probe failed — keep the existing open-panel behavior.
        } finally {
          chromeCheckInFlightRef.current = false;
        }
      }
      then();
    },
    [
      browserSessionId,
      bridgeConnectionId,
      isMobile,
      navigate,
      resolvedAgentId,
      t,
    ],
  );

  const handleToggleBrowserPanel = useCallback(() => {
    void ensureChromeThen(toggleBrowserPanel);
  }, [ensureChromeThen, toggleBrowserPanel]);

  const handleOpenBrowserTab = useCallback(() => {
    void ensureChromeThen(openBrowserTab);
  }, [ensureChromeThen, openBrowserTab]);

  const dockAddTab = useMemo((): ChatDockAddTabHandlers => {
    const handlers: ChatDockAddTabHandlers = {
      onOpenBrowser: handleOpenBrowserTab,
    };
    if (!sharedExpertViewer) {
      handlers.onOpenWorkspace = openWorkspaceTab;
      handlers.workspaceDisabled = !agentChatReady;
      handlers.workspaceDisabledHint = t("workspace.requiresRunning");
      handlers.onOpenFiles = openFileList;
    }
    if (canTerminal) {
      handlers.onOpenTerminal = openTerminalTab;
    }
    return handlers;
  }, [
    agentChatReady,
    canTerminal,
    handleOpenBrowserTab,
    openFileList,
    openTerminalTab,
    openWorkspaceTab,
    sharedExpertViewer,
    t,
  ]);

  const closeToolUiPanel = useCallback(
    (callId: string) => {
      closeDockTab(dockTabIdForToolCall(callId));
    },
    [closeDockTab],
  );

  useEffect(() => {
    setPluginUiDockHandlers({
      openSidePanel: openToolUiTab,
      closeSidePanel: closeToolUiPanel,
    });
    return () => setPluginUiDockHandlers({});
  }, [openToolUiTab, closeToolUiPanel]);

  const composerSession = useMemo(
    () => sessions.find((session) => session.id === activeThreadId) ?? null,
    [sessions, activeThreadId],
  );

  const panelFilePaths = useMemo(() => {
    const fromTabs = openTabs
      .filter((tab) => tab.kind === "file")
      .map((tab) => ({
        path: tab.path,
        ...(tab.agentId ? { agentId: tab.agentId } : {}),
      }));
    const fromThread = (composerSession?.artifacts ?? []).map((item) => ({
      path: item.path,
      ...(item.agent_id ? { agentId: item.agent_id } : {}),
    }));
    return listDockFilePathsForTree(
      [...fromThread, ...fromTabs],
      resolvedAgentId,
    );
  }, [openTabs, resolvedAgentId, composerSession?.artifacts]);

  const dockAgentNameById = useMemo(() => {
    const map: Record<string, string> = {};
    for (const a of agents) {
      if (a.agent_id) map[a.agent_id] = a.name || a.agent_id;
    }
    return map;
  }, [agents]);

  const {
    selectedModel,
    setSelectedModel,
    selectedConnectors,
    selectedKnowledgeBaseIds,
    chatConnectors,
    chatKnowledgeBases,
    availableModels,
    modelsReady,
    activeModelRef,
    reasoningMode,
    reasoningEffort,
    handleReasoningChange,
    conversationMode,
    handleConversationModeChange,
    hitlPolicy,
    handleHitlPolicyChange,
    handleConnectorsChange,
    handleKnowledgeBaseIdsChange,
  } = useChatComposerResources(
    resolvedAgentId,
    activeThreadId,
    composerSession?.modelRef,
    composerSession?.reasoningMode,
    composerSession?.reasoningEffort,
    composerSession?.conversationMode,
    composerSession?.hitlPolicy,
  );

  const { contextMaxTokens, contextUsedTokens } = useChatContextWindow(
    messages,
    contextUsage,
    selectedModel,
    availableModels,
    activeAgent?.default_model,
    activeModelRef,
    activeAgent,
  );

  const sessionUsage = useMemo(() => {
    const acc: TokenUsage = {};
    for (const msg of messages) {
      const u = msg.usage;
      if (!u) continue;
      if (typeof u.input_tokens === "number") {
        acc.input_tokens = (acc.input_tokens || 0) + u.input_tokens;
      }
      if (typeof u.cache_read_tokens === "number") {
        acc.cache_read_tokens =
          (acc.cache_read_tokens || 0) + u.cache_read_tokens;
      }
      if (typeof u.output_tokens === "number") {
        acc.output_tokens = (acc.output_tokens || 0) + u.output_tokens;
      }
      if (typeof u.total_tokens === "number") {
        acc.total_tokens = (acc.total_tokens || 0) + u.total_tokens;
      }
      if (typeof u.decision_saved === "number") {
        acc.decision_saved = (acc.decision_saved || 0) + u.decision_saved;
      }
    }
    if (
      !acc.input_tokens &&
      !acc.output_tokens &&
      !acc.total_tokens &&
      !acc.decision_saved
    ) {
      return null;
    }
    return acc;
  }, [messages]);
  const decisionSaved =
    (sessionUsage?.decision_saved ?? 0) +
    (isStreaming ? runUsage?.decision_saved ?? 0 : 0);
  const sessionUsageLabel = formatRunUsage(sessionUsage, {
    input: t("chatUsage.input"),
    output: t("chatUsage.output"),
    total: t("chatUsage.total"),
    cacheHit: t("chatUsage.cacheHit"),
    decisionSaved: t("chatUsage.decisionSaved"),
  });

  const { resetNavForAgentSwitch, markInitialNavDone } = useChatNavigation({
    routeAgentId,
    threadId,
    resolvedAgentId,
    activeThreadId,
    sessions,
    sessionsLoading,
    prefillInputRef,
    loadHistory,
    clearMessages,
    ensureThreadInList,
    fetchSessions,
    refreshAgents,
  });

  // Full projection — used by surfaces that render an *existing* agent chip
  // (the preview bar above the composer and historical message chips). They
  // must still find an expert that was running when the user picked it but
  // has since been stopped, otherwise the chip silently vanishes.
  const chatAgentOptions = useMemo(
    () => agents.map(projectChatAgentOption),
    [agents],
  );
  // Subset for the chat-side *pickers* (`@` button popover, `@` mention menu).
  // Only running experts — picking a stopped one would dispatch into an
  // unloaded harness and silently fail.
  // Bridge sessions: only peers on the same connection (local ask_agent
  // cannot reach them; peer ask_agent cannot reach local experts).
  const chatAgentOptionsPickable = useMemo(() => {
    let list = selectEnabledExperts(agents, null, { pinActive: false }).filter(
      (item) => !isTeamAgent(item),
    );
    if (activeAgent?.bridge && activeAgent.bridge_connection_id) {
      const cid = activeAgent.bridge_connection_id;
      list = list.filter(
        (item) => item.bridge && item.bridge_connection_id === cid,
      );
    } else {
      list = list.filter((item) => !item.bridge);
    }
    return list.map(projectChatAgentOption);
  }, [agents, activeAgent?.bridge, activeAgent?.bridge_connection_id]);
  const teamExpertOptions = useMemo(() => {
    if (!isTeamChat) return chatAgentOptionsPickable;
    const ids = new Set(activeAgent?.member_ids ?? []);
    return chatAgentOptions.filter((item) => ids.has(item.agent_id));
  }, [
    isTeamChat,
    activeAgent?.member_ids,
    chatAgentOptions,
    chatAgentOptionsPickable,
  ]);

  const composerLookups = useMemo(
    () => ({
      skills: chatSkills,
      connectors: chatConnectors,
      knowledgeBases: chatKnowledgeBases,
      agents: chatAgentOptions,
    }),
    [chatSkills, chatConnectors, chatKnowledgeBases, chatAgentOptions],
  );

  const { handleSend, beginSubmission } = useChatSend({
    resolvedAgentId,
    activeThreadId,
    sessions,
    messagesLength: messages.length,
    selectedModel,
    selectedConnectors,
    selectedKnowledgeBaseIds,
    reasoningMode,
    reasoningEffort,
    conversationMode,
    hitlPolicy,
    defaultModel: activeAgent?.default_model ?? null,
    sendMessage,
    createSession,
    refreshPluginReadiness: (target) =>
      pluginReadinessRefreshRef.current?.(target) ??
      Promise.resolve({ status: "loading" }),
    enqueuePrepared: (target, item) =>
      preparedQueueRef.current?.enqueue(target, item) ?? {
        status: "rejected",
        reason: "blocked",
      },
    requestPreparedFlush: (target) => preparedQueueRef.current?.flush(target),
    renameSession,
    markInitialNavDone,
    onAutoRecordingStarted: useCallback((recordingId: string) => {
      setBrowserRecording(true);
      setBrowserRecordingId(recordingId);
      setBrowserLastRecordingId(recordingId);
    }, []),
    t,
  });

  // --- Skill recording workflow ---
  const { interceptUserMessage } = useSkillRecordingWorkflow({
    agentId: resolvedAgentId,
    threadId: activeThreadId,
    browserRecording,
    browserRecordingId,
    setBrowserRecording,
    setBrowserRecordingId,
    setBrowserLastRecordingId,
  });

  // Wrap handleSend: skill-recording keywords, then pending HITL routing.
  // An open ask pause must become respond-resume (not a new turn); any other
  // pending HITL must not start a turn that would cancel the interrupt.
  const wrappedHandleSend = useCallback(
    async (
      text: string,
      attachments?: ChatAttachment[],
      overrides?: ChatSendOverrides,
    ) =>
      routeGuardedChatSend({
        text,
        attachments,
        overrides,
        activeThreadId,
        pendingAsk,
        hasPendingHitlPause,
        findPendingAskForThread: (threadId) =>
          findPendingAsk(chatStore.getSnapshot(threadId).messages),
        hasPendingHitlForThread: (threadId) =>
          hasPendingHitl(chatStore.getSnapshot(threadId).messages),
        interceptUserMessage,
        resumeHitl: (actions, threadId, pendingId) =>
          resumeHitl(actions, threadId, undefined, undefined, pendingId),
        handleSend,
        warn: (key) => antMessage.warning(t(key)),
      }),
    [
      interceptUserMessage,
      handleSend,
      pendingAsk,
      hasPendingHitlPause,
      activeThreadId,
      resumeHitl,
      t,
    ],
  );

  const flushQueuedItem = useCallback(
    async (item: QueuedChatItem, ctx: ChatQueueFlushContext) => {
      const target = item.target ?? {
        agentId: ctx.agentId,
        threadId: ctx.threadId,
      };
      if (!target.threadId) {
        antMessage.error(t("chat.queue.flushFailed"));
        return { status: "rejected" as const, reason: "blocked" as const };
      }
      const refresh = pluginReadinessRefreshRef.current;
      if (!refresh) return { status: "rejected", reason: "blocked" } as const;
      return await dispatchPreparedQueueItem(
        item,
        target,
        refresh,
        sendMessage,
        editAndResend,
      );
    },
    [sendMessage, editAndResend, t],
  );

  const membersStillTalking =
    isTeamChat &&
    liveSpeakers.some(
      (id) => id.trim() && id !== (activeAgent?.agent_id ?? ""),
    );

  const shouldDeferQueueFlush = useCallback((threadId: string) => {
    return chatStore.isSessionBusyForTurnDispatch(threadId);
  }, []);

  const {
    items: queuedItems,
    allItems: allQueuedItems,
    enqueueForTarget: enqueueQueuedForTarget,
    requestFlush: requestQueuedFlush,
    removeById: removeQueued,
    getById: getQueuedById,
    clear: clearQueued,
  } = useChatMessageQueue({
    agentId: resolvedAgentId,
    threadId: activeThreadId,
    isStreaming: isStreaming || membersStillTalking,
    onFlush: flushQueuedItem,
    shouldDeferFlush: shouldDeferQueueFlush,
  });
  preparedQueueRef.current = {
    enqueue: enqueueQueuedForTarget,
    flush: requestQueuedFlush,
  };

  const reclaimPreparedQueueItem = useCallback(
    (id: string, commit: (item: QueuedChatItem) => boolean) =>
      reclaimQueueItemIfCurrent(id, {
        getById: getQueuedById,
        getOrigin: () => queueOriginRef.current,
        restore: restoreComposerSelections,
        commit,
        remove: removeQueued,
      }),
    [getQueuedById, removeQueued],
  );

  // While streaming, Enter/Send parks a local chip; ↑ Steer on the chip row
  // commits sendSteerWhileBusy. The click always acts: the
  // server routes delivery=auto (busy → steer queue, idle → new turn); a
  // dead socket degrades to the normal send path — never a silent no-op.
  const handleSteerQueued = useCallback(
    (id: string) => {
      const key = activeThreadId || "";
      const item = queuedItems.find((q) => q.id === id);
      if (!item || !key || item.status !== "queued") return;
      if (
        requestBoundQueueItemFlush(
          item.bound || Boolean(item.pluginContext),
          item.target,
          requestQueuedFlush,
        )
      ) {
        return;
      }
      const turnId = activeTurnId || chatStore.getActiveTurnId(key) || "";
      const result = chatStore.sendSteerWhileBusy(key, item.text, {
        expectedTurnId: turnId || undefined,
        attachments: item.attachments,
      });
      if (result === "empty") return;
      removeQueued(id);
      if (result === "no_socket") {
        wrappedHandleSend(item.text, item.attachments);
      }
    },
    [
      activeThreadId,
      activeTurnId,
      queuedItems,
      removeQueued,
      requestQueuedFlush,
      wrappedHandleSend,
    ],
  );

  const handleWithdrawSteer = useCallback(
    (messageId: string) => {
      const key = activeThreadId || "";
      if (!key) return;
      const result = chatStore.withdrawCommittedSteer(key, messageId);
      if (result === "pending_ack") {
        antMessage.info(t("chat.steer.withdrawPending"));
      }
    },
    [activeThreadId, t],
  );

  // Committed steers live in the transcript (optimistic row settled by
  // server frames) — the chip panel holds only uncommitted local drafts
  // (the queue row settles on commit).
  const displayQueuedItems = useMemo(() => {
    const currentIds = new Set(queuedItems.map((item) => item.id));
    const originThreadId =
      activeThreadId && !isPendingThread(activeThreadId)
        ? activeThreadId
        : null;
    return [
      ...queuedItems,
      ...allQueuedItems.filter(
        (item) =>
          !currentIds.has(item.id) &&
          isAuthSessionCurrent(item.authEpoch) &&
          item.displayOrigin?.agentId === resolvedAgentId &&
          item.displayOrigin.threadId === originThreadId,
      ),
    ];
  }, [queuedItems, allQueuedItems, activeThreadId, resolvedAgentId]);

  const {
    handleNewChat: startNewChat,
    handleNewChatWithAgent,
    handleSelectSession,
    navigateToAgent,
    handleDeleteSession,
  } = useChatSessionActions({
    resolvedAgentId,
    activeThreadId,
    sessions,
    isMobile,
    setActiveAgent,
    setSidebarOpen,
    setSelectedModel,
    setHasBrowserTool,
    deleteSession,
    clearMessages,
    resetNavForAgentSwitch,
    markInitialNavDone,
  });

  const handleNewChat = useCallback(() => {
    clearQueued();
    startNewChat();
  }, [clearQueued, startNewChat]);

  useEffect(() => {
    return chatStore.onSlashAction((ev) => {
      if (ev.action === "switch_agent" && ev.agent_id) {
        navigateToAgent(ev.agent_id);
      }
      if (
        ev.action === "set_conversation_mode" &&
        (ev.mode === "ask" || ev.mode === "plan" || ev.mode === "craft")
      ) {
        handleConversationModeChange(ev.mode);
      }
    });
  }, [navigateToAgent, handleConversationModeChange]);

  const handlePromptClick = useCallback(
    (text: string, options?: { prefill?: boolean }) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      if (options?.prefill || promptNeedsUserInput(text)) {
        prefillInputRef.current = trimmed;
        chatInputRef.current?.setPrefillText(trimmed);
        return;
      }
      wrappedHandleSend(trimmed);
    },
    [wrappedHandleSend],
  );

  const handleAcpPermissionSelect = useCallback(
    (permissionMessage: string) => {
      wrappedHandleSend(permissionMessage);
    },
    [wrappedHandleSend],
  );

  const handleHitlDecision = useCallback(
    (
      decisions: Array<{ type: string; message?: string }>,
      policy?: HitlSessionPolicy,
      pendingId?: string,
    ) => {
      if (policy) {
        const next =
          policy.mode === "allow_tools"
            ? mergeAllowTools(hitlPolicy, policy.tools ?? [])
            : policy;
        const pending = findAutoResumableApproval(next, messages);
        if (pending && activeThreadId) {
          autoResumedApprovalRef.current = `${activeThreadId}:${pending.messageId}`;
        }
        handleHitlPolicyChange(next, { persist: false });
        resumeHitl(
          decisions,
          activeThreadId ?? undefined,
          undefined,
          next,
          pendingId ?? pending?.pendingId,
        );
        return;
      }
      resumeHitl(
        decisions,
        activeThreadId ?? undefined,
        undefined,
        undefined,
        pendingId,
      );
    },
    [resumeHitl, activeThreadId, handleHitlPolicyChange, hitlPolicy, messages],
  );

  const handleComposerHitlPolicyChange = useCallback(
    (policy: HitlSessionPolicy) => {
      const pending = findAutoResumableApproval(policy, messages);
      handleHitlPolicyChange(policy, { persist: !pending });
      if (!pending) return;
      if (activeThreadId) {
        autoResumedApprovalRef.current = `${activeThreadId}:${pending.messageId}`;
      }
      resumeHitl(
        pending.actions.map(() => ({ type: "approve" })),
        activeThreadId ?? undefined,
        undefined,
        policy,
        pending.pendingId,
      );
    },
    [handleHitlPolicyChange, messages, resumeHitl, activeThreadId],
  );

  useEffect(() => {
    if (isStreaming || !activeThreadId) return;
    const sessionPolicy = parseHitlSessionPolicy(composerSession?.hitlPolicy);
    const threadId = activeThreadId;
    // A pause can be projected twice. Wait until the latest card is the one
    // we resume, instead of approving an id the next frame already expired.
    const timer = window.setTimeout(() => {
      const pending = findAutoResumableApproval(
        sessionPolicy,
        chatStore.getSnapshot(threadId).messages,
      );
      if (!pending?.pendingId) {
        autoResumedApprovalRef.current = null;
        return;
      }
      const resumeKey = `${threadId}:${pending.messageId}`;
      if (autoResumedApprovalRef.current === resumeKey) return;
      autoResumedApprovalRef.current = resumeKey;
      resumeHitl(
        pending.actions.map(() => ({ type: "approve" })),
        threadId,
        undefined,
        sessionPolicy,
        pending.pendingId,
      );
    }, 50);
    return () => window.clearTimeout(timer);
  }, [
    composerSession?.hitlPolicy,
    messages,
    activeThreadId,
    resumeHitl,
    isStreaming,
  ]);

  /** Close an ask pause without answering: ``respond`` is the only decision
   *  the agent allows for ``ask_user_question``, so tell it to wrap up. */
  const handleAskDismiss = useCallback(
    (actions: unknown[]) => {
      resumeHitl(
        actions.map(() => ({
          type: "respond",
          message: t("chat.ask.dismissMessage"),
        })),
        activeThreadId ?? undefined,
        true,
      );
    },
    [resumeHitl, activeThreadId, t],
  );

  useEffect(() => {
    let cancelled = false;
    browserApi
      .recordReplayStatus()
      .then((status) => {
        if (cancelled) return;
        setBrowserRecording(Boolean(status.active));
        setBrowserRecordingId(status.active?.recordingId ?? null);
        if (status.latestRecordingId) {
          setBrowserLastRecordingId(status.latestRecordingId);
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  // Regenerate: re-send the last user message before this assistant message
  const handleRegenerate = useCallback(
    (messageId: string) => {
      if (hasPendingHitlPause) {
        antMessage.warning(t("chat.hitl.finishPendingFirst"));
        return;
      }
      const idx = messages.findIndex((m) => m.id === messageId);
      if (idx < 0) return;
      // Find the user message that preceded this assistant response
      let userMsg: (typeof messages)[0] | undefined;
      for (let i = idx - 1; i >= 0; i--) {
        if (messages[i].role === "user") {
          userMsg = messages[i];
          break;
        }
      }
      if (!userMsg) return;
      wrappedHandleSend(userMsg.content, userMsg.attachments);
    },
    [messages, wrappedHandleSend, hasPendingHitlPause, t],
  );

  const handleDisableStreamUsage = useCallback(async () => {
    let lastUserModel: string | null = null;
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].role === "user") {
        lastUserModel = messages[i].composerContext?.model ?? null;
        break;
      }
    }
    const providerId = providerIdForTurn(availableModels, [
      lastUserModel,
      selectedModel,
      activeAgent?.default_model,
      activeModelRef,
    ]);
    if (providerId == null) {
      antMessage.error(t("chat.disableStreamUsageFailed"));
      return false;
    }
    try {
      await disableProviderStreamUsage(providerId);
      antMessage.success(t("chat.disableStreamUsageSuccess"));
      return true;
    } catch {
      antMessage.error(t("chat.disableStreamUsageFailed"));
      return false;
    }
  }, [
    availableModels,
    selectedModel,
    activeAgent?.default_model,
    activeModelRef,
    messages,
    t,
  ]);

  // Edit user message: truncate history from that message onwards, replace
  // its content, and re-send — mirrors Claude / ChatGPT "edit message" behaviour.
  const handleEditUserMessage = useCallback(
    async (messageId: string, newText: string) => {
      if (!activeThreadId) return;
      if (hasPendingHitlPause) {
        antMessage.warning(t("chat.hitl.finishPendingFirst"));
        return;
      }
      const agentId = resolvedAgentId ?? "";
      await handleSend(newText, undefined, {
        intent: "edit",
        editMessageId: messageId,
        threadId: activeThreadId,
        agentId,
        refreshPluginReadiness: (target) =>
          pluginReadinessRefreshRef.current?.(target) ??
          Promise.resolve({ status: "loading" }),
      });
    },
    [activeThreadId, handleSend, resolvedAgentId, hasPendingHitlPause, t],
  );

  const [forking, setForking] = useState(false);
  const forkDisabled = forking || isStreaming || hasPendingHitlPause;
  const forkDisabledHint =
    !forking && (isStreaming || hasPendingHitlPause)
      ? t("chat.forkDisabledWhileBusy")
      : undefined;
  const hasAssistantReply = useMemo(
    () =>
      messages.some(
        (message) =>
          message.role === "assistant" &&
          !message.toolData &&
          Boolean(
            (message.content && message.content.trim()) ||
              (message.attachments && message.attachments.length > 0),
          ),
      ),
    [messages],
  );
  const sessionForkDisabled = forkDisabled || !hasAssistantReply;
  const sessionForkDisabledHint = !hasAssistantReply
    ? t("chat.forkNoAssistant")
    : forkDisabledHint;

  const navigateToForkedThread = useCallback(
    async (
      agent: string,
      created: { thread_id: string; copied_messages: number },
    ) => {
      await ensureThreadInList(created.thread_id);
      navigate(`/chat/${agent}/${created.thread_id}`);
      antMessage.success(
        created.copied_messages > 0
          ? t("chat.forkSuccess")
          : t("chat.forkSuccessEmpty"),
      );
    },
    [ensureThreadInList, navigate, t],
  );

  const handleForkAssistantMessage = useCallback(
    async (messageId: string) => {
      const agent = resolvedAgentId;
      if (!agent || !activeThreadId || forkDisabled) return;
      const idx = messages.findIndex((message) => message.id === messageId);
      if (idx < 0) return;
      const assistantMsg = messages[idx];
      if (assistantMsg.role !== "assistant" || assistantMsg.toolData) return;
      const turnsFromEnd = assistantTurnsFromEnd(messages, messageId);
      if (turnsFromEnd < 1) return;
      setForking(true);
      try {
        const created = await octopThreadsApi.fork(agent, activeThreadId, {
          message_id: messageId,
          content: assistantMsg.content,
          assistant_turns_from_end: turnsFromEnd,
        });
        await navigateToForkedThread(agent, created);
      } catch (error) {
        antMessage.error(apiErrorMessage(error, t("chat.forkFailed"), t));
      } finally {
        setForking(false);
      }
    },
    [
      activeThreadId,
      forkDisabled,
      messages,
      navigateToForkedThread,
      resolvedAgentId,
      t,
    ],
  );

  const handleForkSession = useCallback(
    async (threadId: string, agentId?: string | null) => {
      const agent = agentId || resolvedAgentId;
      if (!agent || !threadId || forking) return;
      if (threadId === activeThreadId && (isStreaming || hasPendingHitlPause)) {
        antMessage.warning(t("chat.forkDisabledWhileBusy"));
        return;
      }
      if (threadId === activeThreadId && !hasAssistantReply) {
        antMessage.warning(t("chat.forkNoAssistant"));
        return;
      }
      setForking(true);
      try {
        const created = await octopThreadsApi.fork(agent, threadId, {
          assistant_turns_from_end: 1,
        });
        await navigateToForkedThread(agent, created);
      } catch (error) {
        antMessage.error(apiErrorMessage(error, t("chat.forkFailed"), t));
      } finally {
        setForking(false);
      }
    },
    [
      activeThreadId,
      forking,
      hasAssistantReply,
      hasPendingHitlPause,
      isStreaming,
      navigateToForkedThread,
      resolvedAgentId,
      t,
    ],
  );

  const hasMessages = messages.length > 0;
  // On hard refresh / deep-link into a thread, messages start empty. Showing
  // Welcome until history returns looks like a full page flash. Keep the list
  // shell while that thread is still hydrating.
  const awaitingThreadHistory = Boolean(
    activeThreadId &&
      !hasMessages &&
      !historyError &&
      (historyLoading || !historyHydrated),
  );
  const showWelcome = !hasMessages && !awaitingThreadHistory;

  useEffect(() => {
    if (showWelcome || !agentChatReady || noAgents) {
      setTurnRailVisible(false);
    }
  }, [showWelcome, agentChatReady, noAgents]);

  const activeSession = useMemo(() => {
    if (!activeThreadId || showWelcome) return null;
    return (
      sessions.find((s) => s.id === activeThreadId) ?? {
        id: activeThreadId,
        name: "New Chat",
        threadId: activeThreadId,
        updatedAt: null,
        channelType: "dashboard",
        isActive: true,
        hasActivity: true,
        pinned: false,
      }
    );
  }, [activeThreadId, sessions, showWelcome]);

  const activeSessionTitle = useMemo(() => {
    if (!activeSession) return null;
    const name = activeSession.name?.trim();
    if (!name || name === "New Chat") return t("chatWelcome.newChat");
    return name;
  }, [activeSession, t]);

  const chatSidebarPanel = (
    <ChatSidebarPanel
      isMobile={isMobile}
      sidebarOpen={sidebarOpen}
      sidebarWidth={sidebarWidth}
      isSidebarResizing={isSidebarResizing}
      sidebarElRef={sidebarElRef}
      agents={sidebarAgents}
      sessions={sessions}
      activeThreadId={activeThreadId}
      resolvedAgentId={resolvedAgentId}
      sessionsHasMore={sessionsHasMore}
      sessionsLoadingMore={sessionsLoadingMore}
      onLoadMoreSessions={handleLoadMoreSessions}
      onFetchAllSessions={handleFetchAllSessions}
      onSelectSession={(sessionId, agentId) => {
        setActiveAgent(agentId);
        if (agentId && agentId !== resolvedAgentId) {
          void octopThreadsApi.rebind(agentId, sessionId).catch(() => {});
          navigate(`/chat/${agentId}/${sessionId}`);
          if (isMobile) setSidebarOpen(false);
          return;
        }
        handleSelectSession(sessionId);
      }}
      onAgentSelect={navigateToAgent}
      onNewChatWithAgent={(agentId) => {
        clearQueued();
        handleNewChatWithAgent(agentId);
      }}
      onDeleteSession={handleDeleteSession}
      onRenameSession={renameSession}
      onPinSession={pinSession}
      onForkSession={handleForkSession}
      forkDisabled={sessionForkDisabled}
      forkDisabledHint={sessionForkDisabledHint}
      onSidebarOpenChange={setSidebarOpen}
      onSidebarResizeStart={handleSidebarResizeStart}
      layoutRail={!isMinimalLayout}
      navEmbedded={isMinimalLayout}
    />
  );

  return (
    <ChatFilePreviewProvider
      openFilePreview={openFileAt}
      openKnowledgeCitation={openKnowledgeCitation}
    >
      <ChatToolDockProvider
        dockOpen={dockOpen}
        openTabs={openTabs}
        activeTabId={activeTabId}
        resolvedAgentId={resolvedAgentId}
        activeThreadId={toolUiThreadId}
        scopeIdentity={toolScopeIdentity}
        openToolUiPanel={openToolUiTab}
        closeToolUiPanel={closeToolUiPanel}
        focusToolUiPanel={focusToolUiTab}
        registerToolUiBinding={registerToolUiBinding}
      >
        {chatHistoryRail
          ? createPortal(chatSidebarPanel, chatHistoryRail)
          : null}
        <div
          className={`${styles.chatPage} ${
            dockIsResizing ? styles.panelResizeActive : ""
          } ${
            dockOpen && dockMode === "bottom"
              ? styles.chatPageWithBottomDock
              : ""
          }`}
        >
          {/* Main chat area */}
          <div
            className={[
              styles.chatMain,
              turnRailVisible ? styles.chatMainWithTurnRail : "",
            ]
              .filter(Boolean)
              .join(" ")}
          >
            {/* Mobile toolbar — session list + optional title + agent profile */}
            {isMobile && (
              <div className={styles.mobileToolbar}>
                <button
                  className={styles.menuBtn}
                  onClick={() => {
                    if (isMinimalLayout) {
                      window.dispatchEvent(new Event(OPEN_NAV_RECORDS_EVENT));
                      return;
                    }
                    setSidebarOpen(!sidebarOpen);
                  }}
                  title={t("nav.chatHistory") || "会话列表"}
                >
                  <PanelLeftOpen size={18} strokeWidth={1.8} />
                </button>
                {activeSessionTitle && (
                  <div
                    className={styles.mobileTitle}
                    title={activeSessionTitle}
                  >
                    <span className={styles.mobileTitleText}>
                      {activeSessionTitle}
                    </span>
                    <TeamChatBadge show={isTeamChat} />
                    <RemoteExpertHint agent={activeAgent} />
                  </div>
                )}
                {resolvedAgentId && !sharedExpertViewer && (
                  <div className={styles.mobileToolbarRight}>
                    <button
                      className={styles.menuBtn}
                      onClick={() => setAgentProfileOpen(true)}
                      title={profileOpenLabel}
                      aria-label={profileOpenLabel}
                    >
                      <ProfileIcon size={18} strokeWidth={1.8} />
                    </button>
                    <button
                      className={styles.menuBtn}
                      onClick={toggleWorkspacePanel}
                      disabled={!agentChatReady}
                      title={
                        agentChatReady
                          ? t("chat.openWorkspace", "工作区")
                          : t("workspace.requiresRunning")
                      }
                      aria-label={t("chat.openWorkspace", "工作区")}
                    >
                      <FolderOpen size={18} strokeWidth={1.8} />
                    </button>
                    <button
                      className={styles.menuBtn}
                      onClick={() => void handleToggleBrowserPanel()}
                      title={t("chat.openBrowser")}
                      aria-label={t("chat.openBrowser")}
                    >
                      <Globe size={18} strokeWidth={1.8} />
                    </button>
                    {!sharedExpertViewer && panelFilePaths.length > 0 && (
                      <button
                        className={styles.menuBtn}
                        onClick={() => openFileList()}
                        title={t("chat.modifiedFiles", {
                          count: panelFilePaths.length,
                          defaultValue: "已修改文件（{{count}}）",
                        })}
                        aria-label={t("chat.modifiedFiles", {
                          count: panelFilePaths.length,
                          defaultValue: "已修改文件（{{count}}）",
                        })}
                      >
                        <FilePen size={18} strokeWidth={1.8} />
                      </button>
                    )}
                  </div>
                )}
              </div>
            )}

            {!isMobile && activeSession && activeSessionTitle && (
              <ChatTitleBar
                session={activeSession}
                title={activeSessionTitle}
                onRename={renameSession}
                onPin={pinSession}
                onFork={handleForkSession}
                onDelete={handleDeleteSession}
                forkDisabled={sessionForkDisabled}
                forkDisabledHint={sessionForkDisabledHint}
                isTeam={isTeamChat}
                agent={activeAgent}
              />
            )}

            {memoryMaintVisible && memoryMaint && (
              <MemoryMaintenanceBanner
                status={memoryMaint}
                blocking={memoryMaintBlocking}
                connectionLost={memoryMaintConnectionLost}
              />
            )}

            {historyMigration.visible && historyMigration.status && (
              <HistoryMigrationBanner
                status={historyMigration.status}
                starting={historyMigration.starting}
                startFailed={historyMigration.startFailed}
                onStart={() => void historyMigration.start()}
              />
            )}

            {historyError && (
              <Alert
                type="error"
                showIcon
                message={t("chat.historyLoadFailed")}
                action={
                  <Button
                    size="small"
                    loading={
                      historyLoading || historyRefreshing || historyLoadingMore
                    }
                    onClick={() => void retryHistory()}
                  >
                    {t("chat.historyRetry")}
                  </Button>
                }
              />
            )}
            <div className={styles.chatContent}>
              {!agentChatReady || noAgents ? (
                <AgentNotReadyScreen
                  agent={activeAgent}
                  noAgents={noAgents}
                  loading={agentsLoading}
                />
              ) : showWelcome && modelsReady && availableModels.length === 0 ? (
                <ModelConfigEmpty canConfigure={canConfigureModels} />
              ) : showWelcome ? (
                <WelcomeScreen
                  agentName={activeAgent?.name ?? null}
                  welcomeSuffix={welcomeSuffix}
                  quickCards={expertQuickCards}
                  onPromptClick={handlePromptClick}
                  hideMascot={isStreaming || liveSpeakers.length > 0}
                  isTeam={isTeamChat}
                />
              ) : (
                <ChatAgentProfileProvider
                  canOpen={Boolean(resolvedAgentId) && !sharedExpertViewer}
                  onOpen={(agentId) => {
                    setProfileAgentId(
                      agentId && agentId !== resolvedAgentId ? agentId : null,
                    );
                    setAgentProfileOpen(true);
                  }}
                  isTeam={isTeamChat}
                >
                  <MessageList
                    messages={messages}
                    agentId={resolvedAgentId}
                    composerLookups={composerLookups}
                    loading={awaitingThreadHistory}
                    historyHasMore={historyHasMore}
                    historyLoadingMore={historyLoadingMore}
                    historyRefreshing={historyRefreshing}
                    onLoadMoreHistory={loadMoreHistory}
                    onRefreshHistory={refreshHistory}
                    isStreaming={isStreaming}
                    liveSpeakers={liveSpeakers}
                    thinkingStartedAt={thinkingStartedAt}
                    sessionKey={activeThreadId ?? undefined}
                    onRegenerate={handleRegenerate}
                    onDisableStreamUsage={handleDisableStreamUsage}
                    onEditUserMessage={handleEditUserMessage}
                    onWithdrawSteer={handleWithdrawSteer}
                    onForkAssistantMessage={handleForkAssistantMessage}
                    forkDisabled={forkDisabled}
                    forkDisabledHint={forkDisabledHint}
                    onAcpPermissionSelect={handleAcpPermissionSelect}
                    onHitlDecision={handleHitlDecision}
                    onTurnRailVisibilityChange={setTurnRailVisible}
                    onOpenBrowser={hasBrowserTool ? openBrowserTab : undefined}
                    onEditFile={
                      !sharedExpertViewer && panelFilePaths.length > 0
                        ? openFileList
                        : undefined
                    }
                  />
                </ChatAgentProfileProvider>
              )}
            </div>

            {!isMobile &&
              !dockOpen &&
              !agentProfileOpen &&
              !trajectoryDrawerOpen && (
                <div className={styles.chatFloatActions}>
                  {/* PWA install first when available — same column as browser / experts. */}
                  <PwaInstallPrompt appearance="chatFloat" />
                  {resolvedAgentId && !sharedExpertViewer && (
                    <>
                      <Tooltip
                        title={profileOpenLabel}
                        mouseEnterDelay={0.35}
                        placement="left"
                      >
                        <span className={styles.chatFloatBtnWrap}>
                          <button
                            type="button"
                            className={styles.agentProfileBtn}
                            onClick={() => setAgentProfileOpen(true)}
                            aria-label={profileOpenLabel}
                          >
                            <ProfileIcon size={20} strokeWidth={2.1} />
                          </button>
                        </span>
                      </Tooltip>
                      <Tooltip
                        title={
                          agentChatReady
                            ? t("chat.openWorkspace", "工作区")
                            : t("workspace.requiresRunning")
                        }
                        mouseEnterDelay={0.35}
                        placement="left"
                      >
                        <span className={styles.chatFloatBtnWrap}>
                          <button
                            type="button"
                            className={styles.chatFloatBtn}
                            disabled={!agentChatReady}
                            onClick={toggleWorkspacePanel}
                            aria-label={t("chat.openWorkspace", "工作区")}
                          >
                            <FolderOpen size={20} strokeWidth={2.1} />
                          </button>
                        </span>
                      </Tooltip>
                    </>
                  )}
                  {!sharedExpertViewer && panelFilePaths.length > 0 && (
                    <Tooltip
                      title={t("chat.modifiedFiles", {
                        count: panelFilePaths.length,
                        defaultValue: "已修改文件（{{count}}）",
                      })}
                      mouseEnterDelay={0.35}
                      placement="left"
                    >
                      <span className={styles.chatFloatBtnWrap}>
                        <button
                          type="button"
                          className={styles.chatFloatBtn}
                          onClick={() => openFileList()}
                          aria-label={t("chat.modifiedFiles", {
                            count: panelFilePaths.length,
                            defaultValue: "已修改文件（{{count}}）",
                          })}
                        >
                          <FilePen size={20} strokeWidth={2.1} />
                        </button>
                        {panelFilePaths.length > 1 && (
                          <span className={styles.chatFloatBadge}>
                            {panelFilePaths.length > 99
                              ? "99+"
                              : panelFilePaths.length}
                          </span>
                        )}
                      </span>
                    </Tooltip>
                  )}
                  {canTerminal && (
                    <Tooltip
                      title={t("chat.openTerminal", "打开终端")}
                      mouseEnterDelay={0.35}
                      placement="left"
                    >
                      <span className={styles.chatFloatBtnWrap}>
                        <button
                          type="button"
                          className={styles.terminalFloatBtn}
                          onClick={toggleTerminalPanel}
                          aria-label={t("chat.openTerminal", "打开终端")}
                        >
                          <Terminal size={20} strokeWidth={2.1} />
                        </button>
                      </span>
                    </Tooltip>
                  )}
                  {trajectoryEnabled && (
                    <Tooltip
                      title={
                        !agentChatReady
                          ? t("workspace.requiresRunning")
                          : !activeThreadId
                          ? t(
                              "chat.trajectorySelectSession",
                              "Select a session to view trajectory",
                            )
                          : t("chat.openTrajectory", "运行轨迹")
                      }
                      mouseEnterDelay={0.35}
                      placement="left"
                    >
                      <span className={styles.chatFloatBtnWrap}>
                        <button
                          type="button"
                          className={styles.chatFloatBtn}
                          disabled={!activeThreadId || !agentChatReady}
                          onClick={() => setTrajectoryDrawerOpen(true)}
                          aria-label={t("chat.openTrajectory", "运行轨迹")}
                        >
                          <Activity size={20} strokeWidth={2.1} />
                        </button>
                      </span>
                    </Tooltip>
                  )}
                  <Tooltip
                    title={
                      browserSessionId
                        ? t("browserWorkspace.browserStatusActive", {
                            owner:
                              browserControlOwner === "agent"
                                ? t("browserWorkspace.agentControl")
                                : t("browserWorkspace.userTakeover"),
                          })
                        : t("browserWorkspace.browserStatusIdle")
                    }
                    mouseEnterDelay={0.35}
                    placement="left"
                  >
                    <span className={styles.chatFloatBtnWrap}>
                      <button
                        type="button"
                        className={[
                          styles.browserStatusBtn,
                          browserSessionId ? styles.browserStatusActive : "",
                          browserSessionId &&
                          (browserSessionState === "awaiting_user_auth" ||
                            browserSessionState === "authenticating")
                            ? styles.browserStatusAuth
                            : "",
                          browserSessionId && browserControlOwner === "user"
                            ? styles.browserStatusTakeover
                            : "",
                        ]
                          .filter(Boolean)
                          .join(" ")}
                        onClick={() => void handleToggleBrowserPanel()}
                        aria-label={t("chat.openBrowser")}
                      >
                        <Globe size={20} strokeWidth={2.1} />
                        {browserSessionId && (
                          <span
                            className={`${styles.browserStatusDot} ${
                              styles[`browserStatus_${browserControlOwner}`]
                            }`}
                          />
                        )}
                      </button>
                    </span>
                  </Tooltip>
                </div>
              )}

            <ChatComposerChrome sessionUsageLabel={sessionUsageLabel} />
            {pendingAsk ? (
              <div className={styles.askQuestionDock}>
                <div className={styles.askQuestionDockInner}>
                  <AskQuestionCard
                    key={pendingAsk.messageId}
                    questions={pendingAsk.questions}
                    status="pending"
                    onSubmit={(answer) =>
                      handleHitlDecision(
                        pendingAsk.actions.map(() => ({
                          type: "respond",
                          message: answer,
                        })),
                      )
                    }
                    onDismiss={() => handleAskDismiss(pendingAsk.actions)}
                  />
                </div>
              </div>
            ) : null}
            {conversationMode === "plan" && pendingPlanPath ? (
              <div className={styles.askQuestionDock}>
                <div className={styles.askQuestionDockInner}>
                  <PlanReadyCard
                    path={pendingPlanPath}
                    onExecute={() => {
                      const path = pendingPlanPath;
                      if (activeThreadId) {
                        chatStore.setPendingPlanPath(activeThreadId, null);
                      }
                      handleConversationModeChange("craft", { persist: false });
                      wrappedHandleSend(
                        t("chat.conversationMode.executeUtterance", { path }),
                        undefined,
                        { conversationMode: "craft" },
                      );
                    }}
                    onKeepEditing={() => chatInputRef.current?.focusComposer()}
                  />
                </div>
              </div>
            ) : null}
            {!showWelcome && modelsReady && availableModels.length === 0 ? (
              <div className={styles.modelConfigDock}>
                <div className={styles.modelConfigDockInner}>
                  <Alert
                    type="info"
                    showIcon
                    message={t("modelConfig.promptTitle")}
                    description={
                      canConfigureModels
                        ? t("modelConfig.promptMessage")
                        : t("modelConfig.promptMessageNoPermission")
                    }
                    action={
                      canConfigureModels ? (
                        <Button
                          type="primary"
                          size="small"
                          onClick={() => navigate("/admin/models")}
                        >
                          {t("modelConfig.configureButton")}
                        </Button>
                      ) : null
                    }
                  />
                </div>
              </div>
            ) : null}
            <ChatComposerTools
              routeAgentId={routeAgentId ?? null}
              activeAgentId={activeAgentId}
              threadId={activeThreadId}
              locale={i18n.language || "en"}
              theme={
                document.documentElement.getAttribute("data-theme") ===
                  "dark" || document.documentElement.classList.contains("dark")
                  ? "dark"
                  : "light"
              }
              timeZone={serverTimeZone}
            >
              {({
                agentId: composerAgentId,
                refresh: refreshPluginReadiness,
              }) => {
                pluginReadinessRefreshRef.current = refreshPluginReadiness;
                return (
                  <ChatInput
                    ref={chatInputRef}
                    onAcceptedTarget={(target) =>
                      navigate(`/chat/${target.agentId}/${target.threadId}`, {
                        replace: true,
                      })
                    }
                    beginSubmission={beginSubmission}
                    onSend={(text, attachments, submissionLease) =>
                      wrappedHandleSend(text, attachments, {
                        refreshPluginReadiness,
                        submissionLease,
                      })
                    }
                    onQueue={(item, submissionLease) =>
                      wrappedHandleSend(item.text, item.attachments, {
                        queueIntent: true,
                        refreshPluginReadiness,
                        submissionLease,
                      })
                    }
                    queuedItems={displayQueuedItems}
                    onRemoveQueued={removeQueued}
                    onReclaimQueued={(id, commit, submissionLease) => {
                      if (submissionLease && !submissionLease.isCurrent())
                        return Promise.resolve(false);
                      return reclaimPreparedQueueItem(
                        id,
                        (item) =>
                          (!submissionLease || submissionLease.isCurrent()) &&
                          commit(item),
                      );
                    }}
                    onSteerQueued={handleSteerQueued}
                    onOpenQueuedTarget={(target) =>
                      navigate(`/chat/${target.agentId}/${target.threadId}`)
                    }
                    onCancel={cancelStream}
                    onNewChat={handleNewChat}
                    isStreaming={isStreaming}
                    holdSend={membersStillTalking}
                    isTeam={isTeamChat}
                    disabled={
                      !agentChatReady || noAgents || memoryMaintBlocking
                    }
                    initialText={prefillInputRef.current}
                    onComposerCleared={() => {
                      prefillInputRef.current = "";
                    }}
                    availableModels={availableModels}
                    selectedModel={selectedModel}
                    onModelChange={setSelectedModel}
                    reasoningMode={reasoningMode}
                    reasoningEffort={reasoningEffort}
                    onReasoningChange={handleReasoningChange}
                    conversationMode={conversationMode}
                    onConversationModeChange={handleConversationModeChange}
                    hitlPolicy={hitlPolicy}
                    onHitlPolicyChange={
                      hitlEnabled ? handleComposerHitlPolicyChange : undefined
                    }
                    availableConnectors={
                      isTeamChat ? undefined : chatConnectors
                    }
                    selectedConnectors={isTeamChat ? [] : selectedConnectors}
                    onConnectorsChange={
                      isTeamChat ? undefined : handleConnectorsChange
                    }
                    availableKnowledgeBases={
                      isTeamChat ? undefined : chatKnowledgeBases
                    }
                    selectedKnowledgeBaseIds={
                      isTeamChat ? [] : selectedKnowledgeBaseIds
                    }
                    onKnowledgeBaseIdsChange={
                      isTeamChat ? undefined : handleKnowledgeBaseIdsChange
                    }
                    availableSkills={isTeamChat ? undefined : chatSkills}
                    availableAgents={chatAgentOptions}
                    availableExperts={
                      isTeamChat ? teamExpertOptions : chatAgentOptionsPickable
                    }
                    availableSubagents={isTeamChat ? undefined : chatSubagents}
                    agentId={composerAgentId}
                    threadId={activeThreadId}
                    defaultModel={activeAgent?.default_model ?? null}
                    contextUsedTokens={contextUsedTokens}
                    contextMaxTokens={contextMaxTokens}
                    decisionSaved={decisionSaved}
                  />
                );
              }}
            </ChatComposerTools>
          </div>

          <ChatDockPanels
            isMobile={isMobile}
            dockOpen={dockOpen}
            dockMode={dockMode}
            isResizing={dockIsResizing}
            panelSizes={dockPanelSizes}
            agentId={resolvedAgentId ?? ""}
            filePaths={sharedExpertViewer ? [] : panelFilePaths}
            agentNameById={dockAgentNameById}
            openTabs={openTabs}
            activeTabId={activeTabId}
            onSelectTab={setDockActiveTab}
            onCloseTab={closeDockTab}
            onOpenFile={openFileAt}
            browserEnvironment={browserEnvironment}
            bridgeConnectionId={bridgeConnectionId}
            threadId={activeThreadId}
            isStreamingTurn={isStreaming}
            onModeChange={handleDockModeChange}
            onClose={handleDockClose}
            onResizeStart={dockHandleResizeStart}
            addTab={dockAddTab}
          />

          {!sharedExpertViewer && (
            <AgentProfileDrawer
              open={agentProfileOpen}
              agent={profileAgent}
              isMobile={isMobile}
              onClose={() => {
                setAgentProfileOpen(false);
                setProfileAgentId(null);
              }}
            />
          )}
          {trajectoryEnabled && (
            <TrajectoryDrawer
              agentId={resolvedAgentId ?? ""}
              threadId={activeThreadId}
              open={trajectoryDrawerOpen}
              onClose={() => setTrajectoryDrawerOpen(false)}
            />
          )}
        </div>
      </ChatToolDockProvider>
    </ChatFilePreviewProvider>
  );
}
