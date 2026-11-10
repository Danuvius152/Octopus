import { cancelConnectorOAuth, pollConnectorOAuth, startConnectorOAuth } from "../api/connectors";
import type { ConnectorOAuthStatusResponse } from "../api";
import { connectorInstallations } from "./connectorInstallations";
import { useSessionStore } from "../stores/sessionStore";

export type ConnectorOAuthFlow =
  | { kind: "idle" }
  | { kind: "starting" }
  | { kind: "waiting" }
  | { kind: "cancelling" }
  | { kind: "error"; message: string };

/** One login owns its original credentials and late receipts, beyond its view. */
export class ConnectorOAuthOperation {
  private readonly account;
  private attached = true;
  private readonly stopping = new AbortController();
  private readonly unsubscribe: () => void;
  private readonly update: (flow: ConnectorOAuthFlow) => void;
  private readonly close: () => void;

  constructor(
    update: (flow: ConnectorOAuthFlow) => void,
    close: () => void,
  ) {
    const { token, accountGeneration, dispatchGeneration } = useSessionStore.getState();
    this.account = { token, accountGeneration, dispatchGeneration };
    this.update = update;
    this.close = close;
    this.unsubscribe = useSessionStore.subscribe((state) => {
      if (state.accountGeneration !== this.account.accountGeneration
        || state.token !== this.account.token
        || state.dispatchGeneration !== this.account.dispatchGeneration) {
        this.retire();
        this.close();
      }
    });
  }

  /** Irreversible: re-opening the view cannot reattach this operation. */
  retire() {
    this.attached = false;
    this.unsubscribe();
    this.cancel();
  }

  cancel() {
    if (this.attached) this.update({ kind: "cancelling" });
    this.stopping.abort();
  }

  private async terminal(result: ConnectorOAuthStatusResponse, cancelledMessage?: string) {
    // An error may also represent a lost local save acknowledgment.
    if (result.status === "success" || result.status === "error") {
      await connectorInstallations.connectionCompleted(this.account);
    }
    if (!this.attached) return;
    if (result.status === "success" || result.status === "cancelled" && this.stopping.signal.aborted) this.close();
    else this.update({ kind: "error", message: cancelledMessage || result.message || "Sign-in failed; inspect your connections before trying again." });
  }

  async start(kind: string, installationId?: string) {
    this.update({ kind: "starting" });
    let loginKnown = false;
    try {
      // Do not abort this await when the view retires: it may yield the only ID.
      const started = await startConnectorOAuth(this.account.token, kind, { installation_id: installationId });
      loginKnown = true;
      if (!this.stopping.signal.aborted) {
        window.open(started.authorize_url, "_blank", "noopener,noreferrer");
        this.update({ kind: "waiting" });
      }
      const deadline = Date.now() + 5 * 60 * 1000;
      while (!this.stopping.signal.aborted && Date.now() < deadline) {
        await new Promise<void>((resolve) => {
          const finish = () => { clearTimeout(timer); this.stopping.signal.removeEventListener("abort", finish); resolve(); };
          const timer = setTimeout(finish, 1500);
          this.stopping.signal.addEventListener("abort", finish, { once: true });
        });
        if (this.stopping.signal.aborted || Date.now() >= deadline) break;
        try {
          const result = await pollConnectorOAuth(this.account.token, started.login_id, this.stopping.signal);
          if (result.status === "success" || result.status === "error" || result.status === "cancelled") {
            await this.terminal(result);
            return;
          }
        } catch {
          // A failed read does not settle the login; keep the original deadline.
        }
      }
      const result = await cancelConnectorOAuth(this.account.token, started.login_id);
      await this.terminal(result, result.status === "cancelled" && !this.stopping.signal.aborted
        ? "Timed out waiting for sign-in. Local connection cancelled; remote authorization is unchanged." : undefined);
    } catch (error) {
      if (this.attached) this.update({ kind: "error", message: `Connection outcome is unknown: ${(error as Error).message}. Inspect connections before starting again.` });
      if (loginKnown) await connectorInstallations.connectionCompleted(this.account);
    } finally {
      this.unsubscribe();
    }
  }
}
