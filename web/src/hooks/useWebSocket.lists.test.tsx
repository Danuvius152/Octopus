import { Socket } from "../test-support/socket";
import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useSessionStore } from "../stores/sessionStore";
import type { SessionInfo } from "../stores/sessionStore";
import { applications } from "../lib/applicationList";
import { refreshAgents } from "../lib/refreshAgents";
import { deferred } from "../test-support/deferred";
import type { Agent } from "../stores/sessionStore";
import { useWebSocket } from "./useWebSocket";


afterEach(() => {
  useSessionStore.getState().setToken("");
  vi.useRealTimers(); vi.unstubAllGlobals();
});

it.each([false, true])("reconnects Agent coverage lazily and retires overlapping old snapshots (All loaded=%s)", async (loaded) => {
  vi.useFakeTimers(); Socket.reset(0); vi.stubGlobal("WebSocket", Socket);
  useSessionStore.setState({ ...useSessionStore.getInitialState(), token: "agent-bearer",
    accountGeneration: useSessionStore.getState().accountGeneration + 1, dispatchGeneration: "service" });
  const row = { id: "agent", name: "Original", archived: false, pinned: true } as Agent;
  const retired = { ...row, archived: true };
  const oldRead = deferred<Response>();
  let hold = false;
  let current = [retired];
  const fetcher = vi.fn(async (url: string) => {
    if (!url.includes("/api/agents")) return Response.json([]);
    if (hold) return oldRead.promise;
    return Response.json(url.includes("include_archived=true") ? current : current.filter((agent) => !agent.archived));
  });
  vi.stubGlobal("fetch", fetcher);
  if (loaded) await refreshAgents(undefined, "all");
  const hook = renderHook(() => useWebSocket());
  try {
    const first = Socket.instances[0];
    await act(async () => { first.readyState = 1; first.onopen?.(); });
    const before = fetcher.mock.calls.filter(([url]) => url.includes("include_archived=true")).length;
    expect(before > 0).toBe(loaded);
    hold = true;
    let loading!: Promise<void>;
    act(() => { loading = refreshAgents(undefined, loaded ? "all" : "live"); });
    act(() => { first.readyState = 3; first.onclose?.({ code: 1013 }); vi.advanceTimersByTime(3000); });
    hold = false;
    current = [{ ...row, name: "Restored" }];
    const second = Socket.instances[1];
    await act(async () => { second.readyState = 1; second.onopen?.(); });
    expect(fetcher.mock.calls.filter(([url]) => url.includes("include_archived=true")).length > before).toBe(loaded);
    await act(async () => { oldRead.resolve(Response.json(loaded ? [retired] : [row])); await loading; });
    expect(useSessionStore.getState().agents.map((agent) => agent.name)).toEqual(["Restored"]);
    expect(useSessionStore.getState().archivedAgents).toEqual([]);
    expect(useSessionStore.getState().dispatchGeneration).toBe("service");
  } finally { hook.unmount(); }
});

it("reconnects both domain lists and loaded archives without another change notification", async () => {
  vi.useFakeTimers(); Socket.reset(0);
  vi.stubGlobal("WebSocket", Socket);
  useSessionStore.setState({ token: "list-bearer", accountGeneration: useSessionStore.getState().accountGeneration + 1,
    activeSessionId: null, dispatchGeneration: "service", connected: false });
  let version = 1;
  const fetch = vi.fn(async (url: string) => new Response(JSON.stringify(
    url.endsWith("/api/schedules") ? [{ id: `schedule-${version}` }] : [])));
  vi.stubGlobal("fetch", fetch);
  await applications.refresh("archived");
  const hook = renderHook(() => useWebSocket());
  try {
    const first = Socket.instances[0];
    await act(async () => { first.readyState = 1; first.onopen?.(); });
    const initialSchedules = fetch.mock.calls.filter(([url]) => url.endsWith("/api/schedules")).length;
    const initialArchives = fetch.mock.calls.filter(([url]) => url.includes("archived=true")).length;
    act(() => { first.readyState = 3; first.onclose?.({ code: 1013 }); vi.advanceTimersByTime(3000); });
    version = 2;
    const second = Socket.instances[1];
    await act(async () => { second.readyState = 1; second.onopen?.(); });
    expect(fetch.mock.calls.filter(([url]) => url.endsWith("/api/schedules")).length).toBeGreaterThan(initialSchedules);
    expect(fetch.mock.calls.filter(([url]) => url.includes("archived=true")).length).toBeGreaterThan(initialArchives);
    expect(useSessionStore.getState().schedules.map((row) => row.id)).toEqual(["schedule-2"]);
    act(() => first.onmessage?.({ data: JSON.stringify({ type: "application_updated", application: { id: "old", status: "failed" } }) }));
    expect(useSessionStore.getState().applications).toEqual([]);
    expect(useSessionStore.getState().unseenFailedApplications).toEqual([]);
  } finally { hook.unmount(); }
});

it("keeps a newer idle observation when the reconnect's running snapshot arrives late", async () => {
  Socket.reset(0);
  vi.stubGlobal("WebSocket", Socket);
  const running = { id: "s", name: "Session", status: "running", archived: false } as SessionInfo;
  useSessionStore.setState({ token: "session-bearer", accountGeneration: useSessionStore.getState().accountGeneration + 1,
    activeSessionId: null, dispatchGeneration: "service", connected: false });
  useSessionStore.setState({ sessions: [running] });
  const replies: ((response: Response) => void)[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("/api/sessions")
    ? new Promise<Response>((resolve) => replies.push(resolve)) : Promise.resolve(Response.json([]))));
  const hook = renderHook(() => useWebSocket());
  try {
    const socket = Socket.instances[0];
    await act(async () => { socket.readyState = 1; socket.onopen?.(); });
    act(() => socket.onmessage?.({ data: JSON.stringify({ type: "status", session_id: "s", status: "idle" }) }));
    await act(async () => { replies[0](Response.json([running])); });
    expect(useSessionStore.getState().sessions[0].status).toBe("idle");
    expect(replies).toHaveLength(2);
    await act(async () => { replies[1](Response.json([{ ...running, status: "idle" }])); });
    expect(useSessionStore.getState().sessions[0].status).toBe("idle");
  } finally { hook.unmount(); }
});

it("deletes the selected session and rejects the list and detail already in flight", async () => {
  Socket.reset(0); vi.stubGlobal("WebSocket", Socket);
  const deleted = { id: "deleted", name: "Deleted", status: "idle", archived: false } as SessionInfo;
  useSessionStore.setState({ token: "deletion-bearer", accountGeneration: useSessionStore.getState().accountGeneration + 1,
    dispatchGeneration: "service", connected: false });
  useSessionStore.setState({ sessions: [deleted], activeSessionId: deleted.id,
    pendingQuestions: { deleted: [{ question_id: "q", questions: [{ question: "Choose?" }] }] } });
  const replies: ((response: Response) => void)[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("/api/sessions")
    ? new Promise<Response>((resolve) => replies.push(resolve)) : Promise.resolve(Response.json([]))));
  const hook = renderHook(() => useWebSocket());
  try {
    const socket = Socket.instances[0];
    await act(async () => { socket.readyState = 1; socket.onopen?.(); });
    const deletion = JSON.stringify({ type: "session_deleted", session_id: deleted.id });
    act(() => { socket.onmessage?.({ data: deletion }); socket.onmessage?.({ data: deletion }); });
    expect(useSessionStore.getState().sessions).toEqual([]);
    expect(useSessionStore.getState().activeSessionId).toBeNull();
    expect(useSessionStore.getState().pendingQuestions.deleted).toBeUndefined();
    await act(async () => { replies[0](Response.json([deleted])); });
    expect(useSessionStore.getState().sessions).toEqual([]);
    await act(async () => { replies[1](Response.json([])); });
    expect(useSessionStore.getState().sessions).toEqual([]);
    act(() => useSessionStore.getState().setToken("next-account"));
    useSessionStore.setState({ sessions: [deleted] });
    act(() => socket.onmessage?.({ data: deletion }));
    expect(useSessionStore.getState().sessions).toEqual([deleted]);
  } finally { hook.unmount(); }
});
