import { readRuntimeMetadata } from "./toolIdentity";
import type { MessageContent } from "../api";

export type ToolInputSource = Pick<MessageContent, "type" | "tool_name" | "tool_input" | "content" | "runtime_metadata">;

/** Only a matching native observation proves that native_item is a runtime wrapper. */
export function presentToolInput(message: ToolInputSource): string {
  const input = message.tool_input;
  if (!input) return "";
  const native = readRuntimeMetadata(message.runtime_metadata)?.native;
  const item = input.native_item;
  let shown = input;
  if (message.type === "tool_use" && native?.item_id && item && typeof item === "object" && !Array.isArray(item) &&
      "id" in item && item.id === native.item_id) {
    const parameters = Object.fromEntries(Object.entries(input).filter(([key]) => key !== "native_item"));
    // Without independent arguments, the native record may be the only business payload.
    if (Object.keys(parameters).length) shown = parameters;
  }
  const command = message.type === "tool_use" && message.tool_name === "Bash" && Object.keys(shown).length === 1 &&
    typeof shown.command === "string" ? shown.command : undefined;
  return command ?? JSON.stringify(shown, null, 2);
}

/** Hide only bodies already displayed from their canonical message fields. */
export function displayToolInput(input: Record<string, unknown> | null | undefined, content: unknown) {
  if (!input) return input;
  const result = { ...input };
  const canonicalInput = Object.fromEntries(Object.entries(input).filter(([key]) => !["native_item", "request_context"].includes(key)));
  const same = (left: unknown, right: unknown) => right != null && JSON.stringify(left) === JSON.stringify(right);
  for (const source of ["native_item", "request_context"]) {
    const value = input[source];
    if (!value || typeof value !== "object" || Array.isArray(value)) continue;
    result[source] = Object.fromEntries(Object.entries(value).filter(([key, body]) =>
      !(["text", "content", "output", "aggregatedOutput", "result"].includes(key) && same(body, content)) &&
      !(["arguments", "input"].includes(key) && same(body, canonicalInput))));
  }
  return result;
}
