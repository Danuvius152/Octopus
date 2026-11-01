import { useCallback, useEffect, useRef, useState } from "react";
import type { NativeDetailPage } from "../api";
import { accountFetch } from "../lib/accountFetch";
import { useSessionStore } from "../stores/sessionStore";

/** A separate read view: it never merges detail pages into chat or usage. */
export function NativeSubagentDetails({ sessionId, anchorSeq }: { sessionId: string; anchorSeq?: number }) {
  const [page, setPage] = useState<NativeDetailPage | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const reading = useRef(0);

  useEffect(() => useSessionStore.subscribe((next, previous) => {
    if (next.connected !== previous.connected || next.accountGeneration !== previous.accountGeneration ||
        next.dispatchGeneration !== previous.dispatchGeneration) {
      // This runs synchronously even when React batches disconnect/reconnect
      // into one render with the same final connection and service identity.
      reading.current++;
      setPage(null);
      setRefresh((value) => value + 1);
    }
  }), []);

  const load = useCallback(async (afterSeq?: number, previous?: NativeDetailPage) => {
    const request = ++reading.current;
    const context = useSessionStore.getState();
    const current = () => {
      const state = useSessionStore.getState();
      return reading.current === request && state.accountGeneration === context.accountGeneration &&
        state.dispatchGeneration === context.dispatchGeneration;
    };
    if (anchorSeq === undefined || !context.token) return;
    setBusy(true); setError("");
    try {
      const query = new URLSearchParams();
      if (afterSeq !== undefined) query.set("after_seq", String(afterSeq));
      const response = await accountFetch(`/api/sessions/${encodeURIComponent(sessionId)}/native-details/${anchorSeq}?${query}`, {
        headers: { Authorization: `Bearer ${context.token}` },
      });
      if (!response.ok) throw new Error(`Could not load saved details (HTTP ${response.status}).`);
      const value: NativeDetailPage = await response.json();
      if (!current()) return;
      if (value.anchor_seq !== anchorSeq) throw new Error("Details belong to a different task.");
      const messages = previous ? [...previous.messages, ...value.messages.filter((row) =>
        !previous.messages.some((saved) => saved.seq === row.seq))] : value.messages;
      setPage({ ...value, messages });
    } catch (failure) {
      if (current()) setError(failure instanceof Error ? failure.message : "Could not load saved details.");
    } finally { if (current()) setBusy(false); }
  }, [sessionId, anchorSeq]);
  useEffect(() => {
    const reads = reading;
    setPage(null); setBusy(false); setError("");
    void load();
    return () => { reads.current++; };
  }, [load, refresh]);

  return <section aria-label="Saved helper details" className="space-y-2 border-t border-gray-300 pt-2 text-xs">
    <div className="flex items-center justify-between gap-2">
      <strong>Saved helper details</strong>
      <button type="button" onClick={() => { reading.current++; setRefresh((value) => value + 1); }}>Refresh details</button>
    </div>
    <p className="text-gray-700">Collected content only. Helper completion does not confirm that its parent has consumed the result.</p>
    {anchorSeq === undefined && <p>No saved reference is available for this helper yet.</p>}
    {busy && <p role="status">Loading saved details…</p>}
    {error && <p role="alert" className="text-destructive">{error} Refresh to try again.</p>}
    {page?.availability === "not_collected" && <p>Helper history was not collected. Viewing it cannot recover missing content.</p>}
    {page && <p>Observed helper state: {page.child.status}{page.child.stop_reason ? ` (${page.child.stop_reason})` : ""}</p>}
    {page?.messages.map((message) => <div key={message.seq} className="rounded border border-gray-300 p-2">
      <p className="font-mono text-[10px] text-gray-600">{message.type}{message.tool_name ? ` · ${message.tool_name}` : ""}{message.is_error ? " · failed" : ""}</p>
      {message.tool_input && <pre className="whitespace-pre-wrap break-words">{JSON.stringify(message.tool_input, null, 2)}</pre>}
      {message.content !== null && message.content !== undefined && <pre className="whitespace-pre-wrap break-words">{
        typeof message.content === "string" ? message.content : JSON.stringify(message.content, null, 2)
      }</pre>}
    </div>)}
    {page?.has_more && page.next_after_seq !== null && <button type="button" disabled={busy}
      onClick={() => void load(page.next_after_seq!, page)}>More saved items</button>}
  </section>;
}
