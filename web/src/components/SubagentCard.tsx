import { useMemo, useState } from "react";
import {
  IconChevronDown,
  IconChevronRight,
  IconCircleCheck,
  IconAlertTriangle,
  IconPlayerStop,
  IconHelpCircle,
  IconUsersGroup,
} from "@tabler/icons-react";

import { useSessionStore, type SubagentRun } from "../stores/sessionStore";
import { nativeCard, nativeDetailAnchor } from "../lib/nativeOutput";
import { NativeSubagentDetails } from "./NativeSubagentDetails";

export function NativeSubagentCard({ sessionId, cardId }: { sessionId: string; cardId: string }) {
  const messages = useSessionStore((s) => s.messages[sessionId]);
  const streaming = useSessionStore((s) => s.streamingItems[sessionId]);
  const snapshots = useSessionStore((s) => s.subagents[sessionId]);
  const run = useMemo(() => {
    const saved = nativeCard(messages || [], cardId, Object.values(snapshots || {}));
    if (!saved) return undefined;
    const partials = Object.values(streaming || {}).filter((item) =>
      saved.runtime_metadata && item.runtime_metadata.request_id === saved.runtime_metadata.request_id &&
      item.runtime_metadata.attempt_id === saved.runtime_metadata.attempt_id &&
      item.runtime_metadata.native?.thread_id === saved.native_thread_id &&
      item.runtime_metadata.native?.turn_id === saved.native_turn_id);
    return { ...saved, summary: [saved.summary, ...partials.map((item) => item.content)].filter(Boolean).join("\n\n") };
  }, [messages, streaming, cardId, snapshots]);
  const anchorSeq = run ? nativeDetailAnchor(messages || [], run) : undefined;
  return <SubagentCard key={`${sessionId}:${cardId}`} sessionId={sessionId} observedRun={run} detailAnchor={anchorSeq} />;
}

const stopReasons: Record<string, string> = {
  parent_done: "Parent turn ended before completion",
  parent_error: "Parent execution failed before completion",
  parent_timeout: "Parent execution timed out",
  user_stop: "Stopped by you",
  service_shutdown: "Service shut down",
  work_cancelled: "Delegated work cancelled",
  process_ended: "Execution ended without a confirmed result",
  native_interrupted: "Helper execution was interrupted",
  native_shutdown: "Helper execution shut down",
  native_cancelled: "Helper execution was cancelled",
  native_not_found: "Helper could not be found",
  overlapping_execution: "Another execution began before a result was confirmed",
};

/** A sub-agent the model spawned inside this turn (native-subagents.md §5).
 *
 * Both CLIs can fan work out to a short-lived helper — Claude Code's `Task`,
 * Codex's `spawn_agent` — and both narrate it while it runs. Without this the
 * only trace is a tool call that sits there for minutes looking stuck, and a
 * result that appears from nowhere when it's over.
 *
 * Legacy calls require live cached state. Native observedRun also rebuilds
 * collected work and body from the original transcript after a refresh.
 */
export function SubagentCard({
  sessionId,
  toolCallId,
  observedRun,
  detailAnchor,
}: {
  sessionId: string;
  toolCallId?: string | null;
  observedRun?: SubagentRun;
  detailAnchor?: number;
}) {
  const cached = useSessionStore((s) =>
    toolCallId ? s.subagents[sessionId]?.[toolCallId] : undefined
  );
  const run = observedRun || cached;
  const [open, setOpen] = useState(false);
  if (!run) return null;

  const running = run.status === "running";
  const failed = run.status === "failed";
  const completed = run.status === "completed";
  const unknown = run.status === "unknown";
  const stopReason = run.stop_reason ? stopReasons[run.stop_reason] : undefined;
  const steps = run.steps ?? [];
  const meta = [
    run.tool_uses ? `${run.tool_uses} tool${run.tool_uses === 1 ? "" : "s"}` : "",
    run.tokens ? `${formatTokens(run.tokens)} tokens` : "",
    run.duration_ms ? formatDuration(run.duration_ms) : "",
  ].filter(Boolean);

  return (
    <div
      className={`subagent-card subagent-${run.status} rounded-lg border-[0.7px] px-3 py-2 ${
        failed
          ? "border-destructive/40 bg-destructive/5"
          : running
            ? "border-primary-100 bg-primary-50/50"
            : "border-gray-300 bg-gray-50"
      }`}
    >
      <button
        type="button"
        className="subagent-summary-row flex w-full items-center gap-2 text-left text-[12.5px]"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title={open ? "Hide what it's doing" : "See what it's doing"}
      >
        {running ? (
          <span className="subagent-spinner inline-block size-3 shrink-0 animate-spin rounded-full border-2 border-primary/30 border-t-primary" />
        ) : failed ? (
          <IconAlertTriangle size={14} className="shrink-0 text-destructive" />
        ) : completed ? (
          <IconCircleCheck size={14} className="shrink-0 text-success" />
        ) : unknown ? (
          <IconHelpCircle size={14} className="shrink-0 text-gray-700" />
        ) : (
          <IconPlayerStop size={14} className="shrink-0 text-gray-700" />
        )}
        <IconUsersGroup size={13} className="shrink-0 text-gray-700" />
        <span className="subagent-name shrink-0 font-semibold text-gray-900">
          {run.name || (run.native_thread_id ? `agent ${run.native_thread_id.slice(0, 8)}` : "sub-agent")}
        </span>
        {/* What it is doing right now — the reason this card exists. */}
        <span className="subagent-description truncate text-gray-700">
          {running
            ? run.description || "working"
            : failed
              ? "failed"
              : completed
                ? run.native_thread_id ? "helper finished" : "done"
                : unknown
                  ? "unconfirmed"
                  : run.status}
        </span>
        {meta.length > 0 && (
          <span className="subagent-meta ml-auto shrink-0 font-mono text-[10.5px] text-gray-700">
            {meta.join(" · ")}
          </span>
        )}
        <span className="shrink-0 text-gray-600">
          {open ? <IconChevronDown size={13} /> : <IconChevronRight size={13} />}
        </span>
      </button>

      {run.native_turn_id && <p className="mt-1 font-mono text-[10px] text-gray-600">Turn {run.native_turn_id.slice(0, 8)}</p>}

      {stopReason && (
        <p className="subagent-stop-reason mt-1 text-[11.5px] text-gray-700">{stopReason}</p>
      )}

      {/* The answer it gave, always in reach — one line closed, whole when
        * opened. While it's still working there is no answer yet, so the
        * closed card shows the step it's on (above) and nothing else. */}
      {!open && run.summary && (
        <p className="subagent-summary mt-1.5 line-clamp-2 text-[12px] text-gray-800">
          {run.summary}
        </p>
      )}
      {run.display_truncated && <p className="text-xs text-muted-foreground">
        Live helper preview truncated. Only collected items have saved details.
      </p>}

      {open && (
        <div className="subagent-detail mt-2 space-y-2 border-t border-gray-300 pt-2">
          {run.summary && (
            <div>
              <Label>answer</Label>
              <p className="subagent-summary mt-0.5 whitespace-pre-wrap break-words text-[12px] leading-relaxed text-gray-900">
                {run.summary}
              </p>
            </div>
          )}

          {/* What it has actually been doing. One line per step, newest last
            * — the thing a spinner and a token count can't tell you. */}
          {steps.length > 0 && (
            <div>
              <Label>
                {running ? `doing now · ${steps.length} steps` : `${steps.length} steps`}
              </Label>
              <ol className="subagent-steps mt-0.5 space-y-0.5">
                {steps.map((step, i) => (
                  <li
                    key={`${i}-${step}`}
                    className={`subagent-step flex gap-1.5 text-[11.5px] leading-relaxed ${
                      running && i === steps.length - 1
                        ? "font-medium text-gray-900"
                        : "text-gray-700"
                    }`}
                  >
                    <span className="shrink-0 font-mono text-[10px] text-gray-600">
                      {i + 1}
                    </span>
                    <span className="break-words">{step}</span>
                  </li>
                ))}
              </ol>
            </div>
          )}

          {run.prompt && (
            <div>
              <Label>brief</Label>
              <p className="subagent-prompt mt-0.5 whitespace-pre-wrap break-words text-[11.5px] leading-relaxed text-gray-700">
                {run.prompt}
              </p>
            </div>
          )}
          {run.native_thread_id && <NativeSubagentDetails sessionId={sessionId} anchorSeq={detailAnchor} />}
        </div>
      )}
    </div>
  );
}

/** The small caps label the expanded panel groups by. */
function Label({ children }: { children: React.ReactNode }) {
  return (
    <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-gray-600">
      {children}
    </span>
  );
}

function formatTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms / 60_000)}m`;
}
