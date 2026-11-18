import { Profiler } from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type Agent, type SessionInfo } from "../stores/sessionStore";
import { applyTranscript } from "../lib/transcript";
import { saveQuestionDraft } from "../lib/questionDrafts";

beforeEach(() => {
  vi.useFakeTimers();
  mockChatViewport();
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ items: [], next_cursor: null })));
  useSessionStore.setState({ ...useSessionStore.getInitialState(), accountGeneration: useSessionStore.getState().accountGeneration + 1,
    token: "isolation-account", activeSessionId: "A" }, true);
  // SessionList retires the previous account's rows synchronously. Install
  // this account's real list only after that retirement, as a list read does.
  useSessionStore.setState({ sessions: ["A", "B"].map((id) => ({ id, name: id, status: "idle", backend: "codex" } as SessionInfo)) });
  applyTranscript("A", { messages: [{ role: "assistant", type: "text", seq: 0, content: "Current A text" }], oldest_loaded_seq: 0, next_message_seq: 1 });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it.each(["messages", "questions", "answer drafts"])("does not commit A's display for B's %s updates", async (kind) => {
  const commits = vi.fn();
  render(<Profiler id="current-chat" onRender={commits}><VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider></Profiler>);
  await act(async () => {});
  commits.mockClear();
  act(() => {
    for (let index = 0; index < 3; index++) {
      if (kind === "messages") useSessionStore.getState().addMessage("B", { role: "assistant", type: "text", seq: index, content: `Background ${index}` });
      else {
        const question = { question_id: `question-${index}`, questions: [{ question: `Background question ${index}` }] };
        useSessionStore.getState().addPendingQuestion("B", question);
        if (kind === "answer drafts") saveQuestionDraft({ sessionId: "B", accountGeneration: useSessionStore.getState().accountGeneration,
          question, answers: [{ text: `Background answer ${index}` }] });
      }
    }
  });
  expect(commits).not.toHaveBeenCalled();
  expect(screen.getByText("Current A text")).toBeInTheDocument();
  expect(useSessionStore.getState()[kind === "messages" ? "messages" : "pendingQuestions"].B).toHaveLength(3);
  if (kind === "answer drafts") expect(Object.values(useSessionStore.getState().questionDrafts)).toHaveLength(3);
});

it("ignores unrelated session and agent changes while updating the current display's actual dependencies", async () => {
  const state = useSessionStore.getState();
  useSessionStore.setState({
    sessions: state.sessions.map(session => session.id === "A" ? { ...session, agent_id: "owner",
      parent_session_id: "parent", forked_from_session_id: "source", fork_after_seq: 1 } : session)
      .concat([{ ...state.sessions[0], id: "parent", agent_id: "parent-agent" }]),
    archivedSessions: [{ ...state.sessions[0], id: "source", name: "Original source", archived: true }],
    agents: [{ id: "owner", name: "Original owner" }, { id: "parent-agent", name: "Original parent" },
      { id: "unrelated", name: "Unrelated agent" }] as Agent[],
  });
  const commits = vi.fn();
  render(<Profiler id="current-chat" onRender={commits}><VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider></Profiler>);
  await act(async () => {});
  expect(screen.getByTestId("delegation-banner")).toHaveTextContent("Original parent");
  expect(screen.getByTestId("fork-banner")).toHaveTextContent("Original source");
  commits.mockClear();
  act(() => useSessionStore.setState(current => ({
    sessions: current.sessions.map(session => session.id === "B" ? { ...session, status: "running", name: "Background changed" } : session),
    agents: current.agents.map(agent => agent.id === "unrelated" ? { ...agent, name: "Background agent changed" } : agent),
  })));
  expect(commits).not.toHaveBeenCalled();
  act(() => useSessionStore.setState(current => ({
    sessions: current.sessions.map(session => session.id === "A" ? { ...session, name: "Current renamed" }
      : session.id === "parent" ? { ...session, agent_id: "unrelated" } : session),
    archivedSessions: current.archivedSessions.map(session => ({ ...session, name: "Source renamed" })),
    agents: current.agents.map(agent => agent.id === "owner" ? { ...agent, name: "Owner renamed" } : agent),
  })));
  expect(screen.getByText("Current renamed")).toBeInTheDocument();
  expect(screen.getByTestId("delegation-banner")).toHaveTextContent("Background agent changed");
  expect(screen.getByTestId("fork-banner")).toHaveTextContent("Source renamed");
  expect(screen.getAllByText("Owner renamed").length).toBeGreaterThan(0);
  act(() => applyTranscript("A", { messages: [{ role: "assistant", type: "text", seq: 0, content: "Current changed text" }],
    oldest_loaded_seq: 0, next_message_seq: 1 }));
  expect(screen.getByText("Current changed text")).toBeInTheDocument();
  expect(screen.queryByText("Current A text")).toBeNull();
});
