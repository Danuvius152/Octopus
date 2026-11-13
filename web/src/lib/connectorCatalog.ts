import { createCustomConnector, deleteCustomConnector, fetchCatalog, setOAuthClient } from "../api/connectors";
import type { ConnectorCatalogEntry, CustomConnectorCreateRequest } from "../api";
import { useSessionStore } from "../stores/sessionStore";
import { connectorInstallations } from "./connectorInstallations";
import { unreadList, type ListReadState, type RefreshResult } from "./listState";

interface Account { token: string; accountGeneration: number }
interface Read {
  account: Account; controller: AbortController; timer: ReturnType<typeof setTimeout>;
  promise: Promise<RefreshResult>; finish: (result: RefreshResult) => void;
}

/** Publishes catalog observations; Zustand owns the only catalog array.
 * Accepted reads and writes replace the observation used by write receipts.
 * A confirmed mutation retires every overlapping GET before one confirmation.
 */
class ConnectorCatalog {
  private active: Read | null = null;
  private observation = {};
  // Only in-flight removals need identity beyond the catalog observation:
  // a later same-slug creation can replace their target before they reply.
  private removals = new Set<{ kind: string; replaced: boolean }>();

  constructor() {
    useSessionStore.subscribe((state, before) => {
      if (state.token !== before.token || state.accountGeneration !== before.accountGeneration) {
        this.observation = {};
        this.removals.clear();
        this.retire({ state: "obsolete" });
        this.state(unreadList());
      } else if (state.dispatchGeneration !== before.dispatchGeneration) {
        this.observation = {};
        if (this.active) { this.retire({ state: "pending" }); void this.refresh(); }
      }
    });
  }

  private account(): Account {
    const { token, accountGeneration } = useSessionStore.getState();
    return { token, accountGeneration };
  }
  private current(account: Account): boolean {
    const state = useSessionStore.getState();
    return !!account.token && account.token === state.token && account.accountGeneration === state.accountGeneration;
  }
  private state(patch: Partial<ListReadState>): void {
    useSessionStore.setState((state) => ({ connectorCatalogRead: { ...state.connectorCatalogRead, ...patch } }));
  }
  private retire(result: RefreshResult): void {
    const read = this.active;
    if (!read) return;
    this.active = null;
    clearTimeout(read.timer); read.controller.abort(); read.finish(result);
  }
  refresh(): Promise<RefreshResult> {
    const account = this.account();
    if (!account.token) return Promise.resolve({ state: "obsolete" });
    if (this.active) return this.active.promise;
    let finish!: Read["finish"];
    const promise = new Promise<RefreshResult>((resolve) => { finish = resolve; });
    const read: Read = { account, controller: new AbortController(), promise, finish,
      timer: setTimeout(() => this.fail(read, new Error("Connector catalog timed out after 30 seconds")), 30_000) };
    this.active = read;
    this.state({ loading: true, stale: true, error: null });
    void fetchCatalog(account.token, read.controller.signal).then((rows) => {
      if (this.active !== read || !this.current(account)) return;
      if (!Array.isArray(rows)) { this.fail(read, new Error("Invalid connector catalog response")); return; }
      this.observation = {};
      useSessionStore.setState({ connectorCatalog: rows });
      this.retire({ state: "applied" });
      this.state({ loaded: true, loading: false, stale: false, error: null });
    }, (error) => this.fail(read, error));
    return promise;
  }
  private fail(read: Read, error: unknown): void {
    if (this.active !== read) return;
    const message = error instanceof Error ? error.message : "Could not load connector catalog";
    this.retire({ state: "failed", message });
    this.state({ loading: false, stale: true, error: message });
  }
  private changed(account: Account, publish: boolean, apply: (rows: ConnectorCatalogEntry[]) => ConnectorCatalogEntry[]): void {
    if (!this.current(account)) return;
    this.retire({ state: "pending" });
    this.observation = {};
    if (publish) useSessionStore.setState((state) => ({ connectorCatalog: apply(state.connectorCatalog) }));
    // Synchronization has its own read state; a failure cannot undo this write
    // or cause the caller to replay it. No failed read retries itself.
    void this.refresh();
  }
  async create(body: CustomConnectorCreateRequest): Promise<void> {
    const account = this.account(), observation = this.observation;
    const kind = body.kind.trim().toLowerCase();
    const entry = await createCustomConnector(account.token, body, () => {
      if (!this.current(account)) return;
      for (const removal of this.removals) {
        if (removal.kind === kind) removal.replaced = true;
      }
    });
    this.changed(account, observation === this.observation, (rows) => rows.some((row) => row.kind === entry.kind)
      ? rows.map((row) => row.kind === entry.kind ? entry : row) : [...rows, entry]);
  }
  async configure(kind: string, clientId: string, clientSecret: string): Promise<void> {
    const account = this.account(), observation = this.observation;
    const info = await setOAuthClient(account.token, kind, clientId, clientSecret);
    this.changed(account, observation === this.observation, (rows) => rows.map((row) => row.kind === kind ? { ...row, available: info.configured } : row));
  }
  async remove(kind: string): Promise<void> {
    const account = this.account();
    const removal = { kind, replaced: false };
    this.removals.add(removal);
    try {
      await deleteCustomConnector(account.token, kind);
      // Independent targets still apply. Creation headers retire same-slug
      // removals pending at that confirmation, never DELETEs begun after it.
      this.changed(account, !removal.replaced, (rows) => rows.filter((row) => row.kind !== kind));
      connectorInstallations.definitionRemoved(kind, account, removal.replaced);
    } finally {
      this.removals.delete(removal);
    }
  }
}

export const connectorCatalog = new ConnectorCatalog();
