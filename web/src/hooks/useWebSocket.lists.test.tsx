import { Socket } from "../test-support/socket";
import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useSessionStore } from "../stores/sessionStore";
import type { SessionInfo } from "../stores/sessionStore";
import { applications } from "../lib/applicationList";
import { useWebSocket } from "./useWebSocket";


afterEach(() => {
  useSessionStore.getState().setToken("");
  vi.useRealTimers(); vi.unstubAllGlobals();
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
