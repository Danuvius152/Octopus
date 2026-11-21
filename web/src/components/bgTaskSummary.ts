import type { BgTask } from "../stores/sessionStore";

export const bgTaskCardClass = "octo-bgtask-chip mt-2 rounded-md border overflow-hidden";
export const bgTaskSummaryClass = "flex h-7 items-center gap-2 overflow-hidden border-t border-border/60 px-3 text-[11px] text-muted-foreground";
export const bgTaskOutputClass = "truncate font-mono";

export function bgTaskStopDescription(task: BgTask): string | null {
  switch (task.stop_reason) {
    case "idle_timeout":
      return task.idle_timeout_seconds == null
        ? "Stopped at the idle timeout"
        : `Stopped after ${task.idle_timeout_seconds}s without output`;
    case "total_timeout":
      return task.timeout_seconds == null
        ? "Stopped at the total timeout"
        : `Stopped at the ${task.timeout_seconds}s total timeout`;
    case "user_cancel": return "Cancelled by the user";
    case "work_cancelled": return "Cancelled with delegated work";
    case "external_signal": return "Interrupted by an external signal";
    case "server_shutdown": return "Interrupted when the server stopped";
    case "server_restart": return "Interrupted after a server restart";
    case "exit_nonzero": return "The command exited unsuccessfully";
    default: return task.stop_reason ? `Stopped: ${task.stop_reason}` : null;
  }
}

export function bgTaskSummaryDetails(task: BgTask | undefined, failed: boolean) {
  const details: { key: string; text: string; className: string; title?: string }[] = [];
  if (!task) return details;
  if (!failed && (task.preview_truncated || task.truncated || task.output_preview?.trimEnd().includes("\n"))) {
    details.push({ key: "truncated", text: "Preview truncated", className: "shrink-0" });
  }
  const stop = bgTaskStopDescription(task);
  const delivery = task.delivery_disposition === "unknown" ? "Return admission unconfirmed" :
    task.delivery_disposition === "stopped" ? "Automatic return stopped" : null;
  if (stop) details.push({ key: "stop", text: stop, className: "truncate", title: stop });
  if (delivery) {
    details.push({ key: "delivery", text: delivery, className: "truncate", title: delivery });
    const error = task.delivery_error;
    if (error) details.push({ key: "error", text: error, className: "truncate", title: error });
  }
  return details;
}
