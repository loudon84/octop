import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ComposioTab } from "./ComposioTab";

const {
  api,
  tryOpenAuthPopup,
  navigateAuthWindow,
  navigateCurrentTab,
  confirm,
  messages,
} = vi.hoisted(() => ({
  api: {
    composioGet: vi.fn(),
    oauthStart: vi.fn(),
    oauthPending: vi.fn(),
    oauthCancel: vi.fn(),
    composioCheck: vi.fn(),
    composioDisable: vi.fn(),
    composioEnable: vi.fn(),
    composioDisconnect: vi.fn(),
    composioSearchApps: vi.fn(),
    composioAuthorizeApp: vi.fn(),
  },
  tryOpenAuthPopup: vi.fn(),
  navigateAuthWindow: vi.fn(),
  navigateCurrentTab: vi.fn(),
  confirm: vi.fn(),
  messages: { error: vi.fn(), info: vi.fn(), success: vi.fn() },
}));

vi.mock("../../../api/modules/connectors", () => ({ connectorsApi: api }));
vi.mock("../../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "UTC",
}));
vi.mock("../../../utils/antdMessage", () => ({
  message: messages,
}));
vi.mock("./openAuthWindow", () => ({
  isAuthPopupBlocked: (popup: Window | null) => popup === null,
  navigateAuthWindow,
  navigateCurrentTab,
  openExternalBrowserUrl: vi.fn(),
  tryOpenAuthPopup,
}));
vi.mock("./oauthCallback", () => ({ oauthCallbackSupported: () => true }));
vi.mock("./customMcpUtils", () => ({ notifyConnectorsChanged: vi.fn() }));
vi.mock("antd", async (importOriginal) => {
  const actual = await importOriginal<typeof import("antd")>();
  return { ...actual, Modal: { ...actual.Modal, confirm } };
});

const connection = (overrides: Record<string, unknown> = {}) => ({
  instance_id: "composio-1",
  kind: "composio-connect",
  display_name: "My Composio",
  account_email: null,
  source: "composio",
  mcp_server_name: "composio-connect-composio-1",
  status: "active",
  auth_status: "authenticated",
  health: "reachable",
  usage: "enabled",
  credential_gen: 1,
  last_checked_at: null,
  capabilities: { search_apps: true },
  default_open: false,
  shared: false,
  owner_user_id: 7,
  created_at: 1,
  updated_at: 1,
  ...overrides,
});

const app = {
  toolkit: "gmail",
  name: "Gmail",
  description: "Send and read email",
  source: "composio",
};

function popupWindow() {
  return { closed: false, close: vi.fn(), focus: vi.fn() } as unknown as Window;
}

async function renderConnected(overrides: Record<string, unknown> = {}) {
  api.composioGet.mockResolvedValue({
    connected: true,
    connection: connection(overrides),
  });
  return render(<ComposioTab />);
}

describe("ComposioTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.composioGet.mockResolvedValue({ connected: false, connection: null });
    api.oauthStart.mockResolvedValue({
      authorize_url: "https://composio.dev/oauth",
      state_id: "state-1",
    });
    api.oauthPending.mockResolvedValue({
      kind: "composio-connect",
      applied: true,
      connection: connection(),
    });
    api.oauthCancel.mockResolvedValue({ cancelled: true });
    api.composioCheck.mockResolvedValue({ connection: connection() });
    api.composioDisable.mockResolvedValue({
      connection: connection({ usage: "disabled" }),
    });
    api.composioEnable.mockResolvedValue({ connection: connection() });
    api.composioDisconnect.mockResolvedValue({ ok: true });
    api.composioSearchApps.mockResolvedValue({ apps: [app] });
    api.composioAuthorizeApp.mockResolvedValue({
      result: { status: "unknown" },
    });
    tryOpenAuthPopup.mockReturnValue(popupWindow());
    navigateCurrentTab.mockReset();
  });

  it("renders disconnected state without implying a healthy connection", async () => {
    render(<ComposioTab />);
    expect(
      await screen.findByText("connectors.composio.disconnectedTitle"),
    ).toBeVisible();
    expect(screen.queryByText(/MCP health/)).not.toBeInTheDocument();
  });

  it("opens the OAuth popup before awaiting the server and accepts only its exact state and source", async () => {
    const user = userEvent.setup();
    const popup = popupWindow();
    tryOpenAuthPopup.mockReturnValue(popup);
    let resolveStart!: (value: {
      authorize_url: string;
      state_id: string;
    }) => void;
    api.oauthStart.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveStart = resolve;
        }),
    );
    render(<ComposioTab />);
    await user.click(
      await screen.findByRole("button", {
        name: "connectors.composio.connectCta",
      }),
    );
    expect(tryOpenAuthPopup).toHaveBeenCalledTimes(1);
    expect(api.oauthStart).toHaveBeenCalledTimes(1);
    resolveStart({
      authorize_url: "https://composio.dev/oauth",
      state_id: "state-1",
    });
    await waitFor(() =>
      expect(navigateAuthWindow).toHaveBeenCalledWith(
        popup,
        "https://composio.dev/oauth",
      ),
    );

    const dispatch = (source: MessageEventSource | null, stateId: string) => {
      window.dispatchEvent(
        new MessageEvent("message", {
          origin: window.location.origin,
          source,
          data: { type: "octop:connector-oauth", state_id: stateId },
        }),
      );
    };
    dispatch(popup, "old-state");
    dispatch(window as unknown as MessageEventSource, "state-1");
    expect(api.oauthPending).not.toHaveBeenCalled();
    dispatch(popup, "state-1");
    await waitFor(() =>
      expect(api.oauthPending).toHaveBeenCalledWith("state-1"),
    );
  });

  it("falls back to the current tab when the OAuth popup is blocked", async () => {
    const user = userEvent.setup();
    tryOpenAuthPopup.mockReturnValue(null);
    render(<ComposioTab />);
    await user.click(
      await screen.findByRole("button", {
        name: "connectors.composio.connectCta",
      }),
    );
    await waitFor(() =>
      expect(navigateCurrentTab).toHaveBeenCalledWith(
        "https://composio.dev/oauth",
      ),
    );
    expect(api.oauthCancel).not.toHaveBeenCalled();
  });

  it("cancels the server transaction when the popup flow is unmounted", async () => {
    const user = userEvent.setup();
    const popup = popupWindow();
    tryOpenAuthPopup.mockReturnValue(popup);
    const view = render(<ComposioTab />);
    await user.click(
      await screen.findByRole("button", {
        name: "connectors.composio.connectCta",
      }),
    );
    await waitFor(() => expect(navigateAuthWindow).toHaveBeenCalled());
    view.unmount();
    await waitFor(() =>
      expect(api.oauthCancel).toHaveBeenCalledWith("state-1"),
    );
    expect(popup.close).toHaveBeenCalled();
  });

  it("closes and cancels OAuth if unmounted before the start request resolves", async () => {
    const user = userEvent.setup();
    const popup = popupWindow();
    tryOpenAuthPopup.mockReturnValue(popup);
    let resolveStart!: (value: {
      authorize_url: string;
      state_id: string;
    }) => void;
    api.oauthStart.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveStart = resolve;
        }),
    );
    const view = render(<ComposioTab />);
    await user.click(
      await screen.findByRole("button", {
        name: "connectors.composio.connectCta",
      }),
    );
    await waitFor(() => expect(api.oauthStart).toHaveBeenCalledTimes(1));

    const addEventListener = vi.spyOn(window, "addEventListener");
    const setTimeout = vi.spyOn(window, "setTimeout");
    const setInterval = vi.spyOn(window, "setInterval");
    view.unmount();
    await act(async () => {
      resolveStart({
        authorize_url: "https://composio.dev/oauth",
        state_id: "state-late",
      });
      await Promise.resolve();
    });
    expect(api.oauthCancel).toHaveBeenCalledWith("state-late");
    expect(popup.close).toHaveBeenCalled();
    expect(navigateAuthWindow).not.toHaveBeenCalled();
    expect(
      addEventListener.mock.calls.filter(([type]) => type === "message"),
    ).toHaveLength(0);
    expect(
      setTimeout.mock.calls.filter(([, delay]) => delay === 120_000),
    ).toHaveLength(0);
    expect(
      setInterval.mock.calls.filter(([, delay]) => delay === 500),
    ).toHaveLength(0);
    addEventListener.mockRestore();
    setTimeout.mockRestore();
    setInterval.mockRestore();
  });

  it("cancels a server transaction after the user closes the popup", async () => {
    const user = userEvent.setup();
    const popup = popupWindow();
    tryOpenAuthPopup.mockReturnValue(popup);
    api.oauthPending.mockRejectedValue(new Error("not ready"));
    render(<ComposioTab />);
    await user.click(
      await screen.findByRole("button", {
        name: "connectors.composio.connectCta",
      }),
    );
    await waitFor(() => expect(navigateAuthWindow).toHaveBeenCalled());
    Object.defineProperty(popup, "closed", { value: true });
    await waitFor(
      () => expect(api.oauthCancel).toHaveBeenCalledWith("state-1"),
      { timeout: 5_000 },
    );
  });

  it("does not expose app discovery when the capability is unknown", async () => {
    await renderConnected({ capabilities: {} });
    expect(
      await screen.findByText("connectors.composio.searchUnsupported"),
    ).toBeVisible();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("does not navigate to an unknown or unsafe app authorization result", async () => {
    const user = userEvent.setup();
    await renderConnected();
    await user.type(await screen.findByRole("textbox"), "mail");
    await user.click(
      screen.getByRole("button", { name: "connectors.composio.searchButton" }),
    );
    await user.click(await screen.findByRole("button", { name: /Gmail/ }));
    await user.click(
      await screen.findByRole("button", {
        name: "connectors.composio.connectApp",
      }),
    );
    await waitFor(() =>
      expect(messages.info).toHaveBeenCalledWith(
        "connectors.composio.appAuthorizationUnknown",
      ),
    );
    expect(navigateAuthWindow).not.toHaveBeenCalled();

    api.composioAuthorizeApp.mockResolvedValueOnce({
      result: { redirect_url: "javascript:alert(1)" },
    });
    await user.click(
      screen.getByRole("button", { name: "connectors.composio.connectApp" }),
    );
    await waitFor(() =>
      expect(api.composioAuthorizeApp).toHaveBeenCalledTimes(2),
    );
    expect(navigateAuthWindow).not.toHaveBeenCalled();
  });

  it("clears discovered apps when credentials are replaced", async () => {
    const user = userEvent.setup();
    const view = await renderConnected();
    await user.type(await screen.findByRole("textbox"), "mail");
    await user.click(
      screen.getByRole("button", { name: "connectors.composio.searchButton" }),
    );
    expect(await screen.findByRole("button", { name: /Gmail/ })).toBeVisible();
    api.composioCheck.mockResolvedValueOnce({
      connection: connection({
        credential_gen: 2,
        account_email: "new@example.com",
      }),
    });
    await user.click(
      screen.getByRole("button", { name: "connectors.composio.check" }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /Gmail/ }),
      ).not.toBeInTheDocument(),
    );
    view.unmount();
  });

  it("describes disable and disconnect scope before executing each action", async () => {
    const user = userEvent.setup();
    await renderConnected();
    await screen.findByRole("button", { name: "connectors.composio.disable" });
    await user.click(
      screen.getByRole("button", { name: "connectors.composio.disable" }),
    );
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "connectors.composio.disableConfirmTitle",
        content: "connectors.composio.disableConfirmBody",
      }),
    );
    await user.click(
      screen.getByRole("button", { name: "connectors.composio.disconnect" }),
    );
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "connectors.composio.disconnectConfirmTitle",
        content: "connectors.composio.disconnectConfirmBody",
      }),
    );
  });
});
