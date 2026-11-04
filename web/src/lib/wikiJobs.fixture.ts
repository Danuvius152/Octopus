import type { WikiJobCard } from "../api";

export function wikiJob(overrides: Partial<WikiJobCard> = {}): WikiJobCard {
  return {
    job_id: "job-1", vault_id: "vault-1", subscriber_id: "subscriber-1", origin_request_id: "original-request",
    state_version: 1, attempt: 1, status: "running", settlement_phase: "none", worker_session_id: "worker-1",
    scope: "Preserve the argument and review its sources", scope_truncated: false,
    progress: { total: 2, completed: 0, attention: 0, failed: 0 },
    outcome_refs: [], outcome_count: 0, outcome_refs_truncated: false,
    effects: { recorded: 0, applied: 0, index_pending: 0 }, recording_pending: 0,
    error: null, error_truncated: false, created_at: "2026-10-18T12:00:00Z", updated_at: "2026-10-18T12:00:00Z",
    received_at: "2026-10-18T12:00:00Z", current_state: "available", ...overrides,
  };
}
