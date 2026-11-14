import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { accountFetch } from "../lib/accountFetch";
import { IDLE, historyFromMessages, reconcileHistory, recallNext, recallPrev, type HistoryEntry } from "../lib/composerHistory";
import { isAutoInjectedPrompt } from "../lib/injectedTurns";
import type { TextDraft } from "../lib/textDrafts";

interface HistoryRead {
  controller: AbortController;
  account: number;
  sessionId: string;
  entry: HistoryEntry;
  version: number;
  text: string;
}

/** The composer owns one walk and at most one selected-message body read. */
export function useComposerHistory(draft: TextDraft, textarea: RefObject<HTMLTextAreaElement | null>) {
  const history = useRef(IDLE);
  const read = useRef<HistoryRead | null>(null);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState("");
  const retireRead = useCallback(() => {
    read.current?.controller.abort();
    read.current = null;
    setWaiting(false);
  }, []);
  const reset = useCallback(() => {
    retireRead();
    history.current = IDLE;
    setError("");
  }, [retireRead]);

  useEffect(() => {
    const sessionSubscription = useSessionStore.subscribe((state, before) => {
      if (state.activeSessionId !== before.activeSessionId || state.accountGeneration !== before.accountGeneration) reset();
      else if (history.current.entry && state.activeSessionId && state.messages[state.activeSessionId] !== before.messages[state.activeSessionId]) {
        history.current = reconcileHistory(historyFromMessages(state.messages[state.activeSessionId] ?? [], isAutoInjectedPrompt), history.current);
        if (!history.current.entry) reset();
      }
    });
    const draftSubscription = draft.store.subscribe((state, before) => {
      // Identity restoration can replace text without incrementing its version.
      if (state.version !== before.version || state.text !== before.text) reset();
    });
    return () => {
      sessionSubscription();
      draftSubscription();
      read.current?.controller.abort();
      read.current = null;
      history.current = IDLE;
    };
  }, [draft, reset]);

  const write = (text: string) => {
    // Our synchronous write preserves selection; other draft writers end it.
    const selection = history.current;
    draft.edit(text);
    history.current = selection;
    const version = draft.store.getState().version;
    const element = textarea.current;
    requestAnimationFrame(() => {
      if (draft.current() && history.current === selection && draft.store.getState().version === version && textarea.current === element) {
        element?.setSelectionRange(text.length, text.length);
      }
    });
  };

  const recall = (direction: "up" | "down", current: string): boolean => {
    const state = useSessionStore.getState();
    if (!state.activeSessionId || !draft.current()) return false;
    const entries = historyFromMessages(state.messages[state.activeSessionId] ?? [], isAutoInjectedPrompt);
    const selected = direction === "up" ? recallPrev(entries, history.current, current) : recallNext(entries, history.current, current);
    if (!selected) return false;
    // Repeating Up at the oldest candidate keeps its loaded body or active read.
    if (selected.state.entry && selected.state.entry.identity === history.current.entry?.identity) return true;
    retireRead();
    setError("");
    history.current = selected.state;
    const entry = selected.state.entry;
    if (entry?.body_state !== "preview") {
      write(selected.text);
      return true;
    }
    const { version, text } = draft.store.getState();
    const operation: HistoryRead = { controller: new AbortController(), account: state.accountGeneration,
      sessionId: state.activeSessionId, entry, version, text };
    read.current = operation;
    setWaiting(true);
    const currentRead = () => {
      const now = useSessionStore.getState(), value = draft.store.getState();
      return read.current === operation && !operation.controller.signal.aborted && draft.current() &&
        now.accountGeneration === operation.account && now.activeSessionId === operation.sessionId &&
        history.current.entry?.identity === operation.entry.identity && value.version === operation.version && value.text === operation.text;
    };
    void (async () => {
      try {
        const expected = `/api/sessions/${encodeURIComponent(operation.sessionId)}/messages/${entry.seq}`;
        if (!state.token || !Number.isSafeInteger(entry.seq) || entry.seq! < 0 || entry.detail_ref !== expected) {
          throw new Error("No complete saved history message reference is available.");
        }
        const response = await accountFetch(`${expected}?view=browser`, { headers: { Authorization: `Bearer ${state.token}` },
          signal: AbortSignal.any([operation.controller.signal, AbortSignal.timeout(30_000)]), cache: "no-store" });
        if (!response.ok) throw new Error(`Could not load full history message (HTTP ${response.status}).`);
        const body = await response.json();
        if (!currentRead()) return;
        if (!body || body.seq !== entry.seq || body.role !== "user" || body.type !== "text" ||
            body.body_state === "preview" || typeof body.content !== "string") {
          throw new Error("The saved history message does not match this reference.");
        }
        retireRead();
        write(body.content);
      } catch (failure) {
        if (currentRead()) {
          retireRead();
          setError(failure instanceof Error ? failure.message : "Could not load full history message.");
        }
      }
    })();
    return true;
  };

  return { recall, reset, waiting, error, isWaiting: () => read.current !== null };
}
