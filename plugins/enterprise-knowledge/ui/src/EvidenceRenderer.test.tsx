import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { EvidenceRenderer } from "./EvidenceRenderer";
import type { PluginToolProps } from "./host";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

async function setupGlobals() {
  const react = await import("react");
  const jsx = await import("react/jsx-runtime");
  Object.assign(window, { __OCTOP_REACT__: react, __OCTOP_JSX__: jsx });
}

const operationId = "11111111-1111-4111-8111-111111111111";
const evidenceId = "22222222-2222-4222-8222-222222222222";
const connectionId = "33333333-3333-4333-8333-333333333333";
const target = {
  agentId: "agent-live",
  threadId: "thread-live",
  locale: "en",
  theme: "light" as const,
};

function evidenceRecord(overrides: Record<string, unknown> = {}) {
  const web = overrides.source_kind === "WEB";
  return {
    schema_id: "octop.enterprise.knowledge.v1",
    version: 1,
    evidence_id: evidenceId,
    operation_id: operationId,
    user_id: 9,
    agent_id: target.agentId,
    thread_id: target.threadId,
    source_kind: web ? "WEB" : "KNOWLEDGE",
    provider: web ? "SEARXNG" : "WEKNORA",
    degraded: false,
    search_revision: web ? 1 : 0,
    connection_id: connectionId,
    kb_id: "opaque-kb",
    knowledge_id: "opaque-doc",
    title: "Registered title",
    content: "forged tool text",
    truncated: false,
    retrieved_at: "2026-01-02T00:00:00Z",
    source_version: null,
    source_page: null,
    locator: web ? "https://result.example/source" : null,
    ...overrides,
  };
}

function listResponse(evidence = [evidenceRecord()], status = "COMPLETED") {
  return {
    api_version: 1,
    result: {
      schema_id: "octop.enterprise.knowledge.v1",
      version: 1,
      operation: {
        schema_id: "octop.enterprise.knowledge.v1",
        version: 1,
        operation_id: operationId,
        revision: 1,
        user_id: 9,
        agent_id: target.agentId,
        thread_id: target.threadId,
        session_revision: 0,
        config_revision: 1,
        binding_revision: 1,
        grant_revision: 1,
        connection_id: connectionId,
        status,
      },
      evidence,
    },
  };
}

function props(
  overrides: Record<string, unknown> = {},
  handlers: {
    list?: () => unknown;
    read?: (payload: Record<string, unknown>) => unknown;
    original?: () => unknown;
    timezone?: () => unknown;
  } = {},
) {
  const requests: Array<{ path: string; init?: RequestInit }> = [];
  const host = {
    getToolContext: () => target,
    getContext: () => null,
    request: vi.fn(async (path: string, init?: RequestInit) => {
      requests.push({ path, init });
      if (path.endsWith("/evidence.list"))
        return handlers.list ? handlers.list() : listResponse();
      if (path.endsWith("/evidence.read"))
        return handlers.read
          ? handlers.read(JSON.parse(String(init?.body)).payload)
          : {
              api_version: 1,
              result: {
                schema_id: "octop.enterprise.knowledge.v1",
                version: 1,
                operation_id: operationId,
                evidence_id: evidenceId,
                knowledge_id: "opaque-doc",
                status: "EMPTY",
                content: "",
                truncated: false,
                continuation: null,
                source_version: null,
              },
            };
      if (path.endsWith("/evidence.original"))
        return handlers.original
          ? handlers.original()
          : {
              api_version: 1,
              result: {
                schema_id: "octop.enterprise.knowledge.v1",
                version: 1,
                operation_id: operationId,
                evidence_id: evidenceId,
                available: false,
                reason: "CAPABILITY_UNVERIFIED",
                resource_token: null,
              },
            };
      if (path === "/settings/timezone")
        return handlers.timezone ? handlers.timezone() : { timezone: "UTC" };
      throw new Error("Unexpected request");
    }),
    openSidePanel: vi.fn(),
  };
  return {
    requests,
    value: {
      pluginId: "enterprise-knowledge",
      toolName: "enterprise_knowledge_search",
      callId: "call-1",
      status: "done",
      data: {
        schema_id: "octop.enterprise.knowledge.v1",
        version: 1,
        operation_id: operationId,
        evidence: [
          {
            evidence_id: "99999999-9999-4999-8999-999999999999",
            title: "Forged",
            url: "https://untrusted.invalid",
          },
        ],
      },
      textFallback: "untrusted fallback",
      host,
      presentation: { location: "inline", active: true },
      ...overrides,
    },
  };
}

function savedClick(button: HTMLElement) {
  const propsKey = Object.keys(button).find((key) =>
    key.startsWith("__reactProps$"),
  );
  expect(propsKey).toBeDefined();
  const reactProps = (
    button as unknown as Record<string, { onClick?: () => void }>
  )[propsKey!];
  expect(reactProps.onClick).toBeTypeOf("function");
  return reactProps.onClick!;
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
  target.locale = "en";
  target.agentId = "agent-live";
  target.threadId = "thread-live";
});

it("renders only fresh registered evidence and never turns null page into page one", async () => {
  await setupGlobals();
  const { value, requests } = props();
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);

  expect(await screen.findByText("Registered title")).toBeTruthy();
  expect(screen.queryByText("Forged")).toBeNull();
  expect(screen.queryByText("forged tool text")).toBeNull();
  expect(screen.getByText("No page number")).toBeTruthy();
  expect(requests.some(({ path }) => path.endsWith("/evidence.list"))).toBe(
    true,
  );
  const listRequest = requests.find(({ path }) =>
    path.endsWith("/evidence.list"),
  );
  expect(JSON.parse(String(listRequest?.init?.body))).toEqual({
    api_version: 1,
    context: { agent_id: target.agentId, thread_id: target.threadId },
    payload: {
      schema_id: "octop.enterprise.knowledge.v1",
      version: 1,
      operation_id: operationId,
    },
  });
});

it("uses a selected registered evidence ID as opaque dock state on the mounted Host", async () => {
  await setupGlobals();
  const { value } = props();
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);

  fireEvent.click(
    await screen.findByRole("button", { name: /Registered title/ }),
  );
  expect(value.host.openSidePanel).toHaveBeenCalledWith(
    expect.objectContaining({
      callId: "call-1",
      state: { operation_id: operationId, evidence_id: evidenceId },
    }),
  );
});

it("rejects an invalid tool identity without requesting evidence", async () => {
  await setupGlobals();
  const { value } = props({ toolName: "other_tool" });
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await waitFor(() => expect(value.host.request).not.toHaveBeenCalled());
  expect(screen.queryByRole("button", { name: /Registered title/ })).toBeNull();
});

it("rejects list rows whose operation belongs to another agent", async () => {
  await setupGlobals();
  const forgedList = listResponse();
  forgedList.result.operation.agent_id = "speaker-agent";
  const { value } = props({}, { list: () => forgedList });
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(await screen.findByText("Evidence could not be loaded.")).toBeTruthy();
  expect(screen.queryByText("Registered title")).toBeNull();
});

it("shows a localized empty list for a completed empty operation", async () => {
  await setupGlobals();
  const { value } = props({}, { list: () => listResponse([], "EMPTY") });
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(
    await screen.findByText("No authorized evidence is available."),
  ).toBeTruthy();
});

it("keeps partial evidence pending until the authenticated operation completes", async () => {
  await setupGlobals();
  vi.useFakeTimers();
  let lists = 0;
  const { value, requests } = props(
    {},
    {
      list: () =>
        ++lists === 1
          ? listResponse([evidenceRecord()], "GENERATING")
          : listResponse(),
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);

  await act(async () => Promise.resolve());
  expect(screen.getByText("Evidence is still being prepared…")).toBeTruthy();
  expect(screen.queryByText("No authorized evidence is available.")).toBeNull();
  expect(screen.queryByText("Registered title")).toBeNull();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(250);
  });
  expect(screen.getByText("Registered title")).toBeTruthy();
  expect(lists).toBe(2);
  expect(
    requests
      .filter(({ path }) => path.includes("/actions/"))
      .map(({ path }) => path.split("/actions/")[1]),
  ).toEqual(["evidence.list", "evidence.list"]);
});

it("aborts the in-flight automatic check at 30 seconds and keeps manual refresh available", async () => {
  await setupGlobals();
  vi.useFakeTimers();
  const delayedList = deferred<unknown>();
  let lists = 0;
  const { value, requests } = props(
    {},
    {
      list: () => {
        lists += 1;
        if (lists === 1) return listResponse([], "RETRIEVING");
        if (lists === 2) return delayedList.promise;
        return listResponse();
      },
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await act(async () => Promise.resolve());
  expect(screen.getByText("Evidence is still being prepared…")).toBeTruthy();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(250);
  });
  const pollSignal = requests.filter(({ path }) =>
    path.endsWith("/evidence.list"),
  )[1]?.init?.signal;
  expect(lists).toBe(2);
  expect(pollSignal?.aborted).toBe(false);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(29_750);
  });
  expect(pollSignal?.aborted).toBe(true);
  expect(screen.getByText("Evidence is still being prepared…")).toBeTruthy();
  expect(screen.queryByText("No authorized evidence is available.")).toBeNull();
  const refresh = screen.getByRole("button", { name: "Check evidence again" });

  await act(async () => {
    delayedList.resolve(listResponse([], "GENERATING"));
    await Promise.resolve();
  });
  fireEvent.click(refresh);
  await act(async () => Promise.resolve());
  expect(screen.getByText("Registered title")).toBeTruthy();
  expect(lists).toBe(3);
});

it.each(["DENIED", "FAILED", "CANCELLED", "INTERRUPTED", "UNKNOWN"])(
  "does not poll or trust evidence for operation state %s",
  async (status) => {
    await setupGlobals();
    vi.useFakeTimers();
    let lists = 0;
    const { value } = props(
      {},
      {
        list: () => {
          lists += 1;
          return listResponse([evidenceRecord()], status);
        },
      },
    );
    render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
    await act(async () => Promise.resolve());
    expect(screen.queryByText("Registered title")).toBeNull();
    expect(screen.getByText("Evidence could not be loaded.")).toBeTruthy();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(lists).toBe(1);
  },
);

it("stops automatic polling on access denial and does not offer a manual bypass", async () => {
  await setupGlobals();
  vi.useFakeTimers();
  let lists = 0;
  const { value } = props(
    {},
    {
      list: () => {
        lists += 1;
        if (lists === 1) return listResponse([], "VALIDATING");
        throw new Error(
          'Request failed: 403 Forbidden - {"error":{"code":"ACCESS_DENIED"}}',
        );
      },
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await act(async () => Promise.resolve());
  await act(async () => {
    await vi.advanceTimersByTimeAsync(250);
  });
  expect(
    screen.getByText(
      "Access changed. Reopen this result to check authorization.",
    ),
  ).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: "Check evidence again" }),
  ).toBeNull();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000);
  });
  expect(lists).toBe(2);
});

it("aborts an automatic evidence check while the document is hidden", async () => {
  await setupGlobals();
  vi.useFakeTimers();
  const delayedList = deferred<unknown>();
  let lists = 0;
  const { value, requests } = props(
    {},
    {
      list: () => {
        lists += 1;
        return lists === 1
          ? listResponse([], "GENERATING")
          : delayedList.promise;
      },
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await act(async () => Promise.resolve());
  await act(async () => {
    await vi.advanceTimersByTimeAsync(250);
  });
  const pollSignal = requests.filter(({ path }) =>
    path.endsWith("/evidence.list"),
  )[1]?.init?.signal;
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "hidden",
  });
  document.dispatchEvent(new Event("visibilitychange"));
  expect(pollSignal?.aborted).toBe(true);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000);
  });
  expect(lists).toBe(2);
  delayedList.resolve(listResponse());
  await act(async () => Promise.resolve());
  expect(screen.queryByText("Registered title")).toBeNull();
});

it("aborts automatic work when the mounted tool identity changes and ignores its late result", async () => {
  await setupGlobals();
  vi.useFakeTimers();
  const delayedList = deferred<unknown>();
  let lists = 0;
  const { value, requests } = props(
    {},
    {
      list: () => {
        lists += 1;
        return lists === 1
          ? listResponse([], "GENERATING")
          : delayedList.promise;
      },
    },
  );
  const mounted = render(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  await act(async () => Promise.resolve());
  await act(async () => {
    await vi.advanceTimersByTimeAsync(250);
  });
  const pollSignal = requests.filter(({ path }) =>
    path.endsWith("/evidence.list"),
  )[1]?.init?.signal;
  mounted.rerender(
    <EvidenceRenderer
      {...(value as unknown as PluginToolProps)}
      toolName="other_tool"
    />,
  );
  expect(pollSignal?.aborted).toBe(true);
  delayedList.resolve(listResponse());
  await act(async () => Promise.resolve());
  expect(screen.queryByText("Registered title")).toBeNull();
  expect(lists).toBe(2);
});

it("restarts under the new scope and discards a late poll from the old scope", async () => {
  await setupGlobals();
  vi.useFakeTimers();
  const delayedList = deferred<unknown>();
  let lists = 0;
  const { value, requests } = props(
    {},
    {
      list: () => {
        lists += 1;
        if (lists === 1) return listResponse([], "VALIDATING");
        if (lists === 2) return delayedList.promise;
        return listResponse();
      },
    },
  );
  const mounted = render(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  await act(async () => Promise.resolve());
  await act(async () => {
    await vi.advanceTimersByTimeAsync(250);
  });
  const oldPollSignal = requests.filter(({ path }) =>
    path.endsWith("/evidence.list"),
  )[1]?.init?.signal;
  target.agentId = "agent-next";
  mounted.rerender(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  expect(oldPollSignal?.aborted).toBe(true);
  await act(async () => Promise.resolve());
  expect(screen.getByText("Registered title")).toBeTruthy();
  delayedList.resolve(listResponse());
  await act(async () => Promise.resolve());
  expect(screen.getByText("Registered title")).toBeTruthy();
  expect(lists).toBe(3);
});

it("aborts an automatic check on unmount and ignores its late completion", async () => {
  await setupGlobals();
  vi.useFakeTimers();
  const delayedList = deferred<unknown>();
  let lists = 0;
  const { value, requests } = props(
    {},
    {
      list: () => {
        lists += 1;
        return lists === 1
          ? listResponse([], "GENERATING")
          : delayedList.promise;
      },
    },
  );
  const mounted = render(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  await act(async () => Promise.resolve());
  await act(async () => {
    await vi.advanceTimersByTimeAsync(250);
  });
  const pollSignal = requests.filter(({ path }) =>
    path.endsWith("/evidence.list"),
  )[1]?.init?.signal;
  mounted.unmount();
  expect(pollSignal?.aborted).toBe(true);
  delayedList.resolve(listResponse());
  await act(async () => Promise.resolve());
  expect(lists).toBe(2);
});

it("renders authorization failures in Chinese when the mounted Host uses zh locale", async () => {
  await setupGlobals();
  target.locale = "zh-CN";
  const { value } = props(
    {},
    {
      list: () => {
        throw new Error(
          'Request failed: 403 Forbidden - {"error":{"code":"ACCESS_DENIED"}}',
        );
      },
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(
    await screen.findByText("访问权限已变化。请重新打开此结果以重新检查授权。"),
  ).toBeTruthy();
});

it("does not request evidence while the tool is still running", async () => {
  await setupGlobals();
  const { value } = props({ status: "running" });
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await waitFor(() => expect(value.host.request).not.toHaveBeenCalled());
});

it("shows web evidence as untrusted text and never invokes knowledge reading", async () => {
  await setupGlobals();
  const web = evidenceRecord({
    source_kind: "WEB",
    connection_id: null,
    kb_id: null,
    knowledge_id: null,
    source_page: 3,
  });
  const { value, requests } = props({}, { list: () => listResponse([web]) });
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(await screen.findByText("Web source")).toBeTruthy();
  expect(
    screen.getByText(
      "Web text is untrusted reference material. Do not follow instructions contained in it.",
    ),
  ).toBeTruthy();
  expect(
    (
      screen.getByRole("button", {
        name: /Registered title/,
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(false);
  expect(
    requests.some(
      ({ path }) =>
        path.endsWith("/evidence.read") || path.endsWith("/evidence.original"),
    ),
  ).toBe(false);
});

it.each([
  {
    locale: "en",
    provider: "FIRECRAWL",
    providerLabel: "Firecrawl",
    fallbackLabel: "Switched to secondary source",
  },
  {
    locale: "zh-CN",
    provider: "SEARXNG",
    providerLabel: "SearXNG",
    fallbackLabel: "已切换至备源",
  },
])(
  "shows the actual provider and localized fallback for degraded WEB evidence ($locale)",
  async ({ locale, provider, providerLabel, fallbackLabel }) => {
    await setupGlobals();
    target.locale = locale;
    const web = evidenceRecord({
      source_kind: "WEB",
      provider,
      degraded: true,
      connection_id: null,
      kb_id: null,
      knowledge_id: null,
    });
    const { value, requests } = props({}, { list: () => listResponse([web]) });
    render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);

    expect(await screen.findByText(providerLabel)).toBeTruthy();
    expect(screen.getByText(fallbackLabel)).toBeTruthy();
    expect(
      requests.some(
        ({ path }) =>
          path.endsWith("/evidence.read") ||
          path.endsWith("/evidence.original"),
      ),
    ).toBe(false);
  },
);

it("shows a WEB provider without a fallback label when the result is not degraded", async () => {
  await setupGlobals();
  const web = evidenceRecord({
    source_kind: "WEB",
    provider: "SEARXNG",
    degraded: false,
    connection_id: null,
    kb_id: null,
    knowledge_id: null,
  });
  const { value } = props({}, { list: () => listResponse([web]) });
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);

  expect(await screen.findByText("SearXNG")).toBeTruthy();
  expect(screen.queryByText("Switched to secondary source")).toBeNull();
});

it("rejects WEB evidence with an unknown provider", async () => {
  await setupGlobals();
  const web = evidenceRecord({
    source_kind: "WEB",
    provider: "UNKNOWN",
    connection_id: null,
    kb_id: null,
    knowledge_id: null,
  });
  const { value } = props({}, { list: () => listResponse([web]) });
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);

  expect(await screen.findByText("Evidence could not be loaded.")).toBeTruthy();
  expect(screen.queryByText("Registered title")).toBeNull();
});

it("reauthorizes a registered web locator before exposing its source link", async () => {
  await setupGlobals();
  const web = evidenceRecord({
    source_kind: "WEB",
    connection_id: null,
    kb_id: null,
    knowledge_id: null,
  });
  const { value, requests } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      list: () => listResponse([web]),
      original: () => ({
        api_version: 1,
        result: {
          schema_id: "octop.enterprise.knowledge.v1",
          version: 1,
          operation_id: operationId,
          evidence_id: evidenceId,
          available: true,
          reason: null,
          resource_token: null,
          locator: "https://result.example/source",
        },
      }),
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  fireEvent.click(
    await screen.findByRole("button", { name: "Check source link" }),
  );
  const link = await screen.findByRole("link", {
    name: "Open authorized source",
  });
  expect(link.getAttribute("href")).toBe("https://result.example/source");
  expect(
    requests.filter(({ path }) => path.endsWith("/evidence.original")),
  ).toHaveLength(1);
  expect(
    requests.filter(({ path }) => path.endsWith("/evidence.read")),
  ).toHaveLength(0);
});

it("rejects an unsafe locator in an authoritative web evidence list", async () => {
  await setupGlobals();
  const web = evidenceRecord({
    source_kind: "WEB",
    connection_id: null,
    kb_id: null,
    knowledge_id: null,
    locator: "javascript:alert(1)",
  });
  const { value } = props({}, { list: () => listResponse([web]) });
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(await screen.findByText("Evidence could not be loaded.")).toBeTruthy();
  expect(screen.queryByRole("link")).toBeNull();
});

it("reads registered knowledge through the dock and appends continuation in server order", async () => {
  await setupGlobals();
  const continuation = "44444444-4444-4444-8444-444444444444";
  const readBodies: Record<string, unknown>[] = [];
  const { value, requests } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      read: (payload) => {
        readBodies.push(payload);
        const page =
          readBodies.length === 1
            ? { content: "first ", truncated: true, continuation }
            : { content: "second", truncated: false, continuation: null };
        return {
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: evidenceId,
            knowledge_id: "opaque-doc",
            status: "SUCCESS",
            ...page,
            source_version: null,
          },
        };
      },
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await waitFor(() =>
    expect(document.querySelector("pre")?.textContent).toBe("first "),
  );
  const more = await screen.findByRole("button", { name: "Load more" });
  fireEvent.click(more);
  fireEvent.click(more);
  await waitFor(() =>
    expect(document.querySelector("pre")?.textContent).toBe("first second"),
  );
  expect(readBodies).toEqual([
    { operation_id: operationId, evidence_id: evidenceId },
    { operation_id: operationId, evidence_id: evidenceId, continuation },
  ]);
  expect(
    requests.filter(({ path }) => path.endsWith("/evidence.read")),
  ).toHaveLength(2);
});

it("keeps successful text and ends the cursor when a continuation returns EMPTY", async () => {
  await setupGlobals();
  const continuation = "88888888-8888-4888-8888-888888888888";
  let calls = 0;
  const { value } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      read: () => {
        calls += 1;
        if (calls === 1)
          return {
            api_version: 1,
            result: {
              schema_id: "octop.enterprise.knowledge.v1",
              version: 1,
              operation_id: operationId,
              evidence_id: evidenceId,
              knowledge_id: "opaque-doc",
              status: "SUCCESS",
              content: "retained text",
              truncated: true,
              continuation,
              source_version: null,
            },
          };
        return {
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: evidenceId,
            knowledge_id: "opaque-doc",
            status: "EMPTY",
            content: "",
            truncated: false,
            continuation: null,
            source_version: null,
          },
        };
      },
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await waitFor(() =>
    expect(document.querySelector("pre")?.textContent).toBe("retained text"),
  );
  fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
  expect(
    await screen.findByText("There is no more retrieved text."),
  ).toBeTruthy();
  expect(document.querySelector("pre")?.textContent).toBe("retained text");
  expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
});

it("accepts 8000 Unicode code points when each character uses a surrogate pair", async () => {
  await setupGlobals();
  const text = "😀".repeat(8000);
  const { value } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      read: () => ({
        api_version: 1,
        result: {
          schema_id: "octop.enterprise.knowledge.v1",
          version: 1,
          operation_id: operationId,
          evidence_id: evidenceId,
          knowledge_id: "opaque-doc",
          status: "SUCCESS",
          content: text,
          truncated: false,
          continuation: null,
          source_version: null,
        },
      }),
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await waitFor(() =>
    expect(screen.getByText("Retrieved text", { selector: "h2" })).toBeTruthy(),
  );
  expect(screen.getByText(text)).toBeTruthy();
  expect(screen.queryByText("Retrieved text could not be loaded.")).toBeNull();
});

it("rejects a read page above 8000 Unicode code points without publishing text or cursor", async () => {
  await setupGlobals();
  const text = "😀".repeat(8001);
  const continuation = "88888888-8888-4888-8888-888888888888";
  const { value, requests } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      read: () => ({
        api_version: 1,
        result: {
          schema_id: "octop.enterprise.knowledge.v1",
          version: 1,
          operation_id: operationId,
          evidence_id: evidenceId,
          knowledge_id: "opaque-doc",
          status: "SUCCESS",
          content: text,
          truncated: true,
          continuation,
          source_version: null,
        },
      }),
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(
    await screen.findByText("Retrieved text could not be loaded."),
  ).toBeTruthy();
  expect(document.querySelector("pre")).toBeNull();
  expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  expect(
    requests.filter(({ path }) => path.endsWith("/evidence.read")),
  ).toHaveLength(1);
  expect(
    JSON.parse(
      String(
        requests.find(({ path }) => path.endsWith("/evidence.read"))?.init
          ?.body,
      ),
    ),
  ).toEqual({
    api_version: 1,
    context: { agent_id: target.agentId, thread_id: target.threadId },
    payload: { operation_id: operationId, evidence_id: evidenceId },
  });
});

it("retries a recoverable read failure with the same cursor and keeps prior text", async () => {
  await setupGlobals();
  const continuation = "55555555-5555-4555-8555-555555555555";
  const readPayloads: Array<Record<string, unknown>> = [];
  const { value } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      read: (payload) => {
        readPayloads.push(payload);
        if (readPayloads.length === 1)
          return {
            api_version: 1,
            result: {
              schema_id: "octop.enterprise.knowledge.v1",
              version: 1,
              operation_id: operationId,
              evidence_id: evidenceId,
              knowledge_id: "opaque-doc",
              status: "SUCCESS",
              content: "kept ",
              truncated: true,
              continuation,
              source_version: null,
            },
          };
        if (readPayloads.length === 2) throw new Error("network unavailable");
        return {
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: evidenceId,
            knowledge_id: "opaque-doc",
            status: "SUCCESS",
            content: "continued",
            truncated: false,
            continuation: null,
            source_version: null,
          },
        };
      },
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await waitFor(() =>
    expect(document.querySelector("pre")?.textContent).toBe("kept "),
  );
  fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
  fireEvent.click(await screen.findByRole("button", { name: "Retry reading" }));
  await waitFor(() =>
    expect(document.querySelector("pre")?.textContent).toBe("kept continued"),
  );
  expect(readPayloads).toHaveLength(3);
  expect(readPayloads[1]).toEqual({
    operation_id: operationId,
    evidence_id: evidenceId,
    continuation,
  });
  expect(readPayloads[2]).toEqual({
    operation_id: operationId,
    evidence_id: evidenceId,
    continuation,
  });
});

it("clears terminal cursor errors and requires a fresh list before restart", async () => {
  await setupGlobals();
  const continuation = "66666666-6666-4666-8666-666666666666";
  let reads = 0;
  let lists = 0;
  const { value, requests } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      list: () => {
        lists += 1;
        return listResponse();
      },
      read: () => {
        reads += 1;
        if (reads === 1)
          return {
            api_version: 1,
            result: {
              schema_id: "octop.enterprise.knowledge.v1",
              version: 1,
              operation_id: operationId,
              evidence_id: evidenceId,
              knowledge_id: "opaque-doc",
              status: "SUCCESS",
              content: "read text",
              truncated: true,
              continuation,
              source_version: null,
            },
          };
        if (reads === 2)
          throw new Error(
            'Request failed: 400 Bad Request - {"error":{"code":"READ_CURSOR_INVALID"}}',
          );
        return {
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: evidenceId,
            knowledge_id: "opaque-doc",
            status: "EMPTY",
            content: "",
            truncated: false,
            continuation: null,
            source_version: null,
          },
        };
      },
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(await screen.findByText("read text")).toBeTruthy();
  fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
  expect(
    await screen.findByText(
      "The reading cursor is invalid or expired. Restart reading.",
    ),
  ).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Retry reading" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Restart reading" }));
  await screen.findByText("No retrieved text is available.");
  expect(reads).toBe(3);
  expect(lists).toBe(2);
  expect(
    requests.filter(({ path }) => path.endsWith("/evidence.list")),
  ).toHaveLength(2);
});

it("stops on source change, preserves old text until explicit restart, then starts fresh", async () => {
  await setupGlobals();
  const continuation = "77777777-7777-4777-8777-777777777777";
  const readPayloads: Array<Record<string, unknown>> = [];
  let reads = 0;
  let lists = 0;
  const { value } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      list: () => {
        lists += 1;
        return listResponse([
          evidenceRecord({
            knowledge_id: lists === 1 ? "opaque-doc" : "new-doc",
          }),
        ]);
      },
      read: (payload) => {
        reads += 1;
        readPayloads.push(payload);
        if (reads === 1)
          return {
            api_version: 1,
            result: {
              schema_id: "octop.enterprise.knowledge.v1",
              version: 1,
              operation_id: operationId,
              evidence_id: evidenceId,
              knowledge_id: "opaque-doc",
              status: "SUCCESS",
              content: "old version",
              truncated: true,
              continuation,
              source_version: null,
            },
          };
        if (reads === 2)
          throw new Error(
            'Request failed: 409 Conflict - {"error":{"code":"READ_SOURCE_CHANGED"}}',
          );
        return {
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: evidenceId,
            knowledge_id: "new-doc",
            status: "SUCCESS",
            content: "new version",
            truncated: false,
            continuation: null,
            source_version: null,
          },
        };
      },
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await waitFor(() =>
    expect(document.querySelector("pre")?.textContent).toBe("old version"),
  );
  fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
  expect(
    await screen.findByText(
      "The source changed. Restart reading to load current text.",
    ),
  ).toBeTruthy();
  expect(document.querySelector("pre")?.textContent).toBe("old version");
  expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Restart reading" }));
  await waitFor(() =>
    expect(document.querySelector("pre")?.textContent).toBe("new version"),
  );
  expect(document.querySelector("pre")?.textContent).not.toContain(
    "old version",
  );
  expect(reads).toBe(3);
  expect(lists).toBe(2);
  expect(readPayloads[2]).toEqual({
    operation_id: operationId,
    evidence_id: evidenceId,
  });
});

it("clears cards and retrieved text when a read is denied", async () => {
  await setupGlobals();
  const { value } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      read: () => {
        throw new Error(
          'Request failed: 403 Forbidden - {"error":{"code":"ACCESS_DENIED"}}',
        );
      },
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(
    await screen.findByText(
      "Access changed. Reopen this result to check authorization.",
    ),
  ).toBeTruthy();
  expect(screen.queryByText("Registered title")).toBeNull();
});

it("shows fixed original unavailability and sends no resource request", async () => {
  await setupGlobals();
  const { value, requests } = props({
    presentation: {
      location: "side-panel",
      active: true,
      state: { operation_id: operationId, evidence_id: evidenceId },
    },
  });
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await screen.findByText("No retrieved text is available.");
  fireEvent.click(
    screen.getByRole("button", { name: "Check original availability" }),
  );
  expect(
    await screen.findByText(
      "Original access is unavailable: capability is unverified.",
    ),
  ).toBeTruthy();
  expect(
    requests.filter(
      ({ path }) => path.includes("resource") || path.includes("download"),
    ),
  ).toHaveLength(0);
  expect(
    requests.filter(({ path }) => path.endsWith("/evidence.original")),
  ).toHaveLength(1);
  const originalRequest = requests.find(({ path }) =>
    path.endsWith("/evidence.original"),
  );
  expect(JSON.parse(String(originalRequest?.init?.body))).toEqual({
    api_version: 1,
    context: { agent_id: target.agentId, thread_id: target.threadId },
    payload: { operation_id: operationId, evidence_id: evidenceId },
  });
});

it("fails closed when original returns any available resource claim", async () => {
  await setupGlobals();
  const { value, requests } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      original: () => ({
        api_version: 1,
        result: {
          schema_id: "octop.enterprise.knowledge.v1",
          version: 1,
          operation_id: operationId,
          evidence_id: evidenceId,
          available: true,
          reason: "READY",
          resource_token: "forged",
        },
      }),
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await screen.findByText("No retrieved text is available.");
  fireEvent.click(
    screen.getByRole("button", { name: "Check original availability" }),
  );
  expect(
    await screen.findByText("Original availability could not be checked."),
  ).toBeTruthy();
  expect(
    requests.filter(
      ({ path }) => path.includes("resource") || path.includes("download"),
    ),
  ).toHaveLength(0);
});

it("does not display timestamps if the server timezone is invalid", async () => {
  await setupGlobals();
  const { value } = props({}, { timezone: () => ({ timezone: "not/a-zone" }) });
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(
    await screen.findByText("Server timezone is unavailable."),
  ).toBeTruthy();
  expect(screen.queryByText(/Jan 2, 2026/)).toBeNull();
  expect(screen.queryByText("2026-01-02T00:00:00Z")).toBeNull();
});

it("shows only the registered WEB summary content", async () => {
  await setupGlobals();
  const web = evidenceRecord({
    source_kind: "WEB",
    connection_id: null,
    kb_id: null,
    knowledge_id: null,
    content: "Registered web summary text",
  });
  const { value } = props({}, { list: () => listResponse([web]) });
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(await screen.findByText("Registered web summary text")).toBeTruthy();
  expect(screen.queryByText("forged tool text")).toBeNull();
  expect(
    screen
      .getByRole("button", { name: /Registered title/ })
      .hasAttribute("disabled"),
  ).toBe(false);
});

it("invalidates the whole reader mount after an original permission denial during continuation", async () => {
  await setupGlobals();
  const continuation = "88888888-8888-4888-8888-888888888888";
  const originalResult = deferred<unknown>();
  let reads = 0;
  const { value, requests } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      read: () => {
        reads += 1;
        return reads === 1
          ? {
              api_version: 1,
              result: {
                schema_id: "octop.enterprise.knowledge.v1",
                version: 1,
                operation_id: operationId,
                evidence_id: evidenceId,
                knowledge_id: "opaque-doc",
                status: "SUCCESS",
                content: "first ",
                truncated: true,
                continuation,
                source_version: null,
              },
            }
          : {
              api_version: 1,
              result: {
                schema_id: "octop.enterprise.knowledge.v1",
                version: 1,
                operation_id: operationId,
                evidence_id: evidenceId,
                knowledge_id: "opaque-doc",
                status: "SUCCESS",
                content: "second",
                truncated: false,
                continuation: null,
                source_version: null,
              },
            };
      },
      original: () => originalResult.promise,
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await waitFor(() =>
    expect(document.querySelector("pre")?.textContent).toBe("first "),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Check original availability" }),
  );
  fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
  await screen.findByText("first second");
  originalResult.reject(
    new Error(
      'Request failed: 403 Forbidden - {"error":{"code":"ACCESS_DENIED"}}',
    ),
  );
  expect(
    await screen.findByText(
      "Access changed. Reopen this result to check authorization.",
    ),
  ).toBeTruthy();
  expect(screen.queryByText("first second")).toBeNull();
  expect(screen.queryByRole("button", { name: "Retry reading" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Restart reading" })).toBeNull();
  expect(
    requests.filter(({ path }) => path.endsWith("/evidence.read")),
  ).toHaveLength(2);
});

it("keeps an original success attached to its selection while a continuation succeeds", async () => {
  await setupGlobals();
  const continuation = "abababab-abab-4bab-8bab-abababababab";
  const originalResult = deferred<unknown>();
  let reads = 0;
  const { value } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      read: () => {
        reads += 1;
        return {
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: evidenceId,
            knowledge_id: "opaque-doc",
            status: "SUCCESS",
            content: reads === 1 ? "first" : "second",
            truncated: reads === 1,
            continuation: reads === 1 ? continuation : null,
            source_version: null,
          },
        };
      },
      original: () => originalResult.promise,
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await waitFor(() =>
    expect(document.querySelector("pre")?.textContent).toBe("first"),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Check original availability" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  await waitFor(() =>
    expect(document.querySelector("pre")?.textContent).toBe("firstsecond"),
  );
  originalResult.resolve({
    api_version: 1,
    result: {
      schema_id: "octop.enterprise.knowledge.v1",
      version: 1,
      operation_id: operationId,
      evidence_id: evidenceId,
      available: false,
      reason: "CAPABILITY_UNVERIFIED",
      resource_token: null,
    },
  });
  expect(
    await screen.findByText(
      "Original access is unavailable: capability is unverified.",
    ),
  ).toBeTruthy();
  expect(document.querySelector("pre")?.textContent).toBe("firstsecond");
});

it("does not let an old read finally release a restarted read slot", async () => {
  await setupGlobals();
  const continuation = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd";
  const oldContinuation = deferred<unknown>();
  const freshRead = deferred<unknown>();
  const originalResult = deferred<unknown>();
  let reads = 0;
  const { value } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      read: () => {
        reads += 1;
        if (reads === 2) return oldContinuation.promise;
        if (reads === 3) return freshRead.promise;
        return {
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: evidenceId,
            knowledge_id: "opaque-doc",
            status: "SUCCESS",
            content: "old",
            truncated: true,
            continuation,
            source_version: null,
          },
        };
      },
      original: () => originalResult.promise,
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await waitFor(() =>
    expect(document.querySelector("pre")?.textContent).toBe("old"),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Check original availability" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  await waitFor(() => expect(reads).toBe(2));
  originalResult.reject(
    new Error(
      'Request failed: 409 Conflict - {"error":{"code":"READ_SOURCE_CHANGED"}}',
    ),
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Restart reading" }),
  );
  await waitFor(() => expect(reads).toBe(3));
  oldContinuation.reject(new Error("late cancelled request"));
  freshRead.resolve({
    api_version: 1,
    result: {
      schema_id: "octop.enterprise.knowledge.v1",
      version: 1,
      operation_id: operationId,
      evidence_id: evidenceId,
      knowledge_id: "opaque-doc",
      status: "SUCCESS",
      content: "fresh slot",
      truncated: false,
      continuation: null,
      source_version: null,
    },
  });
  expect(await screen.findByText("fresh slot")).toBeTruthy();
  expect(
    screen.queryByText(
      "Access changed. Reopen this result to check authorization.",
    ),
  ).toBeNull();
});

it("does not issue another list request after this mount is denied", async () => {
  await setupGlobals();
  let lists = 0;
  const { value } = props(
    {},
    {
      list: () => {
        lists += 1;
        throw new Error(
          'Request failed: 403 Forbidden - {"error":{"code":"ACCESS_DENIED"}}',
        );
      },
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await screen.findByText(
    "Access changed. Reopen this result to check authorization.",
  );
  expect(
    screen.queryByRole("button", { name: "Retry evidence list" }),
  ).toBeNull();
  expect(lists).toBe(1);
});

it("aborts an in-flight list retry on hide and ignores its late result", async () => {
  await setupGlobals();
  const retryList = deferred<unknown>();
  let lists = 0;
  const { value, requests } = props(
    {},
    {
      list: () => {
        lists += 1;
        if (lists === 1) throw new Error("temporary list transport failure");
        return retryList.promise;
      },
    },
  );
  const mounted = render(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Retry evidence list" }),
  );
  await waitFor(() => expect(lists).toBe(2));
  const retrySignal = requests
    .filter(({ path }) => path.endsWith("/evidence.list"))
    .at(-1)?.init?.signal;
  mounted.rerender(
    <EvidenceRenderer
      {...(value as unknown as PluginToolProps)}
      presentation={{ location: "inline", active: false }}
    />,
  );
  await waitFor(() => expect(retrySignal?.aborted).toBe(true));
  retryList.resolve(listResponse());
  await waitFor(() =>
    expect(screen.queryByText("Registered title")).toBeNull(),
  );
  expect(lists).toBe(2);
});

it("retries a failed restart by fetching a fresh list before a first-page read", async () => {
  await setupGlobals();
  const continuation = "99999999-9999-4999-8999-999999999999";
  let lists = 0;
  const readPayloads: Array<Record<string, unknown>> = [];
  const { value, requests } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      list: () => {
        lists += 1;
        if (lists === 2) throw new Error("temporary list transport failure");
        return listResponse();
      },
      read: (payload) => {
        readPayloads.push(payload);
        if (readPayloads.length === 1)
          return {
            api_version: 1,
            result: {
              schema_id: "octop.enterprise.knowledge.v1",
              version: 1,
              operation_id: operationId,
              evidence_id: evidenceId,
              knowledge_id: "opaque-doc",
              status: "SUCCESS",
              content: "old",
              truncated: true,
              continuation,
              source_version: null,
            },
          };
        if (readPayloads.length === 2)
          throw new Error(
            'Request failed: 409 Conflict - {"error":{"code":"READ_SOURCE_CHANGED"}}',
          );
        return {
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: evidenceId,
            knowledge_id: "opaque-doc",
            status: "SUCCESS",
            content: "fresh",
            truncated: false,
            continuation: null,
            source_version: null,
          },
        };
      },
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await screen.findByText("old");
  fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
  await screen.findByText(
    "The source changed. Restart reading to load current text.",
  );
  fireEvent.click(screen.getByRole("button", { name: "Restart reading" }));
  await screen.findByText("Retrieved text could not be loaded.");
  expect(readPayloads).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "Retry reading" }));
  await screen.findByText("fresh");
  expect(lists).toBe(3);
  expect(readPayloads).toHaveLength(3);
  expect(readPayloads[2]).toEqual({
    operation_id: operationId,
    evidence_id: evidenceId,
  });
  expect(
    requests.filter(({ path }) => path.endsWith("/evidence.list")),
  ).toHaveLength(3);
  expect(
    requests.filter(({ path }) => path.endsWith("/evidence.list"))[2].init
      ?.signal,
  ).toBeInstanceOf(AbortSignal);
});

it.each([
  ["missing list schema", (value: any) => delete value.result.schema_id],
  ["string list version", (value: any) => (value.result.version = "1")],
  [
    "missing operation version",
    (value: any) => delete value.result.operation.version,
  ],
  [
    "string operation version",
    (value: any) => (value.result.operation.version = "1"),
  ],
  [
    "missing operation schema",
    (value: any) => delete value.result.operation.schema_id,
  ],
  [
    "wrong operation schema type",
    (value: any) => (value.result.operation.schema_id = 1),
  ],
  [
    "missing evidence schema",
    (value: any) => delete value.result.evidence[0].schema_id,
  ],
  [
    "wrong evidence schema type",
    (value: any) => (value.result.evidence[0].schema_id = 1),
  ],
  [
    "missing evidence version",
    (value: any) => delete value.result.evidence[0].version,
  ],
  [
    "string evidence version",
    (value: any) => (value.result.evidence[0].version = "1"),
  ],
  [
    "wrong operation thread",
    (value: any) => (value.result.operation.thread_id = "other-thread"),
  ],
  [
    "wrong evidence thread",
    (value: any) => (value.result.evidence[0].thread_id = "other-thread"),
  ],
  [
    "wrong evidence user",
    (value: any) => (value.result.evidence[0].user_id = 10),
  ],
  [
    "wrong evidence agent",
    (value: any) => (value.result.evidence[0].agent_id = "speaker-agent"),
  ],
  [
    "wrong evidence operation",
    (value: any) =>
      (value.result.evidence[0].operation_id =
        "ffffffff-ffff-4fff-8fff-ffffffffffff"),
  ],
  [
    "missing knowledge connection",
    (value: any) => delete value.result.evidence[0].connection_id,
  ],
  [
    "empty knowledge base id",
    (value: any) => (value.result.evidence[0].kb_id = ""),
  ],
  [
    "missing knowledge base id",
    (value: any) => delete value.result.evidence[0].kb_id,
  ],
  [
    "invalid knowledge connection UUID",
    (value: any) => (value.result.evidence[0].connection_id = "not-a-uuid"),
  ],
  [
    "missing knowledge identifier",
    (value: any) => delete value.result.evidence[0].knowledge_id,
  ],
  [
    "WEB connection must be explicitly null",
    (value: any) => {
      value.result.evidence[0].source_kind = "WEB";
      delete value.result.evidence[0].connection_id;
      value.result.evidence[0].kb_id = null;
      value.result.evidence[0].knowledge_id = null;
    },
  ],
  [
    "duplicate evidence identifier",
    (value: any) => value.result.evidence.push(evidenceRecord()),
  ],
  [
    "non-UTC retrieval time",
    (value: any) =>
      (value.result.evidence[0].retrieved_at = "2026-01-02T00:00:00+08:00"),
  ],
])(
  "fails closed for %s in an authoritative evidence list",
  async (_label, mutate) => {
    await setupGlobals();
    const invalid = listResponse();
    mutate(invalid);
    const { value } = props({}, { list: () => invalid });
    render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
    expect(
      await screen.findByText("Evidence could not be loaded."),
    ).toBeTruthy();
    expect(screen.queryByText("Registered title")).toBeNull();
  },
);

it.each([
  ["missing knowledge id", (value: any) => delete value.result.knowledge_id],
  ["missing read schema", (value: any) => delete value.result.schema_id],
  [
    "wrong read operation",
    (value: any) =>
      (value.result.operation_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"),
  ],
  [
    "wrong read evidence",
    (value: any) =>
      (value.result.evidence_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"),
  ],
  [
    "wrong knowledge id",
    (value: any) => (value.result.knowledge_id = "another-document"),
  ],
  ["unknown read status", (value: any) => (value.result.status = "PARTIAL")],
  [
    "EMPTY response with content",
    (value: any) => (value.result.content = "must reject"),
  ],
  ["cursor mismatch", (value: any) => (value.result.truncated = true)],
  [
    "non-null source version",
    (value: any) => (value.result.source_version = "changed"),
  ],
])("rejects a read response with %s", async (_label, mutate) => {
  await setupGlobals();
  const invalid = {
    api_version: 1,
    result: {
      schema_id: "octop.enterprise.knowledge.v1",
      version: 1,
      operation_id: operationId,
      evidence_id: evidenceId,
      knowledge_id: "opaque-doc",
      status: "EMPTY",
      content: "",
      truncated: false,
      continuation: null,
      source_version: null,
    },
  };
  mutate(invalid);
  const { value } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    { read: () => invalid },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(
    await screen.findByText("Retrieved text could not be loaded."),
  ).toBeTruthy();
  expect(document.querySelector("pre")).toBeNull();
});

it("uses server timezone for localized display and hides time on timezone request failure", async () => {
  await setupGlobals();
  const valid = props(
    {},
    { timezone: () => ({ timezone: "Pacific/Honolulu" }) },
  );
  render(<EvidenceRenderer {...(valid.value as unknown as PluginToolProps)} />);
  expect(await screen.findByText(/Jan 1, 2026, 2:00 PM/)).toBeTruthy();
  cleanup();
  const failed = props(
    {},
    { timezone: () => Promise.reject(new Error("offline")) },
  );
  render(
    <EvidenceRenderer {...(failed.value as unknown as PluginToolProps)} />,
  );
  expect(
    await screen.findByText("Server timezone is unavailable."),
  ).toBeTruthy();
  expect(screen.queryByText("2026-01-02T00:00:00Z")).toBeNull();
});

it("focuses the reader heading after selection activation", async () => {
  await setupGlobals();
  const { value } = props({
    presentation: {
      location: "side-panel",
      active: true,
      state: { operation_id: operationId, evidence_id: evidenceId },
    },
  });
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  const heading = await screen.findByRole("heading", {
    name: "Retrieved text",
  });
  await waitFor(() => expect(document.activeElement).toBe(heading));
});

it("sends only the successful continuation once for repeated same-stack load-more events", async () => {
  await setupGlobals();
  const continuation = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const nextPage = deferred<unknown>();
  const readPayloads: Array<Record<string, unknown>> = [];
  const { value } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      read: (payload) => {
        readPayloads.push(payload);
        if (readPayloads.length === 1)
          return {
            api_version: 1,
            result: {
              schema_id: "octop.enterprise.knowledge.v1",
              version: 1,
              operation_id: operationId,
              evidence_id: evidenceId,
              knowledge_id: "opaque-doc",
              status: "SUCCESS",
              content: "first",
              truncated: true,
              continuation,
              source_version: null,
            },
          };
        return nextPage.promise;
      },
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await waitFor(() =>
    expect(document.querySelector("pre")?.textContent).toBe("first"),
  );
  const button = screen.getByRole("button", { name: "Load more" });
  act(() => {
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  expect(readPayloads).toHaveLength(2);
  expect(readPayloads[1]).toEqual({
    operation_id: operationId,
    evidence_id: evidenceId,
    continuation,
  });
  nextPage.resolve({
    api_version: 1,
    result: {
      schema_id: "octop.enterprise.knowledge.v1",
      version: 1,
      operation_id: operationId,
      evidence_id: evidenceId,
      knowledge_id: "opaque-doc",
      status: "SUCCESS",
      content: " second",
      truncated: false,
      continuation: null,
      source_version: null,
    },
  });
  await waitFor(() =>
    expect(document.querySelector("pre")?.textContent).toBe("first second"),
  );
});

it("treats READ_SOURCE_CHANGED from original as terminal for continuation", async () => {
  await setupGlobals();
  const continuation = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  let reads = 0;
  const { value, requests } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      read: () => {
        reads += 1;
        return {
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: evidenceId,
            knowledge_id: "opaque-doc",
            status: "SUCCESS",
            content: "old text",
            truncated: true,
            continuation,
            source_version: null,
          },
        };
      },
      original: () => {
        throw new Error(
          'Request failed: 409 Conflict - {"error":{"code":"READ_SOURCE_CHANGED"}}',
        );
      },
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await waitFor(() =>
    expect(document.querySelector("pre")?.textContent).toBe("old text"),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Check original availability" }),
  );
  expect(
    await screen.findByText(
      "The source changed. Restart reading to load current text.",
    ),
  ).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  expect(document.querySelector("pre")?.textContent).toBe("old text");
  expect(reads).toBe(1);
  expect(
    requests.filter(({ path }) => path.endsWith("/evidence.original")),
  ).toHaveLength(1);
});

it("reopens a same-call selection from a fresh list and reads the selected registered ID", async () => {
  await setupGlobals();
  const secondId = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
  const readPayloads: Array<Record<string, unknown>> = [];
  let lists = 0;
  const { value } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      list: () => {
        lists += 1;
        return listResponse([
          evidenceRecord(),
          evidenceRecord({
            evidence_id: secondId,
            title: "Second registered title",
          }),
        ]);
      },
      read: (payload) => {
        readPayloads.push(payload);
        return {
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: payload.evidence_id,
            knowledge_id: "opaque-doc",
            status: "SUCCESS",
            content: String(payload.evidence_id),
            truncated: false,
            continuation: null,
            source_version: null,
          },
        };
      },
    },
  );
  const mounted = render(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  expect(await screen.findByText(evidenceId)).toBeTruthy();
  mounted.rerender(
    <EvidenceRenderer
      {...(value as unknown as PluginToolProps)}
      presentation={{
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: secondId },
      }}
    />,
  );
  expect(await screen.findByText(secondId)).toBeTruthy();
  expect(readPayloads.map((payload) => payload.evidence_id)).toEqual([
    evidenceId,
    secondId,
  ]);
  expect(lists).toBe(2);
});

it("does not read when dock state belongs to another operation", async () => {
  await setupGlobals();
  const { value, requests } = props({
    presentation: {
      location: "side-panel",
      active: true,
      state: {
        operation_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
        evidence_id: evidenceId,
      },
    },
  });
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(await screen.findByText("Registered title")).toBeTruthy();
  expect(
    requests.filter(({ path }) => path.endsWith("/evidence.read")),
  ).toHaveLength(0);
});

it("rejects unknown evidence in a matching dock state without falling back to the first row", async () => {
  await setupGlobals();
  const { value, requests } = props({
    presentation: {
      location: "side-panel",
      active: true,
      state: {
        operation_id: operationId,
        evidence_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
      },
    },
  });
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(await screen.findByText("Evidence could not be loaded.")).toBeTruthy();
  expect(screen.queryByText("Registered title")).toBeNull();
  expect(
    requests.filter(
      ({ path }) =>
        path.endsWith("/evidence.read") || path.endsWith("/evidence.original"),
    ),
  ).toHaveLength(0);
});

it.each([
  ["missing schema", (value: any) => delete value.result.schema_id],
  ["missing version", (value: any) => delete value.result.version],
  [
    "wrong operation",
    (value: any) =>
      (value.result.operation_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"),
  ],
  [
    "wrong evidence",
    (value: any) =>
      (value.result.evidence_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"),
  ],
  ["missing reason", (value: any) => delete value.result.reason],
  [
    "missing resource token",
    (value: any) => delete value.result.resource_token,
  ],
])("fails closed when original output has %s", async (_label, mutate) => {
  await setupGlobals();
  const invalid = {
    api_version: 1,
    result: {
      schema_id: "octop.enterprise.knowledge.v1",
      version: 1,
      operation_id: operationId,
      evidence_id: evidenceId,
      available: false,
      reason: "CAPABILITY_UNVERIFIED",
      resource_token: null,
    },
  };
  mutate(invalid);
  const { value, requests } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    { original: () => invalid },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  await screen.findByText("No retrieved text is available.");
  fireEvent.click(
    screen.getByRole("button", { name: "Check original availability" }),
  );
  expect(
    await screen.findByText("Original availability could not be checked."),
  ).toBeTruthy();
  expect(
    requests.filter(
      ({ path }) => path.includes("resource") || path.includes("download"),
    ),
  ).toHaveLength(0);
});

it("renders a native focusable evidence button; browser keyboard activation is separate", async () => {
  await setupGlobals();
  const { value } = props();
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  const button = await screen.findByRole("button", {
    name: /Registered title/,
  });
  button.focus();
  expect(document.activeElement).toBe(button);
  expect(button.tagName).toBe("BUTTON");
  expect(button.tabIndex).toBe(0);
  fireEvent.click(button);
  expect(value.host.openSidePanel).toHaveBeenCalledWith(
    expect.objectContaining({
      callId: "call-1",
      state: { operation_id: operationId, evidence_id: evidenceId },
    }),
  );
});

it("aborts read work on hide and resolves a fresh list and read after reactivation", async () => {
  await setupGlobals();
  const continuation = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
  const latePage = deferred<unknown>();
  let lists = 0;
  let reads = 0;
  const { value, requests } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      list: () => {
        lists += 1;
        return listResponse();
      },
      read: () => {
        reads += 1;
        if (reads === 2) return latePage.promise;
        return {
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: evidenceId,
            knowledge_id: "opaque-doc",
            status: "SUCCESS",
            content: `session-${reads}`,
            truncated: reads === 1,
            continuation: reads === 1 ? continuation : null,
            source_version: null,
          },
        };
      },
    },
  );
  const mounted = render(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  expect(await screen.findByText("session-1")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  await waitFor(() => expect(reads).toBe(2));
  const oldSignal = requests
    .filter(({ path }) => path.endsWith("/evidence.read"))
    .at(-1)?.init?.signal;
  mounted.rerender(
    <EvidenceRenderer
      {...(value as unknown as PluginToolProps)}
      presentation={{ location: "side-panel", active: false }}
    />,
  );
  await waitFor(() => expect(oldSignal?.aborted).toBe(true));
  expect(screen.queryByText("session-1")).toBeNull();
  mounted.rerender(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  expect(await screen.findByText("session-3")).toBeTruthy();
  latePage.resolve({
    api_version: 1,
    result: {
      schema_id: "octop.enterprise.knowledge.v1",
      version: 1,
      operation_id: operationId,
      evidence_id: evidenceId,
      knowledge_id: "opaque-doc",
      status: "SUCCESS",
      content: "stale continuation",
      truncated: false,
      continuation: null,
      source_version: null,
    },
  });
  await waitFor(() =>
    expect(screen.queryByText("stale continuation")).toBeNull(),
  );
  expect(lists).toBe(2);
  expect(reads).toBe(3);
});

it("discards a late list result after the mounted Host target is replaced", async () => {
  await setupGlobals();
  const oldList = deferred<unknown>();
  let currentTarget = { ...target };
  let lists = 0;
  const { value } = props(
    {},
    {
      list: () => {
        lists += 1;
        if (lists === 1) return oldList.promise;
        const response = listResponse([
          evidenceRecord({
            evidence_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            agent_id: currentTarget.agentId,
            thread_id: currentTarget.threadId,
            title: "Replacement target evidence",
          }),
        ]);
        response.result.operation.agent_id = currentTarget.agentId;
        response.result.operation.thread_id = currentTarget.threadId;
        return response;
      },
    },
  );
  value.host.getToolContext = () => currentTarget;
  const mounted = render(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  await waitFor(() => expect(lists).toBe(1));
  currentTarget = {
    ...target,
    agentId: "replacement-agent",
    threadId: "replacement-thread",
  };
  mounted.rerender(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  expect(await screen.findByText("Replacement target evidence")).toBeTruthy();
  oldList.resolve(
    listResponse([evidenceRecord({ title: "Stale prior target evidence" })]),
  );
  await waitFor(() =>
    expect(screen.queryByText("Stale prior target evidence")).toBeNull(),
  );
  expect(lists).toBe(2);
});

it("does not publish a late timezone result after hide and reactivation", async () => {
  await setupGlobals();
  const oldTimezone = deferred<unknown>();
  let zones = 0;
  const { value, requests } = props(
    {},
    {
      timezone: () => {
        zones += 1;
        return zones === 1
          ? oldTimezone.promise
          : { timezone: "Pacific/Honolulu" };
      },
    },
  );
  const mounted = render(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  await waitFor(() => expect(zones).toBe(1));
  const oldSignal = requests.find(({ path }) => path === "/settings/timezone")
    ?.init?.signal;
  mounted.rerender(
    <EvidenceRenderer
      {...(value as unknown as PluginToolProps)}
      presentation={{ location: "inline", active: false }}
    />,
  );
  await waitFor(() => expect(oldSignal?.aborted).toBe(true));
  mounted.rerender(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  expect(await screen.findByText(/Jan 1, 2026, 2:00 PM/)).toBeTruthy();
  oldTimezone.resolve({ timezone: "UTC" });
  await waitFor(() =>
    expect(screen.getByText(/Jan 1, 2026, 2:00 PM/)).toBeTruthy(),
  );
  expect(zones).toBe(2);
});

it.each([
  [
    "unavailable success",
    () => ({
      api_version: 1,
      result: {
        schema_id: "octop.enterprise.knowledge.v1",
        version: 1,
        operation_id: operationId,
        evidence_id: evidenceId,
        available: false,
        reason: "CAPABILITY_UNVERIFIED",
        resource_token: null,
      },
    }),
  ],
  [
    "permission denial",
    () =>
      new Error(
        'Request failed: 403 Forbidden - {"error":{"code":"ACCESS_DENIED"}}',
      ),
  ],
  [
    "source change",
    () =>
      new Error(
        'Request failed: 409 Conflict - {"error":{"code":"READ_SOURCE_CHANGED"}}',
      ),
  ],
])(
  "discards a pending original %s after same-call selection changes",
  async (_label, settleOriginal) => {
    await setupGlobals();
    const secondId = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
    const original = deferred<unknown>();
    const second = evidenceRecord({
      evidence_id: secondId,
      title: "Second selection",
    });
    const { value, requests } = props(
      {
        presentation: {
          location: "side-panel",
          active: true,
          state: { operation_id: operationId, evidence_id: evidenceId },
        },
      },
      {
        list: () => listResponse([evidenceRecord(), second]),
        original: () => original.promise,
        read: (payload) => ({
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: payload.evidence_id,
            knowledge_id: "opaque-doc",
            status: "EMPTY",
            content: "",
            truncated: false,
            continuation: null,
            source_version: null,
          },
        }),
      },
    );
    const mounted = render(
      <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
    );
    await screen.findByText("No retrieved text is available.");
    const originalButton = screen.getByRole("button", {
      name: "Check original availability",
    });
    fireEvent.click(originalButton);
    await screen.findByText("Checking original availability…");
    const oldSignal = requests.find(({ path }) =>
      path.endsWith("/evidence.original"),
    )?.init?.signal;
    mounted.rerender(
      <EvidenceRenderer
        {...(value as unknown as PluginToolProps)}
        presentation={{
          location: "side-panel",
          active: true,
          state: { operation_id: operationId, evidence_id: secondId },
        }}
      />,
    );
    expect(await screen.findByText("Second selection")).toBeTruthy();
    expect(
      await screen.findByText("No retrieved text is available."),
    ).toBeTruthy();
    await waitFor(() => expect(oldSignal?.aborted).toBe(true));
    const callsAfterReplacement = requests.length;
    const result = settleOriginal();
    if (result instanceof Error) original.reject(result);
    else original.resolve(result);
    await act(async () => Promise.resolve());
    expect(requests).toHaveLength(callsAfterReplacement);
    expect(
      screen.queryByText(
        "Original access is unavailable: capability is unverified.",
      ),
    ).toBeNull();
    expect(
      screen.queryByText("Original availability could not be checked."),
    ).toBeNull();
    expect(
      screen.queryByText(
        "The source changed. Restart reading to load current text.",
      ),
    ).toBeNull();
    expect(screen.queryByText("Second selection")).toBeTruthy();
  },
);

it.each([
  [
    "permission denial",
    () =>
      new Error(
        'Request failed: 403 Forbidden - {"error":{"code":"ACCESS_DENIED"}}',
      ),
  ],
  [
    "source change",
    () =>
      new Error(
        'Request failed: 409 Conflict - {"error":{"code":"READ_SOURCE_CHANGED"}}',
      ),
  ],
])(
  "discards pending original %s when its mounted Host target is replaced",
  async (_label, error) => {
    await setupGlobals();
    const original = deferred<unknown>();
    const { value, requests: oldRequests } = props(
      {
        presentation: {
          location: "side-panel",
          active: true,
          state: { operation_id: operationId, evidence_id: evidenceId },
        },
      },
      { original: () => original.promise },
    );
    const oldButton = () =>
      screen.getByRole("button", { name: "Check original availability" });
    const mounted = render(
      <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
    );
    await screen.findByText("No retrieved text is available.");
    fireEvent.click(oldButton());
    await screen.findByText("Checking original availability…");
    const oldSignal = oldRequests.find(({ path }) =>
      path.endsWith("/evidence.original"),
    )?.init?.signal;

    const replacementTarget = {
      ...target,
      agentId: "replacement-agent",
      threadId: "replacement-thread",
    };
    const replacementEvidence = evidenceRecord({
      evidence_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
      agent_id: replacementTarget.agentId,
      thread_id: replacementTarget.threadId,
      title: "Replacement Host evidence",
    });
    const replacementList = listResponse([replacementEvidence]);
    replacementList.result.operation.agent_id = replacementTarget.agentId;
    replacementList.result.operation.thread_id = replacementTarget.threadId;
    const replacement = props(
      {
        presentation: {
          location: "side-panel",
          active: true,
          state: {
            operation_id: operationId,
            evidence_id: replacementEvidence.evidence_id,
          },
        },
      },
      {
        list: () => replacementList,
        read: (payload) => ({
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: payload.evidence_id,
            knowledge_id: "opaque-doc",
            status: "SUCCESS",
            content: "replacement Host read",
            truncated: false,
            continuation: null,
            source_version: null,
          },
        }),
      },
    );
    replacement.value.host.getToolContext = () => replacementTarget;
    mounted.rerender(
      <EvidenceRenderer
        {...(replacement.value as unknown as PluginToolProps)}
      />,
    );
    expect(await screen.findByText("Replacement Host evidence")).toBeTruthy();
    expect(await screen.findByText("replacement Host read")).toBeTruthy();
    await waitFor(() => expect(oldSignal?.aborted).toBe(true));
    original.reject(error());
    await act(async () => Promise.resolve());
    expect(
      screen.queryByText(
        "Access changed. Reopen this result to check authorization.",
      ),
    ).toBeNull();
    expect(
      screen.queryByText(
        "The source changed. Restart reading to load current text.",
      ),
    ).toBeNull();
    expect(screen.queryByText("Replacement Host evidence")).toBeTruthy();
    expect(
      replacement.value.host.request.mock.calls.some(
        ([path, init]) =>
          path === "/plugins/enterprise-knowledge/actions/evidence.list" &&
          init?.signal instanceof AbortSignal,
      ),
    ).toBe(true);
  },
);

it("ignores a late timezone access rejection after hide and reactivation", async () => {
  await setupGlobals();
  const oldZone = deferred<unknown>();
  let lists = 0;
  let zones = 0;
  const { value, requests } = props(
    {},
    {
      list: () => {
        lists += 1;
        return listResponse();
      },
      timezone: () => (++zones === 1 ? oldZone.promise : { timezone: "UTC" }),
    },
  );
  const mounted = render(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  await waitFor(() => expect(lists).toBe(1));
  const oldSignals = requests.map(({ init }) => init?.signal);
  mounted.rerender(
    <EvidenceRenderer
      {...(value as unknown as PluginToolProps)}
      presentation={{ location: "inline", active: false }}
    />,
  );
  await waitFor(() =>
    expect(oldSignals.every((signal) => signal?.aborted)).toBe(true),
  );
  mounted.rerender(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  expect(await screen.findByText("Registered title")).toBeTruthy();
  oldZone.reject(
    new Error(
      'Request failed: 403 Forbidden - {"error":{"code":"ACCESS_DENIED"}}',
    ),
  );
  await act(async () => Promise.resolve());
  expect(screen.getByText("Registered title")).toBeTruthy();
  expect(
    screen.queryByText(
      "Access changed. Reopen this result to check authorization.",
    ),
  ).toBeNull();
  expect(lists).toBe(2);
  expect(zones).toBe(2);
  expect(requests.some(({ path }) => path.endsWith("/evidence.read"))).toBe(
    false,
  );
});

it("ignores a late reader access rejection after hide and reactivation", async () => {
  await setupGlobals();
  const oldRead = deferred<unknown>();
  let reads = 0;
  const { value, requests } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      read: () => {
        reads += 1;
        if (reads === 1) return oldRead.promise;
        return {
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: evidenceId,
            knowledge_id: "opaque-doc",
            status: "SUCCESS",
            content: "new activation text",
            truncated: false,
            continuation: null,
            source_version: null,
          },
        };
      },
    },
  );
  const mounted = render(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  await waitFor(() => expect(reads).toBe(1));
  const oldSignal = requests.find(({ path }) => path.endsWith("/evidence.read"))
    ?.init?.signal;
  mounted.rerender(
    <EvidenceRenderer
      {...(value as unknown as PluginToolProps)}
      presentation={{ location: "side-panel", active: false }}
    />,
  );
  await waitFor(() => expect(oldSignal?.aborted).toBe(true));
  mounted.rerender(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  expect(await screen.findByText("new activation text")).toBeTruthy();
  oldRead.reject(
    new Error(
      'Request failed: 403 Forbidden - {"error":{"code":"ACCESS_DENIED"}}',
    ),
  );
  await act(async () => Promise.resolve());
  expect(screen.getByText("new activation text")).toBeTruthy();
  expect(screen.getByText("Registered title")).toBeTruthy();
  expect(
    screen.queryByText(
      "Access changed. Reopen this result to check authorization.",
    ),
  ).toBeNull();
  expect(reads).toBe(2);
});

it("ignores a late access rejection from a hidden list after a fresh list succeeds", async () => {
  await setupGlobals();
  const oldList = deferred<unknown>();
  let lists = 0;
  const { value } = props(
    {},
    {
      list: () => (++lists === 1 ? oldList.promise : listResponse()),
    },
  );
  const mounted = render(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  await waitFor(() => expect(lists).toBe(1));
  mounted.rerender(
    <EvidenceRenderer
      {...(value as unknown as PluginToolProps)}
      presentation={{ location: "inline", active: false }}
    />,
  );
  mounted.rerender(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  expect(await screen.findByText("Registered title")).toBeTruthy();
  oldList.reject(
    new Error(
      'Request failed: 403 Forbidden - {"error":{"code":"ACCESS_DENIED"}}',
    ),
  );
  await act(async () => Promise.resolve());
  expect(screen.getByText("Registered title")).toBeTruthy();
  expect(
    screen.queryByText(
      "Access changed. Reopen this result to check authorization.",
    ),
  ).toBeNull();
  expect(lists).toBe(2);
});

it("aborts pending list and timezone on true unmount and starts a fresh mount", async () => {
  await setupGlobals();
  const oldList = deferred<unknown>();
  const oldZone = deferred<unknown>();
  let lists = 0;
  let zones = 0;
  const { value, requests } = props(
    {},
    {
      list: () => (++lists === 1 ? oldList.promise : listResponse()),
      timezone: () => (++zones === 1 ? oldZone.promise : { timezone: "UTC" }),
    },
  );
  const mounted = render(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  await waitFor(() => expect(lists).toBe(1));
  const oldSignals = requests.map(({ init }) => init?.signal);
  mounted.unmount();
  await waitFor(() =>
    expect(oldSignals.every((signal) => signal?.aborted)).toBe(true),
  );
  oldList.reject(new Error("late list rejection"));
  oldZone.reject(new Error("late timezone rejection"));
  await act(async () => Promise.resolve());
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(await screen.findByText("Registered title")).toBeTruthy();
  expect(lists).toBe(2);
  expect(zones).toBe(2);
  expect(requests.some(({ path }) => path.endsWith("/evidence.read"))).toBe(
    false,
  );
  expect(screen.queryByText("Evidence could not be loaded.")).toBeNull();
});

it("ignores a late reader access rejection after true unmount and fresh remount", async () => {
  await setupGlobals();
  const oldRead = deferred<unknown>();
  let reads = 0;
  const { value, requests } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      read: () => {
        reads += 1;
        if (reads === 1) return oldRead.promise;
        return {
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: evidenceId,
            knowledge_id: "opaque-doc",
            status: "SUCCESS",
            content: "remounted reader text",
            truncated: false,
            continuation: null,
            source_version: null,
          },
        };
      },
    },
  );
  const mounted = render(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  await waitFor(() => expect(reads).toBe(1));
  const oldSignal = requests.find(({ path }) => path.endsWith("/evidence.read"))
    ?.init?.signal;
  mounted.unmount();
  await waitFor(() => expect(oldSignal?.aborted).toBe(true));
  oldRead.reject(
    new Error(
      'Request failed: 403 Forbidden - {"error":{"code":"ACCESS_DENIED"}}',
    ),
  );
  await act(async () => Promise.resolve());
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(await screen.findByText("remounted reader text")).toBeTruthy();
  expect(reads).toBe(2);
  expect(
    screen.queryByText(
      "Access changed. Reopen this result to check authorization.",
    ),
  ).toBeNull();
});

it("blocks saved reader callbacks after denial, aborts the sibling read, and permits fresh work only after remount", async () => {
  await setupGlobals();
  const continuation = "99999999-9999-4999-8999-999999999999";
  const pendingRead = deferred<unknown>();
  const pendingOriginal = deferred<unknown>();
  let reads = 0;
  let originals = 0;
  let lists = 0;
  const { value, requests } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      list: () => {
        lists += 1;
        return listResponse();
      },
      read: () => {
        reads += 1;
        if (reads === 1)
          return {
            api_version: 1,
            result: {
              schema_id: "octop.enterprise.knowledge.v1",
              version: 1,
              operation_id: operationId,
              evidence_id: evidenceId,
              knowledge_id: "opaque-doc",
              status: "SUCCESS",
              content: "sibling text",
              truncated: true,
              continuation,
              source_version: null,
            },
          };
        if (reads === 2) return pendingRead.promise;
        return {
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: evidenceId,
            knowledge_id: "opaque-doc",
            status: "SUCCESS",
            content: "remounted fresh text",
            truncated: false,
            continuation: null,
            source_version: null,
          },
        };
      },
      original: () => {
        originals += 1;
        return pendingOriginal.promise;
      },
    },
  );
  const mounted = render(
    <EvidenceRenderer {...(value as unknown as PluginToolProps)} />,
  );
  expect(await screen.findByText("sibling text")).toBeTruthy();
  const originalButton = screen.getByRole("button", {
    name: "Check original availability",
  });
  const savedOriginal = savedClick(originalButton);
  const savedMore = savedClick(
    screen.getByRole("button", { name: "Load more" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  await waitFor(() => expect(reads).toBe(2));
  const readSignal = requests
    .filter(({ path }) => path.endsWith("/evidence.read"))
    .at(-1)?.init?.signal;
  savedOriginal();
  await screen.findByText("Checking original availability…");
  const requestCountBeforeDenial = requests.length;
  pendingOriginal.reject(
    new Error(
      'Request failed: 403 Forbidden - {"error":{"code":"ACCESS_DENIED"}}',
    ),
  );
  expect(
    await screen.findByText(
      "Access changed. Reopen this result to check authorization.",
    ),
  ).toBeTruthy();
  await waitFor(() => expect(readSignal?.aborted).toBe(true));
  expect(screen.queryByText("sibling text")).toBeNull();
  await act(async () => {
    pendingRead.resolve({
      api_version: 1,
      result: {
        schema_id: "octop.enterprise.knowledge.v1",
        version: 1,
        operation_id: operationId,
        evidence_id: evidenceId,
        knowledge_id: "opaque-doc",
        status: "SUCCESS",
        content: "late sibling success",
        truncated: false,
        continuation: null,
        source_version: null,
      },
    });
    await pendingRead.promise;
    await Promise.resolve();
  });
  expect(screen.queryByText("late sibling success")).toBeNull();
  expect(screen.queryByText("sibling text")).toBeNull();
  expect(screen.queryByText(/Registered title/)).toBeNull();
  expect(
    screen.getByText(
      "Access changed. Reopen this result to check authorization.",
    ),
  ).toBeTruthy();
  expect(requests).toHaveLength(requestCountBeforeDenial);
  savedMore();
  savedOriginal();
  await act(async () => Promise.resolve());
  expect(requests).toHaveLength(requestCountBeforeDenial);
  expect(reads).toBe(2);
  expect(originals).toBe(1);
  expect(lists).toBe(1);

  mounted.unmount();
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(await screen.findByText("remounted fresh text")).toBeTruthy();
  expect(lists).toBe(2);
  expect(reads).toBe(3);
  expect(requests.length).toBeGreaterThan(requestCountBeforeDenial);
});

it("renders Chinese read progress, retry, source-change restart, and fresh text states", async () => {
  await setupGlobals();
  target.locale = "zh-CN";
  const more = deferred<unknown>();
  let reads = 0;
  let lists = 0;
  const { value } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    {
      list: () => {
        lists += 1;
        return listResponse();
      },
      read: () => {
        reads += 1;
        if (reads === 1) throw new Error("network unavailable");
        if (reads === 2)
          return {
            api_version: 1,
            result: {
              schema_id: "octop.enterprise.knowledge.v1",
              version: 1,
              operation_id: operationId,
              evidence_id: evidenceId,
              knowledge_id: "opaque-doc",
              status: "SUCCESS",
              content: "保留文本",
              truncated: true,
              continuation: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
              source_version: null,
            },
          };
        if (reads === 3) return more.promise;
        return {
          api_version: 1,
          result: {
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation_id: operationId,
            evidence_id: evidenceId,
            knowledge_id: "opaque-doc",
            status: "SUCCESS",
            content: "新的文本",
            truncated: false,
            continuation: null,
            source_version: null,
          },
        };
      },
    },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  const loading = await screen.findByText("正在加载检索文本…");
  expect(loading.getAttribute("role")).toBe("status");
  expect(loading.getAttribute("aria-live")).toBe("polite");
  const failure = await screen.findByText("无法加载检索文本。");
  expect(failure.getAttribute("role")).toBe("status");
  expect(failure.getAttribute("aria-live")).toBe("polite");
  fireEvent.click(await screen.findByRole("button", { name: "重试阅读" }));
  expect(await screen.findByText("保留文本")).toBeTruthy();
  fireEvent.click(await screen.findByRole("button", { name: "加载更多" }));
  const moreLoading = await screen.findByText("正在加载更多…");
  expect(moreLoading.getAttribute("role")).toBe("status");
  expect(moreLoading.getAttribute("aria-live")).toBe("polite");
  more.reject(
    new Error(
      'Request failed: 409 Conflict - {"error":{"code":"READ_SOURCE_CHANGED"}}',
    ),
  );
  const sourceChanged = await screen.findByText(
    "来源已变化。请重新开始阅读以加载当前文本。",
  );
  expect(sourceChanged.getAttribute("role")).toBe("status");
  expect(sourceChanged.getAttribute("aria-live")).toBe("polite");
  expect(screen.getByText("保留文本")).toBeTruthy();
  fireEvent.click(await screen.findByRole("button", { name: "重新开始阅读" }));
  expect(await screen.findByText("新的文本")).toBeTruthy();
  expect(reads).toBe(4);
  expect(lists).toBe(2);
});

it("renders Chinese empty and original-unavailable states with live status announcements", async () => {
  await setupGlobals();
  target.locale = "zh-CN";
  const pendingOriginal = deferred<unknown>();
  const { value } = props(
    {
      presentation: {
        location: "side-panel",
        active: true,
        state: { operation_id: operationId, evidence_id: evidenceId },
      },
    },
    { original: () => pendingOriginal.promise },
  );
  render(<EvidenceRenderer {...(value as unknown as PluginToolProps)} />);
  expect(await screen.findByText("没有可用的检索文本。")).toBeTruthy();
  const emptyStatus = screen.getByText("没有可用的检索文本。");
  expect(emptyStatus.textContent).toBe("没有可用的检索文本。");
  const originalButton = await screen.findByRole("button", {
    name: "检查原文可用性",
  });
  fireEvent.click(originalButton);
  const checking = await screen.findByText("正在检查原文可用性…");
  expect(checking.getAttribute("role")).toBe("status");
  expect(checking.getAttribute("aria-live")).toBe("polite");
  pendingOriginal.resolve({
    api_version: 1,
    result: {
      schema_id: "octop.enterprise.knowledge.v1",
      version: 1,
      operation_id: operationId,
      evidence_id: evidenceId,
      available: false,
      reason: "CAPABILITY_UNVERIFIED",
      resource_token: null,
    },
  });
  const unavailable = await screen.findByText("原文不可用：能力尚未验证。");
  expect(unavailable.getAttribute("role")).toBe("status");
  expect(unavailable.getAttribute("aria-live")).toBe("polite");
});
