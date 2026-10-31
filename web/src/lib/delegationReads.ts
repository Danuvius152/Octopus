import { accountFetch } from "./accountFetch";
import { sessions } from "./sessionList";
import { readDispatchState } from "./sessionDispatch";
import { SnapshotReads } from "./snapshotReads";
import { useSessionStore, type Delegation } from "../stores/sessionStore";

// These tokens order observations only; the store owns rounds and their facts.
const pending = new SnapshotReads();

/** A card can end observation independently of server work or other readers. */
export function observeDelegations(sessionId: string, acceptedDetail?: Promise<Record<string, unknown> | null>) {
  const start = useSessionStore.getState();
  const lifetime = sessions.observe(sessionId);
  const { observation } = lifetime;
  const read = pending.begin(sessionId);
  let active = true;
  const controller = new AbortController();
  const response = accountFetch(`${window.location.origin}/api/sessions/${encodeURIComponent(sessionId)}/delegations`, {
    headers: { Authorization: `Bearer ${start.token}` }, signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30_000)]),
  }).then(async (res) => {
    if (!res.ok) throw new Error(`Could not load delegations (HTTP ${res.status}).`);
    const rows: unknown = await res.json();
    if (!Array.isArray(rows)) throw new Error("Could not read delegations.");
    return rows as Delegation[];
  }).then((rows) => ({ rows }), (error: unknown) => ({ error }));
  const done = (async () => {
    try {
      const detail = acceptedDetail ? await acceptedDetail.catch(() => null) : undefined;
      const result = await response;
      if (!active || acceptedDetail && !detail || !sessions.current(observation, detail ? readDispatchState(detail.dispatch_state)?.generation : undefined)) return null;
      if ("error" in result) throw result.error;
      const latest = read.claim();
      useSessionStore.getState().setDelegations(sessionId, result.rows, start.delegations[sessionId] || [], !latest);
      return result.rows;
    } finally {
      read.finish();
      lifetime.finish();
    }
  })();
  return { done, cancel: () => { active = false; lifetime.finish(); controller.abort(); } };
}
