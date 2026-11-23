import type { SessionArchiveResult } from "../api";
import { accountFetch } from "./accountFetch";
import { RequestDeadline } from "./requestDeadline";
import type { SessionInfo } from "../stores/sessionStore";

export { RequestDeadline as ArchiveDeadline };

export type ArchiveReply =
  | { state: "archived"; successor: SessionInfo | null; result: SessionArchiveResult | null }
  | { state: "refused"; status: number; reason: string }
  | { state: "unknown" };

/** Observe exactly one POST, including every response body, within one budget.
 * The server's shielded archive continues when this local observer expires. */
export async function postArchive(id: string, token: string, replace: boolean, deadline: RequestDeadline): Promise<ArchiveReply> {
  try {
    if (!deadline.active) return { state: "unknown" };
    return await deadline.wait((async (): Promise<ArchiveReply> => {
      const response = await accountFetch(`${window.location.origin}/api/sessions/${id}/archive${replace ? "" : "?replace=false"}`, {
        method: "POST", headers: { Authorization: `Bearer ${token}` }, signal: deadline.signal,
      });
      if (response.status === 204) return { state: "archived", successor: null, result: null };
      const body: unknown = await response.json();
      const result = readArchiveResult(body);
      if (result?.old_session_id === id) return { state: "archived", successor: null, result };
      if (!response.ok) {
        // A generic server failure cannot prove the effect did not commit.
        if (response.status < 400 || response.status >= 500 || response.status === 408) return { state: "unknown" };
        const detail = body && typeof body === "object" && "detail" in body ? body.detail : null;
        const reason = typeof detail === "string" ? detail : detail && typeof detail === "object"
          ? ("message" in detail ? detail.message : "reason" in detail ? detail.reason : "") : "";
        return { state: "refused", status: response.status, reason: typeof reason === "string" ? reason : "" };
      }
      if (!body || typeof body !== "object" || !("id" in body) || typeof body.id !== "string" || !body.id || body.id === id) return { state: "unknown" };
      return { state: "archived", successor: body as SessionInfo, result: null };
    })()) ?? { state: "unknown" };
  } catch {
    return { state: "unknown" };
  } finally {
    deadline.finish();
  }
}

/** HTTP and WS carry the same known archive and successor facts. */
export function readArchiveResult(value: unknown): SessionArchiveResult | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const result = value as Record<string, unknown>;
  if (typeof result.old_session_id !== "string" || !result.old_session_id || result.archived !== true ||
    result.new_session_id !== null && (typeof result.new_session_id !== "string" || !result.new_session_id) ||
    result.new_session_id === result.old_session_id || typeof result.message !== "string") return null;
  if (result.failure_stage === "successor_creation" ? result.new_session_id !== null
    : result.failure_stage !== "schedule_rebinding" || result.new_session_id === null) return null;
  return value as SessionArchiveResult;
}
