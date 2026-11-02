import { Socket } from "../test-support/socket";
import { act, cleanup, render, renderHook, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useSessionStore, type Delegation, type Message, type SessionInfo } from "../stores/sessionStore";
import { useWebSocket } from "../hooks/useWebSocket";
import { AgentDelegationRequestCard } from "./AgentDelegationRequestCard";

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it("resumes exact-round polling after a real reconnect invalidates its in-flight read", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", Socket);
  Socket.reset(1);
  const metadata = { request_id: "request", attempt_id: "attempt", tool_call_id: "call" };
  const tool = { role: "assistant", type: "tool_use", seq: 0, tool_name: "mcp__ask_agent__ask", tool_use_id: "provider", runtime_metadata: metadata } as Message;
  const result = { role: "tool", type: "tool_result", seq: 1, tool_use_id: "provider", runtime_metadata: { ...metadata,
    receipt: { kind: "delegation", delegation_id: "child", work_id: "round" } } } as Message;
  const row = { id: "parent", archived: false } as SessionInfo;
  const round = { delegation_id: "child", work_id: "round", parent_session_id: "parent", state: "running" } as Delegation;
  const dispatch = { generation: "service", revision: 0, paused: false, active_request_id: null, items: [] };
  useSessionStore.setState({ ...useSessionStore.getInitialState(), token: "existing-account", dispatchGeneration: "service",
    activeSessionId: "parent", sessions: [row], messages: { parent: [tool, result] }, delegations: { parent: [round] } }, true);
  const waiting: ((response: Response) => void)[] = [];
  let complete = false;
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (url.endsWith("/delegations")) {
      reads++;
      return complete ? Promise.resolve(Response.json([{ ...round, state: "completed" }])) : new Promise<Response>((resolve) => waiting.push(resolve));
    }
    return Promise.resolve(Response.json(url.endsWith("/api/sessions/parent")
      ? { ...row, dispatch_state: dispatch, messages: [tool, result], next_message_seq: 2 }
      : url.endsWith("/api/sessions") ? [row] : []));
  }));
  renderHook(useWebSocket);
  render(<AgentDelegationRequestCard sessionId="parent" message={tool} agentName="Vera" request="Original request" files={[]} />);
  await act(async () => Socket.instances[0].onopen?.());
  act(() => { Socket.instances[0].readyState = 3; Socket.instances[0].onclose?.({ code: 1006 }); });
  await act(async () => { for (const resolve of waiting.splice(0)) resolve(Response.json([round])); });
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  await act(async () => {
    Socket.instances[1].onopen?.();
    for (let index = 0; index < 20; index++) {
      for (const resolve of waiting.splice(0)) resolve(Response.json([round]));
      await Promise.resolve();
    }
  });
  expect(screen.getByText("running")).toBeTruthy();
  const before = reads;
  complete = true;
  await act(async () => { await vi.advanceTimersByTimeAsync(2500); });
  expect(reads).toBeGreaterThan(before);
  expect(screen.getByText("completed")).toBeTruthy();
});
