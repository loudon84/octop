import {
  createContext,
  useCallback,
  useContext,
  useRef,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";
import { useTheme } from "../../context/ThemeContext";
import { dockToolUiTabId } from "./utils/dockToolUiTabId";
import type {
  DockTab,
  DockTabId,
  ToolUiActivation,
} from "./hooks/useChatDockPanel";
import {
  getAuthSessionEpoch,
  isAuthSessionCurrent,
  subscribeAuthSession,
} from "../../api/authSession";
import { createPluginUiToolHost } from "../../plugins/toolRenderers/host";
import {
  listRegisteredRenderers,
  resolveToolRenderer,
} from "../../plugins/toolRenderers/registry";
import type {
  OpenSidePanelOptions,
  ParsedToolOutput,
  ToolRenderContext,
  ToolRendererRegistration,
} from "../../plugins/toolRenderers/types";

export interface OpenToolUiPanelOptions {
  callId: string;
  title?: string;
  toolName?: string;
  pluginId?: string;
  context?: ToolRenderContext;
  state?: OpenSidePanelOptions["state"];
  scopeIdentity?: object;
  activation?: ToolUiActivation;
  returnFocus?: () => void;
}

interface ChatToolDockContextValue {
  openToolUiPanel: (opts: OpenToolUiPanelOptions) => void;
  closeToolUiPanel: (callId: string) => void;
  focusToolUiPanel: (callId: string) => void;
  registerToolUiBinding: (
    callId: string,
    lease: object,
    deactivate: () => void,
  ) => () => void;
  isToolUiDocked: (callId: string | undefined) => boolean;
  getToolUiTab: (
    callId: string | undefined,
  ) => Extract<DockTab, { kind: "toolUi" }> | null;
  isCurrentScope: (identity: object | undefined) => boolean;
  getCurrentScopeIdentity: () => object;
  createToolHost: (input: {
    registration: ToolRendererRegistration;
    parsed: ParsedToolOutput;
    toolName: string;
    callId: string;
    isMounted: () => boolean;
    deactivate: () => void;
    returnFocus?: () => void;
  }) => ReturnType<typeof createPluginUiToolHost>;
}

const ChatToolDockContext = createContext<ChatToolDockContextValue | null>(
  null,
);

export function ChatToolDockProvider({
  dockOpen,
  openTabs,
  activeTabId,
  resolvedAgentId,
  activeThreadId,
  scopeIdentity,
  openToolUiPanel,
  closeToolUiPanel,
  focusToolUiPanel,
  registerToolUiBinding,
  children,
}: {
  dockOpen: boolean;
  openTabs: DockTab[];
  activeTabId: DockTabId | null;
  resolvedAgentId: string | null;
  activeThreadId: string | null;
  scopeIdentity: object;
  openToolUiPanel: (opts: OpenToolUiPanelOptions) => void;
  closeToolUiPanel: (callId: string) => void;
  focusToolUiPanel: (callId: string) => void;
  registerToolUiBinding: (
    callId: string,
    lease: object,
    deactivate: () => void,
  ) => () => void;
  children: ReactNode;
}) {
  const { i18n } = useTranslation();
  const locale = i18n.language || "en";
  const { mode: theme } = useTheme();
  const authEpoch = useSyncExternalStore(
    subscribeAuthSession,
    getAuthSessionEpoch,
    getAuthSessionEpoch,
  );
  const latestRef = useRef({
    dockOpen,
    openTabs,
    activeTabId,
    openToolUiPanel,
    closeToolUiPanel,
    focusToolUiPanel,
    registerToolUiBinding,
    scopeIdentity,
    authEpoch,
    resolvedAgentId,
    activeThreadId,
    locale,
    theme,
  });
  latestRef.current = {
    dockOpen,
    openTabs,
    activeTabId,
    openToolUiPanel,
    closeToolUiPanel,
    focusToolUiPanel,
    registerToolUiBinding,
    scopeIdentity,
    authEpoch,
    resolvedAgentId,
    activeThreadId,
    locale,
    theme,
  };
  const openPanel = useCallback((opts: OpenToolUiPanelOptions) => {
    const current = latestRef.current;
    current.openToolUiPanel({
      ...opts,
      scopeIdentity: current.scopeIdentity,
      context: {
        agentId: current.resolvedAgentId,
        threadId: current.activeThreadId,
        locale: current.locale,
        theme: current.theme,
      },
    });
  }, []);
  const closePanel = useCallback(
    (callId: string) => latestRef.current.closeToolUiPanel(callId),
    [],
  );
  const focusPanel = useCallback(
    (callId: string) => latestRef.current.focusToolUiPanel(callId),
    [],
  );
  const isToolUiDocked = useCallback((callId: string | undefined) => {
    const current = latestRef.current;
    if (
      !isAuthSessionCurrent(current.authEpoch) ||
      !callId ||
      !current.dockOpen ||
      current.activeTabId == null
    )
      return false;
    const active = current.openTabs.find(
      (tab) => tab.id === current.activeTabId,
    );
    return active?.kind === "toolUi" && active.callId === callId;
  }, []);
  const getToolUiTab = useCallback((callId: string | undefined) => {
    const current = latestRef.current;
    if (!isAuthSessionCurrent(current.authEpoch) || !callId) return null;
    return (
      current.openTabs.find(
        (tab): tab is Extract<DockTab, { kind: "toolUi" }> =>
          tab.kind === "toolUi" && tab.callId === callId,
      ) ?? null
    );
  }, []);
  const isCurrentScope = useCallback((identity: object | undefined) => {
    const current = latestRef.current;
    return (
      !!identity &&
      identity === current.scopeIdentity &&
      isAuthSessionCurrent(current.authEpoch)
    );
  }, []);
  const getCurrentScopeIdentity = useCallback(
    () => latestRef.current.scopeIdentity,
    [],
  );
  const createToolHost = useCallback<
    ChatToolDockContextValue["createToolHost"]
  >(
    ({
      registration,
      parsed,
      toolName,
      callId,
      isMounted,
      deactivate,
      returnFocus,
    }) => {
      const captured = latestRef.current;
      const capturedScope = captured.scopeIdentity;
      const capturedEpoch = captured.authEpoch;
      const isCurrentRegistration = () =>
        listRegisteredRenderers().includes(registration) &&
        resolveToolRenderer({
          toolName,
          pluginId: registration.pluginId,
          parsed,
        }) === registration;
      const isActive = () =>
        latestRef.current.scopeIdentity === capturedScope &&
        isAuthSessionCurrent(capturedEpoch) &&
        isMounted() &&
        !latestRef.current.openTabs.some(
          (tab) =>
            tab.kind === "toolUi" &&
            tab.callId === callId &&
            tab.pluginId !== undefined &&
            tab.pluginId !== registration.pluginId,
        ) &&
        isCurrentRegistration();
      const context: ToolRenderContext = {
        agentId: captured.resolvedAgentId,
        threadId: captured.activeThreadId,
        locale: captured.locale,
        theme: captured.theme,
      };
      return createPluginUiToolHost(registration.pluginId, context, {
        callId,
        isActive,
        getPresentationContext: () => ({
          locale: latestRef.current.locale,
          theme: latestRef.current.theme,
        }),
        openSidePanel: (options) => {
          if (!isActive()) throw new Error("Plugin UI host is inactive");
          deactivate();
          latestRef.current.openToolUiPanel({
            ...options,
            scopeIdentity: capturedScope,
            returnFocus,
          });
        },
        closeSidePanel: (targetCallId) => {
          if (!isActive()) throw new Error("Plugin UI host is inactive");
          latestRef.current.closeToolUiPanel(targetCallId);
        },
      });
    },
    [],
  );
  const value: ChatToolDockContextValue = {
    openToolUiPanel: openPanel,
    closeToolUiPanel: closePanel,
    focusToolUiPanel: focusPanel,
    isToolUiDocked,
    getToolUiTab,
    isCurrentScope,
    getCurrentScopeIdentity,
    createToolHost,
    registerToolUiBinding,
  };

  return (
    <ChatToolDockContext.Provider value={value}>
      {children}
    </ChatToolDockContext.Provider>
  );
}

export function useChatToolDock(): ChatToolDockContextValue | null {
  return useContext(ChatToolDockContext);
}

export function dockTabIdForToolCall(callId: string): DockTabId {
  return dockToolUiTabId(callId);
}
