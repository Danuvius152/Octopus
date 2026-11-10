import { accountFetch } from "./accountFetch";
import { useSessionStore, type CredentialInfo } from "../stores/sessionStore";
import type { RefreshResult } from "./listState";

const API = `${window.location.origin}/api/credentials`;
const READ_TIMEOUT = 30_000;
type Identity = { account: number; token: string };
type Read = Identity & { controller: AbortController; result: Promise<RefreshResult> };
let reading: Read | undefined;

const identity = (): Identity => {
  const state = useSessionStore.getState();
  return { account: state.accountGeneration, token: state.token };
};
const current = (owner: Identity) => {
  const state = useSessionStore.getState();
  return owner.account === state.accountGeneration && owner.token === state.token;
};

function retireRead() {
  const previous = reading;
  reading = undefined;
  previous?.controller.abort();
}

useSessionStore.subscribe((state, previous) => {
  if (state.accountGeneration !== previous.accountGeneration || state.token !== previous.token) retireRead();
  else if (state.dispatchGeneration !== previous.dispatchGeneration && reading) void confirm();
});

function refresh(): Promise<RefreshResult> {
  if (reading) return reading.result;
  const owner = identity();
  if (!owner.token) return Promise.resolve({ state: "obsolete" });
  const controller = new AbortController();
  const read: Read = { ...owner, controller, result: Promise.resolve({ state: "pending" }) };
  reading = read;
  useSessionStore.setState((s) => ({ credentialRead: { ...s.credentialRead, loading: true, error: null } }));
  read.result = (async (): Promise<RefreshResult> => {
    let timeout: ReturnType<typeof setTimeout> | undefined;
    let aborted: (() => void) | undefined;
    try {
      const rows = await Promise.race([
        (async (): Promise<CredentialInfo[]> => {
          const response = await accountFetch(API, {
            headers: { Authorization: `Bearer ${owner.token}` }, signal: controller.signal,
          });
          if (!response.ok) throw new Error(`Could not read credential list (HTTP ${response.status})`);
          return response.json();
        })(),
        new Promise<never>((_, reject) => {
          aborted = () => reject(new Error("Credential list read was retired"));
          controller.signal.addEventListener("abort", aborted, { once: true });
          timeout = setTimeout(() => reject(new Error("Credential list read timed out")), READ_TIMEOUT);
        }),
      ]);
      if (reading !== read || !current(owner)) return { state: "obsolete" };
      useSessionStore.setState({ credentials: rows, credentialRead: { loaded: true, loading: false, stale: false, error: null } });
      return { state: "applied" };
    } catch (error) {
      if (reading !== read || !current(owner)) return { state: "obsolete" };
      const message = String(error);
      useSessionStore.setState((s) => ({ credentialRead: { ...s.credentialRead, loading: false, stale: true, error: message } }));
      return { state: "failed", message };
    } finally {
      clearTimeout(timeout);
      if (aborted) controller.signal.removeEventListener("abort", aborted);
      if (reading === read) reading = undefined;
      controller.abort();
    }
  })();
  return read.result;
}

function confirm() {
  retireRead();
  useSessionStore.setState((s) => ({ credentialRead: { ...s.credentialRead, stale: true } }));
  return refresh();
}

async function remove(id: string): Promise<void> {
  const owner = identity();
  try {
    const response = await accountFetch(`${API}/${encodeURIComponent(id)}`, {
      method: "DELETE", headers: { Authorization: `Bearer ${owner.token}` },
    });
    if (!current(owner)) return;
    if (!response.ok) throw new Error(`Could not delete credential (HTTP ${response.status})`);
    // Each acknowledgment changes the current projection, independent of other writes.
    retireRead();
    useSessionStore.setState((s) => ({ credentials: s.credentials.filter((row) => row.id !== id),
      credentialRead: { ...s.credentialRead, stale: true } }));
    void refresh();
  } catch (error) {
    if (current(owner)) useSessionStore.setState((s) => ({ credentialRead: { ...s.credentialRead, stale: true, error: String(error) } }));
  }
}

async function loginCompleted(): Promise<RefreshResult> {
  const owner = identity();
  let result = confirm();
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const expired = new Promise<RefreshResult>((resolve) => {
    timeout = setTimeout(() => resolve({ state: "failed", message: "Credential list confirmation timed out" }), READ_TIMEOUT);
  });
  try {
    // A later independent write can replace our observation. Follow its confirmation,
    // within one total deadline, without cancelling another consumer's shared read.
    while (current(owner)) {
      const value = await Promise.race([result, expired]);
      if (!current(owner)) return { state: "obsolete" };
      if (value.state !== "obsolete") return value;
      if (reading) result = reading.result;
      else {
        const state = useSessionStore.getState().credentialRead;
        return state.stale ? { state: "failed", message: state.error ?? "Credential list is not synchronized" } : { state: "applied" };
      }
    }
    return { state: "obsolete" };
  } finally {
    clearTimeout(timeout);
  }
}

export const modelCredentials = { refresh, remove, loginCompleted, authenticationExpired: confirm };
