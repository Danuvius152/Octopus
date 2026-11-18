import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { observeAcceptedText, useSessionStore } from "./sessionStore";
import { applyTranscript } from "../lib/transcript";
import { nativeItemKey } from "../lib/nativeOutput";

const releases: (() => void)[] = [];
beforeEach(() => useSessionStore.setState(useSessionStore.getInitialState(), true));
afterEach(() => { releases.splice(0).forEach((release) => release()); });
function observe(callback = vi.fn()) {
  releases.push(observeAcceptedText("text", useSessionStore.getState().accountGeneration, callback));
  return callback;
}

it("notifies only newly accepted nonempty assistant text, independently from snapshot and input publication", () => {
  const publications = vi.fn();
  const unsubscribe = useSessionStore.subscribe(publications);
  const received = observe();
  expect(publications).not.toHaveBeenCalled();
  unsubscribe();
  const store = useSessionStore.getState();
  store.appendStreamingText("text", "");
  store.addMessage("text", { role: "user", type: "text", seq: 1, content: "A user input" });
  store.addMessage("text", { role: "assistant", type: "thinking", seq: 2, content: "Metadata" });
  store.addMessage("text", { role: "assistant", type: "text", seq: 3, content: "" });
  applyTranscript("text", { messages: [{ role: "assistant", type: "text", seq: 4, content: "Saved snapshot" }],
    oldest_loaded_seq: 4, next_message_seq: 5 });
  store.addMessage("text", { role: "assistant", type: "text", seq: 4, content: "Replayed saved snapshot" });
  expect(received).not.toHaveBeenCalled();
  store.appendStreamingText("text", "New live text");
  expect(received).toHaveBeenCalledTimes(1);
  store.addMessage("text", { role: "assistant", type: "text", seq: 5, content: "New completed text" });
  expect(received).toHaveBeenCalledTimes(2);
  expect(received.mock.calls).toEqual([[], []]);
});

it.each(["root", "native"])("notifies each accepted %s delta after truncation without publishing or encoding", (kind) => {
  const store = useSessionStore.getState();
  const attempt = { request_id: "request", attempt_id: "attempt" };
  const native = { ...attempt, native: { thread_id: "root", root_thread_id: "root", turn_id: "turn",
    root_turn_id: "turn", item_id: "one" } };
  store.setActiveAttempt("text", attempt);
  store.appendStreamingText("text", "x".repeat(64 * 1024 + 100), kind === "native" ? native : attempt);
  const saturated = useSessionStore.getState();
  const received = observe();
  const publications = vi.fn();
  const release = useSessionStore.subscribe(publications);
  const encode = vi.spyOn(TextEncoder.prototype, "encode");
  try {
    for (let index = 0; index < 20; index++) {
      store.appendStreamingText("text", "x", kind === "native" ? { ...native, native: { ...native.native } } : { ...attempt });
    }
    expect(received).toHaveBeenCalledTimes(20);
    expect(encode.mock.calls.length).toBe(0);
    expect(publications.mock.calls.length).toBe(0);
    expect(useSessionStore.getState()).toBe(saturated);
  } finally {
    encode.mockRestore();
    release();
  }
});

it.each([16 * 1024, 64 * 1024])("checks the exact %i-byte JSON boundary before keeping a stable preview", (budget) => {
  const store = useSessionStore.getState();
  const attempt = { request_id: "request", attempt_id: "attempt" };
  const metadata = budget === 16 * 1024 ? { ...attempt, native: {
    thread_id: "root", root_thread_id: "root", turn_id: "turn", root_turn_id: "turn", item_id: "item",
  } } : attempt;
  const key = nativeItemKey(metadata);
  const bytes = (value: string) => new TextEncoder().encode(JSON.stringify(value)).length;
  const preview = () => {
    const state = useSessionStore.getState();
    return key ? state.streamingItems.text[key].content : state.streamingText.text.content;
  };
  const send = (chunk: string) => store.appendStreamingText("text", chunk, metadata);
  store.setActiveAttempt("text", attempt);
  const prefix = 'a natural ellipsis… followed by "\\\n';
  send(prefix + "\uD83D");
  send("\uDE00");
  expect(preview()).toBe(prefix + "😀");
  send("x".repeat(budget - bytes(preview() + "…")) + "…");
  const exact = preview();
  expect(bytes(exact)).toBe(budget);
  const before = useSessionStore.getState();
  const encode = vi.spyOn(TextEncoder.prototype, "encode");
  try {
    send("beyond the exact fit");
    expect(encode.mock.calls.length).toBeGreaterThan(0);
    expect(useSessionStore.getState()).toBe(before);
    encode.mockClear();
    send("after confirmed truncation");
    expect(encode.mock.calls.length).toBe(0);
    expect(preview()).toBe(exact);
    expect(useSessionStore.getState()).toBe(before);
  } finally { encode.mockRestore(); }
});

it("keeps original attempt, terminal and precise native item admission in front of the notification", () => {
  const store = useSessionStore.getState();
  const identity = { request_id: "request", attempt_id: "attempt" };
  const native = { ...identity, native: { thread_id: "root", root_thread_id: "root", turn_id: "turn",
    root_turn_id: "turn", item_id: "one" } };
  store.setActiveAttempt("text", identity);
  const received = observe();
  store.appendStreamingText("text", "Root", identity);
  store.appendStreamingText("text", "Native", native);
  store.appendStreamingText("text", "", native);
  store.appendStreamingText("text", "Wrong attempt", { ...native, attempt_id: "old" });
  expect(received).toHaveBeenCalledTimes(2);
  store.addMessage("text", { role: "assistant", type: "text", seq: 1, content: "Native complete", runtime_metadata: native });
  expect(received).toHaveBeenCalledTimes(3);
  store.appendStreamingText("text", "Late completed item delta", native);
  expect(received).toHaveBeenCalledTimes(3);
  store.appendStreamingText("text", "Other item", { ...native, native: { ...native.native, item_id: "two" } });
  expect(received).toHaveBeenCalledTimes(4);
  store.addMessage("text", { role: "assistant", type: "result", seq: 2, content: "Finished", runtime_metadata: identity });
  store.appendStreamingText("text", "After terminal", identity);
  expect(received).toHaveBeenCalledTimes(4);
});

it("does not replay an accepted text into a consumer registered by its store publication", () => {
  const retired = vi.fn(), successor = vi.fn();
  const release = observeAcceptedText("text", useSessionStore.getState().accountGeneration, retired);
  releases.push(release);
  let replaced = false;
  const unsubscribe = useSessionStore.subscribe(() => {
    if (replaced) return;
    replaced = true;
    release();
    observe(successor);
  });
  try {
    useSessionStore.getState().appendStreamingText("text", "Before the new opening's registration");
    expect(retired).not.toHaveBeenCalled();
    expect(successor).not.toHaveBeenCalled();
    useSessionStore.getState().appendStreamingText("text", "After registration");
    expect(successor).toHaveBeenCalledTimes(1);
  } finally { unsubscribe(); }
});

it("keeps same-id output scoped to its account and releases consumers synchronously", () => {
  const old = observe();
  useSessionStore.getState().setToken("next-account");
  const current = observe();
  useSessionStore.getState().appendStreamingText("text", "Current account");
  expect(old).not.toHaveBeenCalled();
  expect(current).toHaveBeenCalledTimes(1);
  releases.splice(0).forEach((release) => release());
  useSessionStore.getState().appendStreamingText("text", "After release");
  expect(current).toHaveBeenCalledTimes(1);
});
