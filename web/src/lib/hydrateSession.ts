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
  "pending_queue",
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
    const res = await fetch(`${API}/api/sessions/${sessionId}`, {
      headers: { Authorization: `Bearer ${store.token}` },
    });
    if (!res.ok) return;
    const data = await res.json();
    if (applySessionSnapshot(sessionId, data, store.dispatchGeneration, store.subagents[sessionId] || {})) {
      addSessionInfo(sessionInfoFromDetail(data));
    }
  } catch {
    // The mount fetch / reconnect refetch will pick it up.
  }
}

/** One queue-state path for selection, archived viewing and reconnect. */
export function applyDispatchSnapshot(
  sessionId: string, detail: Record<string, unknown>, expectedGeneration?: string | null,
): boolean {
  const store = useSessionStore.getState();
  if (detail.dispatch_state) {
    const snapshot = readDispatchState(detail.dispatch_state);
    if (!snapshot || store.retiredDispatchGenerations.includes(snapshot.generation) ||
        (expectedGeneration !== undefined && expectedGeneration !== store.dispatchGeneration &&
         snapshot.generation !== store.dispatchGeneration)) return false;
    store.applyDispatchState(sessionId, detail.dispatch_state, expectedGeneration);
  } else {
    if (expectedGeneration !== undefined && expectedGeneration !== store.dispatchGeneration) return false;
    if (!store.dispatchStates[sessionId]) store.setPendingQueue(sessionId, (detail.pending_queue as string[]) || []);
  }
  return true;
}

/** Reject a retired service before any part of its detail can be rehydrated. */
export function applySessionSnapshot(
  sessionId: string, detail: Record<string, unknown>, expectedGeneration?: string | null,
  baselineSubagents?: Record<string, SubagentRun>,
): boolean {
  if (!applyDispatchSnapshot(sessionId, detail, expectedGeneration)) return false;
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
  store.setSubagents(sessionId, (detail.subagents as SubagentRun[]) || [], baselineSubagents);
  return true;
}

/** Append a session to the list unless it's already there (a concurrent
 * hydrate or the reconnect refetch may have won the race). */
export function addSessionInfo(info: SessionInfo): void {
  const store = useSessionStore.getState();
  if (store.sessions.some((s) => s.id === info.id)) return;
  store.setSessions([...store.sessions, info]);
}
