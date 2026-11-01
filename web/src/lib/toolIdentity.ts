import type { RuntimeMetadata, ToolReceipt } from "../api";
import type { Message } from "../stores/sessionStore";

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

/** One projection for live, snapshot and paged tool messages; provider IDs grant no association. */
export function projectToolCall(message: Message, messages: Message[] | undefined) {
  const identity = readRuntimeMetadata(message.runtime_metadata);
  const result = identity?.tool_call_id ? messages?.find((row) => {
    const candidate = readRuntimeMetadata(row.runtime_metadata);
    return row.type === "tool_result" && candidate && candidate.tool_call_id === identity.tool_call_id &&
      candidate.request_id === identity.request_id && candidate.attempt_id === identity.attempt_id;
  }) : undefined;
  return {
    toolCallId: identity?.tool_call_id,
    result: result || undefined,
    failed: Boolean(result?.is_error),
    receipt: result?.is_error ? undefined : usableReceipt(result?.runtime_metadata?.receipt ?? identity?.receipt),
  };
}
