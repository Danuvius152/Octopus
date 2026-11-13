import type { Message } from "../stores/sessionStore";
import type { components } from "../api/contracts";

export interface TurnOutcome {
  operation?: "message" | "compact";
  status: "success" | "failed" | "interrupted" | "unknown";
  request_id?: string;
  message?: string;
  code?: string;
  provider_code?: string;
  recovery: Record<string, number>;
  final_answer?: components["schemas"]["FinalAnswerRef"] | null;
  native_status?: "completed" | "failed" | "interrupted" | null;
  compact?: components["schemas"]["CompactResult"] | null;
  recording_integrity?: "complete" | "incomplete";
  recording_issues?: Record<string, number>;
  recording_locations?: Record<string, unknown>[];
}

export interface TurnRecovery {
  phase: "diagnostic" | "retrying" | "recovered";
  request_id?: string;
  code?: string;
  message: string;
  attempt?: number;
  maximum?: number;
  delay?: number;
}

export type MessageInput = Omit<Message, "content"> & { content?: unknown };

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

function text(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

export function normalizeMessage(message: MessageInput): Message {
  const data = record(message.content);
  if (message.type === "result") {
    const valid = data.kind === "outcome" && (data.operation === undefined || data.operation === "message" || data.operation === "compact") &&
      ["success", "failed", "interrupted"].includes(String(data.status));
    const recovery = Object.fromEntries(Object.entries(record(data.recovery))
      .filter(([, count]) => typeof count === "number" && Number.isInteger(count) && count >= 0)) as Record<string, number>;
    const outcome: TurnOutcome = message.outcome ?? {
      operation: data.operation === "compact" ? "compact" : "message",
      status: valid ? data.status as TurnOutcome["status"] : message.is_error ? "failed" : "unknown",
      request_id: text(data.request_id),
      message: text(data.message) ?? text(message.content),
      code: text(data.code), provider_code: text(data.provider_code), recovery,
      final_answer: data.final_answer as TurnOutcome["final_answer"],
      native_status: data.native_status as TurnOutcome["native_status"],
      compact: data.compact as TurnOutcome["compact"],
      recording_integrity: data.recording_integrity as TurnOutcome["recording_integrity"],
      recording_issues: data.recording_issues as TurnOutcome["recording_issues"],
      recording_locations: Array.isArray(data.recording_locations)
        ? data.recording_locations.filter((value) => value && typeof value === "object" && !Array.isArray(value)).slice(0, 8) : undefined,
    };
    return { ...message, content: outcome.message, outcome };
  }
  if (message.type === "recovery") {
    const recovery: TurnRecovery = message.recovery ?? {
      phase: data.phase === "retrying" || data.phase === "recovered" ? data.phase : "diagnostic",
      request_id: text(data.request_id), code: text(data.code),
      message: text(data.message) ?? text(message.content) ?? "Backend notification",
      attempt: typeof data.attempt === "number" ? data.attempt : undefined,
      maximum: typeof data.maximum === "number" ? data.maximum : undefined,
      delay: typeof data.delay === "number" ? data.delay : undefined,
    };
    return { ...message, content: recovery.message, recovery };
  }
  return { ...message, content: text(message.content) ?? (message.content == null ? undefined : JSON.stringify(message.content, null, 2)) };
}

/** Read-only presentation of the recorded conclusion; native status never reclassifies it. */
export function presentOutcome(message: Message) {
  const outcome = normalizeMessage(message).outcome!;
  const labels = { success: "Done", failed: "Failed", interrupted: "Interrupted", unknown: "Ended · outcome not recorded" };
  const compactLabel = outcome.status === "success" ? outcome.compact?.effect === "not_applied" ? "No compaction needed" : "Context compacted"
    : outcome.status === "interrupted" ? "Compaction interrupted" : outcome.status === "unknown" ? "Compaction result unknown" : "Compaction failed";
  const compactReason = outcome.compact?.reason;
  const compactNote = outcome.compact?.effect === "applied" ? "Native history was compacted. Chat messages are preserved."
    : outcome.compact?.effect === "unknown" ? "Native history changes could not be confirmed. This operation will not be replayed automatically."
    : undefined;
  // Use the owner's canonical wording for known completed-root gaps so the
  // same primary cause appears once. An unrelated failure never hides a gap.
  let recordingNote: string | undefined;
  if (outcome.recording_integrity === "incomplete") {
    if (outcome.recording_issues?.native_final_missing) {
      recordingNote = outcome.native_status === "completed"
        ? "The model turn completed, but declared final output was not confirmed in the saved transcript."
        : "A declared final answer is missing from the saved transcript.";
    } else if (outcome.native_status === "completed") {
      recordingNote = outcome.recording_issues?.native_collection_incomplete
        ? "The model turn completed, but required tool results were not confirmed."
        : "The model turn completed, but required protocol records could not be validated.";
    } else recordingNote = "Completion records are incomplete.";
  }
  const primary = [outcome.message, recordingNote && !outcome.message?.includes(recordingNote) ? recordingNote : undefined]
    .filter(Boolean).join(" ");
  return { outcome, label: outcome.operation === "compact" ? compactLabel : labels[outcome.status],
    primary: outcome.operation === "compact" ? [primary,
      compactReason && !outcome.message?.includes(compactReason) ? compactReason : undefined,
      compactNote].filter(Boolean).join(" ") : primary };
}
