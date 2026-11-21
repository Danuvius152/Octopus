import type { BgTaskResponse } from "../api";
import { useSessionStore, type BgTask } from "../stores/sessionStore";

export type BgTaskOutput = Pick<BgTaskResponse,
  "id" | "session_id" | "started_at" | "status" | "command" | "stdout" | "stderr" |
  "truncated" | "delivery_disposition" | "delivery_error"
>;

export function bgTaskOutputLine(preview: string | undefined): string {
  return preview?.trimEnd().split("\n").at(-1) ?? "";
}

const MAX_CACHED_BYTES = 8 * 1024 * 1024;
const MAX_CACHED_TASKS = 16;
let cache = new Map<string, { output: BgTaskOutput; bytes: number }>();
let usedBytes = 0;

function remove(key: string) {
  usedBytes -= cache.get(key)?.bytes ?? 0;
  cache.delete(key);
}

useSessionStore.subscribe((next, previous) => {
  if (next.accountGeneration !== previous.accountGeneration || next.token !== previous.token ||
      next.dispatchGeneration !== previous.dispatchGeneration ||
      next.activeSessionId !== previous.activeSessionId || next.mainView !== previous.mainView) {
    cache = new Map();
    usedBytes = 0;
  } else if (next.bgTasks !== previous.bgTasks) {
    for (const [key, { output }] of cache) {
      const session = output.session_id;
      if (next.bgTasks[session] !== previous.bgTasks[session] &&
          !next.bgTasks[session]?.some((task) => task.id === output.id && task.started_at === output.started_at)) remove(key);
    }
  }
});

const cacheKey = (sessionId: string, taskId: string) => JSON.stringify([sessionId, taskId]);

export function cachedBgTaskOutput(task: Pick<BgTask, "session_id" | "id" | "started_at">) {
  const output = cache.get(cacheKey(task.session_id, task.id))?.output;
  return output?.started_at === task.started_at ? output : undefined;
}

/** One view owns full-output admission and terminal reuse. */
export function bgTaskOutputOwner(task: BgTask) {
  const original = cache;
  const current = () => {
    const state = useSessionStore.getState();
    return cache === original && state.mainView === "chat" &&
      (state.activeSessionId === null || state.activeSessionId === task.session_id) &&
      !!state.bgTasks[task.session_id]?.some((row) => row.id === task.id && row.started_at === task.started_at);
  };
  return {
    current,
    accept(row: BgTaskResponse): BgTaskOutput | undefined {
      if (!current()) return;
      const { id, session_id, started_at, status, command, stdout, stderr, truncated, delivery_disposition, delivery_error } = row;
      const output = { id, session_id, started_at, status, command, stdout, stderr, truncated, delivery_disposition, delivery_error };
      if (status !== "pending" && status !== "running") retainTerminalOutput(output);
      return output;
    },
  };
}

function retainTerminalOutput(output: BgTaskOutput) {
  const { stdout, stderr, command, delivery_error } = output;
  const key = cacheKey(output.session_id, output.id);
  const bytes = 2 * (stdout.length + stderr.length + command.length + (delivery_error?.length ?? 0));
  if (bytes > MAX_CACHED_BYTES) return;
  remove(key);
  for (const oldest of cache.keys()) {
    if (usedBytes + bytes <= MAX_CACHED_BYTES && cache.size < MAX_CACHED_TASKS) break;
    remove(oldest);
  }
  cache.set(key, { output, bytes });
  usedBytes += bytes;
}
