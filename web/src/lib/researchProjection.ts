import { readResearchEvidence, type ResearchEvidence } from "./researchEvidence";
import type { WsEvent } from "./wsEvents";

const statuses = ["running", "completed", "failed", "cancelled", "interrupted"] as const;
const stages = ["queued", "processing", "processed", "failed", "recording_unconfirmed", "unknown"] as const;

type ResearchReceipt = {
  request_id: string;
  version: number;
  stage: (typeof stages)[number];
  error?: string | null;
  browser_confirmed_version?: number;
  browser_confirmed_at?: string;
};

export interface ResearchJob {
  id: string;
  session_id: string;
  question: string;
  status: (typeof statuses)[number];
  phase: string | null;
  detail?: string;
  created_at?: string;
  report_path?: string | null;
  counts?: Record<string, number>;
  sources?: string[];
  verified?: number;
  unverified?: number;
  evidence?: ResearchEvidence | null;
  evidence_state?: "available" | "unavailable";
  delivery_receipt?: ResearchReceipt | null;
  error?: string | null;
  injection_status?: "pending" | "delivered" | "failed" | "unknown";
}

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const id = (value: unknown): value is string => typeof value === "string" && value.length > 0;
const integer = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

export function isResearchReceipt(value: unknown): value is ResearchReceipt {
  return record(value) && id(value.request_id) && integer(value.version) && value.version > 0 &&
    stages.some((stage) => stage === value.stage) && (value.error == null || typeof value.error === "string") &&
    (value.browser_confirmed_version === undefined || integer(value.browser_confirmed_version) && value.browser_confirmed_version <= value.version) &&
    (value.browser_confirmed_at === undefined || typeof value.browser_confirmed_at === "string");
}

export function isResearchJob(value: unknown, sessionId: string): value is ResearchJob {
  return record(value) && id(value.id) && value.session_id === sessionId && id(value.question) &&
    statuses.some((status) => status === value.status) && (value.phase === null || typeof value.phase === "string") &&
    (value.delivery_receipt == null || isResearchReceipt(value.delivery_receipt));
}

type ResearchPatch = Omit<Partial<ResearchJob>, "evidence" | "evidence_state"> & { evidence?: unknown; evidence_state?: string };
export type ResearchEvent = Extract<WsEvent, { type: "research_started" | "research_progress" | "research_completed" | "research_failed" | "research_delivery" }>;
type ResearchSource = { kind: "creation" | "event" } | { kind: "snapshot" | "control"; baseline?: ResearchJob; advanceOnly?: boolean };

const progressFields = ["phase", "detail", "counts"] as const;
const reportFields = ["report_path", "sources", "verified", "unverified"] as const;
type ObservedField = (typeof progressFields)[number] | (typeof reportFields)[number] | "evidence" | "injection_status" | "error";
// These marks follow the sole visible record. In-flight reads need only know
// which unversioned fields a live update supplied since they started.
const observations = new WeakMap<ResearchJob, {
  arrival?: symbol;
  fields: Partial<Record<ObservedField, symbol>>;
  confirmedByDetail?: boolean;
}>();

export function researchChangedSince(job: ResearchJob, baseline: ResearchJob): boolean {
  return observations.get(job)?.arrival !== observations.get(baseline)?.arrival;
}

export function confirmResearchDetail(job: ResearchJob): ResearchJob {
  observations.set(job, { fields: {}, ...observations.get(job), confirmedByDetail: true });
  return job;
}

export function researchConfirmedByDetail(job: ResearchJob): boolean {
  return observations.get(job)?.confirmedByDetail === true;
}

function evidenceFields(patch: ResearchPatch) {
  if (patch.evidence === undefined && patch.evidence_state === undefined) return undefined;
  const evidence = patch.evidence_state === undefined || patch.evidence_state === "available" ? readResearchEvidence(patch.evidence) : null;
  return { evidence, evidence_state: evidence ? "available" as const : "unavailable" as const };
}

export function initializeResearchJob(job: ResearchJob, live = false): ResearchJob {
  const added = { ...job, ...evidenceFields(job), ...(job.delivery_receipt && { injection_status: "delivered" as const }) };
  if (live) observations.set(added, { arrival: Symbol(), fields: {} });
  return added;
}

export function researchEventPatch(event: Exclude<ResearchEvent, { type: "research_started" }>): ResearchPatch {
  switch (event.type) {
    case "research_progress":
      return { phase: event.phase, detail: event.detail, counts: event.counts };
    case "research_completed":
      return { status: "completed", phase: "done", report_path: event.report_path, sources: event.sources,
        verified: event.verified, unverified: event.unverified, injection_status: event.injection_status ?? "pending",
        evidence: event.evidence, evidence_state: event.evidence_state };
    case "research_failed":
      return { status: event.status ?? "failed", error: event.error };
    case "research_delivery":
      return { injection_status: event.injection_status, error: event.error, delivery_receipt: event.delivery_receipt };
  }
}

function keepResearchConfirmation(receipt: ResearchReceipt, observed: ResearchReceipt): ResearchReceipt {
  const version = observed.browser_confirmed_version;
  if (version === undefined || version <= (receipt.browser_confirmed_version ?? 0)) return receipt;
  return { ...receipt, browser_confirmed_version: version, browser_confirmed_at: observed.browser_confirmed_at };
}

export function confirmResearchJobSeen(job: ResearchJob, receipt: ResearchReceipt): ResearchJob {
  const before = job.delivery_receipt;
  if (!before || before.request_id !== receipt.request_id || before.version !== receipt.version ||
      receipt.browser_confirmed_version !== before.version) return job;
  const delivery_receipt = keepResearchConfirmation(before, receipt);
  if (delivery_receipt === before) return job;
  const updated = { ...job, delivery_receipt };
  const observation = observations.get(job);
  if (observation) observations.set(updated, observation);
  return updated;
}

export function mergeResearchJob(previous: ResearchJob, patch: ResearchPatch, source: ResearchSource): ResearchJob {
  const observed = observations.get(previous);
  if (source.kind === "creation") {
    const updated = { ...previous };
    for (const field of ["question", "created_at"] as const) {
      if (!previous[field] && patch[field] !== undefined) updated[field] = patch[field];
    }
    if (previous.status === "completed" && patch.status === "completed" && previous.report_path === undefined) {
      updated.report_path = patch.report_path;
    }
    if (observed) observations.set(updated, observed);
    return updated;
  }

  if (previous.status !== "running" && patch.status !== undefined && previous.status !== patch.status) return previous;
  const updated = { ...previous, status: patch.status ?? previous.status };
  const completion = previous.status === "running" && updated.status !== "running";
  const completionEvent = source.kind === "event" && patch.status === "completed";
  const baseline = "baseline" in source ? source.baseline : undefined;
  const before = baseline && observations.get(baseline);
  const advanceOnly = source.kind === "snapshot" && source.advanceOnly;
  const arrival = Symbol();
  const fields = { ...observed?.fields };
  let observedField = false;
  const mark = (field: ObservedField) => {
    if (source.kind === "snapshot") return;
    fields[field] = arrival;
    observedField = true;
  };
  const canWrite = (field: ObservedField, value: unknown) => source.kind === "event" ||
    (observed?.fields[field] === before?.fields[field] && (!advanceOnly || value === undefined));
  const apply = (field: Exclude<ObservedField, "evidence">, firstCompletion = false) => {
    const value = patch[field];
    const prior = previous[field];
    const pendingPlaceholder = previous.injection_status === "pending" &&
      (field === "injection_status" || field === "error" && prior === null);
    if (value === undefined) {
      if (firstCompletion) delete updated[field];
    } else if (firstCompletion || canWrite(field, pendingPlaceholder ? undefined : prior)) {
      Object.assign(updated, { [field]: value });
      mark(field);
    }
  };

  if (source.kind === "snapshot" && patch.created_at !== undefined && previous.created_at === undefined) updated.created_at = patch.created_at;
  if (updated.status === "running") {
    for (const field of progressFields) apply(field);
  } else if (completion) {
    updated.phase = patch.phase ?? previous.phase;
  }

  if (patch.status === "completed") {
    // Running snapshots can describe missing material before the report exists.
    // Those values carry no authority over the first completed projection.
    for (const field of reportFields) {
      if (completion || !completionEvent || previous[field] === undefined) apply(field, completion);
    }
    const evidence = evidenceFields(patch);
    // The completion frame samples the files at commit. It can fill a gap or
    // report unavailable evidence, but only a read can restore observed absence.
    const suppliesEvidence = !completionEvent || previous.evidence_state === undefined ||
      (previous.evidence_state === "available" && evidence?.evidence === null);
    if (evidence && (completion || suppliesEvidence && canWrite("evidence", previous.evidence_state))) {
      Object.assign(updated, evidence);
      mark("evidence");
    } else if (completion) {
      delete updated.evidence;
      delete updated.evidence_state;
    }
  }

  const bound = previous.delivery_receipt;
  const incoming = patch.delivery_receipt;
  const sameRequest = bound?.request_id === incoming?.request_id;
  const canReplaceBinding = source.kind === "snapshot" && !advanceOnly && bound?.request_id === baseline?.delivery_receipt?.request_id;
  if (incoming && (!bound || sameRequest || canReplaceBinding)) {
    updated.delivery_receipt = bound && sameRequest
      ? incoming.version > bound.version ? keepResearchConfirmation(incoming, bound) : keepResearchConfirmation(bound, incoming)
      : incoming;
  } else if (incoming === null && !bound) {
    updated.delivery_receipt = null;
  }

  const currentDelivery = !bound || (incoming
    ? updated.delivery_receipt?.request_id === incoming.request_id && (!sameRequest || incoming.version >= bound.version)
    : patch.injection_status === "delivered");
  if (currentDelivery) {
    // Pending and its empty diagnosis initialize the display. They carry no
    // proof of admission and must not stamp over a read already in flight.
    if (patch.injection_status === "pending") {
      updated.injection_status ??= "pending";
    } else {
      apply("injection_status");
    }
    if (patch.injection_status === "pending" && patch.error === null) updated.error ??= null;
    else apply("error");
  }
  if (updated.delivery_receipt) updated.injection_status = "delivered";

  if (source.kind !== "snapshot" && (completion || observedField || updated.delivery_receipt !== bound ||
      updated.injection_status !== previous.injection_status)) {
    observations.set(updated, { ...observed, arrival, fields });
  } else if (source.kind === "event") return previous;
  else if (observed) observations.set(updated, observed);
  return updated;
}
