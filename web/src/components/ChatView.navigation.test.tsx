import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { mockChatItemLayout, mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { sessions } from "../lib/sessionList";
import { handleWsMessage } from "../hooks/useWebSocket";
import { observeSession } from "../lib/hydrateSession";
import { deferred } from "../test-support/deferred";

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
    sessions: [a, b], archivedSessions: [], agents: [], credentials: [],
    messages: {}, streamingText: {}, lastAppliedSeq: {}, oldestSeq: {}, hasMoreMessages: {},
    pendingQuestions: {}, closedQuestionIds: {}, subagents: {}, wikiJobs: {}, pendingForks: {},
    dispatchUnavailable: {}, dispatchStates: { a: dispatch, b: dispatch },
    dispatchGeneration: "service", retiredDispatchGenerations: [],
  });
});
afterEach(() => { cleanup(); useSessionStore.getState().setToken(""); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it.each(["application", "schedules"] as const)("archive broadcasts preserve %s foreground and existing successor content", async (mainView) => {
  const api = server();
  api.setRows([b, child, { ...a, archived: true }]);
  mount();
  const message = { role: "assistant" as const, type: "text" as const, seq: 10, content: "Successor already has a reply" };
  const activeApplicationId = mainView === "application" ? "selected-app" : null;
  act(() => useSessionStore.setState({ mainView, activeApplicationId, sidebarOpen: true,
    messages: { child: [message] }, pendingQuestions: { child: [{ question_id: "question", questions: [{ question: "Choose" }] }] },
  }));
  const questions = useSessionStore.getState().pendingQuestions.child;
  await act(async () => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name }));
  const state = useSessionStore.getState();
  expect(state.sessions.map((row) => row.id)).toEqual([b.id, child.id]);
  expect(state.archivedSessions.map((row) => row.id)).toEqual([a.id]);
  expect([state.activeSessionId, state.mainView, state.activeApplicationId, state.sidebarOpen]).toEqual([a.id, mainView, activeApplicationId, true]);
  expect(state.messages.child).toEqual([message]);
  expect(state.pendingQuestions.child).toEqual(questions);
});

it("keeps archived history in a receiving tab and offers its explicit successor", async () => {
  const api = server();
  api.setRows([b, child, { ...a, archived: true }]);
  useSessionStore.setState({ messages: { a: [{ role: "assistant", type: "text", seq: 1, content: "Original history" }] } });
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Browse loaded content" }));
  await act(async () => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name }));
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  await waitFor(() => expect(screen.getByText("Original history")).toBeVisible());
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.getByRole("button", { name: "Open successor" })).toBeVisible();
});

it("keeps an already deleted successor unavailable when its delayed archive broadcast cannot be confirmed", async () => {
  const api = server();
  useSessionStore.setState({ sessions: [a, b, child] });
  mount();
  act(() => handleWsMessage({ type: "session_deleted", session_id: child.id }));
  api.failList(true);
  await act(async () => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name }));
  expect(screen.queryByRole("button", { name: "Open successor" })).toBeNull();
  expect(screen.getByRole("button", { name: "Check successor" })).toBeVisible();
  expect(useSessionStore.getState().sessionRead.error).toContain("HTTP 503");
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);

  api.failList(false);
  api.setRows([b, { ...a, archived: true }]);
  await act(async () => { await sessions.refresh(true); });
  expect(useSessionStore.getState().sessionSuccessors[a.id]).toBeUndefined();
  expect(screen.queryByRole("button", { name: "Open successor" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Check successor" })).toBeNull();
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
  mockChatItemLayout();
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("chat-messages") ? 600 : 0;
  });
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
  let fail!: (reason: Error) => void;
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
      return new Promise<Response>((resolve, reject) => { finish = resolve; fail = reject; });
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
    commit: () => { saved = true; },
    failList: (fail: boolean) => { listFailure = fail; },
    complete: async (response?: Response) => { saved = true; await act(async () => finish(response ?? Response.json(result))); },
    loseResponse: async () => { saved = true; await act(async () => fail(new TypeError("Network connection lost"))); },
    refuse: async () => { await act(async () => finish(Response.json({ detail: { message: "Rewind refused" } }, { status: 409 }))); },
    holdList: () => { holdList = true; },
    listPending: () => finishLists.length > 0,
    confirmList: async () => { holdList = false; await act(async () => { finishLists.splice(0).forEach((finish) => finish()); }); },
    holdDetail: (id: string) => { heldDetail = id; },
    detailPending: () => !!finishDetail,
    confirmDetail: async () => { heldDetail = undefined; await act(async () => finishDetail?.()); },
  };
}

it("keeps the source selected when a file was attached while archive was in flight", async () => {
  const api = server();
  const previous = api.fetcher.getMockImplementation()!;
  api.fetcher.mockImplementation(async (url, init) => new URL(url).pathname.endsWith("/attachments")
    ? Response.json({ id: "new-file", filename: "notes.txt" }) : previous(url, init));
  mount();
  command("/archive");
  fireEvent.change(screen.getByTestId("attachment-file-input"), { target: { files: [new File(["read later"], "notes.txt")] } });
  await api.complete();
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(screen.getByRole("button", { name: "Open successor" })).toBeVisible();
  expect(api.fetcher.mock.calls.filter(([url]) => new URL(url).pathname.endsWith("/archive"))).toHaveLength(1);
  expect(api.fetcher.mock.calls.some(([url]) => new URL(url).pathname === `/api/sessions/${child.id}/attachments`)).toBe(false);
});

it("settles an automatically confirmed archive after an attachment revokes handoff", async () => {
  const api = server();
  const previous = api.fetcher.getMockImplementation()!;
  api.fetcher.mockImplementation(async (url, init) => new URL(url).pathname.endsWith("/attachments")
    ? Response.json({ id: "new-file", filename: "notes.txt" }) : previous(url, init));
  mount();
  command("/archive");
  fireEvent.change(screen.getByTestId("attachment-file-input"), {
    target: { files: [new File(["read later"], "notes.txt")] },
  });
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name }));
  api.holdList();
  await api.loseResponse();
  expect(screen.getByText("Checking archive…")).toHaveAttribute("role", "status");
  expect(screen.queryByRole("alert")).toBeNull();
  await api.confirmList();
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(screen.getByRole("alert")).toHaveTextContent("Archive confirmed. Viewing the archived conversation.");
  expect(screen.queryByRole("button", { name: "Confirm original archive" })).toBeNull();
  expect(screen.getByRole("button", { name: "Open successor" })).toBeVisible();
  expect(api.fetcher.mock.calls.some(([url]) => new URL(url).pathname === `/api/sessions/${child.id}`)).toBe(false);
  expect(api.fetcher.mock.calls.filter(([url]) => new URL(url).pathname.endsWith("/archive"))).toHaveLength(1);
});

it.each(["restore", "restore and archive again"])("drops the old archive feedback after %s during the successor read", async (change) => {
  const api = server();
  const archive = { type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name };
  mount();
  api.holdDetail(child.id);
  command("/archive");
  act(() => handleWsMessage(archive));
  await api.loseResponse();
  expect(api.detailPending()).toBe(true);
  expect(screen.getByText("Checking archive…")).toBeVisible();
  expect(screen.queryByRole("textbox")).toBeNull();

  act(() => {
    handleWsMessage({ type: "session_unarchived", session_id: a.id, session: a });
    if (change === "restore and archive again") handleWsMessage(archive);
    else api.setRows([a, b, child]);
  });
  await api.confirmDetail();

  if (change === "restore") expect(screen.getByRole("textbox")).toBeEnabled();
  else expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.queryByText("Checking archive…")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(api.fetcher.mock.calls.filter(([url]) => new URL(url).pathname.endsWith("/archive"))).toHaveLength(1);
});

it.each([["lost", 200], ["lost", 503], ["201", 200], ["201", 503]] as const)(
  "keeps the %s archive receipt across reconnect during a successor read that returns HTTP %s", async (receipt, status) => {
    const api = server();
    const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
    const next = { ...source, socket: {} };
    const fetchSession = api.fetcher.getMockImplementation()!;
    api.fetcher.mockImplementation(async (url, init) => {
      const response = await fetchSession(url, init);
      return status === 503 && new URL(url).pathname === `/api/sessions/${child.id}`
        ? Response.json({ detail: "Unavailable successor" }, { status }) : response;
    });
    await act(async () => { sessions.connect(source); });
    mount();
    api.holdDetail(child.id);
    command("/archive");
    api.commit();
    act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name }, source));
    if (receipt === "lost") await api.loseResponse();
    else await api.complete(Response.json({ ...child, forked_from_session_id: null, fork_prefilled_prompt: null }, { status: 201 }));
    expect(api.detailPending()).toBe(true);
    expect(screen.getByText("Checking archive…")).toBeVisible();

    await act(async () => { sessions.disconnect(source); });
    const disconnectedFeedback = screen.queryByRole("alert")?.textContent ?? "";
    await act(async () => { sessions.connect(next); });
    await api.confirmDetail();

    const receiptMessage = receipt === "lost" ? "Original command result is unknown" : "Archive committed";
    expect(disconnectedFeedback).toContain(receiptMessage);
    const feedback = screen.getByRole("alert");
    expect(feedback).toHaveTextContent(receiptMessage);
    if (receipt === "lost") {
      expect(disconnectedFeedback).toContain("Archive confirmed");
      expect(feedback).toHaveTextContent("Archive confirmed");
    } else expect(feedback).not.toHaveTextContent("unknown");
    expect(feedback).not.toHaveTextContent("Could not load session state");
    expect(screen.getByRole("button", { name: "Open successor" })).toBeEnabled();
    expect(screen.queryByText("Checking archive…")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(useSessionStore.getState().activeSessionId).toBe(a.id);
    expect(api.fetcher.mock.calls.filter(([url]) => new URL(url).pathname.endsWith("/archive"))).toHaveLength(1);
    await act(async () => { sessions.disconnect(next); });
  },
);

it.each(["restore", "restore and archive again", "new service"])(
  "retires a published archive confirmation after reconnect and %s while keeping the unknown receipt", async (change) => {
    const api = server();
    const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
    const next = { ...source, socket: {} };
    const archive = { type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name };
    await act(async () => { sessions.connect(source); });
    mount();
    api.holdDetail(child.id);
    command("/archive");
    act(() => handleWsMessage(archive, source));
    await api.loseResponse();
    expect(api.detailPending()).toBe(true);
    expect(screen.getByText("Checking archive…")).toBeVisible();

    await act(async () => { sessions.disconnect(source); });
    expect(screen.getByRole("alert")).toHaveTextContent("Archive confirmed");
    await act(async () => { sessions.connect(next); });
    expect(screen.getByRole("alert")).toHaveTextContent("Archive confirmed");

    await act(async () => {
      if (change === "new service") {
        useSessionStore.getState().applyDispatchState(a.id, { ...dispatch, generation: "new-service" });
      } else {
        handleWsMessage({ type: "session_unarchived", session_id: a.id, session: a }, next);
        if (change === "restore and archive again") handleWsMessage(archive, next);
        else api.setRows([a, b, child]);
      }
    });
    await api.confirmDetail();

    const feedback = screen.getByRole("alert");
    expect(feedback).toHaveTextContent("Original command result is unknown");
    expect(feedback).not.toHaveTextContent("Archive confirmed");
    if (change === "restore") expect(screen.getByRole("textbox")).toBeEnabled();
    else expect(screen.queryByRole("textbox")).toBeNull();
    expect(useSessionStore.getState().activeSessionId).toBe(a.id);
    expect(api.fetcher.mock.calls.filter(([url]) => new URL(url).pathname.endsWith("/archive"))).toHaveLength(1);
    await act(async () => { sessions.disconnect(next); });
  },
);

it("keeps the successful archive receipt after restoration retires a displayed successor read error", async () => {
  const api = server();
  const fetchSession = api.fetcher.getMockImplementation()!;
  api.fetcher.mockImplementation(async (url, init) => new URL(url).pathname === `/api/sessions/${child.id}`
    ? Response.json({ detail: "Unavailable successor" }, { status: 503 }) : fetchSession(url, init));
  mount();
  command("/archive");
  await api.complete();
  expect(screen.getByRole("alert")).toHaveTextContent("Archive committed");
  expect(screen.getByRole("alert")).toHaveTextContent("Could not load session state (HTTP 503)");

  act(() => handleWsMessage({ type: "session_unarchived", session_id: a.id, session: a }));
  expect(screen.getByRole("textbox")).toBeEnabled();
  expect(screen.getByRole("alert")).toHaveTextContent("Archive committed");
  expect(screen.getByRole("alert")).not.toHaveTextContent("Could not load session state");
  expect(api.fetcher.mock.calls.filter(([url]) => new URL(url).pathname.endsWith("/archive"))).toHaveLength(1);
});

it("clears a displayed successor read error on disconnect while preserving the archive confirmation and receipt", async () => {
  const api = server();
  const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
  const fetchSession = api.fetcher.getMockImplementation()!;
  api.fetcher.mockImplementation(async (url, init) => new URL(url).pathname === `/api/sessions/${child.id}`
    ? Response.json({ detail: "Unavailable successor" }, { status: 503 }) : fetchSession(url, init));
  await act(async () => { sessions.connect(source); });
  mount();
  command("/archive");
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name }, source));
  await api.loseResponse();
  expect(screen.getByRole("alert")).toHaveTextContent("Could not load session state (HTTP 503)");

  await act(async () => { sessions.disconnect(source); });
  const feedback = screen.getByRole("alert");
  expect(feedback).not.toHaveTextContent("Could not load session state");
  expect(feedback).toHaveTextContent("Archive confirmed");
  expect(feedback).toHaveTextContent("Original command result is unknown");
  expect(screen.getByRole("button", { name: "Open successor" })).toBeEnabled();
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(api.fetcher.mock.calls.filter(([url]) => new URL(url).pathname.endsWith("/archive"))).toHaveLength(1);
});

it("can still confirm the original uncertain archive after attaching a file", async () => {
  const api = server();
  api.failList(true);
  const previous = api.fetcher.getMockImplementation()!;
  api.fetcher.mockImplementation(async (url, init) => new URL(url).pathname.endsWith("/attachments")
    ? Response.json({ id: "new-file", filename: "notes.txt" }) : previous(url, init));
  mount();
  command("/archive");
  await api.loseResponse();
  const confirmation = screen.getByRole("button", { name: "Confirm original archive" });
  expect(confirmation).toBeEnabled();
  fireEvent.change(screen.getByTestId("attachment-file-input"), {
    target: { files: [new File(["read later"], "notes.txt")] },
  });
  api.failList(false);
  fireEvent.click(confirmation);
  await waitFor(() => expect(useSessionStore.getState().archivedSessions.map((row) => row.id)).toEqual([a.id]));
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(api.fetcher.mock.calls.filter(([url]) => new URL(url).pathname.endsWith("/archive"))).toHaveLength(1);
});

it("keeps the source readable when the waiting successor is archived", async () => {
  const api = server();
  api.holdDetail(child.id);
  mount();
  command("/archive");
  await api.complete();
  await waitFor(() => expect(api.detailPending()).toBe(true));
  api.setRows([b, { ...a, archived: true }, { ...child, archived: true }]);
  act(() => handleWsMessage({ type: "session_archived", old_session_id: child.id, new_session_id: null }));
  await api.confirmDetail();
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(screen.getByRole("button", { name: "Open successor" })).toBeVisible();
});

it.each(["empty", "preexisting reply", "unavailable"] as const)(
  "waits for the actual successor transcript when its list count is zero: %s", async (outcome) => {
  const api = server();
  api.holdDetail(child.id);
  const previous = api.fetcher.getMockImplementation()!;
  api.fetcher.mockImplementation(async (url, init) => {
    const response = await previous(url, init);
    if (init?.method === "POST" || new URL(url).pathname !== `/api/sessions/${child.id}`) return response;
    if (outcome === "unavailable") return Response.json({ detail: "Detail unavailable" }, { status: 503 });
    return Response.json({ ...await response.json(), messages: [],
      oldest_loaded_seq: null, next_message_seq: 0, has_more_messages: false });
  });
  useSessionStore.setState({ messages: { a: [{ role: "assistant", type: "text", seq: 1, content: "Original history" }] } });
  const view = mount();
  fireEvent.click(screen.getByRole("button", { name: "Browse loaded content" }));
  command("/archive");
  await api.complete();
  await waitFor(() => expect(api.detailPending()).toBe(true));
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(screen.getByText("Original history")).toBeVisible();
  if (outcome === "preexisting reply") {
    act(() => useSessionStore.getState().addMessage(child.id, {
      role: "assistant", type: "text", seq: 1, content: "Reply accepted before detail",
    }));
  }
  await api.confirmDetail();
  if (outcome === "preexisting reply") {
    await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(child.id));
    expect(screen.getByText("Reply accepted before detail")).toBeVisible();
  }
  expect(view.container.querySelector("[data-chat-session][inert]")).toBeNull();
  expect(api.fetcher.mock.calls.filter(([url]) => new URL(url).pathname.endsWith("/archive"))).toHaveLength(1);
  if (outcome === "empty") {
    expect(useSessionStore.getState().activeSessionId).toBe(child.id);
    expect(screen.getByRole("textbox")).toHaveValue("");
    expect(view.container.querySelector(".chat-messages")).toBeVisible();
  } else if (outcome === "unavailable") {
    expect(useSessionStore.getState().activeSessionId).toBe(a.id);
    expect(screen.getByText("Original history")).toBeVisible();
    expect(screen.getByRole("button", { name: "Open successor" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load session state (HTTP 503)");
  }
});

it("resumes a hidden successor's remaining budget and ignores layout after it expires", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] });
  const visibility = vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  const api = server();
  const view = mount();
  const layout = vi.mocked(HTMLElement.prototype.getBoundingClientRect).getMockImplementation()!;
  let ready = false;
  vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function (this: HTMLElement) {
    if (!ready && this.hasAttribute("data-index") && this.closest(`[data-chat-session="${child.id}"]`)) return new DOMRect();
    return layout.call(this);
  });
  command("/archive");
  await api.complete();
  const staged = () => view.container.querySelector(`[data-chat-session="${child.id}"]`);
  expect(staged()).toHaveAttribute("inert");
  act(() => vi.advanceTimersByTime(900));
  act(() => {
    visibility.mockReturnValue(true);
    document.dispatchEvent(new Event("visibilitychange"));
    vi.advanceTimersByTime(20_000);
  });
  expect(staged()).toHaveAttribute("inert");
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  act(() => {
    visibility.mockReturnValue(false);
    document.dispatchEvent(new Event("visibilitychange"));
    vi.advanceTimersByTime(1099);
  });
  expect(staged()).toHaveAttribute("inert");
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(staged()).toBeNull();
  expect(screen.getByRole("alert")).toHaveTextContent("The successor could not be positioned");
  expect(screen.getByRole("button", { name: "Open successor" })).toBeVisible();
  ready = true;
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
});

it("reports an archive refusal with its server reason and never confirms or retries the write", async () => {
  const api = server();
  mount();
  command("/archive");
  await api.complete(Response.json({ detail: "Archive permission denied" }, { status: 403 }));
  expect(screen.getByRole("alert")).toHaveTextContent("Archive permission denied");
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  expect(api.fetcher.mock.calls.filter(([url]) => new URL(url).pathname === "/api/sessions")).toHaveLength(0);
});

it.each(["headers", "success body", "error body"])("bounds the original archive %s and offers only manual read after confirmation expires", async (stage) => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  const api = server();
  mount();
  api.holdList();
  command("/archive");
  if (stage !== "headers") {
    const response = new Response(null, { status: stage === "success body" ? 201 : 500 });
    response.json = () => new Promise(() => {});
    await api.complete(response);
  }
  await act(async () => { await vi.advanceTimersByTimeAsync(29_999); });
  expect(screen.queryByText("Checking archive…")).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(screen.getByText("Checking archive…")).toHaveAttribute("role", "status");
  const post = api.fetcher.mock.calls.find(([, init]) => init?.method === "POST")!;
  expect(post[1]!.signal!.aborted).toBe(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(screen.getByRole("alert")).toHaveTextContent("Archive result is still unknown");
  expect(screen.getByRole("button", { name: "Confirm original archive" })).toBeEnabled();
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  // The timed-out POST body/header can arrive later, but has no remaining
  // authority over this confirmation or the original operation's feedback.
  if (stage === "headers") await api.complete(Response.json(child));
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(screen.getByRole("alert")).toHaveTextContent("Archive result is still unknown");
  await api.confirmList();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Confirm original archive" })); });
  expect(screen.getByRole("alert")).toHaveTextContent("Archive confirmed");
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it.each([201, 409])("shares the POST header/body deadline and rejects a late HTTP %s body during manual confirmation", async (status) => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  const api = server();
  mount(); api.holdList(); command("/archive");
  await act(async () => { await vi.advanceTimersByTimeAsync(29_000); });
  const body = deferred<unknown>();
  const response = new Response(null, { status });
  response.json = () => body.promise;
  await api.complete(response);
  await act(async () => { await vi.advanceTimersByTimeAsync(999); });
  expect(screen.queryByRole("alert")).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(screen.getByText("Checking archive…")).toHaveAttribute("role", "status");
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  const manual = screen.getByRole("button", { name: "Confirm original archive" });
  fireEvent.click(manual);
  await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
  expect(manual).toBeDisabled();
  // The old body has no authority over this successor read or its deadline.
  await act(async () => body.resolve(status === 201 ? child : { detail: "Late refusal" }));
  expect(screen.getByRole("alert")).toHaveTextContent("Archive result is still unknown");
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  fireEvent.click(manual);
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(manual).toBeEnabled();
  expect(screen.getByRole("alert")).toHaveTextContent("Archive result is still unknown");
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("keeps a restored session's new archive separate from an old partial HTTP failure", async () => {
  const api = server();
  const previous = api.fetcher.getMockImplementation()!;
  const old = deferred<Response>(), next = deferred<Response>();
  const posts = [old, next];
  let count = 0;
  api.fetcher.mockImplementation(async (url, init) => init?.method === "POST"
    ? posts[count++].promise : previous(url, init));
  mount();
  command("/archive");
  api.setRows([b, child, { ...a, archived: true }]);
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name }));
  api.setRows([a, b, child]);
  act(() => handleWsMessage({ type: "session_unarchived", session_id: a.id, session: a }));
  command("/archive");
  await act(async () => old.resolve(Response.json({ old_session_id: a.id, archived: true,
    new_session_id: child.id, failure_stage: "successor_configuration", message: "Old configuration failure" }, { status: 500 })));
  expect(screen.queryByText(/Old configuration failure/)).toBeNull();
  const newer = { ...child, id: "newer-successor", name: "New successor" };
  api.setRows([b, child, newer, { ...a, archived: true }]);
  await act(async () => next.resolve(Response.json(newer, { status: 201 })));
  await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(newer.id));
  expect(count).toBe(2);
  expect(screen.queryByText(/Old configuration failure/)).toBeNull();
});

it.each(["successor_creation", "successor_configuration", "schedule_rebinding"])("preserves archive effect and known successor when %s fails", async (failure_stage) => {
  const api = server();
  mount();
  command("/archive");
  const new_session_id = failure_stage === "successor_creation" ? null : child.id;
  const result = { old_session_id: a.id, archived: true, new_session_id, failure_stage, message: "Storage refused" };
  await api.complete(Response.json(result, { status: 500 }));
  expect(screen.getByRole("alert")).toHaveTextContent("Archive committed");
  expect(screen.getByRole("alert")).toHaveTextContent("Storage refused");
  expect(screen.queryByText(/Archive refused/)).toBeNull();
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(useSessionStore.getState().archivedSessions.map((row) => row.id)).toContain(a.id);
  expect(useSessionStore.getState().sessionSuccessors[a.id] ?? null).toBe(new_session_id);
  if (new_session_id) expect(screen.getByRole("button", { name: "Open successor" })).toBeVisible();
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it.each(["receipt", "broadcast"] as const)("shows how to continue a legacy copy after a directory refusal from the %s", async (source) => {
  const api = server();
  const directory = "/srv/octopus/fork/work-legacyfork01";
  const copy = { ...a, working_dir: directory, origin: "fork", fork_is_full_copy: true } satisfies SessionInfo;
  useSessionStore.setState({ sessions: [copy, b] });
  mount();
  command("/archive");
  const result = {
    old_session_id: copy.id, archived: true, new_session_id: null, failure_stage: "successor_creation",
    message: `${directory} requires a directory grant. Ask an administrator to approve it, then restore the session and retry.`,
  };
  api.setRows([b, { ...copy, archived: true }]);
  if (source === "receipt") {
    await api.complete(Response.json(result, { status: 500 }));
  } else {
    act(() => handleWsMessage({
      type: "session_archived", old_session_id: copy.id, new_session_id: null, name: copy.name, archive_result: result,
    }));
    await api.loseResponse();
  }

  const alert = screen.getByRole("alert");
  expect(alert).toHaveTextContent(source === "receipt" ? "Archive committed" : "Session archived");
  expect(alert).toHaveTextContent(`Successor creation failed: ${result.message}`);
  expect(screen.queryByRole("button", { name: "Open successor" })).toBeNull();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(useSessionStore.getState().activeSessionId).toBe(copy.id);
  expect(api.fetcher.mock.calls.filter(([url]) => new URL(url).pathname.endsWith("/archive"))).toHaveLength(1);
});

it("retains the original archive through disconnect and uses a new read scope without reviving navigation", async () => {
  const api = server();
  const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
  sessions.connect(source);
  await act(async () => {});
  mount();
  command("/archive");
  act(() => sessions.disconnect(source));
  await api.loseResponse();
  expect(screen.getByRole("alert")).toHaveTextContent("Archive result is still unknown");
  const next = { ...source, socket: {} };
  act(() => sessions.connect(next));
  await act(async () => {});
  // The known direct successor arrives in the new connection; a manual
  // observation can confirm it but cannot restore the original navigation.
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name }));
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Confirm original archive" })); });
  expect(screen.getByRole("alert")).toHaveTextContent("Archive confirmed");
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(useSessionStore.getState().sessionSuccessors[a.id]).toBe(child.id);
  await act(async () => { sessions.patch(b.id, { status: "running" }); });
  expect(screen.getByRole("alert")).toHaveTextContent("Archive confirmed");
  api.setRows([a, b, child]);
  act(() => handleWsMessage({ type: "session_unarchived", session_id: a.id, session: a }, next));
  expect(screen.getByRole("textbox")).toBeEnabled();
  expect(screen.getByRole("alert")).toHaveTextContent("Original command result is unknown");
  expect(screen.getByRole("alert")).not.toHaveTextContent("Archive confirmed");
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  sessions.disconnect(next);
});

it("confirms the successor known from partial HTTP success after an unrelated list change", async () => {
  const api = server();
  mount();
  command("/archive");
  api.holdList();
  act(() => sessions.patch(b.id, { status: "running" }));
  await api.complete(Response.json({ old_session_id: a.id, archived: true, new_session_id: child.id,
    failure_stage: "successor_configuration", message: "Storage refused" }, { status: 500 }));
  expect(screen.getByRole("alert")).toHaveTextContent("Storage refused");
  await waitFor(() => expect(api.listPending()).toBe(true));
  await api.confirmList();
  await waitFor(() => expect(screen.getByRole("button", { name: "Open successor" })).toBeVisible());
  expect(useSessionStore.getState().sessionSuccessors[a.id]).toBe(child.id);
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("retires a late partial archive link when restoration arrives without the first archive broadcast", async () => {
  const api = server();
  mount();
  command("/archive");
  act(() => handleWsMessage({ type: "session_unarchived", session_id: a.id, session: a }));
  expect(useSessionStore.getState().sessions.map((row) => row.id)).toContain(a.id);
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: null }));
  api.holdList();
  api.setRows([b, child, { ...a, archived: true }]);
  const fresh = sessions.refresh(true);
  await api.complete(Response.json({ old_session_id: a.id, archived: true, new_session_id: child.id,
    failure_stage: "successor_configuration", message: "Old partial archive" }, { status: 500 }));
  await api.confirmList();
  await fresh;
  await waitFor(() => expect(useSessionStore.getState().sessionRead.stale).toBe(false));
  const state = useSessionStore.getState();
  expect(state.archivedSessions.map((row) => row.id)).toContain(a.id);
  expect(state.sessions.map((row) => row.id)).toContain(child.id);
  expect(state.sessionSuccessors[a.id]).toBeUndefined();
  expect(screen.getByRole("alert")).toHaveTextContent("Old partial archive");
  expect(screen.queryByRole("button", { name: "Open successor" })).toBeNull();
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("retires a full archive receipt after unmount, restoration and another archive without a successor", async () => {
  const api = server();
  const previous = mount();
  command("/archive");
  previous.unmount();
  mount();
  api.setRows([b, child, { ...a, archived: true }]);
  act(() => handleWsMessage({ type: "session_unarchived", session_id: a.id, session: a }));
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: null }));
  await api.complete(Response.json(child, { status: 201 }));
  const state = useSessionStore.getState();
  expect(state.archivedSessions.map((row) => row.id)).toContain(a.id);
  expect(state.sessions.map((row) => row.id)).toContain(child.id);
  expect(state.sessionSuccessors[a.id]).toBeUndefined();
  expect(state.activeSessionId).toBe(a.id);
  expect(screen.queryByRole("button", { name: "Open successor" })).toBeNull();
});

it("keeps a new queued fork when an older partial archive receipt arrives after restoration", async () => {
  const api = server();
  mount();
  command("/archive");
  const live = { ...a, status: "running" } as const;
  api.setRows([live, b, child]);
  act(() => handleWsMessage({ type: "session_unarchived", session_id: a.id, session: live }));
  act(() => useSessionStore.getState().setPendingFork(a.id, "After restoration"));
  const newFork = useSessionStore.getState().pendingForks[a.id];
  await api.complete(Response.json({ old_session_id: a.id, archived: true, new_session_id: child.id,
    failure_stage: "successor_configuration", message: "Old partial archive" }, { status: 500 }));
  expect(useSessionStore.getState().pendingForks[a.id]).toBe(newFork);
  expect(useSessionStore.getState().sessions.map((row) => row.id)).toContain(a.id);
  expect(useSessionStore.getState().sessionSuccessors[a.id]).toBeUndefined();
});

it.each(["broadcast first", "refusal first"])("settles a second archive's explicit refusal with %s", async (order) => {
  const api = server();
  const first = deferred<Response>(), second = deferred<Response>();
  const previous = api.fetcher.getMockImplementation()!;
  const posts = [first, second];
  api.fetcher.mockImplementation(async (url, init) => init?.method === "POST"
    ? posts.shift()!.promise : previous(url, init));
  const archive_result = { old_session_id: a.id, archived: true, new_session_id: child.id,
    failure_stage: "successor_configuration", message: "First archive setup failed" };
  const broadcast = () => act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id,
    new_session_id: child.id, name: child.name, archive_result }));
  const refuse = () => act(async () => second.resolve(Response.json({ detail: "Second archive refused" }, { status: 404 })));
  mount();
  command("/archive");
  command("/archive");
  api.setRows([b, child, { ...a, archived: true }]);
  if (order === "broadcast first") {
    broadcast();
    expect(screen.getByRole("alert")).toHaveTextContent("Session archived. Successor configuration failed: First archive setup failed");
    await refuse();
  } else { await refuse(); broadcast(); }
  await act(async () => first.resolve(Response.json(archive_result, { status: 500 })));
  expect(screen.getByRole("alert")).toHaveTextContent("Archive refused (HTTP 404): Second archive refused");
  expect(useSessionStore.getState().sessionSuccessors[a.id]).toBe(child.id);
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(2);
});

it("shows shared archive activity while the local HTTP headers are pending", async () => {
  const api = server();
  mount();
  command("/archive");
  const archive_result = { old_session_id: a.id, archived: true, new_session_id: child.id,
    failure_stage: "successor_configuration", message: "Inherited model could not be saved" };
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id,
    name: child.name, archive_result }));
  expect(screen.getByRole("alert")).toHaveTextContent("Session archived");
  expect(screen.getByRole("alert")).toHaveTextContent("Inherited model could not be saved");
  expect(screen.getByRole("button", { name: "Check successor" })).toBeVisible();
  await api.loseResponse();
  expect(screen.getByRole("alert")).toHaveTextContent("Original command result is unknown");
  expect(screen.getByRole("alert")).toHaveTextContent("Inherited model could not be saved");
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it.each(["network", "json", "shape", "null"])("confirms an unknown %s archive by reading without replaying the command", async (failure) => {
  const api = server();
  mount();
  command("/archive");
  api.holdList();
  if (failure === "network") await api.loseResponse();
  else await api.complete(failure === "json" ? new Response("invalid JSON") : Response.json(failure === "null" ? null : {}));
  await waitFor(() => expect(api.listPending()).toBe(true));
  expect(screen.getByText("Checking archive…")).toHaveAttribute("role", "status");
  expect(screen.queryByRole("alert")).toBeNull();
  await api.confirmList();
  expect(useSessionStore.getState().archivedSessions.map((row) => row.id)).toContain(a.id);
  // A list proves archive, but does not identify a successor by name or agent.
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(screen.getByRole("alert")).toHaveTextContent("Archive confirmed");
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("confirms a lost archive reply through a fresh read after an older GET, preserving the explicit successor", async () => {
  const api = server();
  mount();
  command("/archive");
  api.holdList();
  const oldRead = sessions.refresh(true);
  await waitFor(() => expect(api.listPending()).toBe(true));
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name }));
  const readsBefore = api.fetcher.mock.calls.filter(([url]) => new URL(url).pathname === "/api/sessions").length;
  const oldSignal = api.fetcher.mock.calls.find(([url]) => new URL(url).pathname === "/api/sessions")![1]!.signal!;
  await api.loseResponse();
  expect(oldSignal.aborted).toBe(false);
  expect(api.fetcher.mock.calls.filter(([url]) => new URL(url).pathname === "/api/sessions")).toHaveLength(readsBefore);
  await api.confirmList();
  await oldRead;
  await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(child.id));
  expect(api.fetcher.mock.calls.filter(([url]) => new URL(url).pathname === "/api/sessions")).toHaveLength(readsBefore + 1);
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("retains an unknown archive result when confirmation fails, without a write retry", async () => {
  const api = server();
  mount();
  command("/archive");
  api.failList(true);
  await api.loseResponse();
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Archive result is still unknown"));
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("keeps a concurrent archive broadcast separate from a lost local POST result", async () => {
  const api = server();
  mount();
  command("/archive");
  api.setRows([b, child, { ...a, archived: true }]);
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name,
    archive_result: { old_session_id: a.id, archived: true, new_session_id: child.id,
      failure_stage: "successor_configuration", message: "Other tab's settings failed" } }));
  api.holdList();
  await api.loseResponse();
  expect(api.listPending()).toBe(true);
  expect(screen.getByText("Checking archive…")).toHaveAttribute("role", "status");
  expect(screen.getByRole("alert")).toHaveTextContent("Other tab's settings failed");
  await api.confirmList();
  expect(screen.getByRole("alert")).toHaveTextContent("Original command result is unknown");
  expect(screen.getByRole("alert")).toHaveTextContent("Other tab's settings failed");
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(screen.getByRole("button", { name: "Open successor" })).toBeVisible();
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("keeps the shared failure stage when a partial broadcast arrives during manual confirmation", async () => {
  const api = server();
  mount();
  command("/archive");
  api.failList(true);
  await api.loseResponse();
  const manual = await screen.findByRole("button", { name: "Confirm original archive" });
  api.failList(false);
  api.holdList();
  fireEvent.click(manual);
  await waitFor(() => expect(api.listPending()).toBe(true));
  api.setRows([b, child, { ...a, archived: true }]);
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name,
    archive_result: { old_session_id: a.id, archived: true, new_session_id: child.id,
      failure_stage: "schedule_rebinding", message: "Schedule settings were unavailable" } }));
  await api.confirmList();
  expect(screen.getByRole("alert")).toHaveTextContent("Schedule rebinding failed: Schedule settings were unavailable");
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("shows partial archive activity after a lost POST's GET has confirmed only the archived row", async () => {
  const api = server();
  mount();
  command("/archive");
  api.setRows([b, { ...a, archived: true }]);
  await api.loseResponse();
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Archive confirmed"));
  api.setRows([b, child, { ...a, archived: true }]);
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name,
    archive_result: { old_session_id: a.id, archived: true, new_session_id: child.id,
      failure_stage: "schedule_rebinding", message: "Late schedule failure" } }));
  expect(screen.getByRole("alert")).toHaveTextContent("Late schedule failure");
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(await screen.findByRole("button", { name: "Open successor" })).toBeVisible();
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("receives partial archive activity on reconnect before a manual confirmation", async () => {
  const api = server();
  const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
  sessions.connect(source);
  await act(async () => {});
  mount();
  command("/archive");
  act(() => sessions.disconnect(source));
  await api.loseResponse();
  const manual = await screen.findByRole("button", { name: "Confirm original archive" });
  api.setRows([b, child, { ...a, archived: true }]);
  const next = { ...source, socket: {} };
  act(() => sessions.connect(next));
  await act(async () => {});
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name,
    archive_result: { old_session_id: a.id, archived: true, new_session_id: child.id,
      failure_stage: "successor_configuration", message: "Settings failure after reconnect" } }, next));
  expect(screen.getByRole("alert")).toHaveTextContent("Settings failure after reconnect");
  expect(manual).toBeEnabled();
  await act(async () => { fireEvent.click(manual); });
  expect(screen.getByRole("alert")).toHaveTextContent("Original command result is unknown");
  expect(screen.getByRole("alert")).toHaveTextContent("Settings failure after reconnect");
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  sessions.disconnect(next);
});

it.each([true, false])("continues an in-flight archive confirmation across its first generation (partial: %s)", async (partial) => {
  const api = server();
  useSessionStore.setState({ dispatchGeneration: null, retiredDispatchGenerations: [], dispatchStates: {}, sessionMembership: {} });
  mount();
  api.holdList();
  command("/archive");
  await api.loseResponse();
  await waitFor(() => expect(api.listPending()).toBe(true));
  api.setRows([b, child, { ...a, archived: true }]);
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name,
    dispatch_state: dispatch, ...(partial ? { archive_result: { old_session_id: a.id, archived: true, new_session_id: child.id,
      failure_stage: "successor_configuration", message: "First generation partial result" } } : {}) }));
  await api.confirmList();
  if (partial) {
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Original command result is unknown"));
    expect(screen.getByRole("alert")).toHaveTextContent("First generation partial result");
    expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  } else {
    await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(child.id));
  }
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it.each(["before", "after"])("retains manual confirmation when a shared failure arrives %s the failed read", async (arrival) => {
  const api = server();
  mount();
  command("/archive");
  api.failList(true);
  api.holdList();
  await api.loseResponse();
  await waitFor(() => expect(api.listPending()).toBe(true));
  api.setRows([b, child, { ...a, archived: true }]);
  const broadcast = () => act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id,
    new_session_id: child.id, name: child.name,
    archive_result: { old_session_id: a.id, archived: true, new_session_id: child.id,
      failure_stage: "schedule_rebinding", message: "Schedule work failed" } }));
  if (arrival === "before") broadcast();
  await api.confirmList();
  if (arrival === "after") broadcast();
  expect(screen.getByRole("alert")).toHaveTextContent("Archive result is still unknown");
  expect(screen.getByRole("alert")).toHaveTextContent("Schedule work failed");
  expect(screen.getByRole("button", { name: "Confirm original archive" })).toBeEnabled();
  act(() => handleWsMessage({ type: "session_unarchived", session_id: a.id, session: a }));
  expect(screen.getByRole("alert")).toHaveTextContent("Archive result is still unknown");
  expect(screen.getByRole("alert")).not.toHaveTextContent("Schedule work failed");
  expect(screen.getByRole("button", { name: "Confirm original archive" })).toBeEnabled();
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("confirms a lost POST after the first archive frame establishes the service generation", async () => {
  const api = server();
  useSessionStore.setState({ dispatchGeneration: null, retiredDispatchGenerations: [], dispatchStates: {}, sessionMembership: {} });
  mount();
  command("/archive");
  api.setRows([b, child, { ...a, archived: true }]);
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name,
    dispatch_state: dispatch, archive_result: { old_session_id: a.id, archived: true, new_session_id: child.id,
      failure_stage: "successor_configuration", message: "Original settings failed" } }));
  api.holdList();
  await api.loseResponse();
  await waitFor(() => expect(api.listPending()).toBe(true));
  await api.confirmList();
  expect(screen.getByRole("alert")).toHaveTextContent("Original command result is unknown");
  expect(screen.getByRole("alert")).toHaveTextContent("Original settings failed");
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  expect(api.fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("clears the previous direct successor after a no-replacement archive following reconnect", async () => {
  const api = server();
  api.setRows([b, child, { ...a, archived: true }]);
  const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
  sessions.connect(source);
  await act(async () => {});
  mount();
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name }));
  expect(screen.getByRole("button", { name: "Open successor" })).toBeVisible();
  act(() => sessions.disconnect(source));
  const next = { ...source, socket: {} };
  act(() => sessions.connect(next));
  await act(async () => {});
  act(() => handleWsMessage({ type: "session_archived", old_session_id: a.id, new_session_id: null }));
  expect(useSessionStore.getState().sessionSuccessors[a.id]).toBeUndefined();
  expect(screen.queryByRole("button", { name: "Open successor" })).toBeNull();
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  sessions.disconnect(next);
});

it.each(["draft", "leave-return", "token"])("retires late archive feedback after %s", async (boundary) => {
  const api = server();
  mount();
  command("/archive");
  act(() => {
    if (boundary === "leave-return") {
      useSessionStore.getState().setActiveSessionId(b.id);
      useSessionStore.getState().setActiveSessionId(a.id);
    }
    if (boundary === "token") {
      useSessionStore.getState().setToken("replacement-account");
      useSessionStore.setState({ sessions: [a, b], activeSessionId: a.id });
    }
  });
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Current draft" } });
  await api.complete(Response.json({ detail: "Old archive failure" }, { status: 409 }));
  expect(screen.queryByText(/Old archive failure/)).toBeNull();
  expect(screen.getByRole("textbox")).toHaveValue("Current draft");
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
});

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
  "$mode enters the readable result after $order and owns its draft", async ({ mode, order }) => {
  const api = server();
  mount();
  await launch(mode);
  const event = { type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name };
  if (order === "ws-first") {
    api.commit();
    act(() => handleWsMessage(event));
  }
  await api.complete();
  act(() => { handleWsMessage(event); handleWsMessage(event); });
  await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(child.id));
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
      await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(child.id));
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
  fireEvent.click(await screen.findByRole("button", { name: "Open successor" }));
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
      await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(child.id));
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
  fireEvent.click(screen.getByRole("button", { name: "Browse loaded content" }));
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
  fireEvent.click(screen.getByRole("button", { name: "Check successor" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not load sessions (HTTP 503)");
  expect(useSessionStore.getState().activeSessionId).toBe(a.id);
  api.failList(false);
  fireEvent.click(screen.getByRole("button", { name: "Check successor" }));
  await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(child.id));
  expect(api.fetcher.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
});

it.each(["restore", "restore-and-archive", "delete-target", "leave-return", "connection", "service", "account"])(
  "retires a successor click across %s while confirmation is pending", async (boundary) => {
    const api = server();
    api.setRows([b, child, { ...a, archived: true }]);
    mount();
    const event = { type: "session_archived", old_session_id: a.id, new_session_id: child.id, name: child.name };
    await act(async () => handleWsMessage(event));
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
  await act(async () => {
    handleWsMessage(event);
    handleWsMessage({ type: "session_archived", old_session_id: child.id, new_session_id: grandchild.id, name: grandchild.name });
    handleWsMessage(event);
  });
  expect(useSessionStore.getState().archivedSessions.map((row) => row.id)).toContain(child.id);
  fireEvent.click(screen.getByRole("button", { name: "Open successor" }));
  await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(child.id));
  fireEvent.click(screen.getByRole("button", { name: "Browse loaded content" }));
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
