export interface SubmissionReply {
  accepted: boolean;
  message?: string;
  code?: string;
  requestId?: string;
}

let sequence = 0;

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
  // Older HTTP browsers may have no Web Crypto at all. The counter prevents
  // collisions within this page, even with a coarse clock or random source.
  return `submission-${Date.now().toString(36)}-${(++sequence).toString(36)}-${Math.random().toString(36).slice(2)}`;
}
