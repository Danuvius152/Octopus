import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { NativeDetailPage } from "../api";
import { accountFetch } from "../lib/accountFetch";
import { useSessionStore } from "../stores/sessionStore";
import { MessageDetails, SavedMessageRecord } from "./MessageDetails";
import { displayToolInput } from "../lib/messagePresentation";
import { ToolInputDetails } from "./ToolInputDetails";

/** A separate read view: it never merges detail pages into chat or usage. */
export function NativeSubagentDetails({ sessionId, anchorSeq }: { sessionId: string; anchorSeq?: number }) {
  const [refresh, setRefresh] = useState(0);
  const reading = useRef(0);
  // The existing synchronous subscription wakes a new reader on scope changes,
  // including disconnect/reconnect in one React batch. Capture that scope in
  // the owner too: an old UI callback cannot borrow the new store's identity.
  const identity = useMemo(() => {
    const state = useSessionStore.getState();
    return { sessionId, anchorSeq, refresh, retired: false, account: state.accountGeneration, connected: state.connected,
      service: state.dispatchGeneration, window: state.messageWindows[sessionId] };
  }, [sessionId, anchorSeq, refresh]);
  const currentIdentity = useRef(identity);
  currentIdentity.current = identity;
  const [detail, setDetail] = useState<{ owner: typeof identity; page: NativeDetailPage | null; error: string; busy: boolean }>(
    { owner: identity, page: null, error: "", busy: false });
  const currentResource = useCallback(() => {
    const state = useSessionStore.getState();
    return !identity.retired && currentIdentity.current === identity && state.accountGeneration === identity.account &&
      state.connected === identity.connected && state.dispatchGeneration === identity.service &&
      state.messageWindows[sessionId] === identity.window;
  }, [identity, sessionId]);
  const visible = detail.owner === identity && currentResource();
  const page = visible ? detail.page : null;
  const error = visible ? detail.error : "";
  const busy = visible && detail.busy;

  useEffect(() => useSessionStore.subscribe((next, previous) => {
    const session = currentIdentity.current.sessionId;
    if (next.connected !== previous.connected || next.accountGeneration !== previous.accountGeneration ||
        next.dispatchGeneration !== previous.dispatchGeneration || next.messageWindows[session] !== previous.messageWindows[session]) {
      // This runs synchronously even when React batches disconnect/reconnect
      // into one render with the same final connection and service identity.
      // Retire the reader itself, so old controls cannot start a new request
      // while React's successor commit is pending and scope values return.
      currentIdentity.current.retired = true;
      reading.current++;
      setRefresh((value) => value + 1);
    }
  }), []);

  const load = useCallback(async (afterSeq?: number, previous?: NativeDetailPage) => {
    if (!currentResource()) return;
    const request = ++reading.current;
    const context = useSessionStore.getState();
    const current = () => reading.current === request && currentResource();
    if (anchorSeq === undefined || !context.token) {
      setDetail({ owner: identity, page: null, error: "", busy: false });
      return;
    }
    setDetail({ owner: identity, page: previous ?? null, error: "", busy: true });
    let nextPage = previous ?? null;
    let failureMessage = "";
    try {
      const query = new URLSearchParams();
      query.set("view", "browser");
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
      nextPage = { ...value, messages };
    } catch (failure) {
      failureMessage = failure instanceof Error ? failure.message : "Could not load saved details.";
    } finally { if (current()) setDetail({ owner: identity, page: nextPage, error: failureMessage, busy: false }); }
  }, [sessionId, anchorSeq, identity, currentResource]);
  useEffect(() => {
    const reads = reading;
    void load();
    return () => { if (currentIdentity.current === identity) reads.current++; };
  }, [load, identity]);

  return <section aria-label="Saved helper details" className="space-y-2 border-t border-gray-300 pt-2 text-xs">
    <div className="flex items-center justify-between gap-2">
      <strong>Saved helper details</strong>
      <button type="button" onClick={() => {
        if (!currentResource()) return;
        identity.retired = true;
        reading.current++; setRefresh((value) => value + 1);
      }}>Refresh details</button>
    </div>
    <p className="text-gray-700">Collected content only. Helper completion does not confirm that its parent has consumed the result.</p>
    {anchorSeq === undefined && <p>No saved reference is available for this helper yet.</p>}
    {busy && <p role="status">Loading saved details…</p>}
    {error && <p role="alert" className="text-destructive">{error} Refresh to try again.</p>}
    {page?.availability === "not_collected" && <p>Helper history was not collected. Viewing it cannot recover missing content.</p>}
    {page && <p>Observed helper state: {page.child.status}{page.child.stop_reason ? ` (${page.child.stop_reason})` : ""}</p>}
    {page?.messages.map((message) => <div key={`${sessionId}:${anchorSeq}:${refresh}:${message.seq}`} className="rounded border border-gray-300 p-2">
      <MessageDetails sessionId={sessionId} message={message}>{(shown) => shown !== message &&
        "identity_unavailable" in message && message.identity_unavailable === true
        ? <SavedMessageRecord message={shown} label="Full saved helper record" /> : <>
        <p className="font-mono text-[10px] text-gray-600">{shown.type}{shown.tool_name ? ` · ${shown.tool_name}` : ""}{shown.is_error ? " · failed" : ""}</p>
        {shown.tool_input && (shown.type === "tool_use" ? <ToolInputDetails message={shown} /> :
          <pre className="whitespace-pre-wrap break-words">{JSON.stringify(displayToolInput(shown.tool_input, shown.content), null, 2)}</pre>)}
        {shown.content !== null && shown.content !== undefined && <pre className="whitespace-pre-wrap break-words">{
          typeof shown.content === "string" ? shown.content : JSON.stringify(shown.content, null, 2)
        }</pre>}
      </>}</MessageDetails>
    </div>)}
    {page?.has_more && page.next_after_seq !== null && <button type="button" disabled={busy}
      onClick={() => void load(page.next_after_seq!, page)}>More saved items</button>}
  </section>;
}
