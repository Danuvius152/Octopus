import { accountFetch as fetch } from "./accountFetch";
import type { SessionInfo } from "../stores/sessionStore";
import { useSessionStore } from "../stores/sessionStore";
import { applyTranscript } from "./transcript";
import { readDispatchState } from "./sessionDispatch";
import { sessions, type SessionObservation } from "./sessionList";
import type { BgTask, PendingQuestion, ResearchJob, SessionAuxiliaryKind, SubagentRun } from "../stores/sessionStore";

const API = window.location.origin;
const SNAPSHOT_TIMEOUT_MS = 30_000;

/** Fields `GET /api/sessions/{id}` adds on top of SessionInfo. */
const DETAIL_ONLY_FIELDS = [
  "messages",
  "active_attempt",
  "dispatch_state",
  "pending_questions",
  "subagents",
  "next_message_seq",
] as const;

/** `GET /api/sessions/{id}` returns a SessionInfo superset — drop the
 * detail-only fields so the sidebar list holds a clean SessionInfo. */
export function sessionInfoFromDetail(detail: Record<string, unknown>): SessionInfo {
  const info = { ...detail };
  for (const field of DETAIL_ONLY_FIELDS) delete info[field];
  return info as unknown as SessionInfo;
}

/** Make sure `sessionId` is in the sidebar list.
 *
 * Sessions created server-side — an application's build session, a delegation
 * child, a fork — exist before any client hears about them, and the list is
 * only fetched on mount and on reconnect. Without this, opening one shows a
 * transcript the sidebar and chat header can't name. No-op when it's already
 * there.
 */
export async function hydrateSession(sessionId: string, current = () => true): Promise<void> {
  const store = useSessionStore.getState();
  if (!sessionId || !store.token) return;
  if (store.sessions.some((s) => s.id === sessionId)) return;
  try {
    await loadSessionSnapshot(sessionId, store, undefined, current);
  } catch {
    // The mount fetch / reconnect refetch will pick it up.
  }
}

type SnapshotStart = ReturnType<typeof useSessionStore.getState>;
type OnDetail = (detail: Record<string, unknown>) => void;

async function applyDetailResponse(sessionId: string, res: Response, start: SnapshotStart, observation: SessionObservation, onDetail?: OnDetail, current = () => true) {
  if (!res.ok) throw new Error(`Could not load session state (HTTP ${res.status}).`);
  const data: Record<string, unknown> = await res.json();
  const dispatch = readDispatchState(data.dispatch_state);
  if (!current() || !sessions.current(observation, dispatch?.generation)) return null;
  if (!applySessionSnapshot(sessionId, data, start)) return null;
  sessions.detail(sessionInfoFromDetail(data), observation, dispatch);
  // Adopt detail in this same synchronous step, before an account change
  // can clear the applied transcript and start a new sidebar list.
  onDetail?.(data);
  return data;
}

/** Capture the merge baseline before starting this detail request. Callers
 * with an earlier operation identity can pass that original store snapshot. */
export async function loadSessionSnapshot(sessionId: string, start = useSessionStore.getState(), onDetail?: OnDetail, current = () => true) {
  const observation = sessions.capture(start);
  try {
    const res = await fetch(`${API}/api/sessions/${sessionId}`, {
      headers: { Authorization: `Bearer ${start.token}` }, signal: AbortSignal.timeout(SNAPSHOT_TIMEOUT_MS),
    });
    return await applyDetailResponse(sessionId, res, start, observation, onDetail, current);
  } catch (error) {
    if (current() && sessions.current(observation)) useSessionStore.getState().setDispatchUnavailable(sessionId, true, start);
    throw error;
  }
}

/** Background cards merge against the live records observed before fetch. */
export async function loadBgTaskSnapshot(sessionId: string): Promise<void> {
  return observeSessionAuxiliary(sessionId, "background").done;
}

/** Cancelling a view's observation never cancels the server's job. */
export function observeSessionAuxiliary(sessionId: string, kind: SessionAuxiliaryKind) {
  let active = true;
  return {
    done: loadAuxiliarySnapshot(sessionId, kind, useSessionStore.getState(), undefined, () => active),
    cancel: () => { active = false; },
  };
}

// Outstanding reads own error ordering, never job status. Valid older successes
// may still advance a job; settled groups retain no domain state.
const auxiliaryReads: Record<SessionAuxiliaryKind, Map<string, Set<symbol>>> = {
  background: new Map(), research: new Map(),
};

async function loadAuxiliarySnapshot(
  sessionId: string, kind: SessionAuxiliaryKind, start: SnapshotStart,
  acceptedDetail?: Promise<Record<string, unknown> | null>,
  current = () => true,
): Promise<void> {
  const observation = sessions.capture(start);
  if (!start.token || !sessions.current(observation)) return;
  const group = auxiliaryReads[kind];
  const reads = group.get(sessionId) ?? new Set<symbol>();
  const read = Symbol();
  reads.add(read); group.set(sessionId, reads);
  useSessionStore.getState().setSessionAuxiliaryError(sessionId, kind, null);
  const label = kind === "background" ? "background task details" : "research updates";
  // Start alongside detail, handling rejection immediately. Only its accepted
  // service generation can authorize this paired read's eventual publication.
  const response = fetch(`${API}/api/sessions/${sessionId}/${kind === "background" ? "bg-tasks" : "research"}`, {
    headers: { Authorization: `Bearer ${start.token}` }, signal: AbortSignal.timeout(SNAPSHOT_TIMEOUT_MS),
  }).then(async (res) => {
    if (!res.ok) throw new Error(`Could not refresh ${label} (HTTP ${res.status}).`);
    const rows: unknown = await res.json();
    if (!Array.isArray(rows)) throw new Error(`Could not read ${label}.`);
    return rows;
  }).then((rows) => ({ rows }), (error: unknown) => ({ error }));
  try {
    const detail = acceptedDetail ? await acceptedDetail.catch(() => null) : undefined;
    if (acceptedDetail && !detail) return;
    const generation = detail ? readDispatchState(detail.dispatch_state)?.generation : undefined;
    const result = await response;
    if (!current() || !sessions.current(observation, generation)) {
      if ("error" in result) throw result.error;
      return;
    }
    // Response ordering governs errors. Valid successes still contribute
    // terminal facts even if a newer-started read has already returned.
    const ownsError = reads.has(read);
    if (ownsError) {
      for (const older of reads) {
        reads.delete(older);
        if (older === read) break;
      }
    }
    const store = useSessionStore.getState();
    if ("error" in result) {
      if (ownsError) store.setSessionAuxiliaryError(sessionId, kind, `Could not refresh ${label}.`);
      throw result.error;
    }
    if (kind === "background") store.setBgTasks(sessionId, result.rows as BgTask[], start.bgTasks[sessionId] || [], !ownsError);
    else store.setResearch(sessionId, result.rows as ResearchJob[], start.research[sessionId] || [], !ownsError);
    if (ownsError) store.setSessionAuxiliaryError(sessionId, kind, null);
  } finally {
    reads.delete(read);
    if (!reads.size && group.get(sessionId) === reads) group.delete(sessionId);
  }
}

/** Selection, archived viewing and reconnect retain their original event
 * baseline. The store distinguishes per-job updates from snapshot writes. */
export async function loadResearchSnapshot(sessionId: string): Promise<void> {
  return observeSessionAuxiliary(sessionId, "research").done;
}

/** The accepted detail establishes the service identity without replacing the
 * original account or event baseline captured by its caller. */
function loadResearchAfterDetail(sessionId: string, detail: Record<string, unknown>, start: SnapshotStart): Promise<void> {
  const generation = readDispatchState(detail.dispatch_state)?.generation ?? start.dispatchGeneration;
  const current = useSessionStore.getState();
  if (start.accountGeneration !== current.accountGeneration || generation !== current.dispatchGeneration) return Promise.resolve();
  return loadAuxiliarySnapshot(sessionId, "research", { ...start, dispatchGeneration: generation });
}

/** Selection and archived viewing accept detail before loading its research
 * cards, so the first read also establishes the current service generation. */
export async function loadSessionSnapshots(
  sessionId: string, start = useSessionStore.getState(),
  onDetail?: OnDetail,
) {
  const detail = loadSessionSnapshot(sessionId, start, onDetail);
  // Auxiliary readers own their visible errors; they cannot block navigation
  // or turn a successful authoritative detail into unavailable queue state.
  void loadAuxiliarySnapshot(sessionId, "background", start, detail).catch(() => {});
  void detail.then((accepted) => accepted && loadResearchAfterDetail(sessionId, accepted, start)).catch(() => {});
  return detail;
}

/** One queue-state path for selection, archived viewing and reconnect. */
function applyDispatchSnapshot(
  sessionId: string, detail: Record<string, unknown>, start: SnapshotStart,
): boolean {
  const store = useSessionStore.getState();
  if (start.accountGeneration !== store.accountGeneration) return false;
  if (detail && detail.dispatch_state) {
    const snapshot = readDispatchState(detail.dispatch_state);
    const raw = detail.dispatch_state as { generation?: unknown };
    if (typeof raw.generation === "string" && store.retiredDispatchGenerations.includes(raw.generation)) return false;
    if (!snapshot) {
      store.setDispatchUnavailable(sessionId, true, start);
      return false;
    }
    if (start.dispatchGeneration !== store.dispatchGeneration && snapshot.generation !== store.dispatchGeneration) return false;
    store.applyDispatchState(sessionId, snapshot, start);
  } else {
    if (start.dispatchGeneration !== store.dispatchGeneration) return false;
    if (!detail || detail.archived !== true) {
      store.setDispatchUnavailable(sessionId, true, start);
      return false;
    }
    store.setDispatchUnavailable(sessionId, false, start);
  }
  return true;
}
/** Reject a retired service before any part of its detail can be rehydrated. */
export function applySessionSnapshot(
  sessionId: string, detail: Record<string, unknown>, start = useSessionStore.getState(),
): boolean {
  if (!applyDispatchSnapshot(sessionId, detail, start)) return false;
  applyTranscript(sessionId, detail);
  const store = useSessionStore.getState();
  const active = detail.active_attempt as { request_id?: unknown; attempt_id?: unknown } | undefined;
  if (!store.activeAttempts[sessionId] && typeof active?.request_id === "string" && typeof active.attempt_id === "string" &&
      store.dispatchStates[sessionId]?.active_request_id === active.request_id) {
    store.setActiveAttempt(sessionId, { request_id: active.request_id, attempt_id: active.attempt_id });
  }
  const snapshotMessages = (detail.messages as { seq?: number }[]) || [];
  const snapshotHigh = typeof detail.next_message_seq === "number"
    ? detail.next_message_seq - 1 : Math.max(-1, ...snapshotMessages.map((message) => message.seq ?? -1));
  const newerRequests = new Set((store.messages[sessionId] || []).filter((message) =>
    message.type === "question_request" && typeof message.seq === "number" && message.seq > snapshotHigh
  ).map((message) => message.tool_use_id));
  // The transcript already retains newer WS rows; their live forms must
  // survive the same older snapshot. A generation change clears live state
  // before this merge, and the store still filters resolved question ids.
  const newerPending = (store.pendingQuestions[sessionId] || []).filter((question) =>
    newerRequests.has(question.question_id));
  const pending = new Map([...(detail.pending_questions as PendingQuestion[] || []), ...newerPending]
    .map((question) => [question.question_id, question]));
  store.setPendingQuestions(sessionId, [...pending.values()]);
  store.setSubagents(sessionId, (detail.subagents as SubagentRun[]) || [], start.subagents[sessionId] || {});
  return true;
}
