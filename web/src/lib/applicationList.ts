import * as api from "../api/applications";
import { useSessionStore, type Application } from "../stores/sessionStore";
import { observeSession } from "./hydrateSession";
import { pinnedInOrder } from "./sidebarPins";
import { ListWriteError, unreadList, type ListReadState, type RefreshResult } from "./listState";
import { RequestDeadline } from "./requestDeadline";
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
  order: number;
  intent: symbol | null;
  controller: AbortController;
  timer: ReturnType<typeof setTimeout>;
  promise: Promise<RefreshResult>;
  finish: (result: RefreshResult) => void;
}

type CreationState = "live" | "archived" | "deleting" | "deleted" | "unconfirmed";
type RetryableCreation = { id: string; checkAgain: () => Promise<ApplicationCreation> };
export type ApplicationCreation = { state: "deleted"; id: string } | (RetryableCreation & (
  | { state: "live"; canOpen: () => boolean }
  | { state: Exclude<CreationState, "live" | "deleted"> }
));
type CreationObservation = View | "created" | "deleting" | "deleted" | "hint" | "archiveHint"
  | "missingLive" | "missingArchived";
interface ObservedAt {
  scope: Scope;
  order: number;
}
interface CreationRead {
  scope: Scope;
  deadline: RequestDeadline;
  fields: Partial<Application>;
}
interface CreationWatch {
  scope: Scope;
  lifetime: AbortSignal;
  id: string | null;
  facts: Map<string, Partial<Record<CreationObservation, ObservedAt>>>;
  read?: CreationRead;
}
interface RowObservation {
  order?: number;
  fieldsOnly?: boolean;
}

const creationState = (row: Application): View | "deleting" =>
  row.deletion != null ? "deleting" : row.archived ? "archived" : "live";

/** Application-specific reconciliation; Zustand holds the only row collections.
 * Local changes order observations, never claim to be server revisions.
 */
export class ApplicationList {
  private socket: object | null = null;
  private socketGeneration: string | null = null;
  private connection = 0;
  private change = 0;
  private observationOrder = 0;
  private dirty = new Set<View>();
  private active: Read | null = null;
  private last: View = "archived";
  private archivedRequested = false;
  private scheduled = false;
  private intent: symbol | null = null;
  private engaged = false;
  private closed = false;
  private metadataReads = new Set<ReturnType<typeof observeSession>>();
  private pendingFailureIds = new Set<string>();
  private unsubscribe: () => void;
  private deletionListeners = new Set<(id: string) => void>();
  private creationWatches = new Set<CreationWatch>();
  private confirmedDeletionRows = new WeakMap<Application, number>();

  constructor() {
    this.unsubscribe = useSessionStore.subscribe((state, before) => {
      const open = state.activeApplicationId;
      if (state.mainView === "application" && open && this.pendingFailureIds.has(open) &&
        state.applications.some((app) => app.id === open && app.status === "failed")) {
        this.pendingFailureIds.delete(open);
      }
      const accountChanged = state.accountGeneration !== before.accountGeneration || state.token !== before.token;
      if (!accountChanged && state.dispatchGeneration === before.dispatchGeneration) return;
      this.confirmedDeletionRows = new WeakMap();
      if (accountChanged) for (const watch of this.creationWatches) watch.read?.deadline.finish();
      // A new socket can deliver App events before proving which generation it serves.
      const awaitingSocketGeneration = !accountChanged && this.socket !== null && this.socketGeneration === null;
      this.retireEventObservations(awaitingSocketGeneration);
      this.retire({ state: "obsolete" });
      this.dirty.clear();
      this.change++;
      this.intent = null;
      if (accountChanged) {
        this.engaged = false;
        this.connection++;
        this.socket = null;
        this.socketGeneration = null;
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

  private retireEventObservations(preserveFailureHints = false): void {
    for (const read of this.metadataReads) read.cancel();
    this.metadataReads.clear();
    if (!preserveFailureHints) this.pendingFailureIds.clear();
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

  onDeleted(listener: (id: string) => void): () => void {
    this.deletionListeners.add(listener);
    return () => { this.deletionListeners.delete(listener); };
  }

  private deleted(id: string, scope = this.scope()): void {
    if (scope.generation === this.scope().generation) this.observeCreation(id, "deleted");
    this.pendingFailureIds.delete(id);
    for (const listener of this.deletionListeners) listener(id);
  }

  connect(source: ListEventSource): void {
    if (source.accountGeneration !== useSessionStore.getState().accountGeneration) return;
    this.engaged = true;
    if (this.socket !== source.socket) {
      this.retireEventObservations();
      this.connection++;
      this.retire({ state: "obsolete" });
      this.socket = source.socket;
      this.socketGeneration = null;
      this.intent = null;
      useSessionStore.setState({ applicationOrdering: false, applicationPinOrder: null });
    }
    this.invalidate();
  }

  observeSocketGeneration(source: ListEventSource, generation: string): void {
    const scope = this.scope();
    if (this.closed || source.socket !== scope.socket || source.accountGeneration !== scope.account) return;
    if (generation !== scope.generation && this.socketGeneration !== null) return;
    this.socketGeneration = generation;
    if (generation !== scope.generation) this.pendingFailureIds.clear();
  }

  disconnect(source: ListEventSource): void {
    if (source.socket !== this.socket) return;
    this.retireEventObservations();
    this.connection++;
    this.socket = null;
    this.socketGeneration = null;
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
      order: ++this.observationOrder,
      intent: view === "live" && !useSessionStore.getState().applicationOrdering ? this.intent : null,
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
      this.commit(live, archived, { order: read.order, snapshot: read.view });
      if (read.view === "live") this.confirmFailures(live);
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

  private confirmFailures(live: Application[]): void {
    if (!this.pendingFailureIds.size) return;
    const state = useSessionStore.getState();
    const open = state.mainView === "application" ? state.activeApplicationId : null;
    for (const app of live) {
      if (app.status === "failed" && app.id !== open && this.pendingFailureIds.has(app.id)) {
        state.noteApplicationFailed(app.id);
      }
    }
    this.pendingFailureIds.clear();
  }

  private fail(read: Read, error: unknown): void {
    if (this.active !== read) return;
    if (!this.current(read.scope)) { this.retire({ state: "obsolete" }); return; }
    const message = error instanceof Error ? error.message : "Could not load applications";
    this.retire({ state: "failed", message });
    this.setRead(read.view, { error: message });
    if (read.intent && this.intent === read.intent && !useSessionStore.getState().applicationOrdering) {
      this.intent = null;
      useSessionStore.setState({ applicationPinOrder: null });
    }
    // Starting the read consumed its request. Any remaining work is still due.
    this.kick();
  }

  private commit(live: Application[], archived: Application[],
    observation: RowObservation & { removed?: string; snapshot?: View } = {},
  ): void {
    const before = useSessionStore.getState();
    const previous = new Map([...before.applications, ...before.archivedApplications].map((row) => [row.id, row]));
    const order = observation.order ?? ++this.observationOrder;
    const project = (row: Application) => {
      const prior = previous.get(row.id);
      previous.delete(row.id);
      if (row === prior) return row;
      // A stale snapshot or update cannot clear this generation's deletion marker.
      const priorMark = row.deletion == null && prior?.deletion != null ? this.confirmedDeletionRows.get(prior) : undefined;
      const accepted = priorMark === undefined ? row : { ...row, deletion: prior?.deletion };
      const proofOrder = priorMark ?? order;
      if (accepted.deletion != null) this.confirmedDeletionRows.set(accepted, proofOrder);
      if (!observation.fieldsOnly || row.deletion != null) this.observeCreation(row.id, creationState(accepted), proofOrder);
      return accepted;
    };
    live = live.map(project);
    archived = archived.map(project);
    if (observation.snapshot && this.creationWatches.size) {
      const view = observation.snapshot;
      const present = new Set((view === "live" ? live : archived).map(row => row.id));
      const relevant = new Set(previous.keys());
      for (const watch of this.creationWatches) {
        if (!this.sameAccount(watch.scope)) continue;
        if (watch.id !== null) relevant.add(watch.id);
        else {
          for (const [id, { created }] of watch.facts) {
            if (created && this.current(created.scope)) relevant.add(id);
          }
        }
      }
      for (const id of relevant) {
        if (!present.has(id)) this.observeCreation(id, view === "live" ? "missingLive" : "missingArchived", order);
      }
    }
    const ids = new Set(live.map((row) => row.id));
    useSessionStore.setState((state) => ({ applications: live, archivedApplications: archived,
      unseenFailedApplications: state.unseenFailedApplications.filter((id) => ids.has(id)),
      applicationPinOrder: state.applicationPinOrder?.filter((id) => ids.has(id)) ?? null,
      ...(state.activeApplicationId && !ids.has(state.activeApplicationId) &&
        (state.activeApplicationId === observation.removed || state.applications.some((row) => row.id === state.activeApplicationId))
        ? { activeApplicationId: null, mainView: "chat" as const } : {}),
    }));
  }

  private row(row: Application, observation?: RowObservation): void {
    const state = useSessionStore.getState();
    const previous = [...state.applications, ...state.archivedApplications].find((item) => item.id === row.id);
    const merged = { ...previous, ...row };
    if (previous?.deletion != null && row.deletion === undefined) {
      delete merged.deletion;
    }
    const replace = (rows: Application[], belongs: boolean) => {
      const found = rows.some((item) => item.id === row.id);
      return rows.flatMap((item) => item.id === row.id ? (belongs ? [merged] : []) : [item])
        .concat(belongs && !found ? [merged] : []);
    };
    const live = replace(state.applications, !merged.archived);
    const archived = replace(state.archivedApplications, !!merged.archived);
    this.commit(live, archived, { ...observation, removed: merged.archived ? merged.id : undefined });
    this.change++;
  }

  receive(event: ApplicationEvent, source?: ListEventSource): void {
    const scope = this.scope();
    if (this.closed || (source && (source.socket !== scope.socket || source.accountGeneration !== scope.account ||
      (this.socketGeneration !== null && this.socketGeneration !== scope.generation)))) return;
    this.engaged = true;
    const type = event.type;
    if (type === "application_created" || type === "application_updated") {
      const row = event.application;
      if (row?.id) {
        const before = useSessionStore.getState();
        const live = before.applications.find((item) => item.id === row.id);
        const update = type === "application_updated" && !row.archived && live;
        // HTTP observations supply list membership and sidebar placement.
        if (update) {
          this.observeCreationFields(row);
          this.row({ ...row, archived: update.archived, pinned: update.pinned, pin_order: update.pin_order }, { fieldsOnly: true });
        } else if (row.deletion != null) this.observeCreation(row.id, "deleting");
        else if (type === "application_created") this.observeCreation(row.id, "created");
        else this.observeCreation(row.id, row.archived ? "archiveHint" : "hint");
        if (!row.archived) {
          if (row.status === "failed" && live?.status !== "failed" &&
            !(before.mainView === "application" && before.activeApplicationId === row.id)) {
            if (type === "application_updated" && live) before.noteApplicationFailed(row.id);
            else this.pendingFailureIds.add(row.id);
          }
        }
        if (row.session_id && before.token && ![...before.sessions, ...before.archivedSessions].some((session) => session.id === row.session_id)) {
          const read = observeSession(row.session_id, "metadata");
          this.metadataReads.add(read);
          void read.done.catch(() => {}).finally(() => this.metadataReads.delete(read));
        }
      }
    } else if (type === "application_deleted") {
      this.removeObserved(event.application_id);
    } else if (type === "application_archived") {
      const id = event.application_id ?? event.application?.id;
      if (id) {
        this.observeCreation(id, "archiveHint");
        if (event.application?.deletion != null) this.observeCreation(id, "deleting");
      }
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

  private observeCreation(id: string, state: CreationObservation, order = ++this.observationOrder, scope = this.scope()): void {
    if (!this.creationWatches.size) return;
    for (const watch of this.creationWatches) {
      if (!this.sameAccount(watch.scope) || watch.id !== null && watch.id !== id) continue;
      this.recordCreation(watch, id, state, { scope, order });
      // Until removal is known, the dedicated read can still confirm a 404.
      if (watch.id === id && state === "deleted") watch.read?.deadline.finish();
    }
  }

  private recordCreation(watch: CreationWatch, id: string, state: CreationObservation, observation: ObservedAt): void {
    const facts = watch.facts.get(id) ?? {};
    const previous = facts[state];
    if (previous && this.current(previous.scope) && previous.order > observation.order) return;
    facts[state] = observation;
    watch.facts.set(id, facts);
  }

  private observeCreationFields(row: Application): void {
    if (!this.creationWatches.size) return;
    const fields: Partial<Application> = { ...row };
    delete fields.archived;
    delete fields.pinned;
    delete fields.pin_order;
    delete fields.deletion;
    for (const watch of this.creationWatches) {
      const read = watch.read;
      if (watch.id !== row.id || !read || !this.current(read.scope)) continue;
      Object.assign(read.fields, fields);
    }
  }

  private creationEvidence(watch: CreationWatch, id: string) {
    const facts = this.sameAccount(watch.scope) ? watch.facts.get(id) : undefined;
    const orderOf = (state: CreationObservation) => {
      const fact = facts?.[state];
      if (!fact) return -1;
      if (this.current(fact.scope)) return fact.order;
      // Deletion advances only from marked to removed in the same service.
      if ((state === "deleting" || state === "deleted") && fact.scope.generation === this.scope().generation) return fact.order;
      return -1;
    };
    const hint = orderOf("hint");
    const blocked = {
      live: Math.max(hint, orderOf("archiveHint"), orderOf("missingLive")),
      archived: Math.max(hint, orderOf("missingArchived")),
    };
    const live = orderOf("live");
    const archived = orderOf("archived");
    const view = live > archived ? "live" : "archived";
    const memberOrder = Math.max(live, archived);
    const membership = memberOrder > blocked[view] ? view : undefined;
    return { blocked, memberOrder, membership, deleted: orderOf("deleted") >= 0, deleting: orderOf("deleting") } as const;
  }

  private creationResult(watch: CreationWatch, id: string, state: CreationState): ApplicationCreation {
    if (state === "deleted") return { state, id };
    const checkAgain = () => this.confirmCreation(watch, id);
    if (state !== "live") return { state, id, checkAgain };
    const scope = this.scope();
    const canOpen = () => {
      if (watch.lifetime.aborted || !this.current(scope)) return false;
      const { membership, deleted, deleting } = this.creationEvidence(watch, id);
      return membership === "live" && !deleted && deleting < 0 && useSessionStore.getState().applications
        .some((row) => row.id === id && creationState(row) === "live");
    };
    return { state, id, checkAgain, canOpen };
  }

  private removeObserved(id: string): void {
    const state = useSessionStore.getState();
    this.deleted(id);
    this.commit(state.applications.filter((row) => row.id !== id), state.archivedApplications.filter((row) => row.id !== id), { removed: id });
  }

  private admitCreationRow(watch: CreationWatch, row: Application, order?: number): ApplicationCreation {
    this.row(row, { order });
    const evidence = this.creationEvidence(watch, row.id);
    const state = evidence.deleted ? "deleted" : evidence.deleting >= 0 ? "deleting"
      : evidence.membership ?? "unconfirmed";
    return this.creationResult(watch, row.id, state);
  }

  private admitCreationDeletion(id: string, deletion: NonNullable<Application["deletion"]>,
    scope: Scope, order = ++this.observationOrder,
  ): void {
    const { applications, archivedApplications } = useSessionStore.getState();
    const existing = this.current(scope) && [...applications, ...archivedApplications].find((row) => row.id === id);
    // A confirmed marker can update an existing row without admitting stale membership.
    if (existing) this.row({ ...existing, deletion }, { order, fieldsOnly: true });
    else this.observeCreation(id, "deleting", order, scope);
  }

  private async readCreation(watch: CreationWatch, id: string): Promise<ApplicationCreation> {
    if (!this.sameAccount(watch.scope)) return this.creationResult(watch, id, "unconfirmed");
    if (this.creationEvidence(watch, id).deleted) return { state: "deleted", id };
    const scope = this.scope();
    const order = ++this.observationOrder;
    const deadline = new RequestDeadline();
    const read = { scope, deadline, fields: {} };
    watch.read = read;
    try {
      const row = await deadline.wait(api.fetchApplication(scope.token, id, deadline.signal)).catch(() => null);
      const { blocked, memberOrder, membership, deleted, deleting } = this.creationEvidence(watch, id);
      if (deleted) return { state: "deleted", id };
      const current = this.current(scope) && deadline.active;
      const newer = memberOrder > order;
      if (row === "missing") {
        if (current) {
          this.removeObserved(id);
          this.invalidate();
          return { state: "deleted", id };
        }
      } else if (current && row?.id === id && typeof row.archived === "boolean") {
        const view = row.archived ? "archived" : "live";
        const canApply = !newer && blocked[view] <= order && (row.deletion != null ? deleting <= order : deleting < 0);
        if (canApply) {
          return this.admitCreationRow(watch, { ...row, ...read.fields }, order);
        }
        if (row.deletion != null) {
          if (deleting <= order) this.admitCreationDeletion(id, row.deletion, scope, order);
          return this.creationResult(watch, id, "deleting");
        }
        if (!newer && deleting < 0 && blocked[view] > order) {
          // The later observation blocks this row. It still supersedes an
          // earlier observation from the opposite partition for this retry.
          this.recordCreation(watch, id, view, { scope, order });
          return this.creationResult(watch, id, "unconfirmed");
        }
      }
      if (deleting >= 0) return this.creationResult(watch, id, "deleting");
      if (membership && (newer || membership === "archived")) return this.creationResult(watch, id, membership);
      return this.creationResult(watch, id, "unconfirmed");
    } finally {
      watch.read = undefined;
      deadline.finish();
    }
  }

  async create(input: api.NewApplication, lifetime: AbortSignal): Promise<ApplicationCreation> {
    const scope = this.scope();
    if (!scope.token || this.closed) throw new ListWriteError("Sign in to change applications", "refused");
    const watch: CreationWatch = { scope, lifetime, id: null, facts: new Map() };
    this.creationWatches.add(watch);
    this.engaged = true;
    this.change++;
    this.markStale();
    let result: ApplicationCreation | undefined;
    try {
      result = await this.postCreation(watch, input);
      return result;
    } finally {
      if (result && result.state !== "deleted" && !lifetime.aborted && this.sameAccount(scope)) {
        lifetime.addEventListener("abort", () => {
          if (!watch.read) this.creationWatches.delete(watch);
        }, { once: true });
      } else this.creationWatches.delete(watch);
      if (this.sameAccount(scope)) this.invalidate();
    }
  }

  private async postCreation(watch: CreationWatch, input: api.NewApplication): Promise<ApplicationCreation> {
    const { scope } = watch;
    const deadline = new RequestDeadline();
    let row: Application | null;
    try {
      row = await deadline.wait(api.createApplication(scope.token, input, deadline.signal));
    } catch (error) {
      if (error instanceof api.ApplicationHttpError && error.status === 409) {
        const detail = error.detail;
        if (detail && typeof detail === "object" && "code" in detail && detail.code === "application_deleted_during_creation" &&
            "application_id" in detail && typeof detail.application_id === "string") {
          if (this.current(scope)) this.removeObserved(detail.application_id);
          return { state: "deleted", id: detail.application_id };
        }
      }
      if (error instanceof ListWriteError && error.outcome === "refused") throw error;
      row = null;
    } finally {
      deadline.finish();
    }
    if (!row || typeof row.id !== "string" || !row.id) {
      throw new Error("Creation outcome is unknown. Check the applications list before trying again.");
    }
    const id = watch.id = row.id;
    const facts = watch.facts.get(id);
    watch.facts.clear();
    if (facts) watch.facts.set(id, facts);
    const { blocked, memberOrder, membership, deleted, deleting } = this.creationEvidence(watch, id);
    if (deleted) return { state: "deleted", id };
    if (deleting >= 0) return this.creationResult(watch, id, "deleting");
    const current = this.current(scope);
    const view = typeof row.archived === "boolean" ? row.archived ? "archived" : "live" : undefined;
    if (current && view && memberOrder < 0 && blocked[view] < 0) {
      return this.admitCreationRow(watch, row);
    }
    if (row.deletion != null && this.sameAccount(scope) && scope.generation === this.scope().generation) {
      this.admitCreationDeletion(id, row.deletion, scope);
      return this.creationResult(watch, id, "deleting");
    }
    // The final POST read and a concurrent list can capture either side of
    // archive or restore. Resolve conflicting membership by ID.
    if (membership && (!view || membership === view)) {
      return this.creationResult(watch, id, membership);
    }
    return this.readCreation(watch, id);
  }

  private async confirmCreation(watch: CreationWatch, id: string): Promise<ApplicationCreation> {
    if (watch.lifetime.aborted || !this.creationWatches.has(watch) || watch.read) {
      return this.creationResult(watch, id, "unconfirmed");
    }
    const result = await this.readCreation(watch, id);
    if (watch.lifetime.aborted || result.state === "deleted") this.creationWatches.delete(watch);
    return result;
  }
  build(id: string, prompt: string): Promise<Application> { return this.write((token) => api.buildApplication(token, id, prompt), (row) => this.row(row)); }
  async remove(id: string, keepFiles: boolean): Promise<void> {
    const scope = this.scope();
    await this.write((token) => api.deleteApplication(token, id, keepFiles), () => {
      const state = useSessionStore.getState();
      this.commit(state.applications.filter((row) => row.id !== id), state.archivedApplications.filter((row) => row.id !== id), { removed: id });
    });
    // Deletion is a confirmed account fact even when row admission was skipped.
    if (this.sameAccount(scope)) this.deleted(id, scope);
  }
  archive(id: string): Promise<Application> { return this.write((token) => api.archiveApplication(token, id), (row) => this.row(row)); }
  restore(id: string): Promise<Application> { return this.write((token) => api.unarchiveApplication(token, id), (row) => this.row(row)); }
  setPinned(id: string, pinned: boolean): Promise<Application> { return this.write((token) => api.setApplicationPinned(token, id, pinned), (row) => this.row(row), {}); }
  reorder(ids: string[]): Promise<Application[]> {
    return this.write((token) => api.reorderApplicationPins(token, ids), (rows) => {
      this.commit(rows.filter((row) => !row.archived),
        useSessionStore.getState().archivedApplications.filter((row) => !rows.some((item) => item.id === row.id)),
        { snapshot: "live" });
      this.change++;
    }, { ids });
  }

  dispose(): void { this.retireEventObservations(); this.closed = true; this.unsubscribe(); this.dirty.clear(); this.retire({ state: "obsolete" }); }
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
