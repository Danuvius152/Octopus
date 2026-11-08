import * as api from "../api/applications";
import { useSessionStore, type Application } from "../stores/sessionStore";
import { observeSession } from "./hydrateSession";
import { pinnedInOrder } from "./sidebarPins";
import { ListWriteError, unreadList, type ListReadState, type RefreshResult } from "./listState";
import type { ApplicationEvent } from "./wsEvents";

type View = "live" | "archived";
export interface ListEventSource { socket: object; accountGeneration: number }
interface Scope {
  account: number;
  generation: string | null;
  token: string;
  socket: object | null;
  connection: number;
}
interface Read {
  view: View;
  scope: Scope;
  change: number;
  intent: symbol | null;
  controller: AbortController;
  timer: ReturnType<typeof setTimeout>;
  promise: Promise<RefreshResult>;
  finish: (result: RefreshResult) => void;
}

/** Application-specific reconciliation; Zustand holds the only row collections.
 * Local changes order observations, never claim to be server revisions.
 */
export class ApplicationList {
  private socket: object | null = null;
  private connection = 0;
  private change = 0;
  private dirty = new Set<View>();
  private active: Read | null = null;
  private last: View = "archived";
  private archivedRequested = false;
  private scheduled = false;
  private intent: symbol | null = null;
  private engaged = false;
  private closed = false;
  private metadataReads = new Set<ReturnType<typeof observeSession>>();
  private unsubscribe: () => void;
  private deletionListeners = new Set<(id: string) => void>();

  constructor() {
    this.unsubscribe = useSessionStore.subscribe((state, before) => {
      const accountChanged = state.accountGeneration !== before.accountGeneration || state.token !== before.token;
      if (!accountChanged && state.dispatchGeneration === before.dispatchGeneration) return;
      this.cancelMetadata();
      this.retire({ state: "obsolete" });
      this.dirty.clear();
      this.change++;
      this.intent = null;
      if (accountChanged) {
        this.engaged = false;
        this.connection++;
        this.socket = null;
        this.archivedRequested = false;
        useSessionStore.setState({ applications: [], archivedApplications: [],
          applicationReads: { live: unreadList(), archived: unreadList() },
          applicationOrdering: false, applicationPinOrder: null });
      } else if (this.engaged) {
        useSessionStore.setState({ applicationOrdering: false, applicationPinOrder: null });
        this.invalidate();
      }
    });
  }

  private scope(): Scope {
    const state = useSessionStore.getState();
    return { account: state.accountGeneration, generation: state.dispatchGeneration, token: state.token, socket: this.socket, connection: this.connection };
  }

  private cancelMetadata(): void {
    for (const read of this.metadataReads) read.cancel();
    this.metadataReads.clear();
  }

  private current(scope: Scope): boolean {
    const now = this.scope();
    return !this.closed && now.account === scope.account && now.generation === scope.generation &&
      now.token === scope.token && now.socket === scope.socket && now.connection === scope.connection;
  }

  private sameAccount(scope: Scope): boolean {
    const now = this.scope();
    return !this.closed && now.account === scope.account && now.token === scope.token;
  }

  /** UI callbacks use this only for their own hints/navigation, never row writes. */
  currentGuard(): () => boolean {
    const scope = this.scope();
    return () => this.current(scope);
  }

  /** A confirmed write belongs to the account even after its connection retires. */
  accountGuard(): () => boolean {
    const scope = this.scope();
    return () => this.sameAccount(scope);
  }

  onDeleted(listener: (id: string) => void): () => void {
    this.deletionListeners.add(listener);
    return () => { this.deletionListeners.delete(listener); };
  }

  private deleted(id: string): void {
    for (const listener of this.deletionListeners) listener(id);
  }

  connect(source: ListEventSource): void {
    if (source.accountGeneration !== useSessionStore.getState().accountGeneration) return;
    this.engaged = true;
    if (this.socket !== source.socket) {
      this.cancelMetadata();
      this.connection++;
      this.retire({ state: "obsolete" });
      this.socket = source.socket;
      this.intent = null;
      useSessionStore.setState({ applicationOrdering: false, applicationPinOrder: null });
    }
    this.invalidate();
  }

  disconnect(source: ListEventSource): void {
    if (source.socket !== this.socket) return;
    this.cancelMetadata();
    this.connection++;
    this.socket = null;
    this.change++;
    this.retire({ state: "obsolete" });
    this.dirty.clear();
    this.intent = null;
    useSessionStore.setState({ applicationOrdering: false, applicationPinOrder: null });
    this.markStale();
  }

  refresh(view: View = "live"): Promise<RefreshResult> {
    this.engaged = true;
    if (!this.scope().token || this.closed) return Promise.resolve({ state: "obsolete" });
    if (view === "archived") this.archivedRequested = true;
    if (this.active?.view === view) return this.active.promise;
    this.dirty.add(view);
    const read = this.active ?? this.startNext();
    return read?.view === view ? read.promise : Promise.resolve({ state: "pending" });
  }

  private setRead(view: View, patch: Partial<ListReadState>): void {
    useSessionStore.setState((state) => ({ applicationReads: { ...state.applicationReads,
      [view]: { ...state.applicationReads[view], ...patch } } }));
  }

  private markStale(): void {
    this.setRead("live", { stale: true });
    if (this.archivedRequested) this.setRead("archived", { stale: true });
  }

  private invalidate(): void {
    this.change++;
    this.dirty.add("live");
    if (this.archivedRequested) this.dirty.add("archived");
    this.markStale();
    this.kick();
  }

  private kick(): void {
    if (this.scheduled || this.closed) return;
    this.scheduled = true;
    void Promise.resolve().then(() => { this.scheduled = false; this.startNext(); });
  }

  private startNext(): Read | null {
    if (this.closed || this.active || !this.scope().token || !this.dirty.size) return this.active;
    const view: View = this.last === "live" && this.dirty.has("archived") ? "archived"
      : this.dirty.has("live") ? "live" : "archived";
    this.last = view;
    this.dirty.delete(view);
    let finish!: Read["finish"];
    const promise = new Promise<RefreshResult>((resolve) => { finish = resolve; });
    const read: Read = {
      view, scope: this.scope(), change: this.change,
      intent: useSessionStore.getState().applicationOrdering ? null : this.intent,
      controller: new AbortController(), promise, finish,
      timer: setTimeout(() => this.fail(read, new Error("Application list timed out after 30 seconds")), 30_000),
    };
    this.active = read;
    this.setRead(view, { loading: true, stale: true, error: null });
    void api.fetchApplications(read.scope.token, { archived: view === "archived", signal: read.controller.signal })
      .then((rows) => this.accept(read, rows), (error) => this.fail(read, error));
    return read;
  }

  private retire(result: RefreshResult): void {
    const read = this.active;
    if (!read) return;
    this.active = null;
    clearTimeout(read.timer);
    read.controller.abort();
    this.setRead(read.view, { loading: false, stale: true });
    read.finish(result);
  }

  private accept(read: Read, rows: Application[]): void {
    if (this.active !== read) return;
    if (!this.current(read.scope)) { this.retire({ state: "obsolete" }); return; }
    if (!Array.isArray(rows)) { this.fail(read, new Error("Invalid application list response")); return; }
    this.active = null;
    clearTimeout(read.timer);
    if (read.change !== this.change) {
      this.dirty.add(read.view);
      this.setRead(read.view, { loading: false, stale: true });
      read.finish({ state: "pending" });
    } else {
      const state = useSessionStore.getState();
      const ids = new Set(rows.map((row) => row.id));
      const live = read.view === "live" ? [] : state.applications.filter((row) => !ids.has(row.id));
      const archived = read.view === "archived" ? [] : state.archivedApplications.filter((row) => !ids.has(row.id));
      for (const row of rows) (row.archived ? archived : live).push(row);
      this.commit(live, archived);
      this.change++;
      if (read.intent && this.intent === read.intent && !useSessionStore.getState().applicationOrdering) {
        this.intent = null;
        useSessionStore.setState({ applicationPinOrder: null });
      }
      this.setRead(read.view, { loaded: true, loading: false, stale: false, error: null });
      read.finish({ state: "applied" });
    }
    this.kick();
  }

  private fail(read: Read, error: unknown): void {
    if (this.active !== read) return;
    if (!this.current(read.scope)) { this.retire({ state: "obsolete" }); return; }
    const message = error instanceof Error ? error.message : "Could not load applications";
    this.retire({ state: "failed", message });
    // A failed attempt ends this automatic round, including earlier dirty marks.
    this.dirty.clear();
    this.setRead(read.view, { error: message });
    if (read.intent && this.intent === read.intent && !useSessionStore.getState().applicationOrdering) {
      this.intent = null;
      useSessionStore.setState({ applicationPinOrder: null });
    }
  }

  private commit(live: Application[], archived: Application[], removed?: string): void {
    const ids = new Set(live.map((row) => row.id));
    useSessionStore.setState((state) => ({ applications: live, archivedApplications: archived,
      unseenFailedApplications: state.unseenFailedApplications.filter((id) => ids.has(id)),
      applicationPinOrder: state.applicationPinOrder?.filter((id) => ids.has(id)) ?? null,
      ...(state.activeApplicationId && !ids.has(state.activeApplicationId) &&
        (state.activeApplicationId === removed || state.applications.some((row) => row.id === state.activeApplicationId))
        ? { activeApplicationId: null, mainView: "chat" as const } : {}),
    }));
  }

  private row(row: Application): void {
    const state = useSessionStore.getState();
    const previous = [...state.applications, ...state.archivedApplications].find((item) => item.id === row.id);
    const merged = { ...previous, ...row };
    const replace = (rows: Application[], belongs: boolean) => {
      const found = rows.some((item) => item.id === row.id);
      return rows.flatMap((item) => item.id === row.id ? (belongs ? [merged] : []) : [item])
        .concat(belongs && !found ? [merged] : []);
    };
    const live = replace(state.applications, !merged.archived);
    const archived = replace(state.archivedApplications, !!merged.archived);
    this.commit(live, archived, merged.archived ? merged.id : undefined);
    this.change++;
  }

  receive(event: ApplicationEvent, source?: ListEventSource): void {
    if (this.closed || (source && (source.socket !== this.socket || source.accountGeneration !== this.scope().account))) return;
    this.engaged = true;
    const type = event.type;
    if (type === "application_created" || type === "application_updated") {
      const row = event.application;
      if (row?.id) {
        const before = useSessionStore.getState();
        const wasFailed = before.applications.find((item) => item.id === row.id)?.status === "failed";
        this.row(row);
        if (row.status === "failed" && !wasFailed && !row.archived &&
          !(before.mainView === "application" && before.activeApplicationId === row.id)) {
          useSessionStore.getState().noteApplicationFailed(row.id);
        }
        if (row.session_id && before.token && ![...before.sessions, ...before.archivedSessions].some((session) => session.id === row.session_id)) {
          const read = observeSession(row.session_id, "metadata");
          this.metadataReads.add(read);
          void read.done.catch(() => {}).finally(() => this.metadataReads.delete(read));
        }
      }
    } else if (type === "application_archived" || type === "application_deleted") {
      const state = useSessionStore.getState();
      const row = event.type === "application_archived" ? event.application : undefined;
      const id = event.application_id ?? row?.id;
      if (id && type === "application_deleted") this.deleted(id);
      if (row && type === "application_archived") this.row({ ...row, archived: true });
      else if (id) this.commit(state.applications.filter((item) => item.id !== id),
        type === "application_deleted" ? state.archivedApplications.filter((item) => item.id !== id) : state.archivedApplications, id);
    } else return;
    this.invalidate();
  }

  private async write<T>(call: (token: string) => Promise<T>, apply: (value: T) => void,
    order?: { ids?: string[] }): Promise<T> {
    const scope = this.scope();
    if (!scope.token || this.closed) throw new ListWriteError("Sign in to change applications", "refused");
    if (order && useSessionStore.getState().applicationOrdering) throw new ListWriteError("Sidebar order is still being saved", "refused");
    const intent = order ? Symbol() : null;
    if (intent) {
      this.intent = intent;
      useSessionStore.setState({ applicationOrdering: true, applicationPinOrder: order?.ids ?? null });
    }
    this.engaged = true;
    // Start invalidates reads but does not require another GET immediately;
    // refreshes and events may still read during a long write.
    this.change++;
    this.markStale();
    const change = this.change;
    let applied = false;
    try {
      const value = await call(scope.token);
      if (this.current(scope) && this.change === change) { apply(value); applied = true; }
      // A confirmed write from a retired connection still changed this
      // account. Confirm with a current read without applying its old row.
      else if (!this.current(scope) && this.sameAccount(scope)) this.invalidate();
      return value;
    } catch (error) {
      if (!this.current(scope)) throw new ListWriteError("The operation belongs to an earlier connection", "obsolete");
      if (intent === this.intent && intent) {
        this.intent = null;
        useSessionStore.setState({ applicationPinOrder: null });
      }
      if (error instanceof ListWriteError) throw error;
      throw new ListWriteError(error instanceof Error ? error.message : "Application result is unknown", "unknown");
    } finally {
      if (this.current(scope)) {
        if (intent && intent === this.intent) {
          useSessionStore.setState({ applicationOrdering: false });
          if (applied) { this.intent = null; useSessionStore.setState({ applicationPinOrder: null }); }
          // Retire pre-confirmation reads. Their late errors/receipts cannot
          // clear this intent or block its post-write confirmation.
          this.retire({ state: "pending" });
        } else if (intent && !this.intent) useSessionStore.setState({ applicationOrdering: false });
        this.invalidate();
      }
    }
  }

  create(input: api.NewApplication): Promise<Application> { return this.write((token) => api.createApplication(token, input), (row) => this.row(row)); }
  build(id: string, prompt: string): Promise<Application> { return this.write((token) => api.buildApplication(token, id, prompt), (row) => this.row(row)); }
  async remove(id: string, keepFiles: boolean): Promise<void> {
    const accountCurrent = this.accountGuard();
    await this.write((token) => api.deleteApplication(token, id, keepFiles), () => {
      const state = useSessionStore.getState();
      this.commit(state.applications.filter((row) => row.id !== id), state.archivedApplications.filter((row) => row.id !== id), id);
    });
    // Deletion is a confirmed account fact even when row admission was skipped.
    if (accountCurrent()) this.deleted(id);
  }
  archive(id: string): Promise<Application> { return this.write((token) => api.archiveApplication(token, id), (row) => this.row(row)); }
  restore(id: string): Promise<Application> { return this.write((token) => api.unarchiveApplication(token, id), (row) => this.row(row)); }
  setPinned(id: string, pinned: boolean): Promise<Application> { return this.write((token) => api.setApplicationPinned(token, id, pinned), (row) => this.row(row), {}); }
  reorder(ids: string[]): Promise<Application[]> {
    return this.write((token) => api.reorderApplicationPins(token, ids), (rows) => {
      this.commit(rows.filter((row) => !row.archived), useSessionStore.getState().archivedApplications.filter((row) => !rows.some((item) => item.id === row.id)));
      this.change++;
    }, { ids });
  }

  dispose(): void { this.cancelMetadata(); this.closed = true; this.unsubscribe(); this.dirty.clear(); this.retire({ state: "obsolete" }); }
}

export const applications = new ApplicationList();

/** Overlay only ID order on the latest rows; newly pinned rows remain visible. */
export function orderedApplicationPins(rows: Application[], order: string[] | null): Application[] {
  const pinned = pinnedInOrder(rows);
  if (!order) return pinned;
  const byId = new Map(pinned.map((row) => [row.id, row]));
  const result: Application[] = [];
  for (const id of order) {
    const row = byId.get(id);
    if (row) { result.push(row); byId.delete(id); }
  }
  return [...result, ...pinned.filter((row) => byId.has(row.id))];
}
