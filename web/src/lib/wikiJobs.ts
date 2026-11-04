import type { WikiJobCard, WikiJobDetail, WikiJobOutput } from "../api";
import { useSessionStore } from "../stores/sessionStore";

const API = window.location.origin;
// Match the existing application/schedule list contract. The deadline covers
// headers AND the decoded body, even if a transport ignores abort.
const READ_TIMEOUT_MS = 30_000;
type AccountScope = Pick<ReturnType<typeof useSessionStore.getState>, "token" | "accountGeneration" | "dispatchGeneration">;
type ReadScope = AccountScope & { observation: number };
let observation = 0;
// Controls survive a reconnect, but cannot re-enter a page after it was left.
let controlObservation = 0;
function readScope(): ReadScope {
  const { token, accountGeneration, dispatchGeneration } = useSessionStore.getState();
  return { token, accountGeneration, dispatchGeneration, observation };
}
function sameAccountAndService(scope: AccountScope): boolean {
  const now = useSessionStore.getState();
  return now.token === scope.token && now.accountGeneration === scope.accountGeneration && now.dispatchGeneration === scope.dispatchGeneration;
}
function scopeIsCurrent(scope: ReadScope): boolean { return scope.observation === observation && sameAccountAndService(scope); }
export function wikiReadGuard(): () => boolean { const scope = readScope(); return () => scopeIsCurrent(scope); }
export function wikiControlGuard(): () => boolean {
  const original = controlObservation;
  return () => controlObservation === original;
}
class RetiredWikiRead extends Error {
  constructor() { super("Account or service changed, or the task observation ended. Reopen the task details."); }
}
interface Reading {
  scope: ReadScope; controller: AbortController; again: boolean; promise: Promise<number>;
}
const loading = new Map<string, Reading>();
const reads = new Set<AbortController>();

export function retireWikiJobs(sessionId: string, observation: Promise<number>): void {
  const pending = loading.get(sessionId);
  if (pending?.promise !== observation) return;
  loading.delete(sessionId);
  pending.controller.abort();
}

useSessionStore.subscribe((state, before) => {
  const controlChanged = state.token !== before.token || state.accountGeneration !== before.accountGeneration ||
    state.dispatchGeneration !== before.dispatchGeneration || state.activeSessionId !== before.activeSessionId || state.mainView !== before.mainView;
  if (!controlChanged && state.connected === before.connected) return;
  if (controlChanged) controlObservation++;
  observation++;
  for (const pending of loading.values()) pending.controller.abort();
  loading.clear();
  for (const controller of reads) controller.abort();
});

function readJson(url: string, scope: ReadScope, signal?: AbortSignal): Promise<{ response: Response; data: unknown }> {
  return new Promise((resolve, reject) => {
    const controller = new AbortController();
    let finished = false;
    const finish = (failure?: unknown, value?: { response: Response; data: unknown }) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer); reads.delete(controller);
      signal?.removeEventListener("abort", abort);
      if (failure) reject(failure); else resolve(value!);
    };
    const abort = () => { finish(new RetiredWikiRead()); controller.abort(); };
    const timer = setTimeout(() => {
      finish(new Error("Wiki state read timed out after 30 seconds.")); controller.abort();
    }, READ_TIMEOUT_MS);
    controller.signal.addEventListener("abort", abort, { once: true });
    signal?.addEventListener("abort", abort, { once: true });
    reads.add(controller);
    if (signal?.aborted || !scopeIsCurrent(scope)) { abort(); return; }
    void (async () => {
      const response = await fetch(url, { headers: { Authorization: `Bearer ${scope.token}` }, signal: controller.signal });
      const data: unknown = await response.json().catch((failure) => { if (response.ok) throw failure; return null; });
      if (controller.signal.aborted || !scopeIsCurrent(scope)) throw new RetiredWikiRead();
      return { response, data };
    })().then((value) => finish(undefined, value), (failure) => finish(failure));
  });
}

function readError(sessionId: string, error: string | null): void {
  useSessionStore.setState((state) => {
    const errors = { ...state.wikiJobReadErrors };
    if (error) errors[sessionId] = error; else delete errors[sessionId];
    return { wikiJobReadErrors: errors };
  });
}

export function isWikiJobComplete(card: WikiJobCard): boolean {
  return card.current_state !== "unavailable" && card.status === "completed" && ["none", "confirmed"].includes(card.settlement_phase)
    && (card.recording_pending ?? 0) === 0 && (card.effects?.index_pending ?? 0) === 0;
}

function cardsFrom(value: unknown): WikiJobCard[] {
  if (!Array.isArray(value) || value.some((card) => !card ||
    typeof card.job_id !== "string" || typeof card.vault_id !== "string" ||
    !Number.isInteger(card.state_version) || card.state_version < 1 ||
    !Number.isInteger(card.attempt) || card.attempt < 0 ||
    typeof card.status !== "string" || typeof card.scope !== "string" ||
    typeof card.received_at !== "string" || !card.progress ||
    (card.current_state !== undefined && !["available", "unavailable"].includes(card.current_state)) ||
    ["total", "completed", "attention", "failed"].some((key) =>
      !Number.isInteger(card.progress[key]) || card.progress[key] < 0) ||
    !Array.isArray(card.outcome_refs) || card.outcome_refs.some((ref: Record<string, unknown>) =>
      !ref || typeof ref.path !== "string" || typeof ref.revision !== "string") ||
    !["none", "pending", "attention", "unconfirmed", "confirmed"].includes(card.settlement_phase))) {
    throw new Error("Wiki maintenance returned an invalid state snapshot.");
  }
  return value as WikiJobCard[];
}

type Target = Pick<WikiJobCard, "job_id" | "vault_id" | "state_version">;

async function load(sessionId: string, offset: number, scope: ReadScope, signal?: AbortSignal, target?: Target): Promise<number> {
  // Capture both coordinates before the request. A later generation getter
  // would bless a response from the service or account that just retired.
  if (!scope.token) return 0;
  const query = new URLSearchParams({ offset: String(offset), limit: "30" });
  if (target) { query.set("job_id", target.job_id); query.set("vault_id", target.vault_id); }
  const { response, data } = await readJson(`${API}/api/sessions/${encodeURIComponent(sessionId)}/wiki-jobs?${query}`, scope, signal);
  if (signal?.aborted || !scopeIsCurrent(scope)) throw new RetiredWikiRead();
  if (!response.ok) throw new Error(`Could not load Wiki maintenance (HTTP ${response.status}).`);
  const cards = cardsFrom(data);
  if (target && cards.some((card) => card.job_id !== target.job_id || card.vault_id !== target.vault_id)) {
    throw new Error("Wiki maintenance returned a different task identity.");
  }
  const current = useSessionStore.getState();
  current.mergeWikiJobs(sessionId, cards);
  return cards.length;
}

export function reconcileWikiJobs(sessionId: string): Promise<number> {
  const state = useSessionStore.getState();
  // A disconnected browser may have missed the only invalidation. Recheck
  // retained work by identity even when newer cards fill the first page.
  for (const card of state.wikiJobs[sessionId] || []) {
    state.invalidateWikiJob(sessionId, card);
  }
  return refreshWikiJobs(sessionId);
}

export function refreshWikiJobs(sessionId: string, offset = 0, signal?: AbortSignal): Promise<number> {
  const scope = readScope();
  if (offset) return load(sessionId, offset, scope, signal);
  const previous = loading.get(sessionId);
  if (previous && scopeIsCurrent(previous.scope)) {
    previous.again = true;
    return previous.promise;
  }
  const pending: Reading = { scope, controller: new AbortController(), again: false, promise: Promise.resolve(0) };
  loading.set(sessionId, pending);
  pending.promise = (async () => {
    let count = 0;
    do {
      pending.again = false;
      count = await load(sessionId, 0, scope, pending.controller.signal);
      const dirty = useSessionStore.getState().wikiJobDirty[sessionId] || {};
      for (const target of Object.values(dirty)) {
        if (loading.get(sessionId) !== pending || !scopeIsCurrent(scope)) return count;
        await load(sessionId, 0, scope, pending.controller.signal, target);
      }
      if (loading.get(sessionId) !== pending || !scopeIsCurrent(scope)) break;
    } while (pending.again);
    if (loading.get(sessionId) === pending) readError(sessionId, null);
    return count;
  })().catch((failure) => {
    if (failure instanceof RetiredWikiRead) return 0;
    if (loading.get(sessionId) === pending && scopeIsCurrent(scope)) readError(sessionId, failure instanceof Error ? failure.message : "Could not load Wiki maintenance.");
    throw failure;
  }).finally(() => {
    if (loading.get(sessionId) === pending) loading.delete(sessionId);
  });
  return pending.promise;
}

export type WikiControlResult = { state: "accepted"; message: string; refresh: Promise<boolean> } | { state: "obsolete" };

export async function controlWikiJob(sessionId: string, card: WikiJobCard, action: "cancel" | "retry" | "verify" | "readmit",
  options: { operation_id?: string; paths?: string[]; creation?: { purpose: "requested_knowledge_page"; paths: string[] } } = {}): Promise<WikiControlResult> {
  const scope = readScope();
  if (!scope.token) throw new Error("Sign in before controlling Wiki maintenance.");
  const currentControl = wikiControlGuard();
  useSessionStore.getState().invalidateWikiJob(sessionId, card);
  let controlError: unknown;
  try {
    const response = await fetch(`${API}/api/wiki/jobs/${encodeURIComponent(card.job_id)}/${action}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${scope.token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ expected_attempt: card.attempt, expected_vault_id: card.vault_id, ...options }),
    });
    if (!response.ok) throw new Error(response.status === 409
      ? "The task changed or its original execution is not settled. Review current state before trying again."
      : `Wiki maintenance could not ${action} (HTTP ${response.status}).`);
  } catch (failure) { controlError = failure; }
  if (!currentControl()) return { state: "obsolete" };
  const currentRead = wikiReadGuard();
  // Publish POST acceptance immediately. The original GET owner retains the
  // bounded confirmation, error projection and coalescing responsibility.
  const refresh = refreshWikiJobs(sessionId).then(() => currentRead(), () => false);
  if (controlError) throw controlError;
  const accepted = { cancel: "Cancellation", retry: "Continuation", verify: "Verification", readmit: "Scope readmission" };
  return { state: "accepted", refresh, message: `${accepted[action]} request accepted for attempt ${card.attempt}.` };
}

type JobIdentity = Pick<WikiJobCard, "job_id" | "vault_id">;

class WikiJobReadError extends Error {
  readonly status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}

async function readJob<T>(card: JobIdentity, suffix: string, query: URLSearchParams, signal?: AbortSignal): Promise<T> {
  const scope = readScope();
  const { response, data } = await readJson(`${API}/api/wiki/jobs/${encodeURIComponent(card.job_id)}/${suffix}?${query}`, scope, signal);
  if (!scopeIsCurrent(scope) || signal?.aborted) throw new RetiredWikiRead();
  if (!response.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : null;
    throw new WikiJobReadError(typeof detail === "string" ? detail : `Could not read Wiki task (HTTP ${response.status}).`, response.status);
  }
  return data as T;
}

export async function loadWikiJobDetail(card: JobIdentity, attempt: number, offset = 0, signal?: AbortSignal): Promise<WikiJobDetail | null> {
  try {
    const detail = await readJob<WikiJobDetail>(card, "details", new URLSearchParams({ attempt: String(attempt), offset: String(offset) }), signal);
    if (detail.job_id !== card.job_id || detail.vault_id !== card.vault_id || detail.attempt !== attempt) throw new Error("Wiki task detail identity changed.");
    return detail;
  } catch (failure) {
    if (failure instanceof WikiJobReadError && failure.status === 404) return null;
    throw failure;
  }
}

export function loadWikiJobOutput(card: WikiJobCard, attempt: number, receipt: string, view: "history" | "current", start = 0, revision?: string, signal?: AbortSignal): Promise<WikiJobOutput> {
  const query = new URLSearchParams({ attempt: String(attempt), view, start: String(start) });
  if (revision) query.set("expected_revision", revision);
  return readJob(card, `outputs/${encodeURIComponent(receipt)}`, query, signal);
}
