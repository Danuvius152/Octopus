import { createStore } from "zustand/vanilla";
import type { WorkspaceEditReceipt, WorkspaceInfo } from "../api";
import { useSessionStore } from "../stores/sessionStore";
import { accountFetch } from "./accountFetch";
import { newSubmissionId } from "./submission";

type Phase = "loading" | "load-failed" | "ready" | "writing" | "unknown" | "confirming";
interface Observer { account: number; token: string }
const API = `${window.location.origin}/api/auth`;
const unknown = "Directory permissions may have changed. Confirm their current state before editing again.";

function validWorkspace(value: unknown): value is WorkspaceInfo {
  if (!value || typeof value !== "object") return false;
  const info = value as WorkspaceInfo;
  return typeof info.workspace === "string" && Array.isArray(info.extra_roots) &&
    info.extra_roots.every(root => typeof root === "string") && typeof info.confined === "boolean";
}

/** One browser-runtime flow per account; closing a form cannot retire a PUT. */
class WorkspaceEdits {
  readonly store = createStore(() => ({ info: null as WorkspaceInfo | null, adding: "", phase: "loading" as Phase, error: "" }));
  private flowId: string | null = null;
  private generation: string | null = null;
  private sequence = 0;
  private pending: number | null = null;
  private observer: Observer | null = null;
  private readonly owner: string;
  constructor(owner: string) { this.owner = owner; }

  private current(observer: Observer): boolean {
    const state = useSessionStore.getState();
    return this.observer === observer && state.accountGeneration === observer.account && state.token === observer.token;
  }
  private admitted(account: number): Observer | null {
    const observer = this.observer;
    return observer && observer.account === account && this.current(observer) ? observer : null;
  }
  retire() {
    this.observer = null;
    this.store.setState({ phase: this.pending === null ? "loading" : "unknown", error: this.pending === null ? "" : unknown });
  }
  attach(account: number) {
    const observer = { account, token: useSessionStore.getState().token };
    this.observer = observer;
    if (this.pending !== null) this.store.setState({ phase: "unknown", error: unknown });
    else void this.open(observer);
    return () => { if (this.observer === observer) this.retire(); };
  }
  edit(adding: string, account: number) {
    if (this.admitted(account)) this.store.setState({ adding });
  }
  private async request(observer: Observer, path: string, body: object, method = "POST") {
    return accountFetch(`${API}/users/${encodeURIComponent(this.owner)}/${path}`, {
      method, headers: { Authorization: `Bearer ${observer.token}`, "Content-Type": "application/json" },
      body: JSON.stringify(body), signal: AbortSignal.timeout(30_000), cache: "no-store",
    });
  }
  private receipt(value: WorkspaceEditReceipt, confirming: boolean): boolean {
    return value?.flow_id === this.flowId && typeof value.service_generation === "string" && !!value.service_generation &&
      Number.isSafeInteger(value.sealed_through) && value.sealed_through >= (this.pending ?? 0) && validWorkspace(value.workspace) &&
      (!confirming || value.service_generation === this.generation || value.previous_generation_retired === true);
  }
  private async open(observer: Observer) {
    this.store.setState({ phase: "loading", error: "" });
    try {
      // ID failures belong to editing, never to rendering the account page.
      this.flowId ??= newSubmissionId();
      const response = await this.request(observer, "workspace-edits", { flow_id: this.flowId });
      const data = await response.json();
      if (!this.current(observer)) return;
      if (!response.ok || !this.receipt(data, false)) throw new Error("Could not load directory permissions. Retry loading.");
      this.generation = data.service_generation;
      this.sequence = Math.max(this.sequence, data.sealed_through);
      this.store.setState({ info: data.workspace, phase: "ready" });
    } catch (error) {
      if (this.current(observer)) this.store.setState({ phase: "load-failed", error: error instanceof Error ? error.message : "Could not load directory permissions." });
    }
  }
  retry(account: number) {
    const observer = this.admitted(account);
    if (observer && this.store.getState().phase === "load-failed") void this.open(observer);
  }
  add(account: number) { this.save(account, { add: this.store.getState().adding.trim() }); }
  remove(root: string, account: number) { this.save(account, { remove: root }); }
  private save(account: number, intent: { add: string } | { remove: string }) {
    const observer = this.admitted(account), state = this.store.getState();
    if (!observer || state.phase !== "ready" || !state.info || !this.generation || ("add" in intent && !intent.add)) return;
    const roots = "add" in intent ? [...state.info.extra_roots, intent.add] : state.info.extra_roots.filter(root => root !== intent.remove);
    const sequence = ++this.sequence, generation = this.generation;
    this.pending = sequence;
    // Synchronous admission covers click + Enter before React's next commit.
    this.store.setState({ phase: "writing", error: "" });
    void (async () => {
      try {
        const response = await this.request(observer, "extra-roots", { flow_id: this.flowId, service_generation: generation,
          sequence, extra_roots: roots }, "PUT");
        const data = await response.json().catch(() => null);
        if (!this.current(observer) || this.pending !== sequence || this.store.getState().phase !== "writing") return;
        if (!response.ok && [400, 401, 403, 404, 422].includes(response.status)) {
          this.pending = null;
          this.store.setState({ phase: "ready", error: typeof data?.detail === "string" ? data.detail : `Could not save — HTTP ${response.status}` });
          return;
        }
        if (!response.ok || !validWorkspace(data)) throw new Error(unknown);
        this.pending = null;
        this.store.setState({ info: data, phase: "ready", adding: "" });
      } catch {
        if (this.current(observer) && this.pending === sequence && this.store.getState().phase === "writing") {
          this.store.setState({ phase: "unknown", error: unknown });
        }
      }
    })();
  }
  confirm(account: number) {
    const observer = this.admitted(account), sequence = this.pending, generation = this.generation;
    if (!observer || sequence === null || !generation || this.store.getState().phase !== "unknown") return;
    this.store.setState({ phase: "confirming", error: "" });
    void (async () => {
      try {
        const response = await this.request(observer, "workspace-edits/confirm", { flow_id: this.flowId, service_generation: generation, sequence });
        const data = await response.json();
        if (!this.current(observer) || this.pending !== sequence || this.store.getState().phase !== "confirming") return;
        if (!response.ok || !this.receipt(data, true)) throw new Error(typeof data?.detail === "string" ? data.detail : "Could not confirm directory permissions. Retry confirmation.");
        this.pending = null;
        this.generation = data.service_generation;
        this.sequence = Math.max(this.sequence, data.sealed_through);
        this.store.setState({ info: data.workspace, phase: "ready" });
      } catch (error) {
        if (this.current(observer) && this.pending === sequence) this.store.setState({ phase: "unknown", error: error instanceof Error ? error.message : unknown });
      }
    })();
  }
}

const records = new Map<string, WorkspaceEdits>();
export function workspaceEditsFor(owner: string): WorkspaceEdits {
  let record = records.get(owner);
  if (!record) { record = new WorkspaceEdits(owner); records.set(owner, record); }
  return record;
}
useSessionStore.subscribe((state, before) => {
  if (state.accountGeneration !== before.accountGeneration) for (const record of records.values()) record.retire();
});
