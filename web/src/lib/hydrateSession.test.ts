import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionStore, type BgTask, type SubagentRun, type SessionInfo } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";
import { hydrateSession, loadBgTaskSnapshot, loadResearchSnapshot, loadSessionSnapshots, loadSessionSnapshot } from "./hydrateSession";
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

it("keeps the sidebar order when selecting sessions and receiving their details", async () => {
  const rows = ["first", "middle", "last"].map((id) => ({ id, name: id, archived: false, agent_id: "agent" } as SessionInfo));
  useSessionStore.setState({ sessions: rows, dispatchGeneration: "service" });
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const selected = rows.find((row) => url.endsWith(`/api/sessions/${row.id}`));
    return json(selected ? { ...selected, message_count: 3, messages: [], dispatch_state: dispatch("service") } : []);
  }));

  for (const id of ["first", "middle", "first"]) {
    await selectSession(id);
    expect(useSessionStore.getState().activeSessionId).toBe(id);
    expect(useSessionStore.getState().sessions.map((row) => row.id)).toEqual(["first", "middle", "last"]);
    expect(useSessionStore.getState().sessions.find((row) => row.id === id)?.message_count).toBe(3);
  }
});

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

it.each(["detail", "select"])("%s accepts its initial service after another session first establishes that generation", async (entry) => {
  const response = deferred();
  vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("bg-tasks") ? Promise.resolve(json([])) : response.promise));
  const loaded = entry === "detail" ? loadSessionSnapshot("selected") : selectSession("selected");
  handleWsMessage({ type: "dispatch_state", session_id: "other", dispatch_state: dispatch("service") });
  response.resolve(json({ id: "selected", status: "idle", archived: false,
    dispatch_state: dispatch("service"), messages: [{ role: "user", type: "text", seq: 0, content: "sent history" }] }));
  await loaded;
  expect(useSessionStore.getState().messages.selected).toEqual([expect.objectContaining({ content: "sent history" })]);
  expect(useSessionStore.getState().dispatchStates.selected.generation).toBe("service");
  expect(useSessionStore.getState().sessions.some((row) => row.id === "selected")).toBe(true);
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

it("shows selected history and finishes navigation while background data is still pending", async () => {
  const detail = deferred();
  const background = deferred();
  const fetcher = vi.fn((url: string) => url.endsWith("bg-tasks") ? background.promise : detail.promise);
  vi.stubGlobal("fetch", fetcher);
  const loaded = selectSession("selected", "agent");
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(useSessionStore.getState().activeSessionId).toBe("selected");
  expect(useSessionStore.getState().activeAgentId).toBe("agent");
  detail.resolve(json({ id: "selected", name: "Child", messages: [{ role: "assistant", type: "text", content: "History", seq: 0 }], dispatch_state: dispatch("service") }));
  try {
    await vi.waitFor(() => expect(useSessionStore.getState().messages.selected?.[0].content).toBe("History"));
    await loaded;
  } finally {
    background.resolve(json([]));
  }
  expect(useSessionStore.getState().sessions).toEqual([expect.objectContaining({ id: "selected", name: "Child" })]);
  expect(useSessionStore.getState().sessions[0]).not.toHaveProperty("messages");
});

it("keeps archived history available when background refresh fails", async () => {
  const detail = deferred();
  const background = deferred();
  vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("bg-tasks") ? background.promise : detail.promise));
  const loaded = loadSessionSnapshots("archived");
  detail.resolve(json({ id: "archived", archived: true, messages: [], dispatch_state: null }));
  background.reject(new Error("offline"));
  await loaded;
  await vi.waitFor(() => expect(useSessionStore.getState().sessionAuxiliaryErrors.archived?.background).toBeTruthy());
  expect(useSessionStore.getState().messages.archived).toEqual([]);
  expect(useSessionStore.getState().dispatchUnavailable.archived).toBeUndefined();

  vi.stubGlobal("fetch", vi.fn(async (url: string) => json(url.endsWith("bg-tasks") ? [] : { id: "archived", archived: true, messages: [], dispatch_state: null })));
  await loadSessionSnapshots("archived");
  expect(useSessionStore.getState().messages.archived).toEqual([]);
  expect(useSessionStore.getState().sessions).toEqual([]);
});

it("keeps a background body from an obsolete service out of the current cards", async () => {
  useSessionStore.getState().applyDispatchState("s", dispatch("old"));
  const background = deferred();
  vi.stubGlobal("fetch", vi.fn(() => background.promise));
  const loaded = loadBgTaskSnapshot("s");
  useSessionStore.getState().applyDispatchState("s", dispatch("new"));
  background.resolve(json([{ id: "retired", status: "running" }]));
  await loaded;
  expect(useSessionStore.getState().bgTasks.s).toBeUndefined();
  expect(useSessionStore.getState().sessionAuxiliaryErrors.s).toBeUndefined();
});

it.each(["background", "research"] as const)("a newer %s success retires an older failure", async (kind) => {
  const first = deferred();
  const second = deferred();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise));
  const reader = kind === "background" ? loadBgTaskSnapshot : loadResearchSnapshot;
  const older = reader("s");
  const oldFailure = expect(older).rejects.toThrow("offline");
  const newer = reader("s");
  second.resolve(json([]));
  await newer;
  first.reject(new Error("offline"));
  await oldFailure;
  expect(useSessionStore.getState().sessionAuxiliaryErrors.s).toBeUndefined();
});

it("does not let a pending research body delay selected history", async () => {
  const research = deferred();
  vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("/research") ? research.promise : Promise.resolve(json(
    url.endsWith("/bg-tasks") ? [] : { id: "s", dispatch_state: dispatch("service"), messages: [] },
  ))));
  await selectSession("s");
  expect(useSessionStore.getState().messages.s).toEqual([]);
  research.resolve(json([]));
  await Promise.resolve();
});

it("holds early auxiliary data until the detail accepts its service", async () => {
  const detail = deferred();
  vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("/bg-tasks") ? Promise.resolve(json([{ id: "bg", status: "running" }])) :
    url.endsWith("/research") ? Promise.resolve(json([])) : detail.promise));
  const loaded = loadSessionSnapshots("s");
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(useSessionStore.getState().bgTasks.s).toBeUndefined();
  detail.resolve(json({ id: "s", messages: [], dispatch_state: dispatch("service") }));
  await loaded;
  await vi.waitFor(() => expect(useSessionStore.getState().bgTasks.s?.[0].id).toBe("bg"));
});

it("does not publish auxiliary data when authoritative detail fails", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith("/bg-tasks") ? json([{ id: "bg", status: "running" }]) :
    new Response(null, { status: 503 })));
  await expect(loadSessionSnapshots("s")).rejects.toThrow("HTTP 503");
  expect(useSessionStore.getState().bgTasks.s).toBeUndefined();
  expect(useSessionStore.getState().dispatchUnavailable.s).toBe(true);
  expect(useSessionStore.getState().sessionAuxiliaryErrors.s).toBeUndefined();
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

it("keeps a newer list status when a previously requested detail finishes", async () => {
  const running = { id: "session", status: "running", archived: false } as SessionInfo;
  useSessionStore.setState({ sessions: [running], dispatchGeneration: "service" });
  const response = deferred();
  vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("/api/sessions") ? Promise.resolve(json([{ ...running, status: "idle" }])) : response.promise));
  const loading = loadSessionSnapshot("session");
  handleWsMessage({ type: "status", session_id: "session", status: "idle" });
  response.resolve(json({ ...running, messages: [], dispatch_state: dispatch("service") }));
  await loading;
  expect(useSessionStore.getState().sessions[0].status).toBe("idle");
});

it("rejects list fields from a detail whose dispatch revision is already known to be older", async () => {
  const idle = { id: "session", status: "idle", archived: false } as SessionInfo;
  useSessionStore.setState({ sessions: [idle], dispatchGeneration: "service" });
  useSessionStore.getState().applyDispatchState("session", { ...dispatch("service"), revision: 5 });
  vi.stubGlobal("fetch", vi.fn(async () => json({ ...idle, status: "running", messages: [],
    dispatch_state: { ...dispatch("service"), revision: 4 } })));
  await loadSessionSnapshot("session");
  expect(useSessionStore.getState().sessions[0].status).toBe("idle");
});
