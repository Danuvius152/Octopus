import { createStore } from "zustand/vanilla";
import type { components } from "../api/contracts";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { accountFetch } from "./accountFetch";
import type { SessionList } from "./sessionList";
import { newSubmissionId } from "./submission";

type Identity = components["schemas"]["SessionRestoreRequest"];
type Status = components["schemas"]["SessionRestoreStatus"];
type Phase = "checking" | "writing" | "confirming" | "unknown" | "ready" | "failed";
interface Observer { account: number; token: string; owner: string }
interface Record { owner: string; sessionId: string; identity: Identity | null; active: Observer | null }
interface Result { session: SessionInfo | null; message?: string }
const prefix = "octopus.session-restore.v1:";
const uncertain = "Restoration is unconfirmed. Confirm its state before restoring again.";
const key = (owner: string, id: string) => prefix + JSON.stringify([owner, id]);
const owner = () => useSessionStore.getState().identity?.user_id;
function identity(value: unknown): value is Identity {
  if (!value || typeof value !== "object") return false;
  const data = value as Identity;
  return [data.operation_id, data.service_generation].every(field => typeof field === "string" && /^[a-zA-Z0-9_-]{1,128}$/.test(field));
}
function status(value: unknown): value is Status {
  if (!value || typeof value !== "object") return false;
  const data = value as Status;
  return typeof data.service_generation === "string" && !!data.service_generation &&
    (data.operation_id == null || typeof data.operation_id === "string") &&
    ["idle", "not_registered", "running", "finished", "sealed", "generation_retired", "generation_unknown"].includes(data.state) &&
    (data.status_code == null || Number.isInteger(data.status_code)) && (data.message == null || typeof data.message === "string");
}

/** SessionList owns occupancy. Storage holds only unresolved identities so a
 * logout/reload cannot forget a POST that may still arrive under an old token. */
export class SessionRestorations {
  readonly store = createStore(() => ({ entries: {} as { [id: string]: { phase: Phase } }, error: null as string | null }));
  private records = new Map<string, Record>();
  private readonly list: SessionList;
  constructor(list: SessionList) { this.list = list; }

  retire() {
    for (const record of this.records.values()) record.active = null;
    this.store.setState({ entries: {}, error: null });
  }
  pendingIds(): string[] {
    return Object.keys(this.store.getState().entries).filter(id => !["ready", "failed"].includes(this.store.getState().entries[id].phase));
  }
  private phase(id: string, phase: Phase) {
    this.store.setState(state => ({ entries: { ...state.entries, [id]: { phase } } }));
  }
  private record(user: string, id: string): Record {
    const savedKey = key(user, id);
    let record = this.records.get(savedKey);
    if (!record) {
      const raw = sessionStorage.getItem(savedKey);
      const saved: unknown = raw ? JSON.parse(raw) : null;
      if (saved !== null && !identity(saved)) throw new Error("Saved restoration identity could not be read. Restore remains paused.");
      record = { owner: user, sessionId: id, identity: saved, active: null };
      this.records.set(savedKey, record);
    }
    return record;
  }
  observePending() {
    const user = owner();
    if (!user) return;
    try {
      for (const savedKey of Object.keys(sessionStorage)) {
        if (!savedKey.startsWith(prefix)) continue;
        const [savedOwner, id] = JSON.parse(savedKey.slice(prefix.length));
        if (savedOwner !== user || typeof id !== "string") continue;
        const record = this.record(user, id);
        if (record.identity && !record.active) this.phase(id, "unknown");
      }
      this.store.setState({ error: null });
    } catch { this.store.setState({ error: "Restoration identities could not be read in this tab. Retry loading before restoring." }); }
  }
  deleted(id: string) {
    const user = owner();
    if (!user) return;
    const savedKey = key(user, id);
    const record = this.records.get(savedKey);
    if (record) record.active = null;
    this.records.delete(savedKey);
    // Failure to remove a proven closed identity is safe: a later opening
    // confirms it again. It must never authorize another POST by itself.
    try { sessionStorage.removeItem(savedKey); } catch { /* Keep the closed identity. */ }
    this.store.setState(state => ({ entries: Object.fromEntries(Object.entries(state.entries).filter(([target]) => target !== id)) }));
  }
  private begin(record: Record, phase: Phase): Observer {
    const state = useSessionStore.getState();
    const observer = { account: state.accountGeneration, token: state.token, owner: record.owner };
    record.active = observer;
    this.phase(record.sessionId, phase);
    return observer;
  }
  private current(record: Record, observer: Observer) {
    const state = useSessionStore.getState();
    return record.active === observer && state.accountGeneration === observer.account && state.token === observer.token && owner() === observer.owner;
  }
  private async read(id: string, observer: Observer, suffix: string, body?: Identity): Promise<Status> {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    try {
      return await Promise.race([(async () => {
        const response = await accountFetch(`${window.location.origin}/api/sessions/${encodeURIComponent(id)}/${suffix}`, {
          headers: { Authorization: `Bearer ${observer.token}`, "Content-Type": "application/json" },
          ...(body ? { method: "POST", body: JSON.stringify(body) } : {}), signal: controller.signal, cache: "no-store",
        });
        const data: unknown = await response.json().catch(() => null);
        if (!response.ok && !(response.status === 409 && status(data) && data.state === "sealed")) throw new Error(`Could not confirm restoration (HTTP ${response.status}).`);
        if (!status(data)) throw new Error("Invalid restoration status. Confirm its current state.");
        return data;
      })(), new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => { controller.abort(); reject(new Error("Restoration response timed out. Confirm its current state.")); }, 30_000);
      })]);
    } finally { clearTimeout(timer!); controller.abort(); }
  }
  private save(record: Record, value: Identity) {
    // Persist before sending: failure leaves the write unissued and retryable.
    sessionStorage.setItem(key(record.owner, record.sessionId), JSON.stringify(value));
    record.identity = value;
  }
  private matches(record: Record, proof: Status) {
    const original = record.identity;
    return original !== null && proof.operation_id === original.operation_id &&
      (proof.service_generation === original.service_generation || proof.state === "generation_retired");
  }
  private async settle(record: Record, observer: Observer, proof: Status): Promise<Result> {
    if (!this.matches(record, proof)) throw new Error("Restoration identity changed. Confirm the original operation.");
    if (!["finished", "sealed", "generation_retired"].includes(proof.state)) {
      this.phase(record.sessionId, "unknown");
      record.active = null;
      return { session: null, message: proof.state === "running" ? "Restoration is still running. Continue confirmation."
        : proof.state === "generation_unknown" ? "The previous service's retirement is unconfirmed." : uncertain };
    }
    // A list that started before the closing proof cannot settle this POST.
    let reading = this.list.refreshAfterRestoration();
    while (this.current(record, observer)) {
      const result = await reading;
      if (!this.current(record, observer)) return { session: null };
      if (result.state === "pending" || result.state === "applied" && useSessionStore.getState().sessionRead.stale) {
        reading = this.list.refresh(true); continue;
      }
      if (result.state !== "applied") throw new Error("Could not synchronize restored sessions. Retry confirmation.");
      const state = useSessionStore.getState();
      const session = state.sessions.find(row => row.id === record.sessionId) ?? null;
      // Inclusive absence is processed through SessionList's deletion owner.
      if (!session && !state.archivedSessions.some(row => row.id === record.sessionId)) return { session: null };
      sessionStorage.removeItem(key(record.owner, record.sessionId));
      record.identity = null; record.active = null;
      this.records.delete(key(record.owner, record.sessionId));
      this.phase(record.sessionId, "ready");
      return { session, message: !session ? proof.status_code ? `Restoration ended (HTTP ${proof.status_code}). ${proof.message ?? ""}`
        : "The session is still archived. You can restore it again." : undefined };
    }
    return { session: null };
  }
  restore(id: string): Promise<Result> | null { return this.act(id, false); }
  confirm(id: string): Promise<Result> | null { return this.act(id, true); }
  private act(id: string, confirming: boolean): Promise<Result> | null {
    const user = owner();
    if (!user) return Promise.resolve({ session: null, message: "Account identity is not confirmed. Try again after it loads." });
    if (this.store.getState().error) return Promise.resolve({ session: null, message: this.store.getState().error! });
    let record: Record;
    try { record = this.record(user, id); }
    catch { return Promise.resolve({ session: null, message: "Restoration identity could not be recovered in this tab. Restore remains paused." }); }
    const phase = this.store.getState().entries[id]?.phase;
    // A refused duplicate has no navigation intent. Admission and busy state
    // are synchronous even when React has not rendered the disabled button.
    if (record.active && (["checking", "confirming"].includes(phase ?? "") || phase === "writing" && !confirming)) return null;
    return this.run(record, confirming);
  }
  private async run(record: Record, confirming: boolean): Promise<Result> {
    const id = record.sessionId;
    const observer = this.begin(record, record.identity ? "confirming" : "checking");
    try {
      if (!record.identity) {
        const initial = await this.read(id, observer, "restoration");
        if (!this.current(record, observer)) return { session: null };
        if (initial.state === "running" && initial.operation_id) {
          this.save(record, { operation_id: initial.operation_id, service_generation: initial.service_generation });
          this.phase(id, "unknown"); record.active = null;
          return { session: null, message: "Restoration is already running. Confirm its state." };
        }
        if (initial.state !== "idle") throw new Error("Could not establish restoration admission. Try again.");
        if (confirming) {
          this.phase(id, "ready"); record.active = null;
          return { session: null, message: "No pending restoration. Choose Unarchive to start a new one." };
        }
        this.save(record, { operation_id: newSubmissionId(), service_generation: initial.service_generation });
        this.phase(id, "writing");
        const proof = await this.read(id, observer, "unarchive", record.identity!);
        if (!this.current(record, observer)) return { session: null };
        return await this.settle(record, observer, proof);
      }
      const query = new URLSearchParams({ ...record.identity });
      let proof = await this.read(id, observer, `restoration?${query}`);
      if (!this.current(record, observer)) return { session: null };
      if (!this.matches(record, proof) && proof.state !== "generation_unknown") throw new Error("Restoration identity changed. Confirm the original operation.");
      if (proof.state === "not_registered") {
        proof = await this.read(id, observer, "restoration/seal", record.identity);
        if (!this.current(record, observer)) return { session: null };
      }
      if (proof.state === "generation_unknown") {
        this.phase(id, "unknown"); record.active = null;
        return { session: null, message: "The previous service's retirement is unconfirmed." };
      }
      return await this.settle(record, observer, proof);
    } catch (error) {
      if (!this.current(record, observer)) return { session: null };
      this.phase(id, record.identity ? "unknown" : "failed"); record.active = null;
      return { session: null, message: error instanceof Error ? error.message : uncertain };
    }
  }
}
