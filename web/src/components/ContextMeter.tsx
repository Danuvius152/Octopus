import { useId } from "react";
import { contextDisplay, type ContextState } from "../lib/sessionContext";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "./ui/dialog";

const colors = {
  neutral: "text-muted-foreground",
  amber: "text-warn-foreground",
  red: "text-destructive",
};
const reasons = {
  configuration_changed: "Model or credential settings changed.",
  native_context_changed: "The native context or model changed.",
  compacted: "History was compacted; a new reliable sample is pending.",
  operation_unconfirmed: "An interrupted operation may have changed native history.",
};

export function ContextMeter({ state, historical = false, pending = false }: {
  state: ContextState | null | undefined; historical?: boolean; pending?: boolean;
}) {
  const display = contextDisplay(state, historical || pending);
  const sample = state?.sample;
  const updating = pending || state?.validity === "stale";
  const compacting = !pending && state?.activity === "compacting";
  const descriptionId = useId();
  const description = historical ? "Historical sample from this archived conversation." : compacting
    ? "Native compaction is in progress. Any displayed value is the last sample before compaction."
    : updating ? "Awaiting a new native sample." : display.remaining === null
      ? "Context remaining is unknown." : `${display.percent} context remaining.`;
  return (
    <Dialog>
      <DialogTrigger asChild>
        <button type="button" title="Context remaining" aria-label={`Context remaining: ${display.percent}`}
          aria-describedby={descriptionId}
          data-tone={display.tone}
          className={`inline-flex min-h-8 items-center gap-1 rounded-lg px-1.5 hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring ${colors[display.tone]}`}>
          <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" className="shrink-0 -rotate-90">
            <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="2.5" opacity=".2" />
            {display.remaining !== null && <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="2.5"
              pathLength="100" strokeDasharray={`${display.remaining * 100} 100`} />}
          </svg>
          <span className="text-xs font-mono tabular-nums text-foreground">{display.percent}</span>
          {(compacting || updating) && <span className="text-xs">{compacting ? "Compacting…" : "Awaiting update"}</span>}
        </button>
      </DialogTrigger>
      <span id={descriptionId} className="sr-only">{description}</span>
      <DialogContent className="w-[calc(100%-2rem)] max-w-sm gap-3 text-sm">
        <DialogTitle>Context details</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
        {state?.invalidation && <p>{reasons[state.invalidation.reason]}</p>}
        {sample ? <>
          {(historical || updating) && <p className="font-medium">Historical sample</p>}
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 break-words">
            <dt className="text-muted-foreground">Usage</dt>
            <dd>{sample.used_tokens != null ? sample.capacity_tokens != null
              ? `${sample.used_tokens.toLocaleString()} / ${sample.capacity_tokens.toLocaleString()} tokens`
              : `${sample.used_tokens.toLocaleString()} tokens; window unknown`
              : sample.used_ratio != null ? `${(sample.used_ratio * 100).toFixed(1)}% occupied (native ratio)` : "Not reported"}</dd>
            <dt className="text-muted-foreground">Model</dt><dd>{sample.actual_model ?? "Not reported"}</dd>
            <dt className="text-muted-foreground">Sampled</dt><dd><time dateTime={sample.observed_at}>{sample.observed_at}</time></dd>
          </dl>
          <p>{sample.basis === "codex_recomputed_estimate" ? "Native recomputed estimate after compaction."
            : sample.basis === "claude_last_request_input" ? "Most recent root request input, including cache reads and writes."
            : sample.basis === "provider_ratio" ? "Native reported ratio; exact token counts may be unavailable."
            : "Most recent root context sample; capacity is the native reported effective window."}</p>
          {sample.used_tokens != null && sample.capacity_tokens != null && sample.used_tokens > sample.capacity_tokens
            && <p>The native sample exceeds the reported window; remaining is shown as 0%.</p>}
        </> : <p>No native context data yet.</p>}
        <p className="text-xs text-muted-foreground">Latest native sample. New input and tool results are not estimated between samples.</p>
      </DialogContent>
    </Dialog>
  );
}
