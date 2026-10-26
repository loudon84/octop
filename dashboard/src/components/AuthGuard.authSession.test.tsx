// @vitest-environment jsdom
import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import AuthGuard from "./AuthGuard";
import { authApi } from "../api/modules/auth";
import { clearSetupRequired, request, setAuthToken } from "../api/request";
import {
  getAuthSessionEpoch,
  invalidateAuthSession,
  observeAuthPrincipal,
} from "../api/authSession";
import { applyUserLocale } from "../utils/locale";

vi.mock("../api/modules/auth", () => ({
  authApi: { getAuthStatus: vi.fn(), me: vi.fn() },
}));
vi.mock("../api/config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
}));
vi.mock("../utils/locale", () => ({
  applyUserLocale: vi.fn(async () => "en"),
  isNetworkFetchError: () => false,
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const user = { id: 1, username: "one", role: "user", locale: "en" };

function CurrentPath() {
  return <span data-testid="current-path">{useLocation().pathname}</span>;
}

beforeEach(() => {
  cleanup();
  vi.mocked(authApi.getAuthStatus)
    .mockReset()
    .mockResolvedValue({
      setup_required: false,
      has_admin: true,
    } as never);
  vi.mocked(authApi.me).mockReset();
  localStorage.clear();
  sessionStorage.clear();
  clearSetupRequired();
  setAuthToken("token");
  vi.mocked(applyUserLocale)
    .mockReset()
    .mockResolvedValue("en" as never);
  invalidateAuthSession();
});

describe("AuthGuard auth session observation", () => {
  it("discards an old /auth/me result before locale application when the session changes", async () => {
    const pendingMe = deferred<typeof user>();
    vi.mocked(authApi.me).mockReturnValue(pendingMe.promise as never);
    const before = getAuthSessionEpoch();
    render(
      <MemoryRouter>
        <AuthGuard>
          <div>protected-shell</div>
        </AuthGuard>
      </MemoryRouter>,
    );
    await waitFor(() => expect(authApi.me).toHaveBeenCalledOnce());

    invalidateAuthSession();
    pendingMe.resolve(user);
    await Promise.resolve();
    await Promise.resolve();

    expect(applyUserLocale).not.toHaveBeenCalled();
    expect(screen.queryByText("protected-shell")).toBeNull();
    expect(getAuthSessionEpoch()).toBeGreaterThan(before);
  });

  it("observes the verified principal without a second initial login invalidation", async () => {
    vi.mocked(authApi.me).mockResolvedValue(user as never);
    const before = getAuthSessionEpoch();
    render(
      <MemoryRouter>
        <AuthGuard>
          <div>protected-shell</div>
        </AuthGuard>
      </MemoryRouter>,
    );
    expect(await screen.findByText("protected-shell")).toBeTruthy();
    expect(getAuthSessionEpoch()).toBe(before);

    observeAuthPrincipal({ ...user, permissions: [] });
    expect(getAuthSessionEpoch()).toBe(before);
    observeAuthPrincipal({ ...user, id: 2, username: "two" });
    expect(getAuthSessionEpoch()).toBeGreaterThan(before);
  });

  it("silently discards a real request rejection when the session changes", async () => {
    let resolveFetch!: (response: Response) => void;
    let resolveRequestSettled!: () => void;
    const requestSettled = new Promise<void>((resolve) => {
      resolveRequestSettled = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveFetch = resolve;
          }),
      ),
    );
    vi.mocked(authApi.me).mockImplementation((() => {
      const pending = request<typeof user>("/auth/me");
      void pending.then(resolveRequestSettled, resolveRequestSettled);
      return pending;
    }) as never);
    const { getByTestId } = render(
      <MemoryRouter initialEntries={["/protected"]}>
        <CurrentPath />
        <AuthGuard>
          <div>protected-shell</div>
        </AuthGuard>
      </MemoryRouter>,
    );
    await waitFor(() => expect(authApi.me).toHaveBeenCalledOnce());
    await waitFor(() => expect(fetch).toHaveBeenCalledOnce());

    await act(async () => {
      setAuthToken("replacement-token");
      resolveFetch(
        new Response(JSON.stringify(user), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
      await requestSettled;
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(getByTestId("current-path")).toHaveTextContent("/protected");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText("protected-shell")).toBeNull();
    expect(vi.mocked(applyUserLocale)).not.toHaveBeenCalled();
  });

  it("passes the full verification lease into delayed locale application", async () => {
    const pendingLocale = deferred<"en">();
    vi.mocked(authApi.me).mockResolvedValue(user as never);
    vi.mocked(applyUserLocale).mockReturnValue(pendingLocale.promise as never);
    const { getByTestId } = render(
      <MemoryRouter initialEntries={["/protected"]}>
        <CurrentPath />
        <AuthGuard>
          <div>protected-shell</div>
        </AuthGuard>
      </MemoryRouter>,
    );
    await waitFor(() => expect(applyUserLocale).toHaveBeenCalledOnce());
    const options = vi.mocked(applyUserLocale).mock.calls[0]?.[1];
    expect(options?.isCurrent?.()).toBe(true);

    invalidateAuthSession();
    expect(options?.isCurrent?.()).toBe(false);
    pendingLocale.resolve("en");
    await pendingLocale.promise;
    await Promise.resolve();

    expect(getByTestId("current-path")).toHaveTextContent("/protected");
    expect(screen.queryByText("protected-shell")).toBeNull();
  });
});
