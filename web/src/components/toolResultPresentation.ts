import type { Message } from "../stores/sessionStore";
import { readRuntimeMetadata } from "../lib/toolIdentity";

export const toolResultCardClass = "msg msg-tool-result rounded-lg border overflow-hidden";
export const toolResultHeaderClass = "tool-header w-full flex items-center gap-2 px-3 py-2 pr-12 text-left text-sm";
export const toolResultIconClass = "tool-icon text-muted-foreground size-[14px] shrink-0";
export const toolResultLabelClass = "tool-result-label text-xs font-semibold uppercase tracking-wider shrink-0";
export const toolResultPreviewClass = "tool-preview truncate text-xs text-muted-foreground font-mono";
const previewLength = 120;
const nativeResultPreviews = new WeakMap<Message, string>();

function record(value: unknown): Record<string, unknown> | undefined {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>;
}

function nativeFileChange(message: Message, content: string) {
  if (message.tool_name != null && message.tool_name !== "fileChange") return;
  const source = readRuntimeMetadata(message.runtime_metadata)?.native;
  if (!source?.item_id) return;

  const nativeItem = record(message.tool_input?.native_item);
  if (nativeItem) {
    if (nativeItem.id !== source.item_id || nativeItem.type !== "fileChange") return;
    if (Array.isArray(nativeItem.changes)) return nativeItem;
  }

  // A verified browser type or a saved producer name can identify the
  // complete body after the duplicate item is elided.
  if (!nativeItem && message.native_item_type !== "fileChange" && message.tool_name !== "fileChange") return;
  let payload: Record<string, unknown> | undefined;
  try {
    payload = record(JSON.parse(content));
  } catch {
    return;
  }
  const item = record(payload?.item);
  if (payload?.threadId === source.thread_id && payload.turnId === source.turn_id &&
      item?.id === source.item_id && item.type === "fileChange") return item;
}

function fileChangeEntry(entry: unknown) {
  const change = record(entry);
  const kind = record(change?.kind);
  if (typeof change?.path !== "string" || typeof change.diff !== "string" || typeof kind?.type !== "string") return;
  return { path: change.path, diff: change.diff, kind: kind.type,
    move_path: typeof kind.move_path === "string" ? kind.move_path : undefined };
}

function jsonStringPreview(value: string, budget: number) {
  const quoted = JSON.stringify(value.slice(0, budget));
  if (quoted.length <= budget) return quoted;

  let prefix = "";
  let length = '"…"'.length;
  for (const character of value) {
    length += JSON.stringify(character).length - 2;
    if (length > budget) break;
    prefix += character;
  }
  return JSON.stringify(prefix + "…");
}

function formatFileChange(item: Record<string, unknown> | undefined, preview = false) {
  if (!item || !Array.isArray(item.changes) || typeof item.status !== "string") return;
  if (preview && item.changes.length) {
    const first = fileChangeEntry(item.changes[0]);
    if (!first) return;
    const start = '{\n  "path": ';
    const separator = ',\n  "diff": ';
    const end = "\n}";
    const valueBudget = previewLength - start.length - separator.length - end.length;
    const path = jsonStringPreview(first.path, Math.floor(valueBudget / 2));
    const diff = jsonStringPreview(first.diff, valueBudget - path.length);
    return start + path + separator + diff + end;
  }
  const changes = [];
  for (const entry of item.changes) {
    const change = fileChangeEntry(entry);
    if (!change) return;
    changes.push(change);
  }
  return JSON.stringify({ changes, status: item.status }, null, 2);
}

export function toolResultPresentation(message: Message) {
  const original = message.content || "";
  let preview = nativeResultPreviews.get(message);
  if (preview === undefined) {
    preview = (formatFileChange(nativeFileChange(message, original), true) ?? original).slice(0, previewLength);
    if (message.runtime_metadata?.native) nativeResultPreviews.set(message, preview);
  }
  const errored = !!message.is_error;
  const nativeItem = message.runtime_metadata?.native && message.tool_input?.native_item;
  const native = nativeItem && typeof nativeItem === "object" ? nativeItem as Record<string, unknown> : undefined;
  const savedPath = native?.type === "imageGeneration" && native.status === "completed" && !errored
    && typeof native.savedPath === "string" ? native.savedPath : undefined;
  const label = native?.type === "hookPrompt" ? "Hook prompt" : errored ? "Error" : "Result";
  return { detail: () => formatFileChange(nativeFileChange(message, original)) ?? original, preview, errored, savedPath, label };
}
