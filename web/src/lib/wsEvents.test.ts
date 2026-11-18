import { beforeEach, expect, it } from "vitest";
import { decodeWsEvent } from "./wsEvents";
import producers from "./wsProducerFixtures.json";
import recording from "./recordingReplay.json";
import { handleWsMessage } from "../hooks/useWebSocket";
import { sessions } from "./sessionList";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";

beforeEach(() => useSessionStore.setState(useSessionStore.getInitialState(), true));

const delegationEvent = { type: "delegation_delivery", session_id: "parent", delegation: {
  delegation_id: "child", work_id: "work", sub_session_id: "child", parent_session_id: "parent",
  target_agent_id: "agent", target_agent_name: "Helper", request: "Work", state: "running",
  created_at: "now", finished_at: null, error: null,
} };

it.each(["running", "completed", "failed", "cancelled", "interrupted"])("decodes the %s delegation state", (state) => {
  const event = { ...delegationEvent, delegation: { ...delegationEvent.delegation, state } };
  expect(decodeWsEvent(event).kind).toBe("event");
});

it.each(["invented", "toString", null, 3])("rejects an invalid WS delegation state %s", (state) => {
  const event = { ...delegationEvent, delegation: { ...delegationEvent.delegation, state } };
  expect(decodeWsEvent(event).kind).toBe("invalid");
});

it.each(["stash@{0}", "a".repeat(40)])("preserves recovery reference %s without resolving a moving selector", (stash_ref) => {
  const event = { ...producers.session_unarchived, session: { ...producers.session_unarchived.session,
    fork_revert_record: { ran: true, files: ["a.py"], stash_ref, status: "completed", refused_reason: null, error: null } } };
  expect(decodeWsEvent(event)).toMatchObject({ kind: "event", event: { session: { fork_revert_record: { stash_ref } } } });
});

// test_ws_contract.py runs the Python constructors and checks this same file;
// the Owner's structured outcome has its independent recordingReplay producer.
it.each(Object.entries(producers))("decodes the real %s producer with additive fields", (_sample, event) => {
  expect(decodeWsEvent({ ...event, future_field: true })).toMatchObject({ kind: "event", event: { type: event.type } });
});

it("applies the real partial archive frame as shared activity and a successor relationship", () => {
  const frame = producers.session_archived_partial;
  const source = { ...producers.session_unarchived.session, id: frame.old_session_id } as SessionInfo;
  useSessionStore.setState({ sessions: [source], activeSessionId: source.id });
  const tracked = sessions.observe(source.id, useSessionStore.getState(), "archive");
  try {
    handleWsMessage(frame);
    const state = useSessionStore.getState();
    expect(sessions.archiveActivity(source.id, tracked.observation)).toEqual(frame.archive_result);
    expect(state.archivedSessions).toEqual([{ ...source, archived: true }]);
    expect(state.sessionSuccessors[source.id]).toBe(frame.new_session_id);
    expect(state.activeSessionId).toBe(source.id);
  } finally {
    tracked.finish();
  }
});

it("keeps the original structured Owner outcome through the decoder and store", () => {
  expect(decodeWsEvent(recording.event).kind).toBe("event");
  handleWsMessage(recording.event);
  const message = useSessionStore.getState().messages[recording.event.session_id][0];
  expect(message.outcome).toMatchObject({ request_id: "recording-replay", status: "success" });
});

it.each([
  null, [], "text", { type: "assistant_text", content: "No session" },
  { type: "assistant_text", session_id: "s", content: "Text", seq: -1 },
  { type: "assistant_text", session_id: "s", content: "Text", seq: "9" },
  { type: "question_request", session_id: "s", question_id: "q", questions: [{ question: "Q", options: ["Bad option"] }] },
  { type: "bg_completed", session_id: "s", task_id: "b", status: "invented", completed_at: null },
  { type: "research_delivery", session_id: "s", research_id: "r", injection_status: "invented" },
  { type: "wiki_job_changed", session_id: "s", vault_id: "v", job_id: "j", state_version: 0 },
  { type: "agent_deleted" }, { type: "application_deleted", application_id: 1 },
  { type: "session_unarchived", session_id: "s", session: { id: "s" } },
  { ...producers.agent_updated, agent: { ...producers.agent_updated.agent, pinned: "yes" } },
  { ...producers.application_updated, application: { ...producers.application_updated.application, status: "invented" } },
  { ...producers.subagent, runtime_metadata: { request_id: 4, attempt_id: "a" } },
  { ...producers.session_unarchived, session_id: "another-session" },
  { ...producers.agent_updated, agent_id: "another-agent" },
  { ...producers.agent_updated, type: "agent_archived", agent_id: "another-agent", agent: { ...producers.agent_updated.agent, archived: true } },
  { ...producers.agent_updated, type: "agent_archived" },
])("rejects malformed wire input: %j", (value) => {
  expect(decodeWsEvent(value).kind).toBe("invalid");
});

it("reports an unknown extension separately from an invalid supported event", () => {
  expect(decodeWsEvent({ type: "future_extension", seq: 100 })).toEqual({ kind: "unknown", type: "future_extension" });
});

it.each([
  { old_session_id: "other-old" }, { new_session_id: "other-new" },
  { archived: false }, { failure_stage: "publication" }, { failure_stage: ["schedule_rebinding"] }, { message: null },
])("rejects an archive result that contradicts its envelope or contract: %j", (changed) => {
  expect(decodeWsEvent({ type: "session_archived", old_session_id: "old", new_session_id: "new", name: "Successor",
    archive_result: { old_session_id: "old", new_session_id: "new", archived: true,
      failure_stage: "successor_configuration", message: "Storage unavailable", ...changed } }).kind).toBe("invalid");
});

it("feeds real producers into their original consumers without inventing missing Research cards", () => {
  handleWsMessage(producers.agent_updated);
  handleWsMessage(producers.application_updated);
  handleWsMessage(producers.assistant_text);
  handleWsMessage(producers.research_failed);
  expect(useSessionStore.getState().agents[0].id).toBe("agent");
  expect(useSessionStore.getState().applications[0].id).toBe("application");
  expect(useSessionStore.getState().messages.session[0].content).toBe("Complete text");
  expect(useSessionStore.getState().research.session).toBeUndefined();
});
