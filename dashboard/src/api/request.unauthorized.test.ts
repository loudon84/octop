import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
}));

vi.mock("../i18n", () => ({
  default: { language: "zh" },
}));

function unauthorizedResponse(): Response {
  return new Response("{}", {
    status: 401,
    headers: { "content-type": "application/json" },
  });
}

describe("401 handling", () => {
  const replace = vi.fn();
  let mod: typeof import("./request");

  beforeEach(async () => {
    vi.resetModules();
    localStorage.clear();
    sessionStorage.clear();
    replace.mockClear();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { pathname: "/chat", replace },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(unauthorizedResponse())),
    );
    mod = await import("./request");
    mod.setAuthToken("expired-token");
  });

  it("lets an in-app listener take over the redirect instead of reloading", async () => {
    const listener = vi.fn((event: Event) => event.preventDefault());
    window.addEventListener(mod.UNAUTHORIZED_EVENT, listener);

    await expect(mod.request("/agents")).rejects.toThrow();

    expect(listener).toHaveBeenCalledOnce();
    expect(replace).not.toHaveBeenCalled();
    expect(mod.getAuthToken()).toBe("");

    window.removeEventListener(mod.UNAUTHORIZED_EVENT, listener);
  });

  it("falls back to a full-page navigation when nothing handles the event", async () => {
    await expect(mod.request("/agents")).rejects.toThrow();

    expect(replace).toHaveBeenCalledWith("/login");
  });

  it("keeps the local session when a 401 is a peer credential error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              error: {
                code: "BRIDGE_AUTH_FAILED",
                message: "peer rejected credentials",
                details: {},
              },
            }),
            {
              status: 401,
              headers: { "content-type": "application/json" },
            },
          ),
        ),
      ),
    );

    await expect(
      mod.request("/bridge/probe", { method: "POST" }),
    ).rejects.toThrow(/BRIDGE_AUTH_FAILED/);

    expect(replace).not.toHaveBeenCalled();
    expect(mod.getAuthToken()).toBe("expired-token");
  });

  it("redirects only once when several requests fail concurrently", async () => {
    const listener = vi.fn((event: Event) => event.preventDefault());
    window.addEventListener(mod.UNAUTHORIZED_EVENT, listener);

    await Promise.allSettled([
      mod.request("/agents"),
      mod.request("/skills"),
      mod.request("/cron"),
    ]);

    expect(listener).toHaveBeenCalledOnce();

    window.removeEventListener(mod.UNAUTHORIZED_EVENT, listener);
  });

  it("does not clear a replacement login when an older request returns 401", async () => {
    let resolveFetch!: (response: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveFetch = resolve;
          }),
      ),
    );

    const oldRequest = mod.request("/agents");
    mod.setAuthToken("new-login-token");
    resolveFetch(
      new Response(
        JSON.stringify({ error: { code: "AUTH_FAILED", message: "expired" } }),
        { status: 401, headers: { "content-type": "application/json" } },
      ),
    );

    await expect(oldRequest).rejects.toThrow(/session changed/i);
    expect(mod.getAuthToken()).toBe("new-login-token");
    expect(replace).not.toHaveBeenCalled();
  });

  it("does not persist an old sliding renewal after a replacement login", async () => {
    let resolveFetch!: (response: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveFetch = resolve;
          }),
      ),
    );

    const oldRequest = mod.request("/agents");
    mod.setAuthToken("new-login-token");
    resolveFetch(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: {
          "content-type": "application/json",
          "X-Octop-Access-Token": "old-renewed-token",
        },
      }),
    );

    await expect(oldRequest).rejects.toThrow(/session changed/i);
    expect(mod.getAuthToken()).toBe("new-login-token");
  });

  it("rejects a response whose JSON body settles after the session changes", async () => {
    let releaseBody!: (
      controller: ReadableStreamDefaultController<Uint8Array>,
    ) => void;
    const bodyReady = new Promise<ReadableStreamDefaultController<Uint8Array>>(
      (resolve) => {
        releaseBody = resolve;
      },
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            new ReadableStream<Uint8Array>({
              pull(controller) {
                releaseBody(controller);
              },
            }),
            {
              status: 200,
              headers: { "content-type": "application/json" },
            },
          ),
        ),
      ),
    );

    const oldRequest = mod.request<{ ok: boolean }>("/agents");
    const bodyController = await bodyReady;
    mod.setAuthToken("new-login-token");
    bodyController.enqueue(new TextEncoder().encode('{"ok":true}'));
    bodyController.close();

    await expect(oldRequest).rejects.toThrow(/session changed/i);
  });

  it("rejects a Blob whose body read settles after the session changes", async () => {
    let releaseBody!: (
      controller: ReadableStreamDefaultController<Uint8Array>,
    ) => void;
    const bodyReady = new Promise<ReadableStreamDefaultController<Uint8Array>>(
      (resolve) => {
        releaseBody = resolve;
      },
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            new ReadableStream<Uint8Array>({
              pull(controller) {
                releaseBody(controller);
              },
            }),
            { status: 200, headers: { "content-length": "1" } },
          ),
        ),
      ),
    );

    const oldRequest = mod.requestBlob("/agents/file");
    const bodyController = await bodyReady;
    mod.setAuthToken("new-login-token");
    bodyController.enqueue(new Uint8Array([1]));
    bodyController.close();

    await expect(oldRequest).rejects.toThrow(/session changed/i);
  });

  it("settles an upload with a stale-session rejection from xhr.onload", async () => {
    class DeferredXhr {
      static instance: DeferredXhr;
      status = 200;
      responseText = "{}";
      upload = {};
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      onabort: (() => void) | null = null;
      open = vi.fn();
      setRequestHeader = vi.fn();
      getResponseHeader = vi.fn(() => null);
      send = vi.fn();
      constructor() {
        DeferredXhr.instance = this;
      }
      fireLoad() {
        this.onload?.();
      }
    }
    vi.stubGlobal("XMLHttpRequest", DeferredXhr);

    const oldUpload = mod.requestUpload("/agents/file", new FormData());
    mod.setAuthToken("new-login-token");
    DeferredXhr.instance.fireLoad();

    await expect(oldUpload).rejects.toThrow(/session changed/i);
  });
});
