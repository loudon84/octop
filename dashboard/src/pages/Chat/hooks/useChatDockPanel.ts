import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import type { PanelMode } from "../../../components/BrowserWorkspace";
import {
  canonicalizeDockFilePath,
  dockFileTabId,
  isHostAbsolutePath,
  normalizeDockFilePath,
} from "../utils/dockFilePath";
import type { KnowledgeCitation } from "../../../utils/parseKnowledgeCitations";
import { dockKnowledgeTabId } from "../utils/dockKnowledgeTabId";
import { dockToolUiTabId } from "../utils/dockToolUiTabId";
import type {
  PluginJson,
  ToolRenderContext,
} from "../../../plugins/toolRenderers/types";
import {
  getAuthSessionEpoch,
  subscribeAuthSession,
} from "../../../api/authSession";
import { usePanelResize, type PanelSizes } from "./usePanelResize";

const PANEL_MODE_KEY = "octop:chat-dock:mode";
const PANEL_SIZE_KEY = "octop:chat-dock:size";
/** Legacy keys — read once for migration. */
const LEGACY_FILE_MODE_KEY = "octop:file-panel:mode";
const LEGACY_BROWSER_MODE_KEY = "octop:browser-panel:mode";
const LEGACY_FILE_SIZE_KEY = "octop:file-panel:size";
const LEGACY_BROWSER_SIZE_KEY = "octop:browser-panel:size";

export type DockTab =
  | { id: "files"; kind: "files" }
  | { id: "workspace"; kind: "workspace" }
  | { id: "browser"; kind: "browser" }
  | { id: "terminal"; kind: "terminal" }
  | { id: string; kind: "file"; path: string; agentId?: string }
  | {
      id: string;
      kind: "knowledge";
      citation: KnowledgeCitation;
    }
  | {
      id: string;
      kind: "toolUi";
      callId: string;
      title?: string;
      toolName?: string;
      pluginId?: string;
      state?: PluginJson;
      scopeIdentity?: object;
      activation: ToolUiActivation;
      returnFocus?: () => void;
    };

export interface ToolUiActivation {
  id: number;
  active: boolean;
}

export type DockTabId = DockTab["id"];

/** Drop retired dock tabs (pre-drawer trajectory) if they appear in stored lists. */
export function ensureNoTrajectoryTab<T extends { kind: string }>(
  tabs: readonly T[],
): T[] {
  return tabs.filter((t) => t.kind !== "trajectory");
}

function loadPanelMode(): PanelMode {
  try {
    const saved = localStorage.getItem(PANEL_MODE_KEY);
    if (saved === "bottom" || saved === "right" || saved === "popup") {
      return saved;
    }
    for (const key of [LEGACY_FILE_MODE_KEY, LEGACY_BROWSER_MODE_KEY]) {
      const legacy = localStorage.getItem(key);
      if (legacy === "bottom" || legacy === "right" || legacy === "popup") {
        return legacy;
      }
    }
  } catch {
    /* ignore */
  }
  return "right";
}

function loadPanelSizes(): PanelSizes {
  try {
    const saved = localStorage.getItem(PANEL_SIZE_KEY);
    if (saved) {
      return JSON.parse(saved) as PanelSizes;
    }
    for (const key of [LEGACY_FILE_SIZE_KEY, LEGACY_BROWSER_SIZE_KEY]) {
      const legacy = localStorage.getItem(key);
      if (legacy) {
        return JSON.parse(legacy) as PanelSizes;
      }
    }
  } catch {
    /* ignore */
  }
  return { rightWidth: 560, bottomHeight: 380 };
}

function persistPanelSizes(sizes: PanelSizes) {
  try {
    localStorage.setItem(PANEL_SIZE_KEY, JSON.stringify(sizes));
  } catch {
    /* ignore */
  }
}

function ensureFilesTab(tabs: DockTab[]): DockTab[] {
  if (tabs.some((t) => t.id === "files")) return tabs;
  return [{ id: "files", kind: "files" }, ...tabs];
}

function fallbackActiveId(
  tabs: DockTab[],
  closedId: DockTabId,
  prevActive: DockTabId | null,
): DockTabId | null {
  if (tabs.length === 0) return null;
  if (prevActive !== closedId && tabs.some((t) => t.id === prevActive)) {
    return prevActive;
  }
  if (tabs.some((t) => t.id === "files")) return "files";
  return tabs[tabs.length - 1]?.id ?? null;
}

/**
 * Shared chat dock with tabbed file list / file viewers / browser / terminal.
 */
export function useChatDockPanel(
  isMobile: boolean,
  agentId?: string | null,
  threadId?: string | null,
) {
  const [dockOpen, setDockOpen] = useState(false);
  const [dockMode, setDockMode] = useState<PanelMode>(loadPanelMode);
  const [openTabs, setOpenTabs] = useState<DockTab[]>([]);
  const [activeTabId, setActiveTabId] = useState<DockTabId | null>(null);
  const authEpoch = useSyncExternalStore(
    subscribeAuthSession,
    getAuthSessionEpoch,
    getAuthSessionEpoch,
  );
  // These values intentionally invalidate an otherwise opaque identity token.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const toolScopeIdentity = useMemo(() => ({}), [agentId, threadId, authEpoch]);
  const visibleOpenTabs = openTabs.filter(
    (tab) => tab.kind !== "toolUi" || tab.scopeIdentity === toolScopeIdentity,
  );
  const openTabsRef = useRef(visibleOpenTabs);
  const activeTabIdRef = useRef(activeTabId);
  const nextToolActivationId = useRef(0);
  const pendingToolFocus = useRef<(() => void) | undefined>(undefined);
  const toolBindingsRef = useRef(new Map<string, Map<object, () => void>>());
  openTabsRef.current = visibleOpenTabs;
  activeTabIdRef.current = activeTabId;

  const deactivateActiveToolUi = useCallback(() => {
    const id = activeTabIdRef.current;
    const tab = openTabsRef.current.find((item) => item.id === id);
    if (tab?.kind === "toolUi") {
      tab.activation.active = false;
      for (const deactivate of toolBindingsRef.current
        .get(tab.callId)
        ?.values() ?? []) {
        deactivate();
      }
    }
  }, []);
  const registerToolUiBinding = useCallback(
    (callId: string, lease: object, deactivate: () => void) => {
      const bindings = toolBindingsRef.current.get(callId) ?? new Map();
      bindings.set(lease, deactivate);
      toolBindingsRef.current.set(callId, bindings);
      return () => {
        const current = toolBindingsRef.current.get(callId);
        current?.delete(lease);
        if (current?.size === 0) toolBindingsRef.current.delete(callId);
      };
    },
    [],
  );
  const deactivateToolUiBindings = useCallback((callId: string) => {
    for (const deactivate of toolBindingsRef.current.get(callId)?.values() ??
      []) {
      deactivate();
    }
  }, []);

  useLayoutEffect(() => {
    const focus = pendingToolFocus.current;
    if (!focus) return;
    pendingToolFocus.current = undefined;
    focus();
  }, [openTabs, activeTabId, dockOpen]);

  useEffect(
    () =>
      subscribeAuthSession(() => {
        for (const tab of openTabsRef.current) {
          if (tab.kind === "toolUi") tab.activation.active = false;
        }
        pendingToolFocus.current = undefined;
      }),
    [],
  );

  useLayoutEffect(() => {
    const stale = openTabs.filter(
      (tab) => tab.kind === "toolUi" && tab.scopeIdentity !== toolScopeIdentity,
    );
    if (!stale.length) return;
    for (const tab of stale) {
      if (tab.kind === "toolUi") tab.activation.active = false;
    }
    const nextTabs = openTabs.filter(
      (tab) => tab.kind !== "toolUi" || tab.scopeIdentity === toolScopeIdentity,
    );
    if (stale.some((tab) => tab.id === activeTabId)) {
      const nextActiveId = nextTabs.some((tab) => tab.id === "files")
        ? "files"
        : nextTabs[0]?.id ?? null;
      activeTabIdRef.current = nextActiveId;
      setActiveTabId(nextActiveId);
      if (!nextTabs.length) setDockOpen(false);
    }
    setOpenTabs(nextTabs);
  }, [openTabs, toolScopeIdentity, activeTabId]);
  const { panelSizes, isResizing, handleResizeStart } = usePanelResize(
    loadPanelSizes(),
    persistPanelSizes,
  );

  // File paths and browser sessions are agent-scoped. Only wipe tabs when
  // switching between two real agents (ignore null ↔ id first-paint races).
  const prevAgentIdRef = useRef(agentId);
  useEffect(() => {
    const prev = prevAgentIdRef.current;
    prevAgentIdRef.current = agentId;
    if (prev == null || agentId == null || prev === agentId) return;
    deactivateActiveToolUi();
    setDockOpen(false);
    setOpenTabs([]);
    setActiveTabId(null);
  }, [agentId, deactivateActiveToolUi]);

  const openDock = useCallback(() => {
    setDockOpen(true);
    if (isMobile) {
      setDockMode("bottom");
    }
  }, [isMobile]);

  const handleClose = useCallback(() => {
    const active = openTabsRef.current.find(
      (tab) => tab.id === activeTabIdRef.current,
    );
    if (active?.kind === "toolUi") {
      deactivateActiveToolUi();
      deactivateToolUiBindings(active.callId);
      pendingToolFocus.current = active.returnFocus;
    }
    setDockOpen(false);
    // Closing the dock restores tool UIs to the message stream.
    setOpenTabs((prev) => {
      const next = prev.filter((t) => t.kind !== "toolUi");
      setActiveTabId((current) => {
        if (current == null) return null;
        if (next.some((t) => t.id === current)) return current;
        return next[0]?.id ?? null;
      });
      return next;
    });
  }, [deactivateActiveToolUi, deactivateToolUiBindings]);

  const openFileList = useCallback(() => {
    deactivateActiveToolUi();
    setOpenTabs((prev) => ensureFilesTab(prev));
    setActiveTabId("files");
    openDock();
  }, [deactivateActiveToolUi, openDock]);

  const openFileAt = useCallback(
    (path?: string | null, fileAgentId?: string | null) => {
      deactivateActiveToolUi();
      if (!path?.trim()) {
        openFileList();
        return;
      }
      const ownerId = (fileAgentId || agentId || "").trim() || null;
      const hostAbs = normalizeDockFilePath(path);
      // Keep host-absolute tool paths. Collapsing ``~/.octop/agents/<id>/…`` to a
      // relative key breaks virtual ``root_dir`` nests (bytes live under
      // ``{root}/Users/…/.octop/agents/<id>/…``, not the real agent home).
      const tabPath = isHostAbsolutePath(hostAbs)
        ? hostAbs
        : canonicalizeDockFilePath(path, ownerId);
      if (!tabPath) {
        openFileList();
        return;
      }
      const id = dockFileTabId(tabPath, ownerId);
      setOpenTabs((prev) => {
        if (prev.some((t) => t.id === id)) return prev;
        return [
          ...prev,
          {
            id,
            kind: "file" as const,
            path: tabPath,
            ...(ownerId ? { agentId: ownerId } : {}),
          },
        ];
      });
      setActiveTabId(id);
      openDock();
    },
    [agentId, openDock, openFileList, deactivateActiveToolUi],
  );

  const openWorkspaceTab = useCallback(() => {
    deactivateActiveToolUi();
    setOpenTabs((prev) => {
      if (prev.some((t) => t.id === "workspace")) return prev;
      return [...prev, { id: "workspace", kind: "workspace" }];
    });
    setActiveTabId("workspace");
    openDock();
  }, [deactivateActiveToolUi, openDock]);

  const openBrowserTab = useCallback(() => {
    deactivateActiveToolUi();
    setOpenTabs((prev) => {
      if (prev.some((t) => t.id === "browser")) return prev;
      return [...prev, { id: "browser", kind: "browser" }];
    });
    setActiveTabId("browser");
    openDock();
  }, [deactivateActiveToolUi, openDock]);

  const openTerminalTab = useCallback(() => {
    deactivateActiveToolUi();
    setOpenTabs((prev) => {
      if (prev.some((t) => t.id === "terminal")) return prev;
      return [...prev, { id: "terminal", kind: "terminal" }];
    });
    setActiveTabId("terminal");
    openDock();
  }, [deactivateActiveToolUi, openDock]);

  const openKnowledgeCitation = useCallback(
    (citation: KnowledgeCitation) => {
      const kbId = citation.kbId?.trim();
      const docId = citation.docId?.trim();
      if (!kbId || !docId) return;
      const id = dockKnowledgeTabId(kbId, docId);
      deactivateActiveToolUi();
      setOpenTabs((prev) => {
        const existing = prev.find((t) => t.id === id);
        if (existing?.kind === "knowledge") {
          return prev.map((t) => (t.id === id ? { ...t, citation } : t));
        }
        if (existing) return prev;
        return [...prev, { id, kind: "knowledge" as const, citation }];
      });
      setActiveTabId(id);
      openDock();
    },
    [openDock, deactivateActiveToolUi],
  );

  const openToolUiTab = useCallback(
    (opts: {
      callId: string;
      title?: string;
      toolName?: string;
      pluginId?: string;
      context?: ToolRenderContext;
      state?: PluginJson;
      scopeIdentity?: object;
      returnFocus?: () => void;
    }) => {
      const callId = opts.callId?.trim();
      if (!callId) return;
      deactivateActiveToolUi();
      deactivateToolUiBindings(callId);
      const activation: ToolUiActivation = {
        id: ++nextToolActivationId.current,
        active: true,
      };
      const id = dockToolUiTabId(callId);
      setOpenTabs((prev) => {
        const existing = prev.find((tab) => tab.id === id);
        const nextTab = {
          id,
          kind: "toolUi" as const,
          callId,
          title:
            opts.title ??
            (existing?.kind === "toolUi" ? existing.title : undefined),
          toolName:
            opts.toolName ??
            (existing?.kind === "toolUi" ? existing.toolName : undefined),
          pluginId: opts.pluginId,
          ...(Object.prototype.hasOwnProperty.call(opts, "state")
            ? { state: opts.state }
            : {}),
          scopeIdentity: opts.scopeIdentity ?? toolScopeIdentity,
          activation,
          returnFocus:
            opts.returnFocus ??
            (existing?.kind === "toolUi" ? existing.returnFocus : undefined),
        };
        return existing
          ? prev.map((tab) => (tab.id === id ? nextTab : tab))
          : [...prev, nextTab];
      });
      setActiveTabId(id);
      openDock();
    },
    [
      deactivateActiveToolUi,
      deactivateToolUiBindings,
      openDock,
      toolScopeIdentity,
    ],
  );

  const focusToolUiTab = useCallback(
    (callId: string) => {
      const id = dockToolUiTabId(callId);
      const entering = activeTabIdRef.current !== id || !dockOpen;
      if (entering) {
        deactivateActiveToolUi();
        deactivateToolUiBindings(callId);
      }
      const target = openTabsRef.current.find((tab) => tab.id === id);
      if (target?.kind === "toolUi" && !target.activation.active) {
        const activation = { id: ++nextToolActivationId.current, active: true };
        setOpenTabs((prev) =>
          prev.map((tab) =>
            tab.id === id && tab.kind === "toolUi"
              ? { ...tab, activation }
              : tab,
          ),
        );
      }
      setActiveTabId(id);
      openDock();
    },
    [openDock, deactivateActiveToolUi, deactivateToolUiBindings, dockOpen],
  );

  /** Toggle dock open/closed around a dedicated tab. */
  const toggleDockTab = useCallback(
    (tab: Extract<DockTab, { kind: "browser" | "terminal" | "workspace" }>) => {
      deactivateActiveToolUi();
      setDockOpen((prevOpen) => {
        if (prevOpen && activeTabId === tab.id) {
          return false;
        }
        setOpenTabs((prev) => {
          if (prev.some((t) => t.id === tab.id)) return prev;
          return [...prev, tab];
        });
        setActiveTabId(tab.id);
        return true;
      });
      if (isMobile) {
        setDockMode("bottom");
      }
    },
    [activeTabId, isMobile, deactivateActiveToolUi],
  );

  const toggleWorkspacePanel = useCallback(() => {
    toggleDockTab({ id: "workspace", kind: "workspace" });
  }, [toggleDockTab]);

  const toggleBrowserPanel = useCallback(() => {
    toggleDockTab({ id: "browser", kind: "browser" });
  }, [toggleDockTab]);

  const toggleTerminalPanel = useCallback(() => {
    toggleDockTab({ id: "terminal", kind: "terminal" });
  }, [toggleDockTab]);

  const closeTab = useCallback(
    (id: DockTabId) => {
      const closing = openTabsRef.current.find((tab) => tab.id === id);
      const wasActive = activeTabIdRef.current === id;
      if (wasActive && closing?.kind === "toolUi") {
        deactivateActiveToolUi();
        pendingToolFocus.current = closing.returnFocus;
      }
      const next = openTabsRef.current.filter((tab) => tab.id !== id);
      const nextActiveId = fallbackActiveId(next, id, activeTabIdRef.current);
      const fallback = next.find((tab) => tab.id === nextActiveId);
      const fallbackActivation =
        fallback?.kind === "toolUi" && !fallback.activation.active
          ? { id: ++nextToolActivationId.current, active: true }
          : undefined;
      if (fallback?.kind === "toolUi" && fallbackActivation) {
        deactivateToolUiBindings(fallback.callId);
      }
      const activatedNext = fallbackActivation
        ? next.map((tab) =>
            tab.id === nextActiveId && tab.kind === "toolUi"
              ? { ...tab, activation: fallbackActivation }
              : tab,
          )
        : next;
      openTabsRef.current = activatedNext;
      activeTabIdRef.current = nextActiveId;
      setOpenTabs(activatedNext);
      setActiveTabId(nextActiveId);
      if (next.length === 0) setDockOpen(false);
    },
    [deactivateActiveToolUi, deactivateToolUiBindings],
  );

  const setActiveTab = useCallback(
    (id: DockTabId) => {
      const target = openTabsRef.current.find((tab) => tab.id === id);
      const entering = activeTabIdRef.current !== id || !dockOpen;
      if (entering) {
        deactivateActiveToolUi();
        if (target?.kind === "toolUi") {
          deactivateToolUiBindings(target.callId);
        }
      }
      if (target?.kind === "toolUi" && target.activation.active === false) {
        const activation = { id: ++nextToolActivationId.current, active: true };
        setOpenTabs((prev) =>
          prev.map((tab) =>
            tab.id === id && tab.kind === "toolUi"
              ? { ...tab, activation }
              : tab,
          ),
        );
      }
      setActiveTabId(id);
      setDockOpen(true);
    },
    [deactivateActiveToolUi, deactivateToolUiBindings, dockOpen],
  );

  const handleModeChange = useCallback(
    (mode: PanelMode) => {
      if (isMobile && mode === "right") {
        setDockMode("bottom");
        return;
      }
      setDockMode(mode);
    },
    [isMobile],
  );

  useEffect(() => {
    try {
      localStorage.setItem(PANEL_MODE_KEY, dockMode);
    } catch {
      /* ignore */
    }
  }, [dockMode]);

  const activeTab =
    visibleOpenTabs.find((t) => t.id === activeTabId) ??
    visibleOpenTabs[0] ??
    null;
  const filePath = activeTab?.kind === "file" ? activeTab.path : null;

  return {
    dockOpen,
    dockMode,
    filePath,
    openTabs: visibleOpenTabs,
    toolScopeIdentity,
    activeTabId,
    activeTab,
    panelSizes,
    isResizing,
    handleResizeStart,
    handleClose,
    handleModeChange,
    openFileAt,
    openFileList,
    openKnowledgeCitation,
    openWorkspaceTab,
    toggleWorkspacePanel,
    openBrowserTab,
    toggleBrowserPanel,
    openTerminalTab,
    toggleTerminalPanel,
    openToolUiTab,
    focusToolUiTab,
    closeTab,
    setActiveTab,
    registerToolUiBinding,
  };
}
