import { accountFetch } from "./accountFetch";
import { useSessionStore, type Schedule } from "../stores/sessionStore";
import type { ListEventSource } from "./applicationList";
import { ListWriteError, unreadList, type ListReadState, type RefreshResult } from "./listState";

interface Scope { account: number; generation: string | null; token: string; connection: number }
interface Read {
  scope: Scope; change: number; controller: AbortController; timer: ReturnType<typeof setTimeout>;
  promise: Promise<RefreshResult>; finish: (result: RefreshResult) => void;
}
export interface ScheduleFromTextInput { agent_id: string; text: string; timezone: string; session_id: string }

/** Notifications invalidate this domain; they never supply inferred schedule rows. */
export class ScheduleList {
  private socket: object | null = null;
  private connection = 0;
  private change = 0;
  private dirty = false;
  private active: Read | null = null;
  private scheduled = false;
  private engaged = false;
  private closed = false;
  private unsubscribe: () => void;

  constructor() {
    this.unsubscribe = useSessionStore.subscribe((state, before) => {
      const accountChanged = state.accountGeneration !== before.accountGeneration || state.token !== before.token;
      if (!accountChanged && state.dispatchGeneration === before.dispatchGeneration) return;
      this.retire({ state: "obsolete" }); this.dirty = false; this.change++;
      if (accountChanged) {
        this.engaged = false;
        this.socket = null; this.connection++;
        useSessionStore.setState({ schedules: [], scheduleRead: unreadList() });
      } else if (this.engaged) this.invalidate();
    });
  }

  private scope(): Scope {
    const state = useSessionStore.getState();
    return { account: state.accountGeneration, generation: state.dispatchGeneration, token: state.token, connection: this.connection };
  }
  private current(scope: Scope): boolean {
    const now = this.scope();
    return !this.closed && scope.account === now.account && scope.generation === now.generation &&
      scope.token === now.token && scope.connection === now.connection;
  }
  private sameAccount(scope: Scope): boolean {
    const now = this.scope();
    return !this.closed && scope.account === now.account && scope.token === now.token;
  }
  currentGuard(): () => boolean { const scope = this.scope(); return () => this.current(scope); }
  accountGuard(): () => boolean { const scope = this.scope(); return () => this.sameAccount(scope); }
  private state(patch: Partial<ListReadState>): void {
    useSessionStore.setState((state) => ({ scheduleRead: { ...state.scheduleRead, ...patch } }));
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
    this.socket = null; this.connection++; this.change++;
    this.retire({ state: "obsolete" }); this.dirty = false; this.state({ stale: true });
  }
  changed(source?: ListEventSource): void {
    if (source && (source.socket !== this.socket || source.accountGeneration !== this.scope().account)) return;
    this.engaged = true; this.invalidate();
  }

  refresh(): Promise<RefreshResult> {
    this.engaged = true;
    if (this.closed || !this.scope().token) return Promise.resolve({ state: "obsolete" });
    if (this.active) return this.active.promise;
    this.dirty = true;
    return this.start()!.promise;
  }
  private invalidate(): void {
    this.change++; this.dirty = true; this.state({ stale: true }); this.kick();
  }
  private kick(): void {
    if (this.scheduled || this.closed) return;
    this.scheduled = true;
    void Promise.resolve().then(() => { this.scheduled = false; this.start(); });
  }
  private start(): Read | null {
    if (this.closed || this.active || !this.dirty || !this.scope().token) return this.active;
    this.dirty = false;
    let finish!: Read["finish"];
    const promise = new Promise<RefreshResult>((resolve) => { finish = resolve; });
    const read: Read = { scope: this.scope(), change: this.change, controller: new AbortController(), promise, finish,
      timer: setTimeout(() => this.fail(read, new Error("Schedule list timed out after 30 seconds")), 30_000) };
    this.active = read; this.state({ loading: true, stale: true, error: null });
    void this.request<Schedule[]>("/api/schedules", read.scope.token, { signal: read.controller.signal })
      .then((rows) => {
        if (this.active !== read) return;
        if (!this.current(read.scope)) { this.retire({ state: "obsolete" }); return; }
        if (!Array.isArray(rows)) { this.fail(read, new Error("Invalid schedule list response")); return; }
        if (this.change !== read.change) {
          this.retire({ state: "pending" }); this.dirty = true;
        } else {
          useSessionStore.setState({ schedules: rows }); this.change++;
          this.retire({ state: "applied" }); this.state({ loaded: true, stale: false, error: null });
        }
        this.kick();
      }, (error) => this.fail(read, error));
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
    if (!this.current(read.scope)) { this.retire({ state: "obsolete" }); return; }
    const message = error instanceof Error ? error.message : "Could not load schedules";
    this.retire({ state: "failed", message }); this.dirty = false; this.state({ error: message });
  }

  private async request<T>(path: string, token: string, init: RequestInit = {}): Promise<T> {
    const response = await accountFetch(`${window.location.origin}${path}`, { ...init,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" } });
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw new ListWriteError(typeof body?.detail === "string" ? body.detail : `HTTP ${response.status}`,
        response.status < 500 && response.status !== 408 ? "refused" : "unknown");
    }
    return (response.status === 204 ? undefined : await response.json()) as T;
  }
  private async write<T>(path: string, method: string, body?: unknown): Promise<T> {
    const scope = this.scope();
    if (!scope.token || this.closed) throw new ListWriteError("Sign in to change schedules", "refused");
    this.engaged = true; this.change++; this.state({ stale: true });
    const change = this.change;
    try {
      const value = await this.request<T>(path, scope.token, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      if (this.current(scope) && change === this.change && method === "POST") {
        const row = value as Schedule;
        useSessionStore.setState((state) => ({ schedules: [...state.schedules.filter((item) => item.id !== row.id), row] }));
        this.change++;
      }
      if (!this.current(scope) && this.sameAccount(scope)) this.invalidate();
      return value;
    } catch (error) {
      if (!this.current(scope)) throw new ListWriteError("The operation belongs to an earlier connection", "obsolete");
      if (error instanceof ListWriteError) throw error;
      throw new ListWriteError(error instanceof Error ? error.message : "Schedule result is unknown", "unknown");
    } finally { if (this.current(scope)) this.invalidate(); }
  }
  createFromText(input: ScheduleFromTextInput): Promise<Schedule> {
    const { agent_id, ...body } = input;
    return this.write(`/api/agents/${encodeURIComponent(agent_id)}/schedules/from_text`, "POST", body);
  }
  async setEnabled(id: string, enabled: boolean): Promise<void> { await this.write(`/api/schedules/${encodeURIComponent(id)}`, "PATCH", { enabled }); }
  async setWikiMode(id: string, wiki_mode: string): Promise<void> { await this.write(`/api/schedules/${encodeURIComponent(id)}`, "PATCH", { wiki_mode }); }
  async remove(id: string): Promise<void> { await this.write(`/api/schedules/${encodeURIComponent(id)}`, "DELETE"); }
  dispose(): void { this.closed = true; this.unsubscribe(); this.dirty = false; this.retire({ state: "obsolete" }); }
}

export const schedules = new ScheduleList();
