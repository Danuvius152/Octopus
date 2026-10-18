import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";
import { loadOlderMessages } from "../lib/transcript";

const { child } = vi.hoisted(() => ({ child: {
  id: "fork-child", name: "Fork", status: "idle", backend: "codex", agent_id: "agent",
  created_at: "2026-10-19", working_dir: "/tmp", forked_from_session_id: "parent",
} }));

// Drive ChatView's real rewind callback at its dialog boundary. Duplicate is
// driven through the actual slash-command watcher and POST response below.
vi.mock("./ForkDialog", () => ({ ForkDialog: ({ onForked }: { onForked: (value: unknown) => void }) =>
  <button onClick={() => onForked(child)}>Confirm test rewind</button> }));

const parent = { ...child, id: "parent", name: "Parent", forked_from_session_id: null } as SessionInfo;
const dispatch = (generation: string) => ({ generation, revision: 0, paused: false, active_request_id: null, items: [] });
const detail = () => ({ ...child,
  messages: Array.from({ length: 200 }, (_, index) => ({ role: "assistant", type: "text", seq: 200 + index, content: `old ${index}` })),
  oldest_loaded_seq: 200, has_more_messages: true, next_message_seq: 400,
  pending_questions: [], subagents: [], dispatch_state: dispatch("old-service"),
});

beforeEach(() => useSessionStore.setState({
  token: "synthetic-token", activeSessionId: parent.id, activeAgentId: "agent",
  sessions: [parent], archivedSessions: [], agents: [], credentials: [], backendModels: {},
  messages: {}, streamingText: {}, lastAppliedSeq: {}, oldestSeq: {}, hasMoreMessages: {}, wikiJobs: {},
  pendingQuestions: {}, closedQuestionIds: {}, subagents: {},
  pendingQueue: {}, pendingForks: {}, dispatchStates: {}, dispatchGeneration: null, retiredDispatchGenerations: [],
}));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function mockSessionFetch(handler: (url: string) => Promise<Response>) {
  const fetcher = vi.fn(handler);
  // The integrated Wiki cards load independently of the fork transcript.
  vi.stubGlobal("fetch", (url: string) => new URL(url, window.location.origin).pathname.endsWith("/wiki-jobs")
    ? Promise.resolve(new Response("[]")) : fetcher(url));
  return fetcher;
}

function mount() {
  render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()} approveTool={vi.fn()} denyTool={vi.fn()}
      answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>);
}

async function launch(mode: string) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value: mode === "rewind" ? "/rewind" : "/fork" } });
  fireEvent.click(document.querySelector<HTMLButtonElement>(".btn-send")!);
  if (mode === "rewind") await act(async () => fireEvent.click(screen.getByText("Confirm test rewind")));
}

it.each(["rewind", "duplicate"])("%s loads a paged snapshot without overwriting newer WS rows/questions", async (mode) => {
  let finish!: (response: Response) => void;
  mockSessionFetch((url: string) => {
    if (url.endsWith("/duplicate")) return Promise.resolve(new Response(JSON.stringify(child)));
    if (url.includes("/messages?before_seq=200")) return Promise.resolve(new Response(JSON.stringify({
      messages: [{ role: "user", type: "text", content: "older page", seq: 199 }], oldest_loaded_seq: 199, has_more_messages: true,
    })));
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
  await act(async () => { expect(await loadOlderMessages(child.id)).toBe(1); });
  expect(useSessionStore.getState().messages[child.id][0].seq).toBe(199);
});

it.each(["rewind", "duplicate"])("%s passes captured null and rejects a previously unseen old service", async (mode) => {
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
