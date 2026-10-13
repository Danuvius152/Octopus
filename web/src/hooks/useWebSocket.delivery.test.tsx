import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { useSessionStore } from "../stores/sessionStore";
import { useWebSocket } from "./useWebSocket";

class Socket {
  static OPEN = 1;
  static CONNECTING = 0;
  static instances: Socket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() { Socket.instances.push(this); }
  close() { this.readyState = 3; }
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("reconnects after overflow and reloads the result, tasks, questions and paused queue", async () => {
  vi.useFakeTimers();
  Socket.instances = [];
  vi.stubGlobal("WebSocket", Socket);
  const sid = "overflow-session";
  const outcome = {
    kind: "outcome", request_id: "selected-request", status: "completed", message: "Finished",
    recovery: { transient: 0, idle: 0, stale_session: 0, premature_exit: 0 },
  };
  const dispatch = {
    generation: "service-current", revision: 4, paused: true, active_request_id: null,
    items: [{ request_id: "pending-bg", source: "bg", summary: "Queued result", priority: false }],
  };
  const question = { question_id: "live-question", questions: [{ header: "Next", question: "Continue?", options: [] }] };
  const task = { id: "exact-bg-task", session_id: sid, status: "completed", command: "run tests" };
  const delegation = {
    delegation_id: "child", sub_session_id: "child", parent_session_id: sid,
    target_agent_id: "worker", target_agent_name: "Worker", request: "Check", work_id: "exact-round",
    state: "completed", created_at: "2026-10-14T00:00:00Z", finished_at: "2026-10-14T00:01:00Z", error: null,
  };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    let body: unknown = [];
    if (url.endsWith(`/api/sessions/${sid}`)) {
      body = {
        id: sid, status: "idle", messages: [{ role: "system", type: "result", seq: 2, content: outcome }],
        next_message_seq: 3, dispatch_state: dispatch, pending_questions: [question], subagents: [],
      };
    } else if (url.endsWith("/bg-tasks")) body = [task];
    else if (url.endsWith("/delegations")) body = [delegation];
    return { ok: true, json: async () => body };
  }));
  useSessionStore.setState({
    token: "account-bearer", activeSessionId: sid, connected: false, sessions: [],
    messages: {}, lastAppliedSeq: {}, oldestSeq: {}, streamingText: { [sid]: "incomplete live text" },
    dispatchStates: {}, dispatchGeneration: null, retiredDispatchGenerations: [], pendingQueue: {},
    pendingQuestions: {}, closedQuestionIds: {}, subagents: {}, bgTasks: {}, delegations: {}, research: {},
  });
  const hook = renderHook(() => useWebSocket());
  try {
    const first = Socket.instances[0];
    act(() => {
      first.readyState = 3;
      first.onclose?.({ code: 1013 });
      vi.advanceTimersByTime(3000);
    });
    expect(useSessionStore.getState().token).toBe("account-bearer");
    expect(Socket.instances).toHaveLength(2);
    const reconnected = Socket.instances[1];
    await act(async () => {
      reconnected.readyState = 1;
      reconnected.onopen?.();
      await Promise.resolve();
      await Promise.resolve();
    });
    const state = useSessionStore.getState();
    expect(state.connected).toBe(true);
    expect(state.messages[sid].filter((m) => m.type === "result")).toHaveLength(1);
    expect(state.messages[sid][0].outcome?.request_id).toBe("selected-request");
    expect(state.dispatchStates[sid]).toEqual(dispatch);
    expect(state.pendingQueue[sid]).toEqual(["Queued result"]);
    expect(state.pendingQuestions[sid]).toEqual([question]);
    expect(state.bgTasks[sid]).toEqual([task]);
    expect(state.delegations[sid]).toEqual([delegation]);
    act(() => reconnected.onmessage?.({ data: JSON.stringify({ type: "result", session_id: sid, seq: 2, content: outcome }) }));
    expect(useSessionStore.getState().messages[sid].filter((m) => m.type === "result")).toHaveLength(1);
  } finally {
    hook.unmount();
  }
});
