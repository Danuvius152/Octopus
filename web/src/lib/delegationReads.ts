import { accountFetch } from "./accountFetch";
import { sessions } from "./sessionList";
import { readDispatchState } from "./sessionDispatch";
import { TaskSnapshotReads } from "./snapshotReads";
import { qualifyPairedSnapshot, type DetailGeneration, type DetailPair } from "./sessionSnapshotFlights";
import { isDelegationState } from "./delegationState";
import { useSessionStore, type Delegation } from "../stores/sessionStore";

type Work = Pick<Delegation, "delegation_id" | "work_id">;
type Result = { rows: Delegation[] } | { error: unknown };
type Lifetime = ReturnType<typeof sessions.observe>;
interface Demand {
  lifetime: Lifetime; work?: Work; pair?: DetailPair;
  status?: (unavailable: boolean) => void;
  cancelReplacement?: () => void;
  finish: (rows: Delegation[] | null, error?: unknown) => void;
  cancelled: Promise<null>; cancel: () => void;
}
interface Flight {
  start: ReturnType<typeof useSessionStore.getState>; lifetime: Lifetime;
  order: ReturnType<TaskSnapshotReads["begin"]>; controller: AbortController;
  generation?: DetailGeneration;
  response: Promise<Result>; users: Set<Demand>; startedWith: Set<Demand>;
  settled: boolean; published: boolean;
}

// Only observation/scheduling state lives here. The store owns all round facts.
const pending = new TaskSnapshotReads();
const coordinators = new Map<string, DelegationReads>();

class DelegationReads {
  private demands = new Set<Demand>();
  private flights = new Set<Flight>();
  private ordinary?: Flight;
  private timer?: ReturnType<typeof setTimeout>;
  private failures = 0;
  private key: string;
  private sessionId: string;

  constructor(key: string, sessionId: string) { this.key = key; this.sessionId = sessionId; }

  current() { return [...this.demands].some((demand) => sessions.current(demand.lifetime.observation)); }

  retire() { for (const demand of [...this.demands]) demand.finish(null); }

  observe(pair?: DetailPair, work?: Work, status?: Demand["status"], fresh = false) {
    let resolve!: (rows: Delegation[] | null) => void, reject!: (error: unknown) => void;
    const done = new Promise<Delegation[] | null>((yes, no) => { resolve = yes; reject = no; });
    if (work) void done.catch(() => {});
    let cancel!: () => void;
    const cancelled = new Promise<null>((yes) => { cancel = () => yes(null); });
    const demand: Demand = {
      lifetime: sessions.observe(this.sessionId), pair, work, status, cancelled, cancel,
      finish: (rows, error) => { if (error !== undefined) reject(error); else resolve(rows); this.remove(demand); },
    };
    this.demands.add(demand);
    if (fresh) this.attach(demand, this.start(true, pair?.generation));
    else if (this.ordinary && sessions.current(this.ordinary.lifetime.observation)) this.attach(demand, this.ordinary);
    else if (this.failures >= 3) {
      status?.(true);
      if (!work) demand.finish(null, new Error("Automatic delegation refresh is paused. Refresh status explicitly."));
    } else if (!work || this.timer === undefined) this.attach(demand, this.start(false, pair?.generation));
    return { done, cancel: () => demand.finish(null) };
  }

  private remove(demand: Demand) {
    if (!this.demands.delete(demand)) return;
    demand.cancelReplacement?.();
    demand.cancel(); demand.lifetime.finish();
    for (const flight of this.flights) this.abortUnused(flight);
    if (!this.demands.size) {
      if (this.timer !== undefined) clearTimeout(this.timer);
      this.timer = undefined;
      if (coordinators.get(this.key) === this) coordinators.delete(this.key);
    }
  }

  private abortUnused(flight: Flight) {
    if ([...flight.users].some((user) => this.demands.has(user))) return;
    // The coordinator can outlive one flight, for example when a forced
    // read still has a reader. Withdraw admission before abort can settle.
    if (this.ordinary === flight) this.ordinary = undefined;
    if (!flight.settled) flight.controller.abort();
  }

  private start(fresh = false, generation?: DetailGeneration): Flight {
    if (this.timer !== undefined) clearTimeout(this.timer);
    this.timer = undefined;
    const start = useSessionStore.getState();
    const lifetime = sessions.observe(this.sessionId, start);
    const controller = new AbortController();
    const response = accountFetch(`${window.location.origin}/api/sessions/${encodeURIComponent(this.sessionId)}/delegations`, {
      headers: { Authorization: `Bearer ${start.token}` }, signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30_000)]),
    }).then(async (res) => {
      if (!res.ok) throw new Error(`Could not load delegations (HTTP ${res.status}).`);
      const rows: unknown = await res.json();
      if (!Array.isArray(rows) || rows.some((row) => !row || typeof row !== "object" ||
          typeof row.delegation_id !== "string" || typeof row.work_id !== "string" ||
          !isDelegationState(row.state))) {
        throw new Error("Could not read delegations.");
      }
      return { rows: rows as Delegation[] };
    }).catch((error: unknown) => ({ error }));
    const flight: Flight = { start, lifetime, controller, generation, response, order: pending.begin(this.sessionId),
      users: new Set(), startedWith: new Set(this.demands), settled: false, published: false };
    this.flights.add(flight);
    if (!fresh) this.ordinary = flight;
    // Detail qualification is consumer-local and never holds up transport or
    // the next card refresh. Keep ordering until those consumers settle.
    void response.then((result) => {
      flight.settled = true;
      if (this.ordinary === flight) this.ordinary = undefined;
      if (!controller.signal.aborted && sessions.current(lifetime.observation) && flight.order.claim("health")) {
        if ("error" in result) this.failures++;
        else this.failures = 0;
      }
      this.release(flight);
    });
    for (const demand of this.demands) if (demand.work) this.attach(demand, flight);
    return flight;
  }

  private attach(demand: Demand, flight: Flight) {
    if (flight.users.has(demand)) return;
    flight.users.add(demand);
    void (async () => {
      try {
        const detail = demand.pair ? await Promise.race([demand.pair.accepted.catch(() => null), demand.cancelled]) : undefined;
        if (!this.demands.has(demand)) return;
        const generation = detail ? readDispatchState(detail.dispatch_state)?.generation : undefined;
        const qualify = () => qualifyPairedSnapshot(demand.lifetime.observation, flight.lifetime.observation, generation, flight.generation);
        const qualification = qualify();
        if (demand.pair && !detail || qualification === "obsolete") { demand.finish(null); return; }
        if (qualification === "new-generation") {
          flight.users.delete(demand);
          this.abortUnused(flight); this.release(flight);
          const next = coordinator(this.sessionId).observe();
          demand.cancelReplacement = next.cancel;
          try { demand.finish(await next.done); }
          catch (error) { demand.finish(null, error); }
          return;
        }
        const result = await Promise.race([flight.response, demand.cancelled]);
        if (!result || !this.demands.has(demand)) return;
        if (qualify() !== "current") { demand.finish(null); return; }
        if ("error" in result) {
          if (demand.work) demand.status?.(this.failures >= 3);
          else demand.finish(null, result.error);
          return;
        }
        const latest = flight.order.claim("collection");
        // One eligible consumer authorizes the shared result's single merge.
        // Other consumers still independently qualify and decide their demand.
        if (!flight.published) {
          useSessionStore.getState().setDelegations(this.sessionId, result.rows, flight.start.delegations[this.sessionId] || [], !latest);
          flight.published = true;
        }
        demand.status?.(this.failures >= 3);
        if (!demand.work) demand.finish(result.rows);
        else if (latest && flight.startedWith.has(demand) && !useSessionStore.getState().delegations[this.sessionId]?.some((row) =>
          row.delegation_id === demand.work!.delegation_id && row.work_id === demand.work!.work_id && row.state === "running")) {
          demand.finish(result.rows);
        }
      } finally {
        flight.users.delete(demand);
        this.release(flight);
        this.schedule();
      }
    })();
  }

  private release(flight: Flight) {
    if (!flight.settled || flight.users.size) return;
    flight.order.finish(); flight.lifetime.finish(); this.flights.delete(flight);
  }

  private schedule() {
    if (this.ordinary || this.timer !== undefined || this.failures >= 3 || ![...this.demands].some((demand) => demand.work)) return;
    this.timer = setTimeout(() => {
      this.timer = undefined;
      for (const demand of [...this.demands]) {
        if (demand.work && !sessions.current(demand.lifetime.observation)) demand.finish(null);
      }
      const watchers = [...this.demands].filter((demand) => demand.work);
      if (!watchers.length) return;
      const flight = this.start();
      for (const demand of watchers) this.attach(demand, flight);
    }, 2000);
  }
}

function coordinator(sessionId: string) {
  const key = JSON.stringify([sessionId, sessions.capture().scope]);
  let reads = coordinators.get(key);
  if (reads && !reads.current()) { reads.retire(); reads = undefined; }
  if (!reads) { reads = new DelegationReads(key, sessionId); coordinators.set(key, reads); }
  return reads;
}

/** One-shot hydration shares transport, retaining its accepted-detail gate. */
export function observeDelegations(sessionId: string, pair?: DetailPair, options?: { fresh?: boolean }) {
  return coordinator(sessionId).observe(pair, undefined, undefined, options?.fresh);
}

/** Exact-work demand ends independently of other cards or the server's work. */
export function watchDelegation(sessionId: string, work: Work, status: (unavailable: boolean) => void) {
  return coordinator(sessionId).observe(undefined, work, status);
}

/** Confirmation starts after the control, even with an older GET in flight. */
export function refreshDelegations(sessionId: string) {
  return coordinator(sessionId).observe(undefined, undefined, undefined, true);
}
