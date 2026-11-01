import { beforeEach, expect, test } from "vitest";
import { handleWsMessage } from "../hooks/useWebSocket";
import { useSessionStore } from "../stores/sessionStore";
import { nativeCard, nativeChatMessages } from "./nativeOutput";
import { projectToolCall } from "./toolIdentity";

const identity = (thread: string) => ({ request_id: "r", attempt_id: "a", native: {
  thread_id: thread, turn_id: "t", item_id: "same", root_thread_id: "root", root_turn_id: "t",
} });

beforeEach(() => useSessionStore.setState({ messages: {}, streamingText: {}, streamingItems: {}, lastAppliedSeq: {} }));

test("interleaved native items clear only their own streaming text and keep the transcript high water", () => {
  handleWsMessage({ type: "assistant_delta", session_id: "s", content: "ROOT", runtime_metadata: identity("root") });
  handleWsMessage({ type: "assistant_delta", session_id: "s", content: "CHILD", runtime_metadata: identity("child") });
  handleWsMessage({ type: "assistant_text", session_id: "s", seq: 11, content: "Child done", runtime_metadata: identity("child") });
  const store = useSessionStore.getState();
  expect(Object.values(store.streamingItems.s).map((value) => value.content)).toEqual(["ROOT"]);
  expect(store.streamingText.s).toBeUndefined();
  expect(store.messages.s[0].runtime_metadata?.native?.thread_id).toBe("child");
  expect(store.lastAppliedSeq.s).toBe(11);
  handleWsMessage({ type: "assistant_text", session_id: "s", seq: 12, content: "Root done", runtime_metadata: identity("root") });
  expect(Object.values(useSessionStore.getState().streamingItems.s)).toEqual([]);
});

test("history pagination rebuilds native cards without child text leaking into main chat or lowering the high water", () => {
  const store = useSessionStore.getState();
  store.setLastAppliedSeq("s", 20);
  store.setMessages("s", [
    { role: "system", type: "native_subagent", seq: 7, runtime_metadata: identity("child"), tool_input: { task_id: "child", card_id: "c", status: "completed", native_thread_id: "child", native_turn_id: "t" } },
    { role: "assistant", type: "text", seq: 8, content: "Other attempt must stay out", runtime_metadata: { ...identity("child"), attempt_id: "other" } },
    { role: "assistant", type: "text", seq: 9, content: "Root", runtime_metadata: identity("root") },
  ]);
  store.setMessageWindow("s", 7, true);
  store.prependMessages("s", [
    { role: "system", type: "native_subagent", seq: 1, tool_input: { task_id: "child", card_id: "c", name: "/root/reviewer", status: "running", native_thread_id: "child", native_turn_id: "t" } },
    { role: "assistant", type: "text", seq: 2, content: "Saved child", runtime_metadata: identity("child") },
  ], 1, false);
  const state = useSessionStore.getState();
  expect(nativeChatMessages(state.messages.s).map((message) => message.seq)).toEqual([1, 8, 9]);
  expect(nativeCard(state.messages.s, "c")).toMatchObject({ name: "/root/reviewer", status: "completed", summary: "Saved child" });
  expect(state.lastAppliedSeq.s).toBe(20);
  expect(state.messages.s.map((message) => message.seq)).toEqual([1, 2, 7, 8, 9]);
  const fakeTool = { role: "assistant" as const, type: "tool_use", runtime_metadata: identity("root") };
  expect(projectToolCall(fakeTool, [{ role: "tool", type: "tool_result", runtime_metadata: identity("root") }]).result).toBeUndefined();
});

test("a history window containing child text without its older status row still exposes the captured body as unconfirmed", () => {
  const messages = [{ role: "assistant" as const, type: "text", seq: 50, content: "Saved body", runtime_metadata: identity("child") }];
  const visible = nativeChatMessages(messages);
  expect(visible).toHaveLength(1);
  expect(visible[0].type).toBe("native_subagent");
  const card = nativeCard(messages, visible[0].tool_input!.card_id as string);
  expect(card).toMatchObject({ status: "unknown", summary: "Saved body", native_thread_id: "child", native_turn_id: "t" });
  expect(messages[0].content).toBe("Saved body");
});

test("a snapshot card remains visible when a 200-row tool window excludes its anchor and is deduplicated when paged in", () => {
  const cache = [{ task_id: "child", card_id: "cached", native_thread_id: "child", native_turn_id: "t",
    status: "running" as const, name: "/root/helper", description: "working", summary: "", prompt: "", steps: [],
    runtime_metadata: identity("child") }];
  const window = Array.from({ length: 200 }, (_, index) => ({ role: "assistant" as const, type: "tool_use", seq: index + 100 }));
  const visible = nativeChatMessages(window, cache);
  expect(visible).toHaveLength(201);
  expect(visible[200].seq).toBeUndefined();
  expect(nativeCard(window, "cached", cache)).toMatchObject({ status: "running", name: "/root/helper", native_turn_id: "t" });
  const paged = [{ role: "system" as const, type: "native_subagent", seq: 1, runtime_metadata: identity("child"),
    tool_input: { task_id: "child", card_id: "cached", native_thread_id: "child", native_turn_id: "t", status: "running" } }, ...window];
  expect(nativeChatMessages(paged, cache).filter((row) => row.type === "native_subagent")).toHaveLength(1);
});

test("a new service generation clears partial items from the previous native connection", () => {
  useSessionStore.setState({ dispatchGeneration: "old-service", retiredDispatchGenerations: [], dispatchStates: {} });
  handleWsMessage({ type: "assistant_delta", session_id: "s", content: "Old partial", runtime_metadata: identity("child") });
  useSessionStore.getState().applyDispatchState("s", { generation: "new-service", revision: 1, paused: false, active_request_id: null, items: [] });
  expect(useSessionStore.getState().streamingItems).toEqual({});
});
