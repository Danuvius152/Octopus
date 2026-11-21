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
const BG_TASK_RESULT_PREFIX = "[bg-task-result]";

export const AUTO_INJECTED_PREFIXES = [
  BG_TASK_RESULT_PREFIX,
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

export function backgroundResultMessage(message: { content?: unknown; tool_input?: Record<string, unknown> }) {
  const { content } = message;
  if (typeof content !== "string") return;
  const source = requestSource(message);
  const hasPrefix = content.startsWith(BG_TASK_RESULT_PREFIX);
  if (source !== "bg" && !(source === undefined && hasPrefix)) return;
  const body = hasPrefix ? content.slice(BG_TASK_RESULT_PREFIX.length).trimStart() : content;
  return { body, summary: body.split("\n", 1)[0] };
}

/** Structured provenance wins; prefixes only recognize older history. */
export function isAutoInjectedMessage(message: {
  content?: unknown; tool_input?: Record<string, unknown>;
}): boolean {
  const source = requestSource(message);
  return source === undefined ? isAutoInjectedPrompt(message.content) : source !== "human";
}
