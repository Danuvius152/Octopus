import { accountFetch as fetch } from "./accountFetch";
import { agentReadStatusIsCurrent, agentRequestIsCurrent, beginAgentSnapshot, type AgentRequest } from "./agentUpdates";
import { useSessionStore, type Agent } from "../stores/sessionStore";

export async function withAgentRequest<T>(operation: (request: AgentRequest) => Promise<T>, scope: AgentRequest["scope"] = "live"): Promise<T> {
  const request = useSessionStore.getState().beginAgentRequest(scope);
  try { return await operation(request); }
  finally { useSessionStore.getState().endAgentRequest(request); }
}

/** Initial/reconnect snapshots and failed-operation compensation share the same
 * per-ID merge. Compensation retains its original operation's ordering.
 */
export async function refreshAgents(request?: AgentRequest, scope: AgentRequest["scope"] = request?.scope ?? "live"): Promise<void> {
  const load = async (start: AgentRequest) => {
    const store = useSessionStore.getState();
    if (!store.token || !agentRequestIsCurrent(start, store)) return;
    beginAgentSnapshot(start, scope);
    if (scope === "all" && agentReadStatusIsCurrent(start)) useSessionStore.setState({ archivedAgentsRead: { ...store.archivedAgentsRead, loading: true, error: null } });
    try {
      const response = await fetch(`${window.location.origin}/api/agents${scope === "all" ? "?include_archived=true" : ""}`, { headers: { Authorization: `Bearer ${store.token}` } });
      if (!response.ok) throw new Error(`Could not load agents (HTTP ${response.status}).`);
      store.applyAgentSnapshot(await response.json() as Agent[], start, scope);
    } catch (error) {
      const current = useSessionStore.getState();
      if (scope === "all" && agentRequestIsCurrent(start, current) && agentReadStatusIsCurrent(start)) useSessionStore.setState({
        archivedAgentsRead: { ...current.archivedAgentsRead, loading: false, invalidated: true,
          error: error instanceof Error ? error.message : "Could not load agents." },
      });
      throw error;
    }
  };
  if (request) await load(request);
  else await withAgentRequest(load, scope);
}

/** Reconnect confirms archives only after the user has requested All. */
export function knownAgentScope(): AgentRequest["scope"] {
  const read = useSessionStore.getState().archivedAgentsRead;
  return read.loaded || read.loading || read.error ? "all" : "live";
}

export function refreshKnownAgents(): Promise<void> {
  return refreshAgents(undefined, knownAgentScope());
}
