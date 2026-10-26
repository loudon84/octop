import { afterEach, expect, it, vi } from "vitest";
import {
  cancelStream,
  getSnapshot,
  onSteerRestore,
  removeSession,
  sendSteerWhileBusy,
  sendTurn,
  withdrawCommittedSteer,
} from "./chatStore";

const SESSION = "test-plugin-context-wire";
const DIRECT_SESSION = "test-plugin-context-direct-reject";

class FakeWebSocket {
  static latest: FakeWebSocket | null = null;
  static OPEN = 1;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  sent: string[] = [];

  constructor(_url: string) {
    FakeWebSocket.latest = this;
  }
  send(value: string) {
    this.sent.push(value);
  }
  close() {
    this.readyState = 3;
    this.onclose?.();
  }
}

afterEach(() => {
  cancelStream(SESSION);
  removeSession(SESSION);
  cancelStream(DIRECT_SESSION);
  removeSession(DIRECT_SESSION);
  vi.unstubAllGlobals();
  FakeWebSocket.latest = null;
});

it("sends plugin_context and follow_up as separate user_turn fields", async () => {
  vi.stubGlobal("WebSocket", FakeWebSocket);
  const turn = sendTurn(
    SESSION,
    "native message",
    "agent-1",
    "",
    undefined,
    undefined,
    "model-T1",
    "thread-1",
    ["connector-T1"],
    ["kb-T1"],
    ["expert-T1"],
    "enabled",
    "high",
    "craft",
    { mode: "allow_tools", tools: ["tool-T1"] },
    {
      pluginContext: {
        "enterprise-knowledge": { operation_id: "op-1", ids: [] },
      },
      delivery: "follow_up",
    },
  );
  const ws = FakeWebSocket.latest;
  expect(ws).toBeTruthy();
  ws!.onopen?.();
  expect(JSON.parse(ws!.sent[0] ?? "{}")).toMatchObject({
    type: "user_turn",
    text: "native message",
    plugin_context: {
      "enterprise-knowledge": { operation_id: "op-1", ids: [] },
    },
    delivery: "follow_up",
    model: "model-T1",
    mcp_servers: ["connector-T1"],
    knowledge_base_ids: ["kb-T1"],
    target_agent_ids: ["expert-T1"],
    reasoning_mode: "enabled",
    reasoning_effort: "high",
    conversation_mode: "craft",
    hitl_policy: { mode: "allow_tools", tools: ["tool-T1"] },
  });
  ws!.onmessage?.({
    data: JSON.stringify({
      type: "error",
      thread_id: "thread-1",
      message: "busy",
    }),
  });
  await turn;
});

it("reports terminal outcomes conservatively for immutable turn ownership", async () => {
  vi.stubGlobal("WebSocket", FakeWebSocket);
  const onOutcome = vi.fn();
  const turn = sendTurn(
    SESSION,
    "native message",
    "agent-1",
    "",
    undefined,
    undefined,
    undefined,
    "thread-1",
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    "craft",
    undefined,
    { onOutcome },
  );
  const ws = FakeWebSocket.latest!;
  ws.onopen?.();
  ws.onmessage?.({
    data: JSON.stringify({ type: "turn.ended", thread_id: "thread-1" }),
  });
  await turn;
  expect(onOutcome).toHaveBeenCalledExactlyOnceWith("complete");
});

it("keeps an error frame outcome uncertain because it has no admission code", async () => {
  vi.stubGlobal("WebSocket", FakeWebSocket);
  const onOutcome = vi.fn();
  const turn = sendTurn(
    SESSION,
    "native message",
    "agent-1",
    "",
    undefined,
    undefined,
    undefined,
    "thread-1",
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    "craft",
    undefined,
    { onOutcome },
  );
  const ws = FakeWebSocket.latest!;
  ws.onopen?.();
  ws.onmessage?.({
    data: JSON.stringify({
      type: "error",
      thread_id: "thread-1",
      message: "busy",
    }),
  });
  await turn;
  expect(onOutcome).toHaveBeenCalledExactlyOnceWith("uncertain");
});

it("does not cancel an active run when a bound follow_up arrives while busy", async () => {
  vi.stubGlobal("WebSocket", FakeWebSocket);
  const active = sendTurn(
    SESSION,
    "active",
    "agent-1",
    "",
    undefined,
    undefined,
    undefined,
    "thread-1",
  );
  const prior = FakeWebSocket.latest!;
  prior.onopen?.();
  const onOutcome = vi.fn();
  await sendTurn(
    SESSION,
    "next",
    "agent-1",
    "",
    undefined,
    undefined,
    undefined,
    "thread-1",
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    "craft",
    undefined,
    { delivery: "follow_up", onOutcome },
  );
  expect(FakeWebSocket.latest).toBe(prior);
  expect(prior.sent.some((frame) => frame.includes('"cancel"'))).toBe(false);
  expect(onOutcome).toHaveBeenCalledExactlyOnceWith("rejected");
  cancelStream(SESSION);
  await active;
});

it("settles this socket's explicit busy rejection as rejected", async () => {
  vi.stubGlobal("WebSocket", FakeWebSocket);
  const onOutcome = vi.fn();
  const turn = sendTurn(
    SESSION,
    "native message",
    "agent-1",
    "",
    undefined,
    undefined,
    undefined,
    "thread-1",
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    "craft",
    undefined,
    { delivery: "follow_up", onOutcome },
  );
  const ws = FakeWebSocket.latest!;
  ws.onopen?.();
  ws.onmessage?.({
    data: JSON.stringify({
      type: "steer.ack",
      status: "rejected",
      thread_id: "thread-1",
    }),
  });
  expect(onOutcome).toHaveBeenCalledExactlyOnceWith("rejected");
  await turn;
});

it("settles only an initial follow-up rejection, not later steer or withdraw acks", async () => {
  vi.stubGlobal("WebSocket", FakeWebSocket);
  const onOutcome = vi.fn();
  const restored: Array<{ text: string; reason: string }> = [];
  const stop = onSteerRestore((event) =>
    restored.push({ text: event.text, reason: event.reason }),
  );
  const active = sendTurn(
    SESSION,
    "initial bound follow-up",
    "agent-1",
    "",
    undefined,
    undefined,
    undefined,
    "thread-1",
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    "craft",
    undefined,
    { delivery: "follow_up", onOutcome },
  );
  const socket = FakeWebSocket.latest!;
  socket.readyState = FakeWebSocket.OPEN;
  socket.onopen?.();
  socket.onmessage?.({
    data: JSON.stringify({
      type: "turn.started",
      thread_id: "thread-1",
      turn_id: "turn-1",
    }),
  });

  expect(
    sendSteerWhileBusy(SESSION, "recover this steer", {
      expectedTurnId: "turn-1",
      clientPendingId: "steer-cpid",
    }),
  ).toBe("ok");
  socket.onmessage?.({
    data: JSON.stringify({
      type: "steer.rejected",
      thread_id: "thread-1",
      client_pending_id: "steer-cpid",
      reason: "stale_turn",
    }),
  });
  expect(restored).toEqual([
    { text: "recover this steer", reason: "stale_turn" },
  ]);
  expect(onOutcome).not.toHaveBeenCalled();
  expect(FakeWebSocket.latest).toBe(socket);
  expect(getSnapshot(SESSION).isStreaming).toBe(true);

  expect(
    sendSteerWhileBusy(SESSION, "withdraw this steer", {
      expectedTurnId: "turn-1",
      clientPendingId: "withdraw-cpid",
    }),
  ).toBe("ok");
  socket.onmessage?.({
    data: JSON.stringify({
      type: "steer.queued",
      thread_id: "thread-1",
      client_pending_id: "withdraw-cpid",
      pending_id: "withdraw-server-id",
      preview: "withdraw this steer",
    }),
  });
  expect(withdrawCommittedSteer(SESSION, "steer:withdraw-cpid")).toBe("ok");
  socket.onmessage?.({
    data: JSON.stringify({
      type: "steer.ack",
      status: "rejected",
      thread_id: "thread-1",
      client_pending_id: "withdraw-cpid",
      pending_id: "withdraw-server-id",
      reason: "cannot_withdraw",
    }),
  });
  expect(onOutcome).not.toHaveBeenCalled();
  expect(FakeWebSocket.latest).toBe(socket);
  expect(getSnapshot(SESSION).isStreaming).toBe(true);
  expect(
    getSnapshot(SESSION).messages.some(
      (message) => message.id === "steer:withdraw-cpid",
    ),
  ).toBe(true);

  socket.onmessage?.({
    data: JSON.stringify({
      type: "steer.rejected",
      thread_id: "thread-1",
      client_pending_id: "unrelated-cpid",
      reason: "unrelated_broadcast",
    }),
  });
  expect(onOutcome).not.toHaveBeenCalled();
  expect(getSnapshot(SESSION).isStreaming).toBe(true);
  stop();

  const directOutcome = vi.fn();
  const direct = sendTurn(
    DIRECT_SESSION,
    "independent follow-up",
    "agent-1",
    "",
    undefined,
    undefined,
    undefined,
    "thread-direct",
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    "craft",
    undefined,
    { delivery: "follow_up", onOutcome: directOutcome },
  );
  const directSocket = FakeWebSocket.latest!;
  directSocket.readyState = FakeWebSocket.OPEN;
  directSocket.onopen?.();
  directSocket.onmessage?.({
    data: JSON.stringify({
      type: "steer.ack",
      status: "rejected",
      thread_id: "thread-direct",
      reason: "busy",
    }),
  });
  await direct;
  expect(directOutcome).toHaveBeenCalledExactlyOnceWith("rejected");
  expect(getSnapshot(DIRECT_SESSION).isStreaming).toBe(false);

  cancelStream(SESSION);
  await active;
});
