import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";
import { sessions as sessionList, type SessionObservation } from "../lib/sessionList";
import { applyTranscript } from "../lib/transcript";

const { child } = vi.hoisted(() => ({ child: {
  id: "fork-child", name: "Fork", status: "idle", backend: "codex", agent_id: "agent",
  created_at: "2026-10-19", working_dir: "/tmp", forked_from_session_id: "parent",
} }));

// The real dialog confirms the list before calling ChatView's navigation.
// Duplicate is driven through the actual slash-command watcher and POST below.
vi.mock("./ForkDialog", () => ({ ForkDialog: ({ onForked }: { onForked: (value: unknown, observation: SessionObservation) => void }) =>
  <button onClick={async () => {
    const observation = sessionList.capture();
    if (await sessionList.created(child as SessionInfo, observation, child.forked_from_session_id)) onForked(child, observation);
  }}>Confirm test rewind</button> }));

const parent = { ...child, id: "parent", name: "Parent", forked_from_session_id: null } as SessionInfo;
const dispatch = (generation: string) => ({ generation, revision: 0, paused: false, active_request_id: null, items: [] });
const detail = () => ({ ...child,
  messages: Array.from({ length: 200 }, (_, index) => ({ role: "assistant", type: "text", seq: 200 + index, content: `old ${index}` })),
  oldest_loaded_seq: 200, has_more_messages: true, next_message_seq: 400,
  pending_questions: [], subagents: [], dispatch_state: dispatch("old-service"),
});

beforeEach(() => {
  mockChatViewport();
  useSessionStore.getState().setToken("synthetic-token");
  useSessionStore.setState({
  token: "synthetic-token", activeSessionId: parent.id, activeAgentId: "agent",
  sessions: [parent], archivedSessions: [], agents: [], credentials: [], backendModels: {},
  messages: {}, streamingText: {}, lastAppliedSeq: {}, oldestSeq: {}, hasMoreMessages: {}, wikiJobs: {},
  pendingQuestions: {}, closedQuestionIds: {}, subagents: {},
  dispatchUnavailable: {}, pendingForks: {}, dispatchStates: { parent: dispatch("old-service") },
  dispatchGeneration: "old-service", retiredDispatchGenerations: [],
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function mockSessionFetch(handler: (url: string) => Promise<Response>) {
  const fetcher = vi.fn(handler);
  // The integrated Wiki cards load independently of the fork transcript.
  vi.stubGlobal("fetch", (url: string) => {
    const path = new URL(url, window.location.origin).pathname;
    if (path.endsWith("/wiki-jobs")) return Promise.resolve(Response.json([]));
    if (path.endsWith("/submissions")) return Promise.resolve(Response.json({ items: [] }));
    return fetcher(url);
  });
  return fetcher;
}

function mount() {
  return render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()}
      answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>);
}

async function launch(mode: string) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value: mode === "rewind" ? "/rewind" : mode === "archive" ? "/archive" : "/fork" } });
  fireEvent.click(document.querySelector<HTMLButtonElement>(".btn-send")!);
  if (mode === "rewind") await act(async () => fireEvent.click(screen.getByText("Confirm test rewind")));
}

it.each(["connection", "service"])("releases paging UI after an obsolete %s observation with a continuous snapshot", async (boundary) => {
  const replies: ((response: Response) => void)[] = [];
  mockSessionFetch(async (url) => {
    if (url.includes("/messages?")) return new Promise<Response>((resolve) => { replies.push(resolve); });
    return Response.json([parent]);
  });
  const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
  sessionList.connect(source);
  const snapshot = { messages: [{ role: "assistant", type: "text", seq: 200, content: "Reading" }],
    oldest_loaded_seq: 200, has_more_messages: true, next_message_seq: 201 };
  applyTranscript(parent.id, snapshot);
  mount();
  await waitFor(() => expect(replies).toHaveLength(1));
  const replacement = { ...source, socket: {} };
  act(() => {
    if (boundary === "connection") { sessionList.disconnect(source); sessionList.connect(replacement); }
    else useSessionStore.getState().applyDispatchState(parent.id, dispatch("replacement-service"));
    applyTranscript(parent.id, snapshot);
  });
  const page = { messages: [{ role: "assistant", type: "text", seq: 199, content: "Earlier" }],
    oldest_loaded_seq: 199, has_more_messages: false };
  await act(async () => replies[0](Response.json(page)));
  expect(useSessionStore.getState().oldestSeq.parent).toBe(200);
  await waitFor(() => expect(screen.queryByText("Loading earlier messages…")).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "Load earlier messages" }));
  await waitFor(() => expect(replies).toHaveLength(2));
  await act(async () => replies[1](Response.json(page)));
  expect(useSessionStore.getState().oldestSeq.parent).toBe(199);
  sessionList.disconnect(boundary === "connection" ? replacement : source);
});

it("gives identical unsequenced notices distinct stable display identities", async () => {
  mockSessionFetch(async () => Response.json([]));
  const notice = { role: "system" as const, type: "notice", content: "Model for this session: unchanged" };
  const store = useSessionStore.getState();
  store.setMessages(parent.id, [{ role: "assistant", type: "text", seq: 200, content: "Reading" }]);
  store.setMessageWindow(parent.id, 200, false);
  store.addMessage(parent.id, notice);
  store.addMessage(parent.id, notice);
  mount();
  await waitFor(() => expect(screen.getAllByText(notice.content)).toHaveLength(2));
  const keys = () => screen.getAllByText(notice.content).map((node) => node.closest<HTMLElement>("[data-message-key]")!.dataset.messageKey);
  const original = keys();
  expect(new Set(original).size).toBe(2);
  act(() => useSessionStore.getState().prependMessages(parent.id,
    [{ role: "assistant", type: "text", seq: 199, content: "Earlier" }], 199, false,
    { beforeSeq: 200, window: useSessionStore.getState().messageWindows.parent }));
  await waitFor(() => expect(keys()).toEqual(original));
});

it.each(["rewind", "duplicate"])("%s loads a paged snapshot without overwriting newer WS rows/questions", async (mode) => {
  let finish!: (response: Response) => void;
  let finishOlder!: (response: Response) => void;
  const fetcher = mockSessionFetch((url: string) => {
    if (url.endsWith("/duplicate")) return Promise.resolve(new Response(JSON.stringify(child)));
    if (url.includes("/messages?before_seq=200"))
      return new Promise<Response>((resolve) => { finishOlder = resolve; });
    if (new URL(url, window.location.origin).pathname === `/api/sessions/${child.id}`)
      return new Promise<Response>((resolve) => { finish = resolve; });
    throw new Error(`Unexpected session request: ${url}`);
  });
  mount();
  await launch(mode);
  await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(child.id));
  await waitFor(() => expect(finish).toBeTypeOf("function"));
  act(() => {
    handleWsMessage({ type: "assistant_text", session_id: child.id, seq: 400, content: "new WS row" });
    handleWsMessage({ type: "question_request", session_id: child.id, seq: 401, question_id: "late-question", questions: [{
      question: "Late question?", header: "Choice", options: [{ label: "Yes", description: "Continue" }],
    }] });
  });
  await act(async () => finish(new Response(JSON.stringify(detail()))));
  const state = useSessionStore.getState();
  expect(state.oldestSeq[child.id]).toBe(200);
  expect(state.hasMoreMessages[child.id]).toBe(true);
  expect(state.messages[child.id].find((row) => row.seq === 400)?.content).toBe("new WS row");
  expect(state.pendingQuestions[child.id][0].question_id).toBe("late-question");
  expect(state.sessions.some((row) => row.id === parent.id)).toBe(mode === "duplicate");
  expect(state.archivedSessions.some((row) => row.id === parent.id)).toBe(mode === "rewind");
  // The short WS-only window can already have armed automatic paging. Keep
  // its response behind the snapshot assertions, then exercise the same UI
  // paging path whether it started automatically or needs the header button.
  const olderButton = screen.queryByRole("button", { name: "Load earlier messages" });
  if (olderButton) fireEvent.click(olderButton);
  await waitFor(() => expect(finishOlder).toBeTypeOf("function"));
  expect(fetcher.mock.calls.filter(([url]) => url.includes("/messages?before_seq=")))
    .toEqual([[expect.stringContaining("/messages?before_seq=200")]]);
  await act(async () => finishOlder(Response.json({
    messages: [{ role: "user", type: "text", content: "older page", seq: 199 }], oldest_loaded_seq: 199, has_more_messages: false,
  })));
  // Let Virtuoso apply its prepend adjustment before unmounting the list.
  await act(async () => { await new Promise<void>((resolve) => requestAnimationFrame(() => resolve())); });
  const paged = useSessionStore.getState();
  expect(paged.oldestSeq[child.id]).toBe(199);
  expect(paged.messages[child.id][0].seq).toBe(199);
  expect(paged.messages[child.id].find((row) => row.seq === 400)?.content).toBe("new WS row");
  expect(paged.pendingQuestions[child.id][0].question_id).toBe("late-question");
});

it.each(["switch", "return", "remount"])("a late old page cannot settle the current view during %s", async (transition) => {
  const replies: Record<string, ((response: Response) => void)[]> = { parent: [], "fork-child": [] };
  mockSessionFetch((url) => new Promise<Response>((resolve) => {
    const id = url.includes("/parent/") ? "parent" : "fork-child";
    replies[id].push(resolve);
  }));
  const row = (id: string) => ({ role: "assistant" as const, type: "text", seq: 200, content: `Reading ${id}` });
  useSessionStore.setState({ sessions: [parent, child as SessionInfo],
    messages: { parent: [row("parent")], "fork-child": [row("child")] },
    oldestSeq: { parent: 200, "fork-child": 200 }, hasMoreMessages: { parent: true, "fork-child": true },
  });
  const view = mount();
  await waitFor(() => expect(replies.parent).toHaveLength(1));
  if (transition === "remount") { view.unmount(); mount(); }
  else {
    act(() => useSessionStore.getState().setActiveSessionId(child.id));
    await waitFor(() => expect(replies[child.id]).toHaveLength(1));
    if (transition === "return") act(() => useSessionStore.getState().setActiveSessionId(parent.id));
  }
  const active = transition === "switch" ? child.id : parent.id;
  await waitFor(() => expect(replies[active]).toHaveLength(active === "parent" ? 2 : 1));
  const page = { messages: [{ role: "assistant", type: "text", seq: 199, content: "Earlier" }],
    oldest_loaded_seq: 199, has_more_messages: false };
  await act(async () => replies.parent[0](Response.json(page)));
  expect(useSessionStore.getState().oldestSeq.parent).toBe(200);
  expect(screen.getByText("Loading earlier messages…")).toBeInTheDocument();
  await act(async () => replies[active].at(-1)!(Response.json(page)));
  await waitFor(() => expect(screen.queryByText("Loading earlier messages…")).toBeNull());
  expect(useSessionStore.getState().oldestSeq[active]).toBe(199);
  if (transition === "return") {
    await act(async () => replies[child.id][0](Response.json(page)));
    expect(useSessionStore.getState().oldestSeq[child.id]).toBe(200);
  }
});

it.each(["rewind", "duplicate"])("%s rejects an old service response after a newer service arrives", async (mode) => {
  let finish!: (response: Response) => void;
  mockSessionFetch((url: string) => {
    if (url.endsWith("/duplicate")) return Promise.resolve(new Response(JSON.stringify(child)));
    if (new URL(url, window.location.origin).pathname === `/api/sessions/${child.id}`)
      return new Promise<Response>((resolve) => { finish = resolve; });
    throw new Error(`Unexpected session request: ${url}`);
  });
  mount();
  await launch(mode);
  await waitFor(() => expect(finish).toBeTypeOf("function"));
  act(() => useSessionStore.getState().applyDispatchState(child.id, dispatch("new-service")));
  await act(async () => finish(new Response(JSON.stringify(detail()))));
  expect(useSessionStore.getState().dispatchGeneration).toBe("new-service");
  expect(useSessionStore.getState().messages[child.id] ?? []).toEqual([]);
  expect(useSessionStore.getState().oldestSeq[child.id]).toBeUndefined();
});

it.each([false, true])("confirms a successful duplicate without losing foreground intent (focus changed: %s)", async (moved) => {
  let finishPost!: (response: Response) => void;
  let finishList!: (response: Response) => void;
  const other = { ...parent, id: "other", status: "running" } as SessionInfo;
  useSessionStore.setState({ sessions: [parent, other] });
  const fetcher = mockSessionFetch(async (url: string) => {
    if (url.endsWith("/duplicate")) return new Promise<Response>((resolve) => { finishPost = resolve; });
    const path = new URL(url).pathname;
    if (path === "/api/sessions") return new Promise<Response>((resolve) => { finishList = resolve; });
    if (path === `/api/sessions/${child.id}`) return Response.json(detail());
    throw new Error(`Unexpected session request: ${url}`);
  });
  mount();
  await launch("duplicate");
  await waitFor(() => expect(finishPost).toBeTypeOf("function"));
  act(() => handleWsMessage({ type: "status", session_id: other.id, status: "idle" }));
  await act(async () => finishPost(Response.json(child)));
  await waitFor(() => expect(finishList).toBeTypeOf("function"));
  expect(useSessionStore.getState().activeSessionId).toBe(parent.id);
  if (moved) act(() => useSessionStore.getState().setActiveSessionId(other.id));
  await act(async () => finishList(Response.json([parent, { ...other, status: "idle" }, child])));
  await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(moved ? other.id : child.id));
  expect(useSessionStore.getState().sessions.some((row) => row.id === parent.id)).toBe(true);
  expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/duplicate"))).toHaveLength(1);
});

it("a deferred background duplicate keeps focus and never fetches child detail", async () => {
  const fetcher = mockSessionFetch(async (url: string) => {
    expect(url).toContain("/duplicate");
    return new Response(JSON.stringify(child));
  });
  useSessionStore.setState({ sessions: [{ ...parent, status: "running" }] });
  mount();
  await launch("duplicate");
  expect(fetcher).not.toHaveBeenCalled();
  act(() => useSessionStore.setState({ activeSessionId: "other", sessions: [parent, { ...parent, id: "other" }] }));
  await waitFor(() => expect(useSessionStore.getState().sessions.some((row) => row.id === child.id)).toBe(true));
  expect(useSessionStore.getState().activeSessionId).toBe("other");
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0][0]).toContain("/parent/duplicate");
  expect(useSessionStore.getState().messages.parent.some((row) => String(row.content).includes("Fork ready"))).toBe(true);
});

it.each(["missing", "unavailable"])("waits for a confirmed queue before forking when state is %s", async (kind) => {
  if (kind === "missing") useSessionStore.setState({ dispatchStates: {} });
  else useSessionStore.getState().setDispatchUnavailable(parent.id, true);
  const fetcher = mockSessionFetch(async (url: string) => Response.json(url.endsWith("/duplicate") ? child : detail()));
  mount();
  await act(async () => launch("duplicate"));
  expect(fetcher).not.toHaveBeenCalled();
  expect(useSessionStore.getState().pendingForks.parent).toBeDefined();
  act(() => handleWsMessage({ type: "dispatch_state", session_id: parent.id, dispatch_state: dispatch("old-service") }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith(expect.stringContaining("/parent/duplicate")));
});

it.each(["archive", "duplicate", "background duplicate"])("discards %s side effects when the account changes after list acceptance", async (mode) => {
  const other = { ...parent, id: "other" };
  const foreign = { ...parent, id: "foreign", agent_id: "foreign-agent" };
  const fetcher = mockSessionFetch(async (url: string) => {
    if (url.endsWith("/duplicate") || url.endsWith("/archive")) return Response.json(child);
    if (new URL(url).pathname === `/api/sessions/${child.id}`) return Response.json(detail());
    throw new Error(`Unexpected session request: ${url}`);
  });
  if (mode === "background duplicate") useSessionStore.setState({ sessions: [{ ...parent, status: "running" }, other] });
  const unsubscribe = useSessionStore.subscribe((state) => {
    if (!state.sessions.some((row) => row.id === child.id)) return;
    unsubscribe();
    queueMicrotask(() => {
      useSessionStore.getState().setToken("foreign-token");
      useSessionStore.setState({ sessions: [foreign], activeSessionId: foreign.id, activeAgentId: foreign.agent_id });
    });
  });
  try {
    mount();
    await launch(mode === "background duplicate" ? "duplicate" : mode);
    if (mode === "background duplicate") act(() => useSessionStore.setState({ activeSessionId: other.id, sessions: [parent, other] }));
    await waitFor(() => expect(useSessionStore.getState().token).toBe("foreign-token"));
    expect(useSessionStore.getState().activeSessionId).toBe(foreign.id);
    expect(useSessionStore.getState().activeAgentId).toBe(foreign.agent_id);
    expect(useSessionStore.getState().messages[parent.id]).toBeUndefined();
    expect(useSessionStore.getState().messages[child.id]).toBeUndefined();
    expect(fetcher.mock.calls.some(([url]) => new URL(url).pathname === `/api/sessions/${child.id}`)).toBe(false);
  } finally {
    unsubscribe();
  }
});
