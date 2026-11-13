import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { IconArrowsMaximize, IconArrowsMinimize } from "@tabler/icons-react";
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
  const identity = useMemo(() => ({ account, sessionId, seq, detailRef, messageWindow }), [account, sessionId, seq, detailRef, messageWindow]);
  const read = useRef<{ owner: typeof identity; controller: AbortController } | null>(null);
  const currentIdentity = useRef(identity);
  currentIdentity.current = identity;
  const [detail, setDetail] = useState<{ owner: typeof identity; body: MessageInput | null; error: string; busy: boolean }>(
    { owner: identity, body: null, error: "", busy: false });
  useEffect(() => {
    setDetail((previous) => previous.owner === identity ? previous : { owner: identity, body: null, error: "", busy: false });
    return () => {
      if (read.current?.owner === identity) { read.current.controller.abort(); read.current = null; }
    };
  }, [identity]);
  useEffect(() => useSessionStore.subscribe((next) => {
    const operation = read.current;
    if (operation && (next.accountGeneration !== operation.owner.account ||
        next.messageWindows[operation.owner.sessionId] !== operation.owner.messageWindow)) operation.controller.abort();
  }), []);
  const currentResource = () => {
    const state = useSessionStore.getState();
    return currentIdentity.current === identity && state.accountGeneration === account && state.messageWindows[sessionId] === messageWindow;
  };
  const load = async () => {
    if (!currentResource()) return;
    read.current?.controller.abort();
    const controller = new AbortController();
    const operation = { owner: identity, controller };
    read.current = operation;
    const context = useSessionStore.getState();
    const current = () => !controller.signal.aborted && read.current === operation && currentResource();
    setDetail((previous) => ({ owner: identity, body: previous.owner === identity ? previous.body : null, error: "", busy: true }));
    let body: MessageInput | null = null;
    let error = "";
    try {
      const expected = `/api/sessions/${encodeURIComponent(sessionId)}/messages/${seq}`;
      if (!context.token || seq === undefined || detailRef !== expected) throw new Error("No complete saved message reference is available.");
      const response = await accountFetch(`${expected}?view=browser`, { headers: { Authorization: `Bearer ${context.token}` },
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30_000)]), cache: "no-store" });
      if (!response.ok) throw new Error(`Could not load full message (HTTP ${response.status}).`);
      const value: MessageInput = await response.json();
      if (!current()) return;
      if (!value || value.seq !== seq || !["user", "assistant", "system", "tool"].includes(value.role) || typeof value.type !== "string") {
        throw new Error("The saved message does not match this reference.");
      }
      body = value;
    } catch (failure) {
      error = failure instanceof Error ? failure.message : "Could not load full message.";
    } finally {
      if (current()) setDetail((previous) => ({ owner: identity,
        body: body ?? (previous.owner === identity ? previous.body : null), error, busy: false }));
    }
  };
  return { body: detail.owner === identity ? detail.body : null,
    error: detail.owner === identity ? detail.error : "", busy: detail.owner === identity && detail.busy, load };
}

interface SavedMessageReference {
  seq?: number | null; body_state?: "inline" | "preview"; detail_ref?: string | null; identity_unavailable?: boolean;
}

export function MessageDetails<T extends SavedMessageReference>({ sessionId, message, children, expandWithMessage = false }: {
  sessionId: string; message: T; expandWithMessage?: boolean;
  children: (message: T | MessageInput, loadDetail?: () => void) => ReactNode;
}) {
  const detail = useMessageDetail(sessionId, message.detail_ref, message.seq ?? undefined);
  const [expanded, setExpanded] = useState(true);
  const preview = message.body_state === "preview";
  const displayed = preview && expanded && detail.body;
  const load = () => {
    setExpanded(true);
    if (!detail.body && !detail.busy) void detail.load();
  };
  const action = displayed ? "Show preview" : "View full message";
  return <div className={preview ? `relative group/message${expandWithMessage ? "" : " min-h-12 pr-10"}` : undefined}>
    {children(displayed || message, preview && expandWithMessage ? load : undefined)}
    {preview && !expandWithMessage && <button type="button" aria-label={action} title={action} aria-expanded={Boolean(displayed)}
      className="absolute top-6 right-2 rounded p-1 text-muted-foreground bg-card shadow-sm opacity-0 group-hover/message:opacity-100 focus-visible:opacity-100 [@media(hover:none)]:opacity-100 hover:text-foreground"
      disabled={detail.busy} onClick={() => { if (displayed) setExpanded(false); else load(); }}>
      {displayed ? <IconArrowsMinimize size={14} /> : <IconArrowsMaximize size={14} />}
    </button>}
    {preview && (detail.busy || detail.error) && <div className="text-xs text-muted-foreground space-y-1">
      {detail.busy && <p role="status">Loading full message…</p>}
      {detail.error && <><p role="alert">{detail.error}</p>
        <button type="button" className="underline" disabled={detail.busy} aria-expanded={Boolean(displayed)} onClick={load}>Retry full message</button></>}
    </div>}
  </div>;
}
