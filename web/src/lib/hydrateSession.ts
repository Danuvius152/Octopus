import { accountFetch as fetch } from "./accountFetch";
import type { SessionInfo } from "../stores/sessionStore";
import { useSessionStore } from "../stores/sessionStore";
import { applyTranscript } from "./transcript";
import { readDispatchState } from "./sessionDispatch";
import type { PendingQuestion, SubagentRun } from "../stores/sessionStore";

const API = window.location.origin;

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
export async function hydrateSession(sessionId: string): Promise<void> {
  const store = useSessionStore.getState();
  if (!sessionId || !store.token) return;
  if (store.sessions.some((s) => s.id === sessionId)) return;
  try {
    await loadSessionSnapshot(sessionId, store, (data) => addSessionInfo(sessionInfoFromDetail(data)));
  } catch {
    // The mount fetch / reconnect refetch will pick it up.
  }
}

type SnapshotStart = ReturnType<typeof useSessionStore.getState>;
type OnDetail = (detail: Record<string, unknown>) => void;

async function applyDetailResponse(sessionId: string, res: Response, start: SnapshotStart, onDetail?: OnDetail) {
  if (!res.ok) throw new Error(`Could not load session state (HTTP ${res.status}).`);
  const data: Record<string, unknown> = await res.json();
  if (!applySessionSnapshot(sessionId, data, start)) return null;
  // Adopt detail in this same synchronous step, before an account change
  // can clear the applied transcript and start a new sidebar list.
  onDetail?.(data);
  return data;
}

async function applyBgResponse(sessionId: string, res: Response, start: SnapshotStart): Promise<void> {
  if (res.ok) {
    useSessionStore.getState().setBgTasks(sessionId, await res.json(), start.bgTasks[sessionId] || []);
  }
}

/** Capture the merge baseline before starting this detail request. Callers
 * with an earlier operation identity can pass that original store snapshot. */
export async function loadSessionSnapshot(sessionId: string, start = useSessionStore.getState(), onDetail?: OnDetail) {
  try {
    const res = await fetch(`${API}/api/sessions/${sessionId}`, {
      headers: { Authorization: `Bearer ${start.token}` },
    });
    return await applyDetailResponse(sessionId, res, start, onDetail);
  } catch (error) {
    useSessionStore.getState().setDispatchUnavailable(sessionId, true, start);
    throw error;
  }
}

/** Background cards merge against the live records observed before fetch. */
export async function loadBgTaskSnapshot(sessionId: string, start = useSessionStore.getState()): Promise<void> {
  const res = await fetch(`${API}/api/sessions/${sessionId}/bg-tasks`, {
    headers: { Authorization: `Bearer ${start.token}` },
  });
  await applyBgResponse(sessionId, res, start);
}

/** Selection and archived viewing wait for both responses before applying
 * either one; reconnect deliberately loads the endpoints independently. */
export async function loadSessionAndBgSnapshots(
  sessionId: string, start = useSessionStore.getState(),
  onDetail?: OnDetail,
) {
  const headers = { Authorization: `Bearer ${start.token}` };
  try {
    const [detailRes, bgRes] = await Promise.all([
      fetch(`${API}/api/sessions/${sessionId}`, { headers }),
      fetch(`${API}/api/sessions/${sessionId}/bg-tasks`, { headers }),
    ]);
    const detail = await applyDetailResponse(sessionId, detailRes, start, onDetail);
    await applyBgResponse(sessionId, bgRes, start);
    return detail;
  } catch (error) {
    useSessionStore.getState().setDispatchUnavailable(sessionId, true, start);
    throw error;
  }
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

/** Append a session to the list unless it's already there (a concurrent
 * hydrate or the reconnect refetch may have won the race). */
export function addSessionInfo(info: SessionInfo): void {
  const store = useSessionStore.getState();
  if (info.archived) {
    if (!store.archivedSessions.some((s) => s.id === info.id)) {
      store.setArchivedSessions([...store.archivedSessions, info]);
    }
    if (store.sessions.some((s) => s.id === info.id)) {
      store.setSessions(store.sessions.filter((s) => s.id !== info.id));
    }
    return;
  }
  if (store.sessions.some((s) => s.id === info.id)) return;
  store.setSessions([...store.sessions, info]);
}
