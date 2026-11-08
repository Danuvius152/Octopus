import { deferred } from "../test-support/deferred";
import { Socket } from "../test-support/socket";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { selectSession } from "./selectSession";
import { loadResearchSnapshot } from "./hydrateSession";
import { createResearch } from "./research";
import { sessions } from "./sessionList";
import { handleWsMessage, useWebSocket } from "../hooks/useWebSocket";
import { ResearchCard } from "../components/ResearchCard";
import { ChatView } from "../components/ChatView";
import { mockChatViewport } from "../components/chatViewport.test-support";
import { VirtuosoMockContext } from "react-virtuoso";
import { ArchivedSessionsDialog } from "../components/ArchivedSessionsDialog";
import { useSessionStore, type SessionInfo, type ResearchJob } from "../stores/sessionStore";

const dispatch = (generation = "service") => ({ generation, revision: 1, paused: false, items: [], active_request_id: null, active_operation: null });
const job: ResearchJob = { id: "r1", session_id: "s1", question: "Existing research", status: "running", phase: "search" };
const session = { id: "s1", name: "Saved conversation", archived: false, agent_id: "ag1" } as SessionInfo;
beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.setState({ token: "tok" });
  Socket.reset(0);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function submitResearch(generation: string | null = "service") {
  mockChatViewport();
  useSessionStore.setState({ activeSessionId: "s1", sessions: [{ ...session, status: "idle", backend: "codex" }], dispatchGeneration: generation });
  const view = render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()} connected
      onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "/research Existing research" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  return view;
}

it("confirms the original creation after the first detail establishes its generation", async () => {
  const post = deferred<Response>();
  const confirmation = deferred<Response>();
  const fetcher = vi.fn((url: RequestInfo | URL, init?: RequestInit) => {
    if (String(url).endsWith("/research")) return init?.method === "POST" ? post.promise : confirmation.promise;
    return Promise.resolve(Response.json([]));
  });
  vi.stubGlobal("fetch", fetcher);
  submitResearch(null);
  act(() => useSessionStore.getState().applyDispatchState("s1", dispatch()));
  await act(async () => { post.resolve(Response.json(job, { status: 201 })); });
  const reads = () => fetcher.mock.calls.filter(([url, init]) => String(url).endsWith("/research") && init?.method !== "POST");
  expect(reads()).toHaveLength(1);
  const saved = { ...job, status: "completed", phase: "done", report_path: "report.md" };
  await act(async () => { confirmation.resolve(Response.json([saved])); });
  expect(useSessionStore.getState().research.s1).toEqual([{ ...saved, evidence: null, evidence_state: "unavailable" }]);
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("does not use a pre-generation cached job as current creation confirmation", async () => {
  const post = deferred<Response>();
  const fetcher = vi.fn((url: RequestInfo | URL, init?: RequestInit) => String(url).endsWith("/research")
    ? init?.method === "POST" ? post.promise : Promise.resolve(Response.json([])) : Promise.resolve(Response.json([])));
  vi.stubGlobal("fetch", fetcher);
  submitResearch(null);
  act(() => {
    useSessionStore.getState().setResearch("s1", [job]);
    useSessionStore.getState().applyDispatchState("s1", dispatch());
  });
  await act(async () => { post.resolve(Response.json(job, { status: 201 })); });
  expect(fetcher.mock.calls.filter(([url, init]) => String(url).endsWith("/research") && init?.method !== "POST")).toHaveLength(1);
  expect(useSessionStore.getState().research.s1).toEqual([]);
  expect(useSessionStore.getState().sessionAuxiliaryErrors.s1?.research).toContain("unconfirmed");
});

it.each(["failed", "missing"])("retains the creation identity when its confirmation is %s", async (result) => {
  const post = deferred<Response>();
  const fetcher = vi.fn((url: RequestInfo | URL, init?: RequestInit) => String(url).endsWith("/research")
    ? init?.method === "POST" ? post.promise : Promise.resolve(result === "failed" ? new Response(null, { status: 503 }) : Response.json([]))
    : Promise.resolve(Response.json([])));
  vi.stubGlobal("fetch", fetcher);
  submitResearch(null);
  act(() => useSessionStore.getState().applyDispatchState("s1", dispatch()));
  await act(async () => { post.resolve(Response.json(job, { status: 201 })); });
  expect(useSessionStore.getState().messages.s1.some((message) =>
    message.content?.includes("r1") && message.content.includes("created") && message.content.includes("unconfirmed"))).toBe(true);
  expect(useSessionStore.getState().sessionAuxiliaryErrors.s1?.research).toContain("unconfirmed");
  expect(useSessionStore.getState().research.s1?.length ?? 0).toBe(0);
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it.each(["unmount", "session"])("cancels the current confirmation when the original view leaves via %s", async (change) => {
  const post = deferred<Response>(), confirmation = deferred<Response>();
  const fetcher = vi.fn((url: RequestInfo | URL, init?: RequestInit) => String(url).endsWith("/research")
    ? init?.method === "POST" ? post.promise : confirmation.promise : Promise.resolve(Response.json([])));
  vi.stubGlobal("fetch", fetcher);
  const view = submitResearch(null);
  act(() => useSessionStore.getState().applyDispatchState("s1", dispatch()));
  await act(async () => { post.resolve(Response.json(job, { status: 201 })); });
  const reading = fetcher.mock.calls.find(([url, init]) => String(url).endsWith("/research") && init?.method !== "POST");
  expect(reading).toBeDefined();
  act(() => { if (change === "unmount") view.unmount(); else useSessionStore.getState().setActiveSessionId("s2"); });
  expect(reading![1]?.signal?.aborted).toBe(true);
  await act(async () => { confirmation.resolve(Response.json([job])); });
  expect(useSessionStore.getState().research.s1).toBeUndefined();
  expect(useSessionStore.getState().research.s2).toBeUndefined();
});

it.each(["generation", "account", "token", "connection", "archive", "delete"])("does not transfer a first-generation receipt across %s changes", async (change) => {
  useSessionStore.setState({ activeSessionId: "s1", sessions: [session] });
  const post = deferred<Response>();
  const fetcher = vi.fn((_: RequestInfo | URL, init?: RequestInit) =>
    init?.method === "POST" ? post.promise : Promise.resolve(Response.json([])));
  vi.stubGlobal("fetch", fetcher);
  const creation = createResearch("s1", job.question, () => true);
  useSessionStore.getState().applyDispatchState("s1", dispatch());
  if (change === "generation") useSessionStore.getState().applyDispatchState("s1", dispatch("next-service"));
  if (change === "account") useSessionStore.setState({ accountGeneration: useSessionStore.getState().accountGeneration + 1 });
  if (change === "token") useSessionStore.getState().setToken("new-token");
  if (change === "connection") sessions.connect({ socket: {}, accountGeneration: useSessionStore.getState().accountGeneration });
  if (change === "archive") sessions.archive("s1");
  if (change === "delete") sessions.remove("s1");
  post.resolve(Response.json(job, { status: 201 }));
  await creation;
  expect(useSessionStore.getState().research.s1).toBeUndefined();
  expect(fetcher.mock.calls.filter(([url, init]) => String(url).endsWith("/research") && init?.method !== "POST")).toHaveLength(0);
  expect(useSessionStore.getState().messages.s1?.some((message) => message.is_error)).toBeFalsy();
});

it("reports an unknown creation result across the first generation without repeating the POST", async () => {
  useSessionStore.setState({ activeSessionId: "s1", sessions: [session] });
  const post = deferred<Response>();
  const fetcher = vi.fn(() => post.promise);
  vi.stubGlobal("fetch", fetcher);
  const creation = createResearch("s1", job.question, () => true);
  useSessionStore.getState().applyDispatchState("s1", dispatch());
  post.reject(new TypeError("network disconnected"));
  await creation;
  expect(useSessionStore.getState().messages.s1[0].content).toContain("creation result is unconfirmed");
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it("keeps a new job across an older empty read and accepts a later authoritative deletion", async () => {
  useSessionStore.setState({ activeSessionId: "s1", sessions: [session] });
  const post = deferred<Response>(), older = deferred<Response>(), confirmation = deferred<Response>();
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn((_: RequestInfo | URL, init?: RequestInit) =>
    init?.method === "POST" ? post.promise : ++reads === 1 ? older.promise : reads === 2 ? confirmation.promise : Promise.resolve(Response.json([]))));
  const oldRead = loadResearchSnapshot("s1");
  const creation = createResearch("s1", job.question, () => true);
  useSessionStore.getState().applyDispatchState("s1", dispatch());
  post.resolve(Response.json(job, { status: 201 }));
  confirmation.resolve(Response.json([job]));
  await creation;
  older.resolve(Response.json([]));
  await oldRead;
  expect(useSessionStore.getState().research.s1?.map((item) => item.id)).toEqual(["r1"]);
  await loadResearchSnapshot("s1");
  expect(useSessionStore.getState().research.s1).toEqual([]);
});

it.each(["progress", "completed", "failed", "cancelled"])("a late creation receipt preserves earlier %s facts through ChatView", async (kind) => {
  const response = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn((url: RequestInfo | URL, init?: RequestInit) =>
    String(url).endsWith("/research") && init?.method === "POST" ? response.promise : Promise.resolve(Response.json([]))));
  submitResearch();
  act(() => {
    handleWsMessage({ type: "research_started", session_id: "s1", research_id: "r1", question: job.question });
    handleWsMessage(kind === "progress"
      ? { type: "research_progress", session_id: "s1", research_id: "r1", phase: "verify" }
      : kind === "completed" ? { type: "research_completed", session_id: "s1", research_id: "r1", verified: 3 }
      : { type: "research_failed", session_id: "s1", research_id: "r1", status: kind, error: "Original terminal" });
  });
  await act(async () => { response.resolve(Response.json(job)); });
  const saved = useSessionStore.getState().research.s1[0];
  expect(saved.status).toBe(kind === "progress" ? "running" : kind);
  expect(saved.phase).toBe(kind === "progress" ? "verify" : kind === "completed" ? "done" : "scope");
  if (kind === "completed") expect(saved.verified).toBe(3);
});

it("does not publish a late creation receipt after the original view selects another session", async () => {
  const response = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn((url: RequestInfo | URL, init?: RequestInit) =>
    String(url).endsWith("/research") && init?.method === "POST" ? response.promise : Promise.resolve(Response.json([]))));
  submitResearch();
  act(() => useSessionStore.setState({ activeSessionId: "s2" }));
  await act(async () => response.resolve(Response.json({ ...job, status: "failed", error: "Creation already settled" })));
  expect(useSessionStore.getState().research.s1).toBeUndefined();
  expect(useSessionStore.getState().research.s2).toBeUndefined();
});

it.each(["account", "service", "unmount"])("drops old creation data and errors after %s changes", async (change) => {
  const response = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn((url: RequestInfo | URL, init?: RequestInit) =>
    String(url).endsWith("/research") && init?.method === "POST" ? response.promise : Promise.resolve(Response.json([]))));
  const view = submitResearch();
  act(() => {
    if (change === "account") useSessionStore.setState({ accountGeneration: useSessionStore.getState().accountGeneration + 1 });
    if (change === "service") useSessionStore.setState({ dispatchGeneration: "another-service" });
    if (change === "unmount") view.unmount();
  });
  await act(async () => response.resolve(new Response("Old creation failure", { status: 503 })));
  expect(useSessionStore.getState().research.s1).toBeUndefined();
  expect(useSessionStore.getState().messages.s1?.some((message) => message.is_error)).toBeFalsy();
});

it.each([true, false])("a missed-started snapshot remains authoritative across creation (read first=%s)", async (readFirst) => {
  const post = deferred<Response>(), read = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn((url: RequestInfo | URL, init?: RequestInit) => {
    if (String(url).endsWith("/research")) return init?.method === "POST" ? post.promise : read.promise;
    return Promise.resolve(Response.json([]));
  }));
  submitResearch();
  act(() => handleWsMessage({ type: "research_progress", session_id: "s1", research_id: "r1", phase: "verify" }));
  const loaded = { ...job, status: "completed", phase: "done", injection_status: "failed", error: "Saved but not delivered" };
  await act(async () => {
    if (readFirst) read.resolve(Response.json([loaded]));
    else post.resolve(Response.json(job));
  });
  await act(async () => {
    if (readFirst) post.resolve(Response.json(job));
    else read.resolve(Response.json([loaded]));
  });
  expect(useSessionStore.getState().research.s1[0]).toMatchObject(loaded);
});

it.each([1, 2])("a browser confirmation preserves a concurrent authoritative delivery snapshot v%s", async (version) => {
  const receipt = { request_id: "original", version: 1, stage: "queued" as const };
  useSessionStore.setState({ dispatchGeneration: "service", research: { s1: [{ ...job, status: "completed", delivery_receipt: receipt }] } });
  const snapshot = deferred<Response>(), confirmation = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn((url: RequestInfo | URL, init?: RequestInit) =>
    init?.method === "POST" ? confirmation.promise : String(url).endsWith("/research") ? snapshot.promise : Promise.resolve(Response.json([]))));
  const reading = loadResearchSnapshot("s1");
  render(<ResearchCard sessionId="s1" />);
  await act(async () => confirmation.resolve(Response.json({ delivery_receipt: { ...receipt, browser_confirmed_version: 1, browser_confirmed_at: "seen" } })));
  const incoming = { ...receipt, version, stage: version === 2 ? "processed" : "queued" };
  await act(async () => { snapshot.resolve(Response.json([{ ...job, status: "completed", delivery_receipt: incoming }])); await reading; });
  expect(useSessionStore.getState().research.s1[0].delivery_receipt).toMatchObject({
    ...incoming, browser_confirmed_version: 1, browser_confirmed_at: "seen",
  });
});

it("loads existing research when selecting after WS connects, then accepts progress and cancel", async () => {
  vi.stubGlobal("WebSocket", Socket);
  const fetcher = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/research")) return Response.json([job]);
    if (path.endsWith("/api/sessions/s1")) return Response.json({ ...session, messages: [], dispatch_state: dispatch() });
    if (path.includes("/api/sessions") && !path.includes("/api/sessions/s1")) return Response.json([session]);
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
  const response = deferred<Response>();
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
  const old = deferred<Response>();
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
    if (path.includes("/api/sessions") && !path.includes("/api/sessions/s1")) return Response.json([session]);
    if (path.endsWith("/research")) return Response.json([job]);
    return Response.json([]);
  }));
  renderHook(() => useWebSocket());
  await act(async () => { Socket.instances[0].readyState = 1; Socket.instances[0].onopen?.(); });
  await waitFor(() => expect(useSessionStore.getState().research.s1).toEqual([{ ...job, evidence: null, evidence_state: "unavailable" }]));
});

it.each(["old-first", "new-first"])("keeps recovered completion across overlapping empty-baseline snapshots (%s)", async (order) => {
  useSessionStore.setState({ activeSessionId: "s1", dispatchGeneration: "service" });
  const old = deferred<Response>();
  const latest = deferred<Response>();
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
  const a = deferred<Response>(), b = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => a.promise).mockImplementationOnce(() => b.promise));
  const first = loadResearchSnapshot("s1"), second = loadResearchSnapshot("s1");
  a.resolve(Response.json([job])); await first;
  b.resolve(Response.json([{ ...job, status: "completed", phase: "done" }])); await second;
  expect(useSessionStore.getState().research.s1[0].status).toBe("completed");
});

it("preserves real WS completion and newly started jobs across the newest overlapping response", async () => {
  useSessionStore.setState({ activeSessionId: "s1", dispatchGeneration: "service" });
  const old = deferred<Response>();
  const latest = deferred<Response>();
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
  const old = deferred<Response>();
  const latest = deferred<Response>();
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
  const old = deferred<Response>();
  const latest = deferred<Response>();
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
  const old = deferred<Response>();
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
  const response = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const loaded = loadResearchSnapshot("s1").catch((error: Error) => error.message);
  if (kind === "account") useSessionStore.getState().setToken("other-account");
  else useSessionStore.getState().applyDispatchState("other-session", dispatch("new-service"));
  response.resolve(Response.json([job]));
  await loaded;
  expect(useSessionStore.getState().research.s1).toBeUndefined();
});
