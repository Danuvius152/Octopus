import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionStore } from "../stores/sessionStore";
import { useWebSocket } from "./useWebSocket";

class Socket {
  static OPEN = 1;
  static CONNECTING = 0;
  static instances: Socket[] = [];
  readyState = 1;
  send = vi.fn();
  onopen: (() => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() { Socket.instances.push(this); }
  close() { this.readyState = 3; }
}

beforeEach(() => {
  vi.useFakeTimers();
  Socket.instances = [];
  vi.stubGlobal("WebSocket", Socket);
  useSessionStore.getState().setToken("receipt-test");
  useSessionStore.setState({ controlCommands: {}, activeAttempts: { s: { request_id: "r", attempt_id: "a" } },
    dispatchStates: { s: { generation: "g", revision: 1, active_request_id: "r", paused: false, items: [] } } });
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

it("marks an unconfirmed Stop unknown and never replays it on reconnect", async () => {
  const { result } = renderHook(useWebSocket);
  const socket = Socket.instances[0];
  const pending = result.current.interrupt("s");
  act(() => { socket.readyState = 3; socket.onclose?.({ code: 1006 }); vi.advanceTimersByTime(3000); });
  expect((await pending).state).toBe("unknown");
  expect(Object.values(useSessionStore.getState().controlCommands)[0].state).toBe("unknown");
  expect(Socket.instances[1].send).not.toHaveBeenCalled();
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

it("keeps live card completions across delayed reconnect responses and accepts a later round", async () => {
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
    emit({ type: "bg_completed", task_id: "bg", status: "completed", exit_code: 0 });
    emit({ type: "research_started", research_id: "research", question: "q" });
    emit({ type: "research_completed", research_id: "research", verified: 2 });
  });
  const finish = async (status: string) => act(async () => {
    waiting.get("/api/sessions/s")!(Response.json({ messages: [],
      dispatch_state: { generation: "g", revision: 1, active_request_id: "r", paused: false, items: [] }, subagents: [
      { task_id: "child", tool_call_id: "call", status, summary: "snapshot" },
    ] }));
    waiting.get("/api/sessions/s/bg-tasks")!(Response.json([{ id: "bg", status }]));
    waiting.get("/api/sessions/s/research")!(Response.json([{ id: "research", question: "q", status, verified: 0 }]));
  });
  await finish("running");
  expect(useSessionStore.getState().subagents.s.call.summary).toBe("fresh answer");
  expect(useSessionStore.getState().bgTasks.s[0].status).toBe("completed");
  expect(useSessionStore.getState().research.s[0].verified).toBe(2);
  act(() => socket.onopen?.());
  await finish("running");
  expect(useSessionStore.getState().subagents.s.call.status).toBe("running");
  expect(useSessionStore.getState().bgTasks.s[0].status).toBe("running");
  expect(useSessionStore.getState().research.s[0].status).toBe("running");
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
    request_id: "durable-new-request", execution: { state: "withdrawn", recoverable_input: true } }) }));
  expect(await confirmation).toMatchObject({ accepted: true, execution: { state: "withdrawn" } });
});
