import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { accountFetch } from "./accountFetch";
import { RequestDeadline } from "./requestDeadline";
import type { LiveRowEvidence, SessionList, SessionObservation } from "./sessionList";
import { isSessionInfo } from "./wsEvents";

type Chat = { id: string; fresh: boolean; observation: SessionObservation };
type Work = Promise<Chat | null>;
type Confirmation = { work: Work; publish: (chat: Chat | null) => void };
type Posting = { state: "posting"; work: Work; closedIds: Set<string>; postObservation: SessionObservation };
type Confirming = (
  | { state: "unknown" }
  | { state: "confirmed"; id: string }
) & { postObservation: SessionObservation; confirmation?: Confirmation };
type Creation = Posting | Confirming;
type Receipt = { state: "created"; session: SessionInfo } | { state: "refused"; status: number };

function canOpen(row: SessionInfo, agentId: string): boolean {
  return row.agent_id === agentId && !row.archived && row.origin !== "app" && row.origin !== "delegation";
}

/** Owns only Chat submissions in the current account. Rows stay in SessionList. */
export class AgentChatCreations {
  private pending = new Map<string, Creation>();

  private readonly list: SessionList;

  constructor(list: SessionList) { this.list = list; }

  latestAvailable(agentId: string): SessionInfo | undefined {
    const creation = this.pending.get(agentId);
    const confirmingId = creation?.state === "confirmed" ? creation.id : undefined;
    return useSessionStore.getState().sessions.filter((row) =>
      row.id !== confirmingId && canOpen(row, agentId) && (!creation || this.list.hasCurrentLiveObservation(row)))
      .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  }

  acceptedChat(agentId: string, id: string, required: LiveRowEvidence = "listed"): SessionInfo | undefined {
    return useSessionStore.getState().sessions.find((row) =>
      row.id === id && canOpen(row, agentId) && this.list.hasCurrentLiveObservation(row, required));
  }

  createOrConfirm(agentId: string, observation: SessionObservation): Work {
    const existing = this.pending.get(agentId);
    if (existing?.state === "posting") return existing.work;
    if (existing) return this.confirm(agentId, existing);
    if (!observation.scope.token) return Promise.reject(new Error("Sign in to open a chat."));

    const posting: Posting = {
      state: "posting",
      work: Promise.resolve().then(() => this.post(agentId, posting)),
      closedIds: new Set(),
      postObservation: observation,
    };
    this.pending.set(agentId, posting);
    return posting.work;
  }

  retire(): void {
    for (const creation of this.pending.values()) {
      if (creation.state === "posting") creation.closedIds.clear();
      else creation.confirmation?.publish(null);
    }
    this.pending.clear();
  }

  observeRows(rows: SessionInfo[]): void {
    if (!this.pending.size) return;
    const unknownAgents = new Set<string>();
    for (const row of rows) {
      if (row.archived) {
        this.observeClosed(row.id, "row");
        continue;
      }
      for (const creation of this.pending.values()) {
        if (creation.state === "posting") creation.closedIds.delete(row.id);
      }
      const agentId = row.agent_id;
      if (!agentId) continue;
      const creation = this.pending.get(agentId);
      if (!creation || !canOpen(row, agentId)) continue;
      if (creation.state === "confirmed" && creation.id === row.id) {
        const chat = this.finishFromCurrentEvidence(agentId, row.id);
        if (chat) creation.confirmation?.publish(chat);
      }
      if (creation.state === "unknown" && creation.confirmation) unknownAgents.add(agentId);
    }
    for (const agentId of unknownAgents) {
      const creation = this.pending.get(agentId);
      if (creation?.state !== "unknown") continue;
      const row = this.latestAvailable(agentId);
      if (row) {
        creation.confirmation?.publish({ id: row.id, fresh: false, observation: this.list.capture() });
      }
    }
  }

  observeClosed(id: string, source: "event" | "row" = "event"): void {
    for (const [agentId, creation] of this.pending) {
      // Keep sparse events for the lifetime of POSTs that may return this ID.
      if (creation.state === "posting" && source === "event") creation.closedIds.add(id);
      if (creation.state === "confirmed" && creation.id === id) {
        this.pending.delete(agentId);
        creation.confirmation?.publish(null);
      }
    }
  }

  rememberArchivedRow(row: SessionInfo): void {
    // Keep accepted archive evidence if absence later removes it from the list.
    if (!row.agent_id) return;
    const posting = this.pending.get(row.agent_id);
    if (posting?.state === "posting") posting.closedIds.add(row.id);
  }

  private async post(agentId: string, posting: Posting): Work {
    const observation = posting.postObservation;
    if (!this.list.accountCurrent(observation)) return null;
    const reply = await postChat(agentId, observation.scope.token);
    const closed = reply?.state === "created" && posting.closedIds.has(reply.session.id);
    posting.closedIds.clear();
    if (!this.list.accountCurrent(observation)) return null;

    this.pending.delete(agentId);
    if (reply?.state === "refused") throw new Error(`HTTP ${reply.status}`);
    const receipt = reply?.session;
    const id = receipt?.id;
    if (id && (closed || useSessionStore.getState().archivedSessions.some((row) => row.id === id))) return null;
    const creation: Confirming = id
      ? { state: "confirmed", id, postObservation: observation }
      : { state: "unknown", postObservation: observation };
    this.pending.set(agentId, creation);
    return this.confirm(agentId, creation, receipt);
  }

  private confirm(agentId: string, creation: Confirming, receipt?: SessionInfo): Work {
    if (creation.confirmation) return creation.confirmation.work;
    let publish!: Confirmation["publish"];
    const fact = new Promise<Chat | null>((resolve) => { publish = resolve; });
    const active = () => this.list.accountCurrent(creation.postObservation) && this.pending.get(agentId) === creation &&
      creation.confirmation === confirmation;
    const read = Promise.resolve().then(() => this.confirmFromList(agentId, creation, active, receipt));
    const confirmation: Confirmation = {
      publish,
      work: Promise.race([read, fact]).finally(() => { creation.confirmation = undefined; }),
    };
    creation.confirmation = confirmation;
    return confirmation.work;
  }

  private async confirmFromList(agentId: string, creation: Confirming,
    active: () => boolean, receipt?: SessionInfo,
  ): Work {
    if (!active()) return null;
    const original = creation.postObservation;
    const id = creation.state === "confirmed" ? creation.id : undefined;
    const confirmed = this.finishFromCurrentEvidence(agentId, id);
    if (confirmed) return confirmed;
    if (receipt) {
      const accepted = await this.list.created(receipt, original, { includeArchived: true, active });
      if (!active()) return null;
      const current = this.finishFromCurrentEvidence(agentId, id);
      if (current || !accepted && this.list.current(original)) return current;
    }
    await this.list.settledRead(id !== undefined, active);
    if (!active()) return null;
    if (id !== undefined) return this.finishFromCurrentEvidence(agentId, id);
    const row = this.latestAvailable(agentId);
    if (row) return { id: row.id, fresh: false, observation: this.list.capture() };
    throw new Error("Chat creation could not be confirmed. Click Chat again to check the session list.");
  }

  private finishFromCurrentEvidence(agentId: string, id: string | undefined): Chat | null {
    if (id === undefined || !this.acceptedChat(agentId, id, "confirmed")) return null;
    this.pending.delete(agentId);
    return { id, fresh: true, observation: this.list.capture() };
  }
}

async function postChat(agentId: string, token: string): Promise<Receipt | null> {
  const deadline = new RequestDeadline();
  try {
    return await deadline.wait((async (): Promise<Receipt | null> => {
      const res = await accountFetch(`${window.location.origin}/api/agents/${encodeURIComponent(agentId)}/sessions`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: "{}",
        signal: deadline.signal,
      });
      if (res.status >= 400 && res.status < 500 && res.status !== 408) return { state: "refused", status: res.status };
      if (res.status !== 201) return null;
      const session: unknown = await res.json();
      return isSessionInfo(session) && canOpen(session, agentId) ? { state: "created", session } : null;
    })());
  } catch {
    return null;
  } finally {
    deadline.finish();
  }
}
