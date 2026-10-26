import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

async function setupGlobals() {
  const react = await import("react");
  const jsx = await import("react/jsx-runtime");
  Object.assign(window, { __OCTOP_REACT__: react, __OCTOP_JSX__: jsx });
}

const context = {
  agentId: "expert-1",
  threadId: null,
  locale: "en",
  theme: "light" as const,
  generation: "g1",
  timeZone: "Asia/Shanghai",
};

function adminSettings(revision = 1) {
  return {
    revision,
    recovery_blocked: false,
    primary: {
      role: "PRIMARY",
      provider: "SEARXNG",
      base_url: "https://searx.example",
      ca_bundle_configured: true,
      credential_configured: false,
      reported_version: "1.0",
      origin_relation: "UNKNOWN",
    },
    secondary: {
      role: "SECONDARY",
      provider: "FIRECRAWL",
      base_url: "https://firecrawl.example",
      ca_bundle_configured: false,
      credential_configured: true,
      reported_version: null,
      origin_relation: "UNKNOWN",
    },
    primary_ready: true,
    secondary_ready: true,
    web_search_ready: true,
    diagnostics: [],
  };
}

function actionResult<T>(result: T) {
  return { api_version: 1, result };
}

describe("SearchSettings", () => {
  afterEach(() => cleanup());

  it("loads admin search settings and saves with KEEP defaults", async () => {
    await setupGlobals();
    const host = {
      getContext: () => context,
      request: vi
        .fn()
        .mockResolvedValueOnce(actionResult(adminSettings()))
        .mockResolvedValueOnce(actionResult(adminSettings(2))),
    };
    const { SearchSettings } = await import("./SearchSettings");
    render(
      <SearchSettings host={host as never} context={context as never} />,
    );

    await screen.findByText("Search sources");
    expect(host.request).toHaveBeenCalledWith(
      "/plugins/enterprise-knowledge/actions/search.get",
      expect.objectContaining({ method: "POST" }),
    );

    fireEvent.click(screen.getByRole("button", { name: "Save search sources" }));
    await waitFor(() => expect(host.request).toHaveBeenCalledTimes(2));
    const saveCall = host.request.mock.calls[1];
    const body = JSON.parse(String(saveCall[1].body));
    expect(body.payload.expected_revision).toBe(1);
    expect(body.payload.primary.ca_op).toBe("KEEP");
    expect(body.payload.primary.ca_bundle).toBeUndefined();
    expect(body.payload.secondary.credential_op).toBe("KEEP");
    expect(body.payload.secondary.credential).toBeUndefined();
  });

  it("requires confirmed public query before diagnosis and keeps late result out", async () => {
    await setupGlobals();
    let resolveTest: ((value: unknown) => void) | undefined;
    const host = {
      getContext: () => context,
      request: vi.fn(async (path: string) => {
        if (path.endsWith("search.get")) {
          return actionResult(adminSettings(3));
        }
        if (path.endsWith("search.test")) {
          return await new Promise((resolve) => {
            resolveTest = resolve;
          });
        }
        if (path.endsWith("search.save")) {
          return actionResult(adminSettings(4));
        }
        throw new Error(`unexpected ${path}`);
      }),
    };
    const { SearchSettings } = await import("./SearchSettings");
    render(
      <SearchSettings host={host as never} context={context as never} />,
    );
    await screen.findByText("Search sources");

    const run = screen.getByRole("button", {
      name: "Run diagnosis",
    }) as HTMLButtonElement;
    expect(run.disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("Public test query"), {
      target: { value: " open weather " },
    });
    fireEvent.click(
      screen.getByLabelText(
        "I confirm this exact public query will be sent to the selected source.",
      ),
    );
    expect(run.disabled).toBe(false);
    fireEvent.click(run);

    await waitFor(() =>
      expect(
        host.request.mock.calls.some((call) =>
          String(call[0]).endsWith("search.test"),
        ),
      ).toBe(true),
    );

    // Save bumps revision while diagnosis is in flight.
    fireEvent.click(screen.getByRole("button", { name: "Save search sources" }));
    await waitFor(() =>
      expect(
        host.request.mock.calls.some((call) =>
          String(call[0]).endsWith("search.save"),
        ),
      ).toBe(true),
    );

    resolveTest?.(
      actionResult({
        settings: adminSettings(3),
        diagnostic: {
          source_role: "PRIMARY",
          status: "SUCCESS",
          code: null,
          latency_ms: 12,
          hit_count: 1,
          results_truncated: false,
          observed_revision: 3,
          tested_at: "2026-10-26T08:00:00Z",
        },
      }),
    );

    await screen.findByText(
      "A newer configuration was saved; the late diagnosis was discarded.",
    );
    expect(screen.queryByText(/Latest diagnosis: SUCCESS/)).toBeNull();
  });

  it("blocks save while recovery_blocked and offers recover", async () => {
    await setupGlobals();
    const blocked = {
      ...adminSettings(5),
      recovery_blocked: true,
      primary_ready: false,
      secondary_ready: false,
      web_search_ready: false,
    };
    const host = {
      getContext: () => context,
      request: vi
        .fn()
        .mockResolvedValueOnce(actionResult(blocked))
        .mockResolvedValueOnce(actionResult(adminSettings(5))),
    };
    const { SearchSettings } = await import("./SearchSettings");
    render(
      <SearchSettings host={host as never} context={context as never} />,
    );
    await screen.findByText(/Credential recovery is blocked/);
    expect(
      (
        screen.getByRole("button", {
          name: "Save search sources",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);

    fireEvent.click(
      screen.getByRole("button", {
        name: "Recover blocked credential staging",
      }),
    );
    await waitFor(() => expect(host.request).toHaveBeenCalledTimes(2));
    expect(String(host.request.mock.calls[1][0])).toContain("search.recover");
    await screen.findByText("Recovery finished.");
  });
});
