import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionStore } from "../stores/sessionStore";
import { Socket } from "../test-support/socket";
import { QuestionPrompt } from "../components/QuestionPrompt";
import recording from "../lib/recordingReplay.json";
import { handleWsMessage, useWebSocket } from "./useWebSocket";

beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("rejects malformed and unknown high-sequence frames before changing the snapshot baseline", () => {
  handleWsMessage({ type: "assistant_text", session_id: "s", seq: 10, content: 42 });
  handleWsMessage({ type: "future_extension", session_id: "s", seq: 10 });
  expect(useSessionStore.getState().lastAppliedSeq.s).toBeUndefined();
  expect(useSessionStore.getState().messages.s).toBeUndefined();
  handleWsMessage({ type: "assistant_text", session_id: "s", seq: 10, content: "Valid original output", future: true });
  expect(useSessionStore.getState().lastAppliedSeq.s).toBe(10);
  expect(useSessionStore.getState().messages.s[0].content).toBe("Valid original output");
});

it.each([
  { ...recording.event, content: { ...recording.event.content, request_id: 42 } },
  { ...recording.event, request_id: "another-request" },
  { ...recording.event, content: { ...recording.event.content, status: "invented" } },
  { ...recording.event, content: { ...recording.event.content, final_answer: { thread_id: 42, turn_id: "turn" } } },
  { type: "recovery", content: { kind: "recovery", request_id: 42, phase: "retrying", code: "retry", message: "Retry" } },
  { type: "recovery", content: { kind: "recovery", request_id: "r", phase: "invented", code: "retry", message: "Retry" } },
])("rejects malformed domain content before consuming the sequence: %j", (event) => {
  useSessionStore.setState({ streamingText: { s: "Original stream" } });
  const before = useSessionStore.getState();
  handleWsMessage({ ...event, session_id: "s", seq: 10 });
  expect(useSessionStore.getState()).toBe(before);
  handleWsMessage({ ...recording.event, session_id: "s", seq: 10 });
  expect(useSessionStore.getState().lastAppliedSeq.s).toBe(10);
  expect(useSessionStore.getState().messages.s[0].outcome?.status).toBe("success");
});

it("leaves malformed control and submission receipts pending until their valid original reply arrives", async () => {
  Socket.reset(1);
  vi.stubGlobal("WebSocket", Socket);
  useSessionStore.getState().setToken("validation-test");
  useSessionStore.setState({ dispatchGeneration: "g", activeAttempts: { s: { request_id: "r", attempt_id: "a" } },
    dispatchStates: { s: { generation: "g", revision: 1, paused: false, active_request_id: "r", items: [] } } });
  const { result } = renderHook(useWebSocket);
  const socket = Socket.instances[0];
  const control = result.current.interrupt("s");
  const sent = JSON.parse(socket.send.mock.calls[0][0]);
  const submission = result.current.sendMessage("s", "Original input", undefined, undefined, "original");
  const settled = vi.fn();
  void control.then(settled);
  void submission.then(settled);
  const receipt = { type: "command_receipt", session_id: "s", client_command_id: sent.client_command_id, operation: "interrupt" };
  const admitted = { type: "submission", session_id: "s", client_request_id: "original" };
  act(() => {
    socket.onmessage?.({ data: JSON.stringify({ ...receipt, accepted: "yes" }) });
    socket.onmessage?.({ data: JSON.stringify({ ...admitted, accepted: true, request_id: "r", execution: { phase: 9 } }) });
    for (const request_id of [undefined, null, ""]) {
      socket.onmessage?.({ data: JSON.stringify({ ...admitted, accepted: true, request_id }) });
    }
  });
  await Promise.resolve();
  expect(settled).not.toHaveBeenCalled();
  act(() => {
    socket.onmessage?.({ data: JSON.stringify({ ...receipt, accepted: true }) });
    socket.onmessage?.({ data: JSON.stringify({ ...admitted, accepted: true, request_id: "r" }) });
  });
  expect((await control).accepted).toBe(true);
  expect(await submission).toMatchObject({ accepted: true, requestId: "r" });
});

it("rejects a malformed archive before changing generation, membership, lists or fetching", () => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  const before = useSessionStore.getState();
  handleWsMessage({ type: "session_archived", old_session_id: "s", new_session_id: 7,
    dispatch_state: { generation: "new-service", revision: 100, paused: false, active_request_id: null, items: [] } });
  expect(useSessionStore.getState()).toBe(before);
  expect(fetch).not.toHaveBeenCalled();
});

it("keeps omitted task patch fields and applies explicit nullable clears", () => {
  handleWsMessage({ type: "bg_started", session_id: "s", task_id: "bg", command: "command", started_at: "now" });
  handleWsMessage({ type: "bg_delivery", session_id: "s", task_id: "bg", delivery_disposition: "unknown", delivery_error: "Original diagnostic" });
  handleWsMessage({ type: "bg_delivery", session_id: "s", task_id: "bg", delivery_error: null });
  expect(useSessionStore.getState().bgTasks.s[0]).toMatchObject({ delivery_disposition: "unknown", delivery_error: null });
  handleWsMessage({ type: "bg_delivery", session_id: "s", task_id: "bg", delivery_disposition: null });
  expect(useSessionStore.getState().bgTasks.s[0].delivery_disposition).toBeNull();

  handleWsMessage({ type: "research_started", session_id: "s", research_id: "report", question: "Question" });
  handleWsMessage({ type: "research_progress", session_id: "s", research_id: "report", phase: "search", detail: "Original detail" });
  handleWsMessage({ type: "research_progress", session_id: "s", research_id: "report", counts: { sources: 2 } });
  expect(useSessionStore.getState().research.s[0]).toMatchObject({ phase: "search", detail: "Original detail", counts: { sources: 2 } });
  handleWsMessage({ type: "research_progress", session_id: "s", research_id: "report", phase: null });
  expect(useSessionStore.getState().research.s[0]).toMatchObject({ phase: null, detail: "Original detail" });
});

it("renders and answers a producer's free-text question without an options list", async () => {
  handleWsMessage({ type: "question_request", session_id: "s", question_id: "form", questions: [{ question: "Which directory?" }] });
  const onSubmit = vi.fn();
  render(<QuestionPrompt question={useSessionStore.getState().pendingQuestions.s[0]} onSubmit={onSubmit} />);
  expect(screen.getByText("Which directory?")).toBeVisible();
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "/work" } });
  fireEvent.click(screen.getByRole("button", { name: "Submit" }));
  await waitFor(() => expect(onSubmit).toHaveBeenCalledWith("form", [{ text: "/work", selected: undefined }]));
});
