import type { Message } from "../stores/sessionStore";

const knownClosureReasons = new Set(["cancelled", "communication_failure", "interrupted", "service_shutdown", "hard_cap",
  "idle_timeout", "native_resolved", "native_interrupted", "execution_completed", "execution_failed", "attempt_ended",
  "response_overflow", "deleted", "archived", "reset", "needs_input"]);

/** Unknown historical causes stay readable without inventing a reason or an answer. */
export function questionClosure(message: Message): { primary: string; detail?: string } {
  const reason = message.tool_input?.reason;
  if (typeof reason === "string" && knownClosureReasons.has(reason) && message.content) return { primary: message.content };
  return { primary: "The original question is closed.", detail: message.content };
}
