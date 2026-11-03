import { accountFetch as fetch } from "./accountFetch";
import type { SessionInfo } from "../stores/sessionStore";
import { useSessionStore } from "../stores/sessionStore";
import { applyTranscript } from "./transcript";
import { readDispatchState } from "./sessionDispatch";
import { sessions, type SessionObservation } from "./sessionList";
import { observeDelegations } from "./delegationReads";
import { SnapshotReads, TaskSnapshotReads } from "./snapshotReads";
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
  "oldest_loaded_seq",
  "has_more_messages",
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
  if ([...store.sessions, ...store.archivedSessions].some((s) => s.id === sessionId)) return;
  try {
    await observeSession(sessionId, "metadata").done;
  } catch {
    // The mount fetch / reconnect refetch will pick it up.
  }
}

type SnapshotStart = ReturnType<typeof useSessionStore.getState>;
const detailReads = new SnapshotReads();

async function applyDetailResponse(sessionId: string, res: Response, start: SnapshotStart, observation: SessionObservation, current: () => boolean, read: ReturnType<SnapshotReads["begin"]>) {
  if (!res.ok) throw new Error(`Could not load session state (HTTP ${res.status}).`);
  const data: Record<string, unknown> = await res.json();
  const dispatch = readDispatchState(data.dispatch_state);
  if (!current() || !sessions.current(observation, dispatch?.generation)) return null;
  const applied = applySessionSnapshot(sessionId, data, start, !read.claim());
  if (!applied) return null;
  sessions.detail(sessionInfoFromDetail(data), observation, dispatch);
  return { data, ...applied };
}

interface SessionRead { done: Promise<Record<string, unknown> | null>; cancel: () => void }

/** One read owns its original identity, baseline, publication and lifetime.
 * Ending observation does not stop any server-side execution. */
export function observeSession(sessionId: string, purpose: "complete" | "detail" | "metadata" = "complete", confirmExecution = true): SessionRead {
  const start = useSessionStore.getState();
  const lifetime = sessions.observe(sessionId);
  const { observation } = lifetime;
  const read = detailReads.begin(sessionId);
  let active = true;
  let confirmation: SessionRead | undefined;
  const current = () => active;
  const done = (async () => {
    try {
      const res = await fetch(`${API}/api/sessions/${sessionId}${purpose === "metadata" ? "/info" : ""}`, {
        headers: { Authorization: `Bearer ${start.token}` }, signal: AbortSignal.timeout(SNAPSHOT_TIMEOUT_MS),
      });
      if (purpose === "metadata") {
        if (!res.ok) throw new Error(`Could not load session information (HTTP ${res.status}).`);
        const info: SessionInfo = await res.json();
        if (!current() || !sessions.current(observation)) return null;
        return sessions.detail(info, observation, null) ? info as unknown as Record<string, unknown> : null;
      }
      const result = await applyDetailResponse(sessionId, res, start, observation, current, read);
      const detail = result?.data ?? null;
      if (detail && start.activeSessionId === sessionId && useSessionStore.getState().activeSessionId === sessionId && typeof detail.agent_id === "string") {
        useSessionStore.getState().setActiveAgentId(detail.agent_id);
      }
      if (result?.needsExecutionConfirmation && confirmExecution && current()) {
        // Keep safe history now. One fresh read confirms a target whose live
        // baseline moved; continuous traffic cannot create a polling loop.
        confirmation = observeSession(sessionId, "detail", false);
        void confirmation.done.catch(() => {});
      }
      return detail;
    } catch (error) {
      if (purpose !== "metadata" && current() && sessions.current(observation)) useSessionStore.getState().setDispatchUnavailable(sessionId, true, start);
      throw error;
    } finally {
      read.finish();
      lifetime.finish();
    }
  })();
  let delegations: ReturnType<typeof observeDelegations> | undefined;
  const auxiliary: Array<{ cancel: () => void }> = [];
  if (purpose === "complete") {
    for (const kind of ["background", "research"] as const) {
      const reading = observeAuxiliarySnapshot(sessionId, kind, start, done, current);
      void reading.done.catch(() => {});
      auxiliary.push(reading);
    }
    delegations = observeDelegations(sessionId, done);
    void delegations.done.catch(() => {});
  }
  return { done, cancel: () => { active = false; lifetime.finish(); delegations?.cancel(); confirmation?.cancel(); auxiliary.forEach((read) => read.cancel()); } };
}

export async function loadSessionSnapshot(sessionId: string) {
  return observeSession(sessionId, "detail").done;
}

/** Background cards merge against the live records observed before fetch. */
export async function loadBgTaskSnapshot(sessionId: string): Promise<void> {
  await observeSessionAuxiliary(sessionId, "background").done;
}

/** Cancelling a view's observation never cancels the server's job. */
export function observeSessionAuxiliary(sessionId: string, kind: SessionAuxiliaryKind, bgTaskId?: string) {
  return observeAuxiliarySnapshot(sessionId, kind, useSessionStore.getState(), undefined, undefined, bgTaskId);
}

// Outstanding reads order replaceable fields and errors. Valid older successes
// may still advance execution or fill output; settled groups retain no domain state.
const auxiliaryReads: Record<SessionAuxiliaryKind, SnapshotReads> = {
  background: new SnapshotReads(), research: new SnapshotReads(),
};
const backgroundReads = new TaskSnapshotReads();

function observeAuxiliarySnapshot(
  sessionId: string, kind: SessionAuxiliaryKind, start: SnapshotStart,
  acceptedDetail?: Promise<Record<string, unknown> | null>,
  current = () => true,
  bgTaskId?: string,
): { done: Promise<unknown[] | null>; cancel: () => void } {
  const lifetime = sessions.observe(sessionId, start);
  const { observation } = lifetime;
  if (!start.token || !sessions.current(observation)) { lifetime.finish(); return { done: Promise.resolve(null), cancel: () => {} }; }
  const read = auxiliaryReads[kind].begin(sessionId);
  const taskRead = kind === "background" ? backgroundReads.begin(sessionId) : undefined;
  const controller = new AbortController();
  let active = true;
  const finish = () => { read.finish(); taskRead?.finish(); lifetime.finish(); };
  useSessionStore.getState().setSessionAuxiliaryError(sessionId, kind, null);
  const label = kind === "background" ? "background task details" : "research updates";
  // Start alongside detail, handling rejection immediately. Only its accepted
  // service generation can authorize this paired read's eventual publication.
  const path = kind === "background" ? `bg-tasks${bgTaskId ? `/${encodeURIComponent(bgTaskId)}` : ""}` : "research";
  const response = fetch(`${API}/api/sessions/${encodeURIComponent(sessionId)}/${path}`, {
    headers: { Authorization: `Bearer ${start.token}` }, signal: AbortSignal.any([controller.signal, AbortSignal.timeout(SNAPSHOT_TIMEOUT_MS)]),
  }).then(async (res) => {
    if (!res.ok) throw new Error(`Could not refresh ${label} (HTTP ${res.status}).`);
    const data: unknown = await res.json();
    if (bgTaskId && (!data || typeof data !== "object" || !("id" in data) || data.id !== bgTaskId ||
        !("session_id" in data) || data.session_id !== sessionId)) throw new Error("Invalid background task detail.");
    const rows: unknown = bgTaskId ? [data] : data;
    if (!Array.isArray(rows)) throw new Error(`Could not read ${label}.`);
    return rows;
  }).then((rows) => ({ rows }), (error: unknown) => ({ error }));
  const done = (async () => { try {
    const detail = acceptedDetail ? await acceptedDetail.catch(() => null) : undefined;
    if (acceptedDetail && !detail) return null;
    const generation = detail ? readDispatchState(detail.dispatch_state)?.generation : undefined;
    const result = await response;
    if (!active || !current() || !sessions.current(observation, generation)) {
      if ("error" in result) throw result.error;
      return null;
    }
    // Valid older successes contribute terminal facts and output without
    // replacing a newer snapshot's delivery diagnostics or explicit clearing.
    const ownsError = read.claim();
    const store = useSessionStore.getState();
    if ("error" in result) {
      if (ownsError) store.setSessionAuxiliaryError(sessionId, kind, `Could not refresh ${label}.`);
      throw result.error;
    }
    if (kind === "background" && bgTaskId) store.upsertBgTask(sessionId, result.rows[0] as BgTask,
      start.bgTasks[sessionId]?.find((task) => task.id === bgTaskId) ?? null, !taskRead!.claim(bgTaskId));
    else if (kind === "background") store.setBgTasks(sessionId, result.rows as BgTask[], start.bgTasks[sessionId] || [], false, taskRead!.claim);
    else store.setResearch(sessionId, result.rows as ResearchJob[], start.research[sessionId] || [], !ownsError);
    if (ownsError) store.setSessionAuxiliaryError(sessionId, kind, null);
    return result.rows;
  } finally {
    finish();
  } })();
  return { done, cancel: () => { active = false; controller.abort(); finish(); } };
}

/** Selection, archived viewing and reconnect retain their original event
 * baseline. The store distinguishes per-job updates from snapshot writes. */
export async function loadResearchSnapshot(sessionId: string): Promise<void> {
  await observeSessionAuxiliary(sessionId, "research").done;
}

/** Navigation finishes with detail; auxiliary reads own independent errors. */
export async function loadSessionSnapshots(sessionId: string) {
  return observeSession(sessionId).done;
}

/** One queue-state path for selection, archived viewing and reconnect. */
function applyDispatchSnapshot(
  sessionId: string, detail: Record<string, unknown>, start: SnapshotStart, advanceOnly: boolean,
): { needsExecutionConfirmation: boolean } | null {
  const store = useSessionStore.getState();
  if (start.accountGeneration !== store.accountGeneration) return null;
  if (detail && detail.dispatch_state) {
    const snapshot = readDispatchState(detail.dispatch_state);
    const raw = detail.dispatch_state as { generation?: unknown };
    if (typeof raw.generation === "string" && store.retiredDispatchGenerations.includes(raw.generation)) return null;
    if (!snapshot) {
      store.setDispatchUnavailable(sessionId, true, start);
      return null;
    }
    if (start.dispatchGeneration !== store.dispatchGeneration && snapshot.generation !== store.dispatchGeneration) return null;
    // Older successes can fill history/task output without replacing controls.
    if (!advanceOnly) return { needsExecutionConfirmation: !store.applyExecutionSnapshot(sessionId, detail, start) };
  } else {
    if (start.dispatchGeneration !== store.dispatchGeneration) return null;
    if (!detail || detail.archived !== true) {
      store.setDispatchUnavailable(sessionId, true, start);
      return null;
    }
    store.setDispatchUnavailable(sessionId, false, start);
  }
  return { needsExecutionConfirmation: false };
}
/** Reject a retired service before any part of its detail can be rehydrated. */
export function applySessionSnapshot(
  sessionId: string, detail: Record<string, unknown>, start = useSessionStore.getState(),
  advanceOnly = false,
): { needsExecutionConfirmation: boolean } | null {
  const applied = applyDispatchSnapshot(sessionId, detail, start, advanceOnly);
  if (!applied) return null;
  advanceOnly ||= applied.needsExecutionConfirmation;
  applyTranscript(sessionId, detail, start.streamingItems[sessionId] || {});
  const store = useSessionStore.getState();
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
  if (!advanceOnly) store.setPendingQuestions(sessionId, [...pending.values()]);
  store.setSubagents(sessionId, (detail.subagents as SubagentRun[]) || [], start.subagents[sessionId] || {}, advanceOnly);
  return applied;
}
