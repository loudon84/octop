import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";

afterEach(() => cleanup());

describe("standalone plugin UI package", () => {
  it("ships one self-contained ESM entry and only actual renderer registrations", async () => {
    const source = await readFile(
      resolve(process.cwd(), "dist/index.js"),
      "utf8",
    );
    const manifest = JSON.parse(
      await readFile(resolve(process.cwd(), "dist/manifest.json"), "utf8"),
    );
    expect(source).not.toMatch(/(?:from\s*|import\s*\()\s*["'](?:\.|react|@)/);
    expect(manifest).toEqual({
      renderers: [
        {
          id: "enterprise_knowledge_evidence",
          tools: ["enterprise_knowledge_search"],
        },
      ],
    });
    expect(source).toContain("setup");
  });

  it("registers the settings surface and returns its unregister disposer", async () => {
    Object.assign(window, {
      __OCTOP_REACT__: { Fragment: Symbol.for("react.fragment") },
      __OCTOP_JSX__: { jsx: vi.fn(), jsxs: vi.fn() },
    });
    const moduleUrl = pathToFileURL(resolve(process.cwd(), "dist/index.js"));
    const ui = await import(moduleUrl.href);
    const disposeSurface = vi.fn();
    const host = {
      registerRenderer: vi.fn(),
      registerSurface: vi.fn(() => disposeSurface),
    };
    const dispose = ui.setup(host);
    expect(host.registerRenderer).toHaveBeenCalledWith(
      expect.objectContaining({
        id: "enterprise_knowledge_evidence",
        tools: ["enterprise_knowledge_search"],
        component: expect.any(Function),
      }),
    );
    expect(host.registerSurface).toHaveBeenCalledWith(
      "settings",
      expect.any(Function),
    );
    expect(typeof dispose).toBe("function");
    dispose();
    expect(disposeSurface).toHaveBeenCalledTimes(1);
  });

  it("mounts the actual built setup renderer against the mounted action Host", async () => {
    const react = await import("react");
    const jsx = await import("react/jsx-runtime");
    Object.assign(window, {
      __OCTOP_REACT__: react,
      __OCTOP_JSX__: jsx,
      IS_REACT_ACT_ENVIRONMENT: true,
    });
    const ui = await import(
      `${
        pathToFileURL(resolve(process.cwd(), "dist/index.js")).href
      }?mounted-host`
    );
    let renderer: ((props: Record<string, unknown>) => unknown) | undefined;
    const requests: Array<{ path: string; init?: RequestInit }> = [];
    const host = {
      getToolContext: () => ({
        agentId: "built-agent",
        threadId: "built-thread",
        locale: "en",
        theme: "light",
      }),
      request: async <T = unknown>(
        path: string,
        init?: RequestInit,
      ): Promise<T> => {
        requests.push({ path, init });
        if (path.endsWith("/evidence.list"))
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
                agent_id: "built-agent",
                thread_id: "built-thread",
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
                  agent_id: "built-agent",
                  thread_id: "built-thread",
                  source_kind: "WEB",
                  connection_id: null,
                  kb_id: null,
                  knowledge_id: null,
                  title: "Built ESM title",
                  content: "Built ESM summary",
                  truncated: false,
                  retrieved_at: "2026-01-02T00:00:00Z",
                  source_version: null,
                  source_page: null,
                  locator: null,
                },
              ],
            },
          } as T;
        if (path === "/settings/timezone") return { timezone: "UTC" } as T;
        throw new Error(`Unexpected built renderer request ${path}`);
      },
      openSidePanel: vi.fn(),
      registerRenderer: (registration: {
        component: (props: Record<string, unknown>) => unknown;
      }) => {
        renderer = registration.component;
      },
      registerSurface: () => () => undefined,
    };
    ui.setup(host);
    expect(renderer).toBeDefined();
    const component = createElement(
      renderer! as React.ComponentType<Record<string, unknown>>,
      {
        pluginId: "enterprise-knowledge",
        toolName: "enterprise_knowledge_search",
        callId: "built-call",
        status: "done",
        args: {},
        data: {
          schema_id: "octop.enterprise.knowledge.v1",
          version: 1,
          operation_id: "11111111-1111-4111-8111-111111111111",
        },
        host,
        presentation: { location: "inline", active: true },
        isStreaming: false,
      },
    );
    render(component);
    await waitFor(() =>
      expect(requests.some(({ path }) => path.endsWith("/evidence.list"))).toBe(
        true,
      ),
    );
    expect(await screen.findByText("Built ESM summary")).toBeTruthy();
    const list = requests.find(({ path }) => path.endsWith("/evidence.list"));
    expect(list).toBeDefined();
    expect(JSON.parse(String(list?.init?.body))).toEqual({
      api_version: 1,
      context: { agent_id: "built-agent", thread_id: "built-thread" },
      payload: {
        schema_id: "octop.enterprise.knowledge.v1",
        version: 1,
        operation_id: "11111111-1111-4111-8111-111111111111",
      },
    });
  });
});
