import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { selectSession } from "./selectSession";
import { loadResearchSnapshot } from "./hydrateSession";
import { handleWsMessage, useWebSocket } from "../hooks/useWebSocket";
import { ResearchCard } from "../components/ResearchCard";
import { ArchivedSessionsDialog } from "../components/ArchivedSessionsDialog";
import { useSessionStore, type SessionInfo, type ResearchJob } from "../stores/sessionStore";

const dispatch = (generation = "service") => ({ generation, revision: 1, paused: false, items: [], active_request_id: null });
const job: ResearchJob = { id: "r1", session_id: "s1", question: "Existing research", status: "running", phase: "search" };
const session = { id: "s1", name: "Saved conversation", archived: false, agent_id: "ag1" } as SessionInfo;
class Socket {
  static OPEN = 1;
  static CONNECTING = 0;
  static instances: Socket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  close() { this.readyState = 3; }
  constructor() { Socket.instances.push(this); }
}
function deferred() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((yes) => { resolve = yes; });
  return { promise, resolve };
}
beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.setState({ token: "tok" });
  Socket.instances = [];
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("loads existing research when selecting after WS connects, then accepts progress and cancel", async () => {
  vi.stubGlobal("WebSocket", Socket);
  const fetcher = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/research")) return Response.json([job]);
    if (path.endsWith("/api/sessions/s1")) return Response.json({ ...session, messages: [], dispatch_state: dispatch() });
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  renderHook(() => useWebSocket());
  await act(async () => { Socket.instances[0].readyState = 1; Socket.instances[0].onopen?.(); });
  expect(useSessionStore.getState().activeSessionId).toBeNull();
  await act(async () => selectSession("s1"));
  render(<ResearchCard sessionId="s1" />);
  expect(screen.getByText("Existing research")).toBeTruthy();
  act(() => handleWsMessage({ type: "research_progress", session_id: "s1", research_id: "r1", phase: "verify" }));
  expect(screen.getByText(/Verifying claims/)).toBeTruthy();
  fireEvent.click(screen.getByLabelText("Cancel research"));
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).endsWith("/research/r1/cancel"))).toBe(true));
});

it("loads retained research when viewing archived history", async () => {
  const archived = { ...session, archived: true };
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.includes("include_archived")) return Response.json([archived]);
    if (path.endsWith("/research")) return Response.json([{ ...job, status: "completed" }]);
    if (path.endsWith("/bg-tasks")) return Response.json([]);
    return Response.json({ ...archived, messages: [], dispatch_state: null });
  }));
  render(<ArchivedSessionsDialog open onOpenChange={() => {}} />);
  fireEvent.click(await screen.findByRole("button", { name: "View" }));
  await waitFor(() => expect(useSessionStore.getState().research.s1?.[0].id).toBe("r1"));
  expect(useSessionStore.getState().activeSessionId).toBe("s1");
  render(<ResearchCard sessionId="s1" />);
  expect(screen.getByText("Existing research")).toBeTruthy();
  expect(screen.queryByLabelText("Cancel research")).toBeNull();
});

it("preserves newer WS progress and newly started jobs when a snapshot arrives late", async () => {
  useSessionStore.setState({ dispatchGeneration: "service", research: { s1: [job] } });
  const response = deferred();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const loaded = loadResearchSnapshot("s1");
  handleWsMessage({ type: "research_progress", session_id: "s1", research_id: "r1", phase: "verify" });
  handleWsMessage({ type: "research_started", session_id: "s1", research_id: "r2", question: "New research" });
  response.resolve(Response.json([job]));
  await loaded;
  expect(useSessionStore.getState().research.s1).toEqual([
    expect.objectContaining({ id: "r1", phase: "verify" }),
    expect.objectContaining({ id: "r2", question: "New research" }),
  ]);
});

it("recovers an unknown progress event during the first snapshot without accepting its stale status", async () => {
  useSessionStore.setState({ activeSessionId: "s1", dispatchGeneration: "service" });
  const old = deferred();
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => old.promise)
    .mockResolvedValueOnce(Response.json([{ ...job, phase: "verify" }])));
  const loaded = loadResearchSnapshot("s1");
  handleWsMessage({ type: "research_progress", session_id: "s1", research_id: "r1", phase: "verify" });
  await waitFor(() => expect(useSessionStore.getState().research.s1?.[0].phase).toBe("verify"));
  old.resolve(Response.json([job]));
  await loaded;
  expect(useSessionStore.getState().research.s1[0].phase).toBe("verify");
});

it("reloads research on WS reconnect after establishing the detail's service generation", async () => {
  useSessionStore.setState({ activeSessionId: "s1" });
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/api/sessions/s1")) return Response.json({ ...session, messages: [], dispatch_state: dispatch() });
    if (path.endsWith("/research")) return Response.json([job]);
    return Response.json([]);
  }));
  renderHook(() => useWebSocket());
  await act(async () => { Socket.instances[0].readyState = 1; Socket.instances[0].onopen?.(); });
  await waitFor(() => expect(useSessionStore.getState().research.s1).toEqual([job]));
});

it.each(["old-first", "new-first"])("keeps recovered completion across overlapping empty-baseline snapshots (%s)", async (order) => {
  useSessionStore.setState({ activeSessionId: "s1", dispatchGeneration: "service" });
  const old = deferred();
  const latest = deferred();
  const fetcher = vi.fn().mockImplementationOnce(() => old.promise).mockImplementationOnce(() => latest.promise);
  vi.stubGlobal("fetch", fetcher);
  const first = loadResearchSnapshot("s1");
  handleWsMessage({ type: "research_completed", session_id: "s1", research_id: "r1", verified: 1, sources: [] });
  expect(fetcher).toHaveBeenCalledTimes(2);
  const completed = { ...job, status: "completed", phase: "done", verified: 1 };
  if (order === "old-first") {
    old.resolve(Response.json([job]));
    await first;
    latest.resolve(Response.json([completed]));
  } else {
    latest.resolve(Response.json([completed]));
    await waitFor(() => expect(useSessionStore.getState().research.s1?.[0].status).toBe("completed"));
    old.resolve(Response.json([job]));
    await first;
  }
  await waitFor(() => expect(useSessionStore.getState().research.s1?.[0].status).toBe("completed"));
  render(<ResearchCard sessionId="s1" />);
  expect(screen.queryByLabelText("Cancel research")).toBeNull();
});

it("does not treat an earlier snapshot as a live event across overlapping empty-baseline reads", async () => {
  useSessionStore.setState({ dispatchGeneration: "service" });
  const a = deferred(), b = deferred();
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => a.promise).mockImplementationOnce(() => b.promise));
  const first = loadResearchSnapshot("s1"), second = loadResearchSnapshot("s1");
  a.resolve(Response.json([job])); await first;
  b.resolve(Response.json([{ ...job, status: "completed", phase: "done" }])); await second;
  expect(useSessionStore.getState().research.s1[0].status).toBe("completed");
});

it("preserves real WS completion and newly started jobs across the newest overlapping response", async () => {
  useSessionStore.setState({ activeSessionId: "s1", dispatchGeneration: "service" });
  const old = deferred();
  const latest = deferred();
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => old.promise).mockImplementationOnce(() => latest.promise));
  const first = loadResearchSnapshot("s1");
  handleWsMessage({ type: "research_progress", session_id: "s1", research_id: "r1", phase: "verify" });
  handleWsMessage({ type: "research_started", session_id: "s1", research_id: "r1", question: job.question });
  handleWsMessage({ type: "research_completed", session_id: "s1", research_id: "r1", verified: 3 });
  handleWsMessage({ type: "research_started", session_id: "s1", research_id: "r2", question: "New research" });
  latest.resolve(Response.json([job]));
  await waitFor(() => expect(useSessionStore.getState().research.s1?.[0].verified).toBe(3));
  old.resolve(Response.json([job]));
  await first;
  await waitFor(() => expect(useSessionStore.getState().research.s1).toEqual([
    expect.objectContaining({ id: "r1", status: "completed", verified: 3 }),
    expect.objectContaining({ id: "r2", question: "New research" }),
  ]));
});

it.each(["account", "service"])("rejects both overlapping responses after changing %s", async (kind) => {
  useSessionStore.setState({ activeSessionId: "s1", dispatchGeneration: "service" });
  const old = deferred();
  const latest = deferred();
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => old.promise).mockImplementationOnce(() => latest.promise));
  const first = loadResearchSnapshot("s1").catch(() => {});
  const second = loadResearchSnapshot("s1").catch(() => {});
  if (kind === "account") useSessionStore.getState().setToken("other-account");
  else useSessionStore.getState().applyDispatchState("other-session", dispatch("new-service"));
  latest.resolve(Response.json([{ ...job, status: "completed" }]));
  await second;
  old.resolve(Response.json([job]));
  await first;
  expect(useSessionStore.getState().research.s1).toBeUndefined();
});

it.each(["account", "service"])("keeps the new %s read owned when old calls and responses finish", async (kind) => {
  useSessionStore.setState({ activeSessionId: "s1", dispatchGeneration: "service" });
  const old = deferred();
  const latest = deferred();
  const fetcher = vi.fn().mockImplementationOnce(() => old.promise).mockImplementationOnce(() => latest.promise);
  vi.stubGlobal("fetch", fetcher);
  const first = loadResearchSnapshot("s1").catch(() => {});
  if (kind === "account") useSessionStore.getState().setToken("other-account");
  else useSessionStore.getState().applyDispatchState("s1", dispatch("new-service"));
  const second = loadResearchSnapshot("s1");
  expect(fetcher).toHaveBeenCalledTimes(2);
  old.resolve(Response.json([job]));
  await first;
  latest.resolve(Response.json([{ ...job, status: "completed", phase: "done" }]));
  await second;
  expect(useSessionStore.getState().research.s1[0].status).toBe("completed");
});

it("settles a failed newest read without letting an older response replace the next successful retry", async () => {
  useSessionStore.setState({ dispatchGeneration: "service" });
  const old = deferred();
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => old.promise)
    .mockResolvedValueOnce(new Response(null, { status: 503 }))
    .mockResolvedValueOnce(Response.json([{ ...job, status: "completed", phase: "done" }])));
  const first = loadResearchSnapshot("s1");
  await expect(loadResearchSnapshot("s1")).rejects.toThrow("HTTP 503");
  await loadResearchSnapshot("s1");
  old.resolve(Response.json([job]));
  await first;
  expect(useSessionStore.getState().research.s1[0].status).toBe("completed");
});

it.each(["account", "service"])("rejects research from a retired %s", async (kind) => {
  useSessionStore.setState({ dispatchGeneration: "service" });
  const response = deferred();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const loaded = loadResearchSnapshot("s1").catch((error: Error) => error.message);
  if (kind === "account") useSessionStore.getState().setToken("other-account");
  else useSessionStore.getState().applyDispatchState("other-session", dispatch("new-service"));
  response.resolve(Response.json([job]));
  await loaded;
  expect(useSessionStore.getState().research.s1).toBeUndefined();
});
