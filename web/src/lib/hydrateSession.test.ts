import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionStore, type BgTask, type SubagentRun } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";
import { hydrateSession, loadBgTaskSnapshot, loadSessionSnapshots, loadSessionSnapshot } from "./hydrateSession";
import { selectSession } from "./selectSession";

function deferred() {
  let resolve!: (response: Response) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<Response>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const json = (value: unknown) => new Response(JSON.stringify(value));
const dispatch = (generation: string) => ({ generation, revision: 0, paused: false, active_request_id: null, items: [] });

beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.setState({ token: "test-bearer" });
});
afterEach(() => vi.unstubAllGlobals());

it("captures detail state before fetch and keeps newer WS rows, questions and subagents", async () => {
  const running = { tool_call_id: "call", status: "running", name: "Worker" } as SubagentRun;
  useSessionStore.getState().upsertSubagent("s", running);
  const response = deferred();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const loaded = loadSessionSnapshot("s");
  handleWsMessage({ type: "assistant_text", session_id: "s", seq: 1, content: "new live row" });
  handleWsMessage({ type: "question_request", session_id: "s", seq: 2, question_id: "q", questions: [] });
  useSessionStore.getState().upsertSubagent("s", { ...running, status: "completed", summary: "new result" });
  response.resolve(json({ messages: [{ role: "user", type: "text", content: "old", seq: 0 }],
    dispatch_state: dispatch("service"), next_message_seq: 1, pending_questions: [], subagents: [running] }));
  await loaded;
  const state = useSessionStore.getState();
  expect(state.messages.s.map((m) => m.seq)).toEqual([0, 1, 2]);
  expect(state.pendingQuestions.s[0].question_id).toBe("q");
  expect(state.subagents.s.call.summary).toBe("new result");
});

it("captures the background baseline before fetch", async () => {
  const bg = { id: "bg", status: "running" } as BgTask;
  useSessionStore.getState().setBgTasks("s", [bg]);
  const response = deferred();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const loaded = loadBgTaskSnapshot("s");
  useSessionStore.getState().upsertBgTask("s", { ...bg, status: "completed", stdout: "live output" });
  response.resolve(json([bg]));
  await loaded;
  expect(useSessionStore.getState().bgTasks.s[0].stdout).toBe("live output");
});

it("retains an explicitly supplied earlier service identity", async () => {
  const start = useSessionStore.getState();
  useSessionStore.getState().applyDispatchState("s", dispatch("new"));
  vi.stubGlobal("fetch", vi.fn(async () => json({ messages: [{ role: "user", type: "text", content: "retired", seq: 0 }],
    dispatch_state: dispatch("old") })));
  expect(await loadSessionSnapshot("s", start)).toBeNull();
  expect(useSessionStore.getState().messages.s).toBeUndefined();
});

it.each(["detail", "background"])("rejects a %s response after an account switch", async (kind) => {
  const response = deferred();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const loaded = kind === "detail" ? loadSessionSnapshot("s") : loadBgTaskSnapshot("s");
  const rejection = expect(loaded).rejects.toThrow("previous sign-in");
  useSessionStore.getState().setToken("other-bearer");
  response.resolve(json(kind === "detail" ? { messages: [] } : []));
  await rejection;
  expect(useSessionStore.getState().messages.s).toBeUndefined();
  expect(useSessionStore.getState().bgTasks.s).toBeUndefined();
  expect(useSessionStore.getState().dispatchUnavailable.s).toBeUndefined();
});

it("rejects a late archived body after a different session changes service generation", async () => {
  useSessionStore.getState().applyDispatchState("other", dispatch("old"));
  const response = deferred();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const loaded = loadSessionSnapshot("archived");
  handleWsMessage({ type: "dispatch_state", session_id: "other", dispatch_state: dispatch("new") });
  response.resolve(json({ archived: true, dispatch_state: null,
    messages: [{ role: "user", type: "text", seq: 0, content: "old body" }],
    pending_questions: [{ question_id: "old-question", questions: [] }],
  }));
  expect(await loaded).toBeNull();
  expect(useSessionStore.getState().messages.archived).toBeUndefined();
  expect(useSessionStore.getState().pendingQuestions.archived).toBeUndefined();
});

it.each(["hydrate", "select"])("%s adopts detail in the same account as the transcript", async (entry) => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => json(url.endsWith("bg-tasks") ? [] : {
    id: "private", name: "Private old account", messages: [], dispatch_state: dispatch("service"),
  })));
  const unsubscribe = useSessionStore.subscribe((state, previous) => {
    if (state.messages.private && !previous.messages.private) {
      queueMicrotask(() => useSessionStore.getState().setToken("other-account"));
    }
  });
  try {
    if (entry === "hydrate") await hydrateSession("private");
    else await selectSession("private");
    expect(useSessionStore.getState().token).toBe("other-account");
    expect(useSessionStore.getState().sessions).toEqual([]);
    expect(useSessionStore.getState().messages.private).toBeUndefined();
  } finally {
    unsubscribe();
  }
});

it("starts selection requests together, waits for both and adopts detail before reading bg", async () => {
  const detail = deferred();
  const background = deferred();
  const fetcher = vi.fn((url: string) => url.endsWith("bg-tasks") ? background.promise : detail.promise);
  vi.stubGlobal("fetch", fetcher);
  const loaded = selectSession("selected", "agent");
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(useSessionStore.getState().activeSessionId).toBe("selected");
  expect(useSessionStore.getState().activeAgentId).toBe("agent");
  detail.resolve(json({ id: "selected", name: "Child", messages: [], dispatch_state: dispatch("service") }));
  await Promise.resolve();
  expect(useSessionStore.getState().sessions).toEqual([]);
  // A malformed bg body still leaves the accepted detail in the sidebar.
  background.resolve(new Response("invalid JSON"));
  await loaded;
  expect(useSessionStore.getState().sessions).toEqual([expect.objectContaining({ id: "selected", name: "Child" })]);
  expect(useSessionStore.getState().sessions[0]).not.toHaveProperty("messages");
});

it("keeps paired request failure atomic and leaves archived detail out of the live list", async () => {
  const detail = deferred();
  const background = deferred();
  vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("bg-tasks") ? background.promise : detail.promise));
  const loaded = loadSessionSnapshots("archived");
  const rejection = expect(loaded).rejects.toThrow("offline");
  detail.resolve(json({ id: "archived", archived: true, messages: [], dispatch_state: null }));
  background.reject(new Error("offline"));
  await rejection;
  expect(useSessionStore.getState().messages.archived).toBeUndefined();

  vi.stubGlobal("fetch", vi.fn(async (url: string) => json(url.endsWith("bg-tasks") ? [] : { id: "archived", archived: true, messages: [], dispatch_state: null })));
  await loadSessionSnapshots("archived");
  expect(useSessionStore.getState().messages.archived).toEqual([]);
  expect(useSessionStore.getState().sessions).toEqual([]);
});

it("routes a selected archived detail to read-only history and its owning agent", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => json(url.endsWith("bg-tasks") ? [] : {
    id: "archived", agent_id: "archive-owner", archived: true, messages: [], dispatch_state: null,
  })));
  await selectSession("archived");
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(useSessionStore.getState().archivedSessions).toEqual([expect.objectContaining({ id: "archived" })]);
  expect(useSessionStore.getState().activeAgentId).toBe("archive-owner");
});
