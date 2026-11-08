import { sessions } from "./sessionList";
import { textDrafts, type TextDraft } from "./textDrafts";
import { useSessionStore } from "../stores/sessionStore";

export const chatDraft = () => textDrafts.get("chat", "shared");
let prefill: { id: string | null; record: TextDraft; version: number; handled: boolean } | null = null;

/** One current selection, sharing the composer's life across view remounts. */
export function prefillChatDraft(record: TextDraft, id: string | null, prompt: string | null | undefined) {
  if (!prefill || prefill.id !== id || prefill.record !== record) {
    prefill = { id, record, version: record.store.getState().version, handled: false };
  }
  if (!id || !prompt || !useSessionStore.getState().identity?.user_id || prefill.handled) return;
  prefill.handled = true;
  const draft = record.store.getState();
  if (draft.version === prefill.version && !draft.text) record.edit(prompt);
}

// Deletion remains effective while ChatView is absent. Keep the shared record
// alive so a mounted composer can continue editing the next version.
sessions.onDeleted((_id, wasActive) => {
  if (!wasActive) return;
  const draft = chatDraft();
  draft.clear(draft.store.getState().version);
});
