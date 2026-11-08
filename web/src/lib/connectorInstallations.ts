import { deleteCustomConnector, deleteInstallation, fetchInstallations } from "../api/connectors";
import type { ConnectorInstallationInfo } from "../api";
import { useSessionStore } from "../stores/sessionStore";
import { unreadList, type ListReadState, type RefreshResult } from "./listState";

interface Account { token: string; accountGeneration: number }
interface Read {
  controller: AbortController; timer: ReturnType<typeof setTimeout>;
  promise: Promise<RefreshResult>; finish: (result: RefreshResult) => void;
}

/** Owns installation observations; Zustand is the only installation array.
 * A confirmed write retires every overlapping read, including reads started
 * while the write was pending. Independent DELETE receipts remain applicable.
 */
class ConnectorInstallations {
  private active: Read | null = null;

  constructor() {
    useSessionStore.subscribe((state, before) => {
      const accountChanged = state.token !== before.token || state.accountGeneration !== before.accountGeneration;
      if (accountChanged) {
        this.retire({ state: "obsolete" });
        this.state(unreadList());
      } else if (state.dispatchGeneration !== before.dispatchGeneration && this.active) {
        this.retire({ state: "pending" });
        void this.refresh();
      }
    });
  }

  private sameAccount(account: Account): boolean {
    const now = useSessionStore.getState();
    return !!account.token && account.token === now.token && account.accountGeneration === now.accountGeneration;
  }
  private state(patch: Partial<ListReadState>): void {
    useSessionStore.setState((state) => ({ connectorRead: { ...state.connectorRead, ...patch } }));
  }
  private retire(result: RefreshResult): void {
    const read = this.active;
    if (!read) return;
    this.active = null;
    clearTimeout(read.timer); read.controller.abort(); read.finish(result);
  }
  refresh(): Promise<RefreshResult> {
    const { token } = useSessionStore.getState();
    if (!token) return Promise.resolve({ state: "obsolete" });
    if (this.active) return this.active.promise;
    let finish!: Read["finish"];
    const promise = new Promise<RefreshResult>((resolve) => { finish = resolve; });
    const read: Read = {
      controller: new AbortController(), promise, finish,
      timer: setTimeout(() => this.fail(read, new Error("Connection list timed out after 30 seconds")), 30_000),
    };
    this.active = read;
    this.state({ loading: true, stale: true, error: null });
    void fetchInstallations(token, read.controller.signal).then((rows) => {
      if (this.active !== read) return;
      if (!Array.isArray(rows)) { this.fail(read, new Error("Invalid connection list response")); return; }
      useSessionStore.setState({ connectorInstallations: rows });
      this.retire({ state: "applied" });
      this.state({ loaded: true, loading: false, stale: false, error: null });
    }, (error) => this.fail(read, error));
    return promise;
  }
  private fail(read: Read, error: unknown): void {
    if (this.active !== read) return;
    const message = error instanceof Error ? error.message : "Could not load connections";
    this.retire({ state: "failed", message });
    this.state({ loading: false, stale: true, error: message });
  }

  connectionCompleted(account: Account): Promise<RefreshResult> {
    if (!this.sameAccount(account)) return Promise.resolve({ state: "obsolete" });
    // A pre-login observation cannot confirm the newly installed connection.
    this.retire({ state: "pending" });
    return this.refresh();
  }

  async disconnect(id: string): Promise<void> {
    const account = useSessionStore.getState();
    await deleteInstallation(account.token, id);
    this.removed(account, (row) => row.id === id);
  }
  async removeCustomDefinition(kind: string): Promise<void> {
    const account = useSessionStore.getState();
    await deleteCustomConnector(account.token, kind);
    this.removed(account, (row) => row.kind === kind);
  }
  private removed(account: Account, matches: (row: ConnectorInstallationInfo) => boolean): void {
    if (!this.sameAccount(account)) return;
    this.retire({ state: "pending" });
    useSessionStore.setState((state) => ({ connectorInstallations: state.connectorInstallations.filter((row) => !matches(row)) }));
    // Confirm the rest of the list too: the retired GET may have contained a
    // newly connected account which must not be lost with that observation.
    void this.refresh();
  }
}

export const connectorInstallations = new ConnectorInstallations();
