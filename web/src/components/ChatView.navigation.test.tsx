import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { sessions } from "../lib/sessionList";
import { handleWsMessage } from "../hooks/useWebSocket";
import { observeSession } from "../lib/hydrateSession";

const a = { id: "a", name: "A", agent_id: "agent", status: "idle", backend: "codex",
  created_at: "2026-11-12", working_dir: "/tmp", archived: false,
  message_count: 0, transcript_integrity: "ok", origin: "user", can_fork: true,
  can_steer: true, can_compact: false, can_observe_context: true, fork_is_full_copy: false } as SessionInfo;
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

it.each(["application", "schedules"] as const)("archive broadcasts preserve %s foreground and existing successor content", async (mainView) => {
  server();
  mount();
  const message = { role: "assistant" as const, type: "text" as const, seq: 10, content: "Successor already has a reply" };
  const activeApplicationId = mainView === "application" ? "selected-app" : null;
  act(() => useSessionStore.setState({ mainView, activeApplicationId, sidebarOpen: true,
    messages: { child: [message] }, pendingQuestions: { child: [{ question_id: "question", questions: [{ question: "Choose" }] }] },
  }));
  const questions = useSessionStore.getState().pendingQuestions.child;
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name }));
  const state = useSessionStore.getState();
  expect(state.sessions.map((row) => row.id)).toEqual([b.id, child.id]);
  expect(state.archivedSessions.map((row) => row.id)).toEqual([a.id]);
  expect([state.activeSessionId, state.mainView, state.activeApplicationId, state.sidebarOpen]).toEqual([a.id, mainView, activeApplicationId, true]);
  expect(state.messages.child).toEqual([message]);
  expect(state.pendingQuestions.child).toEqual(questions);
});

it("keeps archived history in a receiving tab and offers its explicit successor", async () => {
  server();
  useSessionStore.setState({ messages: { a: [{ role: "assistant", type: "text", seq: 1, content: "Original history" }] } });
  mount();
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name }));
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  await waitFor(() => expect(screen.getByText("Original history")).toBeVisible());
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.getByRole("button", { name: "Open successor" })).toBeVisible();
});

it("keeps existing list order when an archive broadcast is duplicated", () => {
  server();
  const first = { ...a, id: "first", archived: true };
  const last = { ...a, id: "last", archived: true };
  useSessionStore.setState({ sessions: [child, b], archivedSessions: [first, { ...a, archived: true }, last] });
  const event = { type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name };
  handleWsMessage(event);
  handleWsMessage(event);
  expect(useSessionStore.getState().sessions.map((row) => row.id)).toEqual([child.id, b.id]);
  expect(useSessionStore.getState().archivedSessions.map((row) => row.id)).toEqual([first.id, a.id, last.id]);
});

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
  const finishLists: (() => void)[] = [];
  let rows: SessionInfo[] | undefined;
  let listFailure = false;
  let heldDetail: string | undefined;
  let finishDetail: (() => void) | undefined;
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
      if (holdList) await new Promise<void>((resolve) => { finishLists.push(resolve); });
      if (listFailure) return Response.json({ detail: "Unavailable" }, { status: 503 });
      const snapshot = rows ?? (saved ? [b, result, { ...a, archived: true }] : [a, b]);
      return Response.json(url.searchParams.has("include_archived") ? snapshot : snapshot.filter((row) => !row.archived));
    }
    const info = rows?.find((row) => url.pathname === `/api/sessions/${row.id}`) ??
      (url.pathname.includes("/child") ? result : url.pathname.includes("/b") ? b : a);
    if (url.pathname === `/api/sessions/${heldDetail}`) await new Promise<void>((resolve) => { finishDetail = resolve; });
    return Response.json({ ...info, messages: [{ role: "user", type: "text", seq: 7, content: `${info.name} original message` }],
      pending_questions: [], subagents: [], dispatch_state: dispatch });
  });
  vi.stubGlobal("fetch", fetcher);
  return {
    fetcher,
    setRows: (next: SessionInfo[]) => { rows = next; },
    failList: (fail: boolean) => { listFailure = fail; },
    complete: async (response?: Response) => { saved = true; await act(async () => finish(response ?? Response.json(result))); },
    refuse: async () => { await act(async () => finish(Response.json({ detail: { message: "Rewind refused" } }, { status: 409 }))); },
    holdList: () => { holdList = true; },
    listPending: () => finishLists.length > 0,
    confirmList: async () => { holdList = false; await act(async () => { finishLists.splice(0).forEach((finish) => finish()); }); },
    holdDetail: (id: string) => { heldDetail = id; },
    detailPending: () => !!finishDetail,
    confirmDetail: async () => { heldDetail = undefined; await act(async () => finishDetail?.()); },
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

it.each((["archive", "rewind"] as const).flatMap((mode) =>
  (["http-first", "ws-first"] as const).map((order) => ({ mode, order }))))(
  "$mode enters the confirmed result through normal selection after $order and owns its draft", async ({ mode, order }) => {
  const api = server();
  mount();
  await launch(mode);
  const event = { type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name };
  if (order === "ws-first") act(() => handleWsMessage(event));
  await api.complete();
  act(() => { handleWsMessage(event); handleWsMessage(event); });
  expect(useSessionStore.getState().activeSessionId).toBe(child.id);
  expect(useSessionStore.getState().activeAgentId).toBe(child.agent_id);
  expect(screen.queryByTestId("fork-dialog")).toBeNull();
  expect(document.querySelector("textarea")).toHaveValue(mode === "rewind" ? child.fork_prefilled_prompt : "");
  expect(useSessionStore.getState().messages[child.id]?.[0].content).toBe("Result original message");
  expect(useSessionStore.getState().archivedSessions.map((row) => row.id)).toEqual([a.id]);
});

it.each((["archive", "rewind"] as const).flatMap((mode) =>
  ([false, true] as const).map((rearchive) => ({ mode, rearchive }))))(
  "$mode retires its late receipt after restoration, rearchive=$rearchive", async ({ mode, rearchive }) => {
    const api = server();
    const newer = { ...child, id: "newer", name: "Newer result" };
    mount();
    await launch(mode);
    act(() => {
      handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name });
      handleWsMessage({ type: "session_unarchived", session_id: a.id, session: a });
      if (rearchive) handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: newer.id, name: newer.name });
    });
    api.setRows([b, child, ...(rearchive ? [newer, { ...a, archived: true }] : [a])]);
    await api.complete();
    expect(useSessionStore.getState().sessionSuccessors).toEqual(rearchive ? { a: newer.id } : {});
    expect(useSessionStore.getState().activeSessionId).toBe(a.id);
    expect(api.fetcher.mock.calls.some(([url]) => new URL(url).pathname === "/api/sessions/child")).toBe(false);
  });

it.each((["archive", "rewind"] as const).flatMap((mode) =>
  (["archived", "restored", "deleted"] as const).map((outcome) => ({ mode, outcome }))))(
  "$mode waits through a live list's missing source and its inclusive $outcome confirmation before its receipt", async ({ mode, outcome }) => {
    const api = server();
    mount();
    await launch(mode);
    api.setRows([b, child, ...(outcome === "deleted" ? [] : [{ ...a, archived: true }])]);
    // The server's live list has already observed the committed archive while
    // both the initiating HTTP response and the broadcast remain delayed.
    api.holdList();
    const liveRead = sessions.refresh();
    await waitFor(() => expect(api.listPending()).toBe(true));
    if (outcome === "restored") {
      // Deliver the live-only result, then restore before the inclusive read.
      const stop = useSessionStore.subscribe((state, before) => {
        if (before.sessions.some((row) => row.id === a.id) && !state.sessions.some((row) => row.id === a.id)) {
          api.setRows([a, b, child]);
          stop();
        }
      });
    }
    await api.confirmList();
    await liveRead;
    await waitFor(() => expect(useSessionStore.getState().sessionRead).toMatchObject({ loading: false, stale: false }));
    expect(api.fetcher.mock.calls.filter(([url]) => new URL(url).pathname === "/api/sessions")
      .map(([url]) => new URL(url).searchParams.has("include_archived"))).toEqual([false, true]);
    await api.complete();
    if (outcome === "archived") {
      expect(useSessionStore.getState().activeSessionId).toBe(child.id);
      expect(useSessionStore.getState().sessionSuccessors).toEqual({ a: child.id });
      expect(useSessionStore.getState().archivedSessions.map((row) => row.id)).toContain(a.id);
    } else {
      expect(useSessionStore.getState().activeSessionId).not.toBe(child.id);
      expect(useSessionStore.getState().sessionSuccessors).toEqual({});
    }
  });

it("confirms and opens a known successor through normal selection", async () => {
  const api = server();
  api.setRows([b, child, { ...a, archived: true }]);
  mount();
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name }));
  fireEvent.click(screen.getByRole("button", { name: "Open successor" }));
  await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(child.id));
  await waitFor(() => expect(useSessionStore.getState().messages.child?.[0].content).toBe("Result original message"));
  expect(api.fetcher.mock.calls.some(([url]) => new URL(url).searchParams.get("include_archived") === "true")).toBe(true);
  expect(api.fetcher.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
});

it.each((["archive", "rewind"] as const).flatMap((mode) =>
  (["archived", "restored", "deleted"] as const).map((outcome) => ({ mode, outcome }))))(
  "$mode does not navigate from a partial list before the inclusive $outcome confirmation", async ({ mode, outcome }) => {
    const api = server();
    mount();
    await launch(mode);
    api.setRows([b, child, { ...a, archived: true }]);
    const stop = useSessionStore.subscribe((state, before) => {
      if (before.sessions.some((row) => row.id === a.id) && !state.sessions.some((row) => row.id === a.id)) {
        api.holdList();
        stop();
      }
    });
    await act(async () => { await sessions.refresh(); });
    await waitFor(() => expect(api.listPending()).toBe(true));
    await api.complete();
    expect(useSessionStore.getState().activeSessionId).toBe(a.id);
    expect(useSessionStore.getState().sessionSuccessors).toEqual({});
    api.setRows([b, child, ...(outcome === "deleted" ? [] : [{ ...a, archived: outcome === "archived" }])]);
    await api.confirmList();
    await waitFor(() => expect(useSessionStore.getState().sessionRead).toMatchObject({ loading: false, stale: false }));
    if (outcome === "archived") {
      expect(useSessionStore.getState().activeSessionId).toBe(child.id);
      expect(useSessionStore.getState().sessionSuccessors).toEqual({ a: child.id });
    } else {
      expect(useSessionStore.getState().activeSessionId).not.toBe(child.id);
      expect(useSessionStore.getState().sessionSuccessors).toEqual({});
    }
  });

it.each(["archive", "rewind"] as const)(
  "a restored source can start a new %s while an old inclusive confirmation is pending", async (mode) => {
    const api = server();
    mount();
    api.setRows([b]);
    const stop = useSessionStore.subscribe((state, before) => {
      if (before.sessions.some((row) => row.id === a.id) && !state.sessions.some((row) => row.id === a.id)) {
        api.holdList();
        stop();
      }
    });
    await act(async () => { await sessions.refresh(); });
    await waitFor(() => expect(api.listPending()).toBe(true));
    act(() => handleWsMessage({ type: "session_unarchived", session_id: a.id, session: a }));
    await launch(mode);
    act(() => handleWsMessage({ type: "status", session_id: b.id, status: "running" }));
    api.setRows([b, child, { ...a, archived: true }]);
    await api.complete();
    await api.confirmList();
    await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(child.id));
    expect(useSessionStore.getState().sessionSuccessors).toEqual({ a: child.id });
    expect(useSessionStore.getState().archivedSessions.map((row) => row.id)).toContain(a.id);
  });

it("does not publish an archive relationship into an account changed after confirmation", async () => {
  const api = server();
  mount();
  command("/archive");
  act(() => sessions.patch(b.id, { name: "Newer name" }));
  api.holdList();
  await api.complete();
  await waitFor(() => expect(api.listPending()).toBe(true));
  // confirmLive resumes first; retire the account before created's outer
  // continuation can publish the relationship or return navigation rights.
  const switched = sessions.refresh(true).then(() => useSessionStore.getState().setToken("new-account"));
  await api.confirmList();
  await switched;
  expect(useSessionStore.getState().sessionSuccessors).toEqual({});
  expect(useSessionStore.getState().activeSessionId).not.toBe(child.id);
});

it("confirms a sparse archive broadcast while the original detail is pending without an external refresh", async () => {
  const api = server();
  useSessionStore.setState({ sessions: [b], messages: { a: [{ role: "assistant", type: "text", seq: 1, content: "Original history" }] } });
  mount();
  api.holdDetail(a.id);
  const detail = observeSession(a.id, "detail");
  await waitFor(() => expect(api.detailPending()).toBe(true));
  api.setRows([b, child, { ...a, archived: true }]);
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name }));
  expect(await screen.findByRole("button", { name: "Open successor" })).toBeVisible();
  await api.confirmDetail();
  await detail.done;
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(useSessionStore.getState().sessions.some((row) => row.id === a.id)).toBe(false);
  expect(screen.queryByRole("textbox")).toBeNull();
  await waitFor(() => expect(screen.getByText("Original history")).toBeVisible());
});

it("keeps a failed confirmation readable and retries only the read", async () => {
  const api = server();
  api.setRows([b, child, { ...a, archived: true }]);
  api.failList(true);
  mount();
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name }));
  fireEvent.click(screen.getByRole("button", { name: "Open successor" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not load sessions (HTTP 503)");
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  api.failList(false);
  fireEvent.click(screen.getByRole("button", { name: "Open successor" }));
  await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(child.id));
  expect(api.fetcher.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
});

it.each(["restore", "restore-and-archive", "delete-target", "leave-return", "connection", "service", "account"])(
  "retires a successor click across %s while confirmation is pending", async (boundary) => {
    const api = server();
    api.setRows([b, child, { ...a, archived: true }]);
    mount();
    const event = { type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name };
    act(() => handleWsMessage(event));
    api.holdList();
    fireEvent.click(screen.getByRole("button", { name: "Open successor" }));
    await waitFor(() => expect(api.listPending()).toBe(true));
    act(() => {
      if (boundary === "restore" || boundary === "restore-and-archive") {
        handleWsMessage({ type: "session_unarchived", session_id: a.id, session: a });
        expect(useSessionStore.getState().sessions.some((row) => row.id === a.id)).toBe(true);
        if (boundary === "restore-and-archive") handleWsMessage(event);
        else api.setRows([a, b, child]);
      } else if (boundary === "delete-target") {
        handleWsMessage({ type: "session_deleted", session_id: child.id });
        api.setRows([b, { ...a, archived: true }]);
      } else if (boundary === "leave-return") {
        useSessionStore.getState().openApplication("app");
        useSessionStore.getState().showChat();
      } else if (boundary === "connection") {
        sessions.connect({ socket: {}, accountGeneration: useSessionStore.getState().accountGeneration });
      } else if (boundary === "service") {
        useSessionStore.getState().applyDispatchState(a.id, { ...dispatch, generation: "new-service" });
      } else useSessionStore.getState().setToken("other-account");
    });
    await api.confirmList();
    expect(useSessionStore.getState().activeSessionId).not.toBe(child.id);
    expect(api.fetcher.mock.calls.some(([url]) => new URL(url).pathname === "/api/sessions/child")).toBe(false);
    expect(api.fetcher.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

it("opens the direct successor's archived history without following its own successor", async () => {
  const api = server();
  const grandchild = { ...child, id: "grandchild", name: "Later successor" };
  api.setRows([b, grandchild, { ...a, archived: true }, { ...child, archived: true }]);
  mount();
  const event = { type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name };
  act(() => {
    handleWsMessage(event);
    handleWsMessage({ type: "session_archived", old_session_id: child.id, new_session_id: grandchild.id, name: grandchild.name });
    handleWsMessage(event);
  });
  expect(useSessionStore.getState().archivedSessions.map((row) => row.id)).toContain(child.id);
  fireEvent.click(screen.getByRole("button", { name: "Open successor" }));
  await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(child.id));
  await waitFor(() => expect(screen.getByText("Result original message")).toBeVisible());
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(api.fetcher.mock.calls.some(([url]) => new URL(url).pathname === "/api/sessions/grandchild")).toBe(false);
});

it.each(["no-successor", "unknown-after-reload"])("keeps read-only history for %s without inferring a link", async (mode) => {
  server();
  if (mode === "unknown-after-reload") useSessionStore.setState({ sessions: [child], archivedSessions: [{ ...a, archived: true }] });
  mount();
  if (mode === "no-successor") act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: null }));
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.queryByRole("button", { name: "Open successor" })).toBeNull();
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
