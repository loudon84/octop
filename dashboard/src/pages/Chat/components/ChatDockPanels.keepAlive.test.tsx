import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import ChatDockPanels from "./ChatDockPanels";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock("../../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => ({ id: 1 }),
}));
vi.mock("../../../utils/browserProfile", () => ({
  resolveBrowserProfile: () => "profile-1",
}));
vi.mock("../../../components/BrowserWorkspace", () => ({
  default: () => <div data-testid="browser-child" />,
}));
vi.mock("../../../components/BrowserWorkspace/ChatDockPanelShell", () => ({
  default: ({ children }: { children: ReactNode }) => (
    <div data-testid="chat-dock-panel">{children}</div>
  ),
}));
vi.mock("../../Agent/Workspace/components/WorkspaceDrawer", () => ({
  default: () => <div data-testid="workspace-child" />,
}));
vi.mock("./ChatDockFileList", () => ({
  default: () => <div data-testid="file-child" />,
}));
vi.mock("./FilePanelContent", () => ({
  default: () => <div data-testid="file-viewer-child" />,
}));
vi.mock("./KnowledgeCitationPanelContent", () => ({
  default: () => <div data-testid="knowledge-child" />,
}));
vi.mock("./ChatDockToolUiContent", () => ({
  default: () => <div data-testid="tool-reader-child" />,
}));
vi.mock("../../Control/Terminal", () => ({
  default: () => <div data-testid="terminal-child" />,
}));

const baseProps = {
  isMobile: false,
  dockMode: "right" as const,
  isResizing: false,
  panelSizes: { rightWidth: 560, bottomHeight: 380 },
  agentId: "agent-a",
  filePaths: [] as string[],
  openTabs: [{ id: "terminal" as const, kind: "terminal" as const }],
  activeTabId: "terminal" as const,
  onSelectTab: vi.fn(),
  onCloseTab: vi.fn(),
  onOpenFile: vi.fn(),
  browserEnvironment: "host" as const,
  onModeChange: vi.fn(),
  onClose: vi.fn(),
  onResizeStart: vi.fn(),
};

describe("ChatDockPanels keep-alive", () => {
  it("keeps the dock panel mounted when closed so terminal state survives", async () => {
    const { rerender } = render(
      <ChatDockPanels {...baseProps} dockOpen={true} />,
    );
    await screen.findByTestId("terminal-child");
    expect(screen.getByTestId("chat-dock-panel")).toBeTruthy();

    rerender(<ChatDockPanels {...baseProps} dockOpen={false} />);
    expect(screen.getByTestId("chat-dock-panel")).toBeTruthy();
  });

  it("does not remount when switching layout mode", async () => {
    const { rerender } = render(
      <ChatDockPanels {...baseProps} dockOpen={true} dockMode="right" />,
    );
    const first = screen.getByTestId("chat-dock-panel");
    await screen.findByTestId("terminal-child");

    rerender(
      <ChatDockPanels {...baseProps} dockOpen={true} dockMode="bottom" />,
    );
    expect(screen.getByTestId("chat-dock-panel")).toBe(first);
  });

  it("does not mount an empty dock when there are no tabs", () => {
    render(
      <ChatDockPanels
        {...baseProps}
        dockOpen={false}
        openTabs={[]}
        activeTabId={null}
      />,
    );
    expect(screen.queryByTestId("chat-dock-panel")).toBeNull();
  });

  it("keeps ordinary children alive across right, bottom, popup and hide while tool reader unmounts", async () => {
    const openTabs = [
      { id: "files" as const, kind: "files" as const },
      { id: "browser" as const, kind: "browser" as const },
      { id: "terminal" as const, kind: "terminal" as const },
      {
        id: "toolUi:probe",
        kind: "toolUi" as const,
        callId: "probe",
        activation: { id: 1, active: true },
      },
    ];
    const props = {
      ...baseProps,
      openTabs,
      dockOpen: true,
      activeTabId: "toolUi:probe",
    };
    const view = render(<ChatDockPanels {...props} />);
    await screen.findByTestId("terminal-child");
    const ordinary = [
      screen.getByTestId("file-child"),
      screen.getByTestId("browser-child"),
      screen.getByTestId("terminal-child"),
    ];
    const initialTool = screen.getByTestId("tool-reader-child");

    view.rerender(<ChatDockPanels {...props} activeTabId="browser" />);
    expect(screen.queryByTestId("tool-reader-child")).toBeNull();
    expect(screen.getByTestId("file-child")).toBe(ordinary[0]);
    expect(screen.getByTestId("browser-child")).toBe(ordinary[1]);
    expect(screen.getByTestId("terminal-child")).toBe(ordinary[2]);

    view.rerender(
      <ChatDockPanels {...props} dockMode="bottom" activeTabId="terminal" />,
    );
    view.rerender(
      <ChatDockPanels
        {...props}
        dockMode="right"
        isMobile
        activeTabId="browser"
      />,
    );
    view.rerender(
      <ChatDockPanels {...props} dockOpen={false} activeTabId="toolUi:probe" />,
    );
    expect(screen.queryByTestId("tool-reader-child")).toBeNull();
    expect(screen.getByTestId("file-child")).toBe(ordinary[0]);
    expect(screen.getByTestId("browser-child")).toBe(ordinary[1]);
    expect(screen.getByTestId("terminal-child")).toBe(ordinary[2]);

    view.rerender(
      <ChatDockPanels
        {...props}
        dockMode="bottom"
        activeTabId="toolUi:probe"
      />,
    );
    expect(screen.getByTestId("tool-reader-child")).not.toBe(initialTool);
  });
});
