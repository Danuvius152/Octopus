/** Observable read state shared by the two domain lists, without owning data. */
export interface ListReadState {
  loaded: boolean;
  loading: boolean;
  stale: boolean;
  error: string | null;
}

export const unreadList = (): ListReadState => ({ loaded: false, loading: false, stale: true, error: null });

export type RefreshResult =
  | { state: "applied" | "pending" | "obsolete" }
  | { state: "failed"; message: string };

export class ListWriteError extends Error {
  readonly outcome: "refused" | "unknown" | "obsolete";
  constructor(message: string, outcome: ListWriteError["outcome"]) {
    super(outcome === "unknown" ? `${message}. Outcome unknown; refresh before trying again.` : message);
    this.outcome = outcome;
  }
}
