/** Temporary ordering for pending Agent reads and writes. The store's agents
 * remain authoritative; these requests retain only IDs until their caller ends.
 */
export interface AgentContext {
  accountGeneration: number;
  dispatchGeneration: string | null;
}
export interface AgentRequest extends AgentContext {
  readonly order: number;
  readonly scope: "live" | "all";
}
const pending = new Map<AgentRequest, {
  changed: Map<string, number>;
  observed: Set<string>;
  confirmedOrder: Map<string, number>;
  optimistic: Set<string>;
  superseded: "live" | "all" | null;
  readStatusCurrent: boolean;
}>();
let order = 0;

function sameContext(a: AgentContext, b: AgentContext): boolean {
  return a.accountGeneration === b.accountGeneration && a.dispatchGeneration === b.dispatchGeneration;
}

export function beginAgentRequest(context: AgentContext, scope: AgentRequest["scope"] = "live"): AgentRequest {
  for (const request of pending.keys()) if (!sameContext(request, context)) pending.delete(request);
  if (!pending.size) order = 0;
  const request = { accountGeneration: context.accountGeneration, dispatchGeneration: context.dispatchGeneration, order: ++order, scope };
  pending.set(request, { changed: new Map(), observed: new Set(), confirmedOrder: new Map(), optimistic: new Set(), superseded: null, readStatusCurrent: true });
  return request;
}

export function endAgentRequest(request: AgentRequest): void {
  pending.delete(request);
}

export function agentRequestIsCurrent(request: AgentRequest, context: AgentContext): boolean {
  return pending.has(request) && sameContext(request, context);
}

export function canApplyAgent(id: string, request?: AgentRequest): boolean {
  return !request || (pending.has(request) && (pending.get(request)!.changed.get(id) ?? 0) <= request.order);
}

export function canApplyAgentObservation(id: string, request: AgentRequest): boolean {
  const state = pending.get(request);
  return !!state && !state.changed.has(id) && !state.observed.has(id);
}

export function recordAgentSnapshot(request: AgentRequest, scope: AgentRequest["scope"], observed: Iterable<string>): void {
  // A complete newer snapshot already supplies every unaffected ID, including
  // absence of IDs this browser never saw. Retire observations, not operations:
  // an independent business receipt can still publish its actual mutation.
  if (pending.get(request)?.superseded === "all") return;
  const ids = [...observed];
  for (const [other, state] of pending) {
    if (other.order >= request.order) continue;
    if (scope === "all") state.superseded = "all";
    else if (!state.superseded) state.superseded = "live";
    for (const id of ids) state.observed.add(id);
  }
}

export function agentSnapshotCovers(request: AgentRequest, scope: AgentRequest["scope"], archived: boolean): boolean {
  const state = pending.get(request);
  return !!state && state.superseded !== "all" && (archived ? scope === "all" : state.superseded !== "live");
}

/** Status follows the latest full-read intent; rows still merge per ID. */
export function beginAgentSnapshot(request: AgentRequest, scope: AgentRequest["scope"]): void {
  // A later GET is a new observation phase of the same operation. Earlier
  // completed reads cannot supersede it; real receipts still protect their
  // rows/order. Attached sorting lists never begin a new phase here.
  const state = pending.get(request);
  if (!state) return;
  state.observed.clear();
  state.superseded = null;
  if (scope !== "all") return;
  for (const [other, state] of pending) if (other.order < request.order) state.readStatusCurrent = false;
}

export function agentReadStatusIsCurrent(request: AgentRequest): boolean {
  return pending.get(request)?.readStatusCurrent === true;
}

/** A drag contributes order only; it cannot hide membership or row facts. */
export function recordAgentOrder(ids: Iterable<string>, request: AgentRequest): void {
  const own = pending.get(request);
  for (const id of ids) own?.optimistic.add(id);
}

export function finishAgentOrder(request: AgentRequest): void {
  pending.get(request)?.optimistic.clear();
}

export function agentOrderIsProtected(id: string, request: AgentRequest, observation = false): boolean {
  const confirmed = pending.get(request)?.confirmedOrder.get(id);
  return confirmed !== undefined && (observation || confirmed > request.order) ||
    [...pending].some(([other, state]) => other !== request && state.optimistic.has(id) &&
      (observation || other.order > request.order));
}

/** A success confirms only submitted positions. Transfer their protection to
 * overlapping requests, so it survives the sorting caller's finally. */
export function recordAgentOrderReceipt(ids: Iterable<string>, request: AgentRequest): void {
  const accepted = [...ids];
  // The response ends this operation's optimism even when a newer order
  // prevents every submitted position from publishing its confirmation.
  finishAgentOrder(request);
  for (const state of pending.values()) for (const id of accepted) {
    state.confirmedOrder.set(id, Math.max(state.confirmedOrder.get(id) ?? 0, request.order));
  }
}

export function recordAgentChanges(ids: Iterable<string>, request?: AgentRequest): void {
  const changed = [...ids];
  const own = request && pending.get(request);
  for (const id of changed) own?.optimistic.delete(id);
  for (const [other, state] of pending) {
    if (other === request) continue;
    // All overlapping snapshots preserve this receipt. Another business ack
    // is ordered by its original operation; observations never enter this map.
    const source = request?.order ?? Infinity;
    for (const id of changed) {
      state.changed.set(id, Math.max(state.changed.get(id) ?? 0, source));
      if (source > other.order) state.optimistic.delete(id);
    }
  }
}
