import { selectSession } from "./selectSession";
import { sessions as sessionList } from "./sessionList";

/** Chat opens the latest available conversation or shares a pending creation. */
export async function openAgentChat(agentId: string, foreground: () => boolean): Promise<void> {
  if (!foreground()) return;
  const latest = sessionList.agentChats.latestAvailable(agentId);
  if (latest) {
    await selectSession(latest.id, agentId);
    return;
  }
  const observation = sessionList.capture();
  try {
    const result = await sessionList.agentChats.createOrConfirm(agentId, observation);
    if (result && sessionList.current(result.observation) && foreground() && sessionList.agentChats.acceptedChat(agentId, result.id)) {
      await selectSession(result.id, agentId, { fresh: result.fresh });
    }
  } catch (error) {
    if (sessionList.accountCurrent(observation) && foreground()) throw error;
  }
}
