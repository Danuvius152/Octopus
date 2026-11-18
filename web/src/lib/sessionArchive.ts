import type { SessionArchiveResult } from "../api";
import { accountFetch } from "./accountFetch";
import type { SessionInfo } from "../stores/sessionStore";

/** One phase of the original archive observation. Waiting may end without
 * cancelling the SessionList transport shared with other consumers. */
export class ArchiveDeadline {
  private controller = new AbortController();
  private until = Date.now() + 30_000;
  private timer = setTimeout(() => this.finish(), 30_000);
  get signal(): AbortSignal { return this.controller.signal; }
  get active(): boolean { return !this.signal.aborted && Date.now() < this.until; }
  finish(): void { clearTimeout(this.timer); this.controller.abort(); }
  wait<T>(work: Promise<T>): Promise<T | null> {
    return new Promise((resolve, reject) => {
      const abort = () => resolve(null);
      if (!this.active) { work.catch(() => {}); resolve(null); return; }
      this.signal.addEventListener("abort", abort, { once: true });
      work.then((result) => {
        this.signal.removeEventListener("abort", abort);
        resolve(this.active ? result : null);
      }, (error: unknown) => {
        this.signal.removeEventListener("abort", abort);
        reject(error);
      });
    });
  }
}

export type ArchiveReply =
  | { state: "archived"; successor: SessionInfo | null; result: SessionArchiveResult | null }
  | { state: "refused"; status: number; reason: string }
  | { state: "unknown" };

/** Observe exactly one POST, including every response body, within one budget.
 * The server's shielded archive continues when this local observer expires. */
export async function postArchive(id: string, token: string, replace: boolean, deadline: ArchiveDeadline): Promise<ArchiveReply> {
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

/** HTTP and WS carry the same known post-effect facts. Neither implies that
 * a missing successor was never created, or that inherited settings succeeded.
 */
export function readArchiveResult(value: unknown): SessionArchiveResult | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const result = value as Record<string, unknown>;
  if (typeof result.old_session_id !== "string" || !result.old_session_id || result.archived !== true ||
    result.new_session_id !== null && (typeof result.new_session_id !== "string" || !result.new_session_id) ||
    result.new_session_id === result.old_session_id || typeof result.message !== "string") return null;
  if (typeof result.failure_stage !== "string" ||
    !["successor_creation", "successor_configuration", "schedule_rebinding"].includes(result.failure_stage)) return null;
  if (result.failure_stage !== "successor_creation" && result.new_session_id === null) return null;
  return value as SessionArchiveResult;
}
