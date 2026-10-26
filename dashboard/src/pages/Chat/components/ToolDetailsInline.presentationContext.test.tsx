import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { memo, useEffect, useState } from "react";
import { I18nextProvider } from "react-i18next";
import { createInstance } from "i18next";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider, useTheme } from "../../../context/ThemeContext";
import { initReactI18next } from "react-i18next";
import { request } from "../../../api/request";
import { invalidateAuthSession } from "../../../api/authSession";
import * as chatStore from "../hooks/chatStore";
import type { ChatMessage } from "../hooks/useChat";
import { useChatDockPanel } from "../hooks/useChatDockPanel";
import ChatDockToolUiContent from "./ChatDockToolUiContent";
import { ChatToolDockProvider } from "../ChatToolDockContext";
import { ToolDetailsInline } from "./MessageBubble";
import {
  clearToolRenderers,
  registerToolRenderer,
} from "../../../plugins/toolRenderers/registry";
import type { ToolRenderProps } from "../../../plugins/toolRenderers/types";

vi.unmock("react-i18next");

const fixture = vi.hoisted(() => ({
  toolMessage: null as ChatMessage | null,
}));

vi.mock("../hooks/useToolMessageByCallId", () => ({
  useToolMessageByCallId: () => fixture.toolMessage,
}));
vi.mock("../../../api/request", () => ({ request: vi.fn() }));
vi.mock("../hooks/chatStore", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../hooks/chatStore")>()),
  patchToolResultData: vi.fn(() => true),
}));

const toolData = {
  callId: "presentation-call",
  name: "presentation_probe",
  pluginId: "generic-presentation-test",
  output: JSON.stringify({
    octop_ui: { renderer: "presentation-renderer", version: 1 },
    data: { ready: true },
  }),
};
const toolMessage = {
  id: "presentation-message",
  role: "assistant",
  content: "",
  status: "done",
  timestamp: 1,
  toolData,
} as ChatMessage;
const readerState = {
  selectionId: "opaque-selection",
  cursor: { itemId: "opaque-item", offset: 7 },
  continuation: { token: "opaque-continuation" },
};

let latestHost: ToolRenderProps["host"] | null = null;
let renderedContexts: string[] = [];
let latestPanelOptions: Record<string, unknown> | null = null;
let latestActivation: { id: number; active: boolean } | null = null;
let mountsByLocation: Record<string, number> = {};
let unmountsByLocation: Record<string, number> = {};
let presentationStates: Record<string, unknown[]> = {};
let selectionsByLocation: Record<string, string[]> = {};
let cursorsByLocation: Record<string, number[]> = {};
let panelOpenCount = 0;

function PresentationRenderer(props: ToolRenderProps) {
  const [selection, setSelection] = useState("empty");
  const [cursor, setCursor] = useState(0);
  const location = props.presentation?.location ?? "inline";
  const context = props.host.getToolContext();
  latestHost = props.host;
  renderedContexts.push(`${context.locale}:${context.theme}`);
  (presentationStates[location] ??= []).push(props.presentation?.state);
  (selectionsByLocation[location] ??= []).push(selection);
  (cursorsByLocation[location] ??= []).push(cursor);
  useEffect(() => {
    mountsByLocation[location] = (mountsByLocation[location] ?? 0) + 1;
    return () => {
      unmountsByLocation[location] = (unmountsByLocation[location] ?? 0) + 1;
    };
  }, [location]);
  return (
    <div data-testid={`presentation-${location}`}>
      <output data-testid={`context-${location}`}>
        {context.locale}:{context.theme}
      </output>
      <output data-testid={`selection-${location}`}>{selection}</output>
      <output data-testid={`cursor-${location}`}>{cursor}</output>
      <button
        type="button"
        onClick={() => setSelection(`selected-${location}`)}
      >
        select-{location}
      </button>
      <button type="button" onClick={() => setCursor((value) => value + 1)}>
        advance-{location}
      </button>
      <button
        type="button"
        onClick={() =>
          props.host.openSidePanel({
            callId: props.callId!,
            state: readerState,
          })
        }
      >
        open-reader
      </button>
      <button
        type="button"
        onClick={() => props.host.closeSidePanel(props.callId!)}
      >
        close-reader
      </button>
    </div>
  );
}

const MemoPresentationRenderer = memo(PresentationRenderer);

function AppearanceControls({
  changeLanguage,
}: {
  changeLanguage: () => void;
}) {
  const { setPreference } = useTheme();
  return (
    <>
      <button type="button" onClick={changeLanguage}>
        change-language
      </button>
      <button type="button" onClick={() => setPreference("dark")}>
        change-theme
      </button>
      <button type="button" onClick={() => setPreference("system")}>
        use-system-theme
      </button>
    </>
  );
}

function DockHarness({
  i18n,
  threadId = "thread-original",
}: {
  i18n: ReturnType<typeof createInstance>;
  threadId?: string;
}) {
  const dock = useChatDockPanel(false, "agent-original", threadId);
  latestActivation =
    dock.activeTab?.kind === "toolUi" ? dock.activeTab.activation : null;
  return (
    <ChatToolDockProvider
      dockOpen={dock.dockOpen}
      openTabs={dock.openTabs}
      activeTabId={dock.activeTabId}
      resolvedAgentId="agent-original"
      activeThreadId={threadId}
      scopeIdentity={dock.toolScopeIdentity}
      openToolUiPanel={(options) => {
        panelOpenCount += 1;
        latestPanelOptions = options;
        dock.openToolUiTab(options);
      }}
      closeToolUiPanel={(callId) => dock.closeTab(`toolUi:${callId}`)}
      focusToolUiPanel={dock.focusToolUiTab}
      registerToolUiBinding={dock.registerToolUiBinding}
    >
      <ToolDetailsInline toolData={toolData} isStreaming={false} />
      {dock.dockOpen && dock.activeTab?.kind === "toolUi" ? (
        <ChatDockToolUiContent
          threadId={threadId}
          callId={dock.activeTab.callId}
          agentId="agent-original"
          isStreamingTurn={false}
        />
      ) : null}
      <AppearanceControls
        changeLanguage={() =>
          void i18n.changeLanguage(i18n.language === "en" ? "zh" : "en")
        }
      />
    </ChatToolDockProvider>
  );
}

function Harness({
  i18n,
  threadId,
}: {
  i18n: ReturnType<typeof createInstance>;
  threadId?: string;
}) {
  return (
    <I18nextProvider i18n={i18n}>
      <ThemeProvider>
        <DockHarness i18n={i18n} threadId={threadId} />
      </ThemeProvider>
    </I18nextProvider>
  );
}

function expectRevokedHost(host: ToolRenderProps["host"]) {
  const previousPanelOpenCount = panelOpenCount;
  const previousRequestCount = vi.mocked(request).mock.calls.length;
  const previousPatchCount = vi.mocked(chatStore.patchToolResultData).mock.calls
    .length;
  expect(() => host.getToolContext()).toThrow(/inactive/i);
  expect(() => host.openSidePanel({ callId: toolData.callId })).toThrow(
    /inactive/i,
  );
  expect(() => host.request("/api/should-not-start")).toThrow(/inactive/i);
  expect(() =>
    host.patchResult(toolData.callId, { wrongTarget: true }),
  ).toThrow(/inactive/i);
  expect(panelOpenCount).toBe(previousPanelOpenCount);
  expect(request).toHaveBeenCalledTimes(previousRequestCount);
  expect(chatStore.patchToolResultData).toHaveBeenCalledTimes(
    previousPatchCount,
  );
}

describe("ToolDetailsInline presentation context", () => {
  let i18n: ReturnType<typeof createInstance>;
  let previousTheme: string | null;

  beforeEach(async () => {
    previousTheme = localStorage.getItem("theme");
    localStorage.setItem(
      "theme",
      JSON.stringify({ preference: "light", palette: "default" }),
    );
    i18n = createInstance();
    await i18n.use(initReactI18next).init({
      lng: "en",
      fallbackLng: "en",
      resources: { en: { translation: {} }, zh: { translation: {} } },
      interpolation: { escapeValue: false },
      react: { useSuspense: false },
    });
    fixture.toolMessage = toolMessage;
    clearToolRenderers();
    registerToolRenderer({
      id: "presentation-renderer",
      pluginId: "generic-presentation-test",
      tools: ["presentation_probe"],
      component: MemoPresentationRenderer,
    });
    latestHost = null;
    renderedContexts = [];
    latestPanelOptions = null;
    latestActivation = null;
    mountsByLocation = {};
    unmountsByLocation = {};
    presentationStates = {};
    selectionsByLocation = {};
    cursorsByLocation = {};
    panelOpenCount = 0;
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
    clearToolRenderers();
    if (previousTheme === null) localStorage.removeItem("theme");
    else localStorage.setItem("theme", previousTheme);
  });

  it("reads fresh locale and resolved theme from the same live host", async () => {
    expect(i18n.language).toBe("en");
    render(<Harness i18n={i18n} />);
    const originalHost = latestHost!;
    expect(originalHost.getToolContext()).toMatchObject({
      agentId: "agent-original",
      threadId: "thread-original",
      locale: "en",
      theme: "light",
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "change-language" }));
    });
    fireEvent.click(screen.getByRole("button", { name: "change-theme" }));

    expect(latestHost).toBe(originalHost);
    expect(originalHost.getToolContext()).toMatchObject({
      agentId: "agent-original",
      threadId: "thread-original",
      locale: "zh",
      theme: "dark",
    });
    act(() => originalHost.openSidePanel({ callId: toolData.callId }));
    expect(latestPanelOptions).toMatchObject({
      callId: toolData.callId,
      pluginId: "generic-presentation-test",
      context: {
        agentId: "agent-original",
        threadId: "thread-original",
        locale: "zh",
        theme: "dark",
      },
    });
  });

  it("updates a memo renderer without remounting or clearing its local state", async () => {
    render(<Harness i18n={i18n} />);
    const originalHost = latestHost!;
    fireEvent.click(screen.getByRole("button", { name: "select-inline" }));
    fireEvent.click(screen.getByRole("button", { name: "advance-inline" }));
    expect(screen.getByTestId("selection-inline")).toHaveTextContent(
      "selected-inline",
    );
    expect(screen.getByTestId("cursor-inline")).toHaveTextContent("1");
    const renderCount = renderedContexts.length;
    let resolveRequest!: (value: unknown) => void;
    vi.mocked(request).mockImplementationOnce(
      () => new Promise((resolve) => (resolveRequest = resolve)) as never,
    );
    const pendingRequest = originalHost.request("/api/example");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "change-language" }));
    });
    fireEvent.click(screen.getByRole("button", { name: "change-theme" }));

    expect(screen.getByTestId("context-inline")).toHaveTextContent("zh:dark");
    expect(renderedContexts).toHaveLength(renderCount + 2);
    expect(latestHost).toBe(originalHost);
    expect(mountsByLocation.inline).toBe(1);
    expect(unmountsByLocation.inline ?? 0).toBe(0);
    expect(screen.getByTestId("selection-inline")).toHaveTextContent(
      "selected-inline",
    );
    expect(screen.getByTestId("cursor-inline")).toHaveTextContent("1");
    expect(request).toHaveBeenCalledTimes(1);
    resolveRequest({ ok: true });
    await expect(pendingRequest).resolves.toEqual({ ok: true });
  });

  it("preserves reader state, activation, local state, and its pending request", async () => {
    render(<Harness i18n={i18n} />);
    const inlineHost = latestHost!;
    fireEvent.click(screen.getByRole("button", { name: "open-reader" }));
    expect(screen.getByTestId("context-side-panel")).toHaveTextContent(
      "en:light",
    );
    const readerHost = latestHost!;
    const readerActivation = latestActivation!;
    const readerStateBefore = presentationStates["side-panel"].at(-1);
    expect(readerStateBefore).toEqual(readerState);
    expect(readerStateBefore).not.toBe(readerState);
    expect(readerActivation).toMatchObject({ active: true });
    fireEvent.click(screen.getByRole("button", { name: "select-side-panel" }));
    fireEvent.click(screen.getByRole("button", { name: "advance-side-panel" }));
    expect(screen.getByTestId("selection-side-panel")).toHaveTextContent(
      "selected-side-panel",
    );
    expect(screen.getByTestId("cursor-side-panel")).toHaveTextContent("1");
    const readerMountCount = mountsByLocation["side-panel"];
    const readerUnmountCount = unmountsByLocation["side-panel"] ?? 0;
    expect(readerMountCount).toBe(1);
    expect(readerUnmountCount).toBe(0);
    let resolveReaderRequest!: (value: unknown) => void;
    vi.mocked(request).mockImplementationOnce(
      () => new Promise((resolve) => (resolveReaderRequest = resolve)) as never,
    );
    const pendingReaderRequest = readerHost.request("/api/reader-pending");
    const requestCountBeforeUpdate = vi.mocked(request).mock.calls.length;

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "change-language" }));
    });
    fireEvent.click(screen.getByRole("button", { name: "change-theme" }));
    expect(screen.getByTestId("context-side-panel")).toHaveTextContent(
      "zh:dark",
    );
    expect(latestHost).toBe(readerHost);
    expect(latestActivation).toBe(readerActivation);
    expect(latestActivation).toMatchObject({ active: true });
    expect(latestActivation!.id).toBe(readerActivation.id);
    const readerStateAfter = presentationStates["side-panel"].at(-1);
    expect(readerStateAfter).toBe(readerStateBefore);
    expect(readerStateAfter).toEqual(readerState);
    expect(selectionsByLocation["side-panel"].at(-1)).toBe(
      "selected-side-panel",
    );
    expect(cursorsByLocation["side-panel"].at(-1)).toBe(1);
    expect(mountsByLocation["side-panel"]).toBe(readerMountCount);
    expect(unmountsByLocation["side-panel"] ?? 0).toBe(readerUnmountCount);
    expect(request).toHaveBeenCalledTimes(requestCountBeforeUpdate);
    expectRevokedHost(inlineHost);
    resolveReaderRequest({ result: "reader-complete" });
    await expect(pendingReaderRequest).resolves.toEqual({
      result: "reader-complete",
    });
  });

  it("keeps a closed reader Host revoked when reopening the same call id", async () => {
    render(<Harness i18n={i18n} />);
    fireEvent.click(screen.getByRole("button", { name: "open-reader" }));
    const oldReaderHost = latestHost!;
    const oldActivation = latestActivation!;
    fireEvent.click(screen.getByRole("button", { name: "close-reader" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "change-language" }));
    });
    fireEvent.click(screen.getByRole("button", { name: "change-theme" }));
    expectRevokedHost(oldReaderHost);

    fireEvent.click(screen.getByRole("button", { name: "open-reader" }));
    const newReaderHost = latestHost!;
    expect(newReaderHost).not.toBe(oldReaderHost);
    expect(latestActivation).not.toBe(oldActivation);
    expect(latestActivation!.id).toBeGreaterThan(oldActivation.id);
    expect(newReaderHost.getToolContext()).toMatchObject({
      locale: "zh",
      theme: "dark",
      agentId: "agent-original",
      threadId: "thread-original",
    });
    expectRevokedHost(oldReaderHost);
  });

  it("updates resolved system theme from a matchMedia change event", async () => {
    let systemDark = false;
    const listeners = new Set<(event: MediaQueryListEvent) => void>();
    const originalMatchMedia = window.matchMedia;
    const mediaQueryList = {
      get matches() {
        return systemDark;
      },
      media: "(prefers-color-scheme: dark)",
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: (
        _type: string,
        listener: (event: MediaQueryListEvent) => void,
      ) => listeners.add(listener),
      removeEventListener: (
        _type: string,
        listener: (event: MediaQueryListEvent) => void,
      ) => listeners.delete(listener),
      dispatchEvent: vi.fn(),
    } as unknown as MediaQueryList;
    window.matchMedia = vi.fn(() => mediaQueryList);
    try {
      render(<Harness i18n={i18n} />);
      const host = latestHost!;
      fireEvent.click(screen.getByRole("button", { name: "use-system-theme" }));
      act(() => {
        systemDark = true;
        listeners.forEach((listener) =>
          listener({ matches: true } as MediaQueryListEvent),
        );
      });
      expect(host.getToolContext()).toMatchObject({ theme: "dark" });
      expect(latestHost).toBe(host);
      expect(screen.getByTestId("context-inline")).toHaveTextContent("en:dark");
    } finally {
      window.matchMedia = originalMatchMedia;
    }
  });

  it.each(["scope", "registration", "auth"] as const)(
    "keeps all old Host methods blocked after %s invalidation and metadata flip",
    async (trigger) => {
      const view = render(<Harness i18n={i18n} />);
      const oldHost = latestHost!;
      const pendingRequests = new Map<
        string,
        { resolve(value: unknown): void; reject(reason: unknown): void }
      >();
      vi.mocked(request).mockImplementation(
        (path: string) =>
          new Promise((resolve, reject) =>
            pendingRequests.set(path, { resolve, reject }),
          ) as never,
      );
      const fulfilledRequest = oldHost.request("/api/pending-fulfilled");
      const rejectedRequest = oldHost.request("/api/pending-rejected");
      expect(request).toHaveBeenCalledTimes(2);

      if (trigger === "scope") {
        view.rerender(<Harness i18n={i18n} threadId="thread-next" />);
      } else if (trigger === "registration") {
        act(() =>
          registerToolRenderer({
            id: "presentation-renderer",
            pluginId: "generic-presentation-test",
            tools: ["presentation_probe"],
            component: () => null,
          }),
        );
      } else {
        act(() => invalidateAuthSession());
      }

      await act(async () => {
        fireEvent.click(
          screen.getByRole("button", { name: "change-language" }),
        );
      });
      fireEvent.click(screen.getByRole("button", { name: "change-theme" }));
      expectRevokedHost(oldHost);
      expect(request).toHaveBeenCalledTimes(2);
      expect(chatStore.patchToolResultData).not.toHaveBeenCalled();

      pendingRequests.get("/api/pending-fulfilled")!.resolve({
        threadId: "thread-original",
        result: "late-success",
      });
      pendingRequests
        .get("/api/pending-rejected")!
        .reject(new Error("late upstream failure"));
      await expect(fulfilledRequest).rejects.toThrow(/inactive/i);
      await expect(rejectedRequest).rejects.toThrow(/inactive/i);
      expect(chatStore.patchToolResultData).not.toHaveBeenCalled();
    },
  );
});
