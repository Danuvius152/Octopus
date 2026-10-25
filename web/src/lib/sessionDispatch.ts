/** The server's in-memory dispatch snapshot; revision is per session. */
export interface DispatchState {
  generation: string;
  revision: number;
  paused: boolean;
  active_request_id: string | null;
  items: { request_id: string; source: string; summary: string; priority: boolean }[];
}

export function readDispatchState(value: unknown): DispatchState | null {
  if (!value || typeof value !== "object") return null;
  const data = value as DispatchState;
  if (typeof data.generation !== "string" || !data.generation ||
      !Number.isInteger(data.revision) || data.revision < 0 ||
      typeof data.paused !== "boolean" ||
      !(data.active_request_id === null || typeof data.active_request_id === "string") ||
      !Array.isArray(data.items) || !data.items.every((item) => item &&
        typeof item.request_id === "string" && typeof item.source === "string" &&
        typeof item.summary === "string" && typeof item.priority === "boolean")) return null;
  return data;
}
