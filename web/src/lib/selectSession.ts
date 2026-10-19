import { addSessionInfo, loadSessionAndBgSnapshots, sessionInfoFromDetail } from "./hydrateSession";
import { useSessionStore } from "../stores/sessionStore";

/** Make `sessionId` the active chat session and load its snapshot.
 *
 * Selecting a session is more than a store write: the transcript, the pending
 * queue, the pending questions and the bg-task records all have to come down
 * with it, and the WS dedup baseline has to be bumped so a broadcast that
 * raced the fetch isn't rendered twice. The sidebar and the application view
 * both need exactly that, so it lives here rather than in either component.
 */
export async function selectSession(
  sessionId: string,
  agentId?: string | null
): Promise<void> {
  const store = useSessionStore.getState();
  if (agentId) store.setActiveAgentId(agentId);
  store.setActiveSessionId(sessionId);

  try {
    await loadSessionAndBgSnapshots(sessionId, store, (data) => {
      // A session created server-side (an application's build session, a
      // delegation child) may not be in the sidebar list yet. We already have
      // its detail in hand — adopt it rather than spending another request.
      addSessionInfo(sessionInfoFromDetail(data));
    });
  } catch {
    // A failed snapshot load leaves the session selected but empty; the WS
    // reconnect path refetches it.
  }
}
