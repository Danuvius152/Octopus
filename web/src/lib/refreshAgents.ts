import { accountFetch as fetch } from "./accountFetch";
import { agentRequestIsCurrent, type AgentRequest } from "./agentUpdates";
import { useSessionStore, type Agent } from "../stores/sessionStore";

export async function withAgentRequest<T>(operation: (request: AgentRequest) => Promise<T>, kind: AgentRequest["kind"] = "write"): Promise<T> {
  const request = useSessionStore.getState().beginAgentRequest(kind);
  try { return await operation(request); }
  finally { useSessionStore.getState().endAgentRequest(request); }
}

/** Initial/reconnect snapshots and failed-operation compensation share the same
 * per-ID merge. Compensation retains its original operation's ordering.
 */
export async function refreshAgents(request?: AgentRequest): Promise<void> {
  const load = async (start: AgentRequest) => {
    const store = useSessionStore.getState();
    if (!store.token || !agentRequestIsCurrent(start, store)) return;
    const response = await fetch(`${window.location.origin}/api/agents`, { headers: { Authorization: `Bearer ${store.token}` } });
    if (!response.ok) throw new Error(`Could not load agents (HTTP ${response.status}).`);
    store.applyAgentSnapshot(await response.json() as Agent[], start);
  };
  if (request) await load(request);
  else await withAgentRequest(load, "read");
}
