import { accountFetch } from "./accountFetch";
import { sessions } from "./sessionList";
import { observeSessionAuxiliary } from "./hydrateSession";
import { useSessionStore, type ResearchJob } from "../stores/sessionStore";

/** Creation owns a receipt, not an event revision. The store keeps live facts. */
export async function createResearch(sessionId: string, question: string, active: () => boolean, signal?: AbortSignal): Promise<void> {
  const start = useSessionStore.getState();
  const lifetime = sessions.observe(sessionId, start);
  let confirmation: ReturnType<typeof observeSessionAuxiliary> | undefined;
  const viewCurrent = () => active() && !signal?.aborted && useSessionStore.getState().activeSessionId === sessionId;
  const current = () => viewCurrent() && sessions.current(lifetime.observation);
  const initialGenerationCurrent = () => viewCurrent() && sessions.initialGenerationCurrent(lifetime.observation);
  const cancel = () => { confirmation?.cancel(); lifetime.finish(); };
  signal?.addEventListener("abort", cancel, { once: true });
  try {
    if (!viewCurrent()) return;
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
      if (current() || initialGenerationCurrent()) useSessionStore.getState().addMessage(sessionId, { role: "system", type: "notice", content: message, is_error: true });
      return;
    }
    const job: ResearchJob = await response.json();
    if (job.session_id !== sessionId || !job.id || !job.status || !job.question) throw new Error("Invalid research creation receipt");
    if (current()) useSessionStore.getState().initializeResearch(sessionId, job);
    else if (initialGenerationCurrent()) {
      confirmation = observeSessionAuxiliary(sessionId, "research");
      if (!viewCurrent()) confirmation.cancel();
      // The read owns its publication and returns only accepted snapshot data.
      // Cached identity alone cannot confirm a different service generation.
      const observed = await confirmation.done.catch(() => null);
      if (!initialGenerationCurrent() || observed?.some((item) => item && typeof item === "object" &&
        "id" in item && item.id === job.id && "session_id" in item && item.session_id === sessionId)) return;
      const message = `Research ${job.id} was created; its current status is unconfirmed. Refresh research updates.`;
      const store = useSessionStore.getState();
      store.addMessage(sessionId, { role: "system", type: "notice", content: message, is_error: true });
      store.setSessionAuxiliaryError(sessionId, "research", message);
    }
  } catch {
    if (current() || initialGenerationCurrent()) useSessionStore.getState().addMessage(sessionId, {
      role: "system", type: "notice", content: "Research creation result is unconfirmed; refresh the current status.", is_error: true,
    });
  } finally {
    signal?.removeEventListener("abort", cancel);
    lifetime.finish();
  }
}
