import { useEffect, useMemo, useRef, useState } from "react";
import { accountFetch } from "../lib/accountFetch";
import { sessions } from "../lib/sessionList";
import { useSessionStore } from "../stores/sessionStore";

type Start = ReturnType<typeof useSessionStore.getState>;
interface Feedback { message: string; failed: boolean }
interface Read { done: Promise<unknown>; cancel: () => void }

/** Card-local control feedback, independent of execution and return delivery. */
export function useTaskControl(sessionId: string, identity: string) {
  const account = useSessionStore((s) => s.accountGeneration);
  const token = useSessionStore((s) => s.token);
  const generation = useSessionStore((s) => s.dispatchGeneration);
  const selected = useSessionStore((s) => s.activeSessionId);
  const connected = useSessionStore((s) => s.connected);
  // Returning to the same session or connection value is a new view lifetime.
  // Its feedback cannot inherit an operation from before leaving.
  const scope = useMemo(() => ({ sessionId, identity, account, token, generation, selected, connected }),
    [sessionId, identity, account, token, generation, selected, connected]);
  type Operation = { scope: typeof scope; read?: Read };
  const [feedback, setFeedback] = useState<{ scope: typeof scope; pending: boolean; result?: Feedback }>();
  const operation = useRef<Operation | null>(null);
  useEffect(() => () => {
    const original = operation.current;
    if (original?.scope !== scope) return;
    operation.current = null;
    const read = original.read;
    original.read = undefined;
    read?.cancel();
  }, [scope]);

  const execute = async (work: (current: () => boolean, start: Start, own: (read: Read) => void) => Promise<Feedback>, unconfirmedMessage: string) => {
    if (operation.current?.scope === scope) return;
    const original: Operation = { scope };
    operation.current = original;
    const started = useSessionStore.getState();
    const lifetime = sessions.observe(sessionId, started);
    const current = () => operation.current === original &&
      sessions.current(lifetime.observation) && useSessionStore.getState().activeSessionId === started.activeSessionId;
    setFeedback({ scope, pending: true });
    try {
      const result = await work(current, started, (read) => { original.read = read; });
      if (current()) setFeedback({ scope, pending: false, result });
    } catch {
      if (current()) setFeedback({ scope, pending: false, result: { message: unconfirmedMessage, failed: true } });
    } finally {
      original.read?.cancel();
      original.read = undefined;
      lifetime.finish();
      if (operation.current === original) operation.current = null;
    }
  };

  const cancel = (url: string, body?: object, consume?: (value: unknown, start: Start) => void) => execute(async (current, start) => {
    const response = await accountFetch(url, {
      method: "POST", headers: { Authorization: `Bearer ${start.token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(30_000),
    });
    if (!response.ok) {
      const label = response.status === 409 ? "Cancellation conflicts with the current task state"
        : response.status === 503 ? "Cancellation is temporarily unavailable" : "Cancellation failed";
      let message = `${label} (HTTP ${response.status}).`;
      try {
        const value = await response.json();
        if (typeof value?.detail === "string") message += ` ${value.detail}`;
      } catch { /* Keep the status when the body cannot be read. */ }
      return { message, failed: true };
    }
    if (consume) {
      const value: unknown = await response.json();
      if (current()) consume(value, start);
    }
    return { message: "Cancellation requested. Refresh status to confirm the result.", failed: false };
  }, "Cancellation result is unconfirmed; refresh current status.");

  const refresh = (read: () => Read) => execute(async (_current, _start, own) => {
    const observation = read();
    own(observation);
    await observation.done;
    return { message: "Current status refreshed.", failed: false };
  }, "Could not confirm current status. Refresh again.");

  return { pending: feedback?.scope === scope && feedback.pending,
    result: feedback?.scope === scope ? feedback.result : undefined, cancel, refresh };
}
