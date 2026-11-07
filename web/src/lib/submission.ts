import { accountFetch } from "./accountFetch";

/** One HTTP receipt observation, including its body; timeout never resends work. */
export async function readSubmission<T>(url: string, init: RequestInit): Promise<T> {
  const controller = new AbortController();
  const cancelled = new Promise<never>((_resolve, reject) => {
    controller.signal.addEventListener("abort", () => reject(controller.signal.reason), { once: true });
  });
  const cancel = () => controller.abort(init.signal?.reason);
  init.signal?.addEventListener("abort", cancel, { once: true });
  const deadline = setTimeout(() => controller.abort(new DOMException("Submission confirmation timed out.", "TimeoutError")), 30_000);
  try {
    if (init.signal?.aborted) cancel();
    return await Promise.race([cancelled, (async () => {
      controller.signal.throwIfAborted();
      const response = await accountFetch(url, { ...init, signal: controller.signal });
      if (!response.ok) throw new Error(`Submission confirmation failed (${response.status}).`);
      return await response.json() as T;
    })()]);
  } finally {
    clearTimeout(deadline);
    init.signal?.removeEventListener("abort", cancel);
    controller.abort();
  }
}

export interface SubmissionReply {
  accepted: boolean;
  message?: string;
  code?: string;
  requestId?: string;
  state?: "unknown";
  clientRequestId?: string;
  execution?: import("../api").ClientSubmissionStatus["execution"];
}

/** Captured before sending; a later view cannot retarget the answer. */
export interface QuestionCommandTarget {
  clientCommandId: string;
  accountGeneration: number;
  generation: string | null;
  requestId: string | null;
  attemptId: string | null;
}

export interface ControlCommandStatus {
  id: string;
  sessionId: string;
  operation: string;
  state: "pending" | "failed" | "unknown";
  message?: string;
}

/** Correlation only: authority always comes from the authenticated socket. */
export function newSubmissionId(): string {
  const crypto = globalThis.crypto;
  if (typeof crypto?.randomUUID === "function") return crypto.randomUUID();
  if (typeof crypto?.getRandomValues === "function") {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  throw new Error("Secure randomness is unavailable. Open Octopus over HTTPS or localhost before sending work.");
}
