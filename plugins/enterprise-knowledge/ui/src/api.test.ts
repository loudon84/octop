import { describe, expect, it, vi } from "vitest";
import { action, classifyTransportError } from "./api";

describe("plugin action transport", () => {
  it("sends the host-scoped action envelope and unwraps the result", async () => {
    const host = {
      getContext: () => ({ agentId: "expert-a", threadId: null }),
      request: vi.fn().mockResolvedValue({
        api_version: 1,
        result: { capabilities: { can_manage_bindings: true } },
      }),
    };

    await expect(action(host, "settings.get", {})).resolves.toEqual({
      capabilities: { can_manage_bindings: true },
    });
    expect(host.request).toHaveBeenCalledWith(
      "/plugins/enterprise-knowledge/actions/settings.get",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Content-Type": "application/json",
        }),
        body: JSON.stringify({
          api_version: 1,
          context: { agent_id: "expert-a", thread_id: null },
          payload: {},
        }),
      }),
    );
  });

  it("classifies Dashboard's Request failed transport error without showing its body", () => {
    const conflict = classifyTransportError(
      new Error(
        'Request failed: 409 Conflict - {"error":{"code":"CONFIG_CONFLICT","message":"stale"}}',
      ),
    );
    const denied = classifyTransportError(
      new Error(
        'Request failed: 403 Forbidden - {"error":{"code":"ACCESS_DENIED","message":"secret details"}}',
      ),
    );

    expect(conflict).toEqual({ status: 409, code: "CONFIG_CONFLICT" });
    expect(denied).toEqual({ status: 403, code: "ACCESS_DENIED" });
    expect(JSON.stringify([conflict, denied])).not.toContain("secret details");
  });
});
