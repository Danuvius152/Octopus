import { accountFetch as fetch } from "./accountFetch";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { selectSession } from "./selectSession";
import { sessions as sessionList } from "./sessionList";

const API = window.location.origin;

/** "Chat with this agent" from the Agents page: its most recent
 * conversation, or a new one when it has none.
 *
 * "Conversation" means the user's own sessions — not the ones an application
 * holds with the agent, and not delegation children, which are another
 * agent's work. Opening the session is also what gives an unpinned agent its
 * temporary place in the sidebar (sidebar-pins.md §5).
 */
export async function openAgentChat(agentId: string, foreground: () => boolean): Promise<void> {
  const { sessions, token } = useSessionStore.getState();
  const latest = sessions
    .filter(
      (s) =>
        s.agent_id === agentId && s.origin !== "app" && s.origin !== "delegation"
    )
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  if (latest) {
    if (foreground()) await selectSession(latest.id, agentId);
    return;
  }
  const observation = sessionList.capture();
  try {
    const res = await fetch(`${API}/api/agents/${agentId}/sessions`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({}),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const session = (await res.json()) as SessionInfo;
    const accepted = await sessionList.created(session, observation);
    if (!accepted || !sessionList.current(observation) || !foreground()) return;
    await selectSession(session.id, agentId);
  } catch (error) {
    if (sessionList.current(observation) && foreground()) throw error;
  }
}
