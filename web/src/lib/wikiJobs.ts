import type { WikiJobCard } from "../api";
import { useSessionStore } from "../stores/sessionStore";

const API = window.location.origin;

function cardsFrom(value: unknown): WikiJobCard[] {
  if (!Array.isArray(value) || value.some((card) => !card ||
    typeof card.job_id !== "string" || typeof card.vault_id !== "string" ||
    !Number.isInteger(card.state_version) || card.state_version < 1 ||
    !Number.isInteger(card.attempt) || card.attempt < 0 ||
    typeof card.status !== "string" || typeof card.scope !== "string" ||
    typeof card.received_at !== "string" || !card.progress ||
    ["total", "completed", "attention", "failed"].some((key) =>
      !Number.isInteger(card.progress[key]) || card.progress[key] < 0) ||
    !Array.isArray(card.outcome_refs) || card.outcome_refs.some((ref: Record<string, unknown>) =>
      !ref || typeof ref.path !== "string" || typeof ref.revision !== "string") ||
    !["none", "pending", "unconfirmed", "confirmed"].includes(card.settlement_phase))) {
    throw new Error("Wiki maintenance returned an invalid state snapshot.");
  }
  return value as WikiJobCard[];
}

type Target = Pick<WikiJobCard, "job_id" | "vault_id" | "state_version">;

async function load(sessionId: string, offset: number, target?: Target): Promise<number> {
  // Capture both coordinates before the request. A later generation getter
  // would bless a response from the service or account that just retired.
  const { token, dispatchGeneration } = useSessionStore.getState();
  if (!token) return 0;
  const query = new URLSearchParams({ offset: String(offset), limit: "30" });
  if (target) { query.set("job_id", target.job_id); query.set("vault_id", target.vault_id); }
  const response = await fetch(`${API}/api/sessions/${encodeURIComponent(sessionId)}/wiki-jobs?${query}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const state = useSessionStore.getState();
  if (state.token !== token || state.dispatchGeneration !== dispatchGeneration) return 0;
  if (!response.ok) throw new Error(`Could not load Wiki maintenance (HTTP ${response.status}).`);
  const cards = cardsFrom(await response.json());
  if (target && cards.some((card) => card.job_id !== target.job_id || card.vault_id !== target.vault_id)) {
    throw new Error("Wiki maintenance returned a different task identity.");
  }
  const current = useSessionStore.getState();
  if (current.token !== token || current.dispatchGeneration !== dispatchGeneration) return 0;
  current.mergeWikiJobs(sessionId, cards);
  return cards.length;
}

const loading = new Map<string, { token: string; generation: string | null; again: boolean; promise: Promise<number> }>();

export function reconcileWikiJobs(sessionId: string): Promise<number> {
  const state = useSessionStore.getState();
  // A disconnected browser may have missed the only invalidation. Recheck
  // retained work by identity even when newer cards fill the first page.
  for (const card of state.wikiJobs[sessionId] || []) {
    if (card.status !== "completed" || card.settlement_phase === "pending" ||
      card.settlement_phase === "unconfirmed" || (card.recording_pending ?? 0) > 0 ||
      (card.effects?.index_pending ?? 0) > 0) {
      state.invalidateWikiJob(sessionId, card);
    }
  }
  return refreshWikiJobs(sessionId);
}

export function refreshWikiJobs(sessionId: string, offset = 0): Promise<number> {
  if (offset) return load(sessionId, offset);
  const { token, dispatchGeneration: generation } = useSessionStore.getState();
  const previous = loading.get(sessionId);
  if (previous && previous.token === token && previous.generation === generation) {
    previous.again = true;
    return previous.promise;
  }
  const pending = { token, generation, again: false, promise: Promise.resolve(0) };
  pending.promise = (async () => {
    let count = 0;
    do {
      pending.again = false;
      count = await load(sessionId, 0);
      const dirty = useSessionStore.getState().wikiJobDirty[sessionId] || {};
      for (const target of Object.values(dirty)) {
        const state = useSessionStore.getState();
        if (state.token !== token || state.dispatchGeneration !== generation) return count;
        await load(sessionId, 0, target);
      }
      const current = useSessionStore.getState();
      if (current.token !== token || current.dispatchGeneration !== generation) break;
    } while (pending.again);
    return count;
  })().finally(() => {
    if (loading.get(sessionId) === pending) loading.delete(sessionId);
  });
  loading.set(sessionId, pending);
  return pending.promise;
}

export async function controlWikiJob(sessionId: string, card: WikiJobCard, action: "cancel" | "retry"): Promise<void> {
  const { token } = useSessionStore.getState();
  if (!token) throw new Error("Sign in before controlling Wiki maintenance.");
  useSessionStore.getState().invalidateWikiJob(sessionId, card);
  const response = await fetch(`${API}/api/wiki/jobs/${encodeURIComponent(card.job_id)}/${action}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ expected_attempt: card.attempt, expected_vault_id: card.vault_id }),
  });
  if (useSessionStore.getState().token !== token) return;
  try { await refreshWikiJobs(sessionId); }
  catch (failure) { if (response.ok) throw failure; }
  if (!response.ok) {
    throw new Error(response.status === 409
      ? "The task changed or its original execution is not settled. Review current state before trying again."
      : `Wiki maintenance could not ${action} (HTTP ${response.status}).`);
  }
}
