import { describe, expect, it, vi } from "vitest";
import { toolAction } from "./api";

const target = {
  agentId: "agent-live",
  threadId: "thread-live",
  locale: "en",
  theme: "light" as const,
};

describe("toolAction", () => {
  it("sends only supported actions through the mounted tool Host with captured target and signal", async () => {
    const signal = new AbortController().signal;
    const host = {
      getToolContext: () => target,
      request: vi
        .fn()
        .mockResolvedValue({ api_version: 1, result: { evidence: [] } }),
      openSidePanel: vi.fn(),
    };

    await expect(
      toolAction(host, target, "evidence.list", { operation_id: "op" }, signal),
    ).resolves.toEqual({ evidence: [] });
    expect(host.request).toHaveBeenCalledWith(
      "/plugins/enterprise-knowledge/actions/evidence.list",
      expect.objectContaining({ method: "POST", signal }),
    );
    expect(JSON.parse(String(host.request.mock.calls[0][1].body))).toEqual({
      api_version: 1,
      context: { agent_id: "agent-live", thread_id: "thread-live" },
      payload: { operation_id: "op" },
    });
  });

  it("rejects a changed mounted target before sending the action", async () => {
    const host = {
      getToolContext: () => ({ ...target, agentId: "other" }),
      request: vi.fn(),
      openSidePanel: vi.fn(),
    };
    await expect(
      toolAction(host, target, "evidence.list", { operation_id: "op" }),
    ).rejects.toThrow(/context/i);
    expect(host.request).not.toHaveBeenCalled();
  });

  it("does not accept unknown action names or show transport response bodies", async () => {
    const host = {
      getToolContext: () => target,
      request: vi.fn().mockRejectedValue(new Error("private provider body")),
      openSidePanel: vi.fn(),
    };
    await expect(
      toolAction(host, target, "provider.download", {}),
    ).rejects.toThrow(/action/i);
    expect(host.request).not.toHaveBeenCalled();
    await expect(toolAction(host, target, "evidence.read", {})).rejects.toThrow(
      "The evidence request could not be completed.",
    );
  });

  it("discards a response if the mounted Host target changes while the request is pending", async () => {
    let current = target;
    let finish!: (value: unknown) => void;
    const host = {
      getToolContext: () => current,
      request: <T = unknown>() =>
        new Promise<T>((resolve) => {
          finish = (value) => resolve(value as T);
        }),
      openSidePanel: vi.fn(),
    };
    const pending = toolAction(host, target, "evidence.list", {
      operation_id: "op",
    });
    current = { ...target, threadId: "thread-replaced" };
    finish({ api_version: 1, result: { evidence: [{ evidence_id: "late" }] } });
    await expect(pending).rejects.toThrow(/context/i);
  });

  it("discards a rejected response when the mounted Host target changes while pending", async () => {
    let current = target;
    let rejectRequest!: (reason: unknown) => void;
    const host = {
      getToolContext: () => current,
      request: <T = unknown>() =>
        new Promise<T>((_resolve, reject) => {
          rejectRequest = reject;
        }),
      openSidePanel: vi.fn(),
    };
    const pending = toolAction(host, target, "evidence.list", {
      operation_id: "op",
    });
    current = { ...target, threadId: "thread-replaced" };
    rejectRequest(
      new Error(
        'Request failed: 403 Forbidden - {"error":{"code":"ACCESS_DENIED"}}',
      ),
    );
    await expect(pending).rejects.toThrow(/context/i);
  });
});
