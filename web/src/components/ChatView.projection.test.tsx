import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import * as nativeOutput from "../lib/nativeOutput";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";

beforeEach(() => {
  mockChatViewport();
  sessionStorage.clear();
  useSessionStore.getState().setToken("");
  useSessionStore.setState({ activeSessionId: "foreground", mainView: "chat",
    sessions: [{ id: "foreground", name: "Foreground", status: "running", backend: "codex" } as SessionInfo],
    archivedSessions: [], agents: [], credentials: [], backendModels: {},
    messages: { foreground: [{ role: "assistant", type: "text", content: "Saved foreground answer", seq: 0 }] },
    subagents: {}, streamingText: {}, streamingItems: {}, dispatchStates: {}, pendingQuestions: {},
    messageWindows: {},
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("does no extra foreground projection for background work or unchanged active request identity", () => {
  // This probe counts the promised work; assertions below also verify the real
  // projection and Footer still respond to their own session's inputs.
  const project = vi.spyOn(nativeOutput, "nativeChatMessages");
  const view = render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()}
      connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>);
  fireEvent.click(view.getByRole("button", { name: "Browse loaded content" }));
  expect(view.getByText("Saved foreground answer")).toBeVisible();
  const dispatch = (id: string, revision: number, request: string | null = null) => {
    useSessionStore.getState().applyDispatchState(id, { generation: "projection-service", revision,
      active_request_id: request, active_operation: request ? "message" : null, paused: false, items: [] });
  };
  act(() => dispatch("foreground", 1, "current-request"));
  project.mockClear();
  for (let revision = 1; revision <= 20; revision++) {
    act(() => dispatch("background", revision, "other-request"));
    act(() => useSessionStore.getState().upsertSubagent("background", {
      task_id: "other-task", tool_call_id: "other-child", name: "Background helper", prompt: "Work",
      summary: "", steps: [], status: "running", description: `Step ${revision}`,
    }));
    act(() => dispatch("foreground", revision + 1, "current-request"));
  }
  expect(project).not.toHaveBeenCalled();
  act(() => useSessionStore.getState().addMessage("foreground", {
    role: "assistant", type: "text", content: "New foreground answer", seq: 1,
  }));
  expect(project).toHaveBeenCalledTimes(1);
  expect(view.getByText("New foreground answer")).toBeVisible();
  project.mockClear();
  const store = useSessionStore.getState();
  const identity = { request_id: "current-request", attempt_id: "attempt" };
  act(() => store.setActiveAttempt("foreground", identity));
  act(() => store.appendStreamingText("background", "Hidden background delta"));
  expect(view.queryByText("Hidden background delta")).toBeNull();
  act(() => store.appendStreamingText("foreground", "Visible foreground delta", identity));
  expect(view.getByTestId("streaming-text")).toHaveTextContent("Visible foreground delta");
  expect(project).not.toHaveBeenCalled();

  // A child can stream before its card is projected. While reading, those
  // deltas leave the saved window and root output untouched.
  const metadata = { ...identity, native: { thread_id: "child", turn_id: "turn", item_id: "item",
    root_thread_id: "root", root_turn_id: "root-turn" } };
  act(() => store.appendStreamingText("foreground", "Unobserved helper", metadata));
  act(() => store.appendStreamingText("foreground", " keeps streaming", metadata));
  expect(view.queryByText(/Unobserved helper/)).not.toBeInTheDocument();
  expect(view.getByTestId("streaming-text")).toHaveTextContent("Visible foreground delta");
  expect(project).not.toHaveBeenCalled();

  act(() => dispatch("foreground", 22, "next-request"));
  expect(project).toHaveBeenCalledTimes(1);
});
