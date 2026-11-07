import { Socket } from "../test-support/socket";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";
import { handleWsMessage, useWebSocket } from "../hooks/useWebSocket";
import { applyTranscript } from "../lib/transcript";
import { applySessionSnapshot } from "../lib/hydrateSession";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { sessions } from "../lib/sessionList";

const sid = "question-lifecycle";
const emptyDispatch = { generation: "service-a", revision: 0, paused: false, items: [], active_request_id: null, active_operation: null };
const question = { question: "Proceed?", options: [{ label: "Yes" }, { label: "No" }] };
const requested = {
  type: "question_request", session_id: sid, seq: 1, question_id: "q1",
  questions: [question], deadline: "2026-10-13T08:30:00Z", request_id: "r1", attempt_id: "a1",
};
const closed = {
  type: "question_closed", session_id: sid, seq: 2, question_id: "q1",
  reason: "interrupted", message: "Question closed: interrupted. No answer was received.",
};

it("keeps an unfinished answer across session switches and form remounts", async () => {
  handleWsMessage(requested);
  const view = renderChat();
  fireEvent.change(await screen.findByPlaceholderText("Or type your own answer…"), { target: { value: "Keep this decision" } });
  act(() => useSessionStore.setState({ activeSessionId: "another" }));
  act(() => useSessionStore.setState({ activeSessionId: sid }));
  expect(await screen.findByPlaceholderText("Or type your own answer…")).toHaveValue("Keep this decision");
  view.unmount();
  renderChat();
  expect(await screen.findByPlaceholderText("Or type your own answer…")).toHaveValue("Keep this decision");
});

it("previews a closed question draft without sending or overwriting the composer", async () => {
  handleWsMessage(requested);
  renderChat();
  fireEvent.change(await screen.findByPlaceholderText("Or type your own answer…"), { target: { value: "My unsent decision" } });
  act(() => handleWsMessage(closed));
  const composer = screen.getByRole("textbox");
  fireEvent.change(composer, { target: { value: "Already writing" } });
  expect(await screen.findByRole("button", { name: "Continue as new message" })).toBeDisabled();
  fireEvent.click(screen.getByText("Saved answer draft"));
  expect(screen.getByText(/My unsent decision/)).toBeVisible();
  fireEvent.change(composer, { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Continue as new message" }));
  expect((composer as HTMLTextAreaElement).value).toContain("My unsent decision");
  expect((composer as HTMLTextAreaElement).value).toContain("Proceed?");
  expect(screen.getByRole("button", { name: "Discard answer draft" })).toBeVisible();
});

it.each(["discard", "another-session"])("allows ordinary explicit input after a question preview becomes %s", async (action) => {
  const sendMessage = vi.fn<Parameters<typeof ChatView>[0]["sendMessage"]>(async () => ({ accepted: true }));
  handleWsMessage(requested);
  renderChat({ sendMessage });
  fireEvent.change(await screen.findByPlaceholderText("Or type your own answer…"), { target: { value: "Original answer draft" } });
  act(() => handleWsMessage(closed));
  fireEvent.click(await screen.findByRole("button", { name: "Continue as new message" }));
  if (action === "discard") fireEvent.click(screen.getByRole("button", { name: "Discard answer draft" }));
  else act(() => useSessionStore.setState((state) => ({ activeSessionId: "another", sessions: [
    ...state.sessions, { ...state.sessions[0], id: "another", name: "Another conversation" },
  ] })));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "An ordinary new message" } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: /Queue message|Send message/ })));
  expect(sendMessage).toHaveBeenCalledTimes(1);
  expect(sendMessage.mock.calls[0].slice(0, 2)).toEqual([action === "discard" ? sid : "another", "An ordinary new message"]);
  if (action === "another-session") expect(Object.values(useSessionStore.getState().questionDrafts)).toHaveLength(1);
});

it("recovers an unknown answer receipt after unmount without creating duplicate authorization", async () => {
  const answerQuestion = vi.fn<Parameters<typeof ChatView>[0]["answerQuestion"]>(async () => ({ accepted: false, state: "unknown" as const }));
  handleWsMessage(requested);
  const view = renderChat({ answerQuestion });
  fireEvent.change(await screen.findByPlaceholderText("Or type your own answer…"), { target: { value: "My answer" } });
  const submit = await screen.findByRole("button", { name: "Submit" });
  await act(async () => fireEvent.click(submit));
  const target = answerQuestion.mock.calls[0]?.[3] as unknown as { clientCommandId: string };
  view.unmount();
  act(() => handleWsMessage(closed));
  renderChat({ answerQuestion });
  expect(await screen.findByRole("button", { name: "Continue as new message" })).toBeDisabled();
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ state: "resolved", resolution: {
    question_id: "q1", request_id: "r1", attempt_id: "a1", disposition: "answered",
    client_command_id: target.clientCommandId,
  } })));
  fireEvent.click(screen.getByRole("button", { name: "Check original receipt" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Continue as new message" })).not.toBeInTheDocument());
  expect(answerQuestion).toHaveBeenCalledTimes(1);
});

it("keeps the original continuation identity when its receipt is unknown across remount", async () => {
  const sendMessage = vi.fn<Parameters<typeof ChatView>[0]["sendMessage"]>(async (_sid, _content, _attachments, _mode, id) =>
    ({ accepted: false, state: "unknown", clientRequestId: id }));
  handleWsMessage(requested);
  const view = renderChat({ sendMessage });
  fireEvent.change(await screen.findByPlaceholderText("Or type your own answer…"), { target: { value: "A late decision" } });
  act(() => handleWsMessage(closed));
  fireEvent.click(await screen.findByRole("button", { name: "Continue as new message" }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Queue message" })));
  const id = sendMessage.mock.calls[0]?.[4];
  expect(id).toEqual(expect.any(String));
  view.unmount();
  renderChat({ sendMessage });
  expect(await screen.findByRole("button", { name: "Continue as new message" })).toBeDisabled();
  const fetch = vi.fn(async () => Response.json({ session_id: sid, client_request_id: id,
    accepted: true, execution: { state: "running" } }));
  vi.stubGlobal("fetch", fetch);
  fireEvent.click(screen.getByRole("button", { name: "Check original receipt" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Continue as new message" })).not.toBeInTheDocument());
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining(`/submissions/${id}`), expect.any(Object));
  expect(sendMessage).toHaveBeenCalledTimes(1);
});

it("clears only the submitted answer snapshot when acceptance arrives after another edit", async () => {
  let finish!: (reply: { accepted: boolean }) => void;
  const answerQuestion = vi.fn<Parameters<typeof ChatView>[0]["answerQuestion"]>(() => new Promise((resolve) => { finish = resolve; }));
  handleWsMessage(requested);
  renderChat({ answerQuestion });
  fireEvent.change(await screen.findByPlaceholderText("Or type your own answer…"), { target: { value: "Submitted answer" } });
  fireEvent.click(await screen.findByRole("button", { name: "Submit" }));
  fireEvent.change(await screen.findByPlaceholderText("Or type your own answer…"), { target: { value: "Later unsent edit" } });
  await act(async () => { finish({ accepted: true }); });
  act(() => handleWsMessage(closed));
  fireEvent.click(await screen.findByText("Saved answer draft"));
  expect(screen.getByText(/Later unsent edit/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Continue as new message" })).toBeEnabled();
});

it.each(["unreachable", "pending", "missing", "legacy", "wrong-attempt"])(
  "keeps an unknown answer uncertain when the original receipt is %s", async (kind) => {
    const answerQuestion = vi.fn<Parameters<typeof ChatView>[0]["answerQuestion"]>(async () => ({ accepted: false, state: "unknown" }));
    handleWsMessage(requested);
    renderChat({ answerQuestion });
    fireEvent.change(await screen.findByPlaceholderText("Or type your own answer…"), { target: { value: "Unconfirmed decision" } });
    await act(async () => fireEvent.click(await screen.findByRole("button", { name: "Submit" })));
    act(() => handleWsMessage(closed));
    vi.stubGlobal("fetch", vi.fn(async () => {
      if (kind === "unreachable") throw new Error("Offline");
      if (kind === "missing") return new Response("missing", { status: 404 });
      return Response.json(kind === "pending" ? { state: "pending" } : { state: "resolved", resolution: {
        question_id: "q1", request_id: "r1", attempt_id: kind === "wrong-attempt" ? "a2" : "a1",
        disposition: "answered",
      } });
    }));
    fireEvent.click(await screen.findByRole("button", { name: "Check original receipt" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Check original receipt" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Continue as new message" })).toBeDisabled();
    expect(answerQuestion).toHaveBeenCalledTimes(1);
  });

it("keeps the saved input when a competing expiry is confirmed rather than treating it as sent", async () => {
  const answerQuestion = vi.fn<Parameters<typeof ChatView>[0]["answerQuestion"]>(async () => ({ accepted: false, state: "unknown" }));
  handleWsMessage(requested);
  renderChat({ answerQuestion });
  fireEvent.change(await screen.findByPlaceholderText("Or type your own answer…"), { target: { value: "Answer just after expiry" } });
  await act(async () => fireEvent.click(await screen.findByRole("button", { name: "Submit" })));
  act(() => handleWsMessage({ type: "question_answer", session_id: sid, seq: 2, question_id: "q1", content: "System expiry", tool_input: {
    question_resolution: { question_id: "q1", request_id: "r1", attempt_id: "a1", disposition: "expired", source: { kind: "system" } },
  } }));
  expect(await screen.findByRole("button", { name: "Continue as new message" })).toBeEnabled();
  fireEvent.click(screen.getByText("Saved answer draft"));
  expect(screen.getByText(/Answer just after expiry/)).toBeVisible();
});

it.each(["deletion", "account-change"])("discards an in-flight answer on confirmed %s and ignores its late receipt", async (kind) => {
  let finish!: (reply: { accepted: false; state: "unknown" }) => void;
  const answerQuestion = vi.fn<Parameters<typeof ChatView>[0]["answerQuestion"]>(() => new Promise((resolve) => { finish = resolve; }));
  handleWsMessage(requested);
  renderChat({ answerQuestion });
  fireEvent.change(await screen.findByPlaceholderText("Or type your own answer…"), { target: { value: "Sensitive old input" } });
  fireEvent.click(await screen.findByRole("button", { name: "Submit" }));
  act(() => {
    if (kind === "deletion") sessions.remove(sid);
    else useSessionStore.getState().setToken("next-account-token");
  });
  await act(async () => { finish({ accepted: false, state: "unknown" }); });
  expect(useSessionStore.getState().questionDrafts).toEqual({});
  expect(screen.queryByText(/Sensitive old input/)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Check original receipt" })).not.toBeInTheDocument();
});

it("projects owner snooze across an older snapshot without resetting the original question", async () => {
  const dispatch = { ...emptyDispatch, revision: 1, active_request_id: "r1", active_operation: "message" as const };
  const optional = { ...requested, interaction_policy: "human", wait_mode: "optional", snoozed: false };
  handleWsMessage({ type: "dispatch_state", session_id: sid, dispatch_state: dispatch });
  handleWsMessage(optional);
  renderChat();
  act(() => handleWsMessage({ type: "question_snoozed", session_id: sid, seq: 2, question_id: "q1",
    request_id: "r1", attempt_id: "a1", deadline: null, snoozed: true }));
  await waitFor(() => expect(screen.getByText("Automatic expiry paused · waiting for your answer")).toBeVisible());
  act(() => applySessionSnapshot(sid, {
    dispatch_state: dispatch, active_attempt: { request_id: "r1", attempt_id: "a1" }, next_message_seq: 2,
    messages: [{ role: "assistant", type: "question_request", seq: 1, tool_use_id: "q1", tool_input: optional }],
    pending_questions: [optional],
  }));
  await waitFor(() => expect(screen.getByText("Automatic expiry paused · waiting for your answer")).toBeVisible());
  expect(useSessionStore.getState().pendingQuestions[sid][0]).toMatchObject({ deadline: null, snoozed: true });
});

it("shows the original human blocking policy from live events and a refreshed snapshot", async () => {
  handleWsMessage({ ...requested, deadline: null, interaction_policy: "human", wait_mode: "blocking" });
  const view = renderChat();
  await waitFor(() => expect(screen.getByText("Waiting for your answer · no automatic question deadline")).toBeVisible());
  expect(screen.queryByText("Claude is asking")).not.toBeInTheDocument();
  view.unmount();
  act(() => {
    useSessionStore.setState({ pendingQuestions: {}, messages: {}, lastAppliedSeq: {} });
    applySessionSnapshot(sid, {
      dispatch_state: { ...emptyDispatch, revision: 1, active_request_id: "r1", active_operation: "message" as const },
      active_attempt: { request_id: "r1", attempt_id: "a1" }, next_message_seq: 2,
      messages: [{ role: "assistant", type: "question_request", seq: 1, tool_use_id: "q1", tool_input: {
        questions: [question], deadline: null, interaction_policy: "human", wait_mode: "blocking",
      } }], pending_questions: [{ question_id: "q1", questions: [question], deadline: null,
        request_id: "r1", attempt_id: "a1", interaction_policy: "human", wait_mode: "blocking" }],
    });
  });
  renderChat();
  await waitFor(() => expect(screen.getByText("Waiting for your answer · no automatic question deadline")).toBeVisible());
});

beforeEach(() => {
  mockChatViewport();
  useSessionStore.setState({
  activeSessionId: sid,
  sessions: [{ id: sid, name: "Question", working_dir: "/tmp", created_at: "2026-10-13", status: "running" } as SessionInfo],
  agents: [], messages: {}, lastAppliedSeq: {}, pendingQuestions: {}, questionDrafts: {}, dispatchUnavailable: {}, closedQuestionIds: {},
  streamingText: {}, pendingForks: {}, credentials: [], backendModels: {},
  dispatchGeneration: null, retiredDispatchGenerations: [], dispatchStates: {},
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderChat(props: Partial<Parameters<typeof ChatView>[0]> = {}) {
  return render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()}
      answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} {...props} />
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
  const dispatch = { generation: "service-a", revision: 1, paused: false, items: [], active_request_id: "r1", active_operation: "message" as const };
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
  const sessionRows = useSessionStore.getState().sessions;
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith(`/api/sessions/${sid}`)
    ? detail : new Response(JSON.stringify(url.includes("/api/sessions") && !url.includes(`/api/sessions/${sid}`)
      ? sessionRows : url.endsWith("/submissions") ? { items: [] } : []))));
  useSessionStore.setState({ token: "question-test-token" });
  useSessionStore.setState({ sessions: sessionRows, activeSessionId: sid });
  const dispatch = { generation: "service-a", revision: 1, paused: false, items: [], active_request_id: "r1", active_operation: "message" as const };
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
  const dispatch = { generation: "service-a", revision: 1, paused: false, items: [], active_request_id: "r1", active_operation: "message" as const };
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
