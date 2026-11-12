import { beforeEach, expect, it } from "vitest";
import { handleWsMessage } from "../hooks/useWebSocket";
import { useSessionStore } from "../stores/sessionStore";
import { applyTranscript } from "./transcript";
import replay from "./recordingReplay.json";
import steering from "./steeringReplay.json";

// The backend regression compares these projections with real get_session
// snapshots and the recovered broadcast, including a lost commit acknowledgement.
const session = replay.event.session_id;
beforeEach(() => useSessionStore.setState({ messages: {}, lastAppliedSeq: {},
  oldestSeq: {}, hasMoreMessages: {}, streamingText: {} }));

it("keeps the real Claude backpressure history through replay, reconnect and a newer request", () => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  const session = steering.output.session_id;
  const initial = { ...steering.initial, oldest_loaded_seq: 0, has_more_messages: false };
  applyTranscript(session, initial);
  handleWsMessage({ type: "attempt_started", session_id: session, request_id: "stdin-original", attempt_id: "original-attempt" });
  handleWsMessage(steering.delta);
  expect(useSessionStore.getState().streamingText[session]).toBe("Output partial");
  handleWsMessage(steering.output);
  expect(useSessionStore.getState().streamingText[session]).toBeUndefined();
  // A read begun before the saved steer returns while stdout has moved on.
  applyTranscript(session, initial);
  handleWsMessage(steering.steer);
  handleWsMessage(steering.output);
  handleWsMessage(steering.steer);
  expect(useSessionStore.getState().messages[session].map((row) => [row.seq, row.content])).toEqual([
    [0, "First"], [1, "Ready"], [2, "Saved correction"], [3, "Output while stdin is blocked"],
  ]);
  expect(useSessionStore.getState().lastAppliedSeq[session]).toBe(1);
  handleWsMessage({ type: "attempt_started", session_id: session, request_id: "new-request", attempt_id: "new-attempt" });
  handleWsMessage({ type: "assistant_delta", session_id: session, content: "New request in progress",
    runtime_metadata: { request_id: "new-request", attempt_id: "new-attempt" } });
  const terminal = { type: "result", session_id: session, seq: 4, request_id: "stdin-original", attempt_id: "original-attempt",
    content: { kind: "outcome", operation: "message", request_id: "stdin-original", status: "success" } };
  handleWsMessage(terminal);
  applyTranscript(session, { ...steering.during, oldest_loaded_seq: 0, has_more_messages: false });
  handleWsMessage(terminal);
  handleWsMessage({ type: "question_answer", session_id: session, seq: 6, question_id: "old-form", content: "Answered" });
  handleWsMessage({ type: "question_request", session_id: session, seq: 5, question_id: "old-form", questions: [{ question: "Choose" }] });
  applyTranscript(session, initial);
  const state = useSessionStore.getState();
  expect(state.messages[session].map((row) => row.seq)).toEqual([0, 1, 2, 3, 4, 5, 6]);
  expect(state.streamingText[session]).toBe("New request in progress");
  expect(state.activeAttempts[session]).toEqual({ request_id: "new-request", attempt_id: "new-attempt" });
  expect(state.pendingQuestions[session] || []).toEqual([]);
  expect(state.lastAppliedSeq[session]).toBe(3);
  expect(state.oldestSeq[session]).toBe(0);
  expect(state.hasMoreMessages[session]).toBe(false);
  handleWsMessage({ ...terminal, seq: 7, request_id: "new-request", attempt_id: "new-attempt",
    content: { ...terminal.content, request_id: "new-request" } });
  expect(useSessionStore.getState().streamingText[session]).toBeUndefined();
  expect(useSessionStore.getState().activeAttempts[session]).toBeUndefined();
});

it.each(["before-save", "lost-ack", "broadcast-before-snapshot"])(
  "renders the recovered terminal once across %s", (mode) => {
    if (mode === "broadcast-before-snapshot") handleWsMessage(replay.event);
    applyTranscript(session, mode === "lost-ack" ? replay.after : replay.before);
    handleWsMessage(replay.event);
    handleWsMessage(replay.event);
    expect(useSessionStore.getState().messages[session]).toHaveLength(1);
    applyTranscript(session, replay.after);
    handleWsMessage(replay.event);
    const messages = useSessionStore.getState().messages[session];
    expect(messages).toHaveLength(1);
    expect(messages[0].seq).toBe(0);
    expect(messages[0].outcome).toMatchObject({ status: "success", request_id: "recording-replay" });
  }
);

it("preserves native outcome and final references through event replay and refresh", () => {
  const content = { kind: "outcome", operation: "message", request_id: "recording-replay", status: "interrupted",
    native_status: "completed", final_answer: { thread_id: "root", turn_id: "turn", message_seqs: [] } };
  handleWsMessage({ ...replay.event, content, is_error: true });
  const live = useSessionStore.getState().messages[session][0].outcome;
  expect(live).toMatchObject({ status: "interrupted", native_status: "completed", final_answer: content.final_answer });
  applyTranscript(session, { ...replay.after,
    messages: replay.after.messages.map((message) => ({ ...message, content, is_error: true })) });
  expect(useSessionStore.getState().messages[session][0].outcome).toEqual(live);
});
