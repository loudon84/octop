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
    fireEvent.click(
      screen.getByRole("button", { name: "Clear selection" }),
    );
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
        generation: "g2",
        signal: new AbortController().signal,
      }),
    ).rejects.toThrow(/Select at least one/);
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
          return wrap(
            {
              schema_id: "octop.enterprise.knowledge.v1",
              version: 1,
              session: session({
                mode: "WEB_ONLY",
                selected_kb_ids: [],
                connection_id: null,
                capabilities: {
                  can_save_selection: true,
                  can_select_knowledge: true,
                  knowledge_ready: false,
                  web_ready: true,
                },
                search_ready: true,
                search_revision: 4,
              }),
            },
          );
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
    document.body.appendChild(trigger);
    trigger.focus();
    const pending = handler!.prepare({
      selection: handler!.captureSelection(),
      target: { agentId: "expert-1", threadId: "thread-1" },
      text: "private internal prompt",
      attachments: [{ name: "secret.txt" }],
      generation: "g-public-cancel",
      signal: new AbortController().signal,
    });
    const cancel = await screen.findByRole("button", { name: "Cancel" });
    expect(document.activeElement).toBe(cancel);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
    fireEvent.click(cancel);
    await expect(pending).rejects.toThrow(/cancelled/);
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(
      host.request.mock.calls.some((call) =>
        String(call[0]).endsWith("query.approve"),
      ),
    ).toBe(false);
    trigger.remove();
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
              connection_id: null,
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
      generation: "g-public-approve",
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
