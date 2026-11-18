import { selectSession } from "../lib/selectSession";
import { refreshDelegations, watchDelegation } from "../lib/delegationReads";
/**
 * Inline card rendered next to a `mcp__ask_agent__ask` tool_use. The
 * tool_use itself shows the model's call shape (target agent name,
 * request, optional files); this card shows the LIVE state of the
 * delegation — running spinner, completed, cancelled, interrupted, failed — plus
 * a deep-link into the child's session and a cancel button while
 * the run is open.
 *
 * State source: `delegations[sessionId]` in the zustand store,
 * populated by either the snapshot fetch (on session load) or the
 * REST round-trip the tool_use kicked off. Once the sibling
 * tool_result arrives, we match the live record by the
 * `delegation_id` and `work_id` captured in its own tool receipt.
 * Every association uses the canonical call and its exact round receipt.
 */

import { useEffect, useMemo, useState } from "react";
import {
  IconCheck,
  IconExclamationCircle,
  IconExternalLink,
  IconHandStop,
  IconLoader2,
  IconSubtask,
  IconX,
} from "@tabler/icons-react";

import { projectToolCall } from "../lib/toolIdentity";
import { useSessionStore, type Delegation, type Message } from "../stores/sessionStore";
import { useTaskControl } from "../hooks/useTaskControl";
import { TaskControlFeedback } from "./TaskControlFeedback";

const STATUS_LABEL: Record<Delegation["state"], string> = {
  running: "running",
  completed: "completed",
  failed: "failed",
  cancelled: "cancelled",
  interrupted: "interrupted",
};

const RETURN_LABEL: Record<NonNullable<Delegation["terminal_disposition"]>, string | null> = {
  accepted: null,
  duplicate: null,
  steered: null,
  unknown: "Return admission unconfirmed",
  stopped: "Automatic return stopped",
  closed: "Automatic return stopped",
  unbound: "Automatic return stopped",
  unavailable: "Automatic return stopped",
  not_replayed: "Automatic return not replayed",
};

function StatusIcon({ state }: { state: Delegation["state"] }) {
  if (state === "running") {
    return <IconLoader2 size={14} className="animate-spin text-primary" />;
  }
  if (state === "completed") {
    return <IconCheck size={14} className="text-green-700" />;
  }
  if (state === "cancelled" || state === "interrupted") {
    return <IconHandStop size={14} className="text-muted-foreground" />;
  }
  return <IconExclamationCircle size={14} className="text-destructive" />;
}

export function AgentDelegationRequestCard({
  sessionId,
  message,
  agentName,
  request,
  files,
  continued = false,
}: {
  sessionId: string;
  message: Message;
  agentName: string;
  request: string;
  files: string[] | undefined;
  continued?: boolean;
}) {
  const token = useSessionStore((s) => s.token);
  const account = useSessionStore((s) => s.accountGeneration);
  const connected = useSessionStore((s) => s.connected);
  const generation = useSessionStore((s) => s.dispatchGeneration);
  const messages = useSessionStore((s) => s.messages[sessionId]);
  const projection = useMemo(() => projectToolCall(message, messages), [message, messages]);
  const receipt = projection.receipt?.kind === "delegation" ? projection.receipt : undefined;
  const delegationId = receipt?.delegation_id;
  const workId = receipt?.work_id;
  const match = useSessionStore((s) => (s.delegations[sessionId] || []).find((delegation) =>
    Boolean(delegationId && workId) && delegation.delegation_id === delegationId && delegation.work_id === workId
  ));
  const control = useTaskControl(sessionId, JSON.stringify([delegationId, workId]));
  const state = match?.state;
  const displayName = match?.target_agent_name || agentName || "agent";

  const terminal = Boolean(state && state !== "running");
  const observationKey = JSON.stringify([sessionId, delegationId, workId, account, token, connected, generation]);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  // The session coordinator owns transport and retry budget; this card owns
  // only demand for its exact receipt and its publication lifetime.
  useEffect(() => {
    if (!delegationId || !workId || terminal) return;
    let observation: ReturnType<typeof watchDelegation> | undefined;
    const update = () => {
      // Visibility owns only this continuous consumer. Hydration and command
      // confirmations retain their independent shared-read lifetimes.
      if (document.hidden || useSessionStore.getState().mainView !== "chat") {
        observation?.cancel(); observation = undefined;
      } else if (!observation) {
        observation = watchDelegation(sessionId, { delegation_id: delegationId, work_id: workId },
          (failed) => setUnavailable(failed ? observationKey : null));
      }
    };
    const unsubscribe = useSessionStore.subscribe((state, previous) => {
      if (state.mainView !== previous.mainView) update();
    });
    document.addEventListener("visibilitychange", update);
    update();
    return () => {
      unsubscribe(); document.removeEventListener("visibilitychange", update);
      observation?.cancel();
    };
  }, [delegationId, workId, terminal, sessionId, observationKey]);

  const openChild = async () => {
    if (!delegationId) return;
    await selectSession(delegationId);
  };

  const cancel = async () => {
    if (!match || state !== "running" || !workId || match.work_id !== workId) return;
    const url = `${window.location.origin}/api/sessions/${encodeURIComponent(
      sessionId
    )}/delegations/${encodeURIComponent(match.delegation_id)}/cancel`;
    await control.cancel(url, { reason: "cancelled from UI", work_id: match.work_id });
  };

  const label = state ? STATUS_LABEL[state] : projection.failed ? "failed to start" : receipt ? "status unavailable" : projection.result ? "receipt unavailable" : "awaiting receipt";
  const returnLabel = match?.terminal_disposition ? RETURN_LABEL[match.terminal_disposition] : null;

  return (
    <div
      className={`agent-delegation-request inline-flex max-w-full items-start gap-2 rounded-md border px-2.5 py-1.5 text-xs [overflow-wrap:anywhere] ${
        state === "completed"
          ? "border-primary/40 bg-primary/5"
          : state === "running" || (!state && !projection.failed)
          ? "border-border bg-card"
          : state === "cancelled" || state === "interrupted"
          ? "border-border bg-muted/40 text-muted-foreground"
          : "border-destructive/40 bg-destructive/5"
      }`}
      data-delegation-state={state ?? (projection.failed ? "failed" : receipt ? "unavailable" : "pending")}
    >
      <IconSubtask size={14} className="text-primary mt-0.5 shrink-0" />
      <div className="flex-1 min-w-0 space-y-0.5">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium truncate">
            {continued ? "Continued" : "Asked"} {displayName}
          </span>
          <span className="inline-flex items-center gap-1 text-muted-foreground">
            {state && <StatusIcon state={state} />}
            <span>{label}</span>
          </span>
        </div>
        <div className="text-muted-foreground truncate" title={request}>
          “{request}”
        </div>
        <TaskControlFeedback control={control} onRefresh={() => { void control.refresh(() => refreshDelegations(sessionId)); }} />
        {unavailable === observationKey && !terminal && <div className="mt-1">
          <div role="alert">Delegation status could not be confirmed.</div>
          {!control.result && <button type="button" className="text-primary underline disabled:opacity-50" disabled={control.pending}
            onClick={() => { void control.refresh(() => refreshDelegations(sessionId)); }}>Refresh status</button>}
        </div>}
        {returnLabel && (
          <div className="text-muted-foreground">
            <div>{returnLabel}</div>
            {match?.terminal_delivery_error && <div>{match.terminal_delivery_error}</div>}
          </div>
        )}
        {files && files.length > 0 && (
          <div className="text-[10px] text-muted-foreground/80">
            files: {files.join(", ")}
          </div>
        )}
      </div>
      <div className="flex items-center gap-1 shrink-0">
        {delegationId && (
          <button
            type="button"
            onClick={openChild}
            className="btn-open inline-flex items-center justify-center h-6 px-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-accent"
            title={`Open ${displayName}'s session`}
          >
            <IconExternalLink size={12} />
          </button>
        )}
        {state === "running" && match && workId === match.work_id && (
          <button
            type="button"
            onClick={cancel}
            disabled={control.pending}
            className="btn-cancel inline-flex items-center justify-center h-6 px-1.5 rounded text-muted-foreground hover:text-destructive hover:bg-destructive/10 disabled:opacity-50"
            title="Cancel delegation"
          >
            <IconX size={12} />
          </button>
        )}
      </div>
    </div>
  );
}
