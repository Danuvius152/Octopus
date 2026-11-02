import { accountFetch } from "./accountFetch";
import { sessions } from "./sessionList";
import { useSessionStore, type ResearchJob } from "../stores/sessionStore";

/** Creation owns a receipt, not an event revision. The store keeps live facts. */
export async function createResearch(sessionId: string, question: string, active: () => boolean): Promise<void> {
  const start = useSessionStore.getState();
  const lifetime = sessions.observe(sessionId, start);
  const current = () => active() && sessions.current(lifetime.observation);
  try {
    const response = await accountFetch(`${window.location.origin}/api/sessions/${sessionId}/research`, {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${start.token}` },
      body: JSON.stringify({ question }), signal: AbortSignal.timeout(30_000),
    });
    if (!response.ok) {
      let message = `Couldn't start research (HTTP ${response.status}).`;
      try {
        const body = await response.json();
        if (typeof body?.detail === "string") message = body.detail;
      } catch { /* Keep the HTTP diagnosis. */ }
      if (current()) useSessionStore.getState().addMessage(sessionId, { role: "system", type: "notice", content: message, is_error: true });
      return;
    }
    const job: ResearchJob = await response.json();
    if (current() && job.session_id === sessionId && job.id && job.status && job.question) {
      useSessionStore.getState().initializeResearch(sessionId, job);
    }
  } catch {
    if (current()) useSessionStore.getState().addMessage(sessionId, {
      role: "system", type: "notice", content: "Research creation result is unconfirmed; refresh the current status.", is_error: true,
    });
  } finally {
    lifetime.finish();
  }
}
