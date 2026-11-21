import type { RuntimeMetadata, ToolReceipt } from "../api";
import type { Message } from "../stores/sessionStore";

export type AttemptIdentity = Pick<RuntimeMetadata, "request_id" | "attempt_id">;

/** WS envelopes and persisted rows carry the same attempt in different fields. */
export function readAttemptIdentity(value: unknown): AttemptIdentity | undefined {
  if (!value || typeof value !== "object") return undefined;
  const row = value as Record<string, unknown>;
  const input = row.tool_input as Record<string, unknown> | null | undefined;
  for (const candidate of [row.runtime_metadata, value, input?.attempt_context]) {
    if (!candidate || typeof candidate !== "object") continue;
    const identity = candidate as Record<string, unknown>;
    if (typeof identity.request_id === "string" && identity.request_id &&
        typeof identity.attempt_id === "string" && identity.attempt_id) {
      return { request_id: identity.request_id, attempt_id: identity.attempt_id };
    }
  }
}

export function sameAttempt(a: AttemptIdentity | undefined, b: AttemptIdentity | undefined): boolean {
  return Boolean(a && b && a.request_id === b.request_id && a.attempt_id === b.attempt_id);
}

export function readRuntimeMetadata(value: unknown): RuntimeMetadata | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const data = value as RuntimeMetadata;
  return [data.request_id, data.attempt_id].every((id) => typeof id === "string" && id.length > 0) &&
    (data.tool_call_id == null || typeof data.tool_call_id === "string" && data.tool_call_id.length > 0) &&
    (data.native == null || [data.native.thread_id, data.native.turn_id,
      data.native.root_thread_id, data.native.root_turn_id].every((id) => typeof id === "string" && id.length > 0) &&
      (data.native.item_id == null || typeof data.native.item_id === "string" && data.native.item_id.length > 0))
    ? data : undefined;
}

function usableReceipt(receipt: ToolReceipt | null | undefined): ToolReceipt | undefined {
  if (receipt?.kind === "bg" && typeof receipt.task_id === "string" && receipt.task_id) return receipt;
  if (receipt?.kind === "delegation" && typeof receipt.delegation_id === "string" && receipt.delegation_id &&
      typeof receipt.work_id === "string" && receipt.work_id) return receipt;
  return undefined;
}

function resultKey(identity: RuntimeMetadata) {
  return JSON.stringify([identity.request_id, identity.attempt_id, identity.tool_call_id]);
}

const toolResultsByTranscript = new WeakMap<Message[], Map<string, Message>>();

function toolResults(messages: Message[]) {
  let results = toolResultsByTranscript.get(messages);
  if (!results) {
    results = new Map();
    for (const row of messages) {
      if (row.type !== "tool_result") continue;
      const identity = readRuntimeMetadata(row.runtime_metadata);
      if (!identity?.tool_call_id) continue;
      const key = resultKey(identity);
      if (!results.has(key)) results.set(key, row);
    }
    toolResultsByTranscript.set(messages, results);
  }
  return results;
}

/** One projection for live, snapshot and paged tool messages; provider IDs grant no association. */
export function projectToolCall(message: Message, messages: Message[] | undefined) {
  const identity = readRuntimeMetadata(message.runtime_metadata);
  const result = identity?.tool_call_id && messages ? toolResults(messages).get(resultKey(identity)) : undefined;
  return {
    toolCallId: identity?.tool_call_id,
    result: result || undefined,
    failed: Boolean(result?.is_error),
    receipt: result?.is_error ? undefined : usableReceipt(result?.runtime_metadata?.receipt ?? identity?.receipt),
  };
}

export function projectBgTaskId(message: Message, messages: Message[] | undefined) {
  if (message.type !== "tool_use" || message.tool_name !== "mcp__bg__run") return;
  const receipt = projectToolCall(message, messages).receipt;
  if (receipt?.kind === "bg") return receipt.task_id;
}

export function representedBgTaskIds(messages: Message[] | undefined): Set<string> {
  const ids = new Set<string>();
  for (const message of messages ?? []) {
    const taskId = projectBgTaskId(message, messages);
    if (taskId) ids.add(taskId);
  }
  return ids;
}
