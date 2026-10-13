import type { Message } from "../stores/sessionStore";

export interface TurnOutcome {
  status: "success" | "failed" | "interrupted" | "unknown";
  request_id?: string;
  message?: string;
  code?: string;
  provider_code?: string;
  recovery: Record<string, number>;
}

export interface TurnRecovery {
  phase: "diagnostic" | "retrying" | "recovered";
  request_id?: string;
  code?: string;
  message: string;
  attempt?: number;
  maximum?: number;
  delay?: number;
}

export type MessageInput = Omit<Message, "content"> & { content?: unknown };

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

function text(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

export function normalizeMessage(message: MessageInput): Message {
  const data = record(message.content);
  if (message.type === "result") {
    const valid = data.kind === "outcome" &&
      ["success", "failed", "interrupted"].includes(String(data.status));
    const recovery = Object.fromEntries(Object.entries(record(data.recovery))
      .filter(([, count]) => typeof count === "number" && Number.isInteger(count) && count >= 0)) as Record<string, number>;
    const outcome: TurnOutcome = message.outcome ?? {
      status: valid ? data.status as TurnOutcome["status"] : message.is_error ? "failed" : "unknown",
      request_id: text(data.request_id),
      message: text(data.message) ?? text(message.content),
      code: text(data.code), provider_code: text(data.provider_code), recovery,
    };
    return { ...message, content: outcome.message, outcome };
  }
  if (message.type === "recovery") {
    const recovery: TurnRecovery = message.recovery ?? {
      phase: data.phase === "retrying" || data.phase === "recovered" ? data.phase : "diagnostic",
      request_id: text(data.request_id), code: text(data.code),
      message: text(data.message) ?? text(message.content) ?? "Backend notification",
      attempt: typeof data.attempt === "number" ? data.attempt : undefined,
      maximum: typeof data.maximum === "number" ? data.maximum : undefined,
      delay: typeof data.delay === "number" ? data.delay : undefined,
    };
    return { ...message, content: recovery.message, recovery };
  }
  return { ...message, content: text(message.content) };
}
