/** Temporary ordering for pending Agent reads and writes. The store's agents
 * remain authoritative; these requests retain only IDs until their caller ends.
 */
export interface AgentContext {
  accountGeneration: number;
  dispatchGeneration: string | null;
}
export interface AgentRequest extends AgentContext {
  readonly order: number;
  readonly kind: "read" | "write";
}
const pending = new Map<AgentRequest, { changed: Set<string>; optimistic: Set<string> }>();
let order = 0;

function sameContext(a: AgentContext, b: AgentContext): boolean {
  return a.accountGeneration === b.accountGeneration && a.dispatchGeneration === b.dispatchGeneration;
}

export function beginAgentRequest(context: AgentContext, kind: AgentRequest["kind"] = "write"): AgentRequest {
  for (const request of pending.keys()) if (!sameContext(request, context)) pending.delete(request);
  if (!pending.size) order = 0;
  const changed = kind === "read" ? new Set([...pending.values()].flatMap((value) => [...value.optimistic])) : new Set<string>();
  const request = { accountGeneration: context.accountGeneration, dispatchGeneration: context.dispatchGeneration, order: ++order, kind };
  pending.set(request, { changed, optimistic: new Set() });
  return request;
}

export function endAgentRequest(request: AgentRequest): void {
  pending.delete(request);
}

export function agentRequestIsCurrent(request: AgentRequest, context: AgentContext): boolean {
  return pending.has(request) && sameContext(request, context);
}

export function canApplyAgent(id: string, request?: AgentRequest): boolean {
  return !request || (pending.has(request) && !pending.get(request)!.changed.has(id));
}

export function recordAgentSnapshot(request: AgentRequest): void {
  // A complete newer snapshot already supplies every unaffected ID, including
  // absence of IDs this browser never saw. Older reads have nothing to add.
  for (const other of pending.keys()) {
    if (other.kind === "read" && other.order < request.order) pending.delete(other);
  }
}

export function recordAgentChanges(ids: Iterable<string>, request?: AgentRequest, optimistic = false): void {
  const changed = [...ids];
  const own = request && pending.get(request);
  for (const id of changed) {
    if (optimistic) own?.optimistic.add(id);
    else own?.optimistic.delete(id);
  }
  for (const [other, state] of pending) {
    if (other === request) continue;
    // A write received during a read protects that ID regardless of which
    // request started first. Read receipts never suppress a pending write.
    if (!request || (other.kind === "read" && (request.kind === "write" || other.order < request.order)) ||
        (other.kind === "write" && request.kind === "write" && other.order < request.order)) {
      for (const id of changed) { state.changed.add(id); state.optimistic.delete(id); }
    }
  }
}
