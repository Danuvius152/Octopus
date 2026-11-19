import { deferred } from "../test-support/deferred";
import { Socket } from "../test-support/socket";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { selectSession } from "./selectSession";
import { loadResearchSnapshot } from "./hydrateSession";
import { createResearch } from "./research";
import evidenceSamples from "./researchEvidenceFixtures.json";
import { sessions } from "./sessionList";
import { handleWsMessage, useWebSocket } from "../hooks/useWebSocket";
import { ResearchCard } from "../components/ResearchCard";
import { SessionAuxiliaryNotice } from "../components/SessionAuxiliaryNotice";
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
  const preWrite = deferred<Response>();
  const confirmation = deferred<Response>();
  let researchReads = 0;
  const fetcher = vi.fn((url: RequestInfo | URL, init?: RequestInit) => {
    if (String(url).endsWith("/research")) return init?.method === "POST" ? post.promise :
      ++researchReads === 1 ? preWrite.promise : confirmation.promise;
    if (new URL(String(url)).pathname === "/api/sessions/s1") {
      return Promise.resolve(Response.json({ ...session, messages: [], active_attempt: null, dispatch_state: dispatch() }));
    }
    return Promise.resolve(Response.json([]));
  });
  vi.stubGlobal("fetch", fetcher);
  submitResearch(null);
  act(() => useSessionStore.getState().applyDispatchState("s1", dispatch()));
  await act(async () => selectSession("s1"));
  await act(async () => { post.resolve(Response.json(job, { status: 201 })); });
  const reads = () => fetcher.mock.calls.filter(([url, init]) => String(url).endsWith("/research") && init?.method !== "POST");
  expect(reads()).toHaveLength(2);
  await act(async () => { preWrite.resolve(Response.json([])); });
  expect(screen.queryByText(/its current status is unconfirmed/)).toBeNull();
  const saved = { ...job, status: "completed", phase: "done", report_path: "report.md" };
  await act(async () => { confirmation.resolve(Response.json([saved])); });
  expect(useSessionStore.getState().research.s1).toEqual([saved]);
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("renders a new Research card using only the full creation receipt", async () => {
  const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) =>
    String(url).endsWith("/research") && init?.method === "POST"
      ? Response.json({ ...job, phase: "synthesize" }, { status: 201 }) : Response.json([]));
  vi.stubGlobal("fetch", fetcher);
  submitResearch();
  expect(await screen.findByText(/Writing the report/)).toBeVisible();
  expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith("/research"))).toHaveLength(1);
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

it.each([true, false])("a missed-started detail remains authoritative across creation (read first=%s)", async (readFirst) => {
  const post = deferred<Response>(), read = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn((url: RequestInfo | URL, init?: RequestInit) => {
    if (String(url).endsWith("/research") && init?.method === "POST") return post.promise;
    if (String(url).endsWith("/research/r1")) return read.promise;
    return Promise.resolve(Response.json([]));
  }));
  submitResearch();
  act(() => handleWsMessage({ type: "research_progress", session_id: "s1", research_id: "r1", phase: "verify" }));
  const loaded = { ...job, status: "completed", phase: "done", injection_status: "failed", error: "Saved but not delivered" };
  await act(async () => {
    if (readFirst) read.resolve(Response.json(loaded));
    else post.resolve(Response.json(job));
  });
  await act(async () => {
    if (readFirst) post.resolve(Response.json(job));
    else read.resolve(Response.json(loaded));
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
  render(<ResearchCard sessionId="s1" foreground />);
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
    const path = new URL(String(url), window.location.origin).pathname;
    if (path.endsWith("/research")) return Response.json([job]);
    if (path.endsWith("/api/sessions/s1")) return Response.json({ ...session, messages: [], active_attempt: null, dispatch_state: dispatch() });
    if (path.includes("/api/sessions") && !path.includes("/api/sessions/s1")) return Response.json([session]);
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  renderHook(() => useWebSocket());
  await act(async () => { Socket.instances[0].readyState = 1; Socket.instances[0].onopen?.(); });
  expect(useSessionStore.getState().activeSessionId).toBeNull();
  await act(async () => selectSession("s1"));
  render(<ResearchCard sessionId="s1" foreground />);
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
  render(<ResearchCard sessionId="s1" foreground />);
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

it("preserves completed research and its follow-up across late events while allowing a snapshot to clear materials", async () => {
  useSessionStore.setState({ dispatchGeneration: "service" });
  const receipt = { request_id: "original", version: 3, stage: "processed" };
  const completed = { ...job, status: "completed", phase: "done" };
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json([{
    ...completed, delivery_receipt: receipt, injection_status: "delivered", report_path: "/owned/report.md",
    sources: ["https://source.test/"], evidence_state: "available", evidence: evidenceSamples.all_failed,
  }])).mockResolvedValueOnce(Response.json([completed])).mockResolvedValueOnce(Response.json([{
    ...completed, report_path: null, sources: [], evidence: null, evidence_state: "unavailable",
  }]));
  vi.stubGlobal("fetch", fetcher);
  await loadResearchSnapshot("s1");

  handleWsMessage({ type: "research_started", session_id: "s1", research_id: "r1", question: job.question });
  handleWsMessage({ type: "research_progress", session_id: "s1", research_id: "r1", phase: "verify" });
  handleWsMessage({ type: "research_completed", session_id: "s1", research_id: "r1", verified: 3, injection_status: "pending" });
  handleWsMessage({ type: "research_delivery", session_id: "s1", research_id: "r1", injection_status: "delivered",
    delivery_receipt: { ...receipt, version: 2, stage: "queued" } });
  const expectRetained = () => expect(useSessionStore.getState().research.s1[0]).toMatchObject({
    ...completed, delivery_receipt: receipt, injection_status: "delivered", report_path: "/owned/report.md",
    sources: ["https://source.test/"], verified: 3, evidence_state: "available", evidence: evidenceSamples.all_failed,
  });
  expectRetained();
  await loadResearchSnapshot("s1");
  expectRetained();

  await loadResearchSnapshot("s1");
  handleWsMessage({ type: "research_completed", session_id: "s1", research_id: "r1", report_path: "/owned/report.md",
    sources: ["https://source.test/"], unverified: 0 });
  expect(useSessionStore.getState().research.s1[0]).toMatchObject({
    ...completed, delivery_receipt: receipt, report_path: null, sources: [], evidence: null, evidence_state: "unavailable", unverified: 0,
  });
});

it("keeps a refused follow-up when its earlier completion event arrives", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json([
    { ...job, status: "completed", phase: "done", injection_status: "failed", error: "No follow-up was accepted", delivery_receipt: null },
  ])));
  await loadResearchSnapshot("s1");
  handleWsMessage({ type: "research_completed", session_id: "s1", research_id: "r1", injection_status: "pending" });
  render(<ResearchCard sessionId="s1" foreground />);
  expect(screen.getByText(/Report retained as material.*No follow-up was accepted/)).toBeVisible();
});

it.each(["completion event", "cancel receipt", "newer running snapshot"])("lets an outstanding snapshot confirm refusal after %s showed delivery pending", async (source) => {
  const running = { ...job, injection_status: "pending", error: null, delivery_receipt: null };
  const completed = { ...running, status: "completed", phase: "done" };
  useSessionStore.setState({ activeSessionId: "s1", dispatchGeneration: "service", research: { s1: [job] } });
  const response = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => response.promise)
    .mockResolvedValueOnce(source === "cancel receipt" ? Response.json(completed) : Response.json([running])));
  render(<ResearchCard sessionId="s1" foreground />);
  const reading = loadResearchSnapshot("s1");
  await act(async () => {
    if (source === "completion event") {
      handleWsMessage({ type: "research_completed", session_id: "s1", research_id: "r1", injection_status: "pending" });
    } else if (source === "cancel receipt") {
      fireEvent.click(screen.getByRole("button", { name: "Cancel research" }));
    } else {
      await loadResearchSnapshot("s1");
    }
  });
  const intermediate = useSessionStore.getState().research.s1[0];
  expect(intermediate).toMatchObject({ status: source === "newer running snapshot" ? "running" : "completed", injection_status: "pending" });
  await act(async () => {
    response.resolve(Response.json([{ ...completed, injection_status: "failed", error: "No follow-up was accepted" }]));
    await reading;
  });
  expect(screen.getByText(/Report retained as material.*No follow-up was accepted/)).toBeVisible();
});

it("retains a snapshot's explicit evidence removal across a delayed completion frame", async () => {
  useSessionStore.setState({ dispatchGeneration: "service" });
  const complete = { ...job, status: "completed", phase: "done", report_path: "/owned/report.md" };
  const available = { ...complete, evidence: evidenceSamples.all_failed, evidence_state: "available" };
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json([available]))
    .mockResolvedValueOnce(Response.json([{ ...complete, evidence: null, evidence_state: "unavailable" }]))
    .mockResolvedValueOnce(Response.json([available]));
  vi.stubGlobal("fetch", fetcher);
  await loadResearchSnapshot("s1");
  render(<ResearchCard sessionId="s1" foreground />);
  expect(screen.getByText(/All searches failed/)).toBeVisible();

  await act(async () => { await loadResearchSnapshot("s1"); });
  expect(screen.getByText(/Evidence unknown or unavailable/)).toBeVisible();
  act(() => handleWsMessage({ type: "research_completed", session_id: "s1", research_id: "r1",
    injection_status: "pending", evidence: evidenceSamples.all_failed, evidence_state: "available", verified: 3 }));
  expect(screen.getByText(/Evidence unknown or unavailable/)).toBeVisible();
  expect(useSessionStore.getState().research.s1[0].verified).toBe(3);

  await act(async () => { await loadResearchSnapshot("s1"); });
  expect(screen.getByText(/All searches failed/)).toBeVisible();
});

it.each(["progress", "completed", "newer read"])("adopts terminal data from an in-flight snapshot after %s", async (source) => {
  useSessionStore.setState({ dispatchGeneration: "service", research: { s1: [job] } });
  const outstanding = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => outstanding.promise)
    .mockResolvedValueOnce(Response.json([{ ...job, phase: "verify", report_path: null, evidence_state: "unavailable" }])));
  const reading = loadResearchSnapshot("s1");
  if (source === "newer read") await loadResearchSnapshot("s1");
  else if (source === "progress") {
    handleWsMessage({ type: "research_progress", session_id: "s1", research_id: "r1", phase: "verify" });
  } else {
    handleWsMessage({ type: "research_completed", session_id: "s1", research_id: "r1", verified: 4 });
  }
  const completed = { ...job, status: "completed", phase: "done", report_path: "/owned/report.md",
    evidence_state: "available", evidence: evidenceSamples.all_failed,
    delivery_receipt: { request_id: "original", version: 3, stage: "processed" } };
  outstanding.resolve(Response.json([{ ...completed, verified: 2 }]));
  await reading;
  expect(useSessionStore.getState().research.s1[0]).toMatchObject({ ...completed, verified: source === "completed" ? 4 : 2 });
});

it("corrects a follow-up identity only from a current snapshot started with the observed binding", async () => {
  const completed = { ...job, status: "completed", phase: "done" };
  const original = { request_id: "old", version: 9, stage: "processed", browser_confirmed_version: 9 };
  const corrected = { request_id: "current", version: 1, stage: "queued" };
  const first = deferred<Response>(), older = deferred<Response>();
  useSessionStore.setState({ dispatchGeneration: "service", research: { s1: [{ ...job, status: "completed", phase: "done" }] } });
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => first.promise).mockImplementationOnce(() => older.promise)
    .mockResolvedValueOnce(Response.json([{ ...completed, delivery_receipt: corrected }])));
  const fromUnbound = loadResearchSnapshot("s1");
  const publishOld = () => handleWsMessage({ type: "research_delivery", session_id: "s1", research_id: "r1",
    injection_status: "delivered", delivery_receipt: original });
  const receipt = () => useSessionStore.getState().research.s1[0].delivery_receipt;
  publishOld();
  first.resolve(Response.json([{ ...completed, delivery_receipt: corrected }]));
  await fromUnbound;
  expect(receipt()).toEqual(original);

  const olderRead = loadResearchSnapshot("s1");
  await loadResearchSnapshot("s1");
  expect(receipt()).toEqual(corrected);
  publishOld();
  older.resolve(Response.json([{ ...completed, delivery_receipt: { ...original, version: 10 } }]));
  await olderRead;
  expect(receipt()).toEqual(corrected);
});

it("recovers an unknown progress event during the first snapshot without accepting its stale status", async () => {
  useSessionStore.setState({ activeSessionId: "s1", dispatchGeneration: "service" });
  const old = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => old.promise)
    .mockResolvedValueOnce(Response.json({ ...job, phase: "verify" })));
  const loaded = loadResearchSnapshot("s1");
  handleWsMessage({ type: "research_progress", session_id: "s1", research_id: "r1", phase: "verify" });
  expect(useSessionStore.getState().research.s1).toBeUndefined();
  await waitFor(() => expect(useSessionStore.getState().research.s1?.[0].phase).toBe("verify"));
  old.resolve(Response.json([job]));
  await loaded;
  expect(useSessionStore.getState().research.s1[0].phase).toBe("verify");
});

it("keeps a Research task recovered by detail when both an old and a fresh list omit it", async () => {
  useSessionStore.setState({ activeSessionId: "s1", dispatchGeneration: "service" });
  const oldList = deferred<Response>(), detail = deferred<Response>();
  const window = Array.from({ length: 200 }, (_, index) => ({ ...job, id: `recent-${index}`, question: `Recent ${index}` }));
  const fetcher = vi.fn().mockImplementationOnce(() => oldList.promise).mockImplementationOnce(() => detail.promise)
    .mockResolvedValueOnce(Response.json(window));
  vi.stubGlobal("fetch", fetcher);
  render(<ResearchCard sessionId="s1" foreground={false} />);

  const reading = loadResearchSnapshot("s1");
  act(() => handleWsMessage({ type: "research_progress", session_id: "s1", research_id: job.id, phase: "verify" }));
  expect(screen.queryByText(job.question)).toBeNull();
  expect(String(fetcher.mock.calls[1][0])).toContain(`/api/sessions/s1/research/${job.id}`);
  await act(async () => detail.resolve(Response.json({ ...job, phase: "verify" })));
  expect(screen.getByText(job.question)).toBeVisible();
  await act(async () => { oldList.resolve(Response.json(window)); await reading; });
  expect(screen.getByText(job.question)).toBeVisible();
  await act(async () => { await loadResearchSnapshot("s1"); });
  expect(screen.getByText(job.question)).toBeVisible();
  expect(screen.getByText("Recent 199")).toBeVisible();
  expect(fetcher).toHaveBeenCalledTimes(3);
});

it.each([200, 503])("confirms card-level status through the exact Research detail (HTTP %s)", async (status) => {
  useSessionStore.getState().setActiveSessionId("s1");
  useSessionStore.getState().applyDispatchState("s1", dispatch());
  const confirmation = deferred<Response>();
  const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") throw new TypeError("Cancellation response lost");
    if (String(url).endsWith("/research")) return Response.json([]);
    if (fetcher.mock.calls.length === 1) return Response.json(job);
    return confirmation.promise;
  });
  vi.stubGlobal("fetch", fetcher);
  render(<ResearchCard sessionId="s1" foreground={false} />);
  act(() => handleWsMessage({ type: "research_progress", session_id: "s1", research_id: job.id, phase: "search" }));
  fireEvent.click(await screen.findByLabelText("Cancel research"));
  expect(await screen.findByRole("alert")).toHaveTextContent("Cancellation result is unconfirmed");

  fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
  expect(String(fetcher.mock.calls[2][0])).toMatch(/\/research\/r1$/);
  expect(screen.queryByText("Current status refreshed.")).toBeNull();
  await act(async () => confirmation.resolve(Response.json({ ...job, status: "cancelled" }, { status })));
  if (status === 200) {
    expect(screen.getByText("Cancelled.")).toBeVisible();
    expect(screen.getByText("Current status refreshed.")).toBeVisible();
  } else {
    expect(screen.getByLabelText("Cancel research")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("Could not confirm current status");
  }
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("coalesces updates during a Research detail read and lets a different task settle independently", async () => {
  useSessionStore.setState({ activeSessionId: "s1", dispatchGeneration: "service" });
  const first = deferred<Response>(), next = deferred<Response>(), other = deferred<Response>();
  const fetcher = vi.fn().mockImplementationOnce(() => first.promise).mockImplementationOnce(() => other.promise)
    .mockImplementationOnce(() => next.promise);
  vi.stubGlobal("fetch", fetcher);
  render(<ResearchCard sessionId="s1" foreground={false} />);
  const deliver = (research_id: string, version: number, stage: "queued" | "processed") => handleWsMessage({
    type: "research_delivery", session_id: "s1", research_id, injection_status: "delivered",
    delivery_receipt: { request_id: `follow-up-${research_id}`, version, stage },
  });
  const queued = { ...job, status: "completed", phase: "done",
    delivery_receipt: { request_id: "follow-up-r1", version: 1, stage: "queued" } };

  act(() => {
    deliver("r1", 1, "queued");
    deliver("r1", 2, "processed");
    deliver("r1", 2, "processed");
    deliver("r2", 2, "processed");
  });
  expect(fetcher.mock.calls.map(([url]) => new URL(String(url)).pathname)).toEqual([
    "/api/sessions/s1/research/r1", "/api/sessions/s1/research/r2",
  ]);
  expect(screen.queryByText(job.question)).toBeNull();
  await act(async () => other.resolve(Response.json({ ...job, id: "r2", question: "Independent task", phase: "verify" })));
  expect(screen.getByText("Independent task")).toBeVisible();
  expect(screen.queryByText(job.question)).toBeNull();
  await act(async () => first.resolve(Response.json(queued)));
  expect(screen.getByText("Report queued for follow-up")).toBeVisible();
  expect(String(fetcher.mock.calls[2][0])).toMatch(/\/research\/r1$/);
  await act(async () => next.resolve(Response.json({ ...queued, delivery_receipt: { ...queued.delivery_receipt, version: 2, stage: "processed" } })));
  expect(screen.getByText("Follow-up processed")).toBeVisible();
  expect(screen.getByText("Independent task")).toBeVisible();
  expect(fetcher).toHaveBeenCalledTimes(3);
});

it.each(["unavailable", "wrong job", "wrong session"])("retries the original Research detail after a %s response", async (failure) => {
  useSessionStore.setState({ activeSessionId: "s1", dispatchGeneration: "service" });
  const invalid = failure === "unavailable" ? new Response(null, { status: 503 })
    : Response.json(failure === "wrong job" ? { ...job, id: "different" } : { ...job, session_id: "s2" });
  const fetcher = vi.fn().mockResolvedValueOnce(invalid).mockResolvedValueOnce(Response.json([]))
    .mockResolvedValueOnce(Response.json(job));
  vi.stubGlobal("fetch", fetcher);
  render(<><SessionAuxiliaryNotice sessionId="s1" /><ResearchCard sessionId="s1" foreground={false} /></>);
  act(() => handleWsMessage({ type: "research_progress", session_id: "s1", research_id: job.id, phase: "verify" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not refresh research updates.");
  expect(screen.queryByText(job.question)).toBeNull();
  await act(async () => { await loadResearchSnapshot("s1"); });
  fireEvent.click(screen.getByRole("button", { name: "Retry research updates" }));
  expect(await screen.findByText(job.question)).toBeVisible();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(fetcher.mock.calls.map(([url]) => new URL(String(url)).pathname)).toEqual([
    "/api/sessions/s1/research/r1", "/api/sessions/s1/research", "/api/sessions/s1/research/r1",
  ]);
});

it("reissues a pending Research detail in the first service generation", async () => {
  useSessionStore.getState().setActiveSessionId("s1");
  const old = deferred<Response>(), current = deferred<Response>();
  const fetcher = vi.fn().mockImplementationOnce(() => old.promise).mockImplementationOnce(() => current.promise);
  vi.stubGlobal("fetch", fetcher);
  render(<><SessionAuxiliaryNotice sessionId="s1" /><ResearchCard sessionId="s1" foreground={false} /></>);
  act(() => handleWsMessage({ type: "research_progress", session_id: "s1", research_id: job.id, phase: "search" }));
  act(() => useSessionStore.getState().applyDispatchState("s1", dispatch()));
  await act(async () => old.resolve(Response.json({ ...job, question: "Unconfirmed generation" })));
  expect(screen.queryByText("Unconfirmed generation")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(fetcher.mock.calls.map(([url]) => new URL(String(url)).pathname)).toEqual([
    "/api/sessions/s1/research/r1", "/api/sessions/s1/research/r1",
  ]);
  await act(async () => current.resolve(Response.json(job)));
  expect(screen.getByText(job.question)).toBeVisible();
});

it.each([true, false])("carries a failed Research detail into the first service generation only within its original view (%s)", async (stay) => {
  useSessionStore.getState().setActiveSessionId("s1");
  const repaired = deferred<Response>();
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(null, { status: 503 }))
    .mockImplementationOnce(() => repaired.promise);
  vi.stubGlobal("fetch", fetcher);
  render(<><SessionAuxiliaryNotice sessionId="s1" /><ResearchCard sessionId="s1" foreground={false} /></>);
  act(() => handleWsMessage({ type: "research_progress", session_id: "s1", research_id: job.id, phase: "verify" }));
  expect(await screen.findByRole("button", { name: "Retry research updates" })).toBeVisible();

  if (!stay) {
    act(() => {
      useSessionStore.getState().setActiveSessionId("s2");
      useSessionStore.getState().setActiveSessionId("s1");
    });
  }
  act(() => useSessionStore.getState().applyDispatchState("s1", dispatch()));
  expect(fetcher.mock.calls.map(([url]) => new URL(String(url)).pathname)).toEqual(
    stay ? ["/api/sessions/s1/research/r1", "/api/sessions/s1/research/r1"] : ["/api/sessions/s1/research/r1"]);
  expect(screen.queryByText(job.question)).toBeNull();
  if (!stay) return;
  await act(async () => repaired.resolve(Response.json({ ...job, phase: "verify" })));
  expect(screen.getByText(job.question)).toBeVisible();
  expect(screen.queryByRole("alert")).toBeNull();
});

it.each([["session", 200], ["view", 503], ["service", 200]] as const)(
  "does not publish or repeat a Research detail after leaving its %s observation", async (change, status) => {
  useSessionStore.setState({ activeSessionId: "s1", dispatchGeneration: "service" });
  const response = deferred<Response>();
  const fetcher = vi.fn(() => response.promise);
  vi.stubGlobal("fetch", fetcher);
  render(<><SessionAuxiliaryNotice sessionId="s1" /><ResearchCard sessionId="s1" foreground={false} /></>);
  const progress = () => handleWsMessage({ type: "research_progress", session_id: "s1", research_id: job.id, phase: "verify" });
  act(() => {
    progress();
    progress();
    const store = useSessionStore.getState();
    if (change === "session") { store.setActiveSessionId("s2"); store.setActiveSessionId("s1"); }
    else if (change === "view") { store.openManage("schedules"); store.showChat(); }
    else store.applyDispatchState("s1", dispatch("next-service"));
  });
  await act(async () => response.resolve(Response.json(job, { status })));
  expect(screen.queryByText(job.question)).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it("reloads research on WS reconnect after establishing the detail's service generation", async () => {
  useSessionStore.setState({ activeSessionId: "s1" });
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = new URL(String(url), window.location.origin).pathname;
    if (path.endsWith("/api/sessions/s1")) return Response.json({ ...session, messages: [], active_attempt: null, dispatch_state: dispatch() });
    if (path.includes("/api/sessions") && !path.includes("/api/sessions/s1")) return Response.json([session]);
    if (path.endsWith("/research")) return Response.json([job]);
    return Response.json([]);
  }));
  renderHook(() => useWebSocket());
  await act(async () => { Socket.instances[0].readyState = 1; Socket.instances[0].onopen?.(); });
  await waitFor(() => expect(useSessionStore.getState().research.s1).toEqual([job]));
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
    latest.resolve(Response.json(completed));
  } else {
    latest.resolve(Response.json(completed));
    await waitFor(() => expect(useSessionStore.getState().research.s1?.[0].status).toBe("completed"));
    old.resolve(Response.json([job]));
    await first;
  }
  await waitFor(() => expect(useSessionStore.getState().research.s1?.[0].status).toBe("completed"));
  render(<ResearchCard sessionId="s1" foreground />);
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
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => old.promise).mockImplementationOnce(() => latest.promise)
    .mockResolvedValueOnce(Response.json({ ...job, status: "completed", phase: "done", verified: 3 })));
  const first = loadResearchSnapshot("s1");
  handleWsMessage({ type: "research_progress", session_id: "s1", research_id: "r1", phase: "verify" });
  handleWsMessage({ type: "research_started", session_id: "s1", research_id: "r1", question: job.question });
  handleWsMessage({ type: "research_completed", session_id: "s1", research_id: "r1", verified: 3 });
  handleWsMessage({ type: "research_started", session_id: "s1", research_id: "r2", question: "New research" });
  latest.resolve(Response.json(job));
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

it.each(["account", "connection", "service"])("rejects research from a retired %s", async (kind) => {
  useSessionStore.setState({ dispatchGeneration: "service" });
  const response = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn((url: RequestInfo | URL) => String(url).endsWith("/research") ? response.promise : Promise.resolve(Response.json([]))));
  const loaded = loadResearchSnapshot("s1").catch((error: Error) => error.message);
  if (kind === "account") useSessionStore.getState().setToken("other-account");
  else if (kind === "connection") sessions.connect({ socket: {}, accountGeneration: useSessionStore.getState().accountGeneration });
  else useSessionStore.getState().applyDispatchState("other-session", dispatch("new-service"));
  response.resolve(Response.json([job]));
  await loaded;
  expect(useSessionStore.getState().research.s1).toBeUndefined();
});
