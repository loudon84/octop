import { act, render } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, expect, it, vi } from "vitest";
import {
  ChatToolDockProvider,
  useChatToolDock,
  type OpenToolUiPanelOptions,
} from "./ChatToolDockContext";
import {
  clearToolRenderers,
  registerToolRenderer,
} from "../../plugins/toolRenderers/registry";
import { parseOctopToolOutput } from "../../plugins/toolRenderers/parseToolOutput";
import type {
  OctopPluginUIHost,
  ToolRenderProps,
} from "../../plugins/toolRenderers/types";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "zh" } }),
}));

function Renderer(_: ToolRenderProps) {
  return null;
}

afterEach(() => clearToolRenderers());

it("creates a tool Host from the current chat scope and rejects replaced registrations", () => {
  const registration = {
    id: "context-renderer",
    pluginId: "context-plugin",
    tools: ["context_tool"],
    component: Renderer,
  };
  registerToolRenderer(registration);
  const opened = vi.fn<(options: OpenToolUiPanelOptions) => void>();
  const hostRef: { current: OctopPluginUIHost | null } = { current: null };
  const scopeIdentity = {};
  function Probe() {
    const dock = useChatToolDock()!;
    const once = useRef(false);
    if (!once.current) {
      once.current = true;
      hostRef.current = dock.createToolHost({
        registration,
        parsed: parseOctopToolOutput("plain"),
        toolName: "context_tool",
        callId: "context-call",
        isMounted: () => true,
        deactivate: vi.fn(),
      });
    }
    return null;
  }
  const children = <Probe />;
  const props = {
    dockOpen: false,
    openTabs: [],
    activeTabId: null,
    resolvedAgentId: "real-agent",
    activeThreadId: "real-thread",
    scopeIdentity,
    openToolUiPanel: opened,
    closeToolUiPanel: vi.fn(),
    focusToolUiPanel: vi.fn(),
    registerToolUiBinding: vi.fn(() => vi.fn()),
    children,
  };
  const view = render(<ChatToolDockProvider {...props} />);

  act(() =>
    hostRef.current!.openSidePanel({
      callId: "context-call",
      state: { filter: "new" },
    }),
  );
  expect(opened).toHaveBeenCalledWith(
    expect.objectContaining({
      callId: "context-call",
      pluginId: "context-plugin",
      context: {
        agentId: "real-agent",
        threadId: "real-thread",
        locale: "zh",
        theme: "light",
      },
      state: { filter: "new" },
      scopeIdentity,
    }),
  );

  registerToolRenderer({ ...registration, component: () => null });
  expect(() =>
    hostRef.current!.openSidePanel({ callId: "context-call" }),
  ).toThrow(/inactive/i);
  view.unmount();
});
