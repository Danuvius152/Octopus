import { deferred } from "../test-support/deferred";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionStore, type BgTask, type SubagentRun, type SessionInfo } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";
import { applySessionSnapshot, hydrateSession, loadBgTaskSnapshot, loadResearchSnapshot, loadSessionSnapshots, loadSessionSnapshot, observeSession, observeSessionAuxiliary } from "./hydrateSession";
import { selectSession } from "./selectSession";

// Real session details always distinguish a missing attempt from explicit null.
const json = (value: unknown) => new Response(JSON.stringify(
  value && typeof value === "object" && "dispatch_state" in value ? { active_attempt: null, ...value } : value
));
const dispatch = (generation: string) => ({ generation, revision: 0, paused: false, active_request_id: null, active_operation: null, items: [] });

beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.setState({ token: "test-bearer" });
});
afterEach(() => vi.unstubAllGlobals());

it.each(["claude-code", "codex"].flatMap((backend) => [false, true].map((newAttempt) => ({ backend, newAttempt }))))(
  "an accepted terminal clears only its own $backend buffer (new attempt=$newAttempt)", ({ backend, newAttempt }) => {
    useSessionStore.getState().applyDispatchState("s", { ...dispatch("stream-service"), active_request_id: "request", active_operation: "message" });
    handleWsMessage({ type: "attempt_started", session_id: "s", request_id: "request", attempt_id: "old" });
    const start = useSessionStore.getState();
    if (newAttempt) handleWsMessage({ type: "attempt_started", session_id: "s", request_id: "request", attempt_id: "new" });
    const identity = { request_id: "request", attempt_id: newAttempt ? "new" : "old",
      ...(backend === "codex" ? { native: { thread_id: "root", root_thread_id: "root", turn_id: "turn", root_turn_id: "turn", item_id: "item" } } : {}),
    };
    handleWsMessage({ type: "assistant_delta", session_id: "s", content: "Interrupted partial", runtime_metadata: identity });
    const content = { kind: "outcome", operation: "message", request_id: "request", status: "interrupted" };
    const terminal = { role: "system", type: "result", seq: 11, content,
      tool_input: { attempt_context: { request_id: "request", attempt_id: "old" } } };
    expect(applySessionSnapshot("s", { id: "s", messages: [terminal], next_message_seq: 12, oldest_loaded_seq: 11,
      dispatch_state: { ...dispatch("stream-service"), revision: 1 }, active_attempt: null }, start)).not.toBeNull();
    handleWsMessage({ type: "result", session_id: "s", seq: 11, content, request_id: "request", attempt_id: "old" });
    handleWsMessage({ type: "assistant_delta", session_id: "s", content: " stale", runtime_metadata: { ...identity, attempt_id: "old" } });
    const state = useSessionStore.getState();
    const partial = backend === "codex" ? Object.values(state.streamingItems.s || {}).map((item) => item.content)
      : state.streamingText.s ? [state.streamingText.s] : [];
    expect(partial).toEqual(newAttempt ? ["Interrupted partial"] : []);
    expect(state.activeAttempts.s).toEqual(newAttempt ? { request_id: "request", attempt_id: "new" } : undefined);
    expect(state.messages.s.map((row) => row.seq)).toEqual([11]);
    expect(state.lastAppliedSeq.s).toBe(11);
  }
);

const backgroundTask = (id: string, fields: Partial<BgTask> = {}) => ({ id, session_id: "s", status: "completed", stdout: "",
  delivery_disposition: "unknown", delivery_error: "Old admission error", ...fields } as BgTask);

it.each([200, 503])("another task's newer HTTP %s cannot prevent this detail from clearing delivery diagnostics", async (status) => {
  useSessionStore.getState().setBgTasks("s", [backgroundTask("a"), backgroundTask("b")]);
  const first = deferred<Response>(), second = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise));
  const a = observeSessionAuxiliary("s", "background", "a").done;
  const b = observeSessionAuxiliary("s", "background", "b").done.catch(() => {});
  second.resolve(status === 200 ? json(backgroundTask("b")) : new Response("Unavailable", { status }));
  await b;
  first.resolve(json(backgroundTask("a", { stdout: "Complete original output", delivery_disposition: null, delivery_error: null })));
  await a;
  expect(useSessionStore.getState().bgTasks.s.find((row) => row.id === "a")).toMatchObject({
    stdout: "Complete original output", delivery_disposition: null, delivery_error: null,
  });
  expect(Boolean(useSessionStore.getState().sessionAuxiliaryErrors.s?.background)).toBe(status === 503);
});

it.each([false, true])("a list fills final output across an execution event while respecting delivery events (%s)", async (delivery) => {
  useSessionStore.getState().setBgTasks("s", [backgroundTask("a", { status: "running", stdout: "short" })]);
  const reply = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn(() => reply.promise));
  const reading = loadBgTaskSnapshot("s");
  handleWsMessage({ type: "bg_completed", session_id: "s", task_id: "a", status: "completed", exit_code: 0, completed_at: "now" });
  if (delivery) handleWsMessage({ type: "bg_delivery", session_id: "s", task_id: "a", delivery_disposition: "unknown", delivery_error: "New live error" });
  reply.resolve(json([backgroundTask("a", { stdout: "Complete original output", delivery_disposition: null, delivery_error: null })]));
  await reading;
  expect(useSessionStore.getState().bgTasks.s[0]).toMatchObject({ status: "completed", stdout: "Complete original output",
    delivery_error: delivery ? "New live error" : null, delivery_disposition: delivery ? "unknown" : null });
});

it.each([true, false])("a finished newer detail or list retains per-task authority until older overlapping reads finish (%s)", async (listFirst) => {
  const old = deferred<Response>(), fresh = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise));
  const older = observeSessionAuxiliary("s", "background", listFirst ? undefined : "a").done;
  const newer = observeSessionAuxiliary("s", "background", listFirst ? "a" : undefined).done;
  const current = backgroundTask("a", { stdout: "New output", delivery_error: null, delivery_disposition: null });
  fresh.resolve(json(listFirst ? current : [current]));
  await newer;
  const previous = backgroundTask("a", { stdout: "Old output with more bytes" });
  old.resolve(json(listFirst ? [previous] : previous));
  await older;
  expect(useSessionStore.getState().bgTasks.s[0]).toMatchObject({ stdout: "Old output with more bytes", delivery_error: null, delivery_disposition: null });
});

it.each([true, false])("same-task successes use request order and preserve complete output (newer returns first=%s)", async (newerFirst) => {
  useSessionStore.getState().setBgTasks("s", [backgroundTask("a")]);
  const old = deferred<Response>(), fresh = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise));
  const older = observeSessionAuxiliary("s", "background", "a").done;
  const newer = observeSessionAuxiliary("s", "background", "a").done;
  const oldData = backgroundTask("a", { stdout: "Complete original output", delivery_error: "Older diagnosis" });
  const newData = backgroundTask("a", { stdout: "Complete", delivery_error: "New diagnosis" });
  if (newerFirst) { fresh.resolve(json(newData)); await newer; old.resolve(json(oldData)); await older; }
  else { old.resolve(json(oldData)); await older; fresh.resolve(json(newData)); await newer; }
  expect(useSessionStore.getState().bgTasks.s[0]).toMatchObject({ stdout: "Complete original output", delivery_error: "New diagnosis" });
});

it("a newer same-task failure owns only the error surface, leaving older successful data useful", async () => {
  useSessionStore.getState().setBgTasks("s", [backgroundTask("a")]);
  const old = deferred<Response>(), fresh = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise));
  const older = observeSessionAuxiliary("s", "background", "a").done;
  const newer = observeSessionAuxiliary("s", "background", "a").done.catch(() => {});
  fresh.resolve(new Response("Unavailable", { status: 503 })); await newer;
  old.resolve(json(backgroundTask("a", { stdout: "Saved output", delivery_error: null, delivery_disposition: null }))); await older;
  expect(useSessionStore.getState().bgTasks.s[0]).toMatchObject({ stdout: "Saved output", delivery_error: null });
  expect(useSessionStore.getState().sessionAuxiliaryErrors.s?.background).toBeTruthy();
});

it("view cancellation releases a read without publishing its late data or poisoning future reads", async () => {
  const old = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(old.promise).mockResolvedValueOnce(json(backgroundTask("a", { delivery_error: "Current" }))));
  const cancelled = observeSessionAuxiliary("s", "background", "a");
  cancelled.cancel();
  await observeSessionAuxiliary("s", "background", "a").done;
  old.resolve(json(backgroundTask("a", { delivery_error: "Obsolete" }))); await cancelled.done;
  expect(useSessionStore.getState().bgTasks.s[0].delivery_error).toBe("Current");
});

it("keeps the sidebar order when selecting sessions and receiving their details", async () => {
  const rows = ["first", "middle", "last"].map((id) => ({ id, name: id, archived: false, agent_id: "agent" } as SessionInfo));
  useSessionStore.setState({ sessions: rows, dispatchGeneration: "service" });
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const selected = rows.find((row) => new URL(url).pathname === `/api/sessions/${row.id}`);
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
  const response = deferred<Response>();
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
  const response = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const loaded = loadBgTaskSnapshot("s");
  useSessionStore.getState().upsertBgTask("s", { ...bg, status: "completed", stdout: "live output" });
  response.resolve(json([bg]));
  await loaded;
  expect(useSessionStore.getState().bgTasks.s[0].stdout).toBe("live output");
});

it.each(["new-attempt", "attempt-cleared", "new-dispatch"])("rejects an old execution snapshot after %s was observed", async (observation) => {
  const initial = { ...dispatch("service"), active_request_id: "r", active_operation: "message" as const, revision: 1 };
  useSessionStore.getState().applyDispatchState("s", initial);
  if (observation !== "attempt-cleared") useSessionStore.getState().setActiveAttempt("s", { request_id: "r", attempt_id: "a1" });
  const response = deferred<Response>();
  const confirmation = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(response.promise).mockReturnValueOnce(confirmation.promise));
  const read = observeSession("s", "detail");
  handleWsMessage({ type: "attempt_started", session_id: "s", request_id: "r", attempt_id: "a2" });
  if (observation === "attempt-cleared") handleWsMessage({ type: "result", session_id: "s", request_id: "r", content: "finished" });
  if (observation === "new-dispatch") handleWsMessage({ type: "dispatch_state", session_id: "s", dispatch_state: { ...initial, revision: 2 } });
  const current = useSessionStore.getState();
  response.resolve(json({ id: "s", messages: [], dispatch_state: initial,
    active_attempt: observation === "new-attempt" ? null : { request_id: "r", attempt_id: "a1" } }));
  expect(await read.done).not.toBeNull();
  expect(useSessionStore.getState().dispatchStates.s).toBe(current.dispatchStates.s);
  expect(useSessionStore.getState().activeAttempts.s).toBe(current.activeAttempts.s);
  read.cancel();
  confirmation.resolve(json({ id: "s", messages: [], dispatch_state: initial, active_attempt: null }));
});

it.each([undefined, {}, { request_id: "other", attempt_id: "a2" }])("refuses incomplete or inconsistent attempt data %j without changing dispatch", async (active) => {
  const initial = { ...dispatch("service"), active_request_id: "r", active_operation: "message" as const, revision: 1 };
  useSessionStore.getState().applyDispatchState("s", initial);
  useSessionStore.getState().setActiveAttempt("s", { request_id: "r", attempt_id: "a1" });
  const before = useSessionStore.getState();
  // Deliberately bypass the valid-detail factory to test the raw trust boundary.
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ id: "s", messages: [], dispatch_state: { ...initial, revision: 2 }, active_attempt: active })));
  expect(await observeSession("s", "detail").done).not.toBeNull();
  expect(useSessionStore.getState().dispatchStates.s).toBe(before.dispatchStates.s);
  expect(useSessionStore.getState().activeAttempts.s).toBe(before.activeAttempts.s);
  expect(useSessionStore.getState().dispatchUnavailable.s).toBe(true);
});

it("clears every old attempt when another session observes a new service generation", () => {
  useSessionStore.getState().applyDispatchState("s", dispatch("old"));
  for (const id of ["s", "other"]) useSessionStore.getState().setActiveAttempt(id, { request_id: "r", attempt_id: "a" });
  handleWsMessage({ type: "dispatch_state", session_id: "third", dispatch_state: dispatch("new") });
  expect(useSessionStore.getState().activeAttempts).toEqual({});
  expect(useSessionStore.getState().executionObservations).toEqual({ third: 1 });
});

it.each([false, true])("fills reconnect history and task results with one confirmation; another live change=%s", async (changedAgain) => {
  const initial = { ...dispatch("service"), revision: 1, active_request_id: "r", active_operation: "message" as const };
  useSessionStore.getState().applyDispatchState("s", initial);
  useSessionStore.getState().setActiveAttempt("s", { request_id: "r", attempt_id: "cached-a1" });
  const first = deferred<Response>(), confirmation = deferred<Response>();
  let detailReads = 0;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (new URL(url).pathname === "/api/sessions/s") return ++detailReads === 1 ? first.promise : confirmation.promise;
    if (url.endsWith("bg-tasks")) return Promise.resolve(json([{ id: "bg", status: "completed", stdout: "Saved output" }]));
    return Promise.resolve(json([]));
  }));
  const read = observeSession("s");
  handleWsMessage({ type: "dispatch_state", session_id: "s", dispatch_state: { ...initial, revision: 2 } });
  handleWsMessage({ type: "assistant_text", session_id: "s", seq: 1, content: "Live tail" });
  first.resolve(json({ id: "s", messages: [{ role: "user", type: "text", seq: 0, content: "Missing history" }],
    next_message_seq: 1, dispatch_state: initial, active_attempt: { request_id: "r", attempt_id: "cached-a1" },
    subagents: [{ tool_call_id: "native", status: "completed", summary: "Saved child answer" }] }));
  try {
    await read.done;
    expect(useSessionStore.getState().messages.s.map((row) => row.content)).toEqual(["Missing history", "Live tail"]);
    expect(useSessionStore.getState().subagents.s.native.summary).toBe("Saved child answer");
    await vi.waitFor(() => expect(useSessionStore.getState().bgTasks.s?.[0].stdout).toBe("Saved output"));
    expect(useSessionStore.getState().dispatchStates.s.revision).toBe(2);
    expect(detailReads).toBe(2);
    if (changedAgain) handleWsMessage({ type: "attempt_started", session_id: "s", request_id: "r", attempt_id: "latest-a3" });
    confirmation.resolve(json({ id: "s", messages: [{ role: "user", type: "text", seq: 2, content: "Confirmation history" }],
      next_message_seq: 3, dispatch_state: { ...initial, revision: 2 }, active_attempt: { request_id: "r", attempt_id: "actual-a2" } }));
    await vi.waitFor(() => expect(useSessionStore.getState().messages.s.some((row) => row.seq === 2)).toBe(true));
    await vi.waitFor(() => expect(useSessionStore.getState().activeAttempts.s.attempt_id).toBe(changedAgain ? "latest-a3" : "actual-a2"));
    expect(detailReads).toBe(2);
  } finally {
    confirmation.resolve(json({ id: "s", messages: [], dispatch_state: { ...initial, revision: 2 }, active_attempt: null }));
    read.cancel();
  }
});

it("ending the original observation also retires its pending execution confirmation", async () => {
  const initial = { ...dispatch("service"), active_request_id: "r", active_operation: "message" as const };
  useSessionStore.getState().applyDispatchState("s", initial);
  const first = deferred<Response>(), confirmation = deferred<Response>();
  const fetcher = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(confirmation.promise);
  vi.stubGlobal("fetch", fetcher);
  const read = observeSession("s", "detail");
  handleWsMessage({ type: "attempt_started", session_id: "s", request_id: "r", attempt_id: "live" });
  first.resolve(json({ id: "s", messages: [], dispatch_state: initial, active_attempt: null }));
  await read.done;
  expect(fetcher).toHaveBeenCalledTimes(2);
  read.cancel();
  confirmation.resolve(json({ id: "s", messages: [], dispatch_state: initial, active_attempt: null }));
  await Promise.resolve(); await Promise.resolve();
  expect(useSessionStore.getState().activeAttempts.s.attempt_id).toBe("live");
});

it("retains the service identity captured by its own read", async () => {
  useSessionStore.getState().applyDispatchState("s", dispatch("old"));
  const response = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const loaded = loadSessionSnapshot("s");
  useSessionStore.getState().applyDispatchState("s", dispatch("new"));
  response.resolve(json({ messages: [{ role: "user", type: "text", content: "retired", seq: 0 }], dispatch_state: dispatch("old") }));
  expect(await loaded).toBeNull();
  expect(useSessionStore.getState().messages.s).toBeUndefined();
});

it.each(["detail", "select"])("%s accepts its initial service after another session first establishes that generation", async (entry) => {
  const response = deferred<Response>();
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
  const response = deferred<Response>();
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
  const response = deferred<Response>();
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

it("select adopts detail in the same account as the transcript", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => json(url.endsWith("bg-tasks") ? [] : {
    id: "private", name: "Private old account", messages: [], dispatch_state: dispatch("service"),
  })));
  const unsubscribe = useSessionStore.subscribe((state, previous) => {
    if (state.messages.private && !previous.messages.private) {
      queueMicrotask(() => useSessionStore.getState().setToken("other-account"));
    }
  });
  try {
    await selectSession("private");
    expect(useSessionStore.getState().token).toBe("other-account");
    expect(useSessionStore.getState().sessions).toEqual([]);
    expect(useSessionStore.getState().messages.private).toBeUndefined();
  } finally {
    unsubscribe();
  }
});

it("hydrates missing metadata without requesting or applying chat history", async () => {
  const fetcher = vi.fn(async () => json({ id: "private", name: "Child", archived: true }));
  vi.stubGlobal("fetch", fetcher);
  await hydrateSession("private");
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher).toHaveBeenCalledWith(expect.stringContaining("/api/sessions/private/info"), expect.anything());
  expect(useSessionStore.getState().messages.private).toBeUndefined();
  expect(useSessionStore.getState().archivedSessions[0].id).toBe("private");
});

it("shows selected history and finishes navigation while background data is still pending", async () => {
  const detail = deferred<Response>();
  const background = deferred<Response>();
  const fetcher = vi.fn((url: string) => url.endsWith("bg-tasks") ? background.promise : detail.promise);
  vi.stubGlobal("fetch", fetcher);
  const loaded = selectSession("selected", "agent");
  expect(fetcher).toHaveBeenCalledTimes(4);
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
  const detail = deferred<Response>();
  const background = deferred<Response>();
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
  const background = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn(() => background.promise));
  const loaded = loadBgTaskSnapshot("s");
  useSessionStore.getState().applyDispatchState("s", dispatch("new"));
  background.resolve(json([{ id: "retired", status: "running" }]));
  await loaded;
  expect(useSessionStore.getState().bgTasks.s).toBeUndefined();
  expect(useSessionStore.getState().sessionAuxiliaryErrors.s).toBeUndefined();
});

it.each(["background", "research"] as const)("a newer %s success retires an older failure", async (kind) => {
  const first = deferred<Response>();
  const second = deferred<Response>();
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
  const research = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("/research") ? research.promise : Promise.resolve(json(
    url.endsWith("/bg-tasks") ? [] : { id: "s", dispatch_state: dispatch("service"), messages: [] },
  ))));
  await selectSession("s");
  expect(useSessionStore.getState().messages.s).toEqual([]);
  research.resolve(json([]));
  await Promise.resolve();
});

it("holds early auxiliary data until the detail accepts its service", async () => {
  const detail = deferred<Response>();
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
  const response = deferred<Response>();
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

it("does not replace a newer successful detail with an earlier failed read", async () => {
  useSessionStore.setState({ dispatchGeneration: "service" });
  const first = deferred<Response>(), second = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise));
  const older = loadSessionSnapshot("s");
  const failed = expect(older).rejects.toThrow("offline");
  const newer = loadSessionSnapshot("s");
  second.resolve(json({ id: "s", messages: [], dispatch_state: dispatch("service") }));
  await newer;
  first.reject(new Error("offline"));
  await failed;
  expect(useSessionStore.getState().dispatchUnavailable.s).toBeFalsy();
});

it("accepts a completed native task from overlapping successful details without a WS event", async () => {
  const running = { tool_call_id: "native", status: "running" } as SubagentRun;
  useSessionStore.getState().setSubagents("s", [running]);
  const first = deferred<Response>(), second = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise));
  const older = loadSessionSnapshot("s"), newer = loadSessionSnapshot("s");
  first.resolve(json({ id: "s", messages: [], dispatch_state: dispatch("service"), subagents: [running] }));
  await older;
  second.resolve(json({ id: "s", messages: [], dispatch_state: dispatch("service"), subagents: [{ ...running, status: "completed", summary: "Finished" }] }));
  await newer;
  expect(useSessionStore.getState().subagents.s.native).toMatchObject({ status: "completed", summary: "Finished" });
});

it("does not resurrect a question absent from the newer successful detail", async () => {
  const question = { question_id: "original", questions: [] };
  useSessionStore.getState().setPendingQuestions("s", [question]);
  const first = deferred<Response>(), second = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise));
  const older = loadSessionSnapshot("s"), newer = loadSessionSnapshot("s");
  second.resolve(json({ id: "s", messages: [], dispatch_state: dispatch("service"), pending_questions: [] })); await newer;
  first.resolve(json({ id: "s", messages: [], dispatch_state: dispatch("service"), pending_questions: [question] })); await older;
  expect(useSessionStore.getState().pendingQuestions.s || []).toEqual([]);
});

it("ends all parts of an observation without cancelling server work", async () => {
  const response = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const read = observeSession("s");
  read.cancel();
  response.resolve(json({ id: "s", messages: [], dispatch_state: dispatch("service") }));
  expect(await read.done).toBeNull();
  expect(useSessionStore.getState().messages.s).toBeUndefined();
  expect(fetch).not.toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ method: "POST" }));
});

it("keeps legal previous-session history without taking selection during A to B to A", async () => {
  const a = deferred<Response>(), b = deferred<Response>(), again = deferred<Response>();
  let aReads = 0;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (new URL(url).pathname === "/api/sessions/a") return ++aReads === 1 ? a.promise : again.promise;
    if (new URL(url).pathname === "/api/sessions/b") return b.promise;
    return Promise.resolve(json([]));
  }));
  const first = selectSession("a"), second = selectSession("b"), third = selectSession("a");
  again.resolve(json({ id: "a", agent_id: "agent-a", messages: [], dispatch_state: dispatch("service") })); await third;
  b.resolve(json({ id: "b", agent_id: "agent-b", messages: [{ type: "text", role: "user", seq: 0, content: "Cached B" }], dispatch_state: dispatch("service") })); await second;
  a.resolve(json({ id: "a", agent_id: "agent-a", messages: [], dispatch_state: dispatch("service") })); await first;
  expect(useSessionStore.getState().activeSessionId).toBe("a");
  expect(useSessionStore.getState().activeAgentId).toBe("agent-a");
  expect(useSessionStore.getState().messages.b[0].content).toBe("Cached B");
});

it.each(["session_deleted", "session_archived"])("does not rehydrate an old open after %s", async (type) => {
  useSessionStore.setState({ sessions: [{ id: "s", archived: false } as SessionInfo], dispatchGeneration: "service" });
  const response = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const read = observeSession("s", "detail");
  handleWsMessage({ type, session_id: "s", old_session_id: "s", new_session_id: null });
  response.resolve(json({ id: "s", archived: false, messages: [{ role: "user", type: "text", seq: 0, content: "stale" }], dispatch_state: dispatch("service") }));
  expect(await read.done).toBeNull();
  expect(useSessionStore.getState().messages.s).toBeUndefined();
  expect(useSessionStore.getState().sessions.some((row) => row.id === "s")).toBe(false);
});

it.each(["complete", "detail"] as const)("invalidates a %s read when its unknown target is deleted", async (purpose) => {
  useSessionStore.setState({ dispatchGeneration: "service" });
  const response = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("/unknown") ? response.promise : Promise.resolve(json([]))));
  const read = observeSession("unknown", purpose);
  handleWsMessage({ type: "session_deleted", session_id: "unknown" });
  response.resolve(json({ id: "unknown", dispatch_state: dispatch("service"), messages: [{ role: "user", type: "text", seq: 0, content: "Deleted original" }] }));
  expect(await read.done).toBeNull();
  await Promise.resolve();
  expect(useSessionStore.getState().messages.unknown).toBeUndefined();
  expect(useSessionStore.getState().bgTasks.unknown).toBeUndefined();
  expect(useSessionStore.getState().research.unknown).toBeUndefined();
  expect(useSessionStore.getState().delegations.unknown).toBeUndefined();
});

it("does not publish a late failure for an unknown target deleted during its read", async () => {
  useSessionStore.setState({ dispatchGeneration: "service" });
  const response = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const read = observeSession("unknown", "detail");
  const failure = expect(read.done).rejects.toThrow("offline");
  handleWsMessage({ type: "session_deleted", session_id: "unknown" });
  response.reject(new Error("offline"));
  await failure;
  expect(useSessionStore.getState().dispatchUnavailable.unknown).toBeUndefined();
});
