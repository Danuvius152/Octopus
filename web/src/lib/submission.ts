import { accountFetch } from "./accountFetch";
import type { ClientSubmissionStatus } from "../api";
import type { components } from "../api/contracts";

type HttpSubmissionReceipt = ClientSubmissionStatus | components["schemas"]["ClientSubmissionRefusal"];

/** One HTTP receipt observation, including its body; timeout never resends work. */
export async function readSubmission(url: string, init: RequestInit,
  original: { sessionId: string; clientId: string; operation: "message" | "compact" }): Promise<SubmissionReply> {
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
      const receipt = await response.json() as HttpSubmissionReceipt | null;
      if (!receipt || receipt.session_id !== original.sessionId || receipt.client_request_id !== original.clientId ||
          typeof receipt.accepted !== "boolean" ||
          ("operation" in receipt ? receipt.operation !== original.operation : receipt.accepted || init.method === "GET")) {
        throw new Error("The receipt does not identify the original submission.");
      }
      return submissionReply(receipt);
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

export function submissionReply(status: HttpSubmissionReceipt): SubmissionReply {
  return { accepted: status.accepted, execution: "execution" in status ? status.execution : undefined, state: status.state ?? undefined,
    message: status.message ?? undefined, code: status.code ?? undefined,
    clientRequestId: status.client_request_id, requestId: "request_id" in status ? status.request_id : undefined };
}

/** Admission and execution are separate facts for both direct and GET receipts. */
export function classifySubmission(
  reply: { accepted: boolean; state?: "unknown" | null; execution?: SubmissionReply["execution"] } | void,
  operation: "message" | "compact" | "continuation",
): "unknown" | "recoverable" | "readonly" | "rejected" | "settled" {
  if (!reply || reply.state === "unknown" || reply.execution?.state === "unknown") return "unknown";
  if (reply.execution?.state === "withdrawn") {
    return operation !== "compact" && reply.execution.recoverable_input ? "recoverable" : "readonly";
  }
  if (reply.execution?.state === "pending" || reply.execution?.state === "running" || reply.execution?.state === "done" ||
      !reply.execution && reply.accepted) return "settled";
  return reply.execution ? "unknown" : "rejected";
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
