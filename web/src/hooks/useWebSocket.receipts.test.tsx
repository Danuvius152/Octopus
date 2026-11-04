import { Socket } from "../test-support/socket";
import { act, cleanup, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CommandNotices } from "../components/CommandNotices";
import { useSessionStore } from "../stores/sessionStore";
import { useWebSocket } from "./useWebSocket";
import { observeSession } from "../lib/hydrateSession";


beforeEach(() => {
  vi.useFakeTimers();
  Socket.reset(1);
  vi.stubGlobal("WebSocket", Socket);
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.getState().setToken("receipt-test");
  useSessionStore.setState({ controlCommands: {}, activeAttempts: { s: { request_id: "r", attempt_id: "a" } },
    dispatchGeneration: "g", dispatchStates: { s: { generation: "g", revision: 1, active_request_id: "r", paused: false, items: [] } } });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it("reports disconnected controls and correlates Stop with its attempt", async () => {
  const { result } = renderHook(useWebSocket);
  const socket = Socket.instances[0];
  socket.readyState = 3;
  expect((await result.current.interrupt("s")).accepted).toBe(false);
  expect(Object.values(useSessionStore.getState().controlCommands)[0].state).toBe("failed");
  socket.readyState = 1;
  const pending = result.current.interrupt("s");
  const payload = JSON.parse(socket.send.mock.calls[0][0]);
  expect(payload).toMatchObject({ request_id: "r", attempt_id: "a", type: "interrupt" });
  expect(useSessionStore.getState().controlCommands[payload.client_command_id].state).toBe("pending");
  act(() => socket.onmessage?.({ data: JSON.stringify({ type: "command_receipt", operation: "interrupt", session_id: "s",
    client_command_id: payload.client_command_id, accepted: true }) }));
  expect((await pending).accepted).toBe(true);
  expect(useSessionStore.getState().controlCommands[payload.client_command_id]).toBeUndefined();
});

it.each([
  ["g", "next-request", "a2"], ["g", "r", "a2"], ["g", "r", null], ["g", null, null], ["new-service", "r2", "a2"],
])("hydrates a coherent Stop target %s / %s / %s before sending the control", async (generation, requestId, attemptId) => {
  const dispatch = { generation, revision: 2, active_request_id: requestId, paused: false, items: [] };
  const active = attemptId === null ? null : { request_id: requestId, attempt_id: attemptId };
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ id: "s", messages: [], dispatch_state: dispatch, active_attempt: active })));
  const { result } = renderHook(useWebSocket);
  const targets: unknown[] = [];
  const unsubscribe = useSessionStore.subscribe((state) => {
    if (state.dispatchStates.s?.revision === 2) targets.push(state.activeAttempts.s ?? null);
  });
  try {
    await act(async () => { await observeSession("s", "detail").done; });
    expect(useSessionStore.getState().activeAttempts.s ?? null).toEqual(active);
    expect(targets.every((target) => JSON.stringify(target) === JSON.stringify(active))).toBe(true);
    const pending = result.current.interrupt("s");
    const socket = Socket.instances[0];
    const payload = JSON.parse(socket.send.mock.calls[0][0]);
    expect(payload).toMatchObject({ type: "interrupt", request_id: requestId, attempt_id: attemptId, generation, revision: 2 });
    act(() => socket.onmessage?.({ data: JSON.stringify({ type: "command_receipt", operation: "interrupt", session_id: "s",
      client_command_id: payload.client_command_id, accepted: true }) }));
    expect((await pending).accepted).toBe(true);
  } finally {
    unsubscribe();
  }
});

it("marks an unconfirmed Stop unknown and never replays it on reconnect", async () => {
  const { result } = renderHook(useWebSocket);
  const socket = Socket.instances[0];
  const pending = result.current.interrupt("s");
  act(() => { socket.readyState = 3; socket.onclose?.({ code: 1006 }); vi.advanceTimersByTime(3000); });
  expect((await pending).state).toBe("unknown");
  expect(Object.values(useSessionStore.getState().controlCommands)[0].state).toBe("unknown");
  expect(Socket.instances[1].send).not.toHaveBeenCalled();
});

it("clears a confirmed answer without leaving a dismissible notice", async () => {
  const { result } = renderHook(useWebSocket);
  const socket = Socket.instances[0];
  const pending = result.current.answerQuestion("s", "question", [{ selected: ["Yes"] }]);
  render(<CommandNotices sessionId="s" />);
  expect(screen.getByRole("status")).toHaveTextContent("awaiting confirmation");
  const payload = JSON.parse(socket.send.mock.calls[0][0]);
  expect(payload).toMatchObject({ type: "answer_question", question_id: "question", answers: [{ selected: ["Yes"] }] });
  expect(useSessionStore.getState().controlCommands[payload.client_command_id].state).toBe("pending");
  act(() => socket.onmessage?.({ data: JSON.stringify({ type: "command_receipt", operation: "answer_question", session_id: "s",
    client_command_id: payload.client_command_id, accepted: true }) }));
  expect((await pending).accepted).toBe(true);
  expect(useSessionStore.getState().controlCommands[payload.client_command_id]).toBeUndefined();
  expect(screen.queryByLabelText("Command confirmations")).toBeNull();
});

it("consumes the original snooze receipt without inventing a paused question projection", async () => {
  const { result } = renderHook(useWebSocket);
  const socket = Socket.instances[0];
  const pending = result.current.snoozeQuestion("s", "q", { clientCommandId: "pause-original",
    accountGeneration: useSessionStore.getState().accountGeneration, generation: "g", requestId: "r", attemptId: "a" });
  expect(useSessionStore.getState().controlCommands["pause-original"].state).toBe("pending");
  act(() => socket.onmessage?.({ data: JSON.stringify({ type: "command_receipt", operation: "snooze_question", session_id: "s",
    client_command_id: "pause-original", accepted: true }) }));
  expect(useSessionStore.getState().controlCommands["pause-original"]).toBeUndefined();
  expect((await pending).accepted).toBe(true);
  expect(useSessionStore.getState().pendingQuestions.s).toBeUndefined();
});

it("sends the answer owner's captured identity and rejects it after an identity change", async () => {
  const { result } = renderHook(useWebSocket);
  const target = { clientCommandId: "draft-command", accountGeneration: useSessionStore.getState().accountGeneration,
    generation: "g", requestId: "original-request", attemptId: "original-attempt" };
  const pending = result.current.answerQuestion("s", "question", [{ text: "once" }], target);
  const socket = Socket.instances[0];
  expect(JSON.parse(socket.send.mock.calls[0][0])).toMatchObject({ client_command_id: "draft-command",
    request_id: "original-request", attempt_id: "original-attempt", generation: "g" });
  act(() => socket.onmessage?.({ data: JSON.stringify({ type: "command_receipt", operation: "answer_question", session_id: "s",
    client_command_id: target.clientCommandId, accepted: true }) }));
  expect((await pending).accepted).toBe(true);
  act(() => useSessionStore.getState().setToken("another-account"));
  expect((await result.current.answerQuestion("s", "question", [{ text: "stale" }], target)).accepted).toBe(false);
  expect(socket.send).toHaveBeenCalledTimes(1);
});

it.each(["failed", "unknown"])("keeps a %s answer notice for review", async (state) => {
  const { result } = renderHook(useWebSocket);
  const socket = Socket.instances[0];
  const pending = result.current.answerQuestion("s", "question", [{ selected: ["Yes"] }]);
  render(<CommandNotices sessionId="s" />);
  const payload = JSON.parse(socket.send.mock.calls[0][0]);
  if (state === "failed") act(() => socket.onmessage?.({ data: JSON.stringify({ type: "command_receipt", operation: "answer_question",
    session_id: "s", client_command_id: payload.client_command_id, accepted: false, message: "The question is closed." }) }));
  else act(() => vi.advanceTimersByTime(15_000));
  expect((await pending).accepted).toBe(false);
  expect(useSessionStore.getState().controlCommands[payload.client_command_id].state).toBe(state);
  expect(screen.getByRole("alert")).toHaveTextContent(state === "failed" ? "The question is closed." : "Command confirmation timed out.");
  expect(screen.getByRole("button", { name: "Dismiss" })).toBeVisible();
});

it("retains the exact submission identity when confirming after a lost ACK", async () => {
  const { result } = renderHook(useWebSocket);
  const socket = Socket.instances[0];
  const pending = result.current.sendMessage("s", "perform once", undefined, undefined, "stable-submission");
  act(() => { socket.readyState = 3; socket.onclose?.({ code: 1006 }); vi.advanceTimersByTime(3000); });
  expect(await pending).toMatchObject({ accepted: false, state: "unknown", clientRequestId: "stable-submission" });
  const retried = result.current.sendMessage("s", "perform once", undefined, undefined, "stable-submission");
  const next = Socket.instances[1];
  expect(JSON.parse(next.send.mock.calls[0][0])).toEqual(JSON.parse(socket.send.mock.calls[0][0]));
  act(() => next.onmessage?.({ data: JSON.stringify({ type: "submission", session_id: "s",
    client_request_id: "stable-submission", accepted: true, request_id: "already-admitted" }) }));
  expect(await retried).toMatchObject({ accepted: true, requestId: "already-admitted" });
});

it("keeps live card completions across delayed reconnect responses and accepts new jobs", async () => {
  const waiting = new Map<string, (response: Response) => void>();
  vi.stubGlobal("fetch", vi.fn((url: string) => new Promise<Response>((resolve) => waiting.set(new URL(url).pathname, resolve))));
  useSessionStore.setState({ activeSessionId: "s", subagents: {}, bgTasks: {}, research: {} });
  renderHook(useWebSocket);
  const socket = Socket.instances[0];
  act(() => socket.onopen?.());
  const emit = (data: Record<string, unknown>) => socket.onmessage?.({ data: JSON.stringify({ session_id: "s", ...data }) });
  act(() => {
    emit({ type: "subagent", task_id: "child", tool_call_id: "call", status: "completed", summary: "fresh answer" });
    emit({ type: "bg_started", task_id: "bg", command: "build", started_at: "now" });
    emit({ type: "bg_completed", task_id: "bg", status: "completed", exit_code: 0, completed_at: "now" });
    emit({ type: "research_started", research_id: "research", question: "q" });
    emit({ type: "research_completed", research_id: "research", verified: 2 });
  });
  const finish = async (status: string, newJobs = false) => {
    await act(async () => {
      waiting.get("/api/sessions/s")!(Response.json({ messages: [], active_attempt: null,
        dispatch_state: { generation: "g", revision: 1, active_request_id: "r", paused: false, items: [] }, subagents: [
        { task_id: "child", tool_call_id: "call", status, summary: "snapshot" },
      ] }));
      waiting.get("/api/sessions/s/bg-tasks")!(Response.json(newJobs
        ? [{ id: "bg", status: "completed" }, { id: "bg-next", status }]
        : [{ id: "bg", status }]));
    });
    // Research publication waits for detail to establish its service identity.
    await act(async () => {
      waiting.get("/api/sessions/s/research")!(Response.json(newJobs
        ? [{ id: "research", question: "q", status: "completed", verified: 2 }, { id: "research-next", question: "Next question", status }]
        : [{ id: "research", question: "q", status, verified: 0 }]));
    });
  };
  await finish("running");
  expect(useSessionStore.getState().subagents.s.call.summary).toBe("fresh answer");
  expect(useSessionStore.getState().bgTasks.s[0].status).toBe("completed");
  expect(useSessionStore.getState().research.s[0].verified).toBe(2);
  act(() => socket.onopen?.());
  await finish("running", true);
  expect(useSessionStore.getState().subagents.s.call.status).toBe("completed");
  expect(useSessionStore.getState().bgTasks.s).toEqual([
    expect.objectContaining({ id: "bg", status: "completed" }), expect.objectContaining({ id: "bg-next", status: "running" }),
  ]);
  expect(useSessionStore.getState().research.s).toEqual([
    expect.objectContaining({ id: "research", status: "completed" }), expect.objectContaining({ id: "research-next", status: "running" }),
  ]);
});

it("keeps recovery provenance on a new identity and on lost-ACK confirmation", async () => {
  const { result } = renderHook(useWebSocket);
  const original = Socket.instances[0];
  const pending = result.current.sendMessage("s", "restored", undefined, undefined, "new-request", "withdrawn-request");
  expect(JSON.parse(original.send.mock.calls[0][0])).toMatchObject({ client_request_id: "new-request", recovery_of_client_id: "withdrawn-request" });
  act(() => { original.readyState = 3; original.onclose?.({ code: 1006 }); vi.advanceTimersByTime(3000); });
  expect((await pending).state).toBe("unknown");
  const next = Socket.instances[1];
  expect(next.send).not.toHaveBeenCalled();
  const confirmation = result.current.sendMessage("s", "restored", undefined, undefined, "new-request", "withdrawn-request");
  expect(JSON.parse(next.send.mock.calls[0][0])).toEqual(JSON.parse(original.send.mock.calls[0][0]));
  act(() => next.onmessage?.({ data: JSON.stringify({ type: "submission", session_id: "s", client_request_id: "new-request", accepted: true,
    request_id: "durable-new-request", execution: { state: "withdrawn", recoverable_input: true,
      delivery_state: "none", state_version: 1, execution_owner_request_id: "durable-new-request" } }) }));
  expect(await confirmation).toMatchObject({ accepted: true, execution: { state: "withdrawn" } });
});
