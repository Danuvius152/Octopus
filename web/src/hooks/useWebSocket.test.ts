import { Socket } from "../test-support/socket";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { handleWsMessage, shouldApplyWsEvent, useWebSocket } from "./useWebSocket";
import { useSessionStore, type Agent, type Application, type SessionInfo } from "../stores/sessionStore";

it("merges an unseen delayed steer before a higher sequence without repeating either row", () => {
  useSessionStore.setState({ messages: {}, lastAppliedSeq: {}, streamingText: {}, streamingItems: {} });
  const output = { type: "assistant_text", session_id: "late-steer", seq: 4, content: "Output while stdin is blocked" };
  const steer = { type: "steered", session_id: "late-steer", seq: 3, content: "Saved correction",
    tool_input: { request_context: { request_id: "inline", source: "human" } } };
  handleWsMessage(output);
  handleWsMessage(steer);
  handleWsMessage(output);
  handleWsMessage(steer);
  const rows = useSessionStore.getState().messages["late-steer"];
  expect(rows.map((row) => [row.seq, row.content])).toEqual([
    [3, "Saved correction"], [4, "Output while stdin is blocked"],
  ]);
  expect(rows[0].tool_input).toEqual(steer.tool_input);
});

it("retains local command and notice order when the next persisted message arrives", () => {
  useSessionStore.setState({ messages: {}, lastAppliedSeq: {} });
  handleWsMessage({ type: "user_message", session_id: "mixed", seq: 0, content: "First" });
  useSessionStore.getState().addMessage("mixed", { role: "user", type: "text", content: "/research local command" });
  useSessionStore.getState().addMessage("mixed", { role: "system", type: "notice", content: "Research started" });
  handleWsMessage({ type: "assistant_text", session_id: "mixed", seq: 1, content: "Following output" });
  expect(useSessionStore.getState().messages.mixed.map((row) => row.content)).toEqual([
    "First", "/research local command", "Research started", "Following output",
  ]);
});

it.each(["claude-code", "codex"])("keeps the current %s stream when old text and result arrive late", (backend) => {
  useSessionStore.setState({ messages: {}, lastAppliedSeq: {}, streamingText: {}, streamingItems: {}, activeAttempts: {} });
  const identity = (request_id: string, attempt_id = "attempt") => ({ request_id, attempt_id,
    ...(backend === "codex" ? { native: { thread_id: "root", root_thread_id: "root", turn_id: "turn",
      root_turn_id: "turn", item_id: "reused-item" } } : {}),
  });
  const source = (request: string) => backend === "claude-code" ? identity(request) : { runtime_metadata: identity(request) };
  handleWsMessage({ type: "attempt_started", session_id: "streams", request_id: "current", attempt_id: "attempt" });
  handleWsMessage({ type: "assistant_delta", session_id: "streams", content: "Current partial", ...source("current") });
  handleWsMessage({ type: "assistant_text", session_id: "streams", seq: 3, content: "Old complete", ...source("old") });
  const partial = () => backend === "codex"
    ? Object.values(useSessionStore.getState().streamingItems.streams || {}).map((item) => item.content)
    : [useSessionStore.getState().streamingText.streams];
  expect(partial()).toEqual(["Current partial"]);
  const terminal = { type: "result", session_id: "streams", seq: 4, request_id: "old", attempt_id: "attempt",
    content: { kind: "outcome", operation: "message", request_id: "old", status: "success" } };
  handleWsMessage(terminal);
  handleWsMessage(terminal);
  expect(partial()).toEqual(["Current partial"]);
  expect(useSessionStore.getState().activeAttempts.streams).toEqual({ request_id: "current", attempt_id: "attempt" });
  expect(useSessionStore.getState().messages.streams.map((row) => row.seq)).toEqual([3, 4]);
  handleWsMessage({ ...terminal, seq: 5, request_id: "current", content: { ...terminal.content, request_id: "current" } });
  expect(useSessionStore.getState().activeAttempts.streams).toBeUndefined();
  expect(useSessionStore.getState().streamingText.streams).toBeUndefined();
  expect(Object.values(useSessionStore.getState().streamingItems.streams || {})).toEqual([]);
});

it.each(["question_answer", "question_closed"])("retains %s resolution facts from the original notification", (type) => {
  useSessionStore.setState({ messages: {}, pendingQuestions: {}, sessionMembership: {}, sessions: [] });
  const toolInput = { reason: "cancelled", question_resolution: { version: 1, question_id: "form", disposition: type === "question_answer" ? "answered" : "closed",
    source: { kind: "account", user_id: "owner" }, answers: [{ question_index: 0, text: "choice", selected: [] }] } };
  handleWsMessage({ type, session_id: "resolution-session", question_id: "form", content: "choice", message: "closed", tool_input: toolInput });
  expect(useSessionStore.getState().messages["resolution-session"][0].tool_input).toEqual(toolInput);
});

it.each(["question_answer", "question_closed"])("retains the late request history after %s without reopening its form", (type) => {
  useSessionStore.setState({ messages: {}, lastAppliedSeq: {}, pendingQuestions: {}, closedQuestionIds: {} });
  handleWsMessage({ type, session_id: "late-question", question_id: "form", seq: 1, content: "A", message: "Closed" });
  const request = { type: "question_request", session_id: "late-question", question_id: "form", seq: 0,
    request_id: "request", attempt_id: "attempt", questions: [{ question: "Choose" }] };
  handleWsMessage(request);
  handleWsMessage(request);
  const state = useSessionStore.getState();
  expect(state.messages["late-question"].map((row) => [row.seq, row.type])).toEqual([[0, "question_request"], [1, type]]);
  expect(state.pendingQuestions["late-question"] || []).toEqual([]);
  expect(state.closedQuestionIds["late-question"].form).toBe(true);
});

it("restores the original session and rejects a delayed archive projection", () => {
  const original: SessionInfo = { id: "woken-original", name: "Original", archived: false,
    working_dir: "/work", status: "idle", created_at: "now", message_count: 0, transcript_integrity: "ok",
    origin: "user", backend: "codex", can_steer: true, can_fork: true, fork_is_full_copy: false,
    can_observe_context: true, can_compact: false };
  useSessionStore.setState({ sessions: [], archivedSessions: [original], activeSessionId: "another-session", sessionMembership: {} });
  const dispatch = { generation: "wake-service", revision: 12, paused: true, active_request_id: null, active_operation: null, items: [] };
  handleWsMessage({ type: "session_unarchived", session_id: original.id, session: original, dispatch_state: dispatch });
  expect(useSessionStore.getState().sessions).toEqual([original]);
  expect(useSessionStore.getState().archivedSessions).toEqual([]);
  expect(useSessionStore.getState().activeSessionId).toBe("another-session");
  handleWsMessage({ type: "session_archived", old_session_id: original.id, new_session_id: null,
    dispatch_state: { ...dispatch, revision: 11 } });
  expect(useSessionStore.getState().sessions).toEqual([original]);
  handleWsMessage({ type: "session_archived", old_session_id: original.id, new_session_id: null,
    dispatch_state: { ...dispatch, revision: 13 } });
  expect(useSessionStore.getState().sessions).toEqual([]);
  handleWsMessage({ type: "session_unarchived", session_id: original.id, session: original, dispatch_state: dispatch });
  expect(useSessionStore.getState().sessions).toEqual([]);
});

it("tracks report completion separately from follow-up delivery", () => {
  useSessionStore.setState({ research: {} });
  handleWsMessage({ type: "research_started", session_id: "research-owner", research_id: "report", question: "Compare X" });
  handleWsMessage({ type: "research_completed", session_id: "research-owner", research_id: "report", verified: 2, sources: ["source"] });
  expect(useSessionStore.getState().research["research-owner"][0]).toMatchObject({
    status: "completed", injection_status: "pending", verified: 2,
  });
  handleWsMessage({ type: "research_delivery", session_id: "research-owner", research_id: "report",
    injection_status: "failed", error: "Original work is closed" });
  expect(useSessionStore.getState().research["research-owner"][0]).toMatchObject({
    status: "completed", injection_status: "failed", error: "Original work is closed", verified: 2,
  });
});

it.each(["native", "without-randomUUID", "without-crypto"])("handles input admission in %s browsers", async (mode) => {
  if (mode === "without-randomUUID") vi.stubGlobal("crypto", { getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto) });
  if (mode === "without-crypto") vi.stubGlobal("crypto", undefined);
  Socket.reset(1);
  vi.stubGlobal("WebSocket", Socket);
  useSessionStore.getState().setToken("submission-token");
  const hook = renderHook(useWebSocket);
  try {
    const socket = Socket.instances[0];
    const pending = hook.result.current.sendMessage("own-session", "retained input");
    if (mode === "without-crypto") {
      expect(await pending).toMatchObject({ accepted: false, code: "secure_randomness_unavailable", message: expect.stringContaining("HTTPS or localhost") });
      expect(socket.send).not.toHaveBeenCalled();
      return;
    }
    const input = JSON.parse(socket.send.mock.calls[0][0]);
    const resolved = vi.fn();
    void pending.then(resolved);
    act(() => socket.onmessage?.({ data: JSON.stringify({ type: "submission", client_request_id: input.client_request_id,
      session_id: "other-session", accepted: true, request_id: "durable-request" }) }));
    await Promise.resolve();
    expect(resolved).not.toHaveBeenCalled();
    act(() => socket.onmessage?.({ data: JSON.stringify({ type: "submission", client_request_id: "other-request",
      session_id: "own-session", accepted: true, request_id: "durable-request" }) }));
    await Promise.resolve();
    expect(resolved).not.toHaveBeenCalled();
    act(() => socket.onmessage?.({ data: JSON.stringify({ type: "submission", client_request_id: input.client_request_id,
      session_id: "own-session", accepted: false, message: "Conversation closed" }) }));
    expect(await pending).toEqual({ accepted: false, clientRequestId: input.client_request_id, message: "Conversation closed" });
    expect(socket.send).toHaveBeenCalledTimes(1);
  } finally {
    hook.unmount();
    vi.unstubAllGlobals();
  }
});

describe("archive replacement settings", () => {
  const old = {
    id: "archive-old", name: "Work", working_dir: "/tmp", backend: "codex",
    model: "stale-model", reasoning_effort: "low", service_tier: "default",
  } as SessionInfo;
  const event = {
    type: "session_archived", old_session_id: old.id, new_session_id: "archive-new",
    name: "Work", model: "gpt-5.4", reasoning_effort: "high", service_tier: "fast",
  };

  beforeEach(() => {
    useSessionStore.setState({
      sessions: [old], archivedSessions: [], activeSessionId: old.id,
      messages: {}, dispatchUnavailable: {}, pendingQuestions: {},
    });
  });

  it.each([
    { model: "gpt-5.4", reasoning_effort: "high", service_tier: "fast" },
    { model: null, reasoning_effort: null, service_tier: null },
  ])("uses the server's settings, including explicit inheritance: %j", (settings) => {
    handleWsMessage({ ...event, ...settings });
    const store = useSessionStore.getState();
    expect(store.activeSessionId).toBe(old.id);
    expect(store.sessions).toEqual([expect.objectContaining({ id: event.new_session_id, ...settings })]);
    expect(store.archivedSessions).toEqual([expect.objectContaining({ ...old, archived: true })]);
  });

  it("receives settings even when the old session is absent locally", () => {
    useSessionStore.setState({ sessions: [] });
    handleWsMessage(event);
    expect(useSessionStore.getState().sessions[0]).toMatchObject({
      model: event.model, reasoning_effort: event.reasoning_effort, service_tier: event.service_tier,
    });
  });

  it("converges when the HTTP response precedes the event and ignores duplicate events", () => {
    const fresh = { ...old, id: event.new_session_id, model: event.model,
      reasoning_effort: "high", service_tier: "fast", archived: false } as SessionInfo;
    useSessionStore.setState({ sessions: [fresh], activeSessionId: fresh.id });
    handleWsMessage(event);
    handleWsMessage(event);
    expect(useSessionStore.getState().sessions).toEqual([fresh]);
    expect(useSessionStore.getState().activeSessionId).toBe(fresh.id);
  });

  it("updates an existing successor loaded before its settings were persisted", () => {
    useSessionStore.setState({ sessions: [old, { ...old, id: event.new_session_id, model: null }] });
    handleWsMessage(event);
    expect(useSessionStore.getState().sessions).toEqual([expect.objectContaining({
      id: event.new_session_id, model: event.model,
      reasoning_effort: event.reasoning_effort, service_tier: event.service_tier,
    })]);
  });
});

it("applies the nested server dispatch frame for an idle Stop and a remote Resume", () => {
  useSessionStore.setState({ dispatchStates: {}, dispatchGeneration: null, retiredDispatchGenerations: [], dispatchUnavailable: {} });
  const paused = { generation: "server-wire", revision: 1, paused: true, active_request_id: null, active_operation: null, items: [] };
  handleWsMessage({ type: "dispatch_state", session_id: "idle-stop", dispatch_state: paused });
  expect(useSessionStore.getState().dispatchStates["idle-stop"]).toEqual(paused);
  const resumed = { ...paused, revision: 2, paused: false };
  handleWsMessage({ type: "dispatch_state", session_id: "idle-stop", dispatch_state: resumed });
  expect(useSessionStore.getState().dispatchStates["idle-stop"]).toEqual(resumed);
});

it("preserves the replacement session's actual queue after an archive broadcast", async () => {
  useSessionStore.getState().setToken("archive-test");
  useSessionStore.setState({ activeSessionId: "archived-old", dispatchStates: {}, dispatchUnavailable: {},
    sessions: [{ id: "archived-old", name: "Archived" } as SessionInfo] });
  const dispatch = { generation: "archive-service", revision: 0, paused: false, active_request_id: null, active_operation: null, items: [] };
  useSessionStore.setState({ dispatchStates: { "archive-replacement": dispatch } });
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  handleWsMessage({ type: "session_archived", old_session_id: "archived-old", new_session_id: "archive-replacement", name: "Archived" });
  expect(useSessionStore.getState().dispatchStates["archive-replacement"]).toEqual(dispatch);
  expect(useSessionStore.getState().activeSessionId).toBe("archived-old");
  expect(fetcher).not.toHaveBeenCalled();
  vi.unstubAllGlobals();
});

it("ignores a superseded socket's close and open after a new bearer connects", () => {
  Socket.reset(0);
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("fetch", vi.fn(async () => new Response("[]")));
  useSessionStore.setState({ token: "first-bearer", activeSessionId: null, connected: false });
  const hook = renderHook(() => useWebSocket());
  const old = Socket.instances[0];
  act(() => useSessionStore.getState().setToken("second-bearer"));
  expect(Socket.instances).toHaveLength(2);
  act(() => old.onclose?.({ code: 4001 }));
  expect(useSessionStore.getState().token).toBe("second-bearer");
  act(() => old.onopen?.());
  expect(useSessionStore.getState().connected).toBe(false);
  hook.unmount();
  vi.unstubAllGlobals();
});

it("uses authoritative queue snapshots and rejects duplicate or older revisions", () => {
  useSessionStore.setState({ dispatchUnavailable: {} });
  const snapshot = {
    generation: "service-a", revision: 3, paused: true, active_request_id: null, active_operation: null,
    items: [{ request_id: "bg-1", operation: "message" as const, source: "bg", summary: "completed command", priority: false }],
  };
  handleWsMessage({ type: "queued", session_id: "dispatch-test", content: "legacy event text", dispatch_state: snapshot });
  expect(useSessionStore.getState().dispatchStates["dispatch-test"].items[0].summary).toBe("completed command");
  handleWsMessage({ type: "queued", session_id: "dispatch-test", content: "duplicate", dispatch_state: snapshot });
  handleWsMessage({ type: "dequeued", session_id: "dispatch-test", dispatch_state: { ...snapshot, revision: 2, paused: false, items: [] } });
  expect(useSessionStore.getState().dispatchStates["dispatch-test"].items[0].summary).toBe("completed command");
});

it("preserves the server's request source instead of treating marker text as identity", () => {
  useSessionStore.setState({ messages: {}, lastAppliedSeq: {} });
  const metadata = { request_context: { request_id: "human-1", source: "human", work_id: null } };
  handleWsMessage({ type: "user_message", session_id: "source-test", seq: 2,
    content: "[bg-task-result] text actually typed by a person", tool_input: metadata });
  expect(useSessionStore.getState().messages["source-test"][0].tool_input).toEqual(metadata);
});

it("shows an expired delivery notice without fabricating a model turn", () => {
  useSessionStore.setState({ messages: {}, lastAppliedSeq: {}, streamingText: {} });
  handleWsMessage({ type: "delivery_notice", session_id: "notice-test", content: "The child question already closed" });
  expect(useSessionStore.getState().messages["notice-test"]).toEqual([
    expect.objectContaining({ role: "system", type: "notice", content: "The child question already closed" }),
  ]);
});

it("keeps the same failed outcome through live delivery, reload and duplicate frames", () => {
  useSessionStore.setState({ messages: {}, lastAppliedSeq: {} });
  const outcome = {
    kind: "outcome", operation: "message", request_id: "request-1", status: "failed",
    message: "Selected model is at capacity", provider_code: "server_overloaded",
    recovery: { transient: 2, idle: 0, stale_session: 0, premature_exit: 0 },
  };
  const frame = { type: "result", session_id: "outcome-test", seq: 4, is_error: true, content: outcome };
  handleWsMessage(frame);
  const live = useSessionStore.getState().messages["outcome-test"][0];
  expect(live.is_error).toBe(true);
  expect(live.content).toBe("Selected model is at capacity");
  expect(live.outcome?.recovery.transient).toBe(2);
  useSessionStore.getState().setMessages("outcome-test", [
    { role: "system", type: "result", seq: 4, is_error: true, content: outcome },
  ]);
  expect(useSessionStore.getState().messages["outcome-test"][0]).toEqual(live);
  handleWsMessage(frame);
  expect(useSessionStore.getState().messages["outcome-test"]).toHaveLength(1);
});

it("preserves recovery progress through reconnect and paged history without ending streaming", () => {
  useSessionStore.setState({ messages: {}, lastAppliedSeq: {}, oldestSeq: {}, streamingText: {} });
  const recovery = {
    kind: "recovery", request_id: "request-1", phase: "retrying", code: "transient_retry",
    message: "Provider overloaded; retrying", attempt: 1, maximum: 2, delay: 1,
  };
  handleWsMessage({ type: "assistant_delta", session_id: "recovery-test", content: "unfinished" });
  handleWsMessage({ type: "recovery", session_id: "recovery-test", seq: 4, content: recovery });
  const live = useSessionStore.getState().messages["recovery-test"][0];
  expect(useSessionStore.getState().streamingText["recovery-test"]).toBe("unfinished");
  expect(live.recovery?.phase).toBe("retrying");
  useSessionStore.getState().setMessages("recovery-test", [
    { role: "assistant", type: "text", seq: 5, content: "continued" },
  ]);
  useSessionStore.getState().setMessageWindow("recovery-test", 5, true);
  useSessionStore.getState().prependMessages("recovery-test", [
    { role: "system", type: "recovery", seq: 4, content: recovery },
  ], 4, false, { beforeSeq: 5, window: useSessionStore.getState().messageWindows["recovery-test"] });
  expect(useSessionStore.getState().messages["recovery-test"][0]).toEqual(live);
  expect(useSessionStore.getState().messages["recovery-test"].some((m) => m.type === "result")).toBe(false);
});

/** Snapshot coverage is independent of individually observed live rows. */
describe("shouldApplyWsEvent", () => {
  it("applies events without seq (ephemeral status/queue updates)", () => {
    expect(shouldApplyWsEvent(undefined, 5)).toBe(true);
    expect(shouldApplyWsEvent(null, 5)).toBe(true);
  });

  it("applies events when no baseline is set yet (fresh session)", () => {
    expect(shouldApplyWsEvent(0, undefined)).toBe(true);
    expect(shouldApplyWsEvent(7, undefined)).toBe(true);
  });

  it("applies events with seq strictly greater than baseline", () => {
    expect(shouldApplyWsEvent(6, 5)).toBe(true);
    expect(shouldApplyWsEvent(100, 99)).toBe(true);
  });

  it("drops events with seq <= baseline (already in snapshot)", () => {
    expect(shouldApplyWsEvent(5, 5)).toBe(false);
    expect(shouldApplyWsEvent(0, 5)).toBe(false);
    expect(shouldApplyWsEvent(99, 100)).toBe(false);
  });

  it("treats baseline=0 distinctly from baseline=undefined", () => {
    // baseline=0 means "seq 0 is in the snapshot, but seq 1+ are not"
    expect(shouldApplyWsEvent(0, 0)).toBe(false);
    expect(shouldApplyWsEvent(1, 0)).toBe(true);
  });
});

/** An agent setting a schedule for itself (schedule-tool.md §6).
 *
 * The server says only that the list moved; the client refetches. Without
 * this, a schedule an agent creates mid-conversation is invisible until the
 * user reloads — which looks exactly like the tool having failed.
 */
describe("schedules_changed", () => {
  beforeEach(() => {
    useSessionStore.getState().setToken("tok");
    useSessionStore.setState({ schedules: [] });
  });

  it("refetches the schedule list into the store", async () => {
    const row = {
      id: "s1",
      agent_id: "a1",
      name: "Morning build check",
      prompt: "check the build",
      cron: "0 9 * * 1-5",
      interval_seconds: null,
      timezone: "America/Los_Angeles",
      recurrence_label: "Weekdays at 09:00",
      enabled: true,
      created_at: "2026-09-21T00:00:00Z",
    };
    const fetchMock = vi.fn(async (url: string) => {
      expect(url).toContain("/api/schedules");
      return { ok: true, json: async () => [row] };
    });
    vi.stubGlobal("fetch", fetchMock);

    handleWsMessage({ type: "schedules_changed" });
    await vi.waitFor(() =>
      expect(useSessionStore.getState().schedules).toHaveLength(1)
    );
    expect(useSessionStore.getState().schedules[0].name).toBe(
      "Morning build check"
    );
    expect(fetchMock).toHaveBeenCalledOnce();
    vi.unstubAllGlobals();
  });

  it("leaves the list alone when signed out", async () => {
    useSessionStore.getState().setToken("");
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    handleWsMessage({ type: "schedules_changed" });
    await Promise.resolve();
    expect(fetchMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});

/** Agent and application rows over the socket (sidebar-pins.md §5, §7).
 *
 * A pin made in one tab has to show in every other; an unpinned app whose
 * build fails where nobody can see it has to earn a place in the sidebar.
 */
describe("agent and application events", () => {
  const agentRow = (o: Partial<Agent> = {}): Agent => ({
    id: "ag2",
    name: "Vera",
    pinned: true,
    pin_order: 2,
    description: "", system_prompt: "", backend: "codex", mcp_servers: [], tool_allow: "", tool_deny: "",
    subagents: [], is_system: false, archived: false, created_at: "now", updated_at: "now", active_session_count: 0,
    ...o,
  });
  const appRow = (o: Partial<Application> = {}): Application => ({
    id: "a1",
    name: "Tracker",
    status: "building",
    pinned: false,
    description: "", app_dir: "/apps/tracker", entrypoint: "index.html", archived: false, created_at: "now", updated_at: "now",
    ...o,
  });

  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 404 }))
    );
    useSessionStore.setState({
      agents: [],
      applications: [],
      unseenFailedApplications: [],
      mainView: "chat",
      activeApplicationId: null,
    });
  });

  it("agent_created / agent_updated upsert the row", () => {
    handleWsMessage({ type: "agent_created", agent_id: "ag2", agent: agentRow() });
    handleWsMessage({
      type: "agent_updated",
      agent_id: "ag2",
      agent: agentRow({ pinned: false }),
    });
    const agents = useSessionStore.getState().agents;
    expect(agents).toHaveLength(1);
    expect(agents[0].pinned).toBe(false);
  });

  it("agent_archived moves the complete row and agent_deleted removes both partitions", () => {
    handleWsMessage({ type: "agent_created", agent_id: "ag2", agent: agentRow() });
    handleWsMessage({ type: "agent_archived", agent_id: "ag2", agent: agentRow({ archived: true }) });
    expect(useSessionStore.getState().agents).toEqual([]);
    expect(useSessionStore.getState().archivedAgents.map((row) => row.id)).toEqual(["ag2"]);
    handleWsMessage({ type: "agent_created", agent_id: "ag2", agent: agentRow() });
    expect(useSessionStore.getState().archivedAgents).toEqual([]);
    handleWsMessage({ type: "agent_deleted", agent_id: "ag2", agent: agentRow() });
    expect(useSessionStore.getState().agents).toEqual([]);
    expect(useSessionStore.getState().archivedAgents).toEqual([]);
  });

  it("application_archived removes the row", () => {
    handleWsMessage({ type: "application_created", application: appRow() });
    handleWsMessage({
      type: "application_archived",
      application_id: "a1",
      application: appRow({ archived: true }),
    });
    expect(useSessionStore.getState().applications).toEqual([]);
  });

  it("a build failing out of sight is remembered until opened", () => {
    handleWsMessage({ type: "application_created", application: appRow() });
    handleWsMessage({
      type: "application_updated",
      application: appRow({ status: "failed" }),
    });
    expect(useSessionStore.getState().unseenFailedApplications).toEqual(["a1"]);
    // Still failed on the next event: noted once, not twice.
    handleWsMessage({
      type: "application_updated",
      application: appRow({ status: "failed", name: "Renamed" }),
    });
    expect(useSessionStore.getState().unseenFailedApplications).toEqual(["a1"]);

    useSessionStore.getState().openApplication("a1");
    expect(useSessionStore.getState().unseenFailedApplications).toEqual([]);
  });

  it("a failure you're watching isn't 'unseen'", () => {
    handleWsMessage({ type: "application_created", application: appRow() });
    useSessionStore.setState({ mainView: "application", activeApplicationId: "a1" });
    handleWsMessage({
      type: "application_updated",
      application: appRow({ status: "failed" }),
    });
    expect(useSessionStore.getState().unseenFailedApplications).toEqual([]);
  });
});
