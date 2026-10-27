import { afterEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

async function setupGlobals() {
  const react = await import("react");
  const jsx = await import("react/jsx-runtime");
  // The component resolves these host-owned globals at module evaluation time.
  Object.assign(window, { __OCTOP_REACT__: react, __OCTOP_JSX__: jsx });
}

const context = {
  agentId: "expert-1",
  threadId: null,
  locale: "en",
  theme: "light" as const,
  generation: "g1",
  timeZone: "Asia/Shanghai",
  readiness: {
    status: "ready" as const,
    requiredPlugins: {},
    unavailablePlugins: [],
  },
};

function connection(revision = 4) {
  return {
    schema_id: "octop.enterprise.knowledge.v1",
    version: 1,
    connection_id: "conn-1",
    revision,
    name: "Primary",
    base_url: "https://knowledge.example",
    tenant_id: "tenant-a",
    allowed_kb_ids: ["kb-a"],
    enabled: true,
    credential_configured: true,
    probe_status: "CONFIGURED",
    probe_components: {},
  };
}

const grants = {
  schema_id: "octop.enterprise.knowledge.v1",
  version: 1,
  capabilities: {
    can_manage_connections: true,
    can_manage_grants: true,
    can_manage_bindings: true,
  },
  connections: [connection()],
  grants: [],
};
const binding = {
  schema_id: "octop.enterprise.knowledge.v1",
  version: 1,
  binding: {
    schema_id: "octop.enterprise.knowledge.v1",
    version: 1,
    agent_id: "expert-1",
    revision: 2,
    connection_ref: "conn-1",
    expert_allowed_kb_ids: [],
    default_kb_ids: [],
    web_allowed: false,
    enabled: false,
  },
};

describe("Enterprise Knowledge settings surface", () => {
  afterEach(() => cleanup());

  it("shows an explicit nearby empty catalog result for the selected connection", async () => {
    await setupGlobals();
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        if (path.endsWith("/settings.get"))
          return { api_version: 1, result: grants };
        if (path.endsWith("/connection.catalog"))
          return {
            api_version: 1,
            result: {
              connection_id: "conn-1",
              observed_revision: 4,
              knowledge_bases: [],
            },
          };
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Load authorized knowledge bases" }),
    );

    expect(
      await screen.findByText(
        "The current connection credentials have no visible knowledge bases. Check the source space, tenant, and access permissions.",
      ),
    ).toBeTruthy();
  });

  it("shows a local loading label, prevents duplicate catalog requests, then reports the loaded count", async () => {
    await setupGlobals();
    let resolveCatalog!: (value: unknown) => void;
    const catalogResult = new Promise((resolve) => {
      resolveCatalog = resolve;
    });
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        if (path.endsWith("/settings.get"))
          return { api_version: 1, result: grants };
        if (path.endsWith("/connection.catalog")) return catalogResult;
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );

    const button = await screen.findByRole("button", {
      name: "Load authorized knowledge bases",
    });
    fireEvent.click(button);
    expect(
      screen.getByRole("button", { name: "Loading knowledge bases…" }),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Loading knowledge bases…" }));
    expect(
      host.request.mock.calls.filter(([path]) => path.endsWith("/connection.catalog")),
    ).toHaveLength(1);

    resolveCatalog({
      api_version: 1,
      result: {
        connection_id: "conn-1",
        observed_revision: 4,
        knowledge_bases: [
          { kb_id: "kb-a", name: "Finance" },
          { kb_id: "kb-b", name: "Operations" },
        ],
      },
    });
    expect(await screen.findByText("Knowledge bases loaded: 2.")).toBeTruthy();
  });

  it("shows a nearby sanitized catalog failure and allows retry", async () => {
    await setupGlobals();
    let attempts = 0;
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        if (path.endsWith("/settings.get"))
          return { api_version: 1, result: grants };
        if (path.endsWith("/connection.catalog")) {
          attempts += 1;
          if (attempts === 1) throw new Error("sensitive upstream details");
          return {
            api_version: 1,
            result: {
              connection_id: "conn-1",
              observed_revision: 4,
              knowledge_bases: [{ kb_id: "kb-a", name: "Finance" }],
            },
          };
        }
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Load authorized knowledge bases" }),
    );
    await waitFor(() =>
      expect(
        screen
          .getAllByRole("status")
          .some((item) =>
            item.textContent?.includes("Knowledge bases could not be loaded. Retry."),
          ),
      ).toBe(true),
    );
    expect(screen.queryByText("sensitive upstream details")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Load authorized knowledge bases" }));
    expect(await screen.findByText("Knowledge bases loaded: 1.")).toBeTruthy();
    expect(attempts).toBe(2);
  });

  it("keeps the new connection loading feedback when an old catalog request finishes", async () => {
    await setupGlobals();
    let resolveFirst!: (value: unknown) => void;
    let resolveSecond!: (value: unknown) => void;
    const first = new Promise((resolve) => {
      resolveFirst = resolve;
    });
    const second = new Promise((resolve) => {
      resolveSecond = resolve;
    });
    const secondConnection = {
      ...connection(2),
      connection_id: "conn-2",
      name: "Secondary",
    };
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        if (path.endsWith("/settings.get"))
          return {
            api_version: 1,
            result: { ...grants, connections: [connection(), secondConnection] },
          };
        if (path.endsWith("/connection.catalog")) {
          const payload = JSON.parse(String(init?.body)).payload;
          return payload.connection_id === "conn-1" ? first : second;
        }
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Load authorized knowledge bases" }),
    );
    fireEvent.change(screen.getByLabelText("Connection"), {
      target: { value: "conn-2" },
    });
    expect(await screen.findByText("Knowledge catalog has not been loaded yet.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Load authorized knowledge bases" }));
    await act(async () => {
      resolveFirst({
        api_version: 1,
        result: {
          connection_id: "conn-1",
          observed_revision: 4,
          knowledge_bases: [{ kb_id: "old", name: "Old" }],
        },
      });
    });
    expect(
      await screen.findByRole("button", { name: "Loading knowledge bases…" }),
    ).toBeTruthy();
    expect(screen.queryByText("Knowledge bases loaded: 1.")).toBeNull();
    await act(async () => {
      resolveSecond({
        api_version: 1,
        result: {
          connection_id: "conn-2",
          observed_revision: 2,
          knowledge_bases: [{ kb_id: "new", name: "New" }],
        },
      });
    });
    expect(await screen.findByText("Knowledge bases loaded: 1.")).toBeTruthy();
  });

  it("keeps the new expert binding loading feedback when an old binding catalog request finishes", async () => {
    await setupGlobals();
    let resolveFirst!: (value: unknown) => void;
    let resolveSecond!: (value: unknown) => void;
    const first = new Promise((resolve) => {
      resolveFirst = resolve;
    });
    const second = new Promise((resolve) => {
      resolveSecond = resolve;
    });
    const secondConnection = {
      ...connection(2),
      connection_id: "conn-2",
      name: "Secondary",
    };
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        if (path.endsWith("/settings.get"))
          return {
            api_version: 1,
            result: { ...grants, connections: [connection(), secondConnection] },
          };
        if (path.endsWith("/binding.get"))
          return {
            api_version: 1,
            result: {
              ...binding,
              binding: { ...binding.binding, connection_ref: "conn-1" },
            },
          };
        if (path.endsWith("/connection.catalog")) {
          const payload = JSON.parse(String(init?.body)).payload;
          return payload.connection_id === "conn-1" ? first : second;
        }
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="expert"
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Load authorized knowledge bases" }),
    );
    fireEvent.change(screen.getByLabelText("Knowledge connection"), {
      target: { value: "conn-2" },
    });
    expect(await screen.findByText("Knowledge catalog has not been loaded yet.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Load authorized knowledge bases" }));
    await act(async () => {
      resolveFirst({
        api_version: 1,
        result: {
          connection_id: "conn-1",
          observed_revision: 4,
          knowledge_bases: [{ kb_id: "old", name: "Old" }],
        },
      });
    });
    expect(
      await screen.findByRole("button", { name: "Loading knowledge bases…" }),
    ).toBeTruthy();
    expect(screen.queryByText("Knowledge bases loaded: 1.")).toBeNull();
    await act(async () => {
      resolveSecond({
        api_version: 1,
        result: {
          connection_id: "conn-2",
          observed_revision: 2,
          knowledge_bases: [{ kb_id: "new", name: "New" }],
        },
      });
    });
    expect(await screen.findByText("Knowledge bases loaded: 1.")).toBeTruthy();
  });

  it("invalidates connection catalog feedback when saving creates a new revision", async () => {
    await setupGlobals();
    let resolveCatalog!: (value: unknown) => void;
    const catalogResult = new Promise((resolve) => {
      resolveCatalog = resolve;
    });
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        if (path.endsWith("/settings.get"))
          return { api_version: 1, result: grants };
        if (path.endsWith("/connection.catalog")) return catalogResult;
        if (path.endsWith("/connection.save"))
          return { api_version: 1, result: { connection: connection(5) } };
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Load authorized knowledge bases" }),
    );
    fireEvent.change(screen.getByLabelText("Connection name"), {
      target: { value: "Updated" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));
    await screen.findByText("Saved.");
    expect(await screen.findByText("Knowledge catalog has not been loaded yet.")).toBeTruthy();
    await act(async () => {
      resolveCatalog({
        api_version: 1,
        result: {
          connection_id: "conn-1",
          observed_revision: 4,
          knowledge_bases: [{ kb_id: "stale", name: "Stale" }],
        },
      });
    });
    expect(screen.queryByText("Knowledge bases loaded: 1.")).toBeNull();
    expect(screen.getByText("Knowledge catalog has not been loaded yet.")).toBeTruthy();
  });

  it("does zero management bootstrap calls for an ordinary employee", async () => {
    await setupGlobals();
    const host = {
      getContext: () => context,
      request: vi.fn().mockResolvedValue({ role: "user", permissions: [] }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );

    await screen.findByText(
      "Management settings are unavailable for this account.",
    );
    expect(host.request).toHaveBeenCalledTimes(1);
    expect(host.request).toHaveBeenCalledWith("/auth/me");
  });

  it("saves a connection, clears the write-only key, and never persists it in browser storage", async () => {
    await setupGlobals();
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        const body = JSON.parse(String(init?.body));
        if (path.endsWith("/settings.get"))
          return { api_version: 1, result: grants };
        if (path.endsWith("/binding.get"))
          return { api_version: 1, result: binding };
        if (path.endsWith("/connection.catalog"))
          return {
            api_version: 1,
            result: {
              connection_id: "conn-1",
              observed_revision: 4,
              knowledge_bases: [{ kb_id: "kb-a", name: "Finance" }],
            },
          };
        if (path.endsWith("/connection.save")) {
          expect(body.payload.api_key).toBe("one-time-secret");
          return { api_version: 1, result: { connection: connection(5) } };
        }
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );

    const key = await screen.findByLabelText("API key (write only)");
    fireEvent.change(screen.getByLabelText("Connection name"), {
      target: { value: "Updated" },
    });
    fireEvent.change(screen.getByLabelText("API key (write only)"), {
      target: { value: "one-time-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));

    await waitFor(() => expect((key as HTMLInputElement).value).toBe(""));
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("preserves safe edits on a 409 and disables overwrite until explicit reload", async () => {
    await setupGlobals();
    let saves = 0;
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        if (path.endsWith("/settings.get"))
          return { api_version: 1, result: grants };
        if (path.endsWith("/binding.get"))
          return { api_version: 1, result: binding };
        if (path.endsWith("/connection.catalog"))
          return {
            api_version: 1,
            result: {
              connection_id: "conn-1",
              observed_revision: 4,
              knowledge_bases: [{ kb_id: "kb-a", name: "Finance" }],
            },
          };
        if (path.endsWith("/connection.save")) {
          saves += 1;
          throw new Error(
            'Request failed: 409 Conflict - {"error":{"code":"CONFIG_CONFLICT","message":"stale"}}',
          );
        }
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );
    const name = await screen.findByLabelText("Connection name");
    fireEvent.change(name, { target: { value: "My safe edit" } });
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));

    expect(
      await screen.findByText(
        "This setting changed elsewhere. Reload before saving again.",
      ),
    ).toBeTruthy();
    expect((name as HTMLInputElement).value).toBe("My safe edit");
    expect(
      (
        screen.getByRole("button", {
          name: "Save connection",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(saves).toBe(1);
    fireEvent.click(
      screen.getByRole("button", { name: "Reload / discard edits" }),
    );
    await waitFor(() =>
      expect(
        (screen.getByLabelText("Connection name") as HTMLInputElement).value,
      ).toBe("Primary"),
    );
    expect(
      (screen.getByLabelText("API key (write only)") as HTMLInputElement).value,
    ).toBe("");
    expect(
      (
        screen.getByRole("button", {
          name: "Save connection",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
  });

  it("keeps binding defaults empty and only enables defaults within explicit allowed IDs", async () => {
    await setupGlobals();
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path === "/auth/me")
          return { role: "user", permissions: ["agent_plugins"] };
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        if (path.endsWith("/settings.get"))
          return {
            api_version: 1,
            result: {
              ...grants,
              capabilities: {
                can_manage_bindings: true,
                can_manage_connections: false,
                can_manage_grants: false,
              },
              grants: [],
            },
          };
        if (path.endsWith("/binding.get"))
          return { api_version: 1, result: binding };
        if (path.endsWith("/binding.save"))
          return {
            api_version: 1,
            result: {
              binding: { ...binding.binding, ...body.payload, revision: 3 },
            },
          };
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="expert"
      />,
    );

    expect(
      (await screen.findAllByText("Choose knowledge bases explicitly.")).length,
    ).toBeGreaterThan(0);
    const enabled = screen.getByLabelText(
      "Binding enabled",
    ) as HTMLInputElement;
    expect(enabled.checked).toBe(false);
    expect(enabled.disabled).toBe(true);
    expect(host.request).not.toHaveBeenCalledWith(
      expect.stringContaining("/grants"),
    );
    await waitFor(() => expect(screen.getAllByRole("checkbox").length).toBe(3));
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    await waitFor(() => expect(screen.getAllByRole("checkbox").length).toBe(4));
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    fireEvent.click(
      screen.getByRole("button", { name: "Save expert binding" }),
    );
    await waitFor(() =>
      expect(host.request).toHaveBeenCalledWith(
        expect.stringContaining("/binding.save"),
        expect.anything(),
      ),
    );
    const saveRequest = host.request.mock.calls.find(([path]) =>
      String(path).endsWith("/binding.save"),
    );
    const savedPayload = JSON.parse(String(saveRequest?.[1]?.body)).payload;
    expect(savedPayload.expert_allowed_kb_ids).toEqual([]);
    expect(savedPayload.default_kb_ids).toEqual([]);
  });

  it("does not apply a probe result started against an older saved revision", async () => {
    await setupGlobals();
    let finishProbe: ((value: unknown) => void) | undefined;
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        if (path.endsWith("/settings.get"))
          return { api_version: 1, result: grants };
        if (path.endsWith("/connection.probe"))
          return await new Promise((resolve) => {
            finishProbe = resolve;
          });
        if (path.endsWith("/connection.save"))
          return { api_version: 1, result: { connection: connection(5) } };
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );
    await screen.findByLabelText("Connection name");
    fireEvent.click(screen.getByRole("button", { name: "LIST" }));
    await waitFor(() => expect(finishProbe).toBeDefined());
    fireEvent.change(screen.getByLabelText("Connection name"), {
      target: { value: "Updated" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));
    await screen.findByText("Saved.");
    finishProbe?.({
      api_version: 1,
      result: {
        connection_id: "conn-1",
        observed_revision: 4,
        component: "LIST",
        status: "SUCCESS",
      },
    });

    await waitFor(() =>
      expect(screen.getByText(/Saved; validation pending/)).toBeTruthy(),
    );
    expect(screen.queryByText(/SUCCESS @4/)).toBeNull();
  });

  it("shows a successful component probe without promoting the saved connection to READY", async () => {
    await setupGlobals();
    let settingsReads = 0;
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        if (path.endsWith("/settings.get")) {
          settingsReads += 1;
          const saved = connection();
          if (settingsReads > 1)
            saved.probe_components = {
              LIST: {
                status: "SUCCESS",
                observed_revision: 4,
                checked_at: "2026-01-02T00:00:00+00:00",
              },
            };
          return {
            api_version: 1,
            result: { ...grants, connections: [saved] },
          };
        }
        if (path.endsWith("/connection.probe"))
          return {
            api_version: 1,
            result: {
              connection_id: "conn-1",
              observed_revision: 4,
              component: "LIST",
              status: "SUCCESS",
            },
          };
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );
    await screen.findByLabelText("Connection name");
    fireEvent.click(screen.getByRole("button", { name: "LIST" }));

    expect(
      await screen.findByRole("button", { name: /LIST · SUCCESS @4/ }),
    ).toBeTruthy();
    expect(screen.getByText(/Saved; validation pending/)).toBeTruthy();
    expect(screen.queryByText("READY (server reported)")).toBeNull();
    const checkedAt = screen.getByText(
      /Jan 2, 2026, 8:00 AM/,
    ) as HTMLTimeElement;
    expect(checkedAt.dateTime).toBe("2026-01-02T00:00:00+00:00");
    expect(settingsReads).toBe(2);
  });

  it("discards a late probe after the settings context generation changes", async () => {
    await setupGlobals();
    let finishProbe: ((value: unknown) => void) | undefined;
    let currentContext = context;
    const host = {
      getContext: () => currentContext,
      request: vi.fn(async (path: string) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        if (path.endsWith("/settings.get"))
          return { api_version: 1, result: grants };
        if (path.endsWith("/connection.probe"))
          return await new Promise((resolve) => {
            finishProbe = resolve;
          });
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    const view = render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );
    await screen.findByLabelText("Connection name");
    fireEvent.click(screen.getByRole("button", { name: "LIST" }));
    await waitFor(() => expect(finishProbe).toBeDefined());
    currentContext = { ...context, generation: "g2" };
    view.rerender(
      <Settings
        host={host as never}
        context={currentContext as never}
        location="connectors"
      />,
    );
    await screen.findByLabelText("Connection name");
    finishProbe?.({
      api_version: 1,
      result: {
        connection_id: "conn-1",
        observed_revision: 4,
        component: "LIST",
        status: "SUCCESS",
      },
    });

    await waitFor(() =>
      expect(screen.getByText(/Saved; validation pending/)).toBeTruthy(),
    );
    expect(screen.queryByText(/SUCCESS @4/)).toBeNull();
  });

  it.each([4, 7])(
    "keeps employee grant identity and revision matched when changing from A to B (B r%d)",
    async (employeeBRevision) => {
      await setupGlobals();
      let savedPayload: Record<string, unknown> | undefined;
      const connectionData = {
        ...connection(),
        allowed_kb_ids: ["kb-a", "kb-b"],
      };
      const employeeGrants = [
        {
          connection_id: "conn-1",
          revision: 4,
          user_id: 11,
          user_allowed_kb_ids: ["kb-a"],
        },
        {
          connection_id: "conn-1",
          revision: employeeBRevision,
          user_id: 22,
          user_allowed_kb_ids: ["kb-b"],
        },
      ];
      const host = {
        getContext: () => context,
        request: vi.fn(async (path: string, init?: RequestInit) => {
          if (path === "/auth/me") return { role: "admin", permissions: [] };
          const body = init?.body ? JSON.parse(String(init.body)) : undefined;
          if (path.endsWith("/settings.get"))
            return {
              api_version: 1,
              result: {
                ...grants,
                connections: [connectionData],
                grants: employeeGrants,
              },
            };
          if (path.endsWith("/connection.catalog"))
            return {
              api_version: 1,
              result: {
                connection_id: "conn-1",
                observed_revision: 4,
                knowledge_bases: [
                  { kb_id: "kb-a", name: "Finance" },
                  { kb_id: "kb-b", name: "People" },
                ],
              },
            };
          if (path.endsWith("/grant.save")) {
            savedPayload = body.payload;
            return {
              api_version: 1,
              result: {
                grant: { ...body.payload, revision: employeeBRevision + 1 },
              },
            };
          }
          throw new Error(`Unexpected request ${path}`);
        }),
      };
      const { Settings } = await import("./Settings");
      render(
        <Settings
          host={host as never}
          context={context as never}
          location="connectors"
        />,
      );
      fireEvent.click(
        await screen.findByRole("button", {
          name: "Load authorized knowledge bases",
        }),
      );
      await within(
        await screen.findByRole("group", { name: "Probe knowledge bases" }),
      ).findByRole("checkbox", { name: /People/ });
      const employeeSelect = await screen.findByRole("combobox", {
        name: "Employee ID",
      });
      fireEvent.change(employeeSelect, { target: { value: "conn-1:11" } });
      fireEvent.change(
        screen.getByRole("spinbutton", { name: "Employee ID" }),
        { target: { value: "22" } },
      );
      fireEvent.click(
        screen.getByRole("button", { name: "Save employee grant" }),
      );

      await waitFor(() => expect(savedPayload).toBeDefined());
      expect(savedPayload).toMatchObject({
        user_id: 22,
        expected_revision: employeeBRevision,
        user_allowed_kb_ids: ["kb-b"],
      });
    },
  );

  it("starts a new employee grant with revision zero and an empty scope after editing another employee", async () => {
    await setupGlobals();
    let savedPayload: Record<string, unknown> | undefined;
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        if (path.endsWith("/settings.get"))
          return {
            api_version: 1,
            result: {
              ...grants,
              grants: [
                {
                  connection_id: "conn-1",
                  revision: 4,
                  user_id: 11,
                  user_allowed_kb_ids: ["kb-a"],
                },
              ],
            },
          };
        if (path.endsWith("/connection.catalog"))
          return {
            api_version: 1,
            result: {
              connection_id: "conn-1",
              observed_revision: 4,
              knowledge_bases: [{ kb_id: "kb-a", name: "Finance" }],
            },
          };
        if (path.endsWith("/grant.save")) {
          savedPayload = body.payload;
          return {
            api_version: 1,
            result: { grant: { ...body.payload, revision: 1 } },
          };
        }
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Load authorized knowledge bases",
      }),
    );
    await within(
      await screen.findByRole("group", {
        name: "Employee allowed knowledge bases",
      }),
    ).findByRole("checkbox", { name: /Finance/ });
    fireEvent.change(
      await screen.findByRole("combobox", { name: "Employee ID" }),
      { target: { value: "conn-1:11" } },
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: "Employee ID" }), {
      target: { value: "33" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Save employee grant" }),
    );

    await waitFor(() => expect(savedPayload).toBeDefined());
    expect(savedPayload).toMatchObject({
      user_id: 33,
      expected_revision: 0,
      user_allowed_kb_ids: [],
    });
  });

  it("preserves unsaved connection secret and catalog/probe selection after saving a grant", async () => {
    await setupGlobals();
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        if (path.endsWith("/settings.get"))
          return { api_version: 1, result: grants };
        if (path.endsWith("/connection.catalog"))
          return {
            api_version: 1,
            result: {
              connection_id: "conn-1",
              observed_revision: 4,
              knowledge_bases: [
                { kb_id: "kb-a", name: "Finance" },
                { kb_id: "kb-b", name: "People" },
              ],
            },
          };
        if (path.endsWith("/grant.save"))
          return {
            api_version: 1,
            result: { grant: { ...body.payload, revision: 1 } },
          };
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );
    await screen.findByLabelText("Connection name");
    fireEvent.change(screen.getByLabelText("Connection name"), {
      target: { value: "Unsaved connection edit" },
    });
    fireEvent.change(screen.getByLabelText("API key (write only)"), {
      target: { value: "unsaved-key" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Load authorized knowledge bases" }),
    );
    const financeProbe = await within(
      await screen.findByRole("group", { name: "Probe knowledge bases" }),
    ).findByRole("checkbox", { name: /Finance/ });
    fireEvent.click(financeProbe);
    fireEvent.change(screen.getByLabelText("Search probe query"), {
      target: { value: "quarterly" },
    });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Employee ID" }), {
      target: { value: "13" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Save employee grant" }),
    );

    await screen.findByText("Saved.");
    expect(
      (screen.getByLabelText("Connection name") as HTMLInputElement).value,
    ).toBe("Unsaved connection edit");
    expect(
      (screen.getByLabelText("API key (write only)") as HTMLInputElement).value,
    ).toBe("unsaved-key");
    expect(
      (
        within(
          screen.getByRole("group", { name: "Probe knowledge bases" }),
        ).getByRole("checkbox", { name: /Finance/ }) as HTMLInputElement
      ).checked,
    ).toBe(true);
    expect(
      (screen.getByLabelText("Search probe query") as HTMLInputElement).value,
    ).toBe("quarterly");
  });

  it("preserves connection edits, selected probe IDs, and catalog after a same-revision probe refresh", async () => {
    await setupGlobals();
    let settingsReads = 0;
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        if (path.endsWith("/settings.get")) {
          settingsReads += 1;
          const saved = connection();
          if (settingsReads > 1)
            saved.probe_components = {
              SEARCH: {
                status: "SUCCESS",
                observed_revision: 4,
                checked_at: "2026-01-02T00:00:00+00:00",
              },
            };
          return {
            api_version: 1,
            result: { ...grants, connections: [saved] },
          };
        }
        if (path.endsWith("/connection.catalog"))
          return {
            api_version: 1,
            result: {
              connection_id: "conn-1",
              observed_revision: 4,
              knowledge_bases: [{ kb_id: "kb-a", name: "Finance" }],
            },
          };
        if (path.endsWith("/connection.probe"))
          return {
            api_version: 1,
            result: {
              connection_id: "conn-1",
              observed_revision: 4,
              component: "SEARCH",
              status: "SUCCESS",
            },
          };
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );
    await screen.findByLabelText("Connection name");
    fireEvent.click(
      screen.getByRole("button", { name: "Load authorized knowledge bases" }),
    );
    fireEvent.click(
      await within(
        await screen.findByRole("group", { name: "Probe knowledge bases" }),
      ).findByRole("checkbox", { name: /Finance/ }),
    );
    fireEvent.change(screen.getByLabelText("Connection name"), {
      target: { value: "Keep this draft" },
    });
    fireEvent.change(screen.getByLabelText("API key (write only)"), {
      target: { value: "secret-draft" },
    });
    fireEvent.change(screen.getByLabelText("Search probe query"), {
      target: { value: "target query" },
    });
    fireEvent.click(screen.getByRole("button", { name: "SEARCH" }));

    await screen.findByText(/SEARCH: SUCCESS/);
    expect(
      (screen.getByLabelText("Connection name") as HTMLInputElement).value,
    ).toBe("Keep this draft");
    expect(
      (screen.getByLabelText("API key (write only)") as HTMLInputElement).value,
    ).toBe("secret-draft");
    expect(
      (
        within(
          screen.getByRole("group", { name: "Probe knowledge bases" }),
        ).getByRole("checkbox", { name: /Finance/ }) as HTMLInputElement
      ).checked,
    ).toBe(true);
    expect(
      (screen.getByLabelText("Search probe query") as HTMLInputElement).value,
    ).toBe("target query");
    expect(
      screen.getByRole("button", { name: "Load authorized knowledge bases" }),
    ).toBeTruthy();
    expect(settingsReads).toBe(2);
  });

  it.each(["allowed ID", "connection"])(
    "lets keyboard users turn off an enabled binding after clearing its final %s and save it",
    async (clearedScope) => {
      await setupGlobals();
      const enabledBinding = {
        ...binding.binding,
        connection_ref: "conn-1",
        expert_allowed_kb_ids: ["kb-a"],
        enabled: true,
      };
      let savedPayload: Record<string, unknown> | undefined;
      const host = {
        getContext: () => context,
        request: vi.fn(async (path: string, init?: RequestInit) => {
          if (path === "/auth/me")
            return { role: "user", permissions: ["agent_plugins"] };
          const body = init?.body ? JSON.parse(String(init.body)) : undefined;
          if (path.endsWith("/settings.get"))
            return {
              api_version: 1,
              result: {
                ...grants,
                capabilities: {
                  can_manage_bindings: true,
                  can_manage_connections: false,
                  can_manage_grants: false,
                },
                grants: [],
              },
            };
          if (path.endsWith("/binding.get"))
            return { api_version: 1, result: { binding: enabledBinding } };
          if (path.endsWith("/binding.save")) {
            savedPayload = body.payload;
            return {
              api_version: 1,
              result: {
                binding: { ...enabledBinding, ...body.payload, revision: 3 },
              },
            };
          }
          throw new Error(`Unexpected request ${path}`);
        }),
      };
      const { Settings } = await import("./Settings");
      render(
        <Settings
          host={host as never}
          context={context as never}
          location="expert"
        />,
      );
      const allowed = await within(
        await screen.findByRole("group", {
          name: "Expert allowed knowledge bases",
        }),
      ).findByRole("checkbox");
      expect(
        ((await screen.findByLabelText("Binding enabled")) as HTMLInputElement)
          .checked,
      ).toBe(true);
      if (clearedScope === "allowed ID") fireEvent.click(allowed);
      else
        fireEvent.change(screen.getByLabelText("Knowledge connection"), {
          target: { value: "" },
        });
      const enabled = screen.getByLabelText(
        "Binding enabled",
      ) as HTMLInputElement;
      expect(enabled.disabled).toBe(false);
      expect(enabled.tabIndex).toBe(0);
      enabled.focus();
      expect(document.activeElement).toBe(enabled);
      fireEvent.click(enabled);
      expect(enabled.checked).toBe(false);
      fireEvent.click(
        screen.getByRole("button", { name: "Save expert binding" }),
      );

      await waitFor(() => expect(savedPayload).toBeDefined());
      expect(savedPayload).toMatchObject({
        connection_ref: clearedScope === "connection" ? null : "conn-1",
        expert_allowed_kb_ids: [],
        enabled: false,
      });
    },
  );

  it.each([
    ["ordinary expert", { ...context, agentId: "expert-1" }],
    ["null expert target", { ...context, agentId: null }],
  ])("does no management bootstrap for %s", async (_label, selectedContext) => {
    await setupGlobals();
    const host = {
      getContext: () => selectedContext,
      request: vi.fn().mockResolvedValue({ role: "user", permissions: [] }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={selectedContext as never}
        location="expert"
      />,
    );
    await waitFor(() => expect(host.request).toHaveBeenCalledTimes(1));
    expect(host.request).toHaveBeenCalledWith("/auth/me");
    expect(screen.queryByText(/Binding/)).toBeNull();
  });

  it("shows a nonowner 403 as denied without requesting a binding", async () => {
    await setupGlobals();
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string) => {
        if (path === "/auth/me")
          return { role: "user", permissions: ["agent_plugins"] };
        if (path.endsWith("/settings.get"))
          throw new Error(
            'Request failed: 403 Forbidden - {"error":{"code":"ACCESS_DENIED","message":"private"}}',
          );
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="expert"
      />,
    );
    expect(
      await screen.findByText(
        "You do not have permission to manage these settings.",
      ),
    ).toBeTruthy();
    expect(
      host.request.mock.calls.some(([path]) =>
        String(path).endsWith("/binding.get"),
      ),
    ).toBe(false);
  });

  it("retains a grant draft on 409 and reloads it only after explicit discard", async () => {
    await setupGlobals();
    let settingsReads = 0;
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        if (path.endsWith("/settings.get")) {
          settingsReads += 1;
          return { api_version: 1, result: grants };
        }
        if (path.endsWith("/connection.catalog"))
          return {
            api_version: 1,
            result: {
              connection_id: "conn-1",
              observed_revision: 4,
              knowledge_bases: [{ kb_id: "kb-a", name: "Finance" }],
            },
          };
        if (path.endsWith("/grant.save"))
          throw new Error(
            'Request failed: 409 Conflict - {"error":{"code":"CONFIG_CONFLICT","message":"stale"}}',
          );
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Load authorized knowledge bases",
      }),
    );
    const grantScope = within(
      await screen.findByRole("group", {
        name: "Employee allowed knowledge bases",
      }),
    );
    await grantScope.findByRole("checkbox", { name: /Finance/ });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Employee ID" }), {
      target: { value: "44" },
    });
    fireEvent.click(grantScope.getByRole("checkbox", { name: /Finance/ }));
    fireEvent.click(
      screen.getByRole("button", { name: "Save employee grant" }),
    );

    expect(
      await screen.findByText(
        "This setting changed elsewhere. Reload before saving again.",
      ),
    ).toBeTruthy();
    expect(
      (
        screen.getByRole("spinbutton", {
          name: "Employee ID",
        }) as HTMLInputElement
      ).value,
    ).toBe("44");
    expect(
      (
        grantScope.getByRole("checkbox", {
          name: /Finance/,
        }) as HTMLInputElement
      ).checked,
    ).toBe(true);
    expect(
      (
        screen.getByRole("button", {
          name: "Save employee grant",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    fireEvent.click(
      screen.getByRole("button", { name: "Reload / discard edits" }),
    );
    await waitFor(() =>
      expect(
        (
          screen.getByRole("spinbutton", {
            name: "Employee ID",
          }) as HTMLInputElement
        ).value,
      ).toBe(""),
    );
    const refreshedGrantScope = within(
      screen.getByRole("group", { name: "Employee allowed knowledge bases" }),
    );
    expect(refreshedGrantScope.queryByRole("checkbox")).toBeNull();
    expect(settingsReads).toBe(2);
  });

  it("retains a binding draft on 409 and discards it only after explicit reload", async () => {
    await setupGlobals();
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path === "/auth/me")
          return { role: "user", permissions: ["agent_plugins"] };
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        if (path.endsWith("/settings.get"))
          return {
            api_version: 1,
            result: {
              ...grants,
              capabilities: {
                can_manage_bindings: true,
                can_manage_connections: false,
                can_manage_grants: false,
              },
              grants: [],
            },
          };
        if (path.endsWith("/binding.get"))
          return { api_version: 1, result: binding };
        if (path.endsWith("/binding.save"))
          throw new Error(
            'Request failed: 409 Conflict - {"error":{"code":"CONFIG_CONFLICT","message":"stale"}}',
          );
        throw new Error(`Unexpected request ${path} ${String(body)}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="expert"
      />,
    );
    const allowedGroup = within(
      await screen.findByRole("group", {
        name: "Expert allowed knowledge bases",
      }),
    );
    const allowed = await allowedGroup.findByRole("checkbox");
    fireEvent.click(allowed);
    fireEvent.click(screen.getByLabelText("Binding enabled"));
    fireEvent.click(
      screen.getByRole("button", { name: "Save expert binding" }),
    );

    expect(
      await screen.findByText(
        "This setting changed elsewhere. Reload before saving again.",
      ),
    ).toBeTruthy();
    expect(
      (allowedGroup.getByRole("checkbox") as HTMLInputElement).checked,
    ).toBe(true);
    expect(
      (screen.getByLabelText("Binding enabled") as HTMLInputElement).checked,
    ).toBe(true);
    expect(
      (
        screen.getByRole("button", {
          name: "Save expert binding",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    fireEvent.click(
      screen.getByRole("button", { name: "Reload / discard edits" }),
    );
    const refreshedAllowedGroup = within(
      await screen.findByRole("group", {
        name: "Expert allowed knowledge bases",
      }),
    );
    await waitFor(() =>
      expect(
        (refreshedAllowedGroup.getByRole("checkbox") as HTMLInputElement)
          .checked,
      ).toBe(false),
    );
    expect(
      (screen.getByLabelText("Binding enabled") as HTMLInputElement).checked,
    ).toBe(false);
  });

  it("focuses the first invalid connection field and does not send a save", async () => {
    await setupGlobals();
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        if (path.endsWith("/settings.get"))
          return {
            api_version: 1,
            result: { ...grants, connections: [], grants: [] },
          };
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );
    const name = await screen.findByLabelText("Connection name");
    fireEvent.submit(name.closest("form") as HTMLFormElement);

    expect(
      await screen.findByText("Complete the required fields."),
    ).toBeTruthy();
    expect(document.activeElement).toBe(name);
    expect(
      host.request.mock.calls.some(([path]) =>
        String(path).endsWith("/connection.save"),
      ),
    ).toBe(false);
  });

  it("focuses an invalid employee ID without sending a grant", async () => {
    await setupGlobals();
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string) => {
        if (path === "/auth/me") return { role: "admin", permissions: [] };
        if (path.endsWith("/settings.get"))
          return { api_version: 1, result: grants };
        if (path.endsWith("/connection.catalog"))
          return {
            api_version: 1,
            result: {
              connection_id: "conn-1",
              observed_revision: 4,
              knowledge_bases: [{ kb_id: "kb-a", name: "Finance" }],
            },
          };
        throw new Error(`Unexpected request ${path}`);
      }),
    };
    const { Settings } = await import("./Settings");
    render(
      <Settings
        host={host as never}
        context={context as never}
        location="connectors"
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Load authorized knowledge bases",
      }),
    );
    const employeeId = await screen.findByRole("spinbutton", {
      name: "Employee ID",
    });
    fireEvent.change(employeeId, { target: { value: "0" } });
    fireEvent.submit(employeeId.closest("form") as HTMLFormElement);

    expect(
      await screen.findByText("Enter a positive employee ID."),
    ).toBeTruthy();
    expect(document.activeElement).toBe(employeeId);
    expect(
      host.request.mock.calls.some(([path]) =>
        String(path).endsWith("/grant.save"),
      ),
    ).toBe(false);
  });
});
