/**
 * Which user-role messages the user never actually typed.
 *
 * Background-task results, agent-to-agent replies/questions/errors and
 * scheduled fires are all persisted with `role="user"` — they enter the
 * conversation the same way a typed turn does, which is the point. But nothing
 * that offers to *reuse* a past turn should offer these: the rewind picker
 * would rewind to a machine message, and composer history would put one back in
 * the box.
 *
 * One list, because two copies of it drift and the drift is invisible until a
 * new marker is added.
 */
export const AUTO_INJECTED_PREFIXES = [
  "[bg-task-result]",
  "[agent-reply:",
  "[agent-question:",
  "[agent-error:",
  "[scheduled:",
] as const;

export function isAutoInjectedPrompt(content: unknown): boolean {
  if (typeof content !== "string") return false;
  const t = content.trimStart();
  return AUTO_INJECTED_PREFIXES.some((p) => t.startsWith(p));
}

export function requestSource(message: { tool_input?: Record<string, unknown> }): string | undefined {
  const context = message.tool_input?.request_context;
  if (!context || typeof context !== "object") return undefined;
  const source = (context as Record<string, unknown>).source;
  return typeof source === "string" ? source : undefined;
}

/** Structured provenance wins; prefixes only recognize older history. */
export function isAutoInjectedMessage(message: {
  content?: unknown; tool_input?: Record<string, unknown>;
}): boolean {
  const source = requestSource(message);
  return source === undefined ? isAutoInjectedPrompt(message.content) : source !== "human";
}
