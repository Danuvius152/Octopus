import { createStore } from "zustand/vanilla";
import { useSessionStore } from "../stores/sessionStore";

type Kind = "application" | "chat";
const records = new Map<string, TextDraft>();
const prefix = "octopus.text-draft.v1:";
const saveFailure = "Text draft could not be saved in this tab. Keep a copy before refreshing.";
let cleanupError: string | null = null;
const user = () => {
  const state = useSessionStore.getState();
  return state.token ? state.identity?.user_id : null;
};
const storageKey = (owner: string, key: string) => prefix + JSON.stringify([owner, ...JSON.parse(key)]);

/** Clear all retired account entries, including Apps never opened this load. */
function clearAccounts(except?: string) {
  try {
    for (const key of Object.keys(sessionStorage)) {
      if (key.startsWith(prefix) && (!except || JSON.parse(key.slice(prefix.length))[0] !== except)) sessionStorage.removeItem(key);
    }
    cleanupError = null;
  } catch { cleanupError = saveFailure; }
}

/** A record identity retires on account change or confirmed deletion. */
export class TextDraft {
  readonly store = createStore(() => ({ text: "", version: 0, saveError: null as string | null }));
  private readonly key: string;
  private recoverable = true;
  constructor(key: string) { this.key = key; }
  current() { return records.get(this.key) === this; }
  edit(text: string) {
    if (!this.current()) return;
    this.recoverable = true;
    this.store.setState(state => ({ text, version: state.version + 1 }));
    this.save();
  }
  restore() {
    const owner = user();
    if (!owner || !this.current()) return;
    if (this.store.getState().version) { this.save(); return; }
    try {
      const text = sessionStorage.getItem(storageKey(owner, this.key)) ?? "";
      this.store.setState({ text, saveError: cleanupError });
    } catch { this.store.setState({ saveError: saveFailure }); }
  }
  private save() {
    const owner = user();
    if (!owner || !this.current()) return;
    try {
      const key = storageKey(owner, this.key), { text } = this.store.getState();
      if (this.recoverable && text) sessionStorage.setItem(key, text);
      else sessionStorage.removeItem(key);
      this.store.setState({ saveError: cleanupError });
    } catch { this.store.setState({ saveError: saveFailure }); }
  }
  /** Submitted text stays in memory, but is no longer an unsent refresh draft. */
  consume(version: number) {
    if (!this.current() || this.store.getState().version !== version) return;
    this.recoverable = false;
    this.save();
  }
  /** A retired composer cannot present a consumed command without its receipt context. */
  clearConsumed() {
    if (!this.recoverable) this.clear(this.store.getState().version);
  }
  clear(version: number): boolean {
    if (!this.current() || this.store.getState().version !== version) return false;
    this.edit("");
    return true;
  }
}

export const textDrafts = {
  get(kind: Kind, id: string): TextDraft {
    const key = JSON.stringify([kind, id]);
    let record = records.get(key);
    if (!record) { record = new TextDraft(key); records.set(key, record); record.restore(); }
    return record;
  },
  remove(kind: Kind, id: string) {
    const key = JSON.stringify([kind, id]);
    const record = records.get(key);
    record?.edit("");
    records.delete(key);
    // Confirmed deletion may arrive before /identity, or concern an unopened App.
    try {
      for (const saved of Object.keys(sessionStorage)) {
        if (!saved.startsWith(prefix)) continue;
        const [owner, savedKind, savedId] = JSON.parse(saved.slice(prefix.length));
        if (savedKind === kind && savedId === id && (!user() || owner === user())) sessionStorage.removeItem(saved);
      }
    } catch {
      cleanupError = saveFailure;
      for (const current of records.values()) current.store.setState({ saveError: saveFailure });
    }
  },
};

useSessionStore.subscribe((state, before) => {
  if (state.accountGeneration !== before.accountGeneration || state.token !== before.token) {
    clearAccounts();
    records.clear();
  } else if (state.identity?.user_id && state.identity.user_id !== before.identity?.user_id) {
    clearAccounts(state.identity.user_id);
    for (const record of records.values()) record.restore();
  }
});
