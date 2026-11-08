/** The same bounded material is used in the report, synthesis and task card. */
export interface ResearchEvidence {
  version: 1;
  searches: { angle: string; status: "findings" | "empty" | "failed"; findings: number; reason: string }[];
  claims: { claim: string; url: string; angle: string; status: "verified" | "refuted" | "unverified" | "failed";
    support: number; refute: number; unverified: number; failed: number;
    votes: { status: "support" | "refute" | "unverified" | "failed"; note: string; conditions: string; sources: string[] }[] }[];
  votes_per_claim: number;
  scope_note: string;
  omitted: boolean;
  notices: string[];
}

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown, limit: number): value is string => typeof value === "string" && value.length <= limit;
const count = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const list = (value: unknown, limit: number, valid: (item: unknown) => boolean) => Array.isArray(value) && value.length <= limit && value.every(valid);

export function readResearchEvidence(value: unknown): ResearchEvidence | null {
  if (!record(value) || value.version !== 1 || !count(value.votes_per_claim) || !text(value.scope_note, 256) ||
      typeof value.omitted !== "boolean" || !list(value.notices, 8, (note) => text(note, 512)) ||
      !list(value.searches, 5, (s) => record(s) && text(s.angle, 256) &&
        ["findings", "empty", "failed"].includes(String(s.status)) && count(s.findings) && text(s.reason, 256)) ||
      !list(value.claims, 12, (c) => record(c) && text(c.claim, 512) && text(c.url, 512) && text(c.angle, 256) &&
        ["verified", "refuted", "unverified", "failed"].includes(String(c.status)) &&
        [c.support, c.refute, c.unverified, c.failed].every(count) &&
        (c.support as number) + (c.refute as number) + (c.unverified as number) + (c.failed as number) === value.votes_per_claim &&
        list(c.votes, 8, (v) => record(v) && ["support", "refute", "unverified", "failed"].includes(String(v.status)) &&
          text(v.note, 256) && text(v.conditions, 256) && list(v.sources, 3, (url) => text(url, 512))))) return null;
  return value as unknown as ResearchEvidence;
}
