import type { components } from "../api/contracts";
import type { Agent, Application, AttachmentMetadata, Delegation, QuestionItem, ResearchJob, SessionInfo } from "../stores/sessionStore";
import { readDispatchState, type DispatchState } from "./sessionDispatch";
import { readRuntimeMetadata } from "./toolIdentity";

type Guard<T> = (value: unknown) => value is T;
type Value<G> = G extends Guard<infer T> ? T : never;
type Rules = Record<string, Guard<unknown>>;
type Fields<S extends Rules> = { [K in keyof S as undefined extends Value<S[K]> ? never : K]: Value<S[K]> } &
  { [K in keyof S as undefined extends Value<S[K]> ? K : never]?: Value<S[K]> };
type Dto = components["schemas"];
/** Provider tool arguments and display metadata have no Octopus-owned schema. */
type OpaquePayload = Record<string, unknown>;

const record: Guard<OpaquePayload> = (v): v is OpaquePayload => v !== null && typeof v === "object" && !Array.isArray(v);
const text: Guard<string> = (v): v is string => typeof v === "string";
const id: Guard<string> = (v): v is string => text(v) && v.length > 0;
const number: Guard<number> = (v): v is number => typeof v === "number" && Number.isFinite(v);
const integer: Guard<number> = (v): v is number => number(v) && Number.isSafeInteger(v) && v >= 0;
const positive: Guard<number> = (v): v is number => integer(v) && v > 0;
const boolean: Guard<boolean> = (v): v is boolean => typeof v === "boolean";
const optional = <T,>(check: Guard<T>): Guard<T | undefined> => (v): v is T | undefined => v === undefined || check(v);
const nullable = <T,>(check: Guard<T>): Guard<T | null> => (v): v is T | null => v === null || check(v);
const maybeText = optional(nullable(text));
const maybeNumber = optional(nullable(number));
const array = <T,>(check: Guard<T>): Guard<T[]> => (v): v is T[] => Array.isArray(v) && v.every(check);
const oneOf = <const T extends string | number,>(...values: T[]): Guard<T> => (v): v is T => values.some((choice) => choice === v);

// These field readers belong only to this wire boundary. Keep extra fields for
// additive protocol compatibility; validate every field a typed consumer reads.
function fields<const S extends Rules>(rules: S): Guard<Fields<S>> {
  return (v): v is Fields<S> => record(v) && Object.entries(rules).every(([key, check]) => check(v[key]));
}
const numberMap: Guard<Record<string, number>> = (v): v is Record<string, number> => record(v) && Object.values(v).every(number);
const countMap: Guard<Record<string, number>> = (v): v is Record<string, number> => record(v) && Object.values(v).every(integer);
const dispatch: Guard<DispatchState> = (v): v is DispatchState => readDispatchState(v) !== null;
const runtime: Guard<Dto["RuntimeMetadata"]> = (v): v is Dto["RuntimeMetadata"] => readRuntimeMetadata(v) !== undefined;
const backend = oneOf("claude-code", "codex");
const tier = optional(nullable(oneOf("default", "fast")));
const settings = { model: maybeText, reasoning_effort: maybeText, service_tier: tier };
const attachment: Guard<AttachmentMetadata> = fields({ id, filename: text, size: integer, mime_type: text });
const question: Guard<QuestionItem> = fields({ question: text, header: optional(text), multiSelect: optional(boolean),
  options: optional(nullable(array(fields({ label: text, description: optional(text) })))) });
const subagentDefinition = fields({ name: text, description: text, prompt: text, model: maybeText, tools: array(text) });
const agent: Guard<Agent> = fields({
  id, name: text, description: text, avatar: maybeText, system_prompt: text, ...settings, credential_id: maybeText,
  backend, mcp_servers: array(text), tool_allow: text, tool_deny: text, subagents: array(subagentDefinition),
  is_system: boolean, archived: boolean, pinned: boolean, pin_order: maybeNumber,
  created_at: text, updated_at: text, active_session_count: integer,
});
const application: Guard<Application> = fields({
  id, name: text, description: text, icon: maybeText, icon_src: maybeText,
  backend: optional(fields({ state: text, port: maybeNumber, error: maybeText, uptime_s: maybeNumber, log_tail: optional(array(text)) })),
  agent_id: maybeText, session_id: maybeText, app_dir: text, entrypoint: text, status: oneOf("building", "ready", "failed"),
  error: maybeText, deletion: optional(nullable(fields({ phase: oneOf("retiring", "files"), keep_files: boolean, error: maybeText }))),
  archived: boolean, created_at: text, updated_at: text, last_built_at: maybeText, pinned: boolean, pin_order: maybeNumber,
});
const session: Guard<SessionInfo> = fields({
  id, name: text, working_dir: text, status: oneOf("idle", "running"), created_at: text,
  message_count: integer, transcript_integrity: oneOf("ok", "duplicate_seq"), claude_session_id: maybeText,
  credential_id: maybeText, agent_id: maybeText, origin: text, backend, ...settings,
  parent_session_id: maybeText, delegation_request: maybeText, app_id: maybeText,
  can_steer: boolean, can_fork: boolean, forked_from_session_id: maybeText, fork_after_seq: maybeNumber,
  fork_prefilled_prompt: maybeText, fork_revert_record: optional(nullable(fields({ ran: boolean, files: array(text),
    stash_ref: maybeText, status: text, refused_reason: maybeText, error: maybeText }))),
  fork_is_full_copy: boolean, archived: boolean,
});
const terminalDisposition = oneOf("accepted", "duplicate", "steered", "closed", "unbound", "unavailable", "not_replayed", "stopped", "unknown");
const delegation: Guard<Delegation & { work_id: string }> = fields({
  delegation_id: id, sub_session_id: id, parent_session_id: id, target_agent_id: id, target_agent_name: text,
  request: text, work_id: id, state: oneOf("running", "completed", "failed", "cancelled", "interrupted"),
  created_at: text, finished_at: nullable(text), error: nullable(text),
  terminal_disposition: optional(nullable(terminalDisposition)), terminal_delivery_error: maybeText,
});
const nativeInput: Guard<Dto["NativeInputEvidence"]> = fields({
  version: oneOf(1), transport: oneOf("codex-app-server"), attempt_id: id, input_id: id, kind: oneOf("start", "steer"),
  connection_generation: maybeText, target_turn_id: maybeText, rpc_id: maybeText, thread_id: maybeText, turn_id: maybeText,
  state: oneOf("not_sent", "unknown", "accepted", "rejected"),
  acceptance_source: optional(nullable(oneOf("start_response", "user_message", "steer_response"))),
  controller: oneOf("not_needed", "unknown", "confirmed"),
});
const execution: Guard<Dto["SubmissionExecution"]> = fields({
  state: oneOf("pending", "running", "done", "withdrawn", "unknown"), delivery_state: oneOf("none", "confirmed", "unknown"),
  reason_code: maybeText, state_version: integer, updated_at: maybeText, execution_owner_request_id: id,
  recoverable_input: boolean, native_inputs: optional(array(nativeInput)),
});
const researchReceipt: Guard<NonNullable<ResearchJob["delivery_receipt"]>> = fields({
  request_id: id, version: positive, stage: oneOf("queued", "processing", "processed", "failed", "recording_unconfirmed", "unknown"),
  error: maybeText, browser_confirmed_version: optional(integer), browser_confirmed_at: optional(text),
});
const finalAnswer: Guard<Dto["FinalAnswerRef"]> = fields({ thread_id: id, turn_id: id, message_seqs: optional(array(integer)) });
const outcome: Guard<Pick<Dto["TurnOutcome"], "kind" | "request_id" | "status" | "message" | "code" | "provider_code" | "recovery" | "final_answer" | "native_status">> = fields({
  kind: oneOf("outcome"), request_id: id, status: oneOf("success", "failed", "interrupted"),
  message: maybeText, code: maybeText, provider_code: maybeText,
  recovery: optional(countMap), final_answer: optional(nullable(finalAnswer)),
  native_status: optional(nullable(oneOf("completed", "failed", "interrupted"))),
  recording_integrity: optional(oneOf("complete", "incomplete")), recording_issues: optional(countMap),
  recording_locations: optional(array(record)),
});
const recovery = fields({ kind: oneOf("recovery"), request_id: id, phase: oneOf("diagnostic", "retrying", "recovered"),
  code: text, message: text, attempt: optional(nullable(integer)), maximum: optional(nullable(integer)), delay: maybeNumber });
const legacyOr = <T,>(check: Guard<T>): Guard<string | T | null> => (v): v is string | T | null => v === null || text(v) || check(v);

const envelope = { session_id: optional(id), seq: optional(nullable(integer)), created_at: maybeText,
  runtime_metadata: optional(nullable(runtime)), tool_input: optional(nullable(record)),
  request_id: maybeText, attempt_id: maybeText, work_id: maybeText };
const inSession = { session_id: id };
const diagnostic = { content: optional(nullable(text)), message: maybeText };
function event<const K extends string, const S extends Rules>(type: K, rules: S) {
  return { type, matches: fields({ ...envelope, type: oneOf(type), ...rules }) };
}
const definitions = [
  event("submission", { ...inSession, client_request_id: id, accepted: boolean, message: maybeText, code: maybeText,
    state: optional(nullable(oneOf("unknown"))), execution: optional(execution) }),
  event("command_receipt", { ...inSession, client_command_id: id, operation: oneOf("interrupt", "answer_question", "snooze_question"),
    accepted: boolean, message: maybeText, code: maybeText }),
  event("attempt_started", { ...inSession, request_id: id, attempt_id: id }),
  event("dispatch_state", { ...inSession, dispatch_state: dispatch }),
  event("queued", { ...inSession, dispatch_state: dispatch }),
  event("dequeued", { ...inSession, dispatch_state: dispatch }),
  event("recording_failed", { ...inSession, dispatch_state: optional(dispatch), message: maybeText }),
  event("delivery_notice", { ...inSession, ...diagnostic }),
  event("assistant_delta", { ...inSession, content: text }),
  event("assistant_text", { ...inSession, content: text }),
  event("subagent", { ...inSession, task_id: id, card_id: maybeText, native_thread_id: maybeText, native_turn_id: maybeText,
    parent_thread_id: maybeText, tool_call_id: maybeText, tool_use_id: maybeText,
    status: optional(oneOf("running", "completed", "failed", "cancelled", "interrupted", "unknown")), stop_reason: maybeText,
    name: optional(text), description: optional(text), prompt: optional(text), summary: optional(text),
    tokens: maybeNumber, tool_uses: maybeNumber, duration_ms: maybeNumber }),
  event("tool_use", { ...inSession, tool: id, input: nullable(record), tool_use_id: maybeText }),
  event("tool_result", { ...inSession, output: optional(legacyOr(record)), tool_use_id: maybeText, is_error: optional(boolean) }),
  event("question_request", { ...inSession, question_id: id, questions: array(question), deadline: maybeText,
    interaction_policy: optional(nullable(oneOf("human", "unattended"))), wait_mode: optional(nullable(oneOf("blocking", "optional", "mcp"))),
    snoozed: optional(nullable(boolean)) }),
  event("question_snoozed", { ...inSession, question_id: id, deadline: nullable(text), snoozed: boolean }),
  event("question_answer", { ...inSession, question_id: id, content: text }),
  event("question_closed", { ...inSession, question_id: id, message: text, reason: maybeText }),
  event("status", { ...inSession, status: oneOf("idle", "running") }),
  event("result", { ...inSession, content: optional(legacyOr(outcome)), message: maybeText, is_error: optional(boolean),
    cost: maybeNumber, claude_session_id: maybeText, code: maybeText }),
  event("recovery", { ...inSession, content: optional(legacyOr(recovery)), message: maybeText }),
  event("steered", { ...inSession, content: text }),
  event("user_message", { ...inSession, content: text, attachments: optional(nullable(array(attachment))) }),
  event("error", { message: text, code: maybeText }),
  event("wiki_job_changed", { ...inSession, vault_id: id, job_id: id, state_version: positive }),
  event("bg_started", { ...inSession, task_id: id, command: text, description: maybeText, started_at: text,
    idle_timeout_seconds: maybeNumber, timeout_seconds: maybeNumber }),
  event("bg_completed", { ...inSession, task_id: id, status: oneOf("completed", "failed", "cancelled", "interrupted"),
    exit_code: maybeNumber, truncated: optional(boolean), completed_at: nullable(text),
    idle_timeout_seconds: maybeNumber, timeout_seconds: maybeNumber, stop_reason: maybeText }),
  event("bg_delivery", { ...inSession, task_id: id, delivery_disposition: optional(nullable(oneOf("stopped", "unknown"))), delivery_error: maybeText }),
  event("delegation_delivery", { ...inSession, delegation }),
  event("research_started", { ...inSession, research_id: id, question: text }),
  event("research_progress", { ...inSession, research_id: id, phase: maybeText, detail: optional(text), counts: optional(numberMap) }),
  event("research_completed", { ...inSession, research_id: id, report_path: maybeText, sources: optional(array(text)),
    verified: optional(integer), unverified: optional(integer), injection_status: optional(oneOf("pending", "delivered", "failed", "unknown")) }),
  event("research_failed", { ...inSession, research_id: id, status: optional(oneOf("failed", "cancelled", "interrupted")), error: maybeText }),
  event("research_delivery", { ...inSession, research_id: id, injection_status: oneOf("pending", "delivered", "failed", "unknown"),
    error: maybeText, delivery_receipt: optional(nullable(researchReceipt)) }),
  event("schedules_changed", {}),
  event("application_created", { application, application_id: optional(id) }),
  event("application_updated", { application, application_id: optional(id) }),
  event("application_archived", { application_id: optional(id), application: optional(application) }),
  event("application_deleted", { application_id: id }),
  event("agent_created", { agent, agent_id: optional(id) }),
  event("agent_updated", { agent, agent_id: optional(id) }),
  event("agent_archived", { agent_id: id }),
  event("agent_deleted", { agent_id: id }),
  event("session_unarchived", { ...inSession, session, dispatch_state: optional(dispatch) }),
  event("session_archived", { old_session_id: id, new_session_id: nullable(id), name: optional(text), ...settings,
    dispatch_state: optional(dispatch) }),
  event("session_forked", { fork_session_id: id }),
  event("session_deleted", inSession),
];

export type WsEvent = Value<typeof definitions[number]["matches"]>;
export type ApplicationEvent = Extract<WsEvent, { type: `application_${string}` }>;
export type WsDecodeResult = { kind: "event"; event: WsEvent } | { kind: "unknown"; type: string } | { kind: "invalid"; type: string | null };

function identitiesAgree(data: WsEvent): boolean {
  switch (data.type) {
    case "submission":
      return !data.accepted || id(data.request_id);
    case "application_archived":
      return !!(data.application_id || data.application?.id) &&
        (data.application_id === undefined || data.application === undefined || data.application_id === data.application.id);
    case "application_created":
    case "application_updated":
      return data.application_id === undefined || data.application_id === data.application.id;
    case "agent_created":
    case "agent_updated":
      return data.agent_id === undefined || data.agent_id === data.agent.id;
    case "session_archived":
      return data.new_session_id === null || data.name !== undefined;
    case "session_unarchived":
      return data.session.id === data.session_id && !data.session.archived;
    case "delegation_delivery":
      return data.delegation.parent_session_id === data.session_id;
    case "result":
    case "recovery":
      return !data.content || typeof data.content === "string" || data.request_id == null || data.request_id === data.content.request_id;
    case "delivery_notice":
      return data.content != null || data.message != null;
    default:
      return true;
  }
}

/** The single decode boundary, shared by direct consumers and socket receipts. */
export function decodeWsEvent(value: unknown): WsDecodeResult {
  if (!record(value) || !text(value.type)) return { kind: "invalid", type: null };
  const definition = definitions.find((entry) => entry.type === value.type);
  if (!definition) return { kind: "unknown", type: value.type };
  if (!definition.matches(value)) return { kind: "invalid", type: value.type };
  if (!identitiesAgree(value)) return { kind: "invalid", type: value.type };
  return { kind: "event", event: value };
}
