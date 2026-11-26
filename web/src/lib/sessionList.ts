import { createStore } from "zustand/vanilla";
import { accountFetch } from "./accountFetch";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import type { ListEventSource } from "./applicationList";
import { unreadList, type ListReadState, type RefreshResult } from "./listState";
import type { DispatchState } from "./sessionDispatch";
import { SessionRestorations } from "./sessionRestorations";
import { ArchiveDeadline } from "./sessionArchive";
import { AgentChatCreations } from "./agentChatCreations";
import { RequestDeadline } from "./requestDeadline";
import type { SessionArchiveResult, SessionPresence } from "../api";

type ArchiveFields = Partial<Pick<SessionInfo, "name" | "model" | "reasoning_effort" | "service_tier">>;

interface Scope { account: number; token: string; generation: string | null; connection: number }
export interface SessionObservation {
  scope: Scope; change: number; invalidated?: boolean;
  // A null-generation observer may confirm only the first accepted generation.
  // A subsequent transition permanently ends that candidate's lifetime.
  initialGeneration?: string | null;
  archiveConfirmation?: ArchiveDeadline;
}
interface Read extends SessionObservation {
  archived: boolean; controller: AbortController; timer: ReturnType<typeof setTimeout>;
  promise: Promise<RefreshResult>; finish: (result: RefreshResult) => void;
  projection: Promise<RefreshResult>; publish: (result: RefreshResult) => void;
  checkingMissing?: boolean;
}
interface ReadMembership {
  observation: SessionObservation;
  rows: ReadonlyMap<string, SessionInfo>;
  checked: ReadonlyMap<string, SessionPresence | null>;
  awaitingPresence?: true;
}
interface ObservationTarget {
  id: string | undefined;
  archiveReceipt: boolean;
  activity?: { result: SessionArchiveResult; connection: number };
}
interface CreatedOptions {
  archivedId?: string;
  deadline?: ArchiveDeadline;
  includeArchived?: boolean;
  active?: () => boolean;
}
export type LiveRowEvidence = "listed" | "confirmed";
type LiveRowObservation = { observation: SessionObservation; evidence: LiveRowEvidence };
type DeletionTarget = Pick<SessionInfo, "id" | "name">;
type DeletionEntry = DeletionTarget & { phase: "deleting" | "retry"; batch?: symbol };
export type ArchiveDeletion = "deleted" | "restored" | "retry" | "obsolete";

export function isSelectedChat(sessionId: string | null, state: Pick<ReturnType<typeof useSessionStore.getState>, "activeSessionId" | "mainView">): boolean {
  return state.activeSessionId === sessionId && state.mainView === "chat";
}

/** Session list observations, not server revisions. Zustand owns the only rows.
 * A conflicting snapshot is discarded and one subsequent read confirms the list;
 * no per-session tombstone or second collection survives that read boundary.
 */
export class SessionList {
  readonly restorations = new SessionRestorations(this);
  readonly agentChats = new AgentChatCreations(this);
  readonly deletions = createStore<Record<string, DeletionEntry>>(() => ({}));
  // The identity stays with the result so late retries can still contribute.
  readonly archiveBatch = createStore<{ id: symbol | null; running: boolean; restored: number }>(
    () => ({ id: null, running: false, restored: 0 }),
  );
  private socket: object | null = null;
  private connection = 0;
  private change = 0;
  private lastInclusiveRead = 0;
  private active: Read | null = null;
  private dirty = false;
  private scheduled = false;
  private engaged = false;
  private archivedRequested = false;
  private closed = false;
  // Evidence follows projected rows without retaining rows that leave the list.
  private liveRowObservations = new WeakMap<SessionInfo, LiveRowObservation>();
  // Membership evidence lives only as long as a consumer holds the read result.
  private readMembership = new WeakMap<RefreshResult, ReadMembership>();
  private deletionLiveObservations = new WeakMap<DeletionEntry, SessionObservation>();
  // Only outstanding reads are retained, including targets not yet in a list.
  private observations = new Map<SessionObservation, ObservationTarget>();
  private deletionListeners = new Set<(id: string, wasActive: boolean) => void>();
  // Provisional identities only until the inclusive confirmation succeeds.
  // They never filter incoming rows or claim deletion by themselves.
  private unconfirmedMissing = new Set<string>();
  private unsubscribe: () => void;

  constructor() {
    this.unsubscribe = useSessionStore.subscribe((state, before) => {
      const account = state.accountGeneration !== before.accountGeneration || state.token !== before.token;
      if (!account && state.dispatchGeneration === before.dispatchGeneration) return;
      if (!account) for (const observation of this.observations.keys()) {
        if (observation.scope.generation === null) observation.initialGeneration =
          observation.initialGeneration === undefined && before.dispatchGeneration === null ? state.dispatchGeneration : null;
      }
      this.retire({ state: "obsolete" }); this.dirty = false;
      if (account) {
        this.restorations.retire();
        this.agentChats.retire();
        this.deletions.setState({}, true);
        this.archiveBatch.setState(this.archiveBatch.getInitialState());
        this.unconfirmedMissing.clear();
        this.socket = null; this.connection++; this.engaged = false; this.archivedRequested = false;
        useSessionStore.setState({ sessions: [], archivedSessions: [], sessionSuccessors: {}, sessionRead: unreadList() });
      } else if (this.engaged) this.invalidate();
    });
  }

  private scope(start = useSessionStore.getState()): Scope {
    return { account: start.accountGeneration, token: start.token, generation: start.dispatchGeneration, connection: this.connection };
  }
  private sameAccount(scope: Scope): boolean {
    const now = this.scope();
    return !this.closed && now.account === scope.account && now.token === scope.token;
  }
  current(observation: SessionObservation, establishedGeneration?: string | null): boolean {
    const now = this.scope(), scope = observation.scope;
    return !observation.invalidated && this.sameAccount(scope) && now.connection === scope.connection &&
      (now.generation === scope.generation || establishedGeneration != null && establishedGeneration === now.generation);
  }
  accountCurrent(observation: SessionObservation): boolean {
    return this.sameAccount(observation.scope);
  }
  hasCurrentLiveObservation(row: SessionInfo, required: LiveRowEvidence = "listed"): boolean {
    const fact = this.liveRowObservations.get(row);
    return !!fact && this.current(fact.observation) && (required === "listed" || fact.evidence === "confirmed");
  }
  initialGenerationCurrent(observation: SessionObservation): boolean {
    return observation.scope.generation === null && observation.initialGeneration != null &&
      this.current(observation, observation.initialGeneration);
  }
  capture(start = useSessionStore.getState()): SessionObservation {
    const now = useSessionStore.getState();
    return { scope: this.scope(start), change: start.sessions === now.sessions && start.archivedSessions === now.archivedSessions ? this.change : -1 };
  }
  // Without a target, track only the scope lifetime (including its first
  // generation); membership changes must not retire navigation after restore.
  observe(id?: string, start = useSessionStore.getState(), purpose: "detail" | "archive" = "detail") {
    const observation = this.capture(start);
    const target: ObservationTarget = { id, archiveReceipt: purpose === "archive" };
    this.observations.set(observation, target);
    return { observation, finish: () => { this.observations.delete(observation); } };
  }
  onDeleted(listener: (id: string, wasActive: boolean) => void): () => void {
    this.deletionListeners.add(listener);
    return () => { this.deletionListeners.delete(listener); };
  }
  private invalidateTarget(id: string, retainArchiveReceipts = false): void {
    for (const [observation, target] of this.observations) {
      if (target.id === id && !(retainArchiveReceipts && target.archiveReceipt)) {
        observation.invalidated = true; this.observations.delete(observation);
      }
    }
    useSessionStore.getState().clearAuxiliaryTaskFailures(id);
  }
  accepts(source?: ListEventSource): boolean {
    return !this.closed && (!source || source.socket === this.socket && source.accountGeneration === this.scope().account);
  }
  connect(source: ListEventSource): void {
    if (source.accountGeneration !== this.scope().account) return;
    this.engaged = true;
    if (source.socket !== this.socket) {
      this.retire({ state: "obsolete" }); this.socket = source.socket; this.connection++;
      useSessionStore.setState({ bgTaskReadFailures: {}, researchReadFailures: {} });
    }
    this.invalidate();
  }
  disconnect(source: ListEventSource): void {
    if (source.socket !== this.socket) return;
    this.socket = null; this.connection++; this.dirty = false;
    useSessionStore.setState({ bgTaskReadFailures: {}, researchReadFailures: {} });
    this.retire({ state: "obsolete" }); this.state({ stale: true });
  }
  private state(patch: Partial<ListReadState>): void {
    useSessionStore.setState((state) => ({ sessionRead: { ...state.sessionRead, ...patch } }));
  }
  private invalidate(): void {
    this.dirty = true; this.state({ stale: true });
    if (this.engaged) this.kick();
  }
  private kick(): void {
    if (this.closed || this.scheduled) return;
    this.scheduled = true;
    void Promise.resolve().then(() => { this.scheduled = false; this.start(); });
  }
  refresh(includeArchived = false): Promise<RefreshResult> {
    return this.requestRead(includeArchived)?.promise ?? Promise.resolve({ state: "obsolete" });
  }
  private requestRead(includeArchived: boolean): Read | null {
    if (this.closed || !this.scope().token) return null;
    this.engaged = true;
    if (includeArchived && !this.archivedRequested) {
      this.archivedRequested = true; this.dirty = true;
    }
    if (this.active) return this.active;
    this.dirty = true;
    return this.start();
  }
  async refreshAfterRestoration(id: string, restart = false): Promise<RefreshResult> {
    if (restart) this.retire({ state: "pending" });
    return this.readForTargets(true, result => this.membershipFromRead(result, id)?.listed === true);
  }
  private async readForTargets(includeArchived: boolean, accepts: (result: RefreshResult) => boolean): Promise<RefreshResult> {
    const read = this.requestRead(includeArchived);
    if (!read) return { state: "obsolete" };
    const result = await read.projection;
    if (read.checkingMissing && this.active !== read) return read.promise;
    if ((!includeArchived || read.archived) && accepts(result)) return result;
    return read.promise;
  }
  needsFollowup(result: RefreshResult): boolean {
    return result.state === "pending" || result.state === "applied" &&
      !this.readMembership.get(result)?.awaitingPresence && useSessionStore.getState().sessionRead.stale;
  }
  membershipFromRead(result: RefreshResult, id: string): { archived: boolean | null; listed: boolean } | undefined {
    const read = this.readMembership.get(result);
    if (!read || !this.current(read.observation)) return;
    const row = read.rows.get(id);
    if (row) {
      const state = useSessionStore.getState();
      const current = row.archived ? state.archivedSessions : state.sessions;
      return current.includes(row) ? { archived: !!row.archived, listed: true } : undefined;
    }
    if (read.observation.change !== this.change) return;
    const presence = read.checked.get(id);
    if (presence) return { archived: presence.archived, listed: false };
  }
  private start(): Read | null {
    if (this.closed || this.active || !this.dirty || !this.scope().token) return this.active;
    this.dirty = false;
    let finish!: Read["finish"], publish!: Read["publish"];
    const promise = new Promise<RefreshResult>((resolve) => { finish = resolve; });
    const projection = new Promise<RefreshResult>((resolve) => { publish = resolve; });
    const read: Read = { ...this.capture(), archived: this.archivedRequested, controller: new AbortController(), promise, finish, projection, publish,
      timer: setTimeout(() => this.fail(read, new Error(read.checkingMissing
        ? "Could not confirm missing sessions: the read timed out after 30 seconds."
        : "Session list timed out after 30 seconds")), 30_000) };
    this.active = read; this.state({ loading: true, stale: true, error: null });
    void accountFetch(`${window.location.origin}/api/sessions${read.archived ? "?include_archived=true" : ""}`, {
      headers: { Authorization: `Bearer ${read.scope.token}` }, signal: read.controller.signal,
    }).then(async (response) => {
      if (!response.ok) throw new Error(`Could not load sessions (HTTP ${response.status}).`);
      return await response.json() as SessionInfo[];
    }).then(async (rows) => {
      if (this.active !== read) return;
      if (!this.current(read)) { this.retire({ state: "obsolete" }); return; }
      if (!Array.isArray(rows)) { this.fail(read, new Error("Invalid session list response")); return; }
      if (read.change !== this.change) { this.retryRead(); return; }
      const store = useSessionStore.getState();
      const observed = new Set([...store.sessions, ...store.archivedSessions].map((row) => row.id));
      for (const id of this.restorations.pendingIds()) observed.add(id);
      for (const [from, to] of Object.entries(store.sessionSuccessors)) { observed.add(from); observed.add(to); }
      if (store.activeSessionId) observed.add(store.activeSessionId);
      for (const id of this.unconfirmedMissing) observed.add(id);
      if (read.archived) {
        for (const entry of Object.values(this.deletions.getState())) {
          if (entry.phase === "retry") observed.add(entry.id);
        }
      }
      const listed = new Set(rows.map((row) => row.id));
      const absent = [...observed].filter((id) => !listed.has(id));
      // A concurrent archive can list the ID twice. A restore can omit it,
      // so keep older rows on display until their own membership is checked.
      const observedArchived = rows.filter((row) => row.archived);
      const archivedIds = new Set(observedArchived.map((row) => row.id));
      const observedLive = rows.filter((row) => !row.archived && !archivedIds.has(row.id));
      const projectedRows = new Map([...observedLive, ...observedArchived].map(row => [row.id, row]));
      const retain = (row: SessionInfo) => !listed.has(row.id);
      const live = read.archived ? [...observedLive, ...store.sessions.filter(retain)] : observedLive;
      const archived = [...observedArchived, ...store.archivedSessions.filter(retain)];
      this.commit(live, archived);
      if (this.active !== read || !this.current(read)) return;
      const projection = this.capture();
      for (const id of listed) this.unconfirmedMissing.delete(id);
      for (const id of absent) this.unconfirmedMissing.add(id);
      if (read.archived) this.lastInclusiveRead = projection.change;
      if (!read.archived && this.unconfirmedMissing.size) { this.archivedRequested = true; this.dirty = true; }
      this.publishChatRows(observedLive, this.dirty ? "listed" : "confirmed", projection);
      if (read.archived) this.publishChatRows(observedArchived, "confirmed", projection);
      if (this.active !== read) return;

      const presences = new Map<string, SessionPresence | null>();
      if (read.archived && absent.length > 0) {
        read.checkingMissing = true;
        const result: RefreshResult = { state: "applied" };
        this.readMembership.set(result, { observation: projection, rows: projectedRows, checked: new Map(), awaitingPresence: true });
        this.state({ loaded: true });
        read.publish(result);
        await Promise.all(absent.map(async (id) => {
          presences.set(id, await this.readPresence(id, read, read.controller.signal));
        }));
        if (this.active !== read || !this.current(read)) return;
        if (projection.change !== this.change) { this.retryRead(); return; }
        for (const id of absent) {
          if (this.active !== read || !this.current(projection)) return;
          const presence = presences.get(id);
          if (presence?.archived === null) this.remove(id, undefined, presence.deletion_pending);
          if (presence?.archived === false) this.observeLiveDeletionTarget(id, projection);
        }
      }
      if (this.active !== read) return;
      const error = [...presences.values()].includes(null) ? "Could not confirm missing sessions." : null;
      const result: RefreshResult = error ? { state: "failed", message: error } : { state: "applied" };
      this.readMembership.set(result, { observation: this.capture(), rows: projectedRows, checked: presences });
      this.retire(result);
      this.state({ loaded: true, stale: this.dirty || !!error, error });
      this.kick();
    }).catch((error: unknown) => this.fail(read, error));
    return read;
  }
  private retryRead(): void {
    this.retire({ state: "pending" });
    this.dirty = true;
    this.kick();
  }
  private retire(result: RefreshResult): void {
    const read = this.active;
    if (!read) return;
    this.active = null; clearTimeout(read.timer); read.controller.abort();
    this.state({ loading: false, stale: true }); read.publish(result); read.finish(result);
  }
  private fail(read: Read, error: unknown): void {
    if (this.active !== read) return;
    if (!this.current(read)) { this.retire({ state: "obsolete" }); return; }
    const message = error instanceof Error ? error.message : "Could not load sessions";
    this.retire({ state: "failed", message }); this.state({ error: message });
    // start() consumed this read's demand. An invalidation that arrived
    // afterward still owns a follow-up, even if the older read failed.
    if (this.dirty) this.kick();
  }
  private publishChatRows(rows: SessionInfo[], evidence: LiveRowEvidence, observation: SessionObservation): void {
    if (!this.current(observation)) return;
    for (const row of rows) {
      if (row.archived) continue;
      this.observeLiveDeletionTarget(row.id, observation);
      if (this.hasCurrentLiveObservation(row, "confirmed")) continue;
      this.liveRowObservations.set(row, { observation, evidence });
    }
    this.agentChats.observeRows(rows);
  }
  private commit(sessions: SessionInfo[], archivedSessions: SessionInfo[], links = useSessionStore.getState().sessionSuccessors,
    activity?: SessionArchiveResult,
  ): void {
    const before = useSessionStore.getState();
    const nextRows = new Map([...sessions, ...archivedSessions].map((row) => [row.id, row]));
    // A sparse event can precede the original row. Absence in a partial list
    // is not deletion; an inclusive confirmation retires missing identities.
    const sessionSuccessors = Object.fromEntries(Object.entries(links).filter(([from, to]) =>
      from !== to && (!nextRows.has(from) || nextRows.get(from)?.archived)));
    for (const [from, to] of Object.entries(before.sessionSuccessors)) {
      if (sessionSuccessors[from] !== to) this.invalidateTarget(from);
    }
    for (const row of [...before.sessions, ...before.archivedSessions]) {
      const next = nextRows.get(row.id);
      if (row.archived && !next) this.agentChats.rememberArchivedRow(row);
      if (!next || Boolean(next.archived) !== Boolean(row.archived)) this.invalidateTarget(row.id, !next || !!next.archived);
      if (next && next !== row && !next.archived && !row.archived) {
        const fact = this.liveRowObservations.get(row);
        if (fact && this.current(fact.observation)) this.liveRowObservations.set(next, fact);
      }
    }
    // Missing live-list rows await inclusive confirmation. An archive receipt
    // survives that gap, but a source returning live is restoration evidence;
    // confirmed absence is retired by remove(), never by partial omission.
    for (const id of this.unconfirmedMissing) {
      const next = nextRows.get(id);
      if (next && !next.archived) {
        this.unconfirmedMissing.delete(id);
        this.invalidateTarget(id);
      }
    }
    if (activity) {
      // Broadcast activity is a fact from this connection. A foreground
      // observer can receive it after reconnect without renewing POST rights.
      for (const [observation, target] of this.observations) {
        if (target.id === activity.old_session_id && target.archiveReceipt && this.archiveReceiptCurrent(observation)) {
          target.activity = { result: activity, connection: this.connection };
        }
      }
    }
    this.change++;
    useSessionStore.setState({ sessions, archivedSessions, sessionSuccessors });
  }
  private canApply(observation?: SessionObservation, generation?: string | null): boolean {
    if (!observation || this.current(observation, generation) && observation.change === this.change) return !this.closed;
    // A current fact changed during this operation. Confirm through a fresh
    // snapshot, while an old identity cannot schedule work for a new account.
    if (this.current(observation, generation)) { this.engaged = true; this.invalidate(); }
    return false;
  }
  private canConfirm(observation?: SessionObservation, includeArchived = false): boolean {
    if (!observation) return !this.closed;
    if (!this.sameAccount(observation.scope)) return false;
    if (this.current(observation) && observation.change === this.change) return true;
    // A successful write still changed this account when its original
    // connection or observation is obsolete. Confirm it in the current scope.
    this.archivedRequested ||= includeArchived;
    this.engaged = true; this.invalidate();
    return false;
  }
  /** Follow coalesced reads while the caller still owns this confirmation. */
  async settledRead(includeArchived: boolean, current: () => boolean, deadline?: ArchiveDeadline,
    accepts?: (result: RefreshResult) => boolean,
  ): Promise<SessionObservation | null> {
    while (current() && (!deadline || deadline.active)) {
      const observation = this.capture();
      const read = accepts ? this.readForTargets(includeArchived, accepts) : this.refresh(includeArchived);
      const result = await (deadline ? deadline.wait(read) : read);
      if (!result || !current()) return null;
      if (!this.current(observation) || this.needsFollowup(result)) continue;
      return (accepts ? accepts(result) : result.state === "applied") ? observation : null;
    }
    return null;
  }
  private async confirmLive(id: string, observation: SessionObservation,
    { includeArchived = false, archivedId, deadline, active }: CreatedOptions,
  ): Promise<boolean> {
    const state = useSessionStore.getState();
    if (state.archivedSessions.some((row) => row.id === id) || state.sessionSuccessors[id]) return false;
    const target = this.observe(id, state);
    const current = () => (active?.() ?? true) && this.current(observation) && this.current(target.observation);
    try {
      const accepted = await this.settledRead(includeArchived, current, deadline, result => {
        const membership = this.membershipFromRead(result, id);
        return membership?.listed === true && membership.archived === false &&
          (!archivedId || this.membershipFromRead(result, archivedId)?.archived === true);
      });
      return !!accepted && current() && useSessionStore.getState().sessions.some((row) => row.id === id);
    } finally {
      target.finish();
    }
  }
  async created(info: SessionInfo, observation: SessionObservation,
    { archivedId, deadline, includeArchived = !!archivedId, active }: CreatedOptions = {},
  ): Promise<boolean> {
    if (active?.() === false || deadline && !deadline.active) return false;
    if (archivedId && !this.archiveObservationCurrent(archivedId, observation)) {
      this.canConfirm(observation, true);
      return false;
    }
    const adopted = this.adoptCreated(info, observation, includeArchived, archivedId);
    const accepted = adopted || await this.confirmLive(info.id, observation, { includeArchived, archivedId, deadline, active });
    if (!accepted || !this.current(observation) || !useSessionStore.getState().sessions.some((row) => row.id === info.id)) return false;
    if (archivedId) {
      if (!this.archiveObservationCurrent(archivedId, observation)) return false;
      const store = useSessionStore.getState();
      if (!adopted && !store.archivedSessions.some((row) => row.id === archivedId)) return false;
      const linked = store.sessionSuccessors[archivedId];
      if (linked && linked !== info.id) return false;
      if (!linked) {
        this.commit(store.sessions, store.archivedSessions, { ...store.sessionSuccessors, [archivedId]: info.id });
      }
    }
    if (archivedId && !useSessionStore.getState().archivedSessions.some((row) => row.id === archivedId)) void this.refresh(true);
    return this.current(observation);
  }
  private adoptCreated(info: SessionInfo, observation: SessionObservation, includeArchived: boolean, archivedId?: string): boolean {
    if (!this.current(observation)) { this.canConfirm(observation, includeArchived); return false; }
    const store = useSessionStore.getState();
    // A preceding event/read can already confirm this new live identity.
    // Navigation may follow that fact without copying the receipt's old fields.
    if (store.sessions.some((row) => row.id === info.id) &&
        (!archivedId || store.archivedSessions.some((row) => row.id === archivedId))) return true;
    if (!this.canConfirm(observation, includeArchived) || store.archivedSessions.some((row) => row.id === info.id)) return false;
    if (archivedId) {
      const old = store.sessions.find((row) => row.id === archivedId);
      this.agentChats.observeClosed(archivedId);
      this.commit([...store.sessions.filter((row) => row.id !== archivedId), info], old ?
        [...store.archivedSessions.filter((row) => row.id !== archivedId), { ...old, archived: true }] : store.archivedSessions);
      this.publishChatRows([info], "confirmed", observation);
      return true;
    }
    return this.row(info);
  }
  detail(info: SessionInfo, observation: SessionObservation, dispatch: DispatchState | null): boolean {
    const known = useSessionStore.getState().dispatchStates[info.id];
    if (dispatch && known?.generation === dispatch.generation && known.revision > dispatch.revision) return false;
    return this.row(info, observation, dispatch?.generation);
  }
  metadata(info: SessionInfo, observation: SessionObservation): boolean {
    if (!this.current(observation) || this.observations.get(observation)?.id !== info.id) return false;
    const { sessions, archivedSessions } = useSessionStore.getState();
    if (info.archived && !sessions.some((row) => row.id === info.id) && !archivedSessions.some((row) => row.id === info.id)) {
      // Target events retire this observation. A later inclusive read can also
      // settle absence. Unrelated live changes cannot settle archival history.
      return observation.change >= this.lastInclusiveRead && this.row(info);
    }
    return this.detail(info, observation, null);
  }
  announced(id: string, info?: SessionInfo): void {
    if (this.closed) return;
    const { sessions, archivedSessions } = useSessionStore.getState();
    // A read may already have observed this identity with newer fields or membership.
    if (sessions.some((row) => row.id === id) || archivedSessions.some((row) => row.id === id)) return;
    if (info) this.row(info);
    else {
      // The summary exceeded the wire budget. An in-flight list may predate it.
      this.change++;
      this.invalidate();
    }
  }
  restored(info: SessionInfo): void {
    if (!info.id || info.archived) return;
    // A restore can arrive after a missed archive, with an unchanged live row.
    this.invalidateTarget(info.id);
    this.row(info);
  }
  row(info: SessionInfo, observation?: SessionObservation, establishedGeneration?: string | null): boolean {
    if (!info.id || !this.canApply(observation, establishedGeneration)) return false;
    const store = useSessionStore.getState();
    const liveObservation = this.capture(store);
    // Details and events update existing rows in place; opening a session
    // must not move it down the sidebar. Only a new identity is appended.
    const replace = (rows: SessionInfo[], belongs: boolean) => {
      if (!belongs) return rows.filter((row) => row.id !== info.id);
      return rows.some((row) => row.id === info.id)
        ? rows.map((row) => row.id === info.id ? info : row)
        : [...rows, info];
    };
    this.commit(replace(store.sessions, !info.archived), replace(store.archivedSessions, !!info.archived));
    this.publishChatRows([info], "confirmed", liveObservation);
    return true;
  }
  patch(id: string, fields: Partial<SessionInfo>, observation?: SessionObservation): boolean {
    if (!this.canConfirm(observation)) return false;
    const store = useSessionStore.getState();
    this.commit(store.sessions.map((row) => row.id === id ? { ...row, ...fields } : row),
      store.archivedSessions.map((row) => row.id === id ? { ...row, ...fields } : row));
    return true;
  }
  archive(id: string, observation?: SessionObservation, successorId?: string | null,
    activity?: SessionArchiveResult, fields: ArchiveFields = {},
  ): boolean {
    if (!this.canConfirm(observation)) return false;
    const store = useSessionStore.getState();
    const targetId = successorId === undefined ? store.sessionSuccessors[id] : successorId;
    if (!store.archivedSessions.some((row) => row.id === id)) this.invalidateTarget(id, true);
    const rows = [...store.sessions, ...store.archivedSessions];
    const old = rows.find((row) => row.id === id);
    const patch = (row: SessionInfo): SessionInfo => {
      if (row.id === id) return { ...row, archived: true,
        name: successorId === null && fields.name !== undefined ? fields.name : row.name };
      if (row.id !== targetId) return row;
      const { name, model, reasoning_effort, service_tier } = fields;
      return { ...row,
        ...(name !== undefined && { name }),
        ...(model !== undefined && { model }),
        ...(reasoning_effort !== undefined && { reasoning_effort }),
        ...(service_tier !== undefined && { service_tier }),
      };
    };
    const live = store.sessions.filter((row) => row.id !== id).map(patch);
    const archived = store.archivedSessions.map(patch);
    if (old && !archived.some((row) => row.id === id)) archived.push(patch(old));
    let links: Record<string, string> | undefined;
    if (successorId !== undefined) {
      links = { ...store.sessionSuccessors };
      if (successorId === null) delete links[id];
      else links[id] = successorId;
    }
    const shared = activity?.old_session_id === id && activity.new_session_id === successorId ? activity : undefined;
    this.agentChats.observeClosed(id);
    this.commit(live, archived, links, shared);
    if (!old || targetId && !rows.some((row) => row.id === targetId)) void this.refresh(true);
    return true;
  }
  async confirmSuccessor(id: string, observation: SessionObservation, deadline?: ArchiveDeadline): Promise<SessionInfo | null> {
    const target = useSessionStore.getState().sessionSuccessors[id];
    if (!target) return null;
    const successor = this.observe(target);
    const budget = deadline ?? new ArchiveDeadline();
    const linked = () => this.current(observation) && this.current(successor.observation) &&
      useSessionStore.getState().sessionSuccessors[id] === target &&
      useSessionStore.getState().archivedSessions.some((row) => row.id === id);
    const accepts = (result: RefreshResult) => this.membershipFromRead(result, id)?.archived === true &&
      this.membershipFromRead(result, target)?.listed === true;
    try {
      while (linked() && budget.active) {
        const result = await budget.wait(this.readForTargets(true, accepts));
        if (!result) return null;
        if (!linked()) return null;
        if (this.needsFollowup(result)) continue;
        if (!accepts(result)) return null;
        const store = useSessionStore.getState();
        return [...store.sessions, ...store.archivedSessions].find((row) => row.id === target) ?? null;
      }
      return null;
    } finally {
      successor.finish();
      if (!deadline) budget.finish();
    }
  }
  archiveActivity(id: string, observation: SessionObservation): SessionArchiveResult | null {
    if (!this.archiveActivityCurrent(id, observation)) return null;
    const activity = this.observations.get(observation)?.activity;
    return activity?.connection === this.connection ? activity.result : null;
  }
  archiveActivityCurrent(id: string, observation: SessionObservation): boolean {
    const target = this.observations.get(observation);
    return target?.id === id && target.archiveReceipt && this.archiveReceiptCurrent(observation);
  }
  archiveReceiptCurrent(observation: SessionObservation): boolean {
    const generation = this.scope().generation;
    return !observation.invalidated && this.accountCurrent(observation) &&
      (observation.scope.generation === generation || generation !== null && observation.initialGeneration === generation);
  }
  archiveObservationCurrent(id: string, original: SessionObservation): boolean {
    return this.archiveActivityCurrent(id, original) &&
      (this.current(original) || this.initialGenerationCurrent(original));
  }
  async confirmArchive(id: string, observation: SessionObservation, deadline?: ArchiveDeadline,
    receipt?: { successorId: string; original: SessionObservation },
  ): Promise<{ archived: boolean; successor: SessionInfo | null } | null> {
    const budget = deadline ?? new ArchiveDeadline();
    const current = () => this.current(observation) || this.initialGenerationCurrent(observation);
    try {
      if (!current()) { this.canConfirm(observation, true); return null; }
      const continuous = receipt && this.archiveObservationCurrent(id, receipt.original);
      const successorTarget = (linked: string | undefined) => {
        if (!receipt) return linked;
        const { original, successorId } = receipt;
        if (!this.archiveReceiptCurrent(original) || linked && linked !== successorId) return;
        if (linked || continuous && this.archiveObservationCurrent(id, original)) return successorId;
      };
      const accepts = (result: RefreshResult) => {
        const membership = this.membershipFromRead(result, id);
        if (!membership?.listed) return false;
        const target = successorTarget(useSessionStore.getState().sessionSuccessors[id]);
        return !membership.archived || !target || target === id || this.membershipFromRead(result, target)?.listed === true;
      };
      // A list started before this confirmation cannot prove its effect.
      // Keep an in-flight list fetch, but retire probes whose list was already
      // published and whose membership baseline this confirmation invalidates.
      if (observation.archiveConfirmation !== budget) {
        observation.archiveConfirmation = budget;
        this.change++;
        if (this.active?.checkingMissing) this.retire({ state: "pending" });
        this.invalidate();
      }
      while (current() && budget.active) {
        const result = await budget.wait(this.readForTargets(true, accepts));
        if (!result) return null;
        if (!current()) return null;
        const state = useSessionStore.getState();
        if (this.needsFollowup(result) ||
            result.state === "obsolete" && this.initialGenerationCurrent(observation)) continue;
        const membership = this.membershipFromRead(result, id);
        if (!membership || membership.archived === null) return null;
        const archived = membership.archived;
        const linked = state.sessionSuccessors[id];
        if (receipt && !archived) {
          const { original, successorId } = receipt;
          if (this.archiveReceiptCurrent(original) && (!linked || linked === successorId)) {
            original.invalidated = true;
            this.observations.delete(original);
          }
        }
        const target = successorTarget(linked);
        const successor = archived && target && target !== id && this.membershipFromRead(result, target)?.listed
          ? [...state.sessions, ...state.archivedSessions].find((row) => row.id === target) ?? null : null;
        if (successor && !linked) {
          this.commit(state.sessions, state.archivedSessions, { ...state.sessionSuccessors, [id]: successor.id });
        }
        return { archived, successor };
      }
      return null;
    } finally { if (!deadline) budget.finish(); }
  }
  private async readPresence(id: string, observation: SessionObservation, signal: AbortSignal): Promise<SessionPresence | null> {
    try {
      const response = await accountFetch(`${window.location.origin}/api/sessions/${encodeURIComponent(id)}/presence`, {
        headers: { Authorization: `Bearer ${observation.scope.token}` }, signal,
      });
      if (!response.ok) return null;
      const presence = await response.json() as Partial<SessionPresence> | null;
      if (!presence || typeof presence.deletion_pending !== "boolean" ||
          presence.archived !== null && typeof presence.archived !== "boolean") return null;
      return { archived: presence.archived, deletion_pending: presence.deletion_pending };
    } catch {
      return null;
    }
  }

  async deleteArchived(target: DeletionTarget, batch?: symbol): Promise<ArchiveDeletion | "restored-in-batch"> {
    const { id, name } = target;
    const original = this.capture();
    const previous = this.deletions.getState()[id];
    if (this.closed || !original.scope.token || previous?.phase === "deleting") return "obsolete";
    const entry: DeletionEntry = { id, name, phase: "deleting", batch: batch ?? previous?.batch };
    this.deletions.setState({ [id]: entry });
    const outcome = await this.requestArchivedDeletion(id, original, entry);
    if (this.deletions.getState()[id] === entry) {
      if (outcome === "retry") this.deletions.setState({ [id]: { ...entry, phase: "retry" } });
      else if (this.finishDeletion(id, outcome)) return "restored-in-batch";
    }
    return outcome;
  }

  async deleteArchivedBatch(targets: readonly DeletionTarget[], confirmation: SessionObservation): Promise<void> {
    if (!this.accountCurrent(confirmation) || this.archiveBatch.getState().running) return;
    const batch = Symbol();
    this.archiveBatch.setState({ id: batch, running: true, restored: 0 });
    try {
      // Finish each target's presence check before the next deletion can change the list.
      for (const target of targets) {
        if (!this.accountCurrent(confirmation)) break;
        await this.deleteArchived(target, batch);
      }
    } finally {
      if (this.archiveBatch.getState().id === batch) this.archiveBatch.setState({ running: false });
    }
  }

  private async requestArchivedDeletion(id: string, original: SessionObservation, entry: DeletionEntry): Promise<ArchiveDeletion> {
    const deadline = new RequestDeadline();
    let reply: Response | null = null;
    try {
      reply = await deadline.wait(accountFetch(`${window.location.origin}/api/sessions/${encodeURIComponent(id)}?archived_only=true`, {
        method: "DELETE", headers: { Authorization: `Bearer ${original.scope.token}` }, signal: deadline.signal,
      }));
    } catch {
      // The server can finish after the observer loses the response.
    } finally { deadline.finish(); }
    if (!this.accountCurrent(original)) return "obsolete";
    if (this.deletions.getState()[id] !== entry) return "deleted";
    if (reply?.status === 204 && this.current(original)) {
      this.remove(id);
      return "deleted";
    }

    const observation = this.capture();
    const confirmation = new RequestDeadline();
    try {
      const presence = await confirmation.wait(this.readPresence(id, observation, confirmation.signal));
      if (!this.accountCurrent(observation)) return "obsolete";
      if (this.deletions.getState()[id] !== entry) return "deleted";
      const current = this.current(observation) && observation.change === this.change;
      const live = this.deletionLiveObservations.get(entry);
      if ((!current || !presence) && live && this.current(live)) return "restored";
      if (!current) {
        this.unconfirmedMissing.add(id);
        this.archivedRequested = true;
        this.engaged = true;
        this.invalidate();
        return "retry";
      }
      if (presence?.archived === null) {
        this.remove(id, undefined, presence.deletion_pending);
        return presence.deletion_pending ? "retry" : "deleted";
      }
      if (presence?.archived === false) {
        void this.refresh(true);
        return "restored";
      }
      return "retry";
    } finally { confirmation.finish(); }
  }

  private finishDeletion(id: string, outcome?: ArchiveDeletion): boolean {
    const state = this.deletions.getState();
    if (!Object.hasOwn(state, id)) return false;
    const { batch } = state[id];
    const next = { ...state };
    delete next[id];
    this.deletions.setState(next, true);
    const currentBatch = this.archiveBatch.getState();
    if (outcome !== "restored" || !batch || batch !== currentBatch.id) return false;
    this.archiveBatch.setState({ restored: currentBatch.restored + 1 });
    return true;
  }

  private observeLiveDeletionTarget(id: string, observation: SessionObservation): void {
    const entry = this.deletions.getState()[id];
    if (entry?.phase === "retry") this.finishDeletion(id, "restored");
    if (entry?.phase === "deleting") this.deletionLiveObservations.set(entry, observation);
  }

  remove(id: string, observation?: SessionObservation, deletionPending = false): boolean {
    if (!this.canConfirm(observation)) return false;
    this.agentChats.observeClosed(id);
    this.forget(id);
    if (!deletionPending) this.finishDeletion(id);
    return true;
  }
  private forget(id: string): void {
    this.invalidateTarget(id);
    this.unconfirmedMissing.delete(id);
    const store = useSessionStore.getState();
    const wasActive = store.activeSessionId === id;
    this.commit(store.sessions.filter((row) => row.id !== id), store.archivedSessions.filter((row) => row.id !== id),
      Object.fromEntries(Object.entries(store.sessionSuccessors).filter(([from, to]) => from !== id && to !== id)));
    useSessionStore.getState().forgetSession(id);
    this.restorations.deleted(id);
    for (const listener of this.deletionListeners) listener(id, wasActive);
  }
  dispose(): void {
    this.restorations.retire();
    this.agentChats.retire();
    this.deletions.setState({}, true);
    this.archiveBatch.setState(this.archiveBatch.getInitialState());
    this.unconfirmedMissing.clear();
    this.deletionListeners.clear();
    this.observations.clear();
    this.closed = true; this.unsubscribe(); this.dirty = false; this.retire({ state: "obsolete" });
  }
}

export const sessions = new SessionList();
