import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { accountFetch } from "../lib/accountFetch";
import type { MessageInput } from "../lib/turnOutcome";
import { useSessionStore } from "../stores/sessionStore";

/** Full historical control records stay separate from current live state. */
export function SavedMessageRecord({ message, label }: { message: { seq?: number | null }; label: string }) {
  return <section aria-label={label} className="text-xs space-y-2">
    <p>{label} · sequence {message.seq}</p>
    <pre className="whitespace-pre-wrap break-words">{JSON.stringify(message, null, 2)}</pre>
  </section>;
}

/** Only the mounted message view owns this body; it never enters shared state. */
export function useMessageDetail(sessionId: string, detailRef?: string | null, seq?: number) {
  const account = useSessionStore((state) => state.accountGeneration);
  const messageWindow = useSessionStore((state) => state.messageWindows[sessionId]);
  const [body, setBody] = useState<MessageInput | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const read = useRef<AbortController | null>(null);
  const identity = useMemo(() => ({ account, sessionId, seq, detailRef, messageWindow }), [account, sessionId, seq, detailRef, messageWindow]);
  const currentIdentity = useRef(identity);
  currentIdentity.current = identity;
  const [bodyOwner, setBodyOwner] = useState(identity);
  useEffect(() => {
    setBody(null); setError(""); setBusy(false);
    return () => { read.current?.abort(); read.current = null; };
  }, [identity]);
  useEffect(() => useSessionStore.subscribe((next, previous) => {
    if (next.accountGeneration !== previous.accountGeneration) read.current?.abort();
  }), []);
  const load = async () => {
    read.current?.abort();
    const controller = new AbortController();
    read.current = controller;
    const context = useSessionStore.getState();
    const current = () => !controller.signal.aborted && read.current === controller &&
      currentIdentity.current === identity && useSessionStore.getState().accountGeneration === context.accountGeneration;
    setBusy(true); setError("");
    try {
      const expected = `/api/sessions/${encodeURIComponent(sessionId)}/messages/${seq}`;
      if (!context.token || seq === undefined || detailRef !== expected) throw new Error("No complete saved message reference is available.");
      const response = await accountFetch(expected, { headers: { Authorization: `Bearer ${context.token}` },
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30_000)]), cache: "no-store" });
      if (!response.ok) throw new Error(`Could not load full message (HTTP ${response.status}).`);
      const value: MessageInput = await response.json();
      if (!current()) return;
      if (!value || value.seq !== seq || !["user", "assistant", "system", "tool"].includes(value.role) || typeof value.type !== "string") {
        throw new Error("The saved message does not match this reference.");
      }
      setBodyOwner(identity); setBody(value);
    } catch (failure) {
      if (current()) setError(failure instanceof Error ? failure.message : "Could not load full message.");
    } finally { if (current()) setBusy(false); }
  };
  return { body: bodyOwner === identity ? body : null, error, busy, load };
}

interface SavedMessageReference {
  seq?: number | null; body_state?: "inline" | "preview"; detail_ref?: string | null; identity_unavailable?: boolean;
}

export function MessageDetails<T extends SavedMessageReference>({ sessionId, message, children }: {
  sessionId: string; message: T; children: (message: T | MessageInput) => ReactNode;
}) {
  const detail = useMessageDetail(sessionId, message.detail_ref, message.seq ?? undefined);
  const [expanded, setExpanded] = useState(true);
  const displayed = expanded && detail.body;
  if (message.body_state !== "preview") return children(message);
  return <div className="space-y-2">
    {children(displayed || message)}
    <div className="text-xs text-muted-foreground space-y-1">
      <p>{displayed ? "Full message" : "Preview · some content is not shown"}</p>
      {message.identity_unavailable && <p>Original message associations could not fit this chat view. The full saved message is read separately.</p>}
      {detail.busy && <p role="status">Loading full message…</p>}
      {detail.error && <p role="alert">{detail.error}</p>}
      {displayed ? <button type="button" className="underline" onClick={() => setExpanded(false)}>Show preview</button> :
        <button type="button" className="underline" disabled={detail.busy} onClick={() => {
          setExpanded(true); if (!detail.body) void detail.load();
        }}>{detail.error ? "Retry full message" : "View full message"}</button>}
    </div>
  </div>;
}
