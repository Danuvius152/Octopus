import { accountFetch as fetch } from "../lib/accountFetch";
import { useState } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { Button } from "./ui/button";

const EMPTY_QUEUE: string[] = [];
const SOURCE_LABELS: Record<string, string> = {
  human: "You", bg: "Background task", delegation: "Agent", research: "Research",
  schedule: "Schedule", app: "App",
};

/** Render the server's queue order and change the gate only on explicit Resume. */
export function DispatchQueue({ sessionId, readOnly = false }: {
  sessionId: string; readOnly?: boolean;
}) {
  const snapshot = useSessionStore((s) => s.dispatchStates[sessionId]);
  const legacy = useSessionStore((s) => s.pendingQueue[sessionId]) ?? EMPTY_QUEUE;
  const [resuming, setResuming] = useState(false);
  const [error, setError] = useState("");
  const items = snapshot?.items ?? legacy.map((summary, index) => ({
    request_id: `legacy-${index}`, source: "", summary, priority: false,
  }));
  const paused = snapshot?.paused ?? false;
  const savingBlocked = Boolean(snapshot?.recording_failure);

  const resume = async () => {
    const store = useSessionStore.getState();
    setResuming(true);
    setError("");
    try {
      const response = await fetch(`${window.location.origin}/api/sessions/${encodeURIComponent(sessionId)}/dispatch/resume${savingBlocked ? "?resume_queue=false" : ""}`, {
        method: "POST", headers: { Authorization: `Bearer ${store.token}` },
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not resume processing.");
      store.applyDispatchState(sessionId, data, store.dispatchGeneration);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not resume processing.");
    } finally {
      setResuming(false);
    }
  };

  if (!savingBlocked && !paused && items.length === 0) return null;
  return <div className="queue-list shrink-0 border-t border-border bg-muted/30 px-4 py-2 text-xs space-y-1"
    aria-label="Queued messages">
    {savingBlocked && <p role="alert" className="recording-failure text-destructive">
      Result saving failed or is unconfirmed. Automatic processing is blocked until the saved result is verified.
    </p>}
    <div className="queue-list-label flex flex-wrap items-center justify-between gap-2 text-muted-foreground mb-2">
      <span>{savingBlocked ? `Saving blocked · ${items.length} pending` : paused ? `Paused · ${items.length} pending` : `Queued (${items.length}) — will fire after the current turn`}</span>
      {(savingBlocked || paused) && !readOnly && <Button type="button" size="sm" variant="outline"
        className="btn-resume-dispatch h-8" disabled={resuming} onClick={resume}>
        {resuming ? savingBlocked ? "Checking saving…" : "Resuming…" : savingBlocked ? "Retry saving" : "Resume processing"}
      </Button>}
    </div>
    {items.map((item) => <div className="queue-item flex items-start gap-2 text-foreground" key={item.request_id}>
      <span className="queue-dot text-muted-foreground shrink-0">›</span>
      {item.source && <span className="queue-source shrink-0 text-muted-foreground">
        {SOURCE_LABELS[item.source] ?? item.source}{item.priority ? " · Priority" : ""}
      </span>}
      <span className="queue-content truncate">{item.summary}</span>
    </div>)}
    {error && <div role="alert" className="text-destructive">{error}</div>}
  </div>;
}
