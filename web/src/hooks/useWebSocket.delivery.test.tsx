import { Socket } from "../test-support/socket";
import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { useSessionStore } from "../stores/sessionStore";
import { BgTaskChip } from "../components/BgTaskChip";
import { SessionAuxiliaryNotice } from "../components/SessionAuxiliaryNotice";
import { loadBgTaskSnapshot } from "../lib/hydrateSession";
import { deferred } from "../test-support/deferred";
import { useWebSocket } from "./useWebSocket";


afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("reconnects after overflow and reloads the result, tasks, questions and paused queue", async () => {
  vi.useFakeTimers();
  Socket.reset(0);
  vi.stubGlobal("WebSocket", Socket);
  const sid = "overflow-session";
  const outcome = {
    kind: "outcome", operation: "message", request_id: "selected-request", status: "completed", message: "Finished",
    recovery: { transient: 0, idle: 0, stale_session: 0, premature_exit: 0 },
  };
  const dispatch = {
    generation: "service-current", revision: 4, paused: true, active_request_id: null, active_operation: null,
    items: [{ request_id: "pending-bg", operation: "message" as const, source: "bg", summary: "Queued result", priority: false }],
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
    if (new URL(url).pathname === `/api/sessions/${sid}`) {
      body = {
        id: sid, status: "idle", messages: [{ role: "system", type: "result", seq: 2, content: outcome }],
        next_message_seq: 3, dispatch_state: dispatch, active_attempt: null, pending_questions: [question], subagents: [],
      };
    } else if (url.endsWith("/bg-tasks")) body = [task];
    else if (url.includes("/api/sessions") && !url.includes(`/api/sessions/${sid}`)) body = [{ id: sid, status: "idle", archived: false }];
    else if (url.endsWith("/delegations")) body = [delegation];
    return { ok: true, json: async () => body };
  }));
  useSessionStore.setState({
    token: "account-bearer", activeSessionId: sid, connected: false, sessions: [],
    messages: {}, lastAppliedSeq: {}, oldestSeq: {}, streamingText: { [sid]: { content: "incomplete live text" } },
    dispatchStates: {}, dispatchGeneration: null, retiredDispatchGenerations: [], dispatchUnavailable: {},
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
    expect(state.dispatchStates[sid].items[0].summary).toBe("Queued result");
    expect(state.pendingQuestions[sid]).toEqual([question]);
    expect(state.bgTasks[sid]).toEqual([task]);
    expect(state.delegations[sid]).toEqual([delegation]);
    act(() => reconnected.onmessage?.({ data: JSON.stringify({ type: "result", session_id: sid, seq: 2, content: outcome }) }));
    expect(useSessionStore.getState().messages[sid].filter((m) => m.type === "result")).toHaveLength(1);
  } finally {
    hook.unmount();
  }
});

it.each([["mounted", false], ["remounted", false], ["mounted", true]] as const)(
  "rechecks an omitted running task after reconnect with the collapsed card %s (detail fails=%s)", async (mount, fails) => {
  vi.useFakeTimers();
  Socket.reset(0);
  vi.stubGlobal("WebSocket", Socket);
  const sid = "reconnect-background-session";
  const taskId = "background-before-reconnect";
  const dispatch = { generation: "same-service", revision: 0, paused: false, items: [], active_request_id: null, active_operation: null };
  const running = { id: taskId, session_id: sid, status: "running", command: "collect output", stdout: "", stderr: "",
    started_at: "2026-11-19T10:00:00Z", completed_at: null, exit_code: null };
  const recent = Array.from({ length: 200 }, (_, i) => ({ ...running, id: `recent-${i}`, status: "completed", started_at: "2026-11-19T11:00:00Z" }));
  const detail = deferred<Response>();
  const retry = deferred<Response>();
  const taskDetails = vi.fn(() => retry.promise).mockReturnValueOnce(detail.promise);
  const taskList = vi.fn(() => Response.json(reconnected ? recent : [running]));
  let reconnected = false;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const path = new URL(url).pathname;
    if (path.endsWith(`/bg-tasks/${taskId}`)) return taskDetails();
    if (path.endsWith("/bg-tasks")) return taskList();
    if (path === `/api/sessions/${sid}`) return Response.json({ id: sid, status: "idle", messages: [], dispatch_state: dispatch });
    if (path === "/api/sessions") return Response.json([{ id: sid, status: "idle", archived: false }]);
    return Response.json([]);
  }));
  useSessionStore.getState().setToken("reconnect-background-account");
  useSessionStore.setState({ activeSessionId: sid, dispatchGeneration: dispatch.generation, dispatchStates: {}, bgTasks: {} });

  const hook = renderHook(useWebSocket);
  const card = () => render(<><SessionAuxiliaryNotice sessionId={sid} /><BgTaskChip sessionId={sid} taskId={taskId} /></>);
  try {
    const first = Socket.instances[0];
    await act(async () => { first.readyState = 1; first.onopen?.(); });
    const original = card();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeVisible();
    expect(taskDetails).not.toHaveBeenCalled();
    if (mount === "remounted") original.unmount();
    act(() => {
      first.readyState = 3;
      first.onclose?.({ code: 1006 });
      vi.advanceTimersByTime(3000);
    });
    reconnected = true;
    const second = Socket.instances[1];
    await act(async () => { second.readyState = 1; second.onopen?.(); });
    if (mount === "remounted") card();
    expect(taskDetails).toHaveBeenCalledTimes(1);
    await act(async () => { await loadBgTaskSnapshot(sid); });
    expect(taskDetails).toHaveBeenCalledTimes(1);

    if (fails) {
      await act(async () => detail.resolve(new Response("Unavailable", { status: 503 })));
      expect(screen.getByRole("alert")).toHaveTextContent("Could not refresh background task details.");
      expect(screen.getByText("bg · running")).toBeVisible();
      const listCalls = taskList.mock.calls.length;
      fireEvent.click(screen.getByRole("button", { name: "Retry background task details" }));
      expect(taskList).toHaveBeenCalledTimes(listCalls);
      expect(taskDetails).toHaveBeenCalledTimes(2);
    }
    await act(async () => (fails ? retry : detail).resolve(Response.json({ ...running, status: "completed", exit_code: 0,
      completed_at: "2026-11-19T10:01:00Z", stdout: "Saved stdout", stderr: "Saved stderr" })));
    expect(screen.getByRole("button", { name: "Expand bg task" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("bg · completed")).toBeVisible();
    expect(screen.getByText("Saved stdout")).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    expect(useSessionStore.getState().bgTasks[sid].find(task => task.id === taskId)?.stderr).toBe("Saved stderr");
    expect(taskDetails).toHaveBeenCalledTimes(fails ? 2 : 1);
  } finally {
    act(() => {
      hook.unmount();
      useSessionStore.getState().setToken("");
    });
  }
});
