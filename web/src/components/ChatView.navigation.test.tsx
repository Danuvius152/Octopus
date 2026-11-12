import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { sessions } from "../lib/sessionList";

const a = { id: "a", name: "A", agent_id: "agent", status: "idle", backend: "codex",
  created_at: "2026-11-12", working_dir: "/tmp" } as SessionInfo;
const b = { ...a, id: "b", name: "B" };
const child = { ...a, id: "child", name: "Result", forked_from_session_id: a.id,
  fork_prefilled_prompt: "Redo A's original message" };
const dispatch = { generation: "service", revision: 0, paused: false, active_request_id: null,
  active_operation: null, items: [] };

beforeEach(() => {
  mockChatViewport();
  useSessionStore.getState().setToken("");
  sessionStorage.clear();
  useSessionStore.getState().setToken("navigation-test");
  useSessionStore.getState().setIdentity({ user_id: "navigation-owner", label: "Navigation owner", is_admin: false });
  useSessionStore.setState({ activeSessionId: a.id, activeAgentId: "agent", mainView: "chat",
    sessions: [a, b], archivedSessions: [], agents: [], credentials: [], backendModels: {},
    messages: {}, streamingText: {}, lastAppliedSeq: {}, oldestSeq: {}, hasMoreMessages: {},
    pendingQuestions: {}, closedQuestionIds: {}, subagents: {}, wikiJobs: {}, pendingForks: {},
    dispatchUnavailable: {}, dispatchStates: { a: dispatch, b: dispatch },
    dispatchGeneration: "service", retiredDispatchGenerations: [],
  });
});
afterEach(() => { cleanup(); useSessionStore.getState().setToken(""); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function mount() {
  return render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()}
      connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>);
}

function command(text: string) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
}

function server() {
  let finish!: (response: Response) => void;
  let saved = false;
  let result: SessionInfo = child;
  let holdList = false;
  let finishList: (() => void) | undefined;
  const fetcher = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, window.location.origin);
    if (init?.method === "POST") {
      if (url.pathname.endsWith("/archive")) result = { ...child, forked_from_session_id: null, fork_prefilled_prompt: null };
      return new Promise<Response>((resolve) => { finish = resolve; });
    }
    if (url.pathname.endsWith("/wiki-jobs")) return Response.json([]);
    if (url.pathname.endsWith("/submissions")) return Response.json({ items: [] });
    if (url.pathname.endsWith("/fork-preview")) return Response.json({ rewind_to_msg_seq: 7,
      prefilled_prompt: "A original message", can_fork: true,
      side_effect_summary: { file_edits: [], bg_tasks: [], other_tools: [], counts: { total: 0, file_edits: 0, bg_tasks: 0 } },
      revert: { available: false, refused_reason: "No file changes" } });
    if (url.pathname === "/api/sessions") {
      if (holdList) await new Promise<void>((resolve) => { finishList = resolve; });
      return Response.json(saved ?
        [b, result, ...(url.searchParams.has("include_archived") ? [{ ...a, archived: true }] : [])] : [a, b]);
    }
    const info = url.pathname.includes("/child") ? result : url.pathname.includes("/b") ? b : a;
    return Response.json({ ...info, messages: [{ role: "user", type: "text", seq: 7, content: `${info.name} original message` }],
      pending_questions: [], subagents: [], dispatch_state: dispatch });
  });
  vi.stubGlobal("fetch", fetcher);
  return {
    fetcher,
    complete: async (response?: Response) => { saved = true; await act(async () => finish(response ?? Response.json(result))); },
    refuse: async () => { await act(async () => finish(Response.json({ detail: { message: "Rewind refused" } }, { status: 409 }))); },
    holdList: () => { holdList = true; },
    listPending: () => finishList !== undefined,
    confirmList: async () => { holdList = false; await act(async () => finishList!()); },
  };
}

it("archive success keeps its list fact after A→B→A without reviving navigation", async () => {
  const api = server();
  mount();
  command("/archive");
  await waitFor(() => expect(api.fetcher.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
  act(() => {
    useSessionStore.getState().setActiveSessionId(b.id);
    useSessionStore.getState().setActiveSessionId(a.id);
  });
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "New draft after returning" } });
  await api.complete();
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(useSessionStore.getState().sessions.map((row) => row.id)).toEqual([b.id, child.id]);
  expect(useSessionStore.getState().archivedSessions.map((row) => row.id)).toEqual([a.id]);
  expect(api.fetcher.mock.calls.some(([url]) => new URL(url).pathname === "/api/sessions/child")).toBe(false);
  // Archived history has no composer; the shared draft remains available in B.
  act(() => useSessionStore.getState().setActiveSessionId(b.id));
  expect(screen.getByRole("textbox")).toHaveValue("New draft after returning");
});

async function openRewind() {
  command("/rewind");
  fireEvent.click(await screen.findByText("A original message"));
  await waitFor(() => expect(screen.getByRole("button", { name: "Rewind" })).toBeEnabled());
}

it("ends the old rewind on A→B→A and preserves a reopened dialog on late success", async () => {
  const api = server();
  mount();
  await openRewind();
  fireEvent.click(screen.getByRole("button", { name: "Rewind" }));
  await waitFor(() => expect(api.fetcher.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
  act(() => {
    useSessionStore.getState().setActiveSessionId(b.id);
    useSessionStore.getState().setActiveSessionId(a.id);
  });
  expect(screen.queryByTestId("fork-dialog")).toBeNull();
  await openRewind();
  fireEvent.change(screen.getByPlaceholderText("Fork label"), { target: { value: "New dialog label" } });
  fireEvent.change(document.querySelector("textarea")!, { target: { value: "New composer draft" } });
  await api.complete();
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(useSessionStore.getState().sessions.map((row) => row.id)).toEqual([b.id, child.id]);
  expect(screen.getByTestId("fork-dialog")).toBeVisible();
  expect(screen.getByPlaceholderText("Fork label")).toHaveValue("New dialog label");
  expect(screen.getByRole("button", { name: "Rewind" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  act(() => useSessionStore.getState().setActiveSessionId(b.id));
  expect(document.querySelector("textarea")).toHaveValue("New composer draft");
});

async function launch(mode: "archive" | "rewind") {
  if (mode === "archive") command("/archive");
  else {
    await openRewind();
    fireEvent.click(screen.getByRole("button", { name: "Rewind" }));
  }
}

it.each(["archive", "rewind"] as const)("%s enters the confirmed result through normal selection and owns its draft", async (mode) => {
  const api = server();
  mount();
  await launch(mode);
  await api.complete();
  expect(useSessionStore.getState().activeSessionId).toBe(child.id);
  expect(useSessionStore.getState().activeAgentId).toBe(child.agent_id);
  expect(screen.queryByTestId("fork-dialog")).toBeNull();
  expect(document.querySelector("textarea")).toHaveValue(mode === "rewind" ? child.fork_prefilled_prompt : "");
  expect(useSessionStore.getState().messages[child.id]?.[0].content).toBe("Result original message");
  expect(useSessionStore.getState().archivedSessions.map((row) => row.id)).toEqual([a.id]);
});

it.each((["archive", "rewind"] as const).flatMap((mode) =>
  (["session", "view", "view-return", "unmount"] as const).map((boundary) => ({ mode, boundary }))))(
  "$mode keeps valid success facts after $boundary without changing the new context", async ({ mode, boundary }) => {
    const api = server();
    let view = mount();
    await launch(mode);
    if (boundary === "unmount") { view.unmount(); view = mount(); }
    else act(() => {
      if (boundary === "session") useSessionStore.getState().setActiveSessionId(b.id);
      else {
        useSessionStore.getState().openManage("schedules");
        if (boundary === "view-return") useSessionStore.getState().showChat();
      }
    });
    if (mode === "rewind") expect(screen.queryByTestId("fork-dialog")).toBeNull();
    fireEvent.change(document.querySelector("textarea")!, { target: { value: "Current context draft" } });
    await api.complete();
    expect(useSessionStore.getState().activeSessionId).toBe(boundary === "session" ? b.id : a.id);
    expect(useSessionStore.getState().mainView).toBe(boundary === "view" ? "schedules" : "chat");
    expect(useSessionStore.getState().sessions.map((row) => row.id)).toEqual([b.id, child.id]);
    expect(useSessionStore.getState().archivedSessions.map((row) => row.id)).toEqual([a.id]);
    expect(api.fetcher.mock.calls.some(([url]) => new URL(url).pathname === "/api/sessions/child")).toBe(false);
    act(() => useSessionStore.getState().setActiveSessionId(b.id));
    expect(document.querySelector("textarea")).toHaveValue("Current context draft");
    view.unmount();
  });

it("does not inherit A's selected message into a dialog for B", async () => {
  const api = server();
  useSessionStore.setState({ messages: {
    a: [{ role: "user", type: "text", seq: 7, content: "A original message" }],
    b: [{ role: "user", type: "text", seq: 7, content: "B original message" }],
  } });
  mount();
  fireEvent.click(await screen.findByTestId("fork-from-here"));
  await waitFor(() => expect(screen.getByRole("button", { name: "Rewind" })).toBeEnabled());
  act(() => useSessionStore.getState().setActiveSessionId(b.id));
  expect(screen.queryByTestId("fork-dialog")).toBeNull();
  expect(api.fetcher.mock.calls.some(([url]) => url.includes("/b/fork-preview"))).toBe(false);
  command("/rewind");
  expect(screen.getByTestId("fork-picker")).toBeVisible();
  expect(screen.queryByTestId("fork-confirm")).toBeNull();
});

it.each((["archive", "rewind"] as const).flatMap((mode) =>
  (["connection", "service", "token", "account"] as const).map((boundary) => ({ mode, boundary }))))(
  "$mode separates a late $boundary receipt from the current observation", async ({ mode, boundary }) => {
    const api = server();
    const first = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
    const second = { ...first, socket: {} };
    await act(async () => { sessions.connect(first); });
    mount();
    await launch(mode);
    act(() => {
      if (boundary === "connection") { sessions.disconnect(first); sessions.connect(second); }
      if (boundary === "service") useSessionStore.getState().applyDispatchState(a.id, { ...dispatch, generation: "new-service" });
      if (boundary === "token") useSessionStore.getState().setToken("new-account");
      if (boundary === "account") useSessionStore.setState({ accountGeneration: first.accountGeneration + 1 });
      if (boundary === "token" || boundary === "account") {
        useSessionStore.setState({ sessions: [b], archivedSessions: [], activeSessionId: b.id });
      }
    });
    // A new account's dialog and composer belong exclusively to that identity.
    if ((boundary === "token" || boundary === "account") && mode === "rewind") {
      command("/rewind");
      await waitFor(() => expect(screen.getByTestId("fork-picker")).toBeVisible());
    }
    fireEvent.change(document.querySelector("textarea")!, { target: { value: "Current scope draft" } });
    await api.complete();
    expect(useSessionStore.getState().activeSessionId).toBe(boundary === "token" || boundary === "account" ? b.id : a.id);
    expect(api.fetcher.mock.calls.some(([url]) => new URL(url).pathname === "/api/sessions/child")).toBe(false);
    if (boundary === "token" || boundary === "account") {
      expect(useSessionStore.getState().sessions.map((row) => row.id)).toEqual([b.id]);
      expect(useSessionStore.getState().archivedSessions).toEqual([]);
      if (mode === "rewind") expect(screen.getByTestId("fork-picker")).toBeVisible();
    } else {
      await waitFor(() => expect(useSessionStore.getState().sessions.map((row) => row.id)).toEqual([b.id, child.id]));
      expect(useSessionStore.getState().archivedSessions.map((row) => row.id)).toEqual([a.id]);
      expect(screen.queryByTestId("fork-dialog")).toBeNull();
    }
    act(() => { sessions.disconnect(second); useSessionStore.getState().setActiveSessionId(b.id); });
    expect(document.querySelector("textarea")).toHaveValue("Current scope draft");
  });

it.each(["success", "failure"])("closing a rewind lets its old %s settle without changing a new instance", async (outcome) => {
  const api = server();
  mount();
  await launch("rewind");
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  await openRewind();
  fireEvent.change(screen.getByPlaceholderText("Fork label"), { target: { value: "Replacement label" } });
  if (outcome === "success") await api.complete();
  else await api.refuse();
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(screen.getByPlaceholderText("Fork label")).toHaveValue("Replacement label");
  expect(screen.queryByTestId("fork-error")).toBeNull();
  expect(screen.getByRole("button", { name: "Rewind" })).toBeEnabled();
  expect(useSessionStore.getState().sessions.some((row) => row.id === child.id)).toBe(outcome === "success");
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("a current rewind refusal releases its own form and preserves the label for retry", async () => {
  const api = server();
  mount();
  await openRewind();
  fireEvent.change(screen.getByPlaceholderText("Fork label"), { target: { value: "Keep my label" } });
  fireEvent.click(screen.getByRole("button", { name: "Rewind" }));
  await api.refuse();
  expect(screen.getByTestId("fork-error")).toHaveTextContent("Rewind refused");
  expect(screen.getByPlaceholderText("Fork label")).toHaveValue("Keep my label");
  expect(screen.getByRole("button", { name: "Rewind" })).toBeEnabled();
  expect(useSessionStore.getState().sessions.map((row) => row.id)).toEqual([a.id, b.id]);
});

it("a new rewind opening in the same session owns a fresh form and cannot inherit an old request", async () => {
  const api = server();
  useSessionStore.setState({ messages: { a: [{ role: "user", type: "text", seq: 7, content: "A original message" }] } });
  mount();
  fireEvent.click(await screen.findByTestId("fork-from-here"));
  await waitFor(() => expect(screen.getByRole("button", { name: "Rewind" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Rewind" }));
  // Another opening replaces the instance even when its session and seq match.
  fireEvent.click(screen.getByTestId("fork-from-here"));
  await waitFor(() => expect(screen.getByRole("button", { name: "Rewind" })).toBeEnabled());
  fireEvent.change(screen.getByPlaceholderText("Fork label"), { target: { value: "Replacement opening" } });
  await api.complete();
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(screen.getByPlaceholderText("Fork label")).toHaveValue("Replacement opening");
});

it.each(["command", "message"])("opens rewind through the %s entry without randomUUID", async (entry) => {
  vi.stubGlobal("crypto", { getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto) });
  server();
  useSessionStore.setState({ messages: { a: [{ role: "user", type: "text", seq: 7, content: "A original message" }] } });
  mount();
  if (entry === "command") command("/rewind");
  else fireEvent.click(await screen.findByTestId("fork-from-here"));
  expect(await screen.findByTestId("fork-dialog")).toBeVisible();
  if (entry === "command") expect(screen.getByTestId("fork-picker")).toBeVisible();
  else await waitFor(() => expect(screen.getByRole("button", { name: "Rewind" })).toBeEnabled());
});

it.each((["archive", "rewind"] as const).flatMap((mode) =>
  (["body", "list confirmation"] as const).map((phase) => ({ mode, phase }))))(
  "$mode loses foreground intent while awaiting $phase but retains the success fact", async ({ mode, phase }) => {
    const api = server();
    mount();
    await launch(mode);
    let body!: ReadableStreamDefaultController<Uint8Array>;
    if (phase === "body") {
      await api.complete(new Response(new ReadableStream<Uint8Array>({ start: (controller) => { body = controller; } })));
    } else {
      api.holdList();
      act(() => { sessions.patch(b.id, { name: "B updated" }, sessions.capture()); });
      await api.complete();
      await waitFor(() => expect(api.listPending()).toBe(true));
    }
    act(() => {
      useSessionStore.getState().setActiveSessionId(b.id);
      useSessionStore.getState().setActiveSessionId(a.id);
    });
    fireEvent.change(document.querySelector("textarea")!, { target: { value: "Draft made during confirmation" } });
    if (phase === "body") await act(async () => { body.enqueue(new TextEncoder().encode(JSON.stringify(child))); body.close(); });
    else await api.confirmList();
    expect(useSessionStore.getState().activeSessionId).toBe(a.id);
    expect(useSessionStore.getState().sessions.map((row) => row.id)).toEqual([b.id, child.id]);
    expect(useSessionStore.getState().archivedSessions.map((row) => row.id)).toEqual([a.id]);
    expect(screen.queryByTestId("fork-dialog")).toBeNull();
    act(() => useSessionStore.getState().setActiveSessionId(b.id));
    expect(document.querySelector("textarea")).toHaveValue("Draft made during confirmation");
  });
