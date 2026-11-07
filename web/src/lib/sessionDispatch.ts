/** The server's in-memory dispatch snapshot; revision is per session. */
export interface DispatchState {
  generation: string;
  revision: number;
  paused: boolean;
  active_request_id: string | null;
  active_operation: "message" | "compact" | null;
  items: { request_id: string; operation: "message" | "compact"; source: string; summary: string; priority: boolean }[];
}

export interface ActiveAttempt { request_id: string; attempt_id: string }

/** Undefined means incomplete/invalid; only explicit null confirms absence. */
export function readActiveAttempt(value: unknown, dispatch: DispatchState): ActiveAttempt | null | undefined {
  if (value === null) return null;
  if (!value || typeof value !== "object" || !("request_id" in value) || !("attempt_id" in value) ||
      typeof value.request_id !== "string" || !value.request_id ||
      typeof value.attempt_id !== "string" || !value.attempt_id ||
      value.request_id !== dispatch.active_request_id) return undefined;
  return { request_id: value.request_id, attempt_id: value.attempt_id };
}

export function readDispatchState(value: unknown): DispatchState | null {
  if (!value || typeof value !== "object") return null;
  const data = value as DispatchState;
  if (typeof data.generation !== "string" || !data.generation ||
      !Number.isInteger(data.revision) || data.revision < 0 ||
      typeof data.paused !== "boolean" ||
      !(data.active_request_id === null || typeof data.active_request_id === "string") ||
      !(data.active_request_id === null ? data.active_operation === null : ["message", "compact"].includes(data.active_operation ?? "")) ||
      !Array.isArray(data.items) || !data.items.every((item) => item &&
        ["message", "compact"].includes(item.operation) &&
        typeof item.request_id === "string" && typeof item.source === "string" &&
        typeof item.summary === "string" && typeof item.priority === "boolean")) return null;
  return data;
}
