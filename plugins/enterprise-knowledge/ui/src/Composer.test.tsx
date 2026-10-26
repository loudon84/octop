import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ComposerPreparationHandler } from "./host";

async function setupGlobals() {
  const react = await import("react");
  const jsx = await import("react/jsx-runtime");
  Object.assign(window, { __OCTOP_REACT__: react, __OCTOP_JSX__: jsx });
}

const context = {
  agentId: "expert-1",
  threadId: "thread-1",
  locale: "en",
  theme: "light" as const,
  generation: "g1",
  timeZone: "Asia/Shanghai",
};

function session(overrides: Record<string, unknown> = {}) {
  return {
    schema_id: "octop.enterprise.knowledge.v1",
    version: 1,
    agent_id: "expert-1",
    thread_id: "thread-1",
    revision: 1,
    selection_present: true,
    selected_kb_ids: ["kb-a"],
    mode: "KB_ONLY",
    connection_id: "11111111-1111-4111-8111-111111111111",
    config_revision: 2,
    binding_revision: 1,
    grant_revision: 1,
    invalid: false,
    invalid_reason: null,
    capabilities: {
      can_save_selection: true,
      can_select_knowledge: true,
      knowledge_ready: true,
      web_ready: false,
    },
    search_ready: false,
    ...overrides,
  };
}

function wrap(result: unknown) {
  return { api_version: 1, result };
}

describe("Composer", () => {
  afterEach(() => cleanup());

  it("initializes revision-zero defaults once and prepares operation_id only", async () => {
    await setupGlobals();
    let handler: ComposerPreparationHandler | null = null;
    const host = {
      getContext: () => context,
      registerComposerPreparation: (next: ComposerPreparationHandler) => {
        handler = next;
        return () => {
          handler = null;
        };
      },
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path.endsWith("session.get")) {
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            session: session({
              revision: 0,
              selection_present: false,
              selected_kb_ids: ["kb-default"],
            }),
          });
        }
        if (path.endsWith("session.save")) {
          const body = JSON.parse(String(init?.body));
          expect(body.payload.expected_revision).toBe(0);
          expect(body.payload.mode).toBe("KB_ONLY");
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            session: session({
              revision: 1,
              selection_present: true,
              selected_kb_ids: ["kb-default"],
            }),
          });
        }
        if (path.endsWith("connection.catalog")) {
          return wrap({
            connection_id: "11111111-1111-4111-8111-111111111111",
            observed_revision: 2,
            knowledge_bases: [
              { kb_id: "kb-default", name: "Default KB" },
              { kb_id: "kb-b", name: "Other KB" },
            ],
          });
        }
        if (path.endsWith("operation.prepare")) {
          const body = JSON.parse(String(init?.body));
          expect(Object.keys(body.payload).sort()).toEqual([
            "expected_session_revision",
            "schema_id",
            "version",
          ]);
          expect(body.payload.question).toBeUndefined();
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation: {
              operation_id: "22222222-2222-4222-8222-222222222222",
              session_revision: 1,
              status: "READY",
            },
          });
        }
        throw new Error(path);
      }),
    };

    const { Composer } = await import("./Composer");
    render(<Composer host={host as never} context={context as never} />);
    await screen.findByText("Default KB");
    expect(handler).not.toBeNull();

    const selection = handler!.captureSelection();
    expect(selection.state).toBe("selected");
    const prepared = await handler!.prepare({
      selection,
      target: { agentId: "expert-1", threadId: "thread-1" },
      text: "what is policy?",
      attachments: [],
      generation: "g1",
      signal: new AbortController().signal,
    });
    expect(prepared).toEqual({
      operation_id: "22222222-2222-4222-8222-222222222222",
    });
  });

  it("clearing selection blocks prepare and keeps empty save", async () => {
    await setupGlobals();
    let handler: ComposerPreparationHandler | null = null;
    const host = {
      getContext: () => context,
      registerComposerPreparation: (next: ComposerPreparationHandler) => {
        handler = next;
        return () => undefined;
      },
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path.endsWith("session.get")) {
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            session: session(),
          });
        }
        if (path.endsWith("connection.catalog")) {
          return wrap({
            connection_id: "11111111-1111-4111-8111-111111111111",
            observed_revision: 2,
            knowledge_bases: [{ kb_id: "kb-a", name: "Alpha" }],
          });
        }
        if (path.endsWith("session.save")) {
          const body = JSON.parse(String(init?.body));
          expect(body.payload.selected_kb_ids).toEqual([]);
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            session: session({
              revision: 2,
              selected_kb_ids: [],
              selection_present: true,
            }),
          });
        }
        throw new Error(path);
      }),
    };
    const { Composer } = await import("./Composer");
    render(<Composer host={host as never} context={context as never} />);
    await screen.findByText("Alpha");
    fireEvent.click(screen.getByRole("button", { name: "Clear selection" }));
    await waitFor(() =>
      expect(
        host.request.mock.calls.some((call) =>
          String(call[0]).endsWith("session.save"),
        ),
      ).toBe(true),
    );
    expect(
      await screen.findAllByText(
        "Select at least one knowledge base before sending a knowledge query.",
      ),
    ).not.toHaveLength(0);
    await expect(
      handler!.prepare({
        selection: handler!.captureSelection(),
        target: { agentId: "expert-1", threadId: "thread-1" },
        text: "q",
        attachments: [],
        generation: "g1",
        signal: new AbortController().signal,
      }),
    ).rejects.toThrow(/Select at least one/);
  });

  it("blocks prepare when the live knowledge selection has not been saved", async () => {
    await setupGlobals();
    let handler: ComposerPreparationHandler | null = null;
    const host = {
      getContext: () => context,
      registerComposerPreparation: (next: ComposerPreparationHandler) => {
        handler = next;
        return () => undefined;
      },
      request: vi.fn(async (path: string) => {
        if (path.endsWith("session.get"))
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            session: session(),
          });
        if (path.endsWith("connection.catalog"))
          return wrap({
            connection_id: "11111111-1111-4111-8111-111111111111",
            observed_revision: 2,
            knowledge_bases: [
              { kb_id: "kb-a", name: "Alpha" },
              { kb_id: "kb-b", name: "Beta" },
            ],
          });
        if (path.endsWith("operation.prepare"))
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation: {
              operation_id: "22222222-2222-4222-8222-222222222222",
              session_revision: 1,
              status: "READY",
            },
          });
        throw new Error(path);
      }),
    };
    const { Composer } = await import("./Composer");
    render(<Composer host={host as never} context={context as never} />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "Beta" }));
    await expect(
      handler!.prepare({
        selection: handler!.captureSelection(),
        target: { agentId: "expert-1", threadId: "thread-1" },
        text: "q",
        attachments: [],
        generation: "g1",
        signal: new AbortController().signal,
      }),
    ).rejects.toThrow(/wait for the current selection to save/i);
    expect(
      host.request.mock.calls.some((call) =>
        String(call[0]).endsWith("operation.prepare"),
      ),
    ).toBe(false);
  });

  it("shows all server-backed modes and keeps unavailable choices disabled", async () => {
    await setupGlobals();
    const host = {
      getContext: () => context,
      registerComposerPreparation: () => () => undefined,
      request: vi.fn(async (path: string) => {
        if (path.endsWith("session.get")) {
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            session: session(),
          });
        }
        if (path.endsWith("connection.catalog")) {
          return wrap({
            connection_id: "11111111-1111-4111-8111-111111111111",
            observed_revision: 2,
            knowledge_bases: [{ kb_id: "kb-a", name: "Alpha" }],
          });
        }
        throw new Error(path);
      }),
    };
    const { Composer } = await import("./Composer");
    render(<Composer host={host as never} context={context as never} />);

    const knowledge = (await screen.findByRole("radio", {
      name: "Knowledge only",
    })) as HTMLInputElement;
    const web = screen.getByRole("radio", {
      name: "Public web only",
    }) as HTMLInputElement;
    const hybrid = screen.getByRole("radio", {
      name: "Knowledge + public web",
    }) as HTMLInputElement;
    expect(knowledge.checked).toBe(true);
    expect(knowledge.disabled).toBe(false);
    expect(web.disabled).toBe(true);
    expect(hybrid.disabled).toBe(true);
    expect(screen.getAllByText("Public web search is not ready.").length).toBe(
      1,
    );
  });

  it("does not load a knowledge catalog for a WEB_ONLY session with a connection", async () => {
    await setupGlobals();
    const host = {
      getContext: () => context,
      registerComposerPreparation: () => () => undefined,
      request: vi.fn(async (path: string) => {
        if (path.endsWith("session.get")) {
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            session: session({
              mode: "WEB_ONLY",
              connection_id: "11111111-1111-4111-8111-111111111111",
              capabilities: {
                can_save_selection: true,
                can_select_knowledge: true,
                knowledge_ready: false,
                web_ready: true,
              },
              search_ready: true,
              search_revision: 4,
            }),
          });
        }
        throw new Error(path);
      }),
    };
    const { Composer } = await import("./Composer");
    render(<Composer host={host as never} context={context as never} />);
    await screen.findByRole("radio", { name: "Public web only" });
    expect(
      host.request.mock.calls.some((call) =>
        String(call[0]).endsWith("connection.catalog"),
      ),
    ).toBe(false);
  });

  it("keeps the selection draft and avoids reloads for equivalent context objects", async () => {
    await setupGlobals();
    let liveContext = { ...context };
    const host = {
      getContext: () => liveContext,
      request: vi.fn(async (path: string) => {
        if (path.endsWith("session.get"))
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            session: session(),
          });
        if (path.endsWith("connection.catalog"))
          return wrap({
            connection_id: "11111111-1111-4111-8111-111111111111",
            observed_revision: 2,
            knowledge_bases: [
              { kb_id: "kb-a", name: "KB A" },
              { kb_id: "kb-b", name: "KB B" },
            ],
          });
        throw new Error(path);
      }),
    };
    const { Composer } = await import("./Composer");
    const view = render(
      <Composer host={host as never} context={context as never} />,
    );
    await screen.findByText("KB B");
    fireEvent.click(screen.getByLabelText("KB B"));

    for (let index = 0; index < 5; index += 1) {
      view.rerender(
        <Composer host={host as never} context={{ ...context } as never} />,
      );
    }

    expect(
      host.request.mock.calls.filter(([path]) =>
        String(path).endsWith("session.get"),
      ),
    ).toHaveLength(1);
    expect(
      host.request.mock.calls.filter(([path]) =>
        String(path).endsWith("connection.catalog"),
      ),
    ).toHaveLength(1);
    expect((screen.getByLabelText("KB B") as HTMLInputElement).checked).toBe(
      true,
    );

    liveContext = { ...context, generation: "g2" };
    view.rerender(
      <Composer host={host as never} context={liveContext as never} />,
    );
    await waitFor(() => {
      expect(
        host.request.mock.calls.filter(([path]) =>
          String(path).endsWith("session.get"),
        ),
      ).toHaveLength(2);
      expect(
        host.request.mock.calls.filter(([path]) =>
          String(path).endsWith("connection.catalog"),
        ),
      ).toHaveLength(2);
    });
  });

  it("initializes an unbound capture against the resolved native target before preparing", async () => {
    await setupGlobals();
    let handler: ComposerPreparationHandler | null = null;
    const liveContext = { ...context, threadId: null };
    const host = {
      getContext: () => liveContext,
      registerComposerPreparation: (next: ComposerPreparationHandler) => {
        handler = next;
        return () => undefined;
      },
      request: vi.fn(async (path: string, init?: RequestInit) => {
        const body = JSON.parse(String(init?.body));
        if (path.endsWith("session.get")) {
          expect(body.context).toEqual({
            agent_id: "expert-1",
            thread_id: "thread-native",
          });
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            session: session({
              thread_id: "thread-native",
              revision: 0,
              selection_present: false,
              selected_kb_ids: ["kb-default"],
            }),
          });
        }
        if (path.endsWith("session.save")) {
          expect(body.context.thread_id).toBe("thread-native");
          expect(body.payload.expected_revision).toBe(0);
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            session: session({
              thread_id: "thread-native",
              revision: 1,
              selection_present: true,
              selected_kb_ids: ["kb-default"],
            }),
          });
        }
        if (path.endsWith("connection.catalog"))
          return wrap({
            connection_id: "11111111-1111-4111-8111-111111111111",
            observed_revision: 2,
            knowledge_bases: [{ kb_id: "kb-default", name: "Default KB" }],
          });
        if (path.endsWith("operation.prepare")) {
          expect(body.context.thread_id).toBe("thread-native");
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation: {
              operation_id: "22222222-2222-4222-8222-222222222222",
              session_revision: 1,
              status: "READY",
            },
          });
        }
        throw new Error(path);
      }),
    };
    const { Composer } = await import("./Composer");
    render(<Composer host={host as never} context={liveContext as never} />);
    expect(handler!.captureSelection().state).toBe("uninitialized");
    const result = await handler!.prepare({
      selection: handler!.captureSelection(),
      target: { agentId: "expert-1", threadId: "thread-native" },
      text: "question",
      attachments: [],
      generation: "g1",
      signal: new AbortController().signal,
    });
    expect(result.operation_id).toBe("22222222-2222-4222-8222-222222222222");
  });

  it("re-reads a revision-zero winner without restoring its saved empty selection", async () => {
    await setupGlobals();
    let handler: ComposerPreparationHandler | null = null;
    let reads = 0;
    let saves = 0;
    const host = {
      getContext: () => context,
      registerComposerPreparation: (next: ComposerPreparationHandler) => {
        handler = next;
        return () => undefined;
      },
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path.endsWith("session.get")) {
          reads += 1;
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            session: session({
              revision: reads === 1 ? 0 : 1,
              selection_present: reads !== 1,
              selected_kb_ids: reads === 1 ? ["kb-default"] : [],
            }),
          });
        }
        if (path.endsWith("session.save")) {
          saves += 1;
          expect(JSON.parse(String(init?.body)).payload.expected_revision).toBe(
            0,
          );
          throw new Error(
            'Request failed: 409 - {"error":{"code":"CONFIG_CONFLICT"}}',
          );
        }
        throw new Error(path);
      }),
    };
    const { Composer } = await import("./Composer");
    render(<Composer host={host as never} context={context as never} />);
    await screen.findByRole("radio", { name: "Knowledge only" });
    expect(saves).toBe(1);
    expect(reads).toBe(2);
    await expect(
      handler!.prepare({
        selection: handler!.captureSelection(),
        target: { agentId: "expert-1", threadId: "thread-1" },
        text: "question",
        attachments: [],
        generation: "g1",
        signal: new AbortController().signal,
      }),
    ).rejects.toThrow(/Select at least one/);
    expect(saves).toBe(1);
  });

  it("loads the catalog only after switching from WEB_ONLY to KB_ONLY", async () => {
    await setupGlobals();
    let handler: ComposerPreparationHandler | null = null;
    const host = {
      getContext: () => context,
      registerComposerPreparation: (next: ComposerPreparationHandler) => {
        handler = next;
        return () => undefined;
      },
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path.endsWith("session.get"))
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            session: session({
              mode: "WEB_ONLY",
              selected_kb_ids: [],
              capabilities: {
                can_save_selection: true,
                can_select_knowledge: true,
                knowledge_ready: false,
                web_ready: true,
              },
              search_ready: true,
            }),
          });
        if (path.endsWith("session.save")) {
          const payload = JSON.parse(String(init?.body)).payload;
          expect(payload.mode).toBe("KB_ONLY");
          expect(payload.selected_kb_ids).toEqual([]);
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            session: session({
              revision: 2,
              mode: "KB_ONLY",
              selected_kb_ids: [],
              capabilities: {
                can_save_selection: true,
                can_select_knowledge: true,
                knowledge_ready: false,
                web_ready: true,
              },
            }),
          });
        }
        if (path.endsWith("connection.catalog"))
          return wrap({
            connection_id: "11111111-1111-4111-8111-111111111111",
            observed_revision: 2,
            knowledge_bases: [{ kb_id: "kb-a", name: "Alpha" }],
          });
        throw new Error(path);
      }),
    };
    const { Composer } = await import("./Composer");
    render(<Composer host={host as never} context={context as never} />);
    const kbMode = await screen.findByRole("radio", { name: "Knowledge only" });
    expect(
      host.request.mock.calls.some((call) =>
        String(call[0]).endsWith("connection.catalog"),
      ),
    ).toBe(false);
    expect((kbMode as HTMLInputElement).disabled).toBe(false);
    fireEvent.click(kbMode);
    await screen.findByText("Alpha");
    await waitFor(() =>
      expect((kbMode as HTMLInputElement).disabled).toBe(false),
    );
    expect(
      host.request.mock.calls.filter((call) =>
        String(call[0]).endsWith("connection.catalog"),
      ),
    ).toHaveLength(1);
    expect(
      (screen.getByRole("checkbox", { name: "Alpha" }) as HTMLInputElement)
        .checked,
    ).toBe(false);
    await expect(
      handler!.prepare({
        selection: handler!.captureSelection(),
        target: { agentId: "expert-1", threadId: "thread-1" },
        text: "q",
        attachments: [],
        generation: "g1",
        signal: new AbortController().signal,
      }),
    ).rejects.toThrow(/access is not ready/i);
    expect(
      host.request.mock.calls.some((call) =>
        String(call[0]).endsWith("operation.prepare"),
      ),
    ).toBe(false);
  });

  it("keeps a newer selection draft when the uninitialized read returns late", async () => {
    await setupGlobals();
    let handler: ComposerPreparationHandler | null = null;
    let resolveRefresh!: (value: unknown) => void;
    let resolvePreparation!: (value: unknown) => void;
    const refreshResponse = new Promise((resolve) => {
      resolveRefresh = resolve;
    });
    const preparationResponse = new Promise((resolve) => {
      resolvePreparation = resolve;
    });
    let sessionReads = 0;
    const host = {
      getContext: () => context,
      registerComposerPreparation: (next: ComposerPreparationHandler) => {
        handler = next;
        return () => undefined;
      },
      request: vi.fn(async (path: string) => {
        if (path.endsWith("session.get")) {
          sessionReads += 1;
          return sessionReads === 1 ? refreshResponse : preparationResponse;
        }
        if (path.endsWith("connection.catalog"))
          return wrap({
            connection_id: "11111111-1111-4111-8111-111111111111",
            observed_revision: 2,
            knowledge_bases: [
              { kb_id: "kb-a", name: "Alpha" },
              { kb_id: "kb-b", name: "Beta" },
            ],
          });
        if (path.endsWith("operation.prepare"))
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation: {
              operation_id: "22222222-2222-4222-8222-222222222222",
              session_revision: 1,
              status: "READY",
            },
          });
        throw new Error(path);
      }),
    };
    const { Composer } = await import("./Composer");
    render(<Composer host={host as never} context={context as never} />);
    await waitFor(() => expect(sessionReads).toBe(1));
    const pending = handler!.prepare({
      selection: handler!.captureSelection(),
      target: { agentId: "expert-1", threadId: "thread-1" },
      text: "q",
      attachments: [],
      generation: "g1",
      signal: new AbortController().signal,
    });
    await waitFor(() => expect(sessionReads).toBe(2));
    resolveRefresh(
      wrap({
        schema_id: "octop.enterprise.knowledge.v1",
        version: 1,
        session: session({ selected_kb_ids: ["kb-a"] }),
      }),
    );
    fireEvent.click(await screen.findByRole("checkbox", { name: "Beta" }));
    expect(
      (screen.getByRole("checkbox", { name: "Beta" }) as HTMLInputElement)
        .checked,
    ).toBe(true);
    resolvePreparation(
      wrap({
        schema_id: "octop.enterprise.knowledge.v1",
        version: 1,
        session: session({ selected_kb_ids: ["kb-a"] }),
      }),
    );
    await expect(pending).rejects.toThrow(/selection to save/i);
    expect(
      (screen.getByRole("checkbox", { name: "Beta" }) as HTMLInputElement)
        .checked,
    ).toBe(true);
    expect(
      host.request.mock.calls.some((call) =>
        String(call[0]).endsWith("operation.prepare"),
      ),
    ).toBe(false);
  });

  it("keeps a draft when the initial refresh returns after preparation initialized the composer", async () => {
    await setupGlobals();
    let handler: ComposerPreparationHandler | null = null;
    let resolveRefresh!: (value: unknown) => void;
    let resolvePreparation!: (value: unknown) => void;
    const refreshResponse = new Promise((resolve) => {
      resolveRefresh = resolve;
    });
    const preparationResponse = new Promise((resolve) => {
      resolvePreparation = resolve;
    });
    let sessionReads = 0;
    const host = {
      getContext: () => context,
      registerComposerPreparation: (next: ComposerPreparationHandler) => {
        handler = next;
        return () => undefined;
      },
      request: vi.fn(async (path: string) => {
        if (path.endsWith("session.get")) {
          sessionReads += 1;
          return sessionReads === 1 ? refreshResponse : preparationResponse;
        }
        if (path.endsWith("connection.catalog"))
          return wrap({
            connection_id: "11111111-1111-4111-8111-111111111111",
            observed_revision: 1,
            knowledge_bases: [
              { kb_id: "kb-a", name: "Alpha" },
              { kb_id: "kb-b", name: "Beta" },
            ],
          });
        if (path.endsWith("operation.prepare"))
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation: {
              operation_id: "22222222-2222-4222-8222-222222222222",
              session_revision: 1,
              status: "READY",
            },
          });
        throw new Error(path);
      }),
    };
    const { Composer } = await import("./Composer");
    render(<Composer host={host as never} context={context as never} />);
    await waitFor(() => expect(sessionReads).toBe(1));
    const pending = handler!.prepare({
      selection: handler!.captureSelection(),
      target: { agentId: "expert-1", threadId: "thread-1" },
      text: "q",
      attachments: [],
      generation: "g1",
      signal: new AbortController().signal,
    });
    await waitFor(() => expect(sessionReads).toBe(2));
    resolvePreparation(
      wrap({
        schema_id: "octop.enterprise.knowledge.v1",
        version: 1,
        session: session({ selected_kb_ids: ["kb-a", "kb-b"] }),
      }),
    );
    await expect(pending).resolves.toEqual({
      operation_id: "22222222-2222-4222-8222-222222222222",
    });
    const beta = await screen.findByRole("checkbox", { name: "kb-b" });
    fireEvent.click(beta);
    expect((beta as HTMLInputElement).checked).toBe(false);

    resolveRefresh(
      wrap({
        schema_id: "octop.enterprise.knowledge.v1",
        version: 1,
        session: session({ selected_kb_ids: ["kb-a", "kb-b"] }),
      }),
    );
    const loadedBeta = await screen.findByRole("checkbox", { name: "Beta" });
    expect((loadedBeta as HTMLInputElement).checked).toBe(false);
    await expect(
      handler!.prepare({
        selection: handler!.captureSelection(),
        target: { agentId: "expert-1", threadId: "thread-1" },
        text: "q",
        attachments: [],
        generation: "g1",
        signal: new AbortController().signal,
      }),
    ).rejects.toThrow(/selection to save/i);
    expect(
      host.request.mock.calls.filter((call) =>
        String(call[0]).endsWith("operation.prepare"),
      ),
    ).toHaveLength(1);
  });

  it("discards a late session response after abort or generation change", async () => {
    await setupGlobals();
    let handler: ComposerPreparationHandler | null = null;
    let resolveSession!: (value: unknown) => void;
    const pendingSession = new Promise((resolve) => {
      resolveSession = resolve;
    });
    const liveContext = { ...context, threadId: null };
    const host = {
      getContext: () => liveContext,
      registerComposerPreparation: (next: ComposerPreparationHandler) => {
        handler = next;
        return () => undefined;
      },
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path.endsWith("session.get")) {
          expect(init?.signal).toBe(abort.signal);
          return pendingSession;
        }
        throw new Error(path);
      }),
    };
    const { Composer } = await import("./Composer");
    render(
      <Composer
        host={host as never}
        context={{ ...context, threadId: null } as never}
      />,
    );
    const abort = new AbortController();
    const pending = handler!.prepare({
      selection: handler!.captureSelection(),
      target: { agentId: "expert-1", threadId: "thread-real" },
      text: "q",
      attachments: [],
      generation: "g1",
      signal: abort.signal,
    });
    abort.abort();
    resolveSession(
      wrap({
        schema_id: "octop.enterprise.knowledge.v1",
        version: 1,
        session: session({
          thread_id: "thread-real",
          selection_present: false,
        }),
      }),
    );
    await expect(pending).rejects.toThrow(/cancelled/i);
    expect(
      host.request.mock.calls.some(
        (call) =>
          String(call[0]).endsWith("session.save") ||
          String(call[0]).endsWith("operation.prepare"),
      ),
    ).toBe(false);
  });

  it("discards an old target response after the active thread changes", async () => {
    await setupGlobals();
    let handler: ComposerPreparationHandler | null = null;
    let resolveSession!: (value: unknown) => void;
    const pendingSession = new Promise((resolve) => {
      resolveSession = resolve;
    });
    let liveContext: Omit<typeof context, "threadId"> & {
      threadId: string | null;
    } = { ...context, threadId: null };
    const host = {
      getContext: () => liveContext,
      registerComposerPreparation: (next: ComposerPreparationHandler) => {
        handler = next;
        return () => undefined;
      },
      request: vi.fn(async (path: string) => {
        if (path.endsWith("session.get")) return pendingSession;
        throw new Error(path);
      }),
    };
    const { Composer } = await import("./Composer");
    render(
      <Composer
        host={host as never}
        context={{ ...context, threadId: null } as never}
      />,
    );
    const pending = handler!.prepare({
      selection: handler!.captureSelection(),
      target: { agentId: "expert-1", threadId: "thread-old" },
      text: "q",
      attachments: [],
      generation: "g1",
      signal: new AbortController().signal,
    });
    liveContext = { ...context, generation: "g2", threadId: "thread-new" };
    resolveSession(
      wrap({
        schema_id: "octop.enterprise.knowledge.v1",
        version: 1,
        session: session({ thread_id: "thread-old", selection_present: false }),
      }),
    );
    await expect(pending).rejects.toThrow(/context changed/i);
    expect(
      host.request.mock.calls.some(
        (call) =>
          String(call[0]).endsWith("session.save") ||
          String(call[0]).endsWith("operation.prepare"),
      ),
    ).toBe(false);
  });

  it("rejects a resolved target from another agent and an aborted preparation", async () => {
    await setupGlobals();
    let handler: ComposerPreparationHandler | null = null;
    const host = {
      getContext: () => ({ ...context, threadId: null }),
      registerComposerPreparation: (next: ComposerPreparationHandler) => {
        handler = next;
        return () => undefined;
      },
      request: vi.fn(async () => {
        throw new Error("unexpected request");
      }),
    };
    const { Composer } = await import("./Composer");
    render(
      <Composer
        host={host as never}
        context={{ ...context, threadId: null } as never}
      />,
    );
    await expect(
      handler!.prepare({
        selection: handler!.captureSelection(),
        target: { agentId: "other-agent", threadId: "thread-real" },
        text: "q",
        attachments: [],
        generation: "g1",
        signal: new AbortController().signal,
      }),
    ).rejects.toThrow();
    await expect(
      handler!.prepare({
        selection: handler!.captureSelection(),
        target: { agentId: "expert-1", threadId: "thread-real" },
        text: "q",
        attachments: [],
        generation: "stale-generation",
        signal: new AbortController().signal,
      }),
    ).rejects.toThrow(/context changed/i);
    const abort = new AbortController();
    abort.abort();
    await expect(
      handler!.prepare({
        selection: handler!.captureSelection(),
        target: { agentId: "expert-1", threadId: "thread-real" },
        text: "q",
        attachments: [],
        generation: "g1",
        signal: abort.signal,
      }),
    ).rejects.toThrow(/cancelled/i);
    expect(host.request).not.toHaveBeenCalled();
  });

  it("cancels public approval without approving and restores focus", async () => {
    await setupGlobals();
    let handler: ComposerPreparationHandler | null = null;
    const host = {
      getContext: () => context,
      registerComposerPreparation: (next: ComposerPreparationHandler) => {
        handler = next;
        return () => undefined;
      },
      request: vi.fn(async (path: string) => {
        if (path.endsWith("session.get")) {
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            session: session({
              mode: "WEB_ONLY",
              selected_kb_ids: [],
              connection_id: "11111111-1111-4111-8111-111111111111",
              capabilities: {
                can_save_selection: true,
                can_select_knowledge: true,
                knowledge_ready: false,
                web_ready: true,
              },
              search_ready: true,
              search_revision: 4,
            }),
          });
        }
        if (path.endsWith("operation.prepare")) {
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation: {
              operation_id: "22222222-2222-4222-8222-222222222222",
              session_revision: 1,
              status: "VALIDATING",
            },
          });
        }
        throw new Error(path);
      }),
    };
    const { Composer } = await import("./Composer");
    render(<Composer host={host as never} context={context as never} />);
    await screen.findByRole("radio", { name: "Public web only" });

    const trigger = document.createElement("button");
    const blurTarget = document.createElement("button");
    document.body.appendChild(trigger);
    document.body.appendChild(blurTarget);
    trigger.focus();
    const selection = handler!.captureSelection();
    trigger.disabled = true;
    blurTarget.focus();
    const pending = handler!.prepare({
      selection,
      target: { agentId: "expert-1", threadId: "thread-1" },
      text: "private internal prompt",
      attachments: [{ name: "secret.txt" }],
      generation: "g1",
      signal: new AbortController().signal,
    });
    const cancel = await screen.findByRole("button", { name: "Cancel" });
    expect(document.activeElement).toBe(cancel);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
    fireEvent.click(cancel);
    trigger.disabled = false;
    await expect(pending).rejects.toThrow(/cancelled/);
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(
      host.request.mock.calls.some((call) =>
        String(call[0]).endsWith("query.approve"),
      ),
    ).toBe(false);
    expect(
      host.request.mock.calls.some((call) =>
        String(call[0]).endsWith("connection.catalog"),
      ),
    ).toBe(false);

    document.body.tabIndex = -1;
    document.body.focus();
    expect(document.activeElement).toBe(document.body);
    const noTriggerSelection = handler!.captureSelection();
    const nextPending = handler!.prepare({
      selection: noTriggerSelection,
      target: { agentId: "expert-1", threadId: "thread-1" },
      text: "another private prompt",
      attachments: [],
      generation: "g1",
      signal: new AbortController().signal,
    });
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await expect(nextPending).rejects.toThrow(/cancelled/);
    await new Promise((resolve) => window.requestAnimationFrame(resolve));
    expect(document.activeElement).not.toBe(trigger);
    trigger.remove();
    blurTarget.remove();
  });

  it("approves the exact public query once before returning the operation", async () => {
    await setupGlobals();
    let handler: ComposerPreparationHandler | null = null;
    let resolveApproval!: (value: unknown) => void;
    const approval = new Promise((resolve) => {
      resolveApproval = resolve;
    });
    const host = {
      getContext: () => context,
      registerComposerPreparation: (next: ComposerPreparationHandler) => {
        handler = next;
        return () => undefined;
      },
      request: vi.fn(async (path: string, init?: RequestInit) => {
        if (path.endsWith("session.get")) {
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            session: session({
              mode: "WEB_ONLY",
              selected_kb_ids: [],
              connection_id: "11111111-1111-4111-8111-111111111111",
              capabilities: {
                can_save_selection: true,
                can_select_knowledge: true,
                knowledge_ready: false,
                web_ready: true,
              },
              search_ready: true,
              search_revision: 4,
            }),
          });
        }
        if (path.endsWith("operation.prepare")) {
          return wrap({
            schema_id: "octop.enterprise.knowledge.v1",
            version: 1,
            operation: {
              operation_id: "22222222-2222-4222-8222-222222222222",
              session_revision: 1,
              status: "VALIDATING",
            },
          });
        }
        if (path.endsWith("query.approve")) {
          const body = JSON.parse(String(init?.body));
          expect(body.payload.public_query).toBe("  exact public query  ");
          return await approval;
        }
        throw new Error(path);
      }),
    };
    const { Composer } = await import("./Composer");
    render(<Composer host={host as never} context={context as never} />);
    await screen.findByRole("radio", { name: "Public web only" });

    const pending = handler!.prepare({
      selection: handler!.captureSelection(),
      target: { agentId: "expert-1", threadId: "thread-1" },
      text: "must not become the public query",
      attachments: [{ name: "must-not-leak.txt" }],
      generation: "g1",
      signal: new AbortController().signal,
    });
    fireEvent.change(await screen.findByRole("textbox"), {
      target: { value: "  exact public query  " },
    });
    const confirm = screen.getByRole("button", {
      name: "Approve and continue",
    });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    expect(
      host.request.mock.calls.filter((call) =>
        String(call[0]).endsWith("query.approve"),
      ),
    ).toHaveLength(1);
    resolveApproval(
      wrap({
        approval: {
          approval_id: "33333333-3333-4333-8333-333333333333",
          operation_id: "22222222-2222-4222-8222-222222222222",
          public_query: "  exact public query  ",
          state: "PENDING",
        },
      }),
    );
    await expect(pending).resolves.toEqual({
      operation_id: "22222222-2222-4222-8222-222222222222",
    });
  });
});
