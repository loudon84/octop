import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { setup } from "./index";
import type {
  PluginHost,
  PluginSetupHost,
  PluginSurfaceContext,
  PluginToolProps,
} from "./host";

async function setupGlobals() {
  const react = await import("react");
  const jsx = await import("react/jsx-runtime");
  Object.assign(window, { __OCTOP_REACT__: react, __OCTOP_JSX__: jsx });
}

const context: PluginSurfaceContext = {
  agentId: "expert-real",
  threadId: "thread-real",
  locale: "en",
  theme: "light",
  generation: "mounted-generation",
  timeZone: "UTC",
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function registerSettingsSurface() {
  let surface:
    | ((props: {
        host: PluginHost;
        context: PluginSurfaceContext;
        location: "expert";
      }) => unknown)
    | undefined;
  let composer:
    | ((props: { host: PluginHost; context: PluginSurfaceContext }) => unknown)
    | undefined;
  let renderer: ((props: PluginToolProps) => unknown) | undefined;
  const registrationRequest = vi.fn(async () => {
    throw new Error("registration host request must not be used");
  });
  const registrationHost: PluginSetupHost = {
    getContext: () => null,
    request: registrationRequest,
    registerSurface: (name, component) => {
      if (name === "settings") surface = component;
      if (name === "composer") composer = component;
      return () => undefined;
    },
    registerRenderer: (registration) => {
      renderer = registration.component;
    },
  };
  setup(registrationHost);
  expect(surface).toBeDefined();
  expect(composer).toBeDefined();
  expect(renderer).toBeDefined();
  return { surface: surface!, renderer: renderer!, registrationRequest };
}

function mountedHost(
  active: () => boolean,
  agentContext: PluginSurfaceContext,
  bindingGet: () => Promise<unknown>,
  requestReturned?: () => void,
) {
  const requests: Array<{ path: string; init?: RequestInit }> = [];
  const host: PluginHost = {
    getContext: () => (active() ? agentContext : null),
    request: async <T = unknown,>(
      path: string,
      init?: RequestInit,
    ): Promise<T> => {
      requests.push({ path, init });
      if (!active()) throw new Error("mounted host inactive");
      try {
        let response: unknown;
        if (path === "/auth/me") {
          response = { role: "user", permissions: ["agent_plugins"] };
        } else if (path.endsWith("/settings.get")) {
          response = {
            api_version: 1,
            result: {
              capabilities: { can_manage_bindings: true },
              connections: [],
              grants: [],
            },
          };
        } else if (path.endsWith("/binding.get")) {
          response = await bindingGet();
        } else {
          throw new Error(`Unexpected mounted request ${path}`);
        }
        if (!active()) throw new Error("mounted host inactive");
        return response as T;
      } finally {
        if (path.endsWith("/binding.get")) requestReturned?.();
      }
    },
    registerSurface: () => () => undefined,
  };
  return { host, requests };
}

function binding(revision: number) {
  return {
    api_version: 1,
    result: {
      binding: {
        agent_id: "expert-real",
        revision,
        connection_ref: null,
        expert_allowed_kb_ids: [],
        default_kb_ids: [],
        web_allowed: false,
        enabled: false,
      },
    },
  };
}

afterEach(() => cleanup());

it("uses the mounted Host context through the real setup callback and completes settings bootstrap", async () => {
  await setupGlobals();
  const { surface, registrationRequest } = registerSettingsSurface();
  const live = mountedHost(
    () => true,
    context,
    async () => binding(1),
  );
  render(
    surface({
      host: live.host,
      context,
      location: "expert",
    }) as React.ReactElement,
  );

  await screen.findByRole("button", { name: "Save expert binding" });
  await waitFor(() => {
    const call = live.requests.find(({ path }) =>
      path.endsWith("/binding.get"),
    );
    expect(call).toBeDefined();
    expect(JSON.parse(String(call?.init?.body))).toEqual({
      api_version: 1,
      context: { agent_id: "expert-real", thread_id: "thread-real" },
      payload: {},
    });
  });
  expect(screen.getByText(/revision.*1/i)).toBeTruthy();
  expect(registrationRequest).not.toHaveBeenCalled();
});

it("does not publish an invalidated old Host result over the replacement mounted context", async () => {
  await setupGlobals();
  const { surface, registrationRequest } = registerSettingsSurface();
  const oldResponse = deferred<unknown>();
  const oldRequestReturned = deferred<void>();
  let oldActive = true;
  const oldMount = mountedHost(
    () => oldActive,
    context,
    () => oldResponse.promise,
    () => oldRequestReturned.resolve(),
  );
  const rendered = render(
    surface({
      host: oldMount.host,
      context,
      location: "expert",
    }) as React.ReactElement,
  );
  await waitFor(() =>
    expect(
      oldMount.requests.some(({ path }) => path.endsWith("/binding.get")),
    ).toBe(true),
  );

  const replacementContext = {
    ...context,
    agentId: "expert-replacement",
    threadId: "thread-replacement",
    generation: "replacement-generation",
  };
  const replacement = mountedHost(
    () => true,
    replacementContext,
    async () => binding(2),
  );
  rendered.rerender(
    surface({
      host: replacement.host,
      context: replacementContext,
      location: "expert",
    }) as React.ReactElement,
  );
  await screen.findByRole("button", { name: "Save expert binding" });
  await waitFor(() => expect(screen.getByText(/revision.*2/i)).toBeTruthy());

  oldActive = false;
  oldResponse.resolve(binding(1));
  await oldRequestReturned.promise;
  await waitFor(() => {
    expect(screen.getByText(/revision.*2/i)).toBeTruthy();
    expect(screen.queryByText(/revision.*1/i)).toBeNull();
  });
  const replacementCall = replacement.requests.find(({ path }) =>
    path.endsWith("/binding.get"),
  );
  expect(JSON.parse(String(replacementCall?.init?.body)).context).toEqual({
    agent_id: "expert-replacement",
    thread_id: "thread-replacement",
  });
  expect(registrationRequest).not.toHaveBeenCalled();
});

it("mounts the registered renderer from setup and lists evidence through its mounted Host", async () => {
  await setupGlobals();
  const { renderer } = registerSettingsSurface();
  const requests: Array<{ path: string; init?: RequestInit }> = [];
  const host: PluginToolProps["host"] = {
    getToolContext: () => ({ ...context }),
    request: async <T = unknown,>(
      path: string,
      init?: RequestInit,
    ): Promise<T> => {
      requests.push({ path, init });
      if (path.endsWith("/evidence.list")) {
        return {
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation: {
              schema_id: "octop.enterprise.knowledge.v1",
              version: 1,
              operation_id: "11111111-1111-4111-8111-111111111111",
              revision: 1,
              user_id: 5,
              agent_id: context.agentId,
              thread_id: context.threadId,
              session_revision: 0,
              config_revision: 0,
              binding_revision: 0,
              grant_revision: 0,
              connection_id: null,
              status: "COMPLETED",
            },
            evidence: [
              {
                schema_id: "octop.enterprise.knowledge.v1",
                version: 1,
                evidence_id: "22222222-2222-4222-8222-222222222222",
                operation_id: "11111111-1111-4111-8111-111111111111",
                user_id: 5,
                agent_id: context.agentId,
                thread_id: context.threadId,
                source_kind: "WEB",
                provider: "SEARXNG",
                degraded: false,
                search_revision: 1,
                connection_id: null,
                kb_id: null,
                knowledge_id: null,
                title: "Setup registered summary",
                content: "Summary through setup",
                truncated: false,
                retrieved_at: "2026-01-02T00:00:00Z",
                source_version: null,
                source_page: null,
                locator: "https://result.example/setup",
              },
            ],
          },
        } as T;
      }
      if (path === "/settings/timezone") return { timezone: "UTC" } as T;
      throw new Error(`Unexpected renderer request ${path}`);
    },
    openSidePanel: () => undefined,
  };
  const Renderer = renderer as React.ComponentType<PluginToolProps>;
  render(
    <Renderer
      pluginId="enterprise-knowledge"
      toolName="enterprise_knowledge_search"
      callId="setup-call"
      status="done"
      args={{}}
      data={{
        schema_id: "octop.enterprise.knowledge.v1",
        version: 1,
        operation_id: "11111111-1111-4111-8111-111111111111",
      }}
      host={host}
      presentation={{ location: "inline", active: true }}
      isStreaming={false}
    />,
  );
  expect(await screen.findByText("Summary through setup")).toBeTruthy();
  const call = requests.find(({ path }) => path.endsWith("/evidence.list"));
  expect(call).toBeDefined();
  expect(JSON.parse(String(call?.init?.body))).toEqual({
    api_version: 1,
    context: { agent_id: context.agentId, thread_id: context.threadId },
    payload: {
      schema_id: "octop.enterprise.knowledge.v1",
      version: 1,
      operation_id: "11111111-1111-4111-8111-111111111111",
    },
  });
});
