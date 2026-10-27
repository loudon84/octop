import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ConnectorsPage from "./index";

const { useConnectorInstances } = vi.hoisted(() => ({
  useConnectorInstances: vi.fn(),
}));
const { oauthPending } = vi.hoisted(() => ({ oauthPending: vi.fn() }));

vi.mock("./useConnectors", () => ({ useConnectorInstances }));
vi.mock("../../../api/modules/connectors", () => ({
  connectorsApi: {
    oauthPending: (...args: unknown[]) => oauthPending(...args),
  },
}));
vi.mock("../../../layouts/PageShell", () => ({
  default: {
    Tabbed: ({
      tabBar,
      children,
    }: {
      tabBar: ReactNode;
      children: ReactNode;
    }) => (
      <div>
        {tabBar}
        <main>{children}</main>
      </div>
    ),
  },
}));
vi.mock("../../../plugins/toolRenderers/PluginSettingsSurface", async () => {
  const ReactModule = await import("react");
  return {
    PluginSettingsSurface: () => {
      const [draft, setDraft] = ReactModule.useState("");
      return (
        <label>
          Enterprise plugin setting
          <input
            value={draft}
            onChange={(event) => setDraft(event.currentTarget.value)}
          />
        </label>
      );
    },
  };
});
vi.mock("./ConnectorCard", () => ({
  ConnectorCard: ({ entry }: { entry: { name: string } }) => (
    <div>{entry.name} built-in connector</div>
  ),
}));
vi.mock("./ConnectorInstanceCard", () => ({
  ConnectorInstanceCard: ({
    instance,
  }: {
    instance: { display_name: string };
  }) => <div>{instance.display_name} enabled connector</div>,
}));
vi.mock("./ComposioTab", () => ({
  ComposioTab: () => <div>Composio applications</div>,
}));
vi.mock("./CustomMcpTab", () => ({
  CustomMcpTab: () => <div>Custom MCP servers</div>,
}));
vi.mock("../../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => ({ id: 1, role: "admin" }),
}));

function LocationProbe() {
  const location = useLocation();
  return (
    <output aria-label="Current URL">
      {location.pathname + location.search}
    </output>
  );
}

function renderPage(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <LocationProbe />
      <ConnectorsPage />
    </MemoryRouter>,
  );
}

describe("connector tab content isolation", () => {
  beforeEach(() => {
    useConnectorInstances.mockReturnValue({
      catalog: [{ kind: "builtin-a", name: "Calendar" }],
      instances: [
        {
          instance_id: "instance-1",
          kind: "builtin-a",
          display_name: "Work calendar",
          has_credentials: true,
        },
      ],
      loading: false,
      refresh: vi.fn(),
    });
  });

  it("shows plugin settings only on enabled and preserves its draft when switching tabs", async () => {
    const user = userEvent.setup();
    renderPage("/connectors?tab=enabled");

    const draft = screen.getByRole("textbox", {
      name: "Enterprise plugin setting",
    });
    expect(draft).toBeVisible();
    fireEvent.change(draft, { target: { value: "unsaved choice" } });
    expect(screen.getByText("Work calendar enabled connector")).toBeVisible();

    await user.click(
      screen.getByRole("tab", { name: "connectors.tabBuiltin" }),
    );
    expect(screen.getByText("Calendar built-in connector")).toBeVisible();
    expect(draft).not.toBeVisible();
    expect(screen.getByLabelText("Current URL")).toHaveTextContent(
      "/connectors?tab=builtin",
    );

    await user.click(
      screen.getByRole("tab", { name: "connectors.tabComposio" }),
    );
    expect(screen.getByText("Composio applications")).toBeVisible();
    expect(draft).not.toBeVisible();
    expect(screen.getByLabelText("Current URL")).toHaveTextContent(
      "/connectors?tab=composio",
    );

    await user.click(screen.getByRole("tab", { name: "connectors.tabCustom" }));
    expect(screen.getByText("Custom MCP servers")).toBeVisible();
    expect(draft).not.toBeVisible();
    expect(screen.getByLabelText("Current URL")).toHaveTextContent(
      "/connectors?tab=custom",
    );

    await user.click(
      screen.getByRole("tab", { name: "connectors.tabEnabled" }),
    );
    expect(draft).toBeVisible();
    expect(draft).toHaveValue("unsaved choice");
    await waitFor(() =>
      expect(screen.getByLabelText("Current URL")).toHaveTextContent(
        "/connectors?tab=enabled",
      ),
    );
  });

  it("does not expose plugin settings when opened directly on the built-in tab", () => {
    renderPage("/connectors?tab=builtin");

    expect(
      screen.getByRole("tab", { name: "connectors.tabBuiltin" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Calendar built-in connector")).toBeVisible();
    expect(
      screen.getByLabelText("Enterprise plugin setting"),
    ).not.toBeVisible();
    expect(screen.getByLabelText("Current URL")).toHaveTextContent(
      "/connectors?tab=builtin",
    );
  });

  it("keeps one Composio instance in Connected and excludes it from built-ins", async () => {
    useConnectorInstances.mockReturnValue({
      catalog: [
        { kind: "builtin-a", name: "Calendar" },
        { kind: "composio-connect", name: "Composio" },
      ],
      instances: [
        {
          instance_id: "composio-1",
          kind: "composio-connect",
          display_name: "My Composio",
          has_credentials: true,
        },
      ],
      loading: false,
      refresh: vi.fn(),
    });
    const user = userEvent.setup();
    renderPage("/connectors?tab=enabled");
    expect(screen.getByText("已启用 1 个连接器实例")).toBeVisible();
    expect(screen.getAllByText("My Composio enabled connector")).toHaveLength(
      1,
    );

    await user.click(
      screen.getByRole("tab", { name: "connectors.tabBuiltin" }),
    );
    expect(
      screen.queryByText("Composio built-in connector"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Calendar built-in connector")).toBeVisible();
  });

  it("returns a successful Composio popup callback to the Composio tab and clears its state query", async () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    useConnectorInstances.mockReturnValue({
      catalog: [],
      instances: [],
      loading: false,
      refresh,
    });
    oauthPending.mockResolvedValue({ kind: "composio-connect", applied: true });
    renderPage("/connectors?oauth_state=state-1");

    await waitFor(() =>
      expect(screen.getByLabelText("Current URL")).toHaveTextContent(
        "/connectors?tab=composio",
      ),
    );
    expect(oauthPending).toHaveBeenCalledWith("state-1");
    expect(refresh).toHaveBeenCalled();
    expect(screen.getByText("Composio applications")).toBeVisible();
  });

  it("claims a one-time OAuth callback only once while the claim is pending", async () => {
    let resolvePending!: (value: { kind: string; applied: boolean }) => void;
    oauthPending.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvePending = resolve;
        }),
    );
    oauthPending.mockClear();
    const refresh = vi.fn().mockResolvedValue(undefined);
    useConnectorInstances.mockReturnValue({
      catalog: [],
      instances: [],
      loading: false,
      refresh,
    });
    const view = renderPage("/connectors?oauth_state=state-once");
    await waitFor(() => expect(oauthPending).toHaveBeenCalledTimes(1));

    useConnectorInstances.mockReturnValue({
      catalog: [{ kind: "new-kind", name: "New connector" }],
      instances: [],
      loading: false,
      refresh: vi.fn().mockResolvedValue(undefined),
    });
    await act(async () => {
      view.rerender(
        <MemoryRouter initialEntries={["/connectors?oauth_state=state-once"]}>
          <LocationProbe />
          <ConnectorsPage />
        </MemoryRouter>,
      );
    });
    expect(oauthPending).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolvePending({ kind: "composio-connect", applied: true });
    });
    await waitFor(() =>
      expect(screen.getByLabelText("Current URL")).toHaveTextContent(
        "tab=composio",
      ),
    );
    expect(oauthPending).toHaveBeenCalledTimes(1);
  });
});
