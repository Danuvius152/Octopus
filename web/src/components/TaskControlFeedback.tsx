import type { useTaskControl } from "../hooks/useTaskControl";

export function TaskControlFeedback({ control, onRefresh }: {
  control: ReturnType<typeof useTaskControl>; onRefresh: () => void;
}) {
  if (!control.result) return null;
  return <div className="mt-1 text-xs text-muted-foreground">
    <div role={control.result.failed ? "alert" : "status"}>{control.result.message}</div>
    <button type="button" className="text-primary underline disabled:opacity-50" disabled={control.pending} onClick={onRefresh}>
      Refresh status
    </button>
  </div>;
}
