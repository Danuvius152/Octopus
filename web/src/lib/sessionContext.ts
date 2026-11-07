import type { components } from "../api/contracts";

export type ContextState = components["schemas"]["ContextState"];
type Obj = Record<string, unknown>;
const record = (value: unknown): value is Obj => value !== null && typeof value === "object" && !Array.isArray(value);
const id = (value: unknown) => typeof value === "string" && value.length > 0;
const count = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const maybeId = (value: unknown) => value === null || id(value);
const maybeCount = (value: unknown) => value === null || count(value);
const target = (value: unknown): value is Obj => record(value) && maybeId(value.native_session_id)
  && id(value.effective_config_key) && maybeCount(value.agent_settings_revision);

/** Shared HTTP/WS boundary. Undefined means malformed; null means no saved observation. */
export function readContextState(value: unknown): ContextState | null | undefined {
  if (value === null) return null;
  if (!record(value) || value.version !== 1 || !count(value.revision) || value.revision === 0
      || !count(value.binding_revision) || value.binding_revision > value.revision || !target(value.binding_target)
      || !["unknown", "valid", "stale"].includes(String(value.validity))
      || ![undefined, null, "compacting"].includes(value.activity as string | null | undefined)) return undefined;
  const invalidation = value.invalidation;
  if (value.validity === "stale") {
    if (!record(invalidation) || !id(invalidation.observed_at)
        || !["configuration_changed", "native_context_changed", "compacted", "operation_unconfirmed"].includes(String(invalidation.reason))) return undefined;
  } else if (invalidation != null) return undefined;
  const sample = value.sample;
  if (value.validity === "valid" && !record(sample)) return undefined;
  if (sample != null) {
    if (!record(sample) || !id(sample.backend) || !id(sample.native_session_id) || !maybeId(sample.actual_model)
        || !target(sample.binding) || !count(sample.binding.binding_revision) || !id(sample.observed_at)
        || !record(sample.source) || !id(sample.source.request_id) || !id(sample.source.attempt_id)
        || !maybeId(sample.source.connection_generation) || !maybeId(sample.source.native_turn_id)
        || sample.source.native_sample_id != null && !id(sample.source.native_sample_id)
        || !count(sample.source.observation_seq) || sample.source.observation_seq === 0
        || sample.used_tokens != null && !count(sample.used_tokens)
        || sample.capacity_tokens != null && (!count(sample.capacity_tokens) || sample.capacity_tokens === 0)
        || sample.used_ratio != null && (typeof sample.used_ratio !== "number" || !Number.isFinite(sample.used_ratio) || sample.used_ratio < 0)
        || sample.used_tokens == null && sample.used_ratio == null
        || !["codex_last_total", "codex_recomputed_estimate", "claude_last_request_input", "provider_ratio"].includes(String(sample.basis))
        || sample.capacity_basis != null && sample.capacity_basis !== "runtime_model_window") return undefined;
    if (value.validity === "valid" && (sample.native_session_id !== value.binding_target.native_session_id
        || sample.binding.native_session_id !== value.binding_target.native_session_id
        || sample.binding.effective_config_key !== value.binding_target.effective_config_key
        || sample.binding.agent_settings_revision !== value.binding_target.agent_settings_revision
        || sample.binding.binding_revision !== value.binding_revision)) return undefined;
  }
  return value as ContextState;
}

export function contextDisplay(state: ContextState | null | undefined, historical = false) {
  const sample = state?.sample;
  let used: number | null = null;
  if (!historical && state?.validity === "valid" && sample) {
    if (typeof sample.used_tokens === "number" && Number.isFinite(sample.used_tokens) && sample.used_tokens >= 0
        && typeof sample.capacity_tokens === "number" && Number.isFinite(sample.capacity_tokens) && sample.capacity_tokens > 0) {
      used = sample.used_tokens / sample.capacity_tokens;
    } else if (sample.basis === "provider_ratio" && typeof sample.used_ratio === "number"
        && Number.isFinite(sample.used_ratio) && sample.used_ratio >= 0) used = sample.used_ratio;
  }
  const remaining = used === null ? null : 1 - Math.min(1, used);
  return { remaining, percent: remaining === null ? "—" : `${Math.round(remaining * 100)}%`,
    tone: used === null || used < .7 ? "neutral" : used < .9 ? "amber" : "red" } as const;
}
