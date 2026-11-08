import { accountFetch } from "./accountFetch";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import type { ListEventSource } from "./applicationList";
import { unreadList, type ListReadState, type RefreshResult } from "./listState";
import type { DispatchState } from "./sessionDispatch";

interface Scope { account: number; token: string; generation: string | null; connection: number }
export interface SessionObservation {
  scope: Scope; change: number; invalidated?: boolean;
  // A null-generation observer may confirm only the first accepted generation.
  // A subsequent transition permanently ends that candidate's lifetime.
  initialGeneration?: string | null;
}
interface Read extends SessionObservation {
  archived: boolean; controller: AbortController; timer: ReturnType<typeof setTimeout>;
  promise: Promise<RefreshResult>; finish: (result: RefreshResult) => void;
}

/** Session list observations, not server revisions. Zustand owns the only rows.
 * A conflicting snapshot is discarded and one subsequent read confirms the list;
 * no per-session tombstone or second collection survives that read boundary.
 */
export class SessionList {
  private socket: object | null = null;
  private connection = 0;
  private change = 0;
  private active: Read | null = null;
  private dirty = false;
  private scheduled = false;
  private engaged = false;
  private archivedRequested = false;
  private closed = false;
  // Only outstanding reads are retained, including targets not yet in a list.
  private observations = new Map<SessionObservation, string>();
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
        this.unconfirmedMissing.clear();
        this.socket = null; this.connection++; this.engaged = false; this.archivedRequested = false;
        useSessionStore.setState({ sessions: [], archivedSessions: [], sessionRead: unreadList() });
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
  initialGenerationCurrent(observation: SessionObservation): boolean {
    return observation.scope.generation === null && observation.initialGeneration != null &&
      this.current(observation, observation.initialGeneration);
  }
  capture(start = useSessionStore.getState()): SessionObservation {
    const now = useSessionStore.getState();
    return { scope: this.scope(start), change: start.sessions === now.sessions && start.archivedSessions === now.archivedSessions ? this.change : -1 };
  }
  observe(id: string, start = useSessionStore.getState()) {
    const observation = this.capture(start);
    this.observations.set(observation, id);
    return { observation, finish: () => { this.observations.delete(observation); } };
  }
  onDeleted(listener: (id: string, wasActive: boolean) => void): () => void {
    this.deletionListeners.add(listener);
    return () => { this.deletionListeners.delete(listener); };
  }
  private invalidateTarget(id: string): void {
    for (const [observation, target] of this.observations) {
      if (target === id) { observation.invalidated = true; this.observations.delete(observation); }
    }
  }
  accepts(source?: ListEventSource): boolean {
    return !this.closed && (!source || source.socket === this.socket && source.accountGeneration === this.scope().account);
  }
  connect(source: ListEventSource): void {
    if (source.accountGeneration !== this.scope().account) return;
    this.engaged = true;
    if (source.socket !== this.socket) {
      this.retire({ state: "obsolete" }); this.socket = source.socket; this.connection++;
    }
    this.invalidate();
  }
  disconnect(source: ListEventSource): void {
    if (source.socket !== this.socket) return;
    this.socket = null; this.connection++; this.dirty = false;
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
    if (this.closed || !this.scope().token) return Promise.resolve({ state: "obsolete" });
    this.engaged = true;
    if (includeArchived && !this.archivedRequested) {
      this.archivedRequested = true; this.dirty = true;
    }
    if (this.active) return this.active.promise;
    this.dirty = true;
    return this.start()!.promise;
  }
  private start(): Read | null {
    if (this.closed || this.active || !this.dirty || !this.scope().token) return this.active;
    this.dirty = false;
    let finish!: Read["finish"];
    const promise = new Promise<RefreshResult>((resolve) => { finish = resolve; });
    const read: Read = { ...this.capture(), archived: this.archivedRequested, controller: new AbortController(), promise, finish,
      timer: setTimeout(() => this.fail(read, new Error("Session list timed out after 30 seconds")), 30_000) };
    this.active = read; this.state({ loading: true, stale: true, error: null });
    void accountFetch(`${window.location.origin}/api/sessions${read.archived ? "?include_archived=true" : ""}`, {
      headers: { Authorization: `Bearer ${read.scope.token}` }, signal: read.controller.signal,
    }).then(async (response) => {
      if (!response.ok) throw new Error(`Could not load sessions (HTTP ${response.status}).`);
      return await response.json() as SessionInfo[];
    }).then((rows) => {
      if (this.active !== read) return;
      if (!this.current(read)) { this.retire({ state: "obsolete" }); return; }
      if (!Array.isArray(rows)) { this.fail(read, new Error("Invalid session list response")); return; }
      if (read.change !== this.change) {
        this.retire({ state: "pending" }); this.dirty = true;
      } else {
        const store = useSessionStore.getState();
        const observed = new Set([...store.sessions, ...store.archivedSessions].map((row) => row.id));
        if (store.activeSessionId) observed.add(store.activeSessionId);
        for (const id of this.unconfirmedMissing) observed.add(id);
        const absent = [...observed].filter((id) => !rows.some((row) => row.id === id));
        const live = rows.filter((row) => !row.archived);
        const archived = read.archived ? rows.filter((row) => row.archived) :
          useSessionStore.getState().archivedSessions.filter((row) => !live.some((item) => item.id === row.id));
        this.commit(live, archived);
        // Only a complete list can distinguish archive from deletion. Every
        // missing known identity can still own cached inputs or late receipts.
        if (read.archived) {
          this.unconfirmedMissing.clear();
          for (const id of absent) this.remove(id);
        } else {
          for (const id of absent) this.unconfirmedMissing.add(id);
          for (const row of rows) this.unconfirmedMissing.delete(row.id);
          if (this.unconfirmedMissing.size) { this.archivedRequested = true; this.dirty = true; }
        }
        this.retire({ state: "applied" }); this.state({ loaded: true, stale: this.dirty, error: null });
      }
      this.kick();
    }).catch((error: unknown) => this.fail(read, error));
    return read;
  }
  private retire(result: RefreshResult): void {
    const read = this.active;
    if (!read) return;
    this.active = null; clearTimeout(read.timer); read.controller.abort();
    this.state({ loading: false, stale: true }); read.finish(result);
  }
  private fail(read: Read, error: unknown): void {
    if (this.active !== read) return;
    if (!this.current(read)) { this.retire({ state: "obsolete" }); return; }
    const message = error instanceof Error ? error.message : "Could not load sessions";
    this.retire({ state: "failed", message }); this.dirty = false; this.state({ error: message });
  }
  private commit(sessions: SessionInfo[], archivedSessions: SessionInfo[]): void {
    const before = useSessionStore.getState();
    const nextRows = new Map([...sessions, ...archivedSessions].map((row) => [row.id, row]));
    for (const row of [...before.sessions, ...before.archivedSessions]) {
      const next = nextRows.get(row.id);
      if (!next || Boolean(next.archived) !== Boolean(row.archived)) this.invalidateTarget(row.id);
    }
    this.change++;
    useSessionStore.setState({ sessions, archivedSessions });
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
  private async confirmLive(id: string, observation: SessionObservation, includeArchived = false): Promise<boolean> {
    while (this.current(observation)) {
      const result = await this.refresh(includeArchived);
      // An accepted snapshot can still predate a later write receipt. Wait
      // through the coalesced confirmation already owned by this list.
      if (result.state === "pending" || result.state === "applied" && useSessionStore.getState().sessionRead.stale) continue;
      return result.state === "applied" && this.current(observation) &&
        useSessionStore.getState().sessions.some((row) => row.id === id);
    }
    return false;
  }
  async created(info: SessionInfo, observation: SessionObservation, archivedId?: string): Promise<boolean> {
    if (!this.adoptCreated(info, observation, archivedId)) return this.confirmLive(info.id, observation, !!archivedId);
    if (archivedId && !useSessionStore.getState().archivedSessions.some((row) => row.id === archivedId)) void this.refresh(true);
    return this.current(observation);
  }
  private adoptCreated(info: SessionInfo, observation: SessionObservation, archivedId?: string): boolean {
    if (!this.current(observation)) { this.canConfirm(observation, !!archivedId); return false; }
    const store = useSessionStore.getState();
    // A preceding event/read can already confirm this new live identity.
    // Navigation may follow that fact without copying the receipt's old fields.
    if (store.sessions.some((row) => row.id === info.id) &&
        (!archivedId || !store.sessions.some((row) => row.id === archivedId))) return true;
    if (!this.canConfirm(observation, !!archivedId) || store.archivedSessions.some((row) => row.id === info.id)) return false;
    if (archivedId) {
      const old = store.sessions.find((row) => row.id === archivedId);
      this.commit([...store.sessions.filter((row) => row.id !== archivedId), info], old ?
        [...store.archivedSessions.filter((row) => row.id !== archivedId), { ...old, archived: true }] : store.archivedSessions);
      return true;
    }
    return this.row(info);
  }
  async restored(info: SessionInfo, observation: SessionObservation): Promise<boolean> {
    if (this.current(observation) && useSessionStore.getState().sessions.some((row) => row.id === info.id)) return true;
    if (this.canConfirm(observation) && this.row(info)) return true;
    return this.confirmLive(info.id, observation, true);
  }
  detail(info: SessionInfo, observation: SessionObservation, dispatch: DispatchState | null): boolean {
    const known = useSessionStore.getState().dispatchStates[info.id];
    if (dispatch && known?.generation === dispatch.generation && known.revision > dispatch.revision) return false;
    return this.row(info, observation, dispatch?.generation);
  }
  row(info: SessionInfo, observation?: SessionObservation, establishedGeneration?: string | null): boolean {
    if (!info.id || !this.canApply(observation, establishedGeneration)) return false;
    const store = useSessionStore.getState();
    // Details and events update existing rows in place; opening a session
    // must not move it down the sidebar. Only a new identity is appended.
    const replace = (rows: SessionInfo[], belongs: boolean) => {
      if (!belongs) return rows.filter((row) => row.id !== info.id);
      return rows.some((row) => row.id === info.id)
        ? rows.map((row) => row.id === info.id ? info : row)
        : [...rows, info];
    };
    this.commit(replace(store.sessions, !info.archived), replace(store.archivedSessions, !!info.archived));
    return true;
  }
  patch(id: string, fields: Partial<SessionInfo>, observation?: SessionObservation): boolean {
    if (!this.canConfirm(observation)) return false;
    const store = useSessionStore.getState();
    this.commit(store.sessions.map((row) => row.id === id ? { ...row, ...fields } : row),
      store.archivedSessions.map((row) => row.id === id ? { ...row, ...fields } : row));
    return true;
  }
  archive(id: string, observation?: SessionObservation, info?: SessionInfo): boolean {
    if (!this.canConfirm(observation)) return false;
    this.invalidateTarget(id);
    const store = useSessionStore.getState();
    const old = info ?? store.sessions.find((row) => row.id === id);
    this.commit(store.sessions.filter((row) => row.id !== id), old ?
      [...store.archivedSessions.filter((row) => row.id !== id), { ...old, archived: true }] : store.archivedSessions);
    return true;
  }
  remove(id: string, observation?: SessionObservation): boolean {
    if (!this.canConfirm(observation)) return false;
    this.invalidateTarget(id);
    this.unconfirmedMissing.delete(id);
    const store = useSessionStore.getState();
    const wasActive = store.activeSessionId === id;
    this.commit(store.sessions.filter((row) => row.id !== id), store.archivedSessions.filter((row) => row.id !== id));
    useSessionStore.getState().forgetSession(id);
    for (const listener of this.deletionListeners) listener(id, wasActive);
    return true;
  }
  dispose(): void {
    this.unconfirmedMissing.clear();
    this.deletionListeners.clear();
    this.observations.clear();
    this.closed = true; this.unsubscribe(); this.dirty = false; this.retire({ state: "obsolete" });
  }
}

export const sessions = new SessionList();
