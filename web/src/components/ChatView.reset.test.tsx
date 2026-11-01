import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type PendingQuestion, type SessionInfo } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";

const session = { id: "reset-parent", name: "Reset", status: "running", backend: "codex", created_at: "2026-10-26", working_dir: "/tmp" } as SessionInfo;
const question = (id: string): PendingQuestion => ({ question_id: id, questions: [{ question: `Question ${id}?`, header: "Decision", options: [{ label: "Yes", description: "Continue" }] }] });

function seedInteractions() {
  useSessionStore.setState({
    activeSessionId: session.id, mainView: "chat",
    sessions: [session], archivedSessions: [], agents: [], messages: {}, streamingText: {},
    lastAppliedSeq: {}, oldestSeq: {}, hasMoreMessages: {},
    pendingQuestions: {}, closedQuestionIds: {}, pendingForks: {}, credentials: [], backendModels: {},
    dispatchStates: {}, dispatchUnavailable: {}, dispatchGeneration: "service-a", retiredDispatchGenerations: [],
  });
  handleWsMessage({ type: "question_request", session_id: session.id, seq: 0, ...question("original") });
  useSessionStore.getState().setPendingFork(session.id, "original fork");
}
beforeEach(() => {
  mockChatViewport();
  useSessionStore.getState().setToken("synthetic-token");
  seedInteractions();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function startReset() {
  let resolve!: (response: Response) => void;
  let reject!: (error: Error) => void;
  const requests = vi.fn((url: string) => {
    if (url.endsWith("/reset")) return new Promise<Response>((yes, no) => { resolve = yes; reject = no; });
    if (url.includes("/submissions")) return Promise.resolve(Response.json({ items: [] }));
    return Promise.resolve(Response.json([]));
  });
  vi.stubGlobal("fetch", requests);
  render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>);
  // Virtuoso schedules initial viewport work; its cold render can exceed the
  // default one-second query wait while other test files share the workers.
  await screen.findByRole("button", { name: "Submit" }, { timeout: 3000 });
  fireEvent.change(screen.getByPlaceholderText(/Send to queue/), { target: { value: "/reset" } });
  fireEvent.click(document.querySelector<HTMLButtonElement>(".btn-send")!);
  await waitFor(() => expect(resolve).toBeTypeOf("function"));
  return { resolve, reject, requests };
}

it.each([401, 503, "network"])("reset refusal %s keeps the question/fork and displays the error", async (outcome) => {
  const { resolve, reject } = await startReset();
  await act(async () => {
    if (outcome === "network") reject(new TypeError("Network unavailable"));
    else resolve(Response.json({ detail: "Reset refused" }, { status: Number(outcome) }));
  });
  expect(screen.getByRole("alert")).toHaveTextContent(outcome === "network" ? "Network unavailable" : "Reset refused");
  expect(screen.getByText("Question original?")).toBeVisible();
  expect(useSessionStore.getState().pendingQuestions[session.id][0].question_id).toBe("original");
  expect(useSessionStore.getState().pendingForks[session.id].label).toBe("original fork");
});

it("reset success clears captured interactions while preserving later questions and fork intents", async () => {
  const { resolve } = await startReset();
  act(() => {
    handleWsMessage({ type: "question_request", session_id: session.id, seq: 1, ...question("later") });
    useSessionStore.getState().setPendingFork(session.id, "later fork");
  });
  await act(async () => resolve(Response.json({ status: "ok" })));
  expect(useSessionStore.getState().pendingQuestions[session.id].map((value) => value.question_id)).toEqual(["later"]);
  expect(useSessionStore.getState().pendingForks[session.id].label).toBe("later fork");
  expect(screen.getByText("Question later?")).toBeVisible();
});

it("confirmed reset closes the original question and fork", async () => {
  const { resolve } = await startReset();
  expect(screen.getByRole("button", { name: "Submit" })).toBeVisible();
  await act(async () => resolve(Response.json({ status: "ok" })));
  expect(useSessionStore.getState().pendingQuestions[session.id]).toBeUndefined();
  expect(useSessionStore.getState().pendingForks[session.id]).toBeUndefined();
  expect(useSessionStore.getState().closedQuestionIds[session.id].original).toBe(true);
  expect(screen.queryByRole("button", { name: "Submit" })).toBeNull();
});

it("does not launch the captured deferred fork when idle arrives before reset confirmation", async () => {
  const { resolve, requests } = await startReset();
  await act(async () => {
    const store = useSessionStore.getState();
    store.removePendingQuestion(session.id, "original");
    store.updateSessionStatus(session.id, "idle");
    store.applyDispatchState(session.id, { generation: "service-a", revision: 1, paused: false, active_request_id: null, items: [] });
  });
  expect(requests.mock.calls.some(([url]) => url.endsWith("/duplicate"))).toBe(false);
  await act(async () => resolve(Response.json({ status: "ok" })));
  expect(useSessionStore.getState().pendingForks[session.id]).toBeUndefined();
  expect(requests.mock.calls.some(([url]) => url.endsWith("/duplicate"))).toBe(false);
});

it.each(["account", "service", "focus"].flatMap((change) => [200, 503].map((status) => ({ change, status }))))("a stale reset response after $change change ($status) leaves current interactions intact", async ({ change, status }) => {
  const { resolve } = await startReset();
  act(() => {
    if (change === "account") {
      useSessionStore.getState().setToken("another-account");
      seedInteractions();
    }
    else if (change === "service") useSessionStore.setState({ dispatchGeneration: "service-b" });
    else {
      useSessionStore.getState().setActiveSessionId("different-session");
      useSessionStore.getState().setActiveSessionId(session.id);
    }
  });
  await act(async () => resolve(Response.json(status === 200 ? { status: "ok" } : { detail: "Obsolete failure" }, { status })));
  expect(useSessionStore.getState().pendingQuestions[session.id][0].question_id).toBe("original");
  expect(useSessionStore.getState().pendingForks[session.id].label).toBe("original fork");
  expect(screen.queryByRole("alert")).toBeNull();
});
