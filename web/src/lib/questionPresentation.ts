import type { Message, PendingQuestion } from "../stores/sessionStore";

export function matchesPendingQuestion(message: Message, question: PendingQuestion): boolean {
  const request = message.tool_input?.request_id ?? message.runtime_metadata?.request_id;
  const attempt = message.tool_input?.attempt_id ?? message.runtime_metadata?.attempt_id;
  return message.type === "question_request" && message.tool_use_id === question.question_id &&
    (typeof request !== "string" || request === question.request_id) &&
    (typeof attempt !== "string" || attempt === question.attempt_id);
}

const knownClosureReasons = new Set(["cancelled", "communication_failure", "interrupted", "service_shutdown", "hard_cap",
  "idle_timeout", "native_resolved", "native_interrupted", "execution_completed", "execution_failed", "attempt_ended",
  "response_overflow", "deleted", "archived", "reset", "needs_input"]);

/** Unknown historical causes stay readable without inventing a reason or an answer. */
export function questionClosure(message: Message): { primary: string; detail?: string } {
  const reason = message.tool_input?.reason;
  if (typeof reason === "string" && knownClosureReasons.has(reason) && message.content) return { primary: message.content };
  return { primary: "The original question is closed.", detail: message.content };
}
