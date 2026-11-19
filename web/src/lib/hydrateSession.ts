import { accountFetch as fetch } from "./accountFetch";
import { captureStreamingRead, useSessionStore, type SessionInfo, type StreamingReadBasis } from "../stores/sessionStore";
import { applyTranscript } from "./transcript";
import { readDispatchState } from "./sessionDispatch";
import { isSelectedChat, sessions, type SessionObservation } from "./sessionList";
import { observeDelegations } from "./delegationReads";
import { qualifyPairedSnapshot, SessionSnapshotFlights, type DetailGeneration, type DetailPair, type SnapshotStart } from "./sessionSnapshotFlights";
import { SnapshotReads, TaskSnapshotReads } from "./snapshotReads";
import { isResearchJob } from "./researchProjection";
import type { BgTask, PendingQuestion, ResearchJob, SessionAuxiliaryKind, SubagentRun } from "../stores/sessionStore";

const API = window.location.origin;

/** Fields `GET /api/sessions/{id}` adds on top of SessionInfo. */
const DETAIL_ONLY_FIELDS = [
  "messages",
  "active_attempt",
  "dispatch_state",
  "context_state",
  "context_generation",
  "context_pending",
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

const detailReads = new SnapshotReads();
type Detail = Record<string, unknown>;
type DetailResult = { data: Detail } & NonNullable<ReturnType<typeof applySessionSnapshot>>;
type SessionReadPurpose = "complete" | "detail" | "metadata";
interface DetailSnapshot {
  response: Promise<Detail>;
  finish: () => void;
  publish: (data: Detail, dispatch: ReturnType<typeof readDispatchState>) => DetailResult | null;
  reportFailure: (error: unknown) => void;
  generation: DetailGeneration;
  confirmations: () => SessionSnapshotFlights<DetailSnapshot>;
}
const detailFlights = new SessionSnapshotFlights<DetailSnapshot>();

function issueDetail(sessionId: string, purpose: SessionReadPurpose, start: SnapshotStart,
  observation: SessionObservation, signal: AbortSignal): DetailSnapshot {
  const metadata = purpose === "metadata";
  const read = metadata ? undefined : detailReads.begin(sessionId);
  const streamingBasis = metadata ? undefined : captureStreamingRead(sessionId);
  if (!metadata) useSessionStore.getState().setTranscriptReadError(sessionId, null, start);
  const response: Promise<Detail> = fetch(`${API}/api/sessions/${encodeURIComponent(sessionId)}${metadata ? "/info" : "?view=browser"}`, {
    headers: { Authorization: `Bearer ${start.token}` }, signal,
  }).then(async (res) => {
    if (!res.ok) throw new Error(`Could not load session ${metadata ? "information" : "state"} (HTTP ${res.status}).`);
    return res.json();
  });
  let result: DetailResult | null | undefined;
  let generation: string | undefined;
  let reported = false;
  let confirmations: SessionSnapshotFlights<DetailSnapshot> | undefined;
  return {
    response, finish: () => read?.finish(), generation: () => generation,
    confirmations: () => confirmations ??= new SessionSnapshotFlights<DetailSnapshot>(),
    publish: (data, dispatch) => {
      if (result !== undefined) return result;
      if (metadata) {
        result = sessions.detail(data as unknown as SessionInfo, observation, null) ?
          { data, needsExecutionConfirmation: false } : null;
      } else {
        const applied = applySessionSnapshot(sessionId, data, start, !read!.claim(), streamingBasis);
        result = applied ? { data, ...applied } : null;
        if (result) {
          sessions.detail(sessionInfoFromDetail(data), observation, dispatch);
          generation = dispatch?.generation;
        }
      }
      return result;
    },
    reportFailure: (error) => {
      if (!read || reported) return;
      reported = true;
      useSessionStore.getState().setDispatchUnavailable(sessionId, true, start);
      if (read.claim()) useSessionStore.getState().setTranscriptReadError(sessionId,
        error instanceof Error ? error.message : String(error), start);
    },
  };
}

interface SessionRead { done: Promise<Detail | null>; cancel: () => void }

export function observeSession(sessionId: string, purpose: SessionReadPurpose = "complete", options?: { fresh?: boolean }): SessionRead {
  return readSession(sessionId, purpose, !options?.fresh && purpose !== "detail");
}

/** Each consumer qualifies independently against the snapshot's original observation. */
function readSession(sessionId: string, purpose: SessionReadPurpose, share: boolean,
  executionConfirmations?: SessionSnapshotFlights<DetailSnapshot>): SessionRead {
  const start = useSessionStore.getState();
  const lease = (executionConfirmations ?? detailFlights).acquire(sessionId, purpose, start, share,
    (original, signal) => issueDetail(sessionId, purpose, start, original, signal));
  const { observation, snapshot, original } = lease;
  let confirmation: SessionRead | undefined;
  const done = (async () => {
    try {
      const data = await lease.wait(snapshot.response);
      if (data === undefined || !lease.active()) return null;
      const dispatch = purpose === "metadata" ? null : readDispatchState(data.dispatch_state);
      if (!sessions.current(observation, dispatch?.generation) || !sessions.current(original, dispatch?.generation)) return null;
      const result = snapshot.publish(data, dispatch);
      const detail = result?.data ?? null;
      if (purpose !== "metadata" && detail && start.activeSessionId === sessionId && useSessionStore.getState().activeSessionId === sessionId && typeof detail.agent_id === "string") {
        useSessionStore.getState().setActiveAgentId(detail.agent_id);
      }
      if (result?.needsExecutionConfirmation && !executionConfirmations && lease.active()) {
        // Keep safe history now. One fresh read confirms a target whose live
        // baseline moved. Each original consumer independently owns its lease.
        confirmation = readSession(sessionId, "detail", true, snapshot.confirmations());
        void confirmation.done.catch(() => {});
      }
      return detail;
    } catch (error) {
      if (lease.active() && sessions.current(observation) && sessions.current(original)) {
        snapshot.reportFailure(error);
      }
      throw error;
    } finally {
      lease.release();
    }
  })();
  let delegations: ReturnType<typeof observeDelegations> | undefined;
  const auxiliary: Array<{ cancel: () => void }> = [];
  if (purpose === "complete") {
    const pair = { accepted: done, generation: snapshot.generation };
    for (const kind of ["background", "research"] as const) {
      const reading = observeAuxiliarySnapshot(sessionId, kind, start, pair, share);
      void reading.done.catch(() => {});
      auxiliary.push(reading);
    }
    delegations = observeDelegations(sessionId, pair, { fresh: !share });
    void delegations.done.catch(() => {});
  }
  return { done, cancel: () => { lease.release(); delegations?.cancel(); confirmation?.cancel(); auxiliary.forEach((read) => read.cancel()); } };
}

export async function loadSessionSnapshot(sessionId: string) {
  return observeSession(sessionId, "detail").done;
}

/** Background cards merge against the live records observed before fetch. */
export async function loadBgTaskSnapshot(sessionId: string): Promise<void> {
  await observeSessionAuxiliary(sessionId, "background").done;
}

/** Cancelling a view's observation never cancels the server's job. */
export function observeSessionAuxiliary(sessionId: string, kind: SessionAuxiliaryKind, taskId?: string) {
  return observeAuxiliarySnapshot(sessionId, kind, useSessionStore.getState(), undefined, false, undefined, taskId);
}

// Outstanding reads order replaceable fields and errors. Valid older successes
// may still advance execution or fill output; settled groups retain no domain state.
const auxiliaryReads: Record<SessionAuxiliaryKind, SnapshotReads> = {
  background: new SnapshotReads(), research: new SnapshotReads(),
};
const auxiliaryTaskReads = {
  background: { success: new TaskSnapshotReads(), outcome: new TaskSnapshotReads() },
  research: { success: new TaskSnapshotReads(), outcome: new TaskSnapshotReads() },
};

// Confirmation owns the request demand and its original observation.
const taskConfirmations = new Map<string, { current: () => boolean; dirty: boolean; resumeIfIdle: () => void }>();
export function confirmAuxiliaryTask(kind: SessionAuxiliaryKind, sessionId: string, taskId: string, source: "event" | "list" | "retry"): void {
  const key = JSON.stringify([kind, sessionId, taskId]);
  const pending = taskConfirmations.get(key);
  if (pending?.current()) {
    if (source === "event") pending.dirty = true;
    pending.resumeIfIdle();
    return;
  }
  const start = useSessionStore.getState();
  const tasks = kind === "background" ? start.bgTasks[sessionId] : start.research[sessionId];
  if (!start.token || kind === "research" && !isSelectedChat(sessionId, start) || source === "event" && tasks?.some(task => task.id === taskId)) return;
  const lifetime = sessions.observe(sessionId, start);
  let read: ReturnType<typeof observeAuxiliarySnapshot> | undefined;
  const failed = (state: SnapshotStart) => state.researchReadFailures[sessionId]?.includes(taskId);
  const demand = {
    current: () => taskConfirmations.get(key) === demand &&
      (sessions.current(lifetime.observation) || sessions.initialGenerationCurrent(lifetime.observation)),
    dirty: false,
    resumeIfIdle: () => { if (!read) void run(); },
  };
  const unsubscribe = kind === "research" ? useSessionStore.subscribe((state) => {
    if (!isSelectedChat(sessionId, state) || !demand.current()) finish();
    else if (!read) {
      if (sessions.initialGenerationCurrent(lifetime.observation)) demand.resumeIfIdle();
      else if (!failed(state)) finish();
    }
  }) : undefined;
  function finish() {
    if (taskConfirmations.get(key) === demand) taskConfirmations.delete(key);
    unsubscribe?.();
    read?.cancel();
    lifetime.finish();
  }
  async function run() {
    do {
      demand.dirty = false;
      // Each pass starts after the events it confirms, with a fresh merge baseline.
      const baseline = useSessionStore.getState();
      read = observeAuxiliarySnapshot(sessionId, kind, baseline, undefined, false, demand.current, taskId);
      await read.done.catch(() => {}); // Existing auxiliary error/retry UI owns failures.
      read = undefined;
      // Establishing the first service preserves the demand and retires the old read.
      demand.dirty ||= baseline.dispatchGeneration !== useSessionStore.getState().dispatchGeneration;
    } while (demand.dirty && demand.current());
    const state = useSessionStore.getState();
    // A settled failure still needs an exact read if the first service is established.
    if (kind === "research" && state.dispatchGeneration === null && demand.current() && failed(state)) return;
    finish();
  }
  taskConfirmations.set(key, demand);
  demand.resumeIfIdle();
}

type AuxiliaryResult = { rows: unknown[] } | { error: unknown };
interface AuxiliarySnapshot {
  response: Promise<AuxiliaryResult>;
  generation?: DetailGeneration;
  publish: (result: AuxiliaryResult) => void;
  finish: () => void;
}
const auxiliaryFlights = new SessionSnapshotFlights<AuxiliarySnapshot>();

function issueAuxiliarySnapshot(sessionId: string, kind: SessionAuxiliaryKind, start: SnapshotStart,
  signal: AbortSignal, generation?: DetailGeneration, taskId?: string): AuxiliarySnapshot {
  const read = taskId ? undefined : auxiliaryReads[kind].begin(sessionId);
  const taskRead = auxiliaryTaskReads[kind].success.begin(sessionId);
  // A failed detail can own its task's Retry while older successes still fill data.
  const taskOutcome = auxiliaryTaskReads[kind].outcome.begin(sessionId);
  const finish = () => { read?.finish(); taskRead.finish(); taskOutcome.finish(); };
  if (read) useSessionStore.getState().setSessionAuxiliaryError(sessionId, kind, null);
  const label = kind === "background" ? "background task details" : "research updates";
  const path = kind === "background" ? "bg-tasks" : "research";
  const target = taskId ? `/${encodeURIComponent(taskId)}` : "";
  const response: Promise<AuxiliaryResult> = fetch(`${API}/api/sessions/${encodeURIComponent(sessionId)}/${path}${target}`, {
    headers: { Authorization: `Bearer ${start.token}` }, signal,
  }).then(async (res) => {
    if (!res.ok) throw new Error(`Could not refresh ${label} (HTTP ${res.status}).`);
    const data: unknown = await res.json();
    if (taskId && (!data || typeof data !== "object" || !("id" in data) || data.id !== taskId ||
        !("session_id" in data) || data.session_id !== sessionId)) {
      throw new Error(kind === "background" ? "Invalid background task detail." : "Invalid research task detail.");
    }
    const rows: unknown = taskId ? [data] : data;
    if (!Array.isArray(rows)) throw new Error(`Could not read ${label}.`);
    if (kind === "research" && !rows.every((row) => isResearchJob(row, sessionId))) throw new Error(`Could not read ${label}.`);
    return rows;
  }).then((rows) => ({ rows }), (error: unknown) => ({ error }));
  let published = false;
  const publish = (result: AuxiliaryResult) => {
    if (published) return;
    published = true;
    // Valid older successes contribute terminal facts and output without
    // replacing a newer snapshot's delivery diagnostics or explicit clearing.
    const ownsError = read?.claim() ?? false;
    const store = useSessionStore.getState();
    if (taskId && taskOutcome.claim(taskId)) store.setAuxiliaryTaskFailure(kind, sessionId, taskId, "error" in result);
    if ("error" in result) {
      if (ownsError) store.setSessionAuxiliaryError(sessionId, kind, `Could not refresh ${label}.`);
      return;
    }
    if (ownsError) store.setSessionAuxiliaryError(sessionId, kind, null);
    if (taskId) {
      const advanceOnly = !taskRead.claim(taskId);
      if (kind === "background") store.upsertBgTask(sessionId, result.rows[0] as BgTask,
        start.bgTasks[sessionId]?.find((task) => task.id === taskId) ?? null, advanceOnly);
      else store.applyResearchDetail(sessionId, result.rows[0] as ResearchJob,
        start.research[sessionId]?.find((job) => job.id === taskId), advanceOnly);
      return;
    }

    const failures = kind === "background" ? store.bgTaskReadFailures[sessionId] : store.researchReadFailures[sessionId];
    for (const task of result.rows as (BgTask | ResearchJob)[]) {
      if (taskOutcome.claim(task.id) && failures?.includes(task.id)) store.setAuxiliaryTaskFailure(kind, sessionId, task.id, false);
    }
    if (kind === "background") {
      const rows = result.rows as BgTask[];
      const basis = start.bgTasks[sessionId] ?? [];
      store.setBgTasks(sessionId, rows, basis, taskRead.claim);

      // A bounded list cannot settle a previously known active task outside
      // its window. Recheck those tasks by id even when their cards are folded.
      const listed = new Set(rows.map(task => task.id));
      const known = new Set(basis.map(task => task.id));
      for (const task of store.bgTasks[sessionId] ?? []) {
        if ((task.status === "pending" || task.status === "running") && known.has(task.id) && !listed.has(task.id)) {
          confirmAuxiliaryTask("background", sessionId, task.id, "list");
        }
      }
    } else store.setResearch(sessionId, result.rows as ResearchJob[], start.research[sessionId] ?? [], taskRead.claim);
  };
  return { response, generation, publish, finish };
}

function observeAuxiliarySnapshot(
  sessionId: string, kind: SessionAuxiliaryKind, start: SnapshotStart,
  pair?: DetailPair, share = false, current = () => true, taskId?: string,
): { done: Promise<unknown[] | null>; cancel: () => void } {
  if (!start.token || !sessions.current(sessions.capture(start))) return { done: Promise.resolve(null), cancel: () => {} };
  const lease = auxiliaryFlights.acquire(sessionId, kind, start, share,
    (_original, signal) => issueAuxiliarySnapshot(sessionId, kind, start, signal, pair?.generation, taskId));
  const { snapshot, observation, original } = lease;
  let next: ReturnType<typeof observeAuxiliarySnapshot> | undefined;
  const done = (async () => {
    try {
      // A consumer waits for its own detail, but a changed generation must
      // also be confirmed by the detail issued with the physical request.
      const detail = pair && await lease.wait(pair.accepted.catch(() => null));
      if (pair && !detail) return null;
      const generation = detail ? readDispatchState(detail.dispatch_state)?.generation : undefined;
      const qualify = () => qualifyPairedSnapshot(observation, original, generation, snapshot.generation);
      if (lease.active() && current() && qualify() === "new-generation") {
        lease.release();
        next = observeAuxiliarySnapshot(sessionId, kind, useSessionStore.getState(), pair, share, current, taskId);
        return await next.done;
      }
      const result = await lease.wait(snapshot.response);
      if (!result || !lease.active()) return null;
      if (current() && qualify() === "current") {
        snapshot.publish(result);
        if ("rows" in result) return result.rows;
      }
      if ("error" in result) throw result.error;
      return null;
    } finally {
      lease.release();
    }
  })();
  return { done, cancel: () => { lease.release(); next?.cancel(); } };
}

/** Selection, archived viewing and reconnect retain their original event
 * baseline. The store distinguishes per-job updates from snapshot writes. */
export async function loadResearchSnapshot(sessionId: string): Promise<void> {
  await observeSessionAuxiliary(sessionId, "research").done;
}

/** Navigation finishes with detail; auxiliary reads own independent errors. */
export async function loadSessionSnapshots(sessionId: string, options?: { fresh?: boolean }) {
  return observeSession(sessionId, "complete", options).done;
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
  advanceOnly = false, streamingBasis?: StreamingReadBasis,
): { needsExecutionConfirmation: boolean } | null {
  if (typeof detail.id === "string" && detail.id !== sessionId) return null;
  const applied = applyDispatchSnapshot(sessionId, detail, start, advanceOnly);
  if (!applied) return null;
  advanceOnly ||= applied.needsExecutionConfirmation;
  const transcriptApplied = applyTranscript(sessionId, detail, streamingBasis);
  advanceOnly ||= !transcriptApplied;
  const store = useSessionStore.getState();
  const snapshotMessages = (detail.messages as { seq?: number }[]) || [];
  if (typeof detail.context_generation === "string" && "context_state" in detail) {
    if (detail.context_pending === undefined || typeof detail.context_pending === "boolean") {
      store.applyContextState(sessionId, detail.context_generation, detail.context_state, start, detail.context_pending ?? false);
    }
  }
  const snapshotHigh = typeof detail.next_message_seq === "number"
    ? detail.next_message_seq - 1 : Math.max(-1, ...snapshotMessages.map((message) => message.seq ?? -1));
  const newerRequests = new Set((store.messages[sessionId] || []).filter((message) =>
    (message.type === "question_request" || message.type === "question_snoozed") && typeof message.seq === "number" && message.seq > snapshotHigh
  ).map((message) => message.tool_use_id));
  // The transcript already retains newer WS rows; their live forms must
  // survive the same older snapshot. A generation change clears live state
  // before this merge, and the store still filters resolved question ids.
  const newerPending = (store.pendingQuestions[sessionId] || []).filter((question) =>
    newerRequests.has(question.question_id));
  const pending = new Map([...(detail.pending_questions as PendingQuestion[] || []), ...newerPending]
    .map((question) => [question.question_id, question]));
  // A persisted interaction can arrive after this snapshot's read even when
  // the original request row was already included. Its seq protects the same
  // question projection; no browser timer or independent version resets it.
  for (const message of store.messages[sessionId] || []) {
    if (message.type !== "question_snoozed" || typeof message.seq !== "number" || message.seq <= snapshotHigh) continue;
    const question = pending.get(message.tool_use_id ?? ""), projection = message.tool_input;
    if (question && projection?.request_id === question.request_id && projection?.attempt_id === question.attempt_id) {
      pending.set(question.question_id, { ...question, deadline: null, snoozed: true });
    }
  }
  if (!advanceOnly) store.setPendingQuestions(sessionId, [...pending.values()]);
  store.setSubagents(sessionId, (detail.subagents as SubagentRun[]) || [], start.subagents[sessionId] || {}, advanceOnly);
  return applied;
}
