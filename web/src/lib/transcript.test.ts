import { beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionStore, type Message } from "../stores/sessionStore";
import { applyTranscript, observeOlderMessages } from "./transcript";
import { handleWsMessage } from "../hooks/useWebSocket";

const msg = (seq: number): Message => ({
  role: "user",
  type: "text",
  content: `msg-${seq}`,
  seq,
});
const loadOlderMessages = (sessionId: string) => observeOlderMessages(sessionId).done;

/** A windowed transcript, and the scroll-back that fills it in
 * (polish-2026-09.md §4 B2).
 *
 * The two ways this path fails are both about the seam between the window and
 * the page before it: a message rendered twice, or a page fetched forever. Both
 * are pinned here, because in the browser they look like ordinary scrolling.
 */
describe("applyTranscript", () => {
  it.each([11, 200, 201])("replaces a disconnected prefix before tail window %i", (oldest) => {
    applyTranscript("s1", { messages: Array.from({ length: 11 }, (_, seq) => msg(seq)),
      oldest_loaded_seq: 0, next_message_seq: 11, has_more_messages: false });
    const start = oldest + 1;
    handleWsMessage({ type: "user_message", session_id: "s1", seq: start + 200, content: "live" });
    applyTranscript("s1", { messages: Array.from({ length: 200 }, (_, index) => msg(start + index)),
      oldest_loaded_seq: start, next_message_seq: start + 200, has_more_messages: true });
    const state = useSessionStore.getState();
    expect(state.messages.s1.map((row) => row.seq)).toEqual(Array.from({ length: 201 }, (_, index) => start + index));
    expect(state.oldestSeq.s1).toBe(start);
    expect(state.hasMoreMessages.s1).toBe(true);
  });

  it.each([10, 11])("preserves a contiguous paginated prefix at seam %i", (oldest) => {
    applyTranscript("s1", { messages: Array.from({ length: 11 }, (_, seq) => msg(seq)),
      oldest_loaded_seq: 0, next_message_seq: 11, has_more_messages: false });
    applyTranscript("s1", { messages: [msg(oldest), msg(oldest + 1)],
      oldest_loaded_seq: oldest, next_message_seq: oldest + 2, has_more_messages: true });
    expect(useSessionStore.getState().messages.s1.map((row) => row.seq))
      .toEqual(Array.from({ length: oldest + 2 }, (_, seq) => seq));
    expect(useSessionStore.getState().oldestSeq.s1).toBe(0);
    expect(useSessionStore.getState().hasMoreMessages.s1).toBe(false);
  });

  it("accepts a reserved terminal that arrives after the stored snapshot", () => {
    applyTranscript("s1", { messages: [msg(0)], next_message_seq: 1 });
    handleWsMessage({ type: "result", session_id: "s1", seq: 1, terminal: true,
      content: { kind: "outcome", request_id: "reserved", status: "success" } });
    expect(useSessionStore.getState().messages.s1.map((row) => row.seq)).toEqual([0, 1]);
    expect(useSessionStore.getState().lastAppliedSeq.s1).toBe(1);
  });

  it("quarantines ambiguous history instead of silently deduplicating its rows", () => {
    applyTranscript("s1", { messages: [msg(4), msg(4)], next_message_seq: 5,
      transcript_integrity: "duplicate_seq", has_more_messages: true });
    expect(useSessionStore.getState().messages.s1).toEqual([]);
    expect(useSessionStore.getState().hasMoreMessages.s1).toBe(false);
    expect(useSessionStore.getState().lastAppliedSeq.s1).toBeUndefined();
  });
  beforeEach(() => {
    useSessionStore.setState({
      token: "t",
      messages: {},
      oldestSeq: {},
      hasMoreMessages: {},
      lastAppliedSeq: {},
      streamingText: {},
      streamingItems: {},
    });
  });

  it("sets the transcript, the window and the dedup baseline together", () => {
    applyTranscript("s1", {
      messages: [msg(10), msg(11)],
      oldest_loaded_seq: 10,
      has_more_messages: true,
      next_message_seq: 12,
    });
    const s = useSessionStore.getState();
    expect(s.messages.s1).toHaveLength(2);
    expect(s.oldestSeq.s1).toBe(10);
    expect(s.hasMoreMessages.s1).toBe(true);
    // Anything at or below 11 is already in the messages above.
    expect(s.lastAppliedSeq.s1).toBe(11);
  });

  it("marks a whole transcript as having nothing older", () => {
    applyTranscript("s1", {
      messages: [msg(0)],
      oldest_loaded_seq: 0,
      has_more_messages: false,
      next_message_seq: 1,
    });
    expect(useSessionStore.getState().hasMoreMessages.s1).toBe(false);
  });

  it("loads earlier history while retaining a newer live message and streaming progress", () => {
    handleWsMessage({ type: "user_message", session_id: "s1", seq: 12, content: "correction",
      tool_input: { request_context: { request_id: "human-12", source: "human" } } });
    handleWsMessage({ type: "assistant_delta", session_id: "s1", content: "new answer in progress" });
    applyTranscript("s1", { messages: [msg(10), msg(11)], oldest_loaded_seq: 10,
      has_more_messages: true, next_message_seq: 12 });
    const state = useSessionStore.getState();
    expect(state.messages.s1.map((m) => m.seq)).toEqual([10, 11, 12]);
    expect(state.messages.s1[2].tool_input).toEqual({ request_context: { request_id: "human-12", source: "human" } });
    expect(state.lastAppliedSeq.s1).toBe(12);
    expect(state.streamingText.s1).toBe("new answer in progress");
    expect(state.oldestSeq.s1).toBe(10);
  });

  it("retains live deltas when the snapshot has no newer stored row", () => {
    handleWsMessage({ type: "user_message", session_id: "s1", seq: 0, content: "question" });
    handleWsMessage({ type: "assistant_delta", session_id: "s1", content: "answer still streaming" });
    applyTranscript("s1", { messages: [msg(0)], next_message_seq: 1 });
    expect(useSessionStore.getState().streamingText.s1).toBe("answer still streaming");
  });

  it("retains root and child native-only deltas that arrive during an older snapshot read", () => {
    const baseline = useSessionStore.getState().streamingItems.s1 || {};
    const identity = (thread: string) => ({ request_id: "r", attempt_id: "a", native: {
      thread_id: thread, turn_id: "t", item_id: "same", root_thread_id: "root", root_turn_id: "t" } });
    handleWsMessage({ type: "assistant_delta", session_id: "s1", content: "Root live", runtime_metadata: identity("root") });
    handleWsMessage({ type: "assistant_delta", session_id: "s1", content: "Child live", runtime_metadata: identity("child") });
    applyTranscript("s1", { messages: [msg(10)], next_message_seq: 11 }, baseline);
    expect(Object.values(useSessionStore.getState().streamingItems.s1).map((item) => item.content)).toEqual(["Root live", "Child live"]);
    applyTranscript("s1", { messages: [msg(10), { role: "assistant", type: "text", seq: 11,
      content: "Root complete", runtime_metadata: identity("root") }], next_message_seq: 12 }, baseline);
    expect(Object.values(useSessionStore.getState().streamingItems.s1).map((item) => item.content)).toEqual(["Child live"]);
    expect(useSessionStore.getState().lastAppliedSeq.s1).toBe(11);
  });
});

describe("loadOlderMessages", () => {
  beforeEach(() => {
    useSessionStore.setState({
      token: "t",
      messages: { s1: [msg(10), msg(11)] },
      oldestSeq: { s1: 10 },
      hasMoreMessages: { s1: true },
      lastAppliedSeq: {},
    });
  });

  it("asks for the page before what it holds and prepends it", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      expect(url).toContain("/api/sessions/s1/messages?before_seq=10");
      return {
        ok: true,
        json: async () => ({
          messages: [msg(8), msg(9)],
          oldest_loaded_seq: 8,
          has_more_messages: true,
        }),
      };
    });
    vi.stubGlobal("fetch", fetchMock);

    expect(await loadOlderMessages("s1")).toEqual({ state: "merged", added: 2 });
    const s = useSessionStore.getState();
    expect(s.messages.s1.map((m) => m.seq)).toEqual([8, 9, 10, 11]);
    expect(s.oldestSeq.s1).toBe(8);
    expect(s.hasMoreMessages.s1).toBe(true);
    vi.unstubAllGlobals();
  });

  it("never renders a message twice when a page overlaps", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        // 10 is already held; only 9 is new.
        json: async () => ({
          messages: [msg(9), msg(10)],
          oldest_loaded_seq: 9,
          has_more_messages: false,
        }),
      }))
    );
    expect(await loadOlderMessages("s1")).toEqual({ state: "merged", added: 1 });
    expect(useSessionStore.getState().messages.s1.map((m) => m.seq)).toEqual([
      9, 10, 11,
    ]);
    vi.unstubAllGlobals();
  });

  it("reports a stalled page without closing the retry entry", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          messages: [msg(10)],
          oldest_loaded_seq: 10,
          has_more_messages: true,
        }),
      }))
    );
    expect(await loadOlderMessages("s1")).toMatchObject({ state: "failed" });
    expect(useSessionStore.getState().hasMoreMessages.s1).toBe(true);
    vi.unstubAllGlobals();
  });

  it("does not ask when there is nothing older, or no session", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    useSessionStore.setState({ hasMoreMessages: { s1: false } });
    expect(await loadOlderMessages("s1")).toEqual({ state: "unneeded" });
    expect(await loadOlderMessages("")).toEqual({ state: "unneeded" });
    expect(fetchMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("rejects a late page across a replaced window without closing its gap", async () => {
    let respond!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { respond = resolve; })));
    const older = loadOlderMessages("s1");
    applyTranscript("s1", { messages: [msg(600), msg(601)], oldest_loaded_seq: 600,
      has_more_messages: true, next_message_seq: 602 });
    respond(Response.json({ messages: [msg(8), msg(9)], oldest_loaded_seq: 8, has_more_messages: false }));
    await older;
    expect(useSessionStore.getState().messages.s1.map((message) => message.seq)).toEqual([600, 601]);
    expect(useSessionStore.getState().oldestSeq.s1).toBe(600);
    expect(useSessionStore.getState().hasMoreMessages.s1).toBe(true);
    vi.unstubAllGlobals();
  });

  it("accepts a valid old page across WS tail appends and equivalent row copies", async () => {
    let respond!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { respond = resolve; })));
    const older = loadOlderMessages("s1");
    handleWsMessage({ type: "assistant_text", session_id: "s1", seq: 12, content: "new tail" });
    const state = useSessionStore.getState();
    state.setMessages("s1", state.messages.s1.map((message) => ({ ...message })));
    respond(Response.json({ messages: [msg(8), msg(9)], oldest_loaded_seq: 8, has_more_messages: true }));
    await older;
    expect(useSessionStore.getState().messages.s1.map((message) => message.seq)).toEqual([8, 9, 10, 11, 12]);
    vi.unstubAllGlobals();
  });

  it("does not ask twice at once", async () => {
    let resolve!: (v: unknown) => void;
    const fetchMock = vi.fn(
      () =>
        new Promise((r) => {
          resolve = r;
        })
    );
    vi.stubGlobal("fetch", fetchMock);
    const first = loadOlderMessages("s1");
    const second = loadOlderMessages("s1");
    expect(await second).toEqual({ state: "in_flight" });
    resolve({
      ok: true,
      json: async () => ({
        messages: [msg(9)],
        oldest_loaded_seq: 9,
        has_more_messages: false,
      }),
    });
    expect(await first).toEqual({ state: "merged", added: 1 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });

  it("leaves the transcript alone when the fetch fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, json: async () => ({}) })));
    expect(await loadOlderMessages("s1")).toMatchObject({ state: "failed" });
    expect(useSessionStore.getState().messages.s1.map((m) => m.seq)).toEqual([
      10, 11,
    ]);
    // Still true, so the user can try again rather than being told there is
    // nothing older when the request simply failed.
    expect(useSessionStore.getState().hasMoreMessages.s1).toBe(true);
    vi.unstubAllGlobals();
  });

  it("releases a cancelled view's slot without letting its late page settle the replacement", async () => {
    const responses: ((response: Response) => void)[] = [];
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { responses.push(resolve); })));
    const oldView = observeOlderMessages("s1");
    oldView.cancel();
    const newView = observeOlderMessages("s1");
    responses[0](Response.json({ messages: [msg(9)], oldest_loaded_seq: 9, has_more_messages: false }));
    expect(await oldView.done).toEqual({ state: "obsolete" });
    expect(useSessionStore.getState().oldestSeq.s1).toBe(10);
    expect(newView.current()).toBe(true);
    responses[1](Response.json({ messages: [msg(8), msg(9)], oldest_loaded_seq: 8, has_more_messages: true }));
    expect(await newView.done).toEqual({ state: "merged", added: 2 });
    expect(useSessionStore.getState().oldestSeq.s1).toBe(8);
    vi.unstubAllGlobals();
  });
});
