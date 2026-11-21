/**
 * Live status chip for a cross-turn background task.
 *
 * Rendered inside ToolUseBlock when the tool is `mcp__bg__run`. The
 * tool_use itself shows the model's request (command + description);
 * the chip shows the LIVE state (running spinner, completed/failed
 * badge, exit code, last-line output preview) and exposes a Cancel
 * button while the task is running.
 *
 * Store observations carry status and a bounded preview. The open card
 * owns its saved stdout/stderr read and can reuse a confirmed terminal body.
 */

import { useCallback, useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconExclamationCircle,
  IconHandStop,
  IconLoader2,
  IconX,
} from "@tabler/icons-react";

import { bgTaskPreviewText, useSessionStore, type BgTask } from "../stores/sessionStore";
import { cn } from "../lib/utils";
import { confirmAuxiliaryTask, observeBgTaskCard, observeBgTaskOutput, observeSessionAuxiliary } from "../lib/hydrateSession";
import { bgTaskOutputLine, cachedBgTaskOutput } from "../lib/bgTaskOutput";
import { useTaskControl } from "../hooks/useTaskControl";
import { TranscriptOutputLayout } from "./chatOutputVisibility";
import { TaskControlFeedback } from "./TaskControlFeedback";

interface Props {
  sessionId: string;
  taskId: string;
}

const STATUS_LABEL: Record<BgTask["status"], string> = {
  pending: "queued",
  running: "running",
  completed: "completed",
  failed: "failed",
  cancelled: "cancelled",
  interrupted: "interrupted",
};

function StatusIcon({ status }: { status: BgTask["status"] }) {
  if (status === "running" || status === "pending") {
    return <IconLoader2 size={14} className="animate-spin text-primary" />;
  }
  if (status === "completed") {
    return <IconCheck size={14} className="text-green-700" />;
  }
  if (status === "cancelled" || status === "interrupted") {
    return <IconHandStop size={14} className="text-muted-foreground" />;
  }
  return <IconExclamationCircle size={14} className="text-destructive" />;
}

function isRunning(task: Pick<BgTask, "status">): boolean {
  return task.status === "running" || task.status === "pending";
}

function stopDescription(task: BgTask): string | null {
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

export function BgTaskChip({ sessionId, taskId }: Props) {
  const token = useSessionStore((s) => s.token);
  const account = useSessionStore((s) => s.accountGeneration);
  const generation = useSessionStore((s) => s.dispatchGeneration);
  const task = useSessionStore((s) =>
    (s.bgTasks[sessionId] || []).find((t) => t.id === taskId)
  );
  const failed = useSessionStore((s) => s.bgTaskReadFailures[sessionId]?.includes(taskId) ?? false);

  useEffect(() => observeBgTaskCard(sessionId, taskId), [sessionId, taskId, token, account, generation]);

  if (!task) {
    return (
      <div className="octo-bgtask-chip mt-2 overflow-hidden rounded-md border border-border bg-muted/40 text-muted-foreground">
        <div className="flex h-8 items-center gap-2 px-2.5 text-xs">
          {failed ? <IconExclamationCircle size={12} /> : <IconLoader2 size={12} className="animate-spin" />}
          <span>{failed ? "Background task" : `Loading bg task ${taskId}…`}</span>
        </div>
        <SummaryLine sessionId={sessionId} taskId={taskId} failed={failed} />
      </div>
    );
  }
  return <BgTaskCard key={JSON.stringify([account, generation, sessionId, taskId, task.started_at])} task={task} failed={failed} />;
}

function SummaryLine({ task, sessionId, taskId, failed }: Props & { task?: BgTask; failed: boolean }) {
  const preview = task?.output_preview;
  const output = failed ? "" : bgTaskOutputLine(preview);
  const text = preview === undefined ? "Output preview not read" :
    output || (task?.has_output ? "Output available" : "No saved output");
  const onOutputLayout = useContext(TranscriptOutputLayout);
  useLayoutEffect(() => {
    if (output) onOutputLayout?.();
  }, [onOutputLayout, output, taskId]);
  const stop = task && stopDescription(task);
  const delivery = task?.delivery_disposition === "unknown" ? "Return admission unconfirmed" :
    task?.delivery_disposition === "stopped" ? "Automatic return stopped" : null;
  return (
    <div className="flex h-7 items-center gap-2 overflow-hidden border-t border-border/60 px-3 text-[11px] text-muted-foreground">
      {failed ? <span role="alert" className="shrink-0">Could not read summary</span> :
        <span className="truncate font-mono" title={text} data-bg-output={output ? taskId : undefined}>{text}</span>}
      {!failed && (task?.preview_truncated || task?.truncated || preview?.trimEnd().includes("\n")) &&
        <span className="shrink-0">Preview truncated</span>}
      {failed && <button type="button" className="shrink-0 underline" aria-label="Retry task summary"
        onClick={() => confirmAuxiliaryTask("background", sessionId, taskId, "card")}>Retry</button>}
      {stop && <span className="truncate" title={stop}>{stop}</span>}
      {delivery && <span className="truncate" title={delivery}>{delivery}</span>}
      {delivery && task?.delivery_error && <span className="truncate" title={task.delivery_error}>{task.delivery_error}</span>}
    </div>
  );
}

function BgTaskCard({ task, failed }: { task: BgTask; failed: boolean }) {
  const sessionId = task.session_id, taskId = task.id;
  const [expanded, setExpanded] = useState(false);
  const control = useTaskControl(sessionId, taskId);
  const running = isRunning(task);

  const cancel = () => control.cancel(`${window.location.origin}/api/sessions/${encodeURIComponent(sessionId)}/bg-tasks/${encodeURIComponent(taskId)}/cancel`);

  const headerStyle: Record<BgTask["status"], string> = {
    pending: "border-border bg-muted/40",
    running: "border-primary/40 bg-primary/5",
    completed: "border-green-700/40 bg-green-50",
    failed: "border-destructive/40 bg-destructive/5",
    cancelled: "border-border bg-muted/40",
    interrupted: "border-border bg-muted/40",
  };

  const stopReason = stopDescription(task);
  const idleLimit = task.idle_timeout_seconds == null
    ? "unknown" : task.idle_timeout_seconds === 0 ? "disabled" : `${task.idle_timeout_seconds}s`;
  const totalLimit = task.timeout_seconds == null ? "unknown" : `${task.timeout_seconds}s`;

  return (
    <div
      className={cn(
        "octo-bgtask-chip mt-2 rounded-md border overflow-hidden",
        headerStyle[task.status]
      )}
    >
      <div className="flex h-8 items-center gap-2 px-2.5 text-xs">
        {/* The expand-toggle is its own button — siblings (exit-code
         * badge, cancel button) live outside so we don't nest <button>
         * inside <button>, which is invalid HTML and breaks a11y. */}
        <button
          type="button"
          onClick={() => setExpanded((view) => !view)}
          className="flex flex-1 min-w-0 items-center gap-2 text-left hover:bg-accent/40 -mx-1 px-1 py-0.5 rounded transition-colors"
          aria-expanded={expanded}
          aria-label={expanded ? "Collapse bg task" : "Expand bg task"}
        >
          <span className="text-muted-foreground shrink-0">
            {expanded ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
          </span>
          <StatusIcon status={task.status} />
          <span className="font-semibold uppercase tracking-wider text-[10px] shrink-0">
            bg · {STATUS_LABEL[task.status]}
          </span>
          {task.description && (
            <span className="truncate text-foreground">{task.description}</span>
          )}
          {!task.description && (
            <code className="truncate font-mono text-muted-foreground">
              {task.command}
            </code>
          )}
        </button>
        {task.exit_code != null && task.exit_code !== 0 && (
          <span className="shrink-0 rounded bg-destructive/10 px-1.5 py-0.5 text-[10px] font-mono text-destructive">
            exit {task.exit_code}
          </span>
        )}
        {running && (
          <button
            type="button"
            onClick={cancel}
            disabled={control.pending}
            className="shrink-0 inline-flex items-center gap-1 rounded border border-border bg-card px-1.5 py-0.5 text-[10px] text-muted-foreground hover:text-foreground hover:border-destructive/50 transition-colors disabled:opacity-50"
            title="Cancel this background task"
          >
            <IconX size={10} />
            Cancel
          </button>
        )}
      </div>
      <TaskControlFeedback control={control} onRefresh={() => { void control.refresh(() => observeSessionAuxiliary(sessionId, "background", taskId)); }} />
      {expanded && stopReason && (
        <div className="border-t border-border/60 px-3 py-1 text-[11px] text-muted-foreground">
          {stopReason}
        </div>
      )}
      {expanded && task.delivery_disposition && (
        <div className="border-t border-border/60 px-3 py-1 text-[11px] text-muted-foreground">
          <span>{task.delivery_disposition === "unknown"
            ? "Return admission unconfirmed" : "Automatic return stopped"}</span>
          {task.delivery_error && <div>{task.delivery_error}</div>}
        </div>
      )}
      {(!expanded || failed) && <SummaryLine task={expanded ? undefined : task} sessionId={sessionId} taskId={taskId} failed={failed} />}
      {expanded && (
        <div className="border-t border-border/60 bg-card/60 px-3 py-2 space-y-2">
          <div className="text-[11px] text-muted-foreground">
            Idle timeout: {idleLimit} · Total timeout: {totalLimit}
          </div>
          {!stopReason && task.status !== "running" && task.status !== "pending"
            && task.status !== "completed" && (
            <div className="text-[11px] text-muted-foreground">Stop reason not recorded</div>
          )}
        </div>
      )}
      {expanded && <SavedBgOutput task={task} />}
    </div>
  );
}

function SavedBgOutput({ task }: { task: BgTask }) {
  const [detail, setDetail] = useState(() => cachedBgTaskOutput(task));
  const [phase, setPhase] = useState<"loading" | "ready" | "failed">(detail ? "ready" : "loading");
  const activeRead = useRef<ReturnType<typeof observeBgTaskOutput> | null>(null);
  const { session_id: sessionId, id: taskId, started_at: startedAt } = task;

  const readOutput = useCallback(() => {
    const current = useSessionStore.getState().bgTasks[sessionId]?.find((row) => row.id === taskId);
    if (!current || current.started_at !== startedAt) return;
    activeRead.current?.cancel();
    const read = observeBgTaskOutput(current);
    activeRead.current = read;
    const settle = (output?: Awaited<typeof read.done>) => {
      if (activeRead.current !== read) return;
      activeRead.current = null;
      const accepted = read.current() ? output : undefined;
      setPhase(accepted ? "ready" : "failed");
      if (accepted) setDetail((previous) => previous && !isRunning(previous) && isRunning(accepted) ? previous : accepted);
    };
    void read.done.then(settle, () => settle());
  }, [sessionId, taskId, startedAt]);

  useEffect(() => {
    if (!cachedBgTaskOutput({ session_id: sessionId, id: taskId, started_at: startedAt })) readOutput();
    return () => {
      activeRead.current?.cancel();
      activeRead.current = null;
    };
  }, [readOutput, sessionId, taskId, startedAt]);

  const diagnostic = detail?.delivery_error;
  const preview = diagnostic && bgTaskPreviewText(diagnostic, 160);
  const additionalDiagnostic = diagnostic && diagnostic !== preview && preview === task.delivery_error &&
    detail?.delivery_disposition === task.delivery_disposition ? diagnostic : null;
  return (
    <div className="space-y-2 bg-card/60 px-3 pb-2 text-[11px] text-muted-foreground">
      <div className="flex items-center justify-between gap-2">
        <span>{detail?.truncated ? "Saved output (truncated)" : "Saved output"}</span>
        <button type="button" className="shrink-0 underline" onClick={() => {
          setPhase("loading");
          readOutput();
        }}>Refresh output</button>
      </div>
      {phase === "loading" && <p role="status">Loading saved output…</p>}
      {phase === "failed" && <p role="alert">Could not load saved output.</p>}
      {detail && (
        <>
          <div><strong>command</strong>{" "}<code className="break-words font-mono">{detail.command}</code></div>
          {additionalDiagnostic && <details>
            <summary>Return diagnostic from the saved read</summary>
            <div className="whitespace-pre-wrap break-words">{additionalDiagnostic}</div>
          </details>}
          {(["stdout", "stderr"] as const).map((stream) => detail[stream] && (
            <div key={stream} className={stream === "stderr" ? "text-destructive" : undefined}>
              <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider">{stream}</div>
              <pre className="m-0 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-muted/40 px-2 py-1.5 font-mono">{detail[stream]}</pre>
            </div>
          ))}
          {!detail.stdout && !detail.stderr && <p>{isRunning(detail) ? "No saved output yet" : "No saved output"}</p>}
        </>
      )}
    </div>
  );
}
