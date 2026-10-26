import { afterEach, expect, it } from "vitest";
import {
  getSnapshot,
  patchToolResultData,
  removeSession,
  setFocusedChatSession,
  setMessages,
} from "./chatStore";
import type { ChatMessage } from "./useChat";

const THREAD_A = "plugin-tool-patch-a";
const THREAD_B = "plugin-tool-patch-b";
const toolMessage = (id: string, text: string): ChatMessage => ({
  id,
  role: "assistant",
  content: "",
  status: "done",
  timestamp: 1,
  toolData: {
    callId: "same-call-id",
    name: "test_tool",
    output: JSON.stringify({ octop_ui: { renderer: "test" }, data: { text } }),
  },
});

afterEach(() => {
  setFocusedChatSession(null);
  removeSession(THREAD_A);
  removeSession(THREAD_B);
});

it("patches only the explicit thread and never falls back when its call is absent", () => {
  setMessages(THREAD_A, [toolMessage("a-tool", "a-before")]);
  setMessages(THREAD_B, [toolMessage("b-tool", "b-before")]);
  setFocusedChatSession(THREAD_A);

  expect(
    patchToolResultData("same-call-id", { text: "b-after" }, THREAD_B),
  ).toBe(true);
  expect(
    JSON.parse(getSnapshot(THREAD_A).messages[0].toolData!.output!).data.text,
  ).toBe("a-before");
  expect(
    JSON.parse(getSnapshot(THREAD_B).messages[0].toolData!.output!).data.text,
  ).toBe("b-after");

  setMessages(THREAD_B, []);
  expect(
    patchToolResultData("same-call-id", { text: "wrong-thread" }, THREAD_B),
  ).toBe(false);
  expect(
    JSON.parse(getSnapshot(THREAD_A).messages[0].toolData!.output!).data.text,
  ).toBe("a-before");
});

it("preserves the legacy two argument focused then live session lookup", () => {
  setMessages(THREAD_A, [toolMessage("a-tool", "a-before")]);
  setMessages(THREAD_B, [toolMessage("b-tool", "b-before")]);
  setFocusedChatSession(THREAD_A);

  expect(patchToolResultData("same-call-id", { text: "legacy" })).toBe(true);
  expect(
    JSON.parse(getSnapshot(THREAD_A).messages[0].toolData!.output!).data.text,
  ).toBe("legacy");
  expect(
    JSON.parse(getSnapshot(THREAD_B).messages[0].toolData!.output!).data.text,
  ).toBe("b-before");
});
