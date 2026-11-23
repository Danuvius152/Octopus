import type { Message } from "../stores/sessionStore";

export const toolResultCardClass = "msg msg-tool-result rounded-lg border overflow-hidden";
export const toolResultHeaderClass = "tool-header w-full flex items-center gap-2 px-3 py-2 pr-12 text-left text-sm";
export const toolResultIconClass = "tool-icon text-muted-foreground size-[14px] shrink-0";
export const toolResultLabelClass = "tool-result-label text-xs font-semibold uppercase tracking-wider shrink-0";
export const toolResultPreviewClass = "tool-preview truncate text-xs text-muted-foreground font-mono";

export function toolResultPresentation(message: Message) {
  const content = message.content || "";
  const preview = content.slice(0, 120);
  const errored = !!message.is_error;
  const nativeItem = message.runtime_metadata?.native && message.tool_input?.native_item;
  const native = nativeItem && typeof nativeItem === "object" ? nativeItem as Record<string, unknown> : undefined;
  const savedPath = native?.type === "imageGeneration" && native.status === "completed" && !errored
    && typeof native.savedPath === "string" ? native.savedPath : undefined;
  const label = native?.type === "hookPrompt" ? "Hook prompt" : errored ? "Error" : "Result";
  return { content, preview, errored, savedPath, label };
}
