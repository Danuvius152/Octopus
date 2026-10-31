import { loadSessionSnapshots } from "./hydrateSession";
import { useSessionStore } from "../stores/sessionStore";

/** Make `sessionId` the active chat session and load its snapshot.
 *
 * Selecting a session is more than a store write: the transcript, the pending
 * dispatch snapshot, pending questions and bg-task records all have to come down
 * with it, and the WS dedup baseline has to be bumped so a broadcast that
 * raced the fetch isn't rendered twice. The sidebar and the application view
 * both need exactly that, so it lives here rather than in either component.
 */
export async function selectSession(
  sessionId: string,
  agentId?: string | null
): Promise<void> {
  const store = useSessionStore.getState();
  const owner = agentId ?? [...store.sessions, ...store.archivedSessions].find((s) => s.id === sessionId)?.agent_id;
  if (owner) store.setActiveAgentId(owner);
  store.setActiveSessionId(sessionId);

  try {
    await loadSessionSnapshots(sessionId);
  } catch {
    // The queue exposes the failed read; reconnect can confirm it again.
  }
}
