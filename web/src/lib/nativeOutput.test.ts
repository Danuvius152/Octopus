import { beforeEach, expect, test } from "vitest";
import { handleWsMessage } from "../hooks/useWebSocket";
import { useSessionStore } from "../stores/sessionStore";
import { nativeCard, nativeChatMessages, nativeDetailAnchor } from "./nativeOutput";
import { projectToolCall } from "./toolIdentity";
import { applyTranscript } from "./transcript";

const identity = (thread: string) => ({ request_id: "r", attempt_id: "a", native: {
  thread_id: thread, turn_id: "t", item_id: "same", root_thread_id: "root", root_turn_id: "t",
} });

test("hidden root reasoning has no chat rows while transcript sequences and child observations survive", () => {
  const messages = [
    { role: "assistant" as const, type: "text", seq: 1, content: "Before" },
    ...["", "", "", "", "", "", "", "Retained reasoning"].map((content, index) => ({
      role: "assistant" as const, type: "thinking", seq: index + 2, content,
    })),
    { role: "assistant" as const, type: "thinking", seq: 10, content: "", runtime_metadata: identity("child") },
    { role: "assistant" as const, type: "text", seq: 11, content: "After" },
  ];
  useSessionStore.getState().setMessages("s", messages);
  useSessionStore.getState().setLastAppliedSeq("s", 11);
  const stored = useSessionStore.getState().messages.s;
  expect(nativeChatMessages(stored).map((row) => [row.type, row.seq])).toEqual([
    ["text", 1], ["native_subagent", 10], ["text", 11],
  ]);
  expect(stored).toEqual(messages);
  expect(useSessionStore.getState().lastAppliedSeq.s).toBe(11);
});

test("a tool-only historical window exposes a saved child reference without inventing completion", () => {
  const messages = [{ role: "tool" as const, type: "tool_result", seq: 40, content: "Saved tool output", runtime_metadata: identity("child") }];
  const cards = nativeChatMessages(messages);
  expect(cards).toHaveLength(1);
  const card = nativeCard(messages, cards[0].tool_input!.card_id as string)!;
  expect(card.status).toBe("unknown");
  expect(nativeDetailAnchor(messages, card)).toBe(40);
});

beforeEach(() => useSessionStore.setState({ messages: {}, streamingText: {}, streamingTextAttempt: {}, streamingItems: {}, lastAppliedSeq: {}, activeAttempts: {} }));

test("a completed native item stays closed while other root and child items keep streaming", () => {
  handleWsMessage({ type: "attempt_started", session_id: "s", request_id: "r", attempt_id: "a" });
  applyTranscript("s", { messages: [{ role: "assistant", type: "text", seq: 5, content: "Root finished", runtime_metadata: identity("root") }],
    next_message_seq: 6 });
  handleWsMessage({ type: "assistant_delta", session_id: "s", content: "Stale root prefix", runtime_metadata: identity("root") });
  handleWsMessage({ type: "assistant_delta", session_id: "s", content: "Child with reused provider ID", runtime_metadata: identity("child") });
  handleWsMessage({ type: "assistant_delta", session_id: "s", content: "Following root item", runtime_metadata: {
    ...identity("root"), native: { ...identity("root").native, item_id: "next" },
  } });
  const state = useSessionStore.getState();
  expect(Object.values(state.streamingItems.s || {}).map((item) => item.content)).toEqual([
    "Child with reused provider ID", "Following root item",
  ]);
  expect(state.messages.s.map((row) => row.seq)).toEqual([5]);
});

test("identified deltas need an observed attempt while identity-free legacy streaming retains its rules", () => {
  useSessionStore.setState({ activeAttempts: {}, streamingTextAttempt: {} });
  handleWsMessage({ type: "assistant_delta", session_id: "s", content: "Unobserved native attempt", runtime_metadata: identity("root") });
  expect(Object.values(useSessionStore.getState().streamingItems.s || {})).toEqual([]);
  useSessionStore.getState().setActiveAttempt("s", { request_id: "r", attempt_id: "a" });
  handleWsMessage({ type: "assistant_delta", session_id: "s", content: "Observed partial", runtime_metadata: identity("root") });
  useSessionStore.getState().setActiveAttempt("s", null);
  handleWsMessage({ type: "assistant_delta", session_id: "s", content: "Stale append", runtime_metadata: identity("root") });
  expect(Object.values(useSessionStore.getState().streamingItems.s || {}).map((item) => item.content)).toEqual(["Observed partial"]);
  handleWsMessage({ type: "assistant_delta", session_id: "s", content: "Legacy partial" });
  expect(useSessionStore.getState().streamingText.s).toBe("Legacy partial");
  handleWsMessage({ type: "assistant_text", session_id: "s", seq: 1, content: "Legacy complete" });
  expect(useSessionStore.getState().streamingText.s).toBeUndefined();
  expect(useSessionStore.getState().messages.s[0].content).toBe("Legacy complete");
});

test("interleaved native items clear only their own streaming text without advancing snapshot coverage", () => {
  handleWsMessage({ type: "attempt_started", session_id: "s", request_id: "r", attempt_id: "a" });
  handleWsMessage({ type: "assistant_delta", session_id: "s", content: "ROOT", runtime_metadata: identity("root") });
  handleWsMessage({ type: "assistant_delta", session_id: "s", content: "CHILD", runtime_metadata: identity("child") });
  handleWsMessage({ type: "assistant_text", session_id: "s", seq: 11, content: "Child done", runtime_metadata: identity("child") });
  const store = useSessionStore.getState();
  expect(Object.values(store.streamingItems.s).map((value) => value.content)).toEqual(["ROOT"]);
  expect(store.streamingText.s).toBeUndefined();
  expect(store.messages.s[0].runtime_metadata?.native?.thread_id).toBe("child");
  expect(store.lastAppliedSeq.s).toBeUndefined();
  expect(store.messages.s.map((row) => row.seq)).toEqual([11]);
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
  ], 1, false, { beforeSeq: 7, window: useSessionStore.getState().messageWindows.s });
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
  handleWsMessage({ type: "attempt_started", session_id: "s", request_id: "r", attempt_id: "a" });
  handleWsMessage({ type: "assistant_delta", session_id: "s", content: "Old partial", runtime_metadata: identity("child") });
  expect(Object.values(useSessionStore.getState().streamingItems.s).map((item) => item.content)).toEqual(["Old partial"]);
  useSessionStore.getState().applyDispatchState("s", { generation: "new-service", revision: 1, paused: false, active_request_id: null, active_operation: null, items: [] });
  expect(useSessionStore.getState().streamingItems).toEqual({});
});
