import { useEffect, useState } from "react";
import { IconBook, IconCheck, IconAlertTriangle } from "@tabler/icons-react";
import type { WikiJobCard } from "../api";
import { useSessionStore } from "../stores/sessionStore";
import { controlWikiJob, reconcileWikiJobs, refreshWikiJobs } from "../lib/wikiJobs";
import { WikiJobDetails } from "./WikiJobDetails";

const LABELS: Record<string, string> = {
  queued: "Queued", running: "Reviewing", interrupted: "Verifying original execution",
  completed: "Completed", completed_with_attention: "Needs review", attention: "Needs review",
  failed: "Failed", cancelled: "Cancelled",
};

function Card({ card, sessionId }: { card: WikiJobCard; sessionId: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [details, setDetails] = useState(false);
  const unsettled = ["pending", "attention", "unconfirmed"].includes(card.settlement_phase);
  const cancel = ["queued", "running", "interrupted"].includes(card.status);
  const act = async (action: "cancel" | "retry") => {
    setBusy(true);
    setError("");
    try { await controlWikiJob(sessionId, card, action); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "Wiki maintenance is unavailable."); }
    finally { setBusy(false); }
  };
  return <article data-wiki-job={card.job_id} data-status={card.status}
    className="rounded-lg border border-border bg-muted/30 px-3 py-2 text-sm">
    <div className="flex items-start gap-2">
      <IconBook size={16} className="mt-0.5 shrink-0 text-primary" />
      <div className="min-w-0 flex-1">
        <div className="font-medium">Wiki maintenance · {LABELS[card.status] ?? card.status}</div>
        {card.scope && <p className="line-clamp-2 text-muted-foreground" title={card.scope}>{card.scope}{card.scope_truncated ? "…" : ""}</p>}
      </div>
      {card.status === "completed" ? <IconCheck size={16} className="text-emerald-600" />
        : ["failed", "completed_with_attention", "attention"].includes(card.status) && <IconAlertTriangle size={16} className="text-destructive" />}
    </div>
    <p className="mt-1 text-xs text-muted-foreground">
      {card.progress.completed}/{card.progress.total} items completed
      {card.progress.attention + card.progress.failed > 0 && ` · ${card.progress.attention + card.progress.failed} need review`}
      {(card.effects?.applied ?? 0) > 0 && ` · ${card.effects!.applied} accepted operations`}
    </p>
    {(card.effects?.index_pending ?? 0) > 0 && <p className="mt-1 text-xs">Content saved; indexing remains pending. Existing output will be recovered.</p>}
    {unsettled && <p role="status" className="mt-1 text-xs">
      {card.settlement_phase === "pending" ? "Cancellation requested; waiting for the original execution to retire."
        : "Original execution or result recording is unconfirmed. Retry is blocked."}
    </p>}
    {(card.recording_pending ?? 0) > 0 && !unsettled && <p className="mt-1 text-xs">Original result recording needs verification; recovery uses the original request.</p>}
    {card.error && <p className="mt-1 whitespace-pre-wrap break-words text-xs text-destructive">{card.error}{card.error_truncated ? "…" : ""}</p>}
    {card.outcome_refs.length > 0 && <details className="mt-1 text-xs">
      <summary className="cursor-pointer">Saved outputs ({card.outcome_count})</summary>
      <ul className="mt-1 space-y-1">{card.outcome_refs.map((ref) => <li key={`${ref.doc_id}/${ref.path}/${ref.revision}`} className="break-all">
        {ref.path} <span className="text-muted-foreground" title={ref.revision}>{ref.revision.slice(0, 12)}</span>
      </li>)}</ul>
      {card.outcome_refs_truncated && <p>Additional outputs are retained in the Wiki task details.</p>}
    </details>}
    <div className="mt-2 flex gap-3 text-xs">
      {cancel && <button type="button" disabled={busy} onClick={() => void act("cancel")} className="text-destructive disabled:opacity-50">Cancel maintenance</button>}
      {!details && <button type="button" onClick={() => setDetails(true)} className="text-primary">Task details</button>}
    </div>
    {details && <WikiJobDetails card={card} sessionId={sessionId} close={() => setDetails(false)} />}
    {error && <p role="alert" className="mt-1 text-xs text-destructive">{error}</p>}
  </article>;
}

export function WikiJobCards({ sessionId }: { sessionId: string }) {
  const token = useSessionStore((state) => state.token);
  const generation = useSessionStore((state) => state.dispatchGeneration);
  const connected = useSessionStore((state) => state.connected);
  const cards = useSessionStore((state) => state.wikiJobs[sessionId]);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [more, setMore] = useState(false);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    let active = true;
    void reconcileWikiJobs(sessionId).then((count) => {
      if (active) { setOffset(count); setMore(count === 30); setError(""); }
    }).catch((failure) => {
      if (active) setError(failure instanceof Error ? failure.message : "Could not load Wiki maintenance.");
    });
    return () => { active = false; };
  }, [sessionId, token, generation, connected]);
  const loadMore = async () => {
    setLoading(true);
    try {
      const count = await refreshWikiJobs(sessionId, offset);
      setOffset(offset + count); setMore(count === 30); setError("");
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Could not load Wiki maintenance."); }
    finally { setLoading(false); }
  };
  if (!cards?.length && !error) return null;
  return <section aria-label="Wiki maintenance tasks" className="max-h-64 overflow-y-auto space-y-2 pb-2">
    {cards?.map((card) => <Card key={`${card.vault_id}/${card.job_id}`} card={card} sessionId={sessionId} />)}
    {more && <button type="button" disabled={loading} onClick={() => void loadMore()} className="text-xs text-primary">More maintenance tasks</button>}
    {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
  </section>;
}
