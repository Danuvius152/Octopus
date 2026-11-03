import { useCallback, useEffect, useRef, useState } from "react";
import { accountFetch } from "../lib/accountFetch";
import { isSessionBusy } from "../lib/deferredFork";
import { sessions as sessionList } from "../lib/sessionList";
import { selectSession } from "../lib/selectSession";
import { useSessionStore } from "../stores/sessionStore";

type Intent = { label: string | null };
interface Operation {
  intent: Intent;
  posting: boolean;
  retries: number;
  timer?: ReturnType<typeof setTimeout>;
}

/** Own the reset receipt / deferred-fork ordering, never another task store. */
export function useSessionFork() {
  const pending = useSessionStore((s) => s.pendingForks);
  const sessions = useSessionStore((s) => s.sessions);
  const dispatch = useSessionStore((s) => s.dispatchStates);
  const unavailable = useSessionStore((s) => s.dispatchUnavailable);
  const questions = useSessionStore((s) => s.pendingQuestions);
  const operations = useRef(new Map<string, Operation>());
  const resets = useRef(new Set<{ sessionId: string }>());
  const mounted = useRef(true);
  const observers = useRef(new Set<() => void>());
  const [tick, wake] = useState(0);

  const clearIntent = useCallback((sid: string, intent: Intent) => {
    if (useSessionStore.getState().pendingForks[sid] === intent)
      useSessionStore.getState().clearPendingFork(sid);
  }, []);

  const cancelFork = useCallback((sid: string) => {
    useSessionStore.getState().clearPendingFork(sid);
    const operation = operations.current.get(sid);
    if (operation?.timer !== undefined) clearTimeout(operation.timer);
    if (!operation?.posting) operations.current.delete(sid);
  }, []);

  useEffect(() => {
    mounted.current = true;
    const active = operations.current;
    const watching = observers.current;
    return () => {
      mounted.current = false;
      for (const finish of watching) finish();
      watching.clear();
      for (const [sid, operation] of active) {
        if (operation.timer !== undefined) clearTimeout(operation.timer);
        // Stop observing a POST without replaying it on the next mount. The
        // server may already have accepted it; its WS/list receipt remains real.
        if (operation.posting) clearIntent(sid, operation.intent);
      }
      active.clear();
    };
  }, [clearIntent]);

  const queueFork = useCallback((sid: string, label: string | null) => {
    const state = useSessionStore.getState();
    const queue = state.dispatchStates[sid];
    const unknown = !queue || state.dispatchUnavailable[sid];
    const busy = unknown || isSessionBusy(state.sessions.find((s) => s.id === sid)?.status,
      queue.items.length, state.pendingQuestions[sid]?.length ?? 0, queue.paused);
    state.setPendingFork(sid, label);
    state.addMessage(sid, { role: "system", type: "notice", content: unknown
      ? "⑂ Fork queued — waiting for current processing state."
      : queue.paused ? "⑂ Fork queued — it will run after processing resumes and this session is idle."
      : busy ? "⑂ Fork queued — it'll run when this session goes idle."
      : "⑂ Forking — copying the working directory…" });
  }, []);

  const reset = useCallback(async (sid: string): Promise<{ error: string | null } | undefined> => {
    const start = useSessionStore.getState();
    const questionIds = (start.pendingQuestions[sid] ?? []).map((q) => q.question_id);
    const fork = start.pendingForks[sid];
    const marker = { sessionId: sid };
    resets.current.add(marker);
    let current = true;
    const unsubscribe = useSessionStore.subscribe((state) => {
      if (state.accountGeneration !== start.accountGeneration || state.dispatchGeneration !== start.dispatchGeneration ||
          state.activeSessionId !== sid || state.mainView !== start.mainView) {
        current = false;
        if (resets.current.delete(marker) && mounted.current) wake((n) => n + 1);
      }
    });
    const stop = () => { current = false; unsubscribe(); };
    observers.current.add(stop);
    try {
      const response = await accountFetch(`${window.location.origin}/api/sessions/${sid}/reset`, {
        method: "POST", headers: { Authorization: `Bearer ${start.token}` },
      });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        const reason = typeof body?.detail === "string" ? body.detail : body?.detail?.message;
        throw new Error(reason || `Reset failed (HTTP ${response.status}).`);
      }
      if (!current || !mounted.current) return;
      const store = useSessionStore.getState();
      for (const id of questionIds) store.removePendingQuestion(sid, id);
      if (fork && store.pendingForks[sid] === fork) cancelFork(sid);
      return { error: null };
    } catch (error) {
      if (current && mounted.current)
        return { error: `Couldn't reset session: ${error instanceof Error ? error.message : "Network error."}` };
    } finally {
      unsubscribe();
      observers.current.delete(stop);
      resets.current.delete(marker);
      if (mounted.current) wake((n) => n + 1);
    }
  }, [cancelFork]);

  useEffect(() => {
    for (const [sid, operation] of operations.current) {
      if (!operation.posting && pending[sid] !== operation.intent) {
        if (operation.timer !== undefined) clearTimeout(operation.timer);
        operations.current.delete(sid);
      }
    }
    for (const [sid, intent] of Object.entries(pending)) {
      if (useSessionStore.getState().pendingForks[sid] !== intent) continue;
      if ([...resets.current].some((marker) => marker.sessionId === sid)) continue;
      let operation = operations.current.get(sid);
      if (operation?.posting || operation?.timer !== undefined) continue;
      const session = sessions.find((s) => s.id === sid);
      if (!session) { clearIntent(sid, intent); continue; }
      const queue = dispatch[sid];
      if (!queue || unavailable[sid] || isSessionBusy(session.status, queue.items.length,
          questions[sid]?.length ?? 0, queue.paused)) continue;
      operation ??= { intent, posting: false, retries: 0 };
      operation.posting = true;
      operations.current.set(sid, operation);
      const original = operation;
      const release = () => {
        if (operations.current.get(sid) === original) operations.current.delete(sid);
      };
      const start = useSessionStore.getState();
      const lifetime = sessionList.observe(sid, start);
      let observing = true;
      const current = () => observing && mounted.current && sessionList.current(lifetime.observation);
      const ownsIntent = () => current() && useSessionStore.getState().pendingForks[sid] === intent;
      let foreground = start.activeSessionId === sid && start.mainView === "chat";
      const unsubscribe = useSessionStore.subscribe((state) => {
        if (state.activeSessionId !== sid || state.mainView !== start.mainView) foreground = false;
      });
      const finish = () => { unsubscribe(); lifetime.finish(); };
      const stop = () => { observing = false; finish(); };
      observers.current.add(stop);
      const notice = (content: string, isError = false) => {
        if (current()) useSessionStore.getState().addMessage(sid, { role: "system", type: "notice", content, is_error: isError });
      };
      void (async () => {
        let retry = false;
        try {
          const response = await accountFetch(`${window.location.origin}/api/sessions/${sid}/duplicate`, {
            method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${start.token}` },
            body: JSON.stringify({ label: intent.label ?? undefined }),
          });
          if (!response.ok) {
            const body = await response.json().catch(() => null);
            const detail = body?.detail;
            if (!ownsIntent()) return;
            if (response.status === 409 && detail?.reason === "fork_blocked_parent_turn_active") {
              retry = true;
              return;
            }
            notice(typeof detail === "string" ? detail : detail?.message || `Couldn't fork (HTTP ${response.status}).`, true);
            return;
          }
          const fork = await response.json();
          if (!current()) return;
          const completing = ownsIntent();
          if (completing) clearIntent(sid, intent);
          const accepted = await sessionList.created(fork, lifetime.observation);
          if (!accepted || !current() || !completing || useSessionStore.getState().pendingForks[sid]) return;
          if (foreground) await selectSession(fork.id, fork.agent_id);
          else notice(`⑂ Fork ready — “${fork.name}”. Open it from the sidebar.`);
        } catch {
          if (ownsIntent()) notice("Couldn't fork — network error.", true);
        } finally {
          finish();
          observers.current.delete(stop);
          original.posting = false;
          if (retry && ownsIntent()) {
            original.retries++;
            if (original.retries > 6) {
              notice("Couldn't fork — the session stayed busy. Try /fork again.", true);
              clearIntent(sid, intent);
              release();
            } else {
              original.timer = setTimeout(() => {
                original.timer = undefined;
                if (mounted.current) wake((n) => n + 1);
              }, Math.min(8000, 400 * 2 ** original.retries));
            }
          } else {
            // Also retire observation after a scope change; an unconfirmed POST
            // must never be replayed automatically. Reference equality preserves
            // a replacement intent even if the account/service changed meanwhile.
            clearIntent(sid, intent);
            release();
          }
          if (mounted.current) wake((n) => n + 1);
        }
      })();
    }
  }, [pending, sessions, dispatch, unavailable, questions, tick, clearIntent]);

  return { queueFork, cancelFork, reset };
}
