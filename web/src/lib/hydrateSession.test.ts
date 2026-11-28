import { deferred } from "../test-support/deferred";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { captureStreamingRead, observeAcceptedText, useSessionStore, type BgTask, type SubagentRun, type SessionInfo } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";
import { applySessionSnapshot, hydrateSession, loadBgTaskSnapshot, loadResearchSnapshot, loadSessionSnapshots, loadSessionSnapshot, observeSession, observeSessionAuxiliary } from "./hydrateSession";
import { selectSession } from "./selectSession";
import { nativeItemKey } from "./nativeOutput";
import { sessions } from "./sessionList";
import { watchDelegation } from "./delegationReads";

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

it("preserves only saturated streams that received text during a higher-watermark detail read", async () => {
  const attempt = { request_id: "r", attempt_id: "a" };
  const dispatchState = { ...dispatch("stream-service"), active_request_id: "r", active_operation: "message" as const };
  useSessionStore.getState().applyDispatchState("s", dispatchState);
  handleWsMessage({ type: "attempt_started", session_id: "s", ...attempt });
  const item = (item_id: string) => ({ ...attempt, native: { thread_id: "root", root_thread_id: "root",
    turn_id: "turn", root_turn_id: "turn", item_id } });
  const a = item("a"), b = item("b");
  const send = (content: string, runtime_metadata = attempt) =>
    handleWsMessage({ type: "assistant_delta", session_id: "s", content, runtime_metadata });
  const large = "x".repeat(70 * 1024);
  for (const metadata of [attempt, a, b]) send(large, metadata);
  const preview = useSessionStore.getState();
  const first = deferred<Response>(), second = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise));
  const reply = (seq: number, messages: unknown[]) => json({ id: "s", messages, next_message_seq: seq + 1,
    oldest_loaded_seq: seq, dispatch_state: dispatchState, active_attempt: attempt });
  const reading = loadSessionSnapshot("s");
  send("root arrived");
  send("item a arrived", a);
  first.resolve(reply(5, [{ role: "user", type: "text", seq: 5, content: "Saved question" }]));
  await reading;
  const merged = useSessionStore.getState();
  expect(merged.lastAppliedSeq.s).toBe(5);
  expect(merged.streamingText.s).toBe(preview.streamingText.s);
  expect(Object.keys(merged.streamingItems.s)).toEqual([nativeItemKey(a)]);
  expect(merged.streamingItems.s[nativeItemKey(a)!]).toBe(preview.streamingItems.s[nativeItemKey(a)!]);

  const completing = loadSessionSnapshot("s");
  send("another root arrival");
  send("another item a arrival", a);
  second.resolve(reply(6, [{ role: "assistant", type: "text", seq: 6, content: "Saved preview",
    body_state: "preview", detail_ref: "saved-a", runtime_metadata: a }]));
  await completing;
  expect(useSessionStore.getState().streamingText.s).toBe(preview.streamingText.s);
  expect(useSessionStore.getState().streamingItems.s ?? {}).toEqual({});
  expect(useSessionStore.getState().messages.s.find((row) => row.seq === 6)).toMatchObject({
    content: "Saved preview", body_state: "preview",
  });
});

it("retires both saturated previews when detail confirms a replacement at the same watermark", async () => {
  const first = { request_id: "r", attempt_id: "first" };
  const second = { ...first, attempt_id: "second" };
  const dispatchState = { ...dispatch("stream-service"), active_request_id: "r", active_operation: "message" as const };
  useSessionStore.getState().applyDispatchState("s", dispatchState);
  handleWsMessage({ type: "attempt_started", session_id: "s", ...first });
  handleWsMessage({ type: "user_message", session_id: "s", seq: 5, content: "Saved question" });
  const native = { thread_id: "root", root_thread_id: "root", turn_id: "turn", root_turn_id: "turn", item_id: "item" };
  const send = (content: string, runtime_metadata?: typeof first & { native?: typeof native }) =>
    handleWsMessage({ type: "assistant_delta", session_id: "s", content, runtime_metadata });
  send("x".repeat(70 * 1024), first);
  send("x".repeat(17 * 1024), { ...first, native });
  const original = useSessionStore.getState();
  let reported = first;
  vi.stubGlobal("fetch", vi.fn(async () => json({ id: "s", messages: [{ role: "user", type: "text", seq: 5,
    content: "Saved question" }], next_message_seq: 6, oldest_loaded_seq: 5, dispatch_state: dispatchState, active_attempt: reported })));
  await loadSessionSnapshot("s");
  expect(useSessionStore.getState().streamingText.s).toBe(original.streamingText.s);
  expect(Object.values(useSessionStore.getState().streamingItems.s)[0]).toBe(Object.values(original.streamingItems.s)[0]);

  reported = second;
  const retired = vi.fn();
  const release = useSessionStore.subscribe((state) => {
    if (state.activeAttempts.s?.attempt_id === second.attempt_id) {
      retired(state.streamingText.s, Object.keys(state.streamingItems.s ?? {}));
    }
  });
  try { await loadSessionSnapshot("s"); }
  finally { release(); }
  expect(retired).toHaveBeenCalled();
  expect(retired.mock.calls.every(([root, items]) => root === undefined && items.length === 0)).toBe(true);
  expect(useSessionStore.getState().activeAttempts.s).toEqual(second);
  expect(useSessionStore.getState().streamingText.s).toBeUndefined();
  expect(Object.values(useSessionStore.getState().streamingItems.s ?? {})).toEqual([]);
  send("New attempt", second);
  send("New native item", { ...second, native });
  handleWsMessage({ type: "attempt_started", session_id: "s", ...second });
  send("Late attempt", first);
  handleWsMessage({ type: "result", session_id: "s", seq: 6, ...first, content: "Old attempt result" });
  send(" without identity");
  expect(useSessionStore.getState().streamingText.s).toEqual({ content: "New attempt without identity", attempt: second });
  expect(Object.values(useSessionStore.getState().streamingItems.s).map((item) => item.content)).toEqual(["New native item"]);
});

it("retires ownerless legacy text at the WS start after detail has already confirmed that attempt", async () => {
  const attempt = { request_id: "r", attempt_id: "b" };
  const dispatchState = { ...dispatch("stream-service"), active_request_id: "r", active_operation: "message" as const };
  useSessionStore.getState().applyDispatchState("s", dispatchState);
  handleWsMessage({ type: "user_message", session_id: "s", seq: 5, content: "Saved question" });
  handleWsMessage({ type: "assistant_delta", session_id: "s", content: "Legacy partial" });
  vi.stubGlobal("fetch", vi.fn(async () => json({ id: "s", messages: [{ role: "user", type: "text", seq: 5,
    content: "Saved question" }], next_message_seq: 6, oldest_loaded_seq: 5, dispatch_state: dispatchState, active_attempt: attempt })));
  await loadSessionSnapshot("s");
  expect(useSessionStore.getState().activeAttempts.s).toEqual(attempt);
  expect(useSessionStore.getState().streamingText.s).toEqual({ content: "Legacy partial" });
  const metadata = { ...attempt, native: {
    thread_id: "root", root_thread_id: "root", turn_id: "turn", root_turn_id: "turn", item_id: "item",
  } };
  handleWsMessage({ type: "assistant_delta", session_id: "s", content: "New item", runtime_metadata: metadata });
  const partial = useSessionStore.getState().streamingItems.s;
  const observe = vi.fn((state: ReturnType<typeof useSessionStore.getState>) => state.streamingText.s);
  const release = useSessionStore.subscribe(observe);
  try { handleWsMessage({ type: "attempt_started", session_id: "s", ...attempt }); }
  finally { release(); }
  expect(observe.mock.results.map((result) => result.value)).toEqual([undefined]);
  expect(useSessionStore.getState().streamingText.s).toBeUndefined();
  expect(useSessionStore.getState().streamingItems.s).toBe(partial);
});

it("retains a changed native item across delayed detail decoding without announcing empty text", async () => {
  const attempt = { request_id: "r", attempt_id: "a" };
  const dispatchState = { ...dispatch("stream-service"), active_request_id: "r", active_operation: "message" as const };
  const store = useSessionStore.getState();
  store.applyDispatchState("s", dispatchState);
  handleWsMessage({ type: "attempt_started", session_id: "s", ...attempt });
  const item = (item_id: string) => ({ ...attempt, native: { thread_id: "root", root_thread_id: "root",
    turn_id: "turn", root_turn_id: "turn", item_id } });
  const changed = item("changed"), equal = item("equal"), earliest = item("earliest");
  const send = (content: string, runtime_metadata: typeof changed & { tool_call_id?: string }) =>
    handleWsMessage({ type: "assistant_delta", session_id: "s", content, runtime_metadata });
  const large = "x".repeat(17 * 1024);
  for (const metadata of [earliest, changed, equal]) send(large, metadata);
  const before = useSessionStore.getState();
  const body = deferred<unknown>();
  const decoding = deferred<void>();
  const response = new Response();
  vi.spyOn(response, "json").mockImplementation(() => { decoding.resolve(); return body.promise; });
  vi.stubGlobal("fetch", vi.fn(async () => response));
  const read = loadSessionSnapshot("s");
  await decoding.promise;
  const heard = vi.fn();
  const release = observeAcceptedText("s", store.accountGeneration, heard);
  try {
    send("", { ...equal, native: { ...equal.native } });
    expect(useSessionStore.getState()).toBe(before);
    const updated = { ...changed, tool_call_id: "m".repeat(216 * 1024) };
    send("", updated);
    const items = useSessionStore.getState().streamingItems.s;
    expect(Object.keys(items)).toEqual([nativeItemKey(changed), nativeItemKey(equal)]);
    expect(new TextEncoder().encode(JSON.stringify(items)).length).toBeLessThanOrEqual(256 * 1024);
    const changedPreview = items[nativeItemKey(changed)!];
    expect(changedPreview.content).toBe(before.streamingItems.s[nativeItemKey(changed)!].content);
    expect(changedPreview.runtime_metadata).toBe(updated);
    expect(heard).not.toHaveBeenCalled();
    body.resolve({ id: "s", messages: [{ role: "user", type: "text", seq: 5, content: "Saved question" }],
      next_message_seq: 6, oldest_loaded_seq: 5, dispatch_state: dispatchState, active_attempt: attempt });
    await read;
    const merged = useSessionStore.getState().streamingItems.s;
    expect(Object.keys(merged)).toEqual([nativeItemKey(changed)]);
    expect(merged[nativeItemKey(changed)!]).toBe(changedPreview);
  } finally { release(); }
});

it("distinguishes an evicted native preview rebuilt with the same key and content during a detail read", async () => {
  const attempt = { request_id: "r", attempt_id: "a" };
  const dispatchState = { ...dispatch("stream-service"), active_request_id: "r", active_operation: "message" as const };
  useSessionStore.getState().applyDispatchState("s", dispatchState);
  handleWsMessage({ type: "attempt_started", session_id: "s", ...attempt });
  const metadata = (item_id: string) => ({ ...attempt, native: { thread_id: "root", root_thread_id: "root",
    turn_id: "turn", root_turn_id: "turn", item_id } });
  const send = (id: string) => handleWsMessage({ type: "assistant_delta", session_id: "s", content: "partial",
    runtime_metadata: metadata(id) });
  const key = nativeItemKey(metadata("original"))!;
  send("original");
  const original = useSessionStore.getState().streamingItems.s[key];
  const response = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const read = loadSessionSnapshot("s");
  for (let i = 0; i < 32; i++) send(String(i));
  expect(useSessionStore.getState().streamingItems.s[key]).toBeUndefined();
  send("original");
  const rebuilt = useSessionStore.getState().streamingItems.s[key];
  expect(rebuilt).toEqual(original);
  expect(rebuilt).not.toBe(original);
  response.resolve(json({ id: "s", messages: [{ role: "user", type: "text", seq: 5, content: "Saved question" }],
    next_message_seq: 6, oldest_loaded_seq: 5, dispatch_state: dispatchState, active_attempt: attempt }));
  await read;
  const merged = useSessionStore.getState().streamingItems.s;
  expect(merged[key]).toBe(rebuilt);
  expect(Object.keys(merged)).toHaveLength(32);
  expect(merged[nativeItemKey(metadata("0"))!]).toBeUndefined();
});

it.each(["claude-code", "codex"].flatMap((backend) => [false, true].map((newAttempt) => ({ backend, newAttempt }))))(
  "an accepted terminal clears only its own $backend buffer (new attempt=$newAttempt)", ({ backend, newAttempt }) => {
    useSessionStore.getState().applyDispatchState("s", { ...dispatch("stream-service"), active_request_id: "request", active_operation: "message" });
    handleWsMessage({ type: "attempt_started", session_id: "s", request_id: "request", attempt_id: "old" });
    const start = useSessionStore.getState();
    const streamingBasis = captureStreamingRead("s");
    if (newAttempt) handleWsMessage({ type: "attempt_started", session_id: "s", request_id: "request", attempt_id: "new" });
    const identity = { request_id: "request", attempt_id: newAttempt ? "new" : "old",
      ...(backend === "codex" ? { native: { thread_id: "root", root_thread_id: "root", turn_id: "turn", root_turn_id: "turn", item_id: "item" } } : {}),
    };
    handleWsMessage({ type: "assistant_delta", session_id: "s", content: "Interrupted partial", runtime_metadata: identity });
    const content = { kind: "outcome", operation: "message", request_id: "request", status: "interrupted" };
    const terminal = { role: "system", type: "result", seq: 11, content,
      tool_input: { attempt_context: { request_id: "request", attempt_id: "old" } } };
    expect(applySessionSnapshot("s", { id: "s", messages: [terminal], next_message_seq: 12, oldest_loaded_seq: 11,
      dispatch_state: { ...dispatch("stream-service"), revision: 1 }, active_attempt: null }, start, false, streamingBasis)).not.toBeNull();
    handleWsMessage({ type: "result", session_id: "s", seq: 11, content, request_id: "request", attempt_id: "old" });
    handleWsMessage({ type: "assistant_delta", session_id: "s", content: " stale", runtime_metadata: { ...identity, attempt_id: "old" } });
    const state = useSessionStore.getState();
    const partial = backend === "codex" ? Object.values(state.streamingItems.s || {}).map((item) => item.content)
      : state.streamingText.s ? [state.streamingText.s.content] : [];
    expect(partial).toEqual(newAttempt ? ["Interrupted partial"] : []);
    expect(state.activeAttempts.s).toEqual(newAttempt ? { request_id: "request", attempt_id: "new" } : undefined);
    expect(state.messages.s.map((row) => row.seq)).toEqual([11]);
    expect(state.lastAppliedSeq.s).toBe(11);
  }
);

const backgroundTask = (id: string, fields: Partial<BgTask> = {}) => ({ id, session_id: "s", status: "completed", command: "echo", description: null, started_at: "start", completed_at: "finish",
  output_preview: "", has_output: false, preview_truncated: false, delivery_disposition: "unknown", delivery_error: "Old admission error", ...fields } as BgTask);

it.each([200, 503])("another task's newer HTTP %s cannot prevent this detail from clearing delivery diagnostics", async (status) => {
  useSessionStore.getState().setBgTasks("s", [backgroundTask("a"), backgroundTask("b")]);
  const first = deferred<Response>(), second = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise));
  const a = observeSessionAuxiliary("s", "background", "a").done;
  const b = observeSessionAuxiliary("s", "background", "b").done.catch(() => {});
  second.resolve(status === 200 ? json(backgroundTask("b")) : new Response("Unavailable", { status }));
  await b;
  first.resolve(json(backgroundTask("a", { output_preview: "Complete original output", delivery_disposition: null, delivery_error: null })));
  await a;
  expect(useSessionStore.getState().bgTasks.s.find((row) => row.id === "a")).toMatchObject({
    output_preview: "Complete original output", delivery_disposition: null, delivery_error: null,
  });
  expect(useSessionStore.getState().bgTaskReadFailures.s).toEqual(status === 503 ? ["b"] : undefined);
});

it.each([false, true])("a list fills final output across an execution event while respecting delivery events (%s)", async (delivery) => {
  useSessionStore.getState().setBgTasks("s", [backgroundTask("a", { status: "running", output_preview: "short" })]);
  const reply = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn(() => reply.promise));
  const reading = loadBgTaskSnapshot("s");
  handleWsMessage({ type: "bg_completed", session_id: "s", task_id: "a", status: "completed", exit_code: 0, completed_at: "now" });
  if (delivery) handleWsMessage({ type: "bg_delivery", session_id: "s", task_id: "a", delivery_disposition: "unknown", delivery_error: "New live error" });
  reply.resolve(json([backgroundTask("a", { output_preview: "Complete original output", delivery_disposition: null, delivery_error: null })]));
  await reading;
  expect(useSessionStore.getState().bgTasks.s[0]).toMatchObject({ status: "completed", output_preview: "Complete original output",
    delivery_error: delivery ? "New live error" : null, delivery_disposition: delivery ? "unknown" : null });
});

it.each([true, false])("a finished newer detail or list retains per-task authority until older overlapping reads finish (%s)", async (listFirst) => {
  const old = deferred<Response>(), fresh = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise));
  const older = observeSessionAuxiliary("s", "background", listFirst ? undefined : "a").done;
  const newer = observeSessionAuxiliary("s", "background", listFirst ? "a" : undefined).done;
  const current = backgroundTask("a", { output_preview: "New output", delivery_error: null, delivery_disposition: null });
  fresh.resolve(json(listFirst ? current : [current]));
  await newer;
  const previous = backgroundTask("a", { output_preview: "Old output with more bytes" });
  old.resolve(json(listFirst ? [previous] : previous));
  await older;
  expect(useSessionStore.getState().bgTasks.s[0]).toMatchObject({ output_preview: "New output", delivery_error: null, delivery_disposition: null });
});

it.each([true, false])("same-task successes use request order for the preview (newer returns first=%s)", async (newerFirst) => {
  useSessionStore.getState().setBgTasks("s", [backgroundTask("a")]);
  const old = deferred<Response>(), fresh = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise));
  const older = observeSessionAuxiliary("s", "background", "a").done;
  const newer = observeSessionAuxiliary("s", "background", "a").done;
  const oldData = backgroundTask("a", { output_preview: "Complete original output", delivery_error: "Older diagnosis" });
  const newData = backgroundTask("a", { output_preview: "Complete", delivery_error: "New diagnosis" });
  if (newerFirst) { fresh.resolve(json(newData)); await newer; old.resolve(json(oldData)); await older; }
  else { old.resolve(json(oldData)); await older; fresh.resolve(json(newData)); await newer; }
  expect(useSessionStore.getState().bgTasks.s[0]).toMatchObject({ output_preview: "Complete", delivery_error: "New diagnosis" });
});

it("a newer same-task failure owns only the error surface, leaving older successful data useful", async () => {
  useSessionStore.getState().setBgTasks("s", [backgroundTask("a")]);
  const old = deferred<Response>(), fresh = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise));
  const older = observeSessionAuxiliary("s", "background", "a").done;
  const newer = observeSessionAuxiliary("s", "background", "a").done.catch(() => {});
  fresh.resolve(new Response("Unavailable", { status: 503 })); await newer;
  old.resolve(json(backgroundTask("a", { output_preview: "Saved output", delivery_error: null, delivery_disposition: null }))); await older;
  expect(useSessionStore.getState().bgTasks.s[0]).toMatchObject({ output_preview: "Saved output", delivery_error: null });
  expect(useSessionStore.getState().bgTaskReadFailures.s).toEqual(["a"]);
});

it.each([false, true])("a newer list only supersedes detail errors for tasks it returns (covered=%s)", async (covered) => {
  const task = backgroundTask("failed-read");
  const other = backgroundTask("other");
  useSessionStore.getState().setBgTasks("s", [task, other]);
  const detail = deferred<Response>();
  const retry = deferred<Response>();
  let lists = 0, exact = 0;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (url.endsWith(task.id)) return ++exact === 1 ? detail.promise : retry.promise;
    lists++;
    return Promise.resolve(json(lists === 1 && covered || lists === 3 ? [task, other] : [other]));
  }));
  const older = observeSessionAuxiliary("s", "background", task.id).done.catch(() => {});
  await loadBgTaskSnapshot("s");
  detail.resolve(new Response(null, { status: 503 }));
  await older;
  expect(useSessionStore.getState().bgTaskReadFailures.s).toEqual(covered ? undefined : [task.id]);
  await loadBgTaskSnapshot("s");
  expect(useSessionStore.getState().bgTaskReadFailures.s).toEqual(covered ? undefined : [task.id]);
  await loadBgTaskSnapshot("s");
  expect(useSessionStore.getState().bgTaskReadFailures.s).toBeUndefined();
  retry.resolve(json(task));
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
  const bg = backgroundTask("bg", { status: "running" });
  useSessionStore.getState().setBgTasks("s", [bg]);
  const response = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const loaded = loadBgTaskSnapshot("s");
  useSessionStore.getState().upsertBgTask("s", { ...bg, status: "completed", output_preview: "live output" });
  response.resolve(json([bg]));
  await loaded;
  expect(useSessionStore.getState().bgTasks.s[0].output_preview).toBe("live output");
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
    if (url.endsWith("bg-task-summaries")) return Promise.resolve(json([backgroundTask("bg", { output_preview: "Saved output" })]));
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
    await vi.waitFor(() => expect(useSessionStore.getState().bgTasks.s?.[0].output_preview).toBe("Saved output"));
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
  vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("bg-task-summaries") ? Promise.resolve(json([])) : response.promise));
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
  vi.stubGlobal("fetch", vi.fn(async (url: string) => json(url.endsWith("bg-task-summaries") ? [] : {
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
  const fetcher = vi.fn((url: string) => url.endsWith("bg-task-summaries") ? background.promise : detail.promise);
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
  vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("bg-task-summaries") ? background.promise : detail.promise));
  const loaded = loadSessionSnapshots("archived");
  detail.resolve(json({ id: "archived", archived: true, messages: [], dispatch_state: null }));
  background.reject(new Error("offline"));
  await loaded;
  await vi.waitFor(() => expect(useSessionStore.getState().sessionAuxiliaryErrors.archived?.background).toBeTruthy());
  expect(useSessionStore.getState().messages.archived).toEqual([]);
  expect(useSessionStore.getState().dispatchUnavailable.archived).toBeUndefined();

  vi.stubGlobal("fetch", vi.fn(async (url: string) => json(url.endsWith("bg-task-summaries") ? [] : { id: "archived", archived: true, messages: [], dispatch_state: null })));
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
  background.resolve(json([backgroundTask("retired", { status: "running" })]));
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
    url.endsWith("/bg-task-summaries") ? [] : { id: "s", dispatch_state: dispatch("service"), messages: [] },
  ))));
  await selectSession("s");
  expect(useSessionStore.getState().messages.s).toEqual([]);
  research.resolve(json([]));
  await Promise.resolve();
});

it("holds early auxiliary data until the detail accepts its service", async () => {
  const detail = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("/bg-task-summaries") ? Promise.resolve(json([backgroundTask("bg", { status: "running" })])) :
    url.endsWith("/research") ? Promise.resolve(json([])) : detail.promise));
  const loaded = loadSessionSnapshots("s");
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(useSessionStore.getState().bgTasks.s).toBeUndefined();
  detail.resolve(json({ id: "s", messages: [], dispatch_state: dispatch("service") }));
  await loaded;
  await vi.waitFor(() => expect(useSessionStore.getState().bgTasks.s?.[0].id).toBe("bg"));
});

it("does not publish auxiliary data when authoritative detail fails", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith("/bg-task-summaries") ? json([backgroundTask("bg", { status: "running" })]) :
    new Response(null, { status: 503 })));
  await expect(loadSessionSnapshots("s")).rejects.toThrow("HTTP 503");
  expect(useSessionStore.getState().bgTasks.s).toBeUndefined();
  expect(useSessionStore.getState().dispatchUnavailable.s).toBe(true);
  expect(useSessionStore.getState().sessionAuxiliaryErrors.s).toBeUndefined();
});

it("routes a selected archived detail to read-only history and its owning agent", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => json(url.endsWith("bg-task-summaries") ? [] : {
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

const auxiliarySuffixes = ["/bg-task-summaries", "/research", "/delegations"] as const;
const suffixes = ["", ...auxiliarySuffixes] as const;
const pathFor = (id: string, suffix: string) => `/api/sessions/${id}${suffix}`;
const selectionInfo = (id: string) => ({ id, agent_id: `agent-${id}`, name: id, status: "idle", archived: false } as SessionInfo);
const selectionDetail = (id: string, generation = "service", content = `Cached ${id.toUpperCase()}`) => ({
  ...selectionInfo(id), messages: [{ type: "text", role: "user", seq: 0, content }], dispatch_state: dispatch(generation),
});
const selectionRows = (id: string, prefix: string) => ({
  "/bg-task-summaries": [backgroundTask(`${prefix}-bg`, { session_id: id })],
  "/research": [{ id: `${prefix}-research`, session_id: id, status: "completed", question: "Research complete", phase: null }],
  "/delegations": [{ delegation_id: `${prefix}-child`, work_id: `${prefix}-work`, state: "completed", parent_session_id: id }],
});

function expectAuxiliaryRows(id: string, prefix: string) {
  const state = useSessionStore.getState();
  expect(state.bgTasks[id]?.map((row) => row.id)).toEqual([`${prefix}-bg`]);
  expect(state.research[id]?.map((row) => row.id)).toEqual([`${prefix}-research`]);
  expect(state.delegations[id]?.map((row) => row.delegation_id)).toEqual([`${prefix}-child`]);
}

it("shares detail, BG, Research and delegation GETs through A to B to A selection", async () => {
  const known = [selectionInfo("a"), selectionInfo("b")];
  useSessionStore.setState({ dispatchGeneration: "service", sessions: known });
  const aResponses = new Map(suffixes.map((suffix) => [pathFor("a", suffix), deferred<unknown>()]));
  const get = vi.fn(async (url: string) => {
    const path = new URL(url).pathname;
    if (path === "/api/sessions") return json(known);
    const held = aResponses.get(path);
    return json(held ? await held.promise : path === pathFor("b", "") ? selectionDetail("b") : []);
  });
  vi.stubGlobal("fetch", get);
  const first = selectSession("a");
  await selectSession("b");
  expect(useSessionStore.getState().messages.b[0].content).toBe("Cached B");
  const returned = selectSession("a");
  const paths = () => get.mock.calls.map(([url]) => new URL(url).pathname).filter((path) => path !== "/api/sessions");
  try {
    for (const id of ["a", "b"]) for (const suffix of suffixes) {
      expect(paths().filter((path) => path === pathFor(id, suffix)), pathFor(id, suffix)).toHaveLength(1);
    }
  } finally {
    const rows = selectionRows("a", "shared");
    for (const suffix of suffixes) aResponses.get(pathFor("a", suffix))!.resolve(suffix ? rows[suffix] : selectionDetail("a"));
    await Promise.all([first, returned]);
  }
  await vi.waitFor(() => expectAuxiliaryRows("a", "shared"));
  expect(paths().sort()).toEqual(["a", "b"].flatMap((id) => suffixes.map((suffix) => pathFor(id, suffix))).sort());
  expect(useSessionStore.getState().activeSessionId).toBe("a");
  expect(useSessionStore.getState().activeAgentId).toBe("agent-a");
  expect(useSessionStore.getState().messages.a[0].content).toBe("Cached A");
});

function holdSelectionReads() {
  const received = new Map<string, Array<{ response: ReturnType<typeof deferred<Response>>; signal: AbortSignal }>>();
  vi.stubGlobal("fetch", vi.fn((url: string, init: RequestInit) => {
    const path = new URL(url).pathname;
    if (path === "/api/sessions") return Promise.resolve(json([selectionInfo("s")]));
    if (path !== pathFor("s", "") && !path.startsWith(pathFor("s", "/"))) return Promise.resolve(json([]));
    const reads = received.get(path) ?? [];
    const response = deferred<Response>();
    reads.push({ response, signal: init.signal! });
    received.set(path, reads);
    return response.promise;
  }));
  return {
    count: (suffix = "") => received.get(pathFor("s", suffix))?.length ?? 0,
    signal: (suffix: string, index: number) => received.get(pathFor("s", suffix))![index].signal,
    detail: (index: number, generation = "service", content?: string) =>
      received.get(pathFor("s", ""))![index].response.resolve(json(selectionDetail("s", generation, content))),
    auxiliary: (index: number, prefix: string) => {
      const rows = selectionRows("s", prefix);
      for (const suffix of auxiliarySuffixes) {
        received.get(pathFor("s", suffix))![index].response.resolve(json(rows[suffix]));
      }
    },
  };
}

it("keeps the shared requests when the first consumer cancels before the first service is confirmed", async () => {
  const http = holdSelectionReads();
  const first = observeSession("s"), next = observeSession("s");
  first.cancel();
  expect(await first.done).toBeNull();
  for (const suffix of suffixes) {
    expect(http.count(suffix)).toBe(1);
    expect(http.signal(suffix, 0).aborted).toBe(false);
  }
  http.auxiliary(0, "shared");
  http.detail(0);
  expect(await next.done).not.toBeNull();
  await vi.waitFor(() => expectAuxiliaryRows("s", "shared"));
});

it("retains the issued execution baseline for a late joiner and keeps its fresh confirmation alive", async () => {
  const initial = { ...dispatch("service"), active_request_id: "r", active_operation: "message" as const };
  useSessionStore.getState().applyDispatchState("s", initial);
  const old = deferred<Response>(), fresh = deferred<Response>();
  const signals: AbortSignal[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init: RequestInit) => {
    if (new URL(url).pathname !== pathFor("s", "")) return Promise.resolve(json([]));
    signals.push(init.signal!);
    return signals.length === 1 ? old.promise : fresh.promise;
  }));
  const first = observeSession("s");
  handleWsMessage({ type: "attempt_started", session_id: "s", request_id: "r", attempt_id: "live" });
  const next = observeSession("s");
  try {
    expect(signals).toHaveLength(1);
    old.resolve(json({ ...selectionDetail("s"), dispatch_state: initial, active_attempt: null }));
    await Promise.all([first.done, next.done]);
    expect(useSessionStore.getState().activeAttempts.s.attempt_id).toBe("live");
    expect(signals).toHaveLength(2);
    first.cancel();
    expect(signals[1].aborted).toBe(false);
    fresh.resolve(json({ ...selectionDetail("s"), dispatch_state: initial,
      active_attempt: { request_id: "r", attempt_id: "confirmed" } }));
    await vi.waitFor(() => expect(useSessionStore.getState().activeAttempts.s.attempt_id).toBe("confirmed"));
    expect(signals).toHaveLength(2);
  } finally { first.cancel(); next.cancel(); }
});

it("does not qualify an old auxiliary response using a later detail from another service", async () => {
  useSessionStore.setState({ dispatchGeneration: "old" });
  const http = holdSelectionReads();
  const first = observeSession("s");
  http.detail(0, "old");
  expect(await first.done).not.toBeNull();
  const next = observeSession("s");
  expect(http.count()).toBe(2);
  for (const suffix of auxiliarySuffixes) expect(http.count(suffix)).toBe(1);
  http.detail(1, "new");
  expect(await next.done).not.toBeNull();
  await vi.waitFor(() => {
    for (const suffix of auxiliarySuffixes) expect(http.count(suffix)).toBe(2);
  });
  http.auxiliary(1, "current");
  await vi.waitFor(() => expectAuxiliaryRows("s", "current"));
  http.auxiliary(0, "retired");
  await new Promise((resolve) => setTimeout(resolve, 0));
  expectAuxiliaryRows("s", "current");
  expect(http.count()).toBe(2);
  for (const suffix of auxiliarySuffixes) expect(http.count(suffix)).toBe(2);
});

it.each([false, true])("releases a cancelled delegation replacement synchronously (card still observing=%s)", async (keepCard) => {
  useSessionStore.setState({ dispatchGeneration: "old" });
  const http = holdSelectionReads();
  const first = observeSession("s");
  http.detail(0, "old");
  await first.done;
  const next = observeSession("s");
  http.detail(1, "new");
  await next.done;
  await vi.waitFor(() => expect(http.count("/delegations")).toBe(2));
  const card = keepCard ? watchDelegation("s", { delegation_id: "current-child", work_id: "current-work" }, () => {}) : undefined;
  try {
    expect(http.count("/delegations")).toBe(2);
    http.auxiliary(1, "current");
    next.cancel();
    expect(http.signal("/delegations", 1).aborted).toBe(!keepCard);
    http.auxiliary(0, "retired");
    await new Promise((resolve) => setTimeout(resolve, 0));
    const state = useSessionStore.getState();
    expect(state.delegations.s?.map((row) => row.delegation_id)).toEqual(keepCard ? ["current-child"] : undefined);
    expect(state.bgTasks.s).toBeUndefined();
    expect(state.research.s).toBeUndefined();
  } finally { card?.cancel(); first.cancel(); next.cancel(); }
});

it("retires cold-start auxiliary responses after the first accepted service is replaced", async () => {
  const http = holdSelectionReads();
  const opening = observeSession("s");
  try {
    handleWsMessage({ type: "dispatch_state", session_id: "other", dispatch_state: dispatch("first") });
    http.auxiliary(0, "first");
    await new Promise((resolve) => setTimeout(resolve, 0));
    handleWsMessage({ type: "dispatch_state", session_id: "other", dispatch_state: dispatch("current") });
    http.detail(0, "current");
    await opening.done;
    await new Promise((resolve) => setTimeout(resolve, 0));
    const state = useSessionStore.getState();
    expect(state.dispatchGeneration).toBe("current");
    expect(state.bgTasks.s).toBeUndefined();
    expect(state.research.s).toBeUndefined();
    expect(state.delegations.s).toBeUndefined();
    for (const suffix of auxiliarySuffixes) expect(http.count(suffix)).toBe(1);
  } finally { opening.cancel(); }
});

it.each(["account", "connection", "service", "archive"])("uses new GETs when the original %s changes", async (boundary) => {
  useSessionStore.setState({ dispatchGeneration: "service" });
  const http = holdSelectionReads();
  const first = observeSession("s");
  const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
  const generation = boundary === "service" ? "new" : "service";
  if (boundary === "account") {
    useSessionStore.getState().setToken("other-account");
    useSessionStore.getState().setToken("test-bearer");
    useSessionStore.setState({ dispatchGeneration: "service" });
  }
  if (boundary === "connection") sessions.connect(source);
  if (boundary === "service") useSessionStore.getState().applyDispatchState("other", dispatch(generation));
  if (boundary === "archive") sessions.archive("s");
  const next = observeSession("s");
  const retired = boundary === "account" ? expect(first.done).rejects.toThrow("previous sign-in") : expect(first.done).resolves.toBeNull();
  try {
    for (const suffix of suffixes) expect(http.count(suffix)).toBe(2);
    http.detail(1, generation, "Current detail"); http.auxiliary(1, "current");
    http.detail(0, "service", "Retired detail"); http.auxiliary(0, "retired");
    await retired;
    expect(await next.done).not.toBeNull();
    await vi.waitFor(() => expectAuxiliaryRows("s", "current"));
    expect(useSessionStore.getState().messages.s[0].content).toBe("Current detail");
  } finally {
    first.cancel(); next.cancel();
    if (boundary === "connection") sessions.disconnect(source);
  }
});

it("uses fresh GETs for a post-write observation and keeps ordinary navigation separate", async () => {
  useSessionStore.setState({ dispatchGeneration: "service" });
  const http = holdSelectionReads();
  const beforeWrite = observeSession("s");
  const afterWrite = observeSession("s", "complete", { fresh: true });
  const ordinary = observeSession("s");
  try {
    for (const suffix of suffixes) expect(http.count(suffix)).toBe(2);
    http.detail(0, "service", "Pre-write detail"); http.auxiliary(0, "before");
    expect(await beforeWrite.done).toEqual(expect.objectContaining({ messages: [expect.objectContaining({ content: "Pre-write detail" })] }));
    expect(await ordinary.done).not.toBeNull();
    const finished = vi.fn();
    void afterWrite.done.then(finished);
    await vi.waitFor(() => expectAuxiliaryRows("s", "before"));
    expect(finished).not.toHaveBeenCalled();
    http.detail(1, "service", "Post-write detail"); http.auxiliary(1, "after");
    expect(await afterWrite.done).toEqual(expect.objectContaining({ messages: [expect.objectContaining({ content: "Post-write detail" })] }));
    await vi.waitFor(() => {
      const state = useSessionStore.getState();
      expect(state.bgTasks.s?.[0].id).toBe("after-bg");
      expect(state.research.s?.[0].id).toBe("after-research");
      expect(state.delegations.s?.[0].delegation_id).toBe("after-child");
    });
  } finally { beforeWrite.cancel(); afterWrite.cancel(); ordinary.cancel(); }
});

it("keeps the original sidebar baseline when metadata is joined after a settings write", async () => {
  const old = { ...selectionInfo("s"), model: "old", configuration_version: 0 };
  const updated = { ...old, model: "new", configuration_version: 1 };
  useSessionStore.setState({ dispatchGeneration: "service", sessions: [old] });
  const metadata = deferred<Response>();
  const get = vi.fn((url: string) => {
    const path = new URL(url).pathname;
    if (path === pathFor("s", "/info")) return metadata.promise;
    return Promise.resolve(json(path === "/api/sessions" ? [updated] : { ...selectionDetail("s"), model: "new" }));
  });
  vi.stubGlobal("fetch", get);
  const first = observeSession("s", "metadata");
  expect(sessions.settingsSaved(updated, sessions.capture())).toBe(true);
  const next = observeSession("s", "metadata");
  first.cancel();
  const detail = observeSession("s", "detail");
  metadata.resolve(json(old));
  expect(await first.done).toBeNull();
  expect(await next.done).toBeNull();
  expect(await detail.done).toEqual(expect.objectContaining({ model: "new" }));
  expect(get.mock.calls.filter(([url]) => url.endsWith("/info"))).toHaveLength(1);
  expect(useSessionStore.getState().sessions[0].model).toBe("new");
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
