import type { components } from "../api/contracts";

type DelegationState = components["schemas"]["DelegationResponse"]["state"];
const states: Record<DelegationState, true> = {
  running: true, completed: true, failed: true, cancelled: true, interrupted: true,
};

export const isDelegationState = (value: unknown): value is DelegationState =>
  typeof value === "string" && Object.hasOwn(states, value);
