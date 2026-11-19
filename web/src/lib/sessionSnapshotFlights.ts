import { useSessionStore } from "../stores/sessionStore";
import { sessions, type SessionObservation } from "./sessionList";

export type SnapshotStart = ReturnType<typeof useSessionStore.getState>;
export type DetailGeneration = () => string | undefined;
export interface DetailPair {
  accepted: Promise<Record<string, unknown> | null>;
  generation: DetailGeneration;
}

function acceptsPairedGeneration(observation: SessionObservation, generation: string | undefined): boolean {
  // A cold observation can be qualified by only the first recorded service.
  return (observation.scope.generation !== null || generation === undefined || observation.initialGeneration === generation) &&
    sessions.current(observation, generation);
}

export function qualifyPairedSnapshot(
  consumer: SessionObservation, source: SessionObservation, generation: string | undefined, original?: DetailGeneration,
) {
  if (!acceptsPairedGeneration(consumer, generation) || !acceptsPairedGeneration(source, generation) ||
      generation !== undefined && generation !== useSessionStore.getState().dispatchGeneration) return "obsolete";
  if (sessions.current(source) || original?.() === generation) return "current";
  // This consumer established a service that the detail paired with the
  // physical request did not confirm. Read the auxiliary data in that service.
  return "new-generation";
}

type Snapshot = { response: Promise<unknown>; finish: () => void };
interface Flight<T> {
  snapshot: T;
  observation: SessionObservation;
  users: number;
  release: () => void;
}

/** Share transport and its original merge basis until body settlement.
 * Each lease owns its eligibility and can stop waiting independently. */
export class SessionSnapshotFlights<T extends Snapshot> {
  private pending = new Map<string, Flight<T>>();

  acquire(
    sessionId: string, purpose: string, start: SnapshotStart, share: boolean,
    issue: (observation: SessionObservation, signal: AbortSignal) => T,
  ) {
    const consumer = sessions.observe(sessionId, start);
    const key = JSON.stringify([sessionId, purpose, consumer.observation.scope]);
    const pending = share ? this.pending.get(key) : undefined;
    const flight = pending && sessions.current(pending.observation) ? pending :
      this.issue(sessionId, key, start, share, issue);
    flight.users++;
    let cancel!: () => void;
    const cancelled = new Promise<undefined>((resolve) => { cancel = () => resolve(undefined); });
    let active = true;
    return {
      observation: consumer.observation,
      original: flight.observation,
      snapshot: flight.snapshot,
      active: () => active,
      wait: <R>(response: Promise<R>) => Promise.race([response, cancelled]),
      release: () => {
        if (!active) return;
        active = false;
        cancel();
        consumer.finish();
        if (--flight.users === 0) flight.release();
      },
    };
  }

  private issue(
    sessionId: string, key: string, start: SnapshotStart, share: boolean,
    issue: (observation: SessionObservation, signal: AbortSignal) => T,
  ): Flight<T> {
    const source = sessions.observe(sessionId, start);
    const controller = new AbortController();
    const snapshot = issue(source.observation, AbortSignal.any([controller.signal, AbortSignal.timeout(30_000)]));
    let settled = false;
    const withdraw = () => { if (this.pending.get(key) === flight) this.pending.delete(key); };
    const flight = { snapshot, observation: source.observation, users: 0, release: () => {
      withdraw();
      if (!settled) controller.abort();
      snapshot.finish();
      source.finish();
    } };
    if (share) this.pending.set(key, flight);
    const settle = () => { settled = true; withdraw(); };
    void snapshot.response.then(settle, settle);
    return flight;
  }
}
