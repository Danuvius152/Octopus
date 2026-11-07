import type { ContextState } from "./sessionContext";

export const contextState = (revision = 1): ContextState => ({
  version: 1, revision, binding_revision: 0,
  binding_target: { native_session_id: "native", effective_config_key: "config", agent_settings_revision: 0 },
  validity: "valid", invalidation: null, activity: null,
  sample: { backend: "codex", native_session_id: "native", actual_model: "model",
    binding: { native_session_id: "native", effective_config_key: "config", agent_settings_revision: 0, binding_revision: 0 },
    source: { request_id: "request", attempt_id: "attempt", connection_generation: "connection",
      native_turn_id: "turn", native_sample_id: null, observation_seq: revision },
    observed_at: "2026-11-07T07:00:00Z", used_tokens: 250, capacity_tokens: 1000, used_ratio: null,
    basis: "codex_last_total", capacity_basis: "runtime_model_window" },
});
