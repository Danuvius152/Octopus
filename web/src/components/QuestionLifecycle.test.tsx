import { Socket } from "../test-support/socket";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";
import { handleWsMessage, useWebSocket } from "../hooks/useWebSocket";
import { applyTranscript } from "../lib/transcript";
import { applySessionSnapshot } from "../lib/hydrateSession";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";

const sid = "question-lifecycle";
const emptyDispatch = { generation: "service-a", revision: 0, paused: false, items: [], active_request_id: null };
const question = { question: "Proceed?", options: [{ label: "Yes" }, { label: "No" }] };
const requested = {
  type: "question_request", session_id: sid, seq: 1, question_id: "q1",
  questions: [question], deadline: "2026-10-13T08:30:00Z", request_id: "r1", attempt_id: "a1",
};
const closed = {
  type: "question_closed", session_id: sid, seq: 2, question_id: "q1",
  reason: "interrupted", message: "Question closed: interrupted. No answer was received.",
};

beforeEach(() => {
  mockChatViewport();
  useSessionStore.setState({
  activeSessionId: sid,
  sessions: [{ id: sid, name: "Question", working_dir: "/tmp", created_at: "2026-10-13", status: "running" } as SessionInfo],
  agents: [], messages: {}, lastAppliedSeq: {}, pendingQuestions: {}, dispatchUnavailable: {}, closedQuestionIds: {},
  streamingText: {}, pendingForks: {}, credentials: [], backendModels: {},
  dispatchGeneration: null, retiredDispatchGenerations: [], dispatchStates: {},
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderChat() {
  return render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()}
      answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>);
}

it("keeps persisted approval history readable after reload without live actions or status", async () => {
  applyTranscript(sid, {
    messages: [{
      role: "tool", type: "tool_approval_request", seq: 1, tool_name: "Write", tool_use_id: "legacy-tool",
      tool_input: { file_path: "/tmp/history.txt", content: "Earlier approval input" },
    }],
    next_message_seq: 2,
  });
  expect(useSessionStore.getState().sessions[0].status).toBe("running");
  renderChat();
  expect(await screen.findByText(/Historical tool approval request/)).toBeTruthy();
  expect(screen.getByText(/Earlier approval input/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Allow" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Deny" })).toBeNull();
  const history = useSessionStore.getState().messages[sid];
  act(() => applyTranscript(sid, { messages: history, next_message_seq: 2 }));
  expect(await screen.findByText(/Historical tool approval request/)).toBeTruthy();
  expect(screen.getByText(/Earlier approval input/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Allow" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Deny" })).toBeNull();
});

it("closes the live form and renders the same closure after history reload and replay", async () => {
  handleWsMessage(requested);
  renderChat();
  expect(await screen.findByRole("button", { name: "Submit" })).toBeTruthy();
  expect(useSessionStore.getState().pendingQuestions[sid][0]).toMatchObject({
    deadline: requested.deadline, request_id: "r1", attempt_id: "a1",
  });
  act(() => handleWsMessage(closed));
  expect(screen.queryByRole("button", { name: "Submit" })).toBeNull();
  expect(await screen.findByText(closed.message)).toBeTruthy();
  const live = useSessionStore.getState().messages[sid];
  act(() => {
    applyTranscript(sid, { messages: live, next_message_seq: 3 });
    useSessionStore.getState().setPendingQuestions(sid, []);
    handleWsMessage(requested);
    handleWsMessage(closed);
  });
  expect(screen.queryByRole("button", { name: "Submit" })).toBeNull();
  expect(screen.getAllByText(closed.message)).toHaveLength(1);
  expect(useSessionStore.getState().messages[sid]).toHaveLength(2);
});

it("keeps an already closed question closed when an old pending snapshot arrives", async () => {
  const dispatch = { generation: "service-a", revision: 1, paused: false, items: [], active_request_id: "r1" };
  handleWsMessage({ type: "dispatch_state", session_id: sid, dispatch_state: dispatch });
  handleWsMessage(requested);
  renderChat();
  expect(await screen.findByRole("button", { name: "Submit" })).toBeTruthy();
  act(() => handleWsMessage(closed));
  expect(await screen.findByText(closed.message)).toBeTruthy();
  act(() => {
    applySessionSnapshot(sid, {
      active_attempt: null, dispatch_state: dispatch, next_message_seq: 2,
      messages: [{ role: "assistant", type: "question_request", seq: 1, tool_use_id: "q1", tool_input: { questions: [question] } }],
      pending_questions: [{ question_id: "q1", questions: [question] }],
    });
    handleWsMessage({ ...requested, seq: undefined });
  });
  expect(screen.queryByRole("button", { name: "Submit" })).toBeNull();
  expect(await screen.findByText(closed.message)).toBeTruthy();
  expect(useSessionStore.getState().messages[sid]).toHaveLength(2);
});

it("closes a form when history records its resolution before the pending snapshot is applied", async () => {
  handleWsMessage(requested);
  renderChat();
  expect(await screen.findByRole("button", { name: "Submit" })).toBeTruthy();
  act(() => {
    applySessionSnapshot(sid, {
      active_attempt: null, dispatch_state: emptyDispatch,
      next_message_seq: 3,
      messages: [
        { role: "assistant", type: "question_request", seq: 1, tool_use_id: "q1", tool_input: { questions: [question] } },
        { role: "user", type: "question_answer", seq: 2, tool_use_id: "q1", content: "Q: Proceed?\nA: Yes" },
      ],
      pending_questions: [{ question_id: "q1", questions: [question] }],
    });
  });
  expect(screen.queryByRole("button", { name: "Submit" })).toBeNull();
  expect(await screen.findByText(/A: Yes/)).toBeTruthy();
});

it("keeps a newer WebSocket question answerable when the earlier HTTP refetch completes", async () => {
  Socket.reset(0);
  let resolveDetail!: (response: Response) => void;
  const detail = new Promise<Response>((resolve) => { resolveDetail = resolve; });
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith(`/api/sessions/${sid}`)
    ? detail : new Response(JSON.stringify(url.endsWith("/api/sessions")
      ? useSessionStore.getState().sessions : url.endsWith("/submissions") ? { items: [] } : []))));
  useSessionStore.setState({ token: "question-test-token" });
  const dispatch = { generation: "service-a", revision: 1, paused: false, items: [], active_request_id: "r1" };
  handleWsMessage({ type: "dispatch_state", session_id: sid, dispatch_state: dispatch });

  function ConnectedChat() {
    useWebSocket();
    return <ChatView sendMessage={vi.fn()} interrupt={vi.fn()}
      answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />;
  }
  render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ConnectedChat />
  </VirtuosoMockContext.Provider>);
  const socket = Socket.instances[0];
  act(() => { socket.readyState = Socket.OPEN; socket.onopen?.(); });
  act(() => socket.onmessage?.({ data: JSON.stringify(requested) }));
  expect(await screen.findByRole("button", { name: "Submit" })).toBeTruthy();
  await act(async () => {
    resolveDetail(new Response(JSON.stringify({
      active_attempt: null, dispatch_state: dispatch, messages: [], next_message_seq: 1, pending_questions: [],
    })));
    await detail;
  });
  expect(screen.getByRole("button", { name: "Submit" })).toBeTruthy();
  expect(useSessionStore.getState().pendingQuestions[sid]).toHaveLength(1);
  expect(useSessionStore.getState().pendingQuestions[sid][0]).toMatchObject({
    question_id: "q1", deadline: requested.deadline, request_id: "r1", attempt_id: "a1",
  });
  expect(useSessionStore.getState().messages[sid]).toHaveLength(1);
});

it("retains the newer live form while keeping a resolved question out of an older pending snapshot", async () => {
  handleWsMessage(requested);
  renderChat();
  expect(await screen.findByRole("button", { name: "Submit" })).toBeTruthy();
  act(() => {
    handleWsMessage(closed);
    handleWsMessage({ ...requested, seq: 3, question_id: "q2", attempt_id: "a2" });
  });
  expect(await screen.findByRole("button", { name: "Submit" })).toBeTruthy();
  act(() => applySessionSnapshot(sid, {
    active_attempt: null, dispatch_state: emptyDispatch,
    next_message_seq: 2,
    messages: [{ role: "assistant", type: "question_request", seq: 1,
      tool_use_id: "q1", tool_input: { questions: [question] } }],
    pending_questions: [{ question_id: "q1", questions: [question] }],
  }));
  expect(screen.getAllByRole("button", { name: "Submit" })).toHaveLength(1);
  expect(useSessionStore.getState().pendingQuestions[sid].map((pending) => pending.question_id)).toEqual(["q2"]);
  expect(screen.getByText(closed.message)).toBeTruthy();
  expect(useSessionStore.getState().messages[sid]).toHaveLength(3);
});

it("clears live questions on a service generation change and rejects the retired snapshot", async () => {
  const dispatch = { generation: "service-a", revision: 1, paused: false, items: [], active_request_id: "r1" };
  handleWsMessage({ type: "dispatch_state", session_id: sid, dispatch_state: dispatch });
  handleWsMessage(requested);
  renderChat();
  expect(await screen.findByRole("button", { name: "Submit" })).toBeTruthy();
  act(() => applySessionSnapshot(sid, {
    active_attempt: null, dispatch_state: { ...dispatch, generation: "service-b" },
    next_message_seq: 1, messages: [], pending_questions: [],
  }));
  expect(screen.queryByRole("button", { name: "Submit" })).toBeNull();
  expect(useSessionStore.getState().pendingQuestions[sid]).toBeUndefined();
  act(() => handleWsMessage({ ...requested, seq: 3, question_id: "q2" }));
  expect(await screen.findByRole("button", { name: "Submit" })).toBeTruthy();
  act(() => {
    expect(applySessionSnapshot(sid, {
      active_attempt: null, dispatch_state: dispatch, next_message_seq: 5, messages: [], pending_questions: [],
    })).toBeNull();
  });
  expect(screen.getByRole("button", { name: "Submit" })).toBeTruthy();
  expect(useSessionStore.getState().pendingQuestions[sid][0].question_id).toBe("q2");
});
