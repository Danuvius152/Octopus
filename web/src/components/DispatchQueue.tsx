import { accountFetch as fetch } from "../lib/accountFetch";
import { useState } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { Button } from "./ui/button";

const SOURCE_LABELS: Record<string, string> = {
  human: "You", bg: "Background task", delegation: "Agent", research: "Research",
  schedule: "Schedule", app: "App",
};

/** Render the server's queue order and change the gate only on explicit Resume. */
export function DispatchQueue({ sessionId, readOnly = false, archived = false }: {
  sessionId: string; readOnly?: boolean; archived?: boolean;
}) {
  const snapshot = useSessionStore((s) => s.dispatchStates[sessionId]);
  const unavailable = useSessionStore((s) => s.dispatchUnavailable[sessionId]);
  const [resuming, setResuming] = useState(false);
  const [error, setError] = useState("");
  const items = snapshot?.items ?? [];
  const paused = snapshot?.paused ?? false;

  const resume = async () => {
    const store = useSessionStore.getState();
    setResuming(true);
    setError("");
    try {
      const response = await fetch(`${window.location.origin}/api/sessions/${encodeURIComponent(sessionId)}/dispatch/resume`, {
        method: "POST", headers: { Authorization: `Bearer ${store.token}` },
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not resume processing.");
      store.applyDispatchState(sessionId, data, store);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not resume processing.");
    } finally {
      setResuming(false);
    }
  };

  if (archived) return null;
  if (!snapshot && !unavailable) return <p role="status" className="px-4 py-2 text-xs text-muted-foreground">Loading queue state…</p>;
  if (!unavailable && !paused && items.length === 0) return null;
  return <div className="queue-list shrink-0 border-t border-border bg-muted/30 px-4 py-2 text-xs space-y-1"
    aria-label="Queued messages">
    {unavailable && <p role="alert" className="text-destructive">Queue state unavailable. Current processing state could not be confirmed.</p>}
    <div className="queue-list-label flex flex-wrap items-center justify-between gap-2 text-muted-foreground mb-2">
      <span>{paused ? `Paused · ${items.length} pending` : `Queued (${items.length}) — will fire after the current turn`}</span>
      {paused && !readOnly && <Button type="button" size="sm" variant="outline"
        className="btn-resume-dispatch h-8" disabled={resuming} onClick={resume}>
        {resuming ? "Resuming…" : "Resume processing"}
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
